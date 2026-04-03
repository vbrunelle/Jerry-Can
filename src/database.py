"""Database module for storing Quebec fuel price data.

Uses Delta Lake for change-based (Slowly Changing Value) storage: only
price differences between consecutive snapshots are persisted via the
``merge`` operation, drastically reducing storage volume while providing
built-in time travel for full state reconstruction at any recorded point.

See ``docs/architecture-decision.md`` for the rationale behind this design.
"""

import logging
import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from deltalake import DeltaTable, write_deltalake

from src.config import DATABASE_PATH

logger = logging.getLogger(__name__)

# Delta table lives in a directory next to the old SQLite path.
# E.g. DATABASE_PATH="fuel_prices.db" -> DATA_DIR="data/station_prices"
DATA_DIR = os.getenv(
    "DELTA_TABLE_PATH",
    os.path.join(os.path.dirname(DATABASE_PATH) or ".", "data", "station_prices"),
)

MERGE_PREDICATE = (
    "target.station_id = source.station_id "
    "AND target.fuel_type = source.fuel_type"
)

SCHEMA_COLUMNS = [
    "station_id",
    "station_name",
    "address",
    "city",
    "region",
    "latitude",
    "longitude",
    "fuel_type",
    "price",
]


def _table_exists(table_path: str = DATA_DIR) -> bool:
    """Return True if a Delta table already exists at *table_path*."""
    try:
        DeltaTable(table_path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _records_to_dataframe(records: list[dict]) -> pd.DataFrame:
    """Convert fetcher records into a DataFrame with the expected schema."""
    rows = []
    for rec in records:
        rows.append({
            "station_id": rec["station_id"],
            "station_name": rec.get("station_name", ""),
            "address": rec.get("address", ""),
            "city": rec.get("city", ""),
            "region": rec.get("region", ""),
            "latitude": rec.get("latitude"),
            "longitude": rec.get("longitude"),
            "fuel_type": rec.get("fuel_type", "regular"),
            "price": float(rec["price"]),
        })
    return pd.DataFrame(rows, columns=SCHEMA_COLUMNS)


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def init_db(table_path: str = DATA_DIR) -> None:
    """Ensure the parent directory for the Delta table exists.

    The actual Delta table is created lazily on the first
    :func:`save_snapshot` call (``write_deltalake``).
    """
    os.makedirs(os.path.dirname(table_path) or ".", exist_ok=True)
    os.makedirs(table_path, exist_ok=True)
    logger.info("Delta table directory ready at %s", table_path)


def save_snapshot(
    records: list[dict], table_path: str = DATA_DIR
) -> int:
    """Persist only the *changes* from a new set of price records.

    Uses Delta Lake ``merge`` with three clauses:

    * **when_matched_update_all** — updates rows whose price (or other
      fields) changed.
    * **when_not_matched_insert_all** — inserts brand-new station/fuel
      pairs.
    * **when_not_matched_by_source_delete** — removes station/fuel pairs
      that disappeared from the source.

    On the very first call the table is created via ``write_deltalake``.

    Returns the number of rows in the incoming snapshot (for logging
    compatibility).
    """
    if not records:
        logger.warning("No records to save.")
        return 0

    source_df = _records_to_dataframe(records)

    if not _table_exists(table_path):
        write_deltalake(table_path, source_df)
        logger.info(
            "Created Delta table with %d initial records.", len(source_df)
        )
        return len(source_df)

    dt = DeltaTable(table_path)
    (
        dt.merge(
            source=source_df,
            predicate=MERGE_PREDICATE,
            source_alias="source",
            target_alias="target",
        )
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .when_not_matched_by_source_delete()
        .execute()
    )

    version = DeltaTable(table_path).version()
    logger.info("Merge complete — Delta table now at version %d.", version)
    return len(source_df)


def get_state_at(
    version: int, table_path: str = DATA_DIR
) -> list[dict[str, Any]]:
    """Reconstruct the full state of all stations at a given Delta version.

    Parameters
    ----------
    version:
        The Delta table version number (0-based, incremented on each
        merge that produces changes).
    table_path:
        Path to the Delta table directory.

    Returns
    -------
    list[dict]
        One dict per active station/fuel pair with keys matching
        ``SCHEMA_COLUMNS``.
    """
    try:
        dt = DeltaTable(table_path, version=version)
    except Exception:  # noqa: BLE001
        return []
    df = dt.to_pandas()
    return df.to_dict(orient="records")


def get_current_state(table_path: str = DATA_DIR) -> list[dict[str, Any]]:
    """Return the latest known state of all stations."""
    if not _table_exists(table_path):
        return []
    dt = DeltaTable(table_path)
    df = dt.to_pandas()
    return df.to_dict(orient="records")


def get_history(table_path: str = DATA_DIR) -> list[dict]:
    """Return the Delta table commit history."""
    if not _table_exists(table_path):
        return []
    dt = DeltaTable(table_path)
    return dt.history()
