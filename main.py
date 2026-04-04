"""Main entry point for the Jerry-Can fuel price collector."""

import logging
import sys
import time

import pytest
import schedule

from src.config import FETCH_INTERVAL_MINUTES, PERSISTENCE_BACKEND

if PERSISTENCE_BACKEND == "hudi":
    from src.hudi_writer import init_hudi as init_db
    from src.hudi_writer import save_snapshot
    from src.hudi_writer import stop as _stop_backend
    import src.database as _sqlite_db  # SQLite mirror for the management server
else:
    from src.database import init_db, save_snapshot
    _sqlite_db = None  # not needed in sqlite-only mode

    def _stop_backend() -> None:  # noqa: E303
        """No-op for the SQLite backend."""

from src.datasource import GeoJsonDataSource

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

logger = logging.getLogger(__name__)

source = GeoJsonDataSource()


def run_once() -> None:
    """Fetch current fuel prices and persist them to the database."""
    try:
        records = source.fetch()
        if _sqlite_db is not None:
            _sqlite_db.save_snapshot(records)  # fast write first — unblocks the management server UI
        count = save_snapshot(records)
        logger.info("Snapshot complete: %d records stored.", count)
    except Exception as exc:  # noqa: BLE001
        logger.error("Snapshot failed: %s", exc)


def run_tests() -> None:
    """Run unit tests before starting the collector. Aborts on failure."""
    logger.info("Running unit tests before startup...")
    result = pytest.main(["-m", "not integration", "-q", "--tb=short"])
    if result != pytest.ExitCode.OK:
        logger.error("Unit tests failed — aborting startup.")
        sys.exit(1)
    logger.info("All unit tests passed.")


def main() -> None:
    """Initialise the database and start the periodic scheduler."""
    run_tests()
    logger.info(
        "Jerry-Can starting up (persistence backend: %s).",
        PERSISTENCE_BACKEND,
    )
    init_db()
    if _sqlite_db is not None:
        _sqlite_db.init_db()  # initialise the SQLite mirror for the management server

    # Run immediately on startup, then every FETCH_INTERVAL_MINUTES minutes
    run_once()
    schedule.every(FETCH_INTERVAL_MINUTES).minutes.do(run_once)

    logger.info(
        "Scheduler running — fetching every %d minute(s). Press Ctrl+C to stop.",
        FETCH_INTERVAL_MINUTES,
    )
    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    except KeyboardInterrupt:
        _stop_backend()
        logger.info("Jerry-Can stopped.")


if __name__ == "__main__":
    main()
