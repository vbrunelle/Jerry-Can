"""Quick phase timing for the pandas-based inspector."""
import time

t0 = time.perf_counter()
import os, json
from pathlib import Path
t_stdlib = time.perf_counter()

import pandas as pd
t_pandas = time.perf_counter()

import pyarrow.parquet as pq
t_pyarrow = time.perf_counter()

from src.config import HUDI_TABLE_PATH, HUDI_TABLE_NAME
t_config = time.perf_counter()

# Read parquet files
parquet_files = []
for root, dirs, files in os.walk(HUDI_TABLE_PATH):
    dirs[:] = [d for d in dirs if not d.startswith(".")]
    for f in files:
        if f.endswith(".parquet"):
            parquet_files.append(os.path.join(root, f))
t_walk = time.perf_counter()

dfs = [pq.read_table(fp).to_pandas() for fp in parquet_files]
t_read = time.perf_counter()

df = pd.concat(dfs, ignore_index=True)
t_concat = time.perf_counter()

# Now run the methods
from src.inspector_pandas import PandasHudiInspector
inspector = PandasHudiInspector(HUDI_TABLE_PATH)
inspector._df = df

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

print(f"\n{'='*60}")
print(f"  PANDAS INSPECTOR TIMING")
print(f"{'='*60}")
print(f"  import stdlib:     {t_stdlib - t0:>8.4f}s")
print(f"  import pandas:     {t_pandas - t_stdlib:>8.4f}s")
print(f"  import pyarrow:    {t_pyarrow - t_pandas:>8.4f}s")
print(f"  import config:     {t_config - t_pyarrow:>8.4f}s")
print(f"  walk dirs:         {t_walk - t_config:>8.4f}s")
print(f"  read parquet:      {t_read - t_walk:>8.4f}s  ({len(parquet_files)} files)")
print(f"  concat:            {t_concat - t_read:>8.4f}s  ({len(df)} rows)")
print(f"  show_schema:       {t_schema - t_concat:>8.4f}s")
print(f"  show_summary:      {t_summary - t_schema:>8.4f}s")
print(f"  show_snapshots:    {t_snapshots - t_summary:>8.4f}s")
print(f"  show_regions:      {t_regions - t_snapshots:>8.4f}s")
print(f"  show_latest:       {t_latest - t_regions:>8.4f}s")
print(f"  show_variations:   {t_variations - t_latest:>8.4f}s")
print(f"  ---")
print(f"  TOTAL:             {t_variations - t0:>8.4f}s")
print(f"{'='*60}")
