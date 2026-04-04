"""Quick phase-by-phase timing of show.py / HudiInspector.

Usage:
    docker exec jerry-can-jerry-can-1 python bench_quick.py
"""

import time

t_start = time.perf_counter()

# Phase 1: Python imports + Spark session init
from src.config import HUDI_TABLE_PATH
from src.hudi_writer import _get_spark

t_imports = time.perf_counter()
spark = _get_spark()
t_spark = time.perf_counter()

# Phase 2: Hudi read (read_optimized)
df = (
    spark.read.format("hudi")
    .option("hoodie.datasource.query.type", "read_optimized")
    .load(HUDI_TABLE_PATH)
)
t_hudi_lazy = time.perf_counter()

# Phase 3: Materialize (cache + count)
df.cache()
count = df.count()
t_cache = time.perf_counter()

# Phase 4: Direct Parquet read (bypass Hudi)
try:
    df2 = spark.read.option("recursiveFileLookup", "true").parquet(HUDI_TABLE_PATH)
    t_parquet_lazy = time.perf_counter()
    count2 = df2.count()
    t_parquet_mat = time.perf_counter()
except Exception as e:
    t_parquet_lazy = time.perf_counter()
    t_parquet_mat = t_parquet_lazy
    count2 = f"ERROR: {e}"

# Phase 5: show_all with cached DF
from src.inspector_hudi import HudiInspector
inspector = HudiInspector(HUDI_TABLE_PATH)
inspector._df = df  # inject pre-cached DF
inspector._total_rows = count
t_setup_inspector = time.perf_counter()

inspector.show_schema()
t_schema = time.perf_counter()
inspector.show_summary()
t_summary = time.perf_counter()
inspector.show_snapshots()
t_snapshots = time.perf_counter()
inspector.show_regions()
t_regions = time.perf_counter()
inspector.show_latest_prices()
t_latest = time.perf_counter()
inspector.show_price_variations()
t_variations = time.perf_counter()

# Cleanup
df.unpersist()

t_end = time.perf_counter()

print(f"\n{'='*60}")
print(f"  TIMING BREAKDOWN")
print(f"{'='*60}")
print(f"  Imports:                {t_imports - t_start:>8.3f}s")
print(f"  Spark session init:     {t_spark - t_imports:>8.3f}s")
print(f"  Hudi lazy load:         {t_hudi_lazy - t_spark:>8.3f}s")
print(f"  Cache + count ({count}):  {t_cache - t_hudi_lazy:>8.3f}s")
print(f"  Direct parquet (lazy):  {t_parquet_lazy - t_cache:>8.3f}s")
print(f"  Direct parquet (count): {t_parquet_mat - t_parquet_lazy:>8.3f}s (got {count2})")
print(f"  ---")
print(f"  show_schema:            {t_schema - t_setup_inspector:>8.3f}s")
print(f"  show_summary:           {t_summary - t_schema:>8.3f}s")
print(f"  show_snapshots:         {t_snapshots - t_summary:>8.3f}s")
print(f"  show_regions:           {t_regions - t_snapshots:>8.3f}s")
print(f"  show_latest_prices:     {t_latest - t_regions:>8.3f}s")
print(f"  show_price_variations:  {t_variations - t_latest:>8.3f}s")
print(f"  ---")
print(f"  TOTAL:                  {t_end - t_start:>8.3f}s")
print(f"{'='*60}")
