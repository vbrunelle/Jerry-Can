"""Lightweight Hudi inspector using pandas + pyarrow (no Spark/JVM).

Reads the base Parquet files from the Hudi table's partition directories
directly, bypassing the Spark/Hudi read path entirely.  This is equivalent
to a ``read_optimized`` query (only base files, no MOR log merge) but
starts in milliseconds instead of seconds.

For a dataset of ~6 500 rows this cuts inspection time from ~13 s (Spark)
to < 1 s (pandas).
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow.parquet as pq

from src.config import HUDI_TABLE_NAME, HUDI_TABLE_PATH
from src.inspector import Inspector


def _read_hudi_parquet(table_path: str) -> pd.DataFrame:
    """Read all base Parquet files from a Hudi table into a pandas DataFrame.

    Walks the partition directories and reads every ``*.parquet`` file,
    skipping Hudi internal directories (``.hoodie``, ``.hoodie_partition_metadata``).
    """
    parquet_files: list[str] = []
    for root, dirs, files in os.walk(table_path):
        # Skip Hudi metadata directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".parquet"):
                parquet_files.append(os.path.join(root, f))

    if not parquet_files:
        return pd.DataFrame()

    # Read all parquet files and concatenate
    dfs = [pq.read_table(fp).to_pandas() for fp in parquet_files]
    return pd.concat(dfs, ignore_index=True)


class PandasHudiInspector(Inspector):
    """Inspect a Jerry-Can Hudi table using pandas (no Spark required)."""

    def __init__(self, table_path: str = HUDI_TABLE_PATH) -> None:
        self._table_path = table_path
        self._df: Optional[pd.DataFrame] = None

    def _load(self) -> Optional[pd.DataFrame]:
        if self._df is not None:
            return self._df
        if not os.path.isdir(self._table_path):
            print(
                f"No Hudi table found.\n"
                f"  Table path '{self._table_path}' does not exist.\n"
                "  Run 'python main.py' with PERSISTENCE_BACKEND=hudi to collect data first."
            )
            return None
        df = _read_hudi_parquet(self._table_path)
        if df.empty:
            print("No data found in Hudi table.")
            return None
        self._df = df
        return df

    def show_all(self) -> None:
        df = self._load()
        if df is None:
            return
        self.show_schema()
        self.show_summary()
        self.show_snapshots()
        self.show_regions()
        self.show_latest_prices()
        self.show_price_variations()

    def show_schema(self) -> None:
        df = self._load()
        if df is None:
            return
        print("Schema:")
        print(f"\n  {HUDI_TABLE_NAME} ({len(df)} rows)")
        print(f"    {'Column':<30} {'Type':<15}")
        print(f"    {'-'*30} {'-'*15}")
        for col in df.columns:
            print(f"    {col:<30} {str(df[col].dtype):<15}")

    def show_summary(self) -> None:
        df = self._load()
        if df is None:
            return

        total = len(df)
        stations = df["station_id"].nunique()
        snapshots = df["fetched_at"].nunique()
        first = df["fetched_at"].min()
        last = df["fetched_at"].max()

        print(f"Hudi table: {self._table_path}")
        print(f"  Stations: {stations}")
        print(f"  Price records: {total}")
        print(f"  Snapshots: {snapshots}")
        if total:
            print(f"  First snapshot: {first}")
            print(f"  Last snapshot:  {last}")

    def show_latest_prices(self, limit: int = 20) -> None:
        df = self._load()
        if df is None or df.empty:
            print("\nNo price data yet.")
            return

        max_fetched = df["fetched_at"].max()
        latest = df[df["fetched_at"] == max_fetched].nsmallest(limit, "price")

        print(f"\nLowest prices (snapshot {max_fetched}):")
        print(f"{'Station':<40} {'City':<20} {'Fuel':<10} {'Price':>8}")
        print("-" * 82)
        for _, r in latest.iterrows():
            name = (r.get("station_name") or "")[:39]
            city = (r.get("city") or "")[:19]
            print(f"{name:<40} {city:<20} {r['fuel_type']:<10} {r['price']:>7.1f}¢")

    def show_regions(self) -> None:
        df = self._load()
        if df is None or df.empty:
            print("\nNo stations yet.")
            return

        # Station count per region
        region_stations = (
            df[["station_id", "region"]]
            .drop_duplicates()
            .groupby("region")["station_id"]
            .count()
            .sort_values(ascending=False)
        )

        if region_stations.empty:
            print("\nNo stations yet.")
            return

        # Average price per (region, fuel_type)
        avg_prices = df.groupby(["region", "fuel_type"])["price"].mean().round(1)

        fuel_types = sorted(df["fuel_type"].unique())
        col_w = 10

        header = f"\n{'Région':<40} {'Stations':>8}"
        for ft in fuel_types:
            header += f"  {ft[:col_w]:>{col_w}}"
        print(header)
        print("-" * (50 + (col_w + 2) * len(fuel_types)))

        for region, cnt in region_stations.items():
            line = f"{region:<40} {cnt:>8}"
            for ft in fuel_types:
                try:
                    val = avg_prices.loc[(region, ft)]
                    cell = f"{val:.1f}¢"
                except KeyError:
                    cell = "  —"
                line += f"  {cell:>{col_w}}"
            print(line)

    def show_price_variations(self, limit: int = 10) -> None:
        from src.price_history import get_latest_changes_summary

        try:
            summary = get_latest_changes_summary(self._table_path, limit=limit)
        except Exception as exc:
            print(f"\nImpossible d'afficher les variations de prix: {exc}")
            return

        total = summary["total_changes"]
        if total == 0:
            print("\nNo price variations yet (need at least 2 snapshots).")
            return

        print(
            f"\n{total} changement(s) de prix sur {summary['commit_count']} commits"
            f" ({summary['latest_changed_count']} dans le dernier commit)."
        )

        header = f"{'Station':<35} {'City':<18} {'Fuel':<10} {'Avant':>8} {'Après':>8} {'Δ':>7}  {'Date'}"
        sep = "-" * 110

        def _print_rows(title, rows_list):
            print(f"\n=== {title} (top {limit}) ===")
            print(header)
            print(sep)
            for r in rows_list:
                delta = r.get("delta") or 0
                name = (r.get("station_name") or "")[:34]
                city = (r.get("city") or "")[:17]
                prev = r.get("prev_price")
                prev_str = f"{prev:>7.1f}¢" if prev is not None else "    N/A"
                print(
                    f"{name:<35} {city:<18} {r['fuel_type']:<10}"
                    f" {prev_str} {r['price']:>7.1f}¢"
                    f" {delta:+.1f}¢  {r.get('fetched_at', '?')}"
                )

        _print_rows("Hausses", summary["rises"])
        _print_rows("Baisses", summary["drops"])

    def show_snapshots(self) -> None:
        hoodie_dir = os.path.join(self._table_path, ".hoodie")
        if not os.path.isdir(hoodie_dir):
            print("\nNo commits yet.")
            return

        commits = []
        for fname in sorted(os.listdir(hoodie_dir), reverse=True):
            if fname.endswith(".commit"):
                instant = fname[:-7]
            elif fname.endswith(".deltacommit"):
                instant = fname[:-12]
            else:
                continue
            try:
                ts = datetime.strptime(instant[:17].ljust(17, "0"), "%Y%m%d%H%M%S%f")
                ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = None
            try:
                with open(os.path.join(hoodie_dir, fname)) as fh:
                    data = json.load(fh)
                stats = data.get("partitionToWriteStats", {})
                inserts = sum(s.get("numInserts", 0) for p in stats.values() for s in p)
                updates = sum(s.get("numUpdateWrites", 0) for p in stats.values() for s in p)
            except Exception:
                inserts = updates = 0
            commits.append((instant, ts, inserts, updates))

        if not commits:
            print("\nNo commits yet.")
            return

        print(f"\n{'Commit instant':<22} {'Timestamp (UTC)':<22} {'Inserts':>8} {'Updates':>8}")
        print("-" * 64)
        for instant, ts, ins, upd in commits[:10]:
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
            print(f"{instant:<22} {ts_str:<22} {ins:>8} {upd:>8}")
