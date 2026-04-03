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

        # Station count per region
        region_counts = (
            df.select("station_id", "region")
            .distinct()
            .groupBy("region")
            .agg(F.count("station_id").alias("cnt"))
            .orderBy(F.desc("cnt"))
            .collect()
        )

        if not region_counts:
            print("\nNo stations yet.")
            return

        # Average price per (region, fuel_type) from latest snapshot
        avg_rows = (
            df.groupBy("region", "fuel_type")
            .agg(F.round(F.avg("price"), 1).alias("avg_price"))
            .collect()
        )
        # Build lookup: {region: {fuel_type: avg_price}}
        avg_map: dict = {}
        for row in avg_rows:
            avg_map.setdefault(row["region"] or "N/A", {})[row["fuel_type"]] = row["avg_price"]

        fuel_types = sorted({row["fuel_type"] for row in avg_rows})

        col_w = 10
        header = f"\n{'Région':<40} {'Stations':>8}"
        for ft in fuel_types:
            header += f"  {ft[:col_w]:>{col_w}}"
        print(header)
        print("-" * (50 + (col_w + 2) * len(fuel_types)))
        for r in region_counts:
            region = r["region"] or "N/A"
            line = f"{region:<40} {r['cnt']:>8}"
            prices = avg_map.get(region, {})
            for ft in fuel_types:
                val = prices.get(ft)
                cell = f"{val:.1f}¢" if val is not None else "  —"
                line += f"  {cell:>{col_w}}"
            print(line)

    def show_price_variations(self, limit: int = 10) -> None:
        from pyspark.sql import Window
        from src.reader import HudiReader

        try:
            df_changes = HudiReader(self._table_path).read_changes()
        except Exception as exc:
            if _is_missing_table_error(exc):
                print(
                    "\nNo price data yet.\n"
                    f"  Table path '{self._table_path}' does not exist.\n"
                    "  Run 'python main.py' with PERSISTENCE_BACKEND=hudi to collect data first."
                )
            else:
                print(f"\nImpossible de lire l'historique CDC: {type(exc).__name__}")
                print("  Les logs CDC sont peut-être corrompus. Supprimer le dossier data/ et relancer.")
            return

        # Only real price-change events: updates where price actually moved
        df_pairs = df_changes.filter(
            (F.col("op") == "u") & F.col("delta").isNotNull() & (F.col("delta") != 0)
        )

        if df_pairs.count() == 0:
            print("\nNo price variations yet (need at least 2 snapshots).")
            return

        # Last actual change per station/fuel_type
        w2 = Window.partitionBy("station_id", "fuel_type").orderBy(F.desc("fetched_at"))
        df_last = (
            df_pairs
            .withColumn("rn", F.row_number().over(w2))
            .filter(F.col("rn") == 1)
            .drop("rn")
        )

        # Count stations that changed in the most recent commit
        latest_ts = df_pairs.agg(F.max("fetched_at")).collect()[0][0]
        changed_count = (
            df_pairs
            .filter(F.col("fetched_at") == latest_ts)
            .select("station_id")
            .distinct()
            .count()
        )

        print(f"\n{changed_count} station(s) ont changé de prix lors du dernier snapshot.")

        header = f"{'Station':<35} {'City':<18} {'Fuel':<10} {'Avant':>8} {'Après':>8} {'Δ':>7}  {'De':<20} {'À'}"
        sep = "-" * 120

        rises = df_last.orderBy(F.desc("delta")).limit(limit).collect()
        drops = df_last.orderBy(F.asc("delta")).limit(limit).collect()

        def _print_rows(title, rows_list):
            print(f"\n=== {title} (top {limit}) ===")
            print(header)
            print(sep)
            for r in rows_list:
                delta = r["delta"] or 0
                name = (r["station_name"] or "")[:34]
                city = (r["city"] or "")[:17]
                print(
                    f"{name:<35} {city:<18} {r['fuel_type']:<10}"
                    f" {r['prev_price']:>7.1f}¢ {r['price']:>7.1f}¢"
                    f" {delta:+.1f}¢  {r['prev_fetched_at']:<20} {r['fetched_at']}"
                )

        _print_rows("Hausses", rises)
        _print_rows("Baisses", drops)

    def show_snapshots(self) -> None:
        hoodie_dir = os.path.join(self._table_path, ".hoodie")
        if not os.path.isdir(hoodie_dir):
            print("\nNo commits yet.")
            return

        commits = []
        for fname in sorted(os.listdir(hoodie_dir), reverse=True):
            if fname.endswith(".commit"):
                instant = fname[:-7]  # strip ".commit"
            elif fname.endswith(".deltacommit"):
                instant = fname[:-12]  # strip ".deltacommit"
            else:
                continue
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

        # Count actual price changes per commit via CDC (MOR only).
        # One Spark read, grouped by _hoodie_commit_time.
        price_changes_by_commit: dict = {}
        try:
            from pyspark.sql import functions as F
            cdc_df = (
                _get_spark().read.format("hudi")
                .option("hoodie.datasource.query.type", "incremental")
                .option("hoodie.datasource.query.incremental.format", "cdc")
                .option("hoodie.datasource.read.begin.instanttime", "000")
                .load(self._table_path)
            )
            rows = (
                cdc_df
                .filter(F.col("op") == "u")
                .withColumn("after_price",  F.get_json_object(F.col("after"),  "$.price").cast("double"))
                .withColumn("before_price", F.get_json_object(F.col("before"), "$.price").cast("double"))
                .filter(F.col("after_price") != F.col("before_price"))
                .withColumn("commit_time", F.get_json_object(F.col("after"), "$._hoodie_commit_time"))
                .groupBy("commit_time")
                .agg(F.count("*").alias("n"))
                .collect()
            )
            price_changes_by_commit = {r["commit_time"]: r["n"] for r in rows}
        except Exception:
            pass  # CDC not available (COW table or first commit)

        has_cdc = bool(price_changes_by_commit)
        if has_cdc:
            print(f"\n{'Commit instant':<22} {'Timestamp (UTC)':<22} {'Inserts':>8} {'Updates':>8} {'Px changés':>10}")
            print("-" * 76)
        else:
            print(f"\n{'Commit instant':<22} {'Timestamp (UTC)':<22} {'Inserts':>8} {'Updates':>8}")
            print("-" * 64)

        for instant, ts, ins, upd in commits[:10]:
            ts_str = ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "?"
            if has_cdc:
                px = price_changes_by_commit.get(instant, 0)
                print(f"{instant:<22} {ts_str:<22} {ins:>8} {upd:>8} {px:>10}")
            else:
                print(f"{instant:<22} {ts_str:<22} {ins:>8} {upd:>8}")
