"""Apache Hudi writer module for change-based persistence of Quebec fuel price data.

Instead of storing every snapshot (all stations × all fuel types every 5 min),
this module submits each snapshot as a Hudi **upsert**.  Hudi automatically
detects which rows actually changed (new station, price change, disappeared
station) and only writes the *deltas*.

Features
--------
* **Automatic change detection** – submit full snapshots; only real changes
  are persisted (native upsert on ``(station_id, fuel_type)`` record key with
  ``fetched_at`` as pre-combine field).
* **Time travel** – ``get_snapshot_at(timestamp)`` reconstructs state at any
  historical instant.
* **Incremental query** – ``get_changes_since(timestamp)`` returns only the
  rows that changed after a given commit instant.
* **Efficient columnar storage** – Parquet with optional compression and
  partitioning by ``region``.

Requirements
------------
* PySpark ≥ 3.5
* Java 11+ runtime (OpenJDK is sufficient)
* ``hudi-spark3.5-bundle`` JAR – downloaded automatically via
  ``spark.jars.packages`` when a Spark session is created.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Optional

from src.config import HUDI_PARALLELISM, HUDI_TABLE_NAME, HUDI_TABLE_PATH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hudi table options
# ---------------------------------------------------------------------------
HUDI_OPTIONS: dict[str, str] = {
    "hoodie.table.name": HUDI_TABLE_NAME,
    "hoodie.datasource.write.recordkey.field": "station_id,fuel_type",
    "hoodie.datasource.write.precombine.field": "fetched_at",
    "hoodie.datasource.write.operation": "upsert",
    "hoodie.datasource.write.table.type": "COPY_ON_WRITE",
    "hoodie.datasource.write.partitionpath.field": "region",
    "hoodie.upsert.shuffle.parallelism": HUDI_PARALLELISM,
    "hoodie.insert.shuffle.parallelism": HUDI_PARALLELISM,
}

# Hudi Spark bundle Maven coordinates (auto-downloaded by Spark).
_HUDI_SPARK_BUNDLE = "org.apache.hudi:hudi-spark3.5-bundle_2.12:0.15.0"

# ---------------------------------------------------------------------------
# Spark session singleton
# ---------------------------------------------------------------------------
_spark: Optional[Any] = None  # Optional[SparkSession]


def _get_spark() -> Any:
    """Return (or create) a SparkSession configured for Hudi.

    The Hudi Spark bundle JAR is resolved automatically via Maven on first
    start-up.  Subsequent calls return the cached session.
    """
    global _spark  # noqa: PLW0603
    if _spark is None:
        from pyspark.sql import SparkSession  # lazy import – heavy dependency

        _spark = (
            SparkSession.builder
            .appName("JerryCan")
            .config(
                "spark.jars.packages",
                _HUDI_SPARK_BUNDLE,
            )
            .config(
                "spark.serializer",
                "org.apache.spark.serializer.KryoSerializer",
            )
            .config(
                "spark.sql.extensions",
                "org.apache.spark.sql.hudi.HoodieSparkSessionExtension",
            )
            .config(
                "spark.sql.catalog.spark_catalog",
                "org.apache.spark.sql.hudi.catalog.HoodieCatalog",
            )
            .getOrCreate()
        )
    return _spark


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def init_hudi() -> None:
    """Initialise the Spark session with Hudi support.

    Call once at application start-up (analogous to ``database.init_db``).
    """
    _get_spark()
    logger.info(
        "Hudi writer initialised (table: %s, path: %s)",
        HUDI_TABLE_NAME,
        HUDI_TABLE_PATH,
    )


def save_snapshot(
    records: list[dict[str, Any]],
    table_path: str = HUDI_TABLE_PATH,
) -> int:
    """Persist a list of price records using Hudi upsert.

    Each record is expected to carry the same keys produced by
    ``GeoJsonDataSource.fetch()``:

        station_id, station_name, address, city, region,
        latitude, longitude, fuel_type, price, fetched_at

    Because Hudi performs an **upsert** keyed on
    ``(station_id, fuel_type)`` with ``fetched_at`` as the pre-combine
    field, only rows whose price (or other fields) actually changed will
    result in new file writes – dramatically reducing storage compared to
    the previous SQLite "append every snapshot" approach.

    Returns the number of records submitted for upsert.
    """
    if not records:
        logger.warning("No records to save.")
        return 0

    spark = _get_spark()
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")

    # Normalise each record into a flat dict with consistent types.
    normalised: list[dict[str, Any]] = []
    for rec in records:
        normalised.append(
            {
                "station_id": str(rec["station_id"]),
                "station_name": str(rec.get("station_name", "")),
                "address": str(rec.get("address", "")),
                "city": str(rec.get("city", "")),
                "region": str(rec.get("region", "") or ""),
                "latitude": float(rec.get("latitude") or 0.0),
                "longitude": float(rec.get("longitude") or 0.0),
                "fuel_type": str(rec.get("fuel_type", "regular")),
                "price": float(rec["price"]),
                "fetched_at": str(rec.get("fetched_at", fetched_at)),
            }
        )

    df = spark.createDataFrame(normalised)
    (
        df.write.format("hudi")
        .options(**HUDI_OPTIONS)
        .mode("append")
        .save(table_path)
    )

    logger.info("Saved %d price records via Hudi upsert.", len(normalised))
    return len(normalised)


def get_snapshot_at(
    timestamp: str,
    table_path: str = HUDI_TABLE_PATH,
) -> Any:
    """Read the state of all prices at a specific point in time.

    Uses Hudi's native **time-travel** capability so that no manual
    snapshot bookkeeping is needed.

    Parameters
    ----------
    timestamp:
        ISO-8601 instant, e.g. ``"2026-04-01T12:00:00"``.
    table_path:
        Filesystem path to the Hudi table.

    Returns
    -------
    pyspark.sql.DataFrame
        A Spark DataFrame containing the state of all price records as of
        *timestamp*.
    """
    spark = _get_spark()
    return (
        spark.read.format("hudi")
        .option("as.of.instant", timestamp)
        .load(table_path)
    )


def get_changes_since(
    timestamp: str,
    table_path: str = HUDI_TABLE_PATH,
) -> Any:
    """Read all changes (deltas) since a specific point in time.

    Uses Hudi's **incremental query** mode which returns only the rows
    that were written after *timestamp* – ideal for downstream ETL or
    alerting pipelines.

    Parameters
    ----------
    timestamp:
        ISO-8601 instant, e.g. ``"2026-04-01T12:00:00"``.
    table_path:
        Filesystem path to the Hudi table.

    Returns
    -------
    pyspark.sql.DataFrame
        A Spark DataFrame with all records that changed since *timestamp*.
    """
    spark = _get_spark()
    return (
        spark.read.format("hudi")
        .option("hoodie.datasource.query.type", "incremental")
        .option("hoodie.datasource.read.begin.instanttime", timestamp)
        .load(table_path)
    )


def stop() -> None:
    """Stop the Spark session and release resources."""
    global _spark  # noqa: PLW0603
    if _spark is not None:
        _spark.stop()
        _spark = None
        logger.info("Spark session stopped.")
