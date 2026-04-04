"""Historicized price changes — pure pyarrow + pandas, no Spark/JVM.

Reads **all** base Parquet files from the Hudi table (including older
versions kept across compactions).  Each row carries
``_hoodie_commit_time``, which identifies the commit that wrote it.

Algorithm
---------
1. Read every ``*.parquet`` under the table path (skip ``.hoodie``).
2. Sort by ``(station_id, fuel_type, _hoodie_commit_time)``.
3. Within each group, ``shift(1)`` on ``price`` gives ``prev_price``.
4. Keep only rows where the price actually changed (or the first
   appearance of a key — insert).

This runs in pure Python/C (pyarrow columnar I/O + pandas vectorised
ops) and scales linearly with total parquet bytes — no JVM, no Spark
session, no per-commit reads.

Public API
----------
``get_commit_instants(table_path)``
    List commit timestamps from ``.hoodie/``.

``build_historicized_changes_pandas(table_path)``
    pandas DataFrame of every price change across all commits.

``get_latest_changes_summary(table_path, limit)``
    Dict summary for the inspector UI.
"""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

# Columns we care about (+ Hudi metadata for commit time)
_KEY_COLS = ["station_id", "fuel_type"]
_DATA_COLS = ["station_name", "city", "region", "price", "fetched_at"]
_HOODIE_COMMIT = "_hoodie_commit_time"
_SELECT_COLS = [_HOODIE_COMMIT] + _KEY_COLS + _DATA_COLS


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
# Parquet I/O — read ALL base files (all versions)
# ------------------------------------------------------------------

def _read_all_parquet(table_path: str) -> pd.DataFrame:
    """Read every base Parquet file under *table_path* into a DataFrame.

    Walks partition directories (skipping ``.hoodie``), reads only the
    columns we need (projection pushdown), then zero-copy concatenates
    at the Arrow level before converting to pandas once.

    File reads are parallelised across threads for I/O-bound speedup.
    """
    parquet_files: list[str] = []
    for root, dirs, files in os.walk(table_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".parquet"):
                parquet_files.append(os.path.join(root, f))

    if not parquet_files:
        return pd.DataFrame(columns=_SELECT_COLS)

    def _read_one(fp: str) -> pa.Table:
        return pq.read_table(fp, columns=_SELECT_COLS)

    with ThreadPoolExecutor(max_workers=min(8, len(parquet_files))) as pool:
        tables = list(pool.map(_read_one, parquet_files))

    return pa.concat_tables(tables).to_pandas()


# ------------------------------------------------------------------
# Core: build the historicized changes DataFrame (pure pandas)
# ------------------------------------------------------------------

def build_historicized_changes_pandas(table_path: str) -> pd.DataFrame:
    """Build a pandas DataFrame of every price change across all Hudi commits.

    Returns one row per actual price movement.  First appearances
    (inserts at the earliest commit for a given key) have
    ``prev_price = NaN`` and ``delta = NaN``.

    Columns: commit_instant, station_id, station_name, city, region,
    fuel_type, prev_price, price, delta, fetched_at.
    """
    df = _read_all_parquet(table_path)
    if df.empty:
        logger.warning("No parquet data found at %s.", table_path)
        return pd.DataFrame(columns=[
            "commit_instant", *_KEY_COLS, *_DATA_COLS, "prev_price", "delta",
        ])

    df = df.rename(columns={_HOODIE_COMMIT: "commit_instant"})

    # Sort so that shift(1) within each group gives the previous version
    df = df.sort_values([*_KEY_COLS, "commit_instant"]).reset_index(drop=True)

    # Vectorised prev_price via grouped shift
    df["prev_price"] = df.groupby(_KEY_COLS)["price"].shift(1)
    df["delta"] = (df["price"] - df["prev_price"]).round(2)

    # Keep only actual changes: price differs OR first appearance (prev_price NaN)
    changed = df["prev_price"].isna() | ((df["price"] - df["prev_price"]).abs() > 0.001)
    df = df.loc[changed].reset_index(drop=True)

    # Reorder columns to match the documented schema
    col_order = [
        "commit_instant", *_KEY_COLS,
        "station_name", "city", "region",
        "prev_price", "price", "delta", "fetched_at",
    ]
    return df[col_order].sort_values(
        ["commit_instant", "station_id", "fuel_type"],
    ).reset_index(drop=True)


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
