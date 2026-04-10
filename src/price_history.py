"""Historicized price changes — Spark + Hudi native API.

Reads from the Hudi table using the native Hudi/Spark API (CDC incremental,
read_optimized, metadata timeline) rather than scanning raw Parquet files.

Public API
----------
``get_commit_instants(table_path)``
    List commit timestamps from ``.hoodie/`` (filesystem only, no Spark).

``build_historicized_changes_spark(table_path)``
    pandas DataFrame of every price change, via Hudi CDC.

``build_historicized_changes_pandas(table_path)``
    Deprecated alias for ``build_historicized_changes_spark``.

``get_latest_changes_summary(table_path, limit)``
    Dict summary for the inspector UI.

``get_snapshot_dates(table_path)``
    Distinct snapshot dates (YYYY-MM-DD), most recent first.
    Reads only ``.hoodie/`` metadata — zero Parquet files read.

``get_snapshots_for_date(table_path, date_str)``
    Snapshots for a given date, via Spark read_optimized.

``get_current_prices(table_path)``
    Latest prices, via Spark read_optimized.

``get_inspection_data(table_path)``
    Full inspection dict, using a single Spark read with caching.
"""

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Columns we care about (+ Hudi metadata for commit time)
_KEY_COLS = ["station_id", "fuel_type"]
_DATA_COLS = ["station_name", "city", "region", "price", "fetched_at"]
_HOODIE_COMMIT = "_hoodie_commit_time"
_SELECT_COLS = [_HOODIE_COMMIT] + _KEY_COLS + _DATA_COLS


# ------------------------------------------------------------------
# Spark session helper
# ------------------------------------------------------------------

def _get_spark_session():
    """Return the shared SparkSession, importing lazily to support both
    the ``src.hudi_writer`` package path and the flat ``hudi_writer``
    module path used in the management-server Docker image.
    """
    try:
        from src.hudi_writer import _get_spark
    except ImportError:
        from hudi_writer import _get_spark  # type: ignore[no-redef]
    return _get_spark()


# ------------------------------------------------------------------
# Commit instant discovery (filesystem only, no Spark)
# ------------------------------------------------------------------

def get_commit_instants(table_path: str) -> list[str]:
    """Return all commit instants from the Hudi timeline, sorted chronologically."""
    hoodie_dir = os.path.join(table_path, ".hoodie")
    if not os.path.isdir(hoodie_dir):
        return []

    instants: list[str] = []
    for fname in sorted(os.listdir(hoodie_dir)):
        if fname.endswith(".commit") or fname.endswith(".deltacommit"):
            instant = fname.split(".")[0]
            instants.append(instant)
    return instants


# ------------------------------------------------------------------
# Core: build the historicized changes DataFrame via Hudi CDC
# ------------------------------------------------------------------

def build_historicized_changes_spark(table_path: str) -> pd.DataFrame:
    """Build a pandas DataFrame of every price change using Hudi CDC.

    Uses the native CDC incremental query (``op``, ``before``, ``after``
    JSON fields) instead of reading raw Parquet files.

    Columns: commit_instant, station_id, station_name, city, region,
    fuel_type, op, prev_price, price, delta, fetched_at.
    """
    _empty_cols = [
        "commit_instant", *_KEY_COLS,
        "station_name", "city", "region",
        "op", "prev_price", "price", "delta", "fetched_at",
    ]

    spark = _get_spark_session()

    try:
        from pyspark.sql import functions as F

        cdc_df = (
            spark.read.format("hudi")
            .option("hoodie.datasource.query.type", "incremental")
            .option("hoodie.datasource.query.incremental.format", "cdc")
            .option("hoodie.datasource.read.begin.instanttime", "000")
            .load(table_path)
        )

        changes = cdc_df.select(
            F.col("_hoodie_commit_time").alias("commit_instant"),
            F.col("op"),
            F.coalesce(
                F.get_json_object(F.col("after"), "$.station_id"),
                F.get_json_object(F.col("before"), "$.station_id"),
            ).alias("station_id"),
            F.coalesce(
                F.get_json_object(F.col("after"), "$.fuel_type"),
                F.get_json_object(F.col("before"), "$.fuel_type"),
            ).alias("fuel_type"),
            F.coalesce(
                F.get_json_object(F.col("after"), "$.station_name"),
                F.get_json_object(F.col("before"), "$.station_name"),
            ).alias("station_name"),
            F.coalesce(
                F.get_json_object(F.col("after"), "$.city"),
                F.get_json_object(F.col("before"), "$.city"),
            ).alias("city"),
            F.coalesce(
                F.get_json_object(F.col("after"), "$.region"),
                F.get_json_object(F.col("before"), "$.region"),
            ).alias("region"),
            F.get_json_object(F.col("before"), "$.price").cast("double").alias("prev_price"),
            F.get_json_object(F.col("after"), "$.price").cast("double").alias("price"),
            F.get_json_object(F.col("after"), "$.fetched_at").alias("fetched_at"),
        ).withColumn(
            "delta",
            F.round(F.col("price") - F.col("prev_price"), 2),
        ).orderBy("commit_instant", "station_id", "fuel_type")

        return changes.toPandas()

    except Exception:
        logger.warning("CDC query failed for %s — returning empty DataFrame.", table_path, exc_info=True)
        return pd.DataFrame(columns=_empty_cols)


def build_historicized_changes_pandas(table_path: str) -> pd.DataFrame:
    """Deprecated alias for ``build_historicized_changes_spark``.

    Kept for backward compatibility with callers that use the pandas name
    (e.g. ``_generate_csv_from_hudi``).
    """
    return build_historicized_changes_spark(table_path)


# ------------------------------------------------------------------
# Read helpers for inspectors / dashboard
# ------------------------------------------------------------------

def get_latest_changes_summary(
    table_path: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Return a summary of the most recent price changes.

    Returns a dict with:
        - ``total_changes``: number of price changes across all commits
        - ``commit_count``: number of distinct commits with changes
        - ``rises``: top *limit* biggest price increases (list of dicts)
        - ``drops``: top *limit* biggest price decreases (list of dicts)
        - ``latest_changed_count``: stations that changed in the last commit
    """
    df = build_historicized_changes_pandas(table_path)

    if df.empty:
        return {
            "total_changes": 0,
            "commit_count": 0,
            "rises": [],
            "drops": [],
            "latest_changed_count": 0,
        }

    total = len(df)
    commits = df["commit_instant"].nunique()

    # Exclude inserts (delta NaN) for rises/drops ranking
    updates = df.dropna(subset=["delta"])

    rises = updates.nlargest(limit, "delta").to_dict("records") if not updates.empty else []
    drops = updates.nsmallest(limit, "delta").to_dict("records") if not updates.empty else []

    latest_instant = df["commit_instant"].max()
    latest = df[
        (df["commit_instant"] == latest_instant) & df["delta"].notna()
    ]
    latest_count = latest["station_id"].nunique()

    return {
        "total_changes": total,
        "commit_count": commits,
        "rises": rises,
        "drops": drops,
        "latest_changed_count": latest_count,
    }


def get_snapshots_data(table_path: str, limit: int = 10) -> list[dict]:
    """Return snapshot rows as a list of dicts (same data as show_snapshots()).

    Each dict has: fetched_at (str), record_count (int|None), changes (int|None).
    Sorted by fetched_at descending.  Uses Spark read_optimized for record
    counts and the CDC DataFrame for change counts.
    """
    try:
        from pyspark.sql import functions as F

        spark = _get_spark_session()
        all_df = (
            spark.read.format("hudi")
            .option("hoodie.datasource.query.type", "read_optimized")
            .load(table_path)
        )

        counts_rows = (
            all_df.groupBy("fetched_at")
            .count()
            .orderBy(F.col("fetched_at").desc())
            .limit(limit)
            .collect()
        )
        record_counts = {r["fetched_at"]: r["count"] for r in counts_rows}

        hist = build_historicized_changes_spark(table_path)
        changes_per_snap: dict = {}
        if not hist.empty and "fetched_at" in hist.columns:
            changes_per_snap = hist.groupby("fetched_at").size().to_dict()

        all_ts = sorted(
            set(record_counts.keys()) | set(changes_per_snap.keys()),
            reverse=True,
        )[:limit]

        result = []
        for ts in all_ts:
            key = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            rec = record_counts.get(ts)
            chg = changes_per_snap.get(ts)
            result.append({
                "fetched_at": key,
                "record_count": int(rec) if rec is not None else None,
                "changes": int(chg) if chg is not None else None,
            })
        return result

    except Exception:
        logger.warning("get_snapshots_data failed for %s.", table_path, exc_info=True)
        return []


# ------------------------------------------------------------------
# Helpers for the management-server dashboard (Hudi-native reads)
# ------------------------------------------------------------------

def get_snapshot_dates(table_path: str) -> list[str]:
    """Return distinct snapshot dates (YYYY-MM-DD), most recent first.

    Reads only the Hudi timeline metadata — zero Parquet files read.
    Parses ``.commit`` and ``.deltacommit`` filenames from ``.hoodie/``.
    """
    hoodie_dir = os.path.join(table_path, ".hoodie")
    if not os.path.isdir(hoodie_dir):
        return []
    dates: set[str] = set()
    for fname in os.listdir(hoodie_dir):
        if fname.endswith(".commit") or fname.endswith(".deltacommit"):
            instant = fname.split(".")[0]  # e.g. "20260410120000000"
            if len(instant) >= 8:
                date_str = f"{instant[:4]}-{instant[4:6]}-{instant[6:8]}"
                dates.add(date_str)
    return sorted(dates, reverse=True)


def get_snapshots_for_date(table_path: str, date_str: str) -> list[dict]:
    """Return snapshots for a given date (YYYY-MM-DD), most recent first.

    Each dict has: fetched_at (str), record_count (int).
    Uses Spark read_optimized with pushdown filter on ``fetched_at``.
    """
    try:
        from pyspark.sql import functions as F

        spark = _get_spark_session()
        rows = (
            spark.read.format("hudi")
            .option("hoodie.datasource.query.type", "read_optimized")
            .load(table_path)
            .filter(F.date_format(F.col("fetched_at"), "yyyy-MM-dd") == date_str)
            .groupBy("fetched_at")
            .count()
            .withColumnRenamed("count", "record_count")
            .orderBy(F.col("fetched_at").desc())
            .collect()
        )
        return [
            {"fetched_at": str(r["fetched_at"]), "record_count": int(r["record_count"])}
            for r in rows
        ]
    except Exception:
        logger.warning("get_snapshots_for_date failed for %s / %s.", table_path, date_str, exc_info=True)
        return []


def get_current_prices(table_path: str) -> tuple[list[dict], str | None]:
    """Return (prices, latest_ts) from the most recent snapshot in Hudi.

    *prices* is a list of dicts with keys: station_name, city, region,
    fuel_type, price.  *latest_ts* is the ``fetched_at`` value of the
    latest snapshot, or ``None`` when no data exists.
    Uses Spark read_optimized.
    """
    try:
        from pyspark.sql import functions as F

        spark = _get_spark_session()
        df = (
            spark.read.format("hudi")
            .option("hoodie.datasource.query.type", "read_optimized")
            .load(table_path)
        )

        latest_ts = df.agg(F.max("fetched_at")).collect()[0][0]
        if latest_ts is None:
            return [], None

        rows = (
            df.filter(F.col("fetched_at") == latest_ts)
            .select("station_name", "city", "region", "fuel_type", "price")
            .orderBy("region", "city", "station_name", "fuel_type")
            .collect()
        )
        prices = [
            {
                "station_name": str(r["station_name"] or ""),
                "city": str(r["city"] or ""),
                "region": str(r["region"] or ""),
                "fuel_type": str(r["fuel_type"] or ""),
                "price": float(r["price"]),
            }
            for r in rows
        ]
        return prices, str(latest_ts)

    except Exception:
        logger.warning("get_current_prices failed for %s.", table_path, exc_info=True)
        return [], None


def _empty_inspection_data() -> dict[str, Any]:
    return {
        "summary": {
            "station_count": 0,
            "price_count": 0,
            "snapshot_count": 0,
            "first_snapshot": None,
            "last_snapshot": None,
        },
        "regions": [],
        "latest_prices": [],
        "snapshots": [],
        "price_variations": {"increases": [], "decreases": []},
    }


def get_inspection_data(table_path: str) -> dict[str, Any]:
    """Return a dict with inspection data read entirely from Hudi.

    Uses a single Spark ``read_optimized`` load cached in memory, deriving
    summary, regions and latest prices from it.  Snapshot counts and price
    variations are obtained via CDC / metadata.

    Keys: summary, regions, latest_prices, snapshots, price_variations.
    """
    try:
        from pyspark.sql import functions as F
    except ImportError:
        logger.warning("pyspark not available — returning empty inspection data.")
        return _empty_inspection_data()

    spark = _get_spark_session()
    all_df = None
    latest_df = None

    try:
        # One read, cached for multiple operations
        all_df = (
            spark.read.format("hudi")
            .option("hoodie.datasource.query.type", "read_optimized")
            .load(table_path)
            .cache()
        )

        if all_df.count() == 0:
            return _empty_inspection_data()

        # --- Summary ---
        summary_row = all_df.agg(
            F.countDistinct("station_id").alias("station_count"),
            F.count("*").alias("price_count"),
            F.countDistinct("fetched_at").alias("snapshot_count"),
            F.min("fetched_at").alias("first_snapshot"),
            F.max("fetched_at").alias("last_snapshot"),
        ).collect()[0]

        summary = {
            "station_count": int(summary_row["station_count"]),
            "price_count": int(summary_row["price_count"]),
            "snapshot_count": int(summary_row["snapshot_count"]),
            "first_snapshot": str(summary_row["first_snapshot"]),
            "last_snapshot": str(summary_row["last_snapshot"]),
        }

        latest_ts = summary_row["last_snapshot"]
        latest_df = all_df.filter(F.col("fetched_at") == latest_ts).cache()

        # --- Regions ---
        region_counts = (
            all_df.dropDuplicates(["station_id"])
            .groupBy("region")
            .count()
            .withColumnRenamed("count", "station_count")
        )
        avg_prices = (
            latest_df.groupBy("region", "fuel_type")
            .agg(F.round(F.mean("price"), 1).alias("avg_price"))
        )
        regions_rows = region_counts.orderBy(F.col("station_count").desc()).collect()
        avg_rows = avg_prices.collect()
        avg_map: dict[str, dict[str, float]] = {}
        for r in avg_rows:
            avg_map.setdefault(r["region"] or "N/A", {})[r["fuel_type"]] = r["avg_price"]
        regions = [
            {
                "region": r["region"] or "N/A",
                "station_count": int(r["station_count"]),
                "avg_prices": avg_map.get(r["region"] or "N/A", {}),
            }
            for r in regions_rows
        ]

        # --- Latest prices (lowest 50) ---
        latest_prices_rows = (
            latest_df.select("station_name", "city", "region", "fuel_type", "price")
            .orderBy("price")
            .limit(50)
            .collect()
        )
        latest_prices = [
            {
                "station_name": str(r["station_name"] or ""),
                "city": str(r["city"] or ""),
                "region": str(r["region"] or ""),
                "fuel_type": str(r["fuel_type"] or ""),
                "price": float(r["price"]),
            }
            for r in latest_prices_rows
        ]

        # --- Snapshots (last 10) — via metadata timeline ---
        snapshots = get_snapshots_data(table_path, limit=10)

        # --- Price variations — via CDC ---
        hist_df_pd = build_historicized_changes_spark(table_path)
        increases: list[dict] = []
        decreases: list[dict] = []
        if not hist_df_pd.empty:
            updates = hist_df_pd.dropna(subset=["delta"])
            if not updates.empty:
                updates = updates.sort_values("fetched_at", ascending=False)
                last_per_key = updates.drop_duplicates(subset=["station_id", "fuel_type"])

                rises = last_per_key[last_per_key["delta"] > 0].nlargest(10, "delta")
                drops = last_per_key[last_per_key["delta"] < 0].nsmallest(10, "delta")

                for _, r in rises.iterrows():
                    increases.append({
                        "station_name": str(r.get("station_name", "")),
                        "city": str(r.get("city", "")),
                        "fuel_type": str(r.get("fuel_type", "")),
                        "prev_price": round(float(r["prev_price"]), 1),
                        "price": round(float(r["price"]), 1),
                        "delta": round(float(r["delta"]), 1),
                    })
                for _, r in drops.iterrows():
                    decreases.append({
                        "station_name": str(r.get("station_name", "")),
                        "city": str(r.get("city", "")),
                        "fuel_type": str(r.get("fuel_type", "")),
                        "prev_price": round(float(r["prev_price"]), 1),
                        "price": round(float(r["price"]), 1),
                        "delta": round(float(r["delta"]), 1),
                    })

        return {
            "summary": summary,
            "regions": regions,
            "latest_prices": latest_prices,
            "snapshots": snapshots,
            "price_variations": {"increases": increases, "decreases": decreases},
        }

    except Exception:
        logger.warning("get_inspection_data failed for %s.", table_path, exc_info=True)
        return _empty_inspection_data()

    finally:
        if latest_df is not None:
            try:
                latest_df.unpersist()
            except Exception:
                pass
        if all_df is not None:
            try:
                all_df.unpersist()
            except Exception:
                pass
