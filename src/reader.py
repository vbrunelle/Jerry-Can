"""Price-history readers — abstract base + one concrete class per backend.

Architecture
------------
``PriceReader``  (ABC)
    ├── ``read_all()``     → raw price rows, all captures
    └── ``read_changes()`` → price-change events with before/after values

``SqliteReader``
    SQLite appends EVERY station on every snapshot, even those whose price
    did not change.  ``read_changes()`` therefore loads the full history and
    applies a Spark LAG window function to isolate real changes.
    Columns added: ``prev_price``, ``prev_fetched_at``, ``delta``.

``HudiReader``
    Relies entirely on Hudi's native CDC (Change Data Capture).
    With ``hoodie.table.cdc.enabled=true`` and ``DATA_BEFORE_AFTER`` mode,
    Hudi writes a CDC log file alongside every base-file write that captures
    the exact before/after state per row — no LAG needed in Spark.

    ``read_all()``     → incremental query from t=000
    ``read_changes()`` → CDC query from t=000 (``query.incremental.format=cdc``) via ``query.incremental.format=cdc``

    CDC result schema (Hudi 0.15.0 — ``DATA_BEFORE_AFTER`` mode):
        op            STRING   — "i" insert, "u" update, "d" delete
        ts_ms         STRING   — commit timestamp
        before        STRING   — JSON row before the change (null on insert)
        after         STRING   — JSON row after the change (null on delete)

    ``read_changes()`` extracts fields from the JSON strings using
    ``get_json_object`` and flattens them to the same columns as
    ``SqliteReader.read_changes()`` so callers need no backend-specific logic:
        station_id, station_name, city, region, fuel_type,
        prev_price, price, delta, prev_fetched_at, fetched_at, op

    For deletes (``op="d"``), identity fields (station_id, station_name, etc.)
    are taken from ``before`` since ``after`` is null; ``price`` and
    ``fetched_at`` are null while ``prev_price`` holds the last known price.

Factory
-------
    reader = get_reader()          # uses PERSISTENCE_BACKEND env var
    reader = get_reader("sqlite", "./data/fuel_prices.db")
    reader = get_reader("hudi",   "/data/hudi/fuel_prices")
"""

import sqlite3
from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from src.config import DATABASE_PATH, HUDI_TABLE_PATH, PERSISTENCE_BACKEND


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class PriceReader(ABC):
    """Abstract reader — returns Spark DataFrames from the price store."""

    @abstractmethod
    def read_all(self):
        """Return all raw price rows as a Spark DataFrame.

        For SQLite this is every row in ``prices`` joined with ``stations``.
        For Hudi this is every upserted row (one per real write event).
        """

    @abstractmethod
    def read_changes(self):
        """Return only price-change events as a Spark DataFrame.

        Guaranteed columns:
            station_id, station_name, city, region, fuel_type,
            prev_price, price, delta, prev_fetched_at, fetched_at
        """


# ---------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------

class SqliteReader(PriceReader):
    """Read price history from a SQLite database.

    SQLite records ALL stations at every snapshot, so change detection
    requires a LAG window function to find rows where price actually moved.
    """

    def __init__(self, db_path: str = DATABASE_PATH) -> None:
        self._db_path = db_path

    def _spark(self):
        from src.hudi_writer import _get_spark  # lazy — PySpark is heavy
        return _get_spark()

    def read_all(self):
        conn = sqlite3.connect(self._db_path)
        try:
            df_pd = pd.read_sql_query(
                """
                SELECT
                    p.station_id,
                    s.name              AS station_name,
                    s.city,
                    s.region,
                    s.address,
                    CAST(s.latitude  AS REAL) AS latitude,
                    CAST(s.longitude AS REAL) AS longitude,
                    p.fuel_type,
                    CAST(p.price AS REAL) AS price,
                    p.fetched_at
                FROM prices p
                JOIN stations s ON s.id = p.station_id
                ORDER BY p.station_id, p.fuel_type, p.fetched_at
                """,
                conn,
            )
        finally:
            conn.close()

        return self._spark().createDataFrame(df_pd)

    def read_changes(self):
        from pyspark.sql import Window
        from pyspark.sql import functions as F

        w = Window.partitionBy("station_id", "fuel_type").orderBy("fetched_at")
        return (
            self.read_all()
            .withColumn("prev_price",      F.lag("price").over(w))
            .withColumn("prev_fetched_at", F.lag("fetched_at").over(w))
            .withColumn("delta", F.round(F.col("price") - F.col("prev_price"), 2))
            .filter(
                F.col("prev_price").isNotNull()
                & (F.col("price") != F.col("prev_price"))
            )
        )


# ---------------------------------------------------------------------------
# Hudi implementation
# ---------------------------------------------------------------------------

class HudiReader(PriceReader):
    """Read price history from an Apache Hudi table.

    Leverages two native Hudi query modes:

    ``read_all()``
        Incremental query from t=000.  Returns one row per (station, fuel_type)
        per real upsert commit — i.e. every time a price change was written.

    ``read_changes()``
        CDC query from t=000.  Hudi's native Change Data Capture returns the
        ``before`` and ``after`` structs for every write, so no LAG is needed.
        Only ``op="u"`` rows (updates) represent actual price changes; inserts
        (``op="i"``) are the station's first-ever price record.  Deletes
        (``op="d"``) indicate a station that disappeared from the source.

        The result is flattened to match the ``SqliteReader.read_changes()``
        schema, with an extra ``op`` column.
    """

    def __init__(self, table_path: str = HUDI_TABLE_PATH) -> None:
        self._table_path = table_path

    def _spark(self):
        from src.hudi_writer import _get_spark
        return _get_spark()

    def read_all(self):
        """All committed rows via Hudi incremental query from the beginning."""
        return (
            self._spark().read.format("hudi")
            .option("hoodie.datasource.query.type", "incremental")
            .option("hoodie.datasource.read.begin.instanttime", "000")
            .load(self._table_path)
        )

    def read_changes(self):
        """Price-change events via Hudi native CDC — no LAG required.

        Uses ``query.incremental.format=cdc`` (Hudi 0.15 API).  Hudi writes
        CDC log entries alongside each MOR delta log when
        ``hoodie.table.cdc.enabled=true``.

        ``before`` and ``after`` are JSON strings; fields are extracted with
        ``get_json_object``.  The result is flattened so callers see the same
        columns as ``SqliteReader.read_changes()``, plus ``op`` (i/u).
        """
        from pyspark.sql import functions as F

        cdc_df = (
            self._spark().read.format("hudi")
            .option("hoodie.datasource.query.type", "incremental")
            .option("hoodie.datasource.query.incremental.format", "cdc")
            .option("hoodie.datasource.read.begin.instanttime", "000")
            .load(self._table_path)
        )

        # before is NULL for inserts; after is NULL for deletes.
        # Use COALESCE to pick from the available side.
        return (
            cdc_df
            .withColumn("station_id", F.coalesce(
                F.get_json_object(F.col("after"), "$.station_id"),
                F.get_json_object(F.col("before"), "$.station_id"),
            ))
            .withColumn("station_name", F.coalesce(
                F.get_json_object(F.col("after"), "$.station_name"),
                F.get_json_object(F.col("before"), "$.station_name"),
            ))
            .withColumn("city", F.coalesce(
                F.get_json_object(F.col("after"), "$.city"),
                F.get_json_object(F.col("before"), "$.city"),
            ))
            .withColumn("region", F.coalesce(
                F.get_json_object(F.col("after"), "$.region"),
                F.get_json_object(F.col("before"), "$.region"),
            ))
            .withColumn("fuel_type", F.coalesce(
                F.get_json_object(F.col("after"), "$.fuel_type"),
                F.get_json_object(F.col("before"), "$.fuel_type"),
            ))
            .withColumn("price",
                F.get_json_object(F.col("after"), "$.price").cast("double"))
            .withColumn("fetched_at",
                F.get_json_object(F.col("after"), "$.fetched_at"))
            .withColumn("prev_price",
                F.get_json_object(F.col("before"), "$.price").cast("double"))
            .withColumn("prev_fetched_at",
                F.get_json_object(F.col("before"), "$.fetched_at"))
            .withColumn(
                "delta",
                F.when(
                    F.col("prev_price").isNotNull() & F.col("price").isNotNull(),
                    F.round(F.col("price") - F.col("prev_price"), 2),
                ).otherwise(F.lit(None))
            )
            .select(
                "op", "station_id", "station_name", "city", "region",
                "fuel_type", "prev_price", "price", "delta",
                "prev_fetched_at", "fetched_at",
            )
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_reader(
    backend: Optional[str] = None,
    path: Optional[str] = None,
) -> PriceReader:
    """Return the appropriate ``PriceReader`` for *backend*.

    Parameters
    ----------
    backend:
        ``"sqlite"`` or ``"hudi"``.  Defaults to ``PERSISTENCE_BACKEND``.
    path:
        Override the default path (``DATABASE_PATH`` / ``HUDI_TABLE_PATH``).
    """
    backend = (backend or PERSISTENCE_BACKEND).lower()
    if backend == "sqlite":
        return SqliteReader(path or DATABASE_PATH)
    if backend in ("hudi", "spark"):
        return HudiReader(path or HUDI_TABLE_PATH)
    raise ValueError(f"Unknown backend: {backend!r}. Use 'sqlite' or 'hudi'.")


# ---------------------------------------------------------------------------
# Backward-compatible helpers
# ---------------------------------------------------------------------------

def read_price_history(
    backend: Optional[str] = None,
    path: Optional[str] = None,
):
    """Shortcut: ``get_reader(backend, path).read_all()``."""
    return get_reader(backend, path).read_all()


def read_price_changes(
    backend: Optional[str] = None,
    path: Optional[str] = None,
):
    """Shortcut: ``get_reader(backend, path).read_changes()``."""
    return get_reader(backend, path).read_changes()
