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


def get_snapshots_data(table_path: str, limit: int = 10) -> list[dict]:
    """Return snapshot rows as a list of dicts (same data as show_snapshots()).

    Each dict has: fetched_at (str), record_count (int|None), changes (int|None).
    Sorted by fetched_at descending.
    """
    hist = build_historicized_changes_pandas(table_path)
    if hist.empty:
        return []

    all_df = _read_all_parquet(table_path)
    record_counts: dict = {}
    if not all_df.empty:
        record_counts = all_df.groupby("fetched_at").size().to_dict()

    changes_per_snap = hist.groupby("fetched_at").size()

    all_ts = sorted(
        set(record_counts.keys()) | set(changes_per_snap.index),
        reverse=True,
    )[:limit]

    result = []
    for ts in all_ts:
        key = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        rec = record_counts.get(ts)
        chg = int(changes_per_snap[ts]) if ts in changes_per_snap.index else None
        result.append({
            "fetched_at": key,
            "record_count": int(rec) if rec is not None else None,
            "changes": chg,
        })
    return result


# ------------------------------------------------------------------
# Helpers for the management-server dashboard (Hudi-native reads)
# ------------------------------------------------------------------

def get_snapshot_dates(table_path: str) -> list[str]:
    """Return distinct snapshot dates (YYYY-MM-DD) from Hudi, most recent first."""
    all_df = _read_all_parquet(table_path)
    if all_df.empty:
        return []

    fetched = pd.to_datetime(all_df["fetched_at"], errors="coerce")
    dates = fetched.dt.date.dropna().unique()
    return sorted((d.isoformat() for d in dates), reverse=True)


def get_snapshots_for_date(table_path: str, date_str: str) -> list[dict]:
    """Return snapshots for a given date (YYYY-MM-DD) from Hudi, most recent first.

    Each dict has: fetched_at (str), record_count (int).
    """
    all_df = _read_all_parquet(table_path)
    if all_df.empty:
        return []

    fetched = pd.to_datetime(all_df["fetched_at"], errors="coerce")
    mask = fetched.dt.date.astype(str) == date_str
    subset = all_df.loc[mask]
    if subset.empty:
        return []

    counts = subset.groupby("fetched_at").size().reset_index(name="record_count")
    counts = counts.sort_values("fetched_at", ascending=False)
    return [
        {"fetched_at": str(row["fetched_at"]), "record_count": int(row["record_count"])}
        for _, row in counts.iterrows()
    ]


def get_current_prices(table_path: str) -> tuple[list[dict], str | None]:
    """Return (prices, latest_ts) from the most recent snapshot in Hudi.

    *prices* is a list of dicts with keys: station_name, city, region,
    fuel_type, price.  *latest_ts* is the ``fetched_at`` value of the
    latest snapshot, or ``None`` when no data exists.
    """
    all_df = _read_all_parquet(table_path)
    if all_df.empty:
        return [], None

    latest_ts = all_df["fetched_at"].max()
    latest = all_df[all_df["fetched_at"] == latest_ts]

    latest = latest.sort_values(["region", "city", "station_name", "fuel_type"])
    prices = [
        {
            "station_name": str(row.get("station_name", "")),
            "city": str(row.get("city", "")),
            "region": str(row.get("region", "")),
            "fuel_type": str(row.get("fuel_type", "")),
            "price": float(row["price"]),
        }
        for _, row in latest.iterrows()
    ]
    ts_str = latest_ts.isoformat() if hasattr(latest_ts, "isoformat") else str(latest_ts)
    return prices, ts_str


def get_inspection_data(table_path: str) -> dict[str, Any]:
    """Return a dict with inspection data read entirely from Hudi.

    Keys: summary, regions, latest_prices, snapshots, price_variations.
    """
    all_df = _read_all_parquet(table_path)

    empty = {
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

    if all_df.empty:
        return empty

    # --- Summary ---
    station_count = int(all_df["station_id"].nunique())
    price_count = len(all_df)
    snapshot_count = int(all_df["fetched_at"].nunique())
    first_snapshot = str(all_df["fetched_at"].min())
    last_snapshot = str(all_df["fetched_at"].max())

    summary = {
        "station_count": station_count,
        "price_count": price_count,
        "snapshot_count": snapshot_count,
        "first_snapshot": first_snapshot,
        "last_snapshot": last_snapshot,
    }

    # --- Regions ---
    stations = all_df.drop_duplicates(subset=["station_id"])[
        ["station_id", "region"]
    ]
    region_counts = stations.groupby("region").size().reset_index(name="station_count")

    latest_ts = all_df["fetched_at"].max()
    latest = all_df[all_df["fetched_at"] == latest_ts]
    avg_prices = (
        latest.groupby(["region", "fuel_type"])["price"]
        .mean()
        .round(1)
        .reset_index()
    )
    avg_map: dict[str, dict[str, float]] = {}
    for _, row in avg_prices.iterrows():
        region_key = row["region"] or "N/A"
        avg_map.setdefault(region_key, {})[row["fuel_type"]] = row["price"]

    regions = []
    for _, row in region_counts.sort_values("station_count", ascending=False).iterrows():
        region_name = row["region"] or "N/A"
        regions.append({
            "region": region_name,
            "station_count": int(row["station_count"]),
            "avg_prices": avg_map.get(region_name, {}),
        })

    # --- Latest prices (lowest 50) ---
    latest_sorted = latest.sort_values("price").head(50)
    latest_prices = [
        {
            "station_name": str(r.get("station_name", "")),
            "city": str(r.get("city", "")),
            "region": str(r.get("region", "")),
            "fuel_type": str(r.get("fuel_type", "")),
            "price": float(r["price"]),
        }
        for _, r in latest_sorted.iterrows()
    ]

    # --- Snapshots (last 10) ---
    snapshots = get_snapshots_data(table_path, limit=10)

    # --- Price variations ---
    hist = build_historicized_changes_pandas(table_path)
    increases: list[dict] = []
    decreases: list[dict] = []
    if not hist.empty:
        updates = hist.dropna(subset=["delta"])
        if not updates.empty:
            # Most recent change per (station_id, fuel_type)
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
