"""Append-only SQLite changelog for durable price-change history.

Hudi's CDC logs are tied to delta-log files that can be removed by
compaction and cleaning.  This module provides an independent, permanent
record of every meaningful change (insert / update / delete) that survives
regardless of Hudi's internal file lifecycle.

The changelog is populated by ``record_changes()`` which is called from
``hudi_writer.save_snapshot()`` **before** the Hudi upsert — it compares
the incoming batch with the current Hudi table state and logs the diff.
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Generator

logger = logging.getLogger(__name__)

# Default path — sits alongside the Hudi data inside the Docker volume.
DEFAULT_CHANGELOG_PATH = "/data/changelog.db"

CREATE_CHANGELOG_TABLE = """
CREATE TABLE IF NOT EXISTS changelog (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at  TEXT    NOT NULL,
    op           TEXT    NOT NULL,
    station_id   TEXT    NOT NULL,
    station_name TEXT,
    city         TEXT,
    region       TEXT,
    fuel_type    TEXT    NOT NULL,
    prev_price   REAL,
    price        REAL,
    delta        REAL,
    fetched_at   TEXT
)
"""

CREATE_CHANGELOG_INDEX = """
CREATE INDEX IF NOT EXISTS idx_changelog_station_fuel
ON changelog (station_id, fuel_type, recorded_at)
"""


# ------------------------------------------------------------------
# Connection helpers
# ------------------------------------------------------------------

@contextmanager
def _connect(db_path: str = DEFAULT_CHANGELOG_PATH) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_changelog(db_path: str = DEFAULT_CHANGELOG_PATH) -> None:
    """Create the changelog table if it does not exist yet."""
    with _connect(db_path) as conn:
        conn.execute(CREATE_CHANGELOG_TABLE)
        conn.execute(CREATE_CHANGELOG_INDEX)
        conn.commit()
    logger.info("Changelog initialised at %s", db_path)


# ------------------------------------------------------------------
# Change detection and recording
# ------------------------------------------------------------------

def record_changes(
    incoming: list[dict[str, Any]],
    current_prices: dict[tuple[str, str], dict[str, Any]],
    db_path: str = DEFAULT_CHANGELOG_PATH,
) -> int:
    """Compare *incoming* records with *current_prices* and persist events.

    Parameters
    ----------
    incoming:
        The normalised list of records about to be upserted into Hudi.
        Each dict has at least: station_id, station_name, city, region,
        fuel_type, price, fetched_at.
    current_prices:
        A mapping ``{(station_id, fuel_type): {price, station_name, …}}``
        representing the current state in the Hudi table.  Empty on the
        first run (all records are inserts).
    db_path:
        Path to the changelog SQLite database.

    Returns
    -------
    int
        Number of changelog events written (inserts + updates + deletes).
    """
    now = datetime.now(UTC).isoformat(timespec="seconds")
    events: list[dict[str, Any]] = []

    incoming_keys: set[tuple[str, str]] = set()
    for rec in incoming:
        key = (str(rec["station_id"]), str(rec["fuel_type"]))
        incoming_keys.add(key)
        existing = current_prices.get(key)

        if existing is None:
            # New station/fuel → insert
            events.append({
                "recorded_at": now,
                "op": "i",
                "station_id": rec["station_id"],
                "station_name": rec.get("station_name", ""),
                "city": rec.get("city", ""),
                "region": rec.get("region", ""),
                "fuel_type": rec["fuel_type"],
                "prev_price": None,
                "price": rec["price"],
                "delta": None,
                "fetched_at": rec.get("fetched_at", now),
            })
        else:
            old_price = existing.get("price")
            new_price = rec["price"]
            if old_price is not None and abs(new_price - old_price) > 0.001:
                # Price changed → update
                events.append({
                    "recorded_at": now,
                    "op": "u",
                    "station_id": rec["station_id"],
                    "station_name": rec.get("station_name", ""),
                    "city": rec.get("city", ""),
                    "region": rec.get("region", ""),
                    "fuel_type": rec["fuel_type"],
                    "prev_price": old_price,
                    "price": new_price,
                    "delta": round(new_price - old_price, 2),
                    "fetched_at": rec.get("fetched_at", now),
                })

    # Stations present in Hudi but absent from this fetch → delete
    for key, existing in current_prices.items():
        if key not in incoming_keys:
            events.append({
                "recorded_at": now,
                "op": "d",
                "station_id": key[0],
                "station_name": existing.get("station_name", ""),
                "city": existing.get("city", ""),
                "region": existing.get("region", ""),
                "fuel_type": key[1],
                "prev_price": existing.get("price"),
                "price": None,
                "delta": None,
                "fetched_at": None,
            })

    if not events:
        return 0

    with _connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO changelog
                (recorded_at, op, station_id, station_name, city, region,
                 fuel_type, prev_price, price, delta, fetched_at)
            VALUES
                (:recorded_at, :op, :station_id, :station_name, :city, :region,
                 :fuel_type, :prev_price, :price, :delta, :fetched_at)
            """,
            events,
        )
        conn.commit()

    logger.info("Changelog: %d events recorded (%s).", len(events), now)
    return len(events)


def read_changelog(db_path: str = DEFAULT_CHANGELOG_PATH) -> list[dict[str, Any]]:
    """Read all changelog events, ordered chronologically.

    Returns a list of dicts suitable for building a pandas DataFrame or CSV.
    """
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT recorded_at, op, station_id, station_name, city, region,
                   fuel_type, prev_price, price, delta, fetched_at
            FROM changelog
            ORDER BY station_id, fuel_type, recorded_at
            """
        ).fetchall()
    return [dict(r) for r in rows]
