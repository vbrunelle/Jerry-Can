"""Tests for the database module."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.database import init_db, save_snapshot, connect


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    """Return a path to a freshly initialised temporary database."""
    db_path = str(tmp_path / "test_fuel.db")
    init_db(db_path)
    return db_path


def _sample_records(n: int = 3) -> list[dict]:
    return [
        {
            "station_id": f"ST{i:04d}",
            "station_name": f"Station {i}",
            "address": f"{i} Rue Principale",
            "city": "Montréal",
            "region": "Montréal",
            "latitude": 45.5 + i * 0.01,
            "longitude": -73.5 - i * 0.01,
            "fuel_type": "regular",
            "price": 175.0 + i,
            "fetched_at": "2026-04-01T12:00:00",
        }
        for i in range(1, n + 1)
    ]


class TestInitDb:
    def test_creates_tables(self, tmp_path: Path) -> None:
        db_path = str(tmp_path / "fresh.db")
        init_db(db_path)
        conn = sqlite3.connect(db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "stations" in tables
        assert "prices" in tables
        conn.close()

    def test_idempotent(self, tmp_db: str) -> None:
        """Calling init_db twice should not raise."""
        init_db(tmp_db)


class TestSaveSnapshot:
    def test_saves_records(self, tmp_db: str) -> None:
        records = _sample_records(3)
        count = save_snapshot(records, tmp_db)
        assert count == 3

    def test_returns_zero_for_empty_list(self, tmp_db: str) -> None:
        count = save_snapshot([], tmp_db)
        assert count == 0

    def test_station_upsert(self, tmp_db: str) -> None:
        """Saving the same station twice should not create duplicate rows."""
        records = _sample_records(1)
        save_snapshot(records, tmp_db)
        # Update the station name and save again
        records[0]["station_name"] = "Station Updated"
        save_snapshot(records, tmp_db)

        with connect(tmp_db) as conn:
            rows = conn.execute("SELECT * FROM stations").fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "Station Updated"

    def test_price_rows_accumulate(self, tmp_db: str) -> None:
        """Each snapshot should add new price rows."""
        records = _sample_records(2)
        save_snapshot(records, tmp_db)
        save_snapshot(records, tmp_db)

        with connect(tmp_db) as conn:
            count = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        assert count == 4

    def test_price_values_stored_correctly(self, tmp_db: str) -> None:
        records = _sample_records(1)
        records[0]["price"] = 189.9
        save_snapshot(records, tmp_db)

        with connect(tmp_db) as conn:
            row = conn.execute("SELECT price FROM prices").fetchone()
        assert row["price"] == pytest.approx(189.9)

    def test_multiple_fuel_types(self, tmp_db: str) -> None:
        records = [
            {
                "station_id": "ST001",
                "station_name": "Station 1",
                "address": "1 Rue Principale",
                "city": "Québec",
                "region": "Québec",
                "latitude": 46.8,
                "longitude": -71.2,
                "fuel_type": "regular",
                "price": 175.0,
                "fetched_at": "2026-04-01T12:00:00",
            },
            {
                "station_id": "ST001",
                "station_name": "Station 1",
                "address": "1 Rue Principale",
                "city": "Québec",
                "region": "Québec",
                "latitude": 46.8,
                "longitude": -71.2,
                "fuel_type": "diesel",
                "price": 165.0,
                "fetched_at": "2026-04-01T12:00:00",
            },
        ]
        count = save_snapshot(records, tmp_db)
        assert count == 2

        with connect(tmp_db) as conn:
            fuel_types = {
                row["fuel_type"]
                for row in conn.execute("SELECT fuel_type FROM prices").fetchall()
            }
        assert fuel_types == {"regular", "diesel"}
