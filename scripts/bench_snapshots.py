"""Benchmark script comparing different Hudi snapshot read approaches.

Measures and compares three approaches to reading snapshot data from a Hudi table:

  A — Current (full CDC from "000"): ``build_historicized_changes_spark()``
  B — Bounded incremental (new approach): incremental query bounded by the last N commits
  C — read_optimized (Parquet baseline): ``read_optimized`` grouped by fetched_at

Usage
-----
    # Inside the jerry-can container:
    docker exec jerry-can-jerry-can-1 python scripts/bench_snapshots.py

    # With a custom table path:
    docker exec jerry-can-jerry-can-1 python scripts/bench_snapshots.py \\
        --table-path /tmp/jerry_can_scale_test

    # Save results:
    docker exec jerry-can-jerry-can-1 python scripts/bench_snapshots.py \\
        --table-path /tmp/jerry_can_scale_test --save bench_scale

Les résultats sont sauvegardés dans benchmark_results/<name>.json.
"""

import argparse
import json
import os
import time
from typing import Any

RESULTS_DIR = "benchmark_results"
_DEFAULT_TABLE_PATH = "/tmp/jerry_can_scale_test"
_DEFAULT_RUNS = 3
_DEFAULT_LIMIT = 10


# ---------------------------------------------------------------------------
# Approach A — Current: full CDC scan from instant "000"
# ---------------------------------------------------------------------------

def _approach_a(table_path: str, limit: int) -> None:
    """Approach A: build_historicized_changes_spark() — full CDC from '000'.

    This is the current production implementation that becomes extremely slow
    as MOR log files accumulate.
    """
    from src.price_history import build_historicized_changes_spark
    df = build_historicized_changes_spark(table_path)
    # Trigger a simple aggregation to ensure the DataFrame is fully materialised
    _ = df.groupby("fetched_at").size().to_dict()


# ---------------------------------------------------------------------------
# Approach B — Bounded incremental (proposed new approach)
# ---------------------------------------------------------------------------

def _approach_b(table_path: str, limit: int) -> None:
    """Approach B: incremental query bounded by the last N commit instants.

    Reads only the delta log files for the most recent ``limit`` commits
    instead of scanning from the beginning of history.
    """
    from src.price_history import get_commit_instants

    try:
        from src.hudi_writer import _get_spark
    except ImportError:
        from hudi_writer import _get_spark  # type: ignore[no-redef]

    instants = get_commit_instants(table_path)
    if not instants:
        return

    begin = instants[-(limit + 1)] if len(instants) > limit else instants[0]
    spark = _get_spark()

    (
        spark.read.format("hudi")
        .option("hoodie.datasource.query.type", "incremental")
        .option("hoodie.datasource.read.begin.instanttime", begin)
        .load(table_path)
        .groupBy("fetched_at")
        .count()
        .collect()
    )


# ---------------------------------------------------------------------------
# Approach C — read_optimized (Parquet baseline)
# ---------------------------------------------------------------------------

def _approach_c(table_path: str, limit: int) -> None:
    """Approach C: read_optimized grouped by fetched_at.

    Reads only compacted Parquet base files — no delta log merging.
    Does not see the most recent (uncompacted) commits, but is very fast.
    """
    try:
        from src.hudi_writer import _get_spark
    except ImportError:
        from hudi_writer import _get_spark  # type: ignore[no-redef]

    spark = _get_spark()

    (
        spark.read.format("hudi")
        .option("hoodie.datasource.query.type", "read_optimized")
        .load(table_path)
        .groupBy("fetched_at")
        .count()
        .collect()
    )


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

_APPROACHES: list[tuple[str, str, Any]] = [
    ("A", "Full CDC from '000' (current)", _approach_a),
    ("B", "Bounded incremental (proposed)", _approach_b),
    ("C", "read_optimized / Parquet baseline", _approach_c),
]


def _run_approach(
    label: str,
    description: str,
    fn: Any,
    table_path: str,
    runs: int,
    limit: int,
) -> dict[str, Any]:
    """Run a single approach *runs* times and return timing statistics."""
    print(f"\n▶ Approach {label} — {description}")
    times: list[float] = []

    for i in range(runs):
        t0 = time.perf_counter()
        fn(table_path, limit)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)
        print(f"  Run {i + 1}/{runs}: {elapsed:.3f}s")

    avg = sum(times) / len(times)
    minimum = min(times)
    maximum = max(times)

    print(f"  → avg={avg:.3f}s  min={minimum:.3f}s  max={maximum:.3f}s")

    return {
        "label": label,
        "description": description,
        "runs": runs,
        "times_s": [round(t, 3) for t in times],
        "avg_s": round(avg, 3),
        "min_s": round(minimum, 3),
        "max_s": round(maximum, 3),
    }


def run_benchmark(table_path: str, runs: int, limit: int) -> dict[str, Any]:
    """Run all three approaches and return the results dict."""
    print(f"\n{'=' * 70}")
    print(f"  Jerry-Can — Snapshot Read Benchmark")
    print(f"  Table : {table_path}")
    print(f"  Runs  : {runs}")
    print(f"  Limit : {limit}")
    print(f"{'=' * 70}")

    results = []
    for label, description, fn in _APPROACHES:
        result = _run_approach(label, description, fn, table_path, runs, limit)
        results.append(result)

    _print_comparison_table(results)

    return {
        "table_path": table_path,
        "runs": runs,
        "limit": limit,
        "results": results,
    }


def _print_comparison_table(results: list[dict[str, Any]]) -> None:
    """Print a comparison table with gain % vs Approach A."""
    ref_avg = results[0]["avg_s"] if results else None

    print(f"\n{'=' * 90}")
    print(
        f"  {'Approach':<6} {'Description':<36} "
        f"{'Avg(s)':>8} {'Min(s)':>8} {'Max(s)':>8} {'vs A':>8}"
    )
    print(
        f"  {'-' * 6} {'-' * 36} "
        f"{'-' * 8} {'-' * 8} {'-' * 8} {'-' * 8}"
    )

    for r in results:
        if ref_avg and ref_avg > 0 and r["avg_s"] is not None:
            gain_pct = (ref_avg - r["avg_s"]) / ref_avg * 100
            gain_str = f"{gain_pct:+.1f}%"
        else:
            gain_str = "N/A"

        print(
            f"  {r['label']:<6} {r['description']:<36} "
            f"{r['avg_s']:>8.3f} {r['min_s']:>8.3f} {r['max_s']:>8.3f} {gain_str:>8}"
        )

    print(f"{'=' * 90}\n")


# ---------------------------------------------------------------------------
# Save results
# ---------------------------------------------------------------------------

def save_results(data: dict[str, Any], name: str) -> None:
    """Save benchmark results to ``benchmark_results/<name>.json``."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{name}.json")
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"Résultats sauvegardés → {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark different Hudi snapshot read approaches.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--table-path",
        default=_DEFAULT_TABLE_PATH,
        help="Path to the Hudi table",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=_DEFAULT_RUNS,
        help="Number of repetitions per approach (results are averaged)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIMIT,
        help="Number of most-recent commits to use for bounded incremental (Approach B)",
    )
    parser.add_argument(
        "--save",
        metavar="NAME",
        help="Save results to benchmark_results/<NAME>.json",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    data = run_benchmark(args.table_path, args.runs, args.limit)
    if args.save:
        save_results(data, args.save)


if __name__ == "__main__":
    main()
