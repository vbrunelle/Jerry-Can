"""Tests for new price_history dashboard helper functions."""

import tempfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.price_history import (
    get_current_prices,
    get_inspection_data,
    get_snapshot_dates,
    get_snapshots_data,
    get_snapshots_for_date,
)


# ---------------------------------------------------------------------------
# Fixture: minimal Hudi-like Parquet table on disk
# ---------------------------------------------------------------------------


def _write_test_parquet(table_path: str) -> None:
    """Write a minimal set of Parquet files that mimic a Hudi table."""
    data = {
        "_hoodie_commit_time": [
            "20240101100000000",
            "20240101100000000",
            "20240102100000000",
            "20240102100000000",
        ],
        "station_id": ["ST1", "ST2", "ST1", "ST2"],
        "fuel_type": ["regular", "regular", "regular", "regular"],
        "station_name": ["Station A", "Station B", "Station A", "Station B"],
        "city": ["Montréal", "Québec", "Montréal", "Québec"],
        "region": ["Montréal", "Québec", "Montréal", "Québec"],
        "price": [155.0, 160.0, 158.0, 157.0],
        "fetched_at": [
            "2024-01-01T10:00:00",
            "2024-01-01T10:00:00",
            "2024-01-02T10:00:00",
            "2024-01-02T10:00:00",
        ],
    }
    table = pa.table(data)
    path = Path(table_path)
    path.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, str(path / "part-0.parquet"))


@pytest.fixture()
def hudi_table(tmp_path: Path) -> str:
    table_path = str(tmp_path / "fuel_prices")
    _write_test_parquet(table_path)
    return table_path


@pytest.fixture()
def empty_table(tmp_path: Path) -> str:
    table_path = str(tmp_path / "empty_table")
    Path(table_path).mkdir(parents=True)
    return table_path


# ---------------------------------------------------------------------------
# Tests: get_snapshot_dates
# ---------------------------------------------------------------------------


class TestGetSnapshotDates:
    def test_returns_dates_descending(self, hudi_table: str) -> None:
        dates = get_snapshot_dates(hudi_table)
        assert dates == ["2024-01-02", "2024-01-01"]

    def test_returns_empty_for_empty_table(self, empty_table: str) -> None:
        dates = get_snapshot_dates(empty_table)
        assert dates == []


# ---------------------------------------------------------------------------
# Tests: get_snapshots_for_date
# ---------------------------------------------------------------------------


class TestGetSnapshotsForDate:
    def test_returns_snapshots_for_date(self, hudi_table: str) -> None:
        snaps = get_snapshots_for_date(hudi_table, "2024-01-01")
        assert len(snaps) == 1
        assert snaps[0]["record_count"] == 2
        assert "2024-01-01" in snaps[0]["fetched_at"]

    def test_returns_empty_for_missing_date(self, hudi_table: str) -> None:
        snaps = get_snapshots_for_date(hudi_table, "2099-01-01")
        assert snaps == []

    def test_returns_empty_for_empty_table(self, empty_table: str) -> None:
        snaps = get_snapshots_for_date(empty_table, "2024-01-01")
        assert snaps == []


# ---------------------------------------------------------------------------
# Tests: get_current_prices
# ---------------------------------------------------------------------------


class TestGetCurrentPrices:
    def test_returns_latest_prices(self, hudi_table: str) -> None:
        prices, ts = get_current_prices(hudi_table)
        assert len(prices) == 2
        assert ts is not None
        assert "2024-01-02" in str(ts)
        names = [p["station_name"] for p in prices]
        assert "Station A" in names
        assert "Station B" in names

    def test_returns_empty_for_empty_table(self, empty_table: str) -> None:
        prices, ts = get_current_prices(empty_table)
        assert prices == []
        assert ts is None


# ---------------------------------------------------------------------------
# Tests: get_inspection_data
# ---------------------------------------------------------------------------


class TestGetInspectionData:
    def test_returns_proper_structure(self, hudi_table: str) -> None:
        data = get_inspection_data(hudi_table)
        assert data["summary"]["station_count"] == 2
        assert data["summary"]["price_count"] == 4
        assert data["summary"]["snapshot_count"] == 2
        assert data["summary"]["first_snapshot"] is not None
        assert data["summary"]["last_snapshot"] is not None
        assert len(data["regions"]) > 0
        assert len(data["latest_prices"]) > 0
        assert "increases" in data["price_variations"]
        assert "decreases" in data["price_variations"]

    def test_returns_empty_for_empty_table(self, empty_table: str) -> None:
        data = get_inspection_data(empty_table)
        assert data["summary"]["station_count"] == 0
        assert data["summary"]["price_count"] == 0
        assert data["regions"] == []
        assert data["latest_prices"] == []
        assert data["snapshots"] == []


# ---------------------------------------------------------------------------
# Tests: get_snapshots_data (existing function — regression)
# ---------------------------------------------------------------------------


class TestGetSnapshotsData:
    def test_returns_snapshots(self, hudi_table: str) -> None:
        snaps = get_snapshots_data(hudi_table)
        assert len(snaps) > 0
        for s in snaps:
            assert "fetched_at" in s
            assert "record_count" in s
            assert "changes" in s

    def test_returns_empty_for_empty_table(self, empty_table: str) -> None:
        snaps = get_snapshots_data(empty_table)
        assert snaps == []
