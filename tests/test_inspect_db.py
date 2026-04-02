"""Tests for the inspect_db module."""

from pathlib import Path

import pytest

from src.database import init_db, save_snapshot, connect
from inspect_db import (
    show_schema,
    show_summary,
    show_latest_prices,
    show_regions,
    show_snapshots,
)


@pytest.fixture
def populated_db(tmp_path: Path) -> str:
    """Return a path to a temporary database with sample data."""
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    records = [
        {
            "station_id": f"ST{i:03d}",
            "station_name": f"Station {i}",
            "address": f"{i} Rue Principale",
            "city": "Montréal" if i % 2 else "Québec",
            "region": "Montréal" if i % 2 else "Capitale-Nationale",
            "latitude": 45.5 + i * 0.01,
            "longitude": -73.5 - i * 0.01,
            "fuel_type": "regular",
            "price": 170.0 + i,
            "fetched_at": "2026-04-01T12:00:00",
        }
        for i in range(1, 6)
    ]
    # Add a diesel record
    records.append({
        "station_id": "ST001",
        "station_name": "Station 1",
        "address": "1 Rue Principale",
        "city": "Montréal",
        "region": "Montréal",
        "latitude": 45.51,
        "longitude": -73.51,
        "fuel_type": "diesel",
        "price": 165.0,
        "fetched_at": "2026-04-01T12:00:00",
    })
    save_snapshot(records, db_path)
    return db_path


@pytest.fixture
def empty_db(tmp_path: Path) -> str:
    """Return a path to an empty initialised database."""
    db_path = str(tmp_path / "empty.db")
    init_db(db_path)
    return db_path


class TestShowSchema:
    def test_lists_tables_and_columns(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_schema(populated_db)
        out = capsys.readouterr().out
        assert "Schema:" in out
        assert "stations" in out
        assert "prices" in out

    def test_shows_column_details(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_schema(populated_db)
        out = capsys.readouterr().out
        # stations table columns
        assert "id" in out
        assert "name" in out
        assert "address" in out
        assert "latitude" in out
        # prices table columns
        assert "station_id" in out
        assert "fuel_type" in out
        assert "price" in out
        assert "fetched_at" in out

    def test_shows_types(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_schema(populated_db)
        out = capsys.readouterr().out
        assert "TEXT" in out
        assert "REAL" in out
        assert "INTEGER" in out

    def test_shows_primary_key(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_schema(populated_db)
        out = capsys.readouterr().out
        assert "PK" in out

    def test_shows_row_counts(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_schema(populated_db)
        out = capsys.readouterr().out
        assert "5 rows" in out   # 5 stations
        assert "6 rows" in out   # 6 price records

    def test_shows_indexes(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_schema(populated_db)
        out = capsys.readouterr().out
        assert "Indexes:" in out
        assert "idx_prices_station_fetched" in out

    def test_empty_db_still_shows_schema(self, empty_db: str, capsys: pytest.CaptureFixture) -> None:
        show_schema(empty_db)
        out = capsys.readouterr().out
        assert "stations" in out
        assert "0 rows" in out


class TestShowSummary:
    def test_displays_counts(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_summary(populated_db)
        out = capsys.readouterr().out
        assert "Stations: 5" in out
        assert "Price records: 6" in out
        assert "Snapshots: 1" in out

    def test_displays_snapshot_range(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_summary(populated_db)
        out = capsys.readouterr().out
        assert "2026-04-01T12:00:00" in out

    def test_empty_db(self, empty_db: str, capsys: pytest.CaptureFixture) -> None:
        show_summary(empty_db)
        out = capsys.readouterr().out
        assert "Stations: 0" in out
        assert "Price records: 0" in out


class TestShowSnapshots:
    def test_lists_snapshots(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_snapshots(populated_db)
        out = capsys.readouterr().out
        assert "2026-04-01T12:00:00" in out
        assert "6" in out

    def test_empty_db(self, empty_db: str, capsys: pytest.CaptureFixture) -> None:
        show_snapshots(empty_db)
        out = capsys.readouterr().out
        assert "No snapshots yet." in out


class TestShowRegions:
    def test_lists_regions(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_regions(populated_db)
        out = capsys.readouterr().out
        assert "Montréal" in out
        assert "Capitale-Nationale" in out

    def test_empty_db(self, empty_db: str, capsys: pytest.CaptureFixture) -> None:
        show_regions(empty_db)
        out = capsys.readouterr().out
        assert "No stations yet." in out


class TestShowLatestPrices:
    def test_shows_prices_sorted(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_latest_prices(populated_db)
        out = capsys.readouterr().out
        assert "Lowest prices" in out
        # Diesel at 165.0 should appear first
        lines = out.strip().split("\n")
        data_lines = [l for l in lines if "¢" in l]
        assert len(data_lines) > 0
        assert "165.0" in data_lines[0]

    def test_respects_limit(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_latest_prices(populated_db, limit=2)
        out = capsys.readouterr().out
        data_lines = [l for l in out.strip().split("\n") if "¢" in l]
        assert len(data_lines) == 2

    def test_empty_db(self, empty_db: str, capsys: pytest.CaptureFixture) -> None:
        show_latest_prices(empty_db)
        out = capsys.readouterr().out
        assert "No price data yet." in out
