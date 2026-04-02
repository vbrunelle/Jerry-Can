"""Simple script to inspect the Jerry-Can database contents."""

import sqlite3
import sys

from src.config import DATABASE_PATH
from src.database import connect


def show_schema(db_path: str) -> None:
    with connect(db_path) as conn:
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


def show_summary(db_path: str) -> None:
    with connect(db_path) as conn:
        stations = conn.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
        prices = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        snapshots = conn.execute(
            "SELECT COUNT(DISTINCT fetched_at) FROM prices"
        ).fetchone()[0]

        print(f"Database: {db_path}")
        print(f"  Stations: {stations}")
        print(f"  Price records: {prices}")
        print(f"  Snapshots: {snapshots}")

        if prices:
            row = conn.execute(
                "SELECT MIN(fetched_at), MAX(fetched_at) FROM prices"
            ).fetchone()
            print(f"  First snapshot: {row[0]}")
            print(f"  Last snapshot:  {row[1]}")


def show_latest_prices(db_path: str, limit: int = 20) -> None:
    with connect(db_path) as conn:
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


def show_regions(db_path: str) -> None:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT region, COUNT(*) as cnt
            FROM stations
            GROUP BY region
            ORDER BY cnt DESC
            """
        ).fetchall()

        if not rows:
            print("\nNo stations yet.")
            return

        print(f"\n{'Region':<40} {'Stations':>8}")
        print("-" * 50)
        for r in rows:
            print(f"{(r['region'] or 'N/A'):<40} {r['cnt']:>8}")


def show_snapshots(db_path: str) -> None:
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT fetched_at, COUNT(*) as cnt
            FROM prices
            GROUP BY fetched_at
            ORDER BY fetched_at DESC
            LIMIT 10
            """
        ).fetchall()

        if not rows:
            print("\nNo snapshots yet.")
            return

        print(f"\n{'Snapshot':<30} {'Records':>8}")
        print("-" * 40)
        for r in rows:
            print(f"{r['fetched_at']:<30} {r['cnt']:>8}")


def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else DATABASE_PATH
    show_schema(db_path)
    show_summary(db_path)
    show_snapshots(db_path)
    show_regions(db_path)
    show_latest_prices(db_path)


if __name__ == "__main__":
    main()
