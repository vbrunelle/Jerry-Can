"""Tests for price_history dashboard helper functions."""

import os
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from src.price_history import (
    get_snapshot_dates,
    get_snapshots_data,
)


# ---------------------------------------------------------------------------
# Fixture: minimal Hudi-like table with .hoodie/ timeline on disk
# ---------------------------------------------------------------------------


def _write_test_hoodie_timeline(table_path: str) -> None:
    """Write minimal ``.hoodie/`` commit files that mimic a Hudi timeline."""
    hoodie_dir = Path(table_path) / ".hoodie"
    hoodie_dir.mkdir(parents=True, exist_ok=True)
    # Two distinct commit instants on two different dates
    (hoodie_dir / "20240101100000000.commit").touch()
    (hoodie_dir / "20240102100000000.commit").touch()


@pytest.fixture()
def hudi_table(tmp_path: Path) -> str:
    table_path = str(tmp_path / "fuel_prices")
    _write_test_hoodie_timeline(table_path)
    return table_path


@pytest.fixture()
def empty_table(tmp_path: Path) -> str:
    table_path = str(tmp_path / "empty_table")
    Path(table_path).mkdir(parents=True)
    return table_path


# ---------------------------------------------------------------------------
# Tests: get_snapshot_dates  (filesystem-only, no Spark)
# ---------------------------------------------------------------------------


class TestGetSnapshotDates:
    def test_returns_dates_descending(self, hudi_table: str) -> None:
        dates = get_snapshot_dates(hudi_table)
        assert dates == ["2024-01-02", "2024-01-01"]

    def test_returns_empty_for_empty_table(self, empty_table: str) -> None:
        dates = get_snapshot_dates(empty_table)
        assert dates == []

    def test_returns_empty_for_nonexistent_path(self, tmp_path: Path) -> None:
        dates = get_snapshot_dates(str(tmp_path / "nonexistent"))
        assert dates == []

    def test_ignores_non_commit_files(self, tmp_path: Path) -> None:
        table_path = str(tmp_path / "table")
        hoodie_dir = Path(table_path) / ".hoodie"
        hoodie_dir.mkdir(parents=True)
        (hoodie_dir / "20240101100000000.commit").touch()
        (hoodie_dir / "somefile.json").touch()
        (hoodie_dir / "20240102100000000.deltacommit").touch()
        dates = get_snapshot_dates(table_path)
        assert dates == ["2024-01-02", "2024-01-01"]

    def test_deduplicates_same_date(self, tmp_path: Path) -> None:
        table_path = str(tmp_path / "table")
        hoodie_dir = Path(table_path) / ".hoodie"
        hoodie_dir.mkdir(parents=True)
        (hoodie_dir / "20240101100000000.commit").touch()
        (hoodie_dir / "20240101120000000.commit").touch()
        dates = get_snapshot_dates(table_path)
        assert dates == ["2024-01-01"]


# ---------------------------------------------------------------------------
# Integration tests: Spark-dependent functions
# (skipped in unit test runs — require a real Hudi+Spark setup)
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGetSnapshotsForDate:
    def test_returns_snapshots_for_date(self, hudi_table: str) -> None:
        from src.price_history import get_snapshots_for_date
        snaps = get_snapshots_for_date(hudi_table, "2024-01-01")
        assert len(snaps) == 1
        assert snaps[0]["record_count"] == 2
        assert "2024-01-01" in snaps[0]["fetched_at"]

    def test_returns_empty_for_missing_date(self, hudi_table: str) -> None:
        from src.price_history import get_snapshots_for_date
        snaps = get_snapshots_for_date(hudi_table, "2099-01-01")
        assert snaps == []

    def test_returns_empty_for_empty_table(self, empty_table: str) -> None:
        from src.price_history import get_snapshots_for_date
        snaps = get_snapshots_for_date(empty_table, "2024-01-01")
        assert snaps == []


@pytest.mark.integration
class TestGetCurrentPrices:
    def test_returns_latest_prices(self, hudi_table: str) -> None:
        from src.price_history import get_current_prices
        prices, ts = get_current_prices(hudi_table)
        assert len(prices) == 2
        assert ts is not None
        assert "2024-01-02" in str(ts)
        names = [p["station_name"] for p in prices]
        assert "Station A" in names
        assert "Station B" in names

    def test_returns_empty_for_empty_table(self, empty_table: str) -> None:
        from src.price_history import get_current_prices
        prices, ts = get_current_prices(empty_table)
        assert prices == []
        assert ts is None


@pytest.mark.integration
class TestGetInspectionData:
    def test_returns_proper_structure(self, hudi_table: str) -> None:
        from src.price_history import get_inspection_data
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
        from src.price_history import get_inspection_data
        data = get_inspection_data(empty_table)
        assert data["summary"]["station_count"] == 0
        assert data["summary"]["price_count"] == 0
        assert data["regions"] == []
        assert data["latest_prices"] == []
        assert data["snapshots"] == []


@pytest.mark.integration
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


@pytest.mark.integration
class TestBuildHistoricizedChangesSpark:
    def test_returns_dataframe(self, hudi_table: str) -> None:
        from src.price_history import build_historicized_changes_spark
        df = build_historicized_changes_spark(hudi_table)
        assert isinstance(df, pd.DataFrame)

    def test_pandas_alias_calls_spark(self, hudi_table: str) -> None:
        from src.price_history import build_historicized_changes_pandas
        df = build_historicized_changes_pandas(hudi_table)
        assert isinstance(df, pd.DataFrame)
