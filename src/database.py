"""Database module for storing Quebec fuel price data."""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Generator

from src.config import DATABASE_PATH

logger = logging.getLogger(__name__)

CREATE_STATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS stations (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT,
    city        TEXT,
    region      TEXT,
    latitude    REAL,
    longitude   REAL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

CREATE_PRICES_TABLE = """
CREATE TABLE IF NOT EXISTS prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    station_id  TEXT NOT NULL,
    fuel_type   TEXT NOT NULL,
    price       REAL NOT NULL,
    fetched_at  TEXT NOT NULL,
    FOREIGN KEY (station_id) REFERENCES stations(id)
)
"""

CREATE_PRICES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_prices_station_fetched
ON prices (station_id, fetched_at)
"""


def init_db(db_path: str = DATABASE_PATH) -> None:
    """Initialise the SQLite database and create tables if they don't exist."""
    with connect(db_path) as conn:
        conn.execute(CREATE_STATIONS_TABLE)
        conn.execute(CREATE_PRICES_TABLE)
        conn.execute(CREATE_PRICES_INDEX)
        conn.commit()
    logger.info("Database initialised at %s", db_path)


@contextmanager
def connect(db_path: str = DATABASE_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that provides a SQLite connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def upsert_station(conn: sqlite3.Connection, station: dict) -> None:
    """Insert or update a station record."""
    conn.execute(
        """
        INSERT INTO stations (id, name, address, city, region, latitude, longitude)
        VALUES (:id, :name, :address, :city, :region, :latitude, :longitude)
        ON CONFLICT(id) DO UPDATE SET
            name      = excluded.name,
            address   = excluded.address,
            city      = excluded.city,
            region    = excluded.region,
            latitude  = excluded.latitude,
            longitude = excluded.longitude
        """,
        station,
    )


def insert_price(conn: sqlite3.Connection, price: dict) -> None:
    """Insert a price record."""
    conn.execute(
        """
        INSERT INTO prices (station_id, fuel_type, price, fetched_at)
        VALUES (:station_id, :fuel_type, :price, :fetched_at)
        """,
        price,
    )


def save_snapshot(records: list[dict], db_path: str = DATABASE_PATH) -> int:
    """
    Persist a list of price records to the database.

    Each record is expected to have the following keys:
        station_id, station_name, address, city, region,
        latitude, longitude, fuel_type, price, fetched_at

    Returns the number of price rows inserted.
    """
    if not records:
        logger.warning("No records to save.")
        return 0

    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    count = 0

    with connect(db_path) as conn:
        for rec in records:
            station = {
                "id": rec["station_id"],
                "name": rec.get("station_name", ""),
                "address": rec.get("address", ""),
                "city": rec.get("city", ""),
                "region": rec.get("region", ""),
                "latitude": rec.get("latitude"),
                "longitude": rec.get("longitude"),
            }
            upsert_station(conn, station)

            price = {
                "station_id": rec["station_id"],
                "fuel_type": rec.get("fuel_type", "regular"),
                "price": rec["price"],
                "fetched_at": rec.get("fetched_at", fetched_at),
            }
            insert_price(conn, price)
            count += 1

        conn.commit()

    logger.info("Saved %d price records.", count)
    return count
