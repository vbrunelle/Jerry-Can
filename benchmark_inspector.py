"""Benchmark HudiInspector — mesure le temps mur, le nombre de lectures Hudi
et le nombre de jobs Spark déclenchés par chaque méthode de show_all().

Usage
-----
# Mesurer la baseline (avant optimisation) :
    docker exec jerry-can-jerry-can-1 python benchmark_inspector.py
    docker exec jerry-can-jerry-can-1 python benchmark_inspector.py --save baseline

# Mesurer après optimisation :
    docker exec jerry-can-jerry-can-1 python benchmark_inspector.py --save optimized

# Comparer baseline et résultats optimisés :
    docker exec jerry-can-jerry-can-1 python benchmark_inspector.py --compare baseline optimized

Les résultats sont sauvegardés dans benchmark_results/<name>.json.
"""

import argparse
import json
import os
import time
from typing import Optional

from src.config import HUDI_TABLE_PATH
from src.inspector_hudi import HudiInspector

RESULTS_DIR = "benchmark_results"


# ---------------------------------------------------------------------------
# Compteur de lectures Hudi
# ---------------------------------------------------------------------------

class _ReadCounter:
    """Intercepte chaque appel à _read_table() et comptabilise les lectures."""

    def __init__(self, inspector: HudiInspector) -> None:
        self._inspector = inspector
        self._original = inspector._read_table
        self.count = 0

    def __enter__(self):
        def _counted():
            self.count += 1
            return self._original()
        self._inspector._read_table = _counted  # type: ignore[method-assign]
        return self

    def __exit__(self, *args):
        self._inspector._read_table = self._original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# Compteur de jobs Spark
# ---------------------------------------------------------------------------

def _next_job_id() -> Optional[int]:
    """Retourne le prochain jobId Spark (entier monotone), ou None si indisponible."""
    try:
        from src.hudi_writer import _get_spark
        sc = _get_spark().sparkContext
        return sc._jsc.sc().dagScheduler().nextJobId()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Instrumentation d'une méthode
# ---------------------------------------------------------------------------

def _bench_method(inspector: HudiInspector, method_name: str) -> dict:
    """Exécute une méthode de l'inspecteur et retourne les métriques."""
    method = getattr(inspector, method_name)

    job_before = _next_job_id()
    t0 = time.perf_counter()

    # Compter les _read_table() déclenchés par cette méthode uniquement
    original_rt = inspector._read_table
    rt_count = [0]

    def _counted():
        rt_count[0] += 1
        return original_rt()

    inspector._read_table = _counted  # type: ignore[method-assign]
    try:
        method()
    finally:
        inspector._read_table = original_rt  # type: ignore[method-assign]

    elapsed = time.perf_counter() - t0
    job_after = _next_job_id()
    spark_jobs = (job_after - job_before) if (job_before is not None and job_after is not None) else None

    return {
        "method": method_name,
        "wall_time_s": round(elapsed, 3),
        "table_reads": rt_count[0],
        "spark_jobs": spark_jobs,
    }


# ---------------------------------------------------------------------------
# Benchmark complet
# ---------------------------------------------------------------------------

METHODS = [
    "show_schema",
    "show_summary",
    "show_snapshots",
    "show_regions",
    "show_latest_prices",
    "show_price_variations",
]


def run_benchmark(table_path: str = HUDI_TABLE_PATH) -> dict:
    inspector = HudiInspector(table_path)

    print(f"\n{'=' * 70}")
    print(f"  Jerry-Can — Benchmark HudiInspector")
    print(f"  Table: {table_path}")
    print(f"{'=' * 70}\n")

    results = []

    # --- méthodes individuelles ---
    for name in METHODS:
        print(f"▶ {name} ...")
        metrics = _bench_method(inspector, name)
        results.append(metrics)
        spark_str = str(metrics["spark_jobs"]) if metrics["spark_jobs"] is not None else "N/A"
        print(
            f"  ✓ {metrics['wall_time_s']:>8.3f}s  "
            f"reads={metrics['table_reads']}  "
            f"spark_jobs={spark_str}\n"
        )

    # --- show_all() entier ---
    print("▶ show_all() (end-to-end) ...")
    original_rt = inspector._read_table
    total_reads = [0]

    def _counted_all():
        total_reads[0] += 1
        return original_rt()

    inspector._read_table = _counted_all  # type: ignore[method-assign]
    job_before = _next_job_id()
    t0 = time.perf_counter()
    try:
        inspector.show_all()
    finally:
        inspector._read_table = original_rt  # type: ignore[method-assign]

    elapsed_all = time.perf_counter() - t0
    job_after = _next_job_id()
    total_jobs = (job_after - job_before) if (job_before is not None and job_after is not None) else None

    all_metrics = {
        "method": "show_all",
        "wall_time_s": round(elapsed_all, 3),
        "table_reads": total_reads[0],
        "spark_jobs": total_jobs,
    }
    results.append(all_metrics)

    spark_str = str(all_metrics["spark_jobs"]) if all_metrics["spark_jobs"] is not None else "N/A"
    print(
        f"  ✓ {all_metrics['wall_time_s']:>8.3f}s  "
        f"reads={all_metrics['table_reads']}  "
        f"spark_jobs={spark_str}\n"
    )

    # --- tableau récapitulatif ---
    _print_table(results)

    return {"table_path": table_path, "results": results}


def _print_table(results: list[dict]) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {'Method':<30} {'Wall(s)':>9}  {'Reads':>6}  {'Spark jobs':>10}")
    print(f"  {'-' * 30} {'-' * 9}  {'-' * 6}  {'-' * 10}")
    for r in results:
        spark_str = str(r["spark_jobs"]) if r["spark_jobs"] is not None else "N/A"
        marker = "  ★" if r["method"] == "show_all" else ""
        print(
            f"  {r['method']:<30} {r['wall_time_s']:>9.3f}  "
            f"{r['table_reads']:>6}  {spark_str:>10}{marker}"
        )
    print(f"{'=' * 70}\n")


# ---------------------------------------------------------------------------
# Sauvegarde / comparaison
# ---------------------------------------------------------------------------

def save_results(data: dict, name: str) -> None:
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = os.path.join(RESULTS_DIR, f"{name}.json")
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
    print(f"Résultats sauvegardés → {path}")


def compare_results(name_a: str, name_b: str) -> None:
    """Affiche un tableau avant/après avec gains en % et en valeur absolue."""
    path_a = os.path.join(RESULTS_DIR, f"{name_a}.json")
    path_b = os.path.join(RESULTS_DIR, f"{name_b}.json")

    if not os.path.exists(path_a):
        print(f"Fichier introuvable : {path_a}")
        return
    if not os.path.exists(path_b):
        print(f"Fichier introuvable : {path_b}")
        return

    with open(path_a) as fh:
        data_a = json.load(fh)
    with open(path_b) as fh:
        data_b = json.load(fh)

    map_a = {r["method"]: r for r in data_a["results"]}
    map_b = {r["method"]: r for r in data_b["results"]}

    all_methods = list(dict.fromkeys([*map_a.keys(), *map_b.keys()]))

    print(f"\n{'=' * 100}")
    print(f"  Comparaison : {name_a}  →  {name_b}")
    print(f"{'=' * 100}")
    print(
        f"  {'Method':<30} "
        f"{'Before(s)':>10} {'After(s)':>10} {'Gain(s)':>10} {'Gain%':>8}  "
        f"{'Reads △':>8}  {'Jobs △':>8}"
    )
    print(f"  {'-' * 30} {'-' * 10} {'-' * 10} {'-' * 10} {'-' * 8}  {'-' * 8}  {'-' * 8}")

    for method in all_methods:
        ra = map_a.get(method, {})
        rb = map_b.get(method, {})

        t_a = ra.get("wall_time_s")
        t_b = rb.get("wall_time_s")
        reads_a = ra.get("table_reads")
        reads_b = rb.get("table_reads")
        jobs_a = ra.get("spark_jobs")
        jobs_b = rb.get("spark_jobs")

        gain_s  = f"{t_a - t_b:+.3f}" if (t_a is not None and t_b is not None) else "N/A"
        gain_pct = (
            f"{(t_a - t_b) / t_a * 100:+.1f}%"
            if (t_a and t_b is not None)
            else "N/A"
        )
        reads_delta = (
            f"{reads_b - reads_a:+d}"
            if (reads_a is not None and reads_b is not None)
            else "N/A"
        )
        jobs_delta = (
            f"{jobs_b - jobs_a:+d}"
            if (jobs_a is not None and jobs_b is not None)
            else "N/A"
        )

        t_a_str = f"{t_a:.3f}" if t_a is not None else "N/A"
        t_b_str = f"{t_b:.3f}" if t_b is not None else "N/A"
        marker = "  ★" if method == "show_all" else ""

        print(
            f"  {method:<30} "
            f"{t_a_str:>10} {t_b_str:>10} {gain_s:>10} {gain_pct:>8}  "
            f"{reads_delta:>8}  {jobs_delta:>8}{marker}"
        )

    print(f"{'=' * 100}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark HudiInspector")
    parser.add_argument(
        "--save",
        metavar="NAME",
        help="Sauvegarder les résultats sous benchmark_results/<NAME>.json",
    )
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("BEFORE", "AFTER"),
        help="Comparer deux runs sauvegardés (ex: --compare baseline optimized)",
    )
    parser.add_argument(
        "--table-path",
        default=HUDI_TABLE_PATH,
        help=f"Chemin vers la table Hudi (défaut: {HUDI_TABLE_PATH})",
    )
    args = parser.parse_args()

    if args.compare:
        compare_results(args.compare[0], args.compare[1])
        return

    data = run_benchmark(args.table_path)

    if args.save:
        save_results(data, args.save)


if __name__ == "__main__":
    main()
