"""Lightweight end-to-end benchmark for show.py / HudiInspector.

Usage inside the container:
    python bench.py                    # full show_all() timing
    python bench.py --phases           # per-phase breakdown
    python bench.py --save <name>      # persist results to /tmp/bench_<name>.json
"""
import json
import sys
import time


def bench_full():
    """Measure end-to-end wall time of show_all()."""
    t0 = time.perf_counter()

    # Phase 1: Python import + Spark session creation
    t_import_start = time.perf_counter()
    from src.config import HUDI_TABLE_PATH, PERSISTENCE_BACKEND
    t_import_config = time.perf_counter()

    if PERSISTENCE_BACKEND == "hudi":
        from src.inspector_hudi import HudiInspector
        t_import_inspector = time.perf_counter()
        inspector = HudiInspector(HUDI_TABLE_PATH)
    else:
        from src.inspector_sqlite import SqliteInspector
        from src.config import DATABASE_PATH
        t_import_inspector = time.perf_counter()
        inspector = SqliteInspector(DATABASE_PATH)

    # Phase 2: show_all()
    t_show_start = time.perf_counter()
    inspector.show_all()
    t_show_end = time.perf_counter()

    total = t_show_end - t0
    results = {
        "total_s": round(total, 2),
        "import_config_s": round(t_import_config - t_import_start, 2),
        "import_inspector_s": round(t_import_inspector - t_import_config, 2),
        "show_all_s": round(t_show_end - t_show_start, 2),
        "backend": PERSISTENCE_BACKEND,
    }

    print(f"\n{'='*60}")
    print(f"  BENCHMARK RESULTS")
    print(f"{'='*60}")
    for k, v in results.items():
        print(f"  {k:<25} {v}")
    print(f"{'='*60}\n")

    return results


def bench_phases():
    """Measure each phase individually for the Hudi inspector."""
    from src.config import HUDI_TABLE_PATH

    phases = {}

    # Phase: Spark session init
    t0 = time.perf_counter()
    from src.hudi_writer import _get_spark
    spark = _get_spark()
    phases["spark_init_s"] = round(time.perf_counter() - t0, 2)

    # Phase: Hudi table read
    t0 = time.perf_counter()
    df = (
        spark.read.format("hudi")
        .option("hoodie.datasource.query.type", "read_optimized")
        .load(HUDI_TABLE_PATH)
    )
    phases["hudi_read_lazy_s"] = round(time.perf_counter() - t0, 2)

    # Phase: Cache + materialize
    t0 = time.perf_counter()
    df.cache()
    count = df.count()
    phases["cache_materialize_s"] = round(time.perf_counter() - t0, 2)
    phases["row_count"] = count

    # Phase: CDC read
    t0 = time.perf_counter()
    try:
        cdc_df = (
            spark.read.format("hudi")
            .option("hoodie.datasource.query.type", "incremental")
            .option("hoodie.datasource.query.incremental.format", "cdc")
            .option("hoodie.datasource.read.begin.instanttime", "000")
            .load(HUDI_TABLE_PATH)
        )
        cdc_df.cache()
        cdc_count = cdc_df.count()
        phases["cdc_read_s"] = round(time.perf_counter() - t0, 2)
        phases["cdc_row_count"] = cdc_count
        cdc_df.unpersist()
    except Exception as e:
        phases["cdc_read_s"] = round(time.perf_counter() - t0, 2)
        phases["cdc_error"] = str(e)[:100]

    # Phase: Single agg (summary)
    t0 = time.perf_counter()
    from pyspark.sql import functions as F
    df.agg(
        F.count("*"),
        F.countDistinct("station_id"),
        F.countDistinct("fetched_at"),
        F.min("fetched_at"),
        F.max("fetched_at"),
    ).collect()
    phases["agg_summary_s"] = round(time.perf_counter() - t0, 2)

    # Phase: Regions groupBy
    t0 = time.perf_counter()
    df.select("station_id", "region").distinct().groupBy("region").count().collect()
    phases["agg_regions_s"] = round(time.perf_counter() - t0, 2)

    df.unpersist()

    print(f"\n{'='*60}")
    print(f"  PHASE BREAKDOWN")
    print(f"{'='*60}")
    for k, v in phases.items():
        print(f"  {k:<25} {v}")
    print(f"{'='*60}\n")

    return phases


def main():
    save_name = None
    do_phases = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--save" and i + 1 < len(args):
            save_name = args[i + 1]
            i += 2
        elif args[i] == "--phases":
            do_phases = True
            i += 1
        else:
            i += 1

    if do_phases:
        results = bench_phases()
    else:
        results = bench_full()

    if save_name:
        path = f"/tmp/bench_{save_name}.json"
        with open(path, "w") as f:
            json.dump(results, f, indent=2)
        print(f"Saved to {path}")


if __name__ == "__main__":
    main()
