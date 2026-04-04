"""Backfill price history from existing Hudi commits.

Builds the full historicized changes from Hudi time-travel and exports
them to a CSV file.

Usage (inside the jerry-can container):
    python backfill_history.py
    python backfill_history.py /data/hudi/fuel_prices
"""

import sys
from src.config import HUDI_TABLE_PATH
from src.price_history import build_historicized_changes_pandas

if __name__ == "__main__":
    table_path = sys.argv[1] if len(sys.argv) > 1 else HUDI_TABLE_PATH
    df = build_historicized_changes_pandas(table_path)
    print(f"\nBackfill done: {len(df)} price changes reconstructed.")
