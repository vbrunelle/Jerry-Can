"""SQLite implementation of the Jerry-Can data store inspector."""

import os

from src.database import connect
from src.inspector import Inspector


class SqliteInspector(Inspector):
    """Inspect a Jerry-Can SQLite database."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def _check_exists(self) -> None:
        """Raise FileNotFoundError if the database file does not exist."""
        if not os.path.exists(self._db_path):
            raise FileNotFoundError(
                f"Database not found: {self._db_path}\n"
                "Run 'python main.py' first to collect data."
            )

    def show_schema(self) -> None:
        self._check_exists()
        with connect(self._db_path) as conn:
            tables = [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            if not tables:
                print("No tables found.")
                return

            print("Schema:")
            for table in tables:
                cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
                row_count = conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0]
                print(f"\n  {table} ({row_count} rows)")
                print(f"    {'Column':<20} {'Type':<12} {'Nullable':<10} {'PK'}")
                print(f"    {'-'*20} {'-'*12} {'-'*10} {'-'*4}")
                for c in cols:
                    nullable = "NULL" if not c["notnull"] else "NOT NULL"
                    pk = "PK" if c["pk"] else ""
                    print(f"    {c['name']:<20} {(c['type'] or 'ANY'):<12} {nullable:<10} {pk}")

            indexes = conn.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            if indexes:
                print("\n  Indexes:")
                for idx in indexes:
                    print(f"    {idx['name']} ON {idx['tbl_name']}")

    def show_summary(self) -> None:
        self._check_exists()
        with connect(self._db_path) as conn:
            stations = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
            prices = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            snapshots = conn.execute(
                "SELECT COUNT(DISTINCT fetched_at) FROM prices"
            ).fetchone()[0]

            print(f"Database: {self._db_path}")
            print(f"  Stations: {stations}")
            print(f"  Price records: {prices}")
            print(f"  Snapshots: {snapshots}")

            if prices:
                row = conn.execute(
                    "SELECT MIN(fetched_at), MAX(fetched_at) FROM prices"
                ).fetchone()
                print(f"  First snapshot: {row[0]}")
                print(f"  Last snapshot:  {row[1]}")

    def show_latest_prices(self, limit: int = 20) -> None:
        self._check_exists()
        with connect(self._db_path) as conn:
            latest = conn.execute(
                "SELECT MAX(fetched_at) FROM prices"
            ).fetchone()[0]

            if not latest:
                print("\nNo price data yet.")
                return

            rows = conn.execute(
                """
                SELECT s.name, s.city, s.region, p.fuel_type, p.price
                FROM prices p
                JOIN stations s ON s.id = p.station_id
                WHERE p.fetched_at = ?
                ORDER BY p.price ASC
                LIMIT ?
                """,
                (latest, limit),
            ).fetchall()

            print(f"\nLowest prices (snapshot {latest}):")
            print(f"{'Station':<40} {'City':<20} {'Fuel':<10} {'Price':>8}")
            print("-" * 82)
            for r in rows:
                print(f"{r['name'][:39]:<40} {(r['city'] or '')[:19]:<20} {r['fuel_type']:<10} {r['price']:>7.1f}¢")

    def show_regions(self) -> None:
        self._check_exists()
        with connect(self._db_path) as conn:
            count_rows = conn.execute(
                """
                SELECT region, COUNT(*) as cnt
                FROM stations
                GROUP BY region
                ORDER BY cnt DESC
                """
            ).fetchall()

            if not count_rows:
                print("\nNo stations yet.")
                return

            # Average price per (region, fuel_type) using latest price per station.
            avg_rows = conn.execute(
                """
                SELECT s.region, p.fuel_type,
                       ROUND(AVG(p.price), 1) AS avg_price
                FROM prices p
                JOIN stations s ON s.id = p.station_id
                JOIN (
                    SELECT station_id, fuel_type, MAX(fetched_at) AS max_ts
                    FROM prices
                    GROUP BY station_id, fuel_type
                ) latest
                  ON p.station_id = latest.station_id
                 AND p.fuel_type  = latest.fuel_type
                 AND p.fetched_at = latest.max_ts
                GROUP BY s.region, p.fuel_type
                ORDER BY s.region
                """
            ).fetchall()

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
        for r in count_rows:
            region = r["region"] or "N/A"
            line = f"{region:<40} {r['cnt']:>8}"
            prices = avg_map.get(region, {})
            for ft in fuel_types:
                val = prices.get(ft)
                cell = f"{val:.1f}¢" if val is not None else "  —"
                line += f"  {cell:>{col_w}}"
            print(line)

    def show_snapshots(self) -> None:
        self._check_exists()
        with connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT fetched_at, COUNT(*) as cnt
                FROM prices
                GROUP BY fetched_at
                ORDER BY fetched_at DESC
                LIMIT 11
                """
            ).fetchall()

            if not rows:
                print("\nNo snapshots yet.")
                return

            snapshots = []
            for i, r in enumerate(rows[:10]):
                changes = None
                if i + 1 < len(rows):
                    cur_ts = r['fetched_at']
                    prev_ts = rows[i + 1]['fetched_at']
                    change_row = conn.execute(
                        """
                        SELECT
                            SUM(CASE WHEN prev_price IS NULL THEN 1 ELSE 0 END) AS inserts,
                            SUM(CASE WHEN prev_price IS NOT NULL AND cur_price != prev_price THEN 1 ELSE 0 END) AS updates
                        FROM (
                            SELECT c.station_id, c.fuel_type,
                                   c.price AS cur_price, p.price AS prev_price
                            FROM prices c
                            LEFT JOIN prices p
                                ON p.fetched_at = ? AND p.station_id = c.station_id
                               AND p.fuel_type = c.fuel_type
                            WHERE c.fetched_at = ?
                        )
                        """,
                        (prev_ts, cur_ts),
                    ).fetchone()
                    deletions = conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM prices p
                        WHERE p.fetched_at = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM prices c
                              WHERE c.fetched_at = ?
                                AND c.station_id = p.station_id
                                AND c.fuel_type  = p.fuel_type
                          )
                        """,
                        (prev_ts, cur_ts),
                    ).fetchone()[0]
                    changes = (change_row['inserts'] or 0) + (change_row['updates'] or 0) + deletions
                snapshots.append((r['fetched_at'], r['cnt'], changes))

            print(f"\n{'Snapshot':<30} {'Records':>8} {'Changements':>12}")
            print("-" * 52)
            for ts, cnt, changes in snapshots:
                changes_str = str(changes) if changes is not None else "—"
                print(f"{ts:<30} {cnt:>8} {changes_str:>12}")

    def show_price_variations(self, limit: int = 10) -> None:
        self._check_exists()
        # CTE: for each station/fuel_type, keep only the most recent row where
        # the price actually changed (price != prev_price).
        _CTE = """
            WITH all_pairs AS (
                SELECT
                    p.station_id,
                    s.name  AS station_name,
                    s.city,
                    p.fuel_type,
                    p.fetched_at,
                    p.price,
                    LAG(p.price) OVER (
                        PARTITION BY p.station_id, p.fuel_type
                        ORDER BY p.fetched_at
                    ) AS prev_price,
                    LAG(p.fetched_at) OVER (
                        PARTITION BY p.station_id, p.fuel_type
                        ORDER BY p.fetched_at
                    ) AS prev_fetched_at
                FROM prices p
                JOIN stations s ON s.id = p.station_id
            ),
            last_changes AS (
                SELECT *,
                    ROW_NUMBER() OVER (
                        PARTITION BY station_id, fuel_type
                        ORDER BY fetched_at DESC
                    ) AS rn
                FROM all_pairs
                WHERE prev_price IS NOT NULL AND price != prev_price
            ),
            final AS (
                SELECT
                    station_name, city, fuel_type,
                    prev_fetched_at, fetched_at,
                    ROUND(prev_price, 1) AS prev_price,
                    ROUND(price, 1)      AS price,
                    ROUND(price - prev_price, 1) AS delta
                FROM last_changes
                WHERE rn = 1
            )
        """
        with connect(self._db_path) as conn:
            rise_rows = conn.execute(
                _CTE + "SELECT * FROM final WHERE delta > 0 ORDER BY delta DESC LIMIT ?",
                (limit,),
            ).fetchall()
            drop_rows = conn.execute(
                _CTE + "SELECT * FROM final WHERE delta < 0 ORDER BY delta ASC LIMIT ?",
                (limit,),
            ).fetchall()
            changed_count = conn.execute(
                """
                WITH latest_ts AS (SELECT MAX(fetched_at) AS ts FROM prices),
                     prev_ts   AS (SELECT MAX(fetched_at) AS ts FROM prices
                                   WHERE fetched_at < (SELECT ts FROM latest_ts)),
                     latest    AS (SELECT station_id, fuel_type, price FROM prices
                                   WHERE fetched_at = (SELECT ts FROM latest_ts)),
                     prev      AS (SELECT station_id, fuel_type, price FROM prices
                                   WHERE fetched_at = (SELECT ts FROM prev_ts))
                SELECT COUNT(*) AS cnt
                FROM latest l
                JOIN prev p ON l.station_id = p.station_id AND l.fuel_type = p.fuel_type
                WHERE l.price != p.price
                """
            ).fetchone()[0]
            snapshot_count = conn.execute(
                "SELECT COUNT(DISTINCT fetched_at) FROM prices"
            ).fetchone()[0]

        if not rise_rows and not drop_rows:
            if snapshot_count < 2:
                print("\nNo price variations yet (need at least 2 snapshots).")
            else:
                print("\nNo price changes detected across all snapshots.")
            return

        print(f"\n{changed_count} station(s) ont changé de prix lors du dernier snapshot.")

        header = f"{'Station':<35} {'City':<18} {'Fuel':<10} {'Avant':>8} {'Après':>8} {'Δ':>7}  {'De':<20} {'À'}"
        sep = "-" * 120

        print(f"\n=== Hausses (top {limit}) ===")
        print(header)
        print(sep)
        for r in rise_rows:
            delta = r['delta'] if r['delta'] is not None else 0
            print(
                f"{(r['station_name'] or '')[:34]:<35} {(r['city'] or '')[:17]:<18}"
                f" {r['fuel_type']:<10} {r['prev_price']:>7.1f}¢ {r['price']:>7.1f}¢"
                f" {delta:+.1f}¢  {r['prev_fetched_at']:<20} {r['fetched_at']}"
            )

        print(f"\n=== Baisses (top {limit}) ===")
        print(header)
        print(sep)
        for r in drop_rows:
            delta = r['delta'] if r['delta'] is not None else 0
            print(
                f"{(r['station_name'] or '')[:34]:<35} {(r['city'] or '')[:17]:<18}"
                f" {r['fuel_type']:<10} {r['prev_price']:>7.1f}¢ {r['price']:>7.1f}¢"
                f" {delta:+.1f}¢  {r['prev_fetched_at']:<20} {r['fetched_at']}"
            )
