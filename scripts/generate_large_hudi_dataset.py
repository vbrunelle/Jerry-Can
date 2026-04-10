"""Generate a large-scale deterministic Hudi dataset for performance benchmarking.

This script creates a synthetic fuel price table simulating months of Hudi history
with a configurable number of commits, stations and price changes.  All randomness
uses a local ``random.Random(seed)`` instance — never ``random.seed()`` — so the
same seed always produces exactly the same dataset.

Usage
-----
    # Inside the jerry-can container:
    docker exec jerry-can-jerry-can-1 python scripts/generate_large_hudi_dataset.py

    # With custom parameters:
    docker exec jerry-can-jerry-can-1 python scripts/generate_large_hudi_dataset.py \\
        --stations 3000 --commits 500 --seed 42 --output /tmp/jerry_can_scale_test

    # Overwrite an existing table:
    docker exec jerry-can-jerry-can-1 python scripts/generate_large_hudi_dataset.py --force
"""

import argparse
import json
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REGIONS = ["Montréal", "Québec", "Laurentides", "Estrie", "Outaouais", "Saguenay", "Mauricie"]
_BASE_PRICE_MIN = 140.0
_BASE_PRICE_MAX = 185.0
_PRICE_DELTA_MIN = -5.0
_PRICE_DELTA_MAX = 5.0
_COMMIT_INTERVAL_MINUTES = 5
_START_TIMESTAMP = datetime(2026, 1, 1, 0, 0, 0)
_DEFAULT_OUTPUT = "/tmp/jerry_can_scale_test"
_DEFAULT_STATIONS = 3000
_DEFAULT_COMMITS = 500
_DEFAULT_FUEL_TYPES = ["regular", "premium", "diesel"]
_DEFAULT_PRICE_CHANGE_RATE = 0.15
_DEFAULT_SEED = 42


# ---------------------------------------------------------------------------
# Dataset generator
# ---------------------------------------------------------------------------

class ScaleDatasetGenerator:
    """Generate a deterministic large-scale Hudi dataset for benchmarking.

    All randomness is driven by a single local ``random.Random`` instance
    seeded at construction time.  The same ``seed`` value always produces
    the same stations, same commit history and same price changes.
    """

    def __init__(
        self,
        stations: int,
        commits: int,
        fuel_types: list[str],
        price_change_rate: float,
        seed: int,
        output_path: str,
    ) -> None:
        self.stations = stations
        self.commits = commits
        self.fuel_types = fuel_types
        self.price_change_rate = price_change_rate
        self.seed = seed
        self.output_path = output_path
        # Local RNG instance — no global random.seed() is ever called.
        self._rng = random.Random(seed)

    def _generate_stations(self) -> list[dict[str, Any]]:
        """Generate the fixed list of synthetic stations (deterministic via self._rng)."""
        station_list = []
        for i in range(self.stations):
            region = _REGIONS[i % len(_REGIONS)]
            base_price = round(
                self._rng.uniform(_BASE_PRICE_MIN, _BASE_PRICE_MAX), 1
            )
            station_list.append(
                {
                    "station_id": f"STA{i:06d}",
                    "station_name": f"Station {i}",
                    "address": f"{i * 10} Rue Principale",
                    "city": f"Ville-{region}-{i % 20}",
                    "region": region,
                    "latitude": round(45.0 + self._rng.uniform(-3.0, 3.0), 6),
                    "longitude": round(-73.0 + self._rng.uniform(-4.0, 4.0), 6),
                    # One base price per station — fuel-type multipliers applied below
                    "_base_price": base_price,
                }
            )
        return station_list

    def _fuel_price(self, base: float, fuel_type: str) -> float:
        """Return a fuel-type-adjusted price from a station base price."""
        offsets = {"regular": 0.0, "premium": 8.0, "diesel": -4.0}
        return round(base + offsets.get(fuel_type, 0.0), 1)

    def _generate_commit(
        self,
        stations: list[dict[str, Any]],
        current_prices: dict[tuple[str, str], float],
        timestamp: datetime,
    ) -> list[dict[str, Any]]:
        """Generate the records modified for a single commit.

        Selects a random subset of stations whose price changes and returns
        one record per (station, fuel_type) that changed.
        """
        fetched_at = timestamp.strftime("%Y-%m-%dT%H:%M:%S")
        records = []
        for station in stations:
            if self._rng.random() >= self.price_change_rate:
                continue  # no change for this station this commit
            delta = round(
                self._rng.uniform(_PRICE_DELTA_MIN, _PRICE_DELTA_MAX), 1
            )
            for fuel_type in self.fuel_types:
                key = (station["station_id"], fuel_type)
                new_price = round(
                    max(_BASE_PRICE_MIN, min(_BASE_PRICE_MAX, current_prices[key] + delta)),
                    1,
                )
                current_prices[key] = new_price
                records.append(
                    {
                        "station_id": station["station_id"],
                        "station_name": station["station_name"],
                        "address": station["address"],
                        "city": station["city"],
                        "region": station["region"],
                        "latitude": station["latitude"],
                        "longitude": station["longitude"],
                        "fuel_type": fuel_type,
                        "price": new_price,
                        "fetched_at": fetched_at,
                    }
                )
        return records

    def _build_initial_snapshot(
        self,
        stations: list[dict[str, Any]],
        timestamp: datetime,
    ) -> tuple[list[dict[str, Any]], dict[tuple[str, str], float]]:
        """Build the initial snapshot (all stations × all fuel types) and price map."""
        fetched_at = timestamp.strftime("%Y-%m-%dT%H:%M:%S")
        records = []
        current_prices: dict[tuple[str, str], float] = {}
        for station in stations:
            for fuel_type in self.fuel_types:
                price = self._fuel_price(station["_base_price"], fuel_type)
                key = (station["station_id"], fuel_type)
                current_prices[key] = price
                records.append(
                    {
                        "station_id": station["station_id"],
                        "station_name": station["station_name"],
                        "address": station["address"],
                        "city": station["city"],
                        "region": station["region"],
                        "latitude": station["latitude"],
                        "longitude": station["longitude"],
                        "fuel_type": fuel_type,
                        "price": price,
                        "fetched_at": fetched_at,
                    }
                )
        return records, current_prices

    def run(self) -> dict[str, Any]:
        """Generate all commits and write them to Hudi one by one.

        Returns a metadata dict suitable for ``_generation_metadata.json``.
        """
        # Import Hudi writer to get SparkSession and HUDI_OPTIONS
        try:
            from src.hudi_writer import HUDI_OPTIONS, _get_spark
        except ImportError:
            from hudi_writer import HUDI_OPTIONS, _get_spark  # type: ignore[no-redef]

        spark = _get_spark()

        print(f"\n{'=' * 70}")
        print(f"  Jerry-Can — Scale Dataset Generator")
        print(f"  Output   : {self.output_path}")
        print(f"  Stations : {self.stations}")
        print(f"  Commits  : {self.commits}")
        print(f"  Fuels    : {self.fuel_types}")
        print(f"  Rate     : {self.price_change_rate}")
        print(f"  Seed     : {self.seed}")
        print(f"{'=' * 70}\n")

        t_total_start = time.perf_counter()

        # --- Generate station metadata (deterministic) ---
        print("Generating station metadata …")
        stations = self._generate_stations()

        # --- Commit 0: initial snapshot of every station ---
        ts = _START_TIMESTAMP
        print(f"Writing commit 0/{self.commits} (initial snapshot — {len(stations) * len(self.fuel_types)} rows) …")
        t0 = time.perf_counter()
        initial_records, current_prices = self._build_initial_snapshot(stations, ts)
        df = spark.createDataFrame(initial_records)
        df.write.format("hudi").options(**HUDI_OPTIONS).mode("append").save(self.output_path)
        elapsed = time.perf_counter() - t0
        print(f"  ✓ {len(initial_records)} rows written in {elapsed:.1f}s")

        total_rows = len(initial_records)
        first_commit: str | None = None
        last_commit: str | None = None

        # Capture the first commit instant from the timeline
        try:
            from src.price_history import get_commit_instants
        except ImportError:
            from price_history import get_commit_instants  # type: ignore[no-redef]
        instants = get_commit_instants(self.output_path)
        if instants:
            first_commit = instants[0]

        # --- Commits 1..N: incremental price changes ---
        for commit_idx in range(1, self.commits + 1):
            ts = _START_TIMESTAMP + timedelta(minutes=_COMMIT_INTERVAL_MINUTES * commit_idx)
            records = self._generate_commit(stations, current_prices, ts)

            if not records:
                # No price changed this round — skip writing (avoids empty commit)
                continue

            t0 = time.perf_counter()
            df = spark.createDataFrame(records)
            df.write.format("hudi").options(**HUDI_OPTIONS).mode("append").save(self.output_path)
            elapsed = time.perf_counter() - t0
            total_rows += len(records)

            # Estimate remaining time
            done = commit_idx
            elapsed_total = time.perf_counter() - t_total_start
            avg_per_commit = elapsed_total / done
            remaining = avg_per_commit * (self.commits - done)

            print(
                f"  Commit {commit_idx}/{self.commits}  "
                f"{len(records):>6} rows  "
                f"{elapsed:.1f}s  "
                f"(~{remaining / 60:.1f} min remaining)"
            )

        # Capture last commit instant
        instants = get_commit_instants(self.output_path)
        if instants:
            last_commit = instants[-1]

        total_elapsed = time.perf_counter() - t_total_start
        print(f"\n{'=' * 70}")
        print(f"  Generation complete!")
        print(f"  Total time    : {total_elapsed:.1f}s ({total_elapsed / 60:.1f} min)")
        print(f"  Commits       : {self.commits}")
        print(f"  Total rows    : {total_rows}")
        print(f"  First commit  : {first_commit}")
        print(f"  Last commit   : {last_commit}")
        print(f"{'=' * 70}\n")

        metadata: dict[str, Any] = {
            "seed": self.seed,
            "stations": self.stations,
            "commits": self.commits,
            "fuel_types": self.fuel_types,
            "price_change_rate": self.price_change_rate,
            "total_rows_written": total_rows,
            "first_commit": first_commit,
            "last_commit": last_commit,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        }

        # Save metadata alongside the table
        meta_path = os.path.join(self.output_path, "_generation_metadata.json")
        with open(meta_path, "w") as fh:
            json.dump(metadata, fh, indent=2)
        print(f"Metadata saved → {meta_path}")

        return metadata


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a large-scale deterministic Hudi dataset for benchmarking.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--stations",
        type=int,
        default=_DEFAULT_STATIONS,
        help="Number of stations to generate",
    )
    parser.add_argument(
        "--commits",
        type=int,
        default=_DEFAULT_COMMITS,
        help="Number of Hudi commits to write (excluding initial snapshot)",
    )
    parser.add_argument(
        "--fuel-types",
        nargs="+",
        default=_DEFAULT_FUEL_TYPES,
        metavar="FUEL",
        help="Fuel types to include",
    )
    parser.add_argument(
        "--price-change-rate",
        type=float,
        default=_DEFAULT_PRICE_CHANGE_RATE,
        help="Probability that a station changes price each commit (0-1)",
    )
    parser.add_argument(
        "--output",
        default=_DEFAULT_OUTPUT,
        help="Output path for the Hudi table",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=_DEFAULT_SEED,
        help="Random seed for deterministic generation",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete and recreate the table if it already exists",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if os.path.exists(args.output):
        if args.force:
            print(f"--force: removing existing table at {args.output} …")
            shutil.rmtree(args.output)
        else:
            print(
                f"Error: output path already exists: {args.output}\n"
                f"Use --force to overwrite.",
                file=sys.stderr,
            )
            sys.exit(1)

    generator = ScaleDatasetGenerator(
        stations=args.stations,
        commits=args.commits,
        fuel_types=args.fuel_types,
        price_change_rate=args.price_change_rate,
        seed=args.seed,
        output_path=args.output,
    )
    generator.run()


if __name__ == "__main__":
    main()
