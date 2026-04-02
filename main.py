"""Main entry point for the Jerry-Can fuel price collector."""

import logging
import time

import schedule

from src.config import FETCH_INTERVAL_MINUTES
from src.database import init_db, save_snapshot
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
        count = save_snapshot(records)
        logger.info("Snapshot complete: %d records stored.", count)
    except Exception as exc:  # noqa: BLE001
        logger.error("Snapshot failed: %s", exc)


def main() -> None:
    """Initialise the database and start the periodic scheduler."""
    logger.info("Jerry-Can starting up.")
    init_db()

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
        logger.info("Jerry-Can stopped.")


if __name__ == "__main__":
    main()
