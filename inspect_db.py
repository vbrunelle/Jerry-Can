"""Simple script to inspect the Jerry-Can database contents.

Delegates to the appropriate inspector based on PERSISTENCE_BACKEND.

Legacy free-function API
------------------------
The module-level helpers (``show_schema``, ``show_summary``, …) are kept for
backwards compatibility; they instantiate a ``SqliteInspector`` and call the
corresponding method.
"""

import sys

from src.config import DATABASE_PATH, HUDI_TABLE_PATH, PERSISTENCE_BACKEND
from src.inspector_sqlite import SqliteInspector


# ------------------------------------------------------------------
# Backwards-compatible free functions (always target SQLite)
# ------------------------------------------------------------------

def show_schema(db_path: str = DATABASE_PATH) -> None:
    SqliteInspector(db_path).show_schema()


def show_summary(db_path: str = DATABASE_PATH) -> None:
    SqliteInspector(db_path).show_summary()


def show_latest_prices(db_path: str = DATABASE_PATH, limit: int = 20) -> None:
    SqliteInspector(db_path).show_latest_prices(limit)


def show_regions(db_path: str = DATABASE_PATH) -> None:
    SqliteInspector(db_path).show_regions()


def show_snapshots(db_path: str = DATABASE_PATH) -> None:
    SqliteInspector(db_path).show_snapshots()


# ------------------------------------------------------------------
# CLI entry point — picks the right inspector from config
# ------------------------------------------------------------------

def main() -> None:
    if PERSISTENCE_BACKEND == "hudi":
        from src.inspector_hudi import HudiInspector

        table_path = sys.argv[1] if len(sys.argv) > 1 else HUDI_TABLE_PATH
        inspector = HudiInspector(table_path)
    else:
        db_path = sys.argv[1] if len(sys.argv) > 1 else DATABASE_PATH
        inspector = SqliteInspector(db_path)

    inspector.show_all()


if __name__ == "__main__":
    main()
