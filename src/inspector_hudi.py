"""Hudi implementation of the Jerry-Can data store inspector."""

import json
import os
from datetime import datetime, timezone

from pyspark.sql import functions as F

from src.config import HUDI_TABLE_NAME, HUDI_TABLE_PATH
from src.hudi_writer import _get_spark
from src.inspector import Inspector


def _is_missing_table_error(exc: Exception) -> bool:
    """Return True when the exception indicates the Hudi table path does not exist."""
    return "FileNotFoundException" in str(exc) or "does not exist" in str(exc).lower()


class HudiInspector(Inspector):
    """Inspect a Jerry-Can Hudi table via Spark."""

    def __init__(self, table_path: str = HUDI_TABLE_PATH) -> None:
        self._table_path = table_path

    def _read_table(self):
        """Return a Spark DataFrame for the Hudi table."""
        spark = _get_spark()
        return spark.read.format("hudi").load(self._table_path)

    def _load_table(self, empty_message: str):
        """Load the table, printing context-appropriate error and returning None on failure."""
        try:
            return self._read_table()
        except Exception as exc:
            if _is_missing_table_error(exc):
                print(
                    f"{empty_message}\n"
                    f"  Table path '{self._table_path}' does not exist.\n"
                    "  Run 'python main.py' with PERSISTENCE_BACKEND=hudi to collect data first."
                )
            elif isinstance(exc, RuntimeError):
                print(f"Spark error: {exc}")
            else:
                print(empty_message)
            return None

    def show_schema(self) -> None:
        df = self._load_table("No Hudi table found.")
        if df is None:
            return

        print("Schema:")
        print(f"\n  {HUDI_TABLE_NAME} ({df.count()} rows)")
        print(f"    {'Column':<30} {'Type':<15}")
        print(f"    {'-'*30} {'-'*15}")
        for field in df.schema.fields:
            print(f"    {field.name:<30} {str(field.dataType):<15}")

    def show_summary(self) -> None:
        df = self._load_table("No Hudi table found.")
        if df is None:
            return

        total = df.count()
        stations = df.select("station_id").distinct().count()
        snapshots = df.select("fetched_at").distinct().count()

        print(f"Hudi table: {self._table_path}")
        print(f"  Stations: {stations}")
        print(f"  Price records: {total}")
        print(f"  Snapshots: {snapshots}")

        if total:
            row = df.agg(
                F.min("fetched_at").alias("first"),
                F.max("fetched_at").alias("last"),
            ).collect()[0]
            print(f"  First snapshot: {row['first']}")
            print(f"  Last snapshot:  {row['last']}")

    def show_latest_prices(self, limit: int = 20) -> None:
        df = self._load_table("\nNo price data yet.")
        if df is None:
            return

        if df.count() == 0:
            print("\nNo price data yet.")
            return

        max_fetched = df.agg(F.max("fetched_at")).collect()[0][0]
        latest = df.filter(F.col("fetched_at") == max_fetched).orderBy("price").limit(limit)
        rows = latest.collect()

        print(f"\nLowest prices (snapshot {max_fetched}):")
        print(f"{'Station':<40} {'City':<20} {'Fuel':<10} {'Price':>8}")
        print("-" * 82)
        for r in rows:
            name = (r["station_name"] or "")[:39]
            city = (r["city"] or "")[:19]
            print(f"{name:<40} {city:<20} {r['fuel_type']:<10} {r['price']:>7.1f}¢")

    def show_regions(self) -> None:
        df = self._load_table("\nNo stations yet.")
        if df is None:
            return

        regions = (
            df.select("station_id", "region")
            .distinct()
            .groupBy("region")
            .agg(F.count("station_id").alias("cnt"))
            .orderBy(F.desc("cnt"))
            .collect()
        )

        if not regions:
            print("\nNo stations yet.")
            return

        print(f"\n{'Region':<40} {'Stations':>8}")
        print("-" * 50)
        for r in regions:
            region = r["region"] or "N/A"
            print(f"{region:<40} {r['cnt']:>8}")

    def show_snapshots(self) -> None:
        hoodie_dir = os.path.join(self._table_path, ".hoodie")
        if not os.path.isdir(hoodie_dir):
            print("\nNo commits yet.")
            return

        commits = []
        for fname in sorted(os.listdir(hoodie_dir), reverse=True):
            if not fname.endswith(".commit"):
                continue
            instant = fname[:-7]  # strip ".commit"
            try:
                # Hudi instant format: YYYYMMDDHHmmssSSS
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
