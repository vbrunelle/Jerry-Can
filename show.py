"""Inspect the Jerry-Can data store.

Usage
-----
# Auto-detect backend from PERSISTENCE_BACKEND env var (default: sqlite)
python inspect.py

# Force SQLite, optionally on a specific file
PERSISTENCE_BACKEND=sqlite python inspect.py
PERSISTENCE_BACKEND=sqlite python inspect.py /path/to/fuel_prices.db

# Force Hudi (requires Java + Spark)
PERSISTENCE_BACKEND=hudi python inspect.py
PERSISTENCE_BACKEND=hudi JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 python inspect.py
PERSISTENCE_BACKEND=hudi python inspect.py /data/hudi/fuel_prices
"""

import sys

from src.config import DATABASE_PATH, HUDI_TABLE_PATH, PERSISTENCE_BACKEND

if PERSISTENCE_BACKEND == "hudi":
    from src.inspector_hudi import HudiInspector as _Inspector  # type: ignore[assignment]
    _target = sys.argv[1] if len(sys.argv) > 1 else HUDI_TABLE_PATH
else:
    from src.inspector_sqlite import SqliteInspector as _Inspector  # type: ignore[assignment]
    _target = sys.argv[1] if len(sys.argv) > 1 else DATABASE_PATH

if __name__ == "__main__":
    _Inspector(_target).show_all()
