"""Tests for the inspect_db module and inspector classes."""

from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.database import init_db, save_snapshot, connect
from src.inspector import Inspector
from src.inspector_sqlite import SqliteInspector
from inspect_db import (
    show_schema,
    show_summary,
    show_latest_prices,
    show_regions,
    show_snapshots,
    show_price_variations,
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


@pytest.fixture
def two_snapshot_db(tmp_path: Path) -> str:
    """Return a database with two snapshots so price variations exist."""
    db_path = str(tmp_path / "two_snapshots.db")
    init_db(db_path)
    base = [
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
        for i in range(1, 4)
    ]
    save_snapshot(base, db_path)
    # Second snapshot — ST001 goes up 5¢, ST002 drops 3¢, ST003 unchanged
    snapshot2 = [
        {**base[0], "price": 176.0, "fetched_at": "2026-04-02T12:00:00"},
        {**base[1], "price": 169.0, "fetched_at": "2026-04-02T12:00:00"},
        {**base[2], "price": 173.0, "fetched_at": "2026-04-02T12:00:00"},
    ]
    save_snapshot(snapshot2, db_path)
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


class TestShowPriceVariations:
    def test_shows_rises_and_drops(self, two_snapshot_db: str, capsys: pytest.CaptureFixture) -> None:
        show_price_variations(two_snapshot_db)
        out = capsys.readouterr().out
        assert "Hausses" in out
        assert "Baisses" in out

    def test_rise_appears_in_hausses(self, two_snapshot_db: str, capsys: pytest.CaptureFixture) -> None:
        show_price_variations(two_snapshot_db)
        out = capsys.readouterr().out
        # ST001: 171 -> 176, delta = +5.0
        hausses_section = out.split("Baisses")[0]
        assert "+5.0" in hausses_section

    def test_drop_appears_in_baisses(self, two_snapshot_db: str, capsys: pytest.CaptureFixture) -> None:
        show_price_variations(two_snapshot_db)
        out = capsys.readouterr().out
        # ST002: 172 -> 169, delta = -3.0
        baisses_section = out.split("Baisses")[1]
        assert "-3.0" in baisses_section

    def test_respects_limit(self, two_snapshot_db: str, capsys: pytest.CaptureFixture) -> None:
        show_price_variations(two_snapshot_db, limit=1)
        out = capsys.readouterr().out
        data_lines = [l for l in out.strip().split("\n") if "¢" in l]
        # 1 hausse + 1 baisse = 2 data lines total
        assert len(data_lines) == 2

    def test_single_snapshot_shows_no_variations(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        show_price_variations(populated_db)
        out = capsys.readouterr().out
        assert "No price variations yet" in out

    def test_empty_db(self, empty_db: str, capsys: pytest.CaptureFixture) -> None:
        show_price_variations(empty_db)
        out = capsys.readouterr().out
        assert "No price variations yet" in out

    def test_summary_shows_changed_count(
        self, two_snapshot_db: str, capsys: pytest.CaptureFixture
    ) -> None:
        show_price_variations(two_snapshot_db)
        out = capsys.readouterr().out
        # ST001 (+5¢) and ST002 (-3¢) changed; ST003 stayed the same
        assert "2 station" in out

    def test_no_change_message_when_all_prices_stable(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """Two snapshots with identical prices should report no changes detected."""
        db_path = str(tmp_path / "stable.db")
        init_db(db_path)
        records = [
            {
                "station_id": f"ST{i:03d}",
                "station_name": f"Station {i}",
                "address": f"{i} Rue Principale",
                "city": "Montréal",
                "region": "Montréal",
                "latitude": 45.5 + i * 0.01,
                "longitude": -73.5 - i * 0.01,
                "fuel_type": "regular",
                "price": 170.0 + i,
                "fetched_at": "2026-04-01T12:00:00",
            }
            for i in range(1, 4)
        ]
        save_snapshot(records, db_path)
        snapshot2 = [{**r, "fetched_at": "2026-04-02T12:00:00"} for r in records]
        save_snapshot(snapshot2, db_path)

        show_price_variations(db_path)
        out = capsys.readouterr().out
        assert "No price changes detected" in out


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


# ---------------------------------------------------------------------------
# Abstract Inspector tests
# ---------------------------------------------------------------------------


class TestInspectorIsAbstract:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            Inspector()

    def test_subclass_must_implement_all_methods(self) -> None:
        class Incomplete(Inspector):
            pass

        with pytest.raises(TypeError):
            Incomplete()


# ---------------------------------------------------------------------------
# SqliteInspector class-based tests
# ---------------------------------------------------------------------------


class TestSqliteInspector:
    def test_show_all_runs_every_report(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        inspector = SqliteInspector(populated_db)
        inspector.show_all()
        out = capsys.readouterr().out
        assert "Schema:" in out
        assert "Stations: 5" in out
        assert "Lowest prices" in out
        assert "Montréal" in out

    def test_show_schema(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        SqliteInspector(populated_db).show_schema()
        out = capsys.readouterr().out
        assert "stations" in out
        assert "prices" in out

    def test_show_summary(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        SqliteInspector(populated_db).show_summary()
        out = capsys.readouterr().out
        assert "Stations: 5" in out
        assert "Price records: 6" in out

    def test_show_snapshots(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        SqliteInspector(populated_db).show_snapshots()
        out = capsys.readouterr().out
        assert "2026-04-01T12:00:00" in out

    def test_show_regions(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        SqliteInspector(populated_db).show_regions()
        out = capsys.readouterr().out
        assert "Montréal" in out

    def test_show_latest_prices(self, populated_db: str, capsys: pytest.CaptureFixture) -> None:
        SqliteInspector(populated_db).show_latest_prices()
        out = capsys.readouterr().out
        assert "165.0" in out

    def test_empty_db(self, empty_db: str, capsys: pytest.CaptureFixture) -> None:
        inspector = SqliteInspector(empty_db)
        inspector.show_all()
        out = capsys.readouterr().out
        assert "0 rows" in out
        assert "Stations: 0" in out
        assert "No price data yet." in out

    def test_raises_file_not_found_on_nonexistent_db(self, tmp_path: Path) -> None:
        """inspect must NOT silently create a new file — it should raise."""
        nonexistent = str(tmp_path / "does_not_exist.db")
        inspector = SqliteInspector(nonexistent)
        with pytest.raises(FileNotFoundError, match="Database not found"):
            inspector.show_schema()

    def test_does_not_create_file_when_db_missing(self, tmp_path: Path) -> None:
        """Inspecting a missing DB must not create the file on disk."""
        import os
        nonexistent = str(tmp_path / "should_not_be_created.db")
        inspector = SqliteInspector(nonexistent)
        with pytest.raises(FileNotFoundError):
            inspector.show_all()
        assert not os.path.exists(nonexistent), "inspect_db must not create the DB file"


# ---------------------------------------------------------------------------
# HudiInspector tests (mocked Spark)
# ---------------------------------------------------------------------------


def _mock_spark_df(rows, schema_fields=None):
    """Build a mock Spark DataFrame with .count(), .collect(), .select(), etc."""
    df = MagicMock()
    df.count.return_value = len(rows)
    df.collect.return_value = rows

    if schema_fields is None:
        schema_fields = []
    schema = MagicMock()
    schema.fields = schema_fields
    df.schema = schema

    # select().distinct().count()
    select_mock = MagicMock()
    select_mock.distinct.return_value = select_mock
    select_mock.count.return_value = len({r.get("station_id", "") for r in rows}) if rows else 0
    # select().distinct().groupBy().agg().orderBy().collect()
    select_mock.groupBy.return_value = select_mock
    select_mock.agg.return_value = select_mock
    select_mock.orderBy.return_value = select_mock
    select_mock.collect.return_value = rows
    df.select.return_value = select_mock

    # agg().collect()
    agg_mock = MagicMock()
    df.agg.return_value = agg_mock

    # filter().orderBy().limit().collect()
    filter_mock = MagicMock()
    filter_mock.orderBy.return_value = filter_mock
    filter_mock.limit.return_value = filter_mock
    filter_mock.collect.return_value = rows
    df.filter.return_value = filter_mock

    # groupBy().agg().orderBy().limit().collect()
    group_mock = MagicMock()
    group_mock.agg.return_value = group_mock
    group_mock.orderBy.return_value = group_mock
    group_mock.limit.return_value = group_mock
    group_mock.collect.return_value = rows
    df.groupBy.return_value = group_mock

    return df


class TestHudiInspector:
    def test_show_schema_prints_fields(self, capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        field1 = MagicMock()
        field1.name = "station_id"
        field1.dataType = "StringType"
        field2 = MagicMock()
        field2.name = "price"
        field2.dataType = "DoubleType"

        mock_df = _mock_spark_df([], schema_fields=[field1, field2])

        inspector = HudiInspector(table_path="/tmp/fake_hudi")
        with patch.object(inspector, "_read_table", return_value=mock_df):
            inspector.show_schema()

        out = capsys.readouterr().out
        assert "Schema:" in out
        assert "station_id" in out
        assert "price" in out

    def test_show_schema_no_table(self, capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        inspector = HudiInspector(table_path="/tmp/fake_hudi")
        with patch.object(inspector, "_read_table", side_effect=Exception("table not found")):
            inspector.show_schema()

        out = capsys.readouterr().out
        assert "No Hudi table found." in out

    @patch("src.inspector_hudi.F", new_callable=MagicMock)
    def test_show_summary(self, mock_F: MagicMock, capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        rows = [
            {"station_id": "ST001", "fuel_type": "regular", "price": 175.0, "fetched_at": "2026-04-01T12:00:00"},
            {"station_id": "ST002", "fuel_type": "diesel", "price": 165.0, "fetched_at": "2026-04-01T12:00:00"},
        ]
        mock_df = _mock_spark_df(rows)
        agg_row = MagicMock()
        agg_row.__getitem__ = lambda self, k: "2026-04-01T12:00:00"
        mock_df.agg.return_value.collect.return_value = [agg_row]

        inspector = HudiInspector(table_path="/tmp/fake_hudi")
        with patch.object(inspector, "_read_table", return_value=mock_df):
            inspector.show_summary()

        out = capsys.readouterr().out
        assert "Price records: 2" in out

    @patch("src.inspector_hudi.F", new_callable=MagicMock)
    def test_show_latest_prices(self, mock_F: MagicMock, capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        rows = [
            {"station_name": "Station A", "city": "Montréal", "fuel_type": "regular", "price": 175.0, "fetched_at": "2026-04-01T12:00:00"},
        ]
        mock_df = _mock_spark_df(rows)
        mock_df.agg.return_value.collect.return_value = [MagicMock(__getitem__=lambda s, i: "2026-04-01T12:00:00")]

        inspector = HudiInspector(table_path="/tmp/fake_hudi")
        with patch.object(inspector, "_read_table", return_value=mock_df):
            inspector.show_latest_prices()

        out = capsys.readouterr().out
        assert "Lowest prices" in out
        assert "175.0" in out

    def test_show_latest_prices_empty(self, capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        mock_df = _mock_spark_df([])

        inspector = HudiInspector(table_path="/tmp/fake_hudi")
        with patch.object(inspector, "_read_table", return_value=mock_df):
            inspector.show_latest_prices()

        out = capsys.readouterr().out
        assert "No price data yet." in out

    @patch("src.inspector_hudi.F", new_callable=MagicMock)
    def test_show_regions(self, mock_F: MagicMock, capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        region_rows = [
            {"region": "Montréal", "cnt": 10, "fuel_type": "regular", "avg_price": 174.5},
            {"region": "Québec",   "cnt": 5,  "fuel_type": "regular", "avg_price": 175.2},
        ]
        mock_df = _mock_spark_df(region_rows)

        inspector = HudiInspector(table_path="/tmp/fake_hudi")
        with patch.object(inspector, "_read_table", return_value=mock_df):
            inspector.show_regions()

        out = capsys.readouterr().out
        assert "Montréal" in out

    def test_show_regions_no_table(self, capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        inspector = HudiInspector(table_path="/tmp/fake_hudi")
        with patch.object(inspector, "_read_table", side_effect=Exception("nope")):
            inspector.show_regions()

        out = capsys.readouterr().out
        assert "No stations yet." in out

    def test_show_snapshots(self, tmp_path: "Path", capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        hoodie_dir = tmp_path / ".hoodie"
        hoodie_dir.mkdir()
        commit_data = {
            "operationType": "UPSERT",
            "partitionToWriteStats": {
                "Montréal": [{"numInserts": 100, "numUpdateWrites": 50}]
            },
            "extraMetadata": {},
        }
        (hoodie_dir / "20260401120000000.commit").write_text(
            __import__("json").dumps(commit_data)
        )

        inspector = HudiInspector(table_path=str(tmp_path))
        inspector.show_snapshots()

        out = capsys.readouterr().out
        assert "20260401120000000" in out
        assert "100" in out
        assert "50" in out

    def test_show_snapshots_no_table(self, capsys: pytest.CaptureFixture) -> None:
        from src.inspector_hudi import HudiInspector

        inspector = HudiInspector(table_path="/tmp/nonexistent_hudi_table_xyz")
        inspector.show_snapshots()

        out = capsys.readouterr().out
        assert "No commits yet." in out

    def test_file_not_found_shows_helpful_message(self, capsys: pytest.CaptureFixture) -> None:
        """FileNotFoundError from Spark must print a clear 'run main.py' message,
        not a generic 'No Hudi table found' that looks like normal state."""
        from src.inspector_hudi import HudiInspector

        inspector = HudiInspector(table_path="/nonexistent/path")
        with patch.object(
            inspector,
            "_read_table",
            side_effect=Exception("FileNotFoundException: File /nonexistent/path does not exist"),
        ):
            inspector.show_schema()

        out = capsys.readouterr().out
        assert "main.py" in out or "does not exist" in out.lower() or "Run" in out

    def test_spark_error_is_not_swallowed(self, capsys: pytest.CaptureFixture) -> None:
        """A real Spark crash (not a missing file) must surface the error message."""
        from src.inspector_hudi import HudiInspector

        inspector = HudiInspector(table_path="/tmp/fake_hudi")
        with patch.object(
            inspector,
            "_read_table",
            side_effect=RuntimeError("OutOfMemoryError: Java heap space"),
        ):
            inspector.show_schema()

        out = capsys.readouterr().out
        assert "OutOfMemoryError" in out or "Java heap space" in out
