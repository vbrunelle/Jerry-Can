"""Tests for src/reader.py — PriceReader ABC and concrete implementations.

Coverage strategy
-----------------
* Factory (``get_reader``) — pure Python, no Spark.
* ABC compliance — abstract-class invariants, no Spark.
* ``SqliteReader`` — real SQLite DB + real Spark session.
  Marked ``integration`` because PySpark must be active.
* ``HudiReader`` — real Hudi table written to a temp directory.
  Marked ``integration``.  Requires the Hudi Spark bundle (Docker env).
* Backward-compat helpers — real end-to-end call on a SQLite fixture.
  Marked ``integration``.
"""

from abc import ABC
from pathlib import Path

import pandas as pd
import pytest

from src.database import init_db, save_snapshot
from src.reader import (
    HudiReader,
    PriceReader,
    SqliteReader,
    get_reader,
    read_price_changes,
    read_price_history,
)


# ---------------------------------------------------------------------------
# Shared data helpers
# ---------------------------------------------------------------------------

def _station(sid: str, price: float, ts: str) -> dict:
    return {
        "station_id": sid,
        "station_name": f"Station {sid}",
        "address": "1 Rue Test",
        "city": "Montréal",
        "region": "Montréal",
        "latitude": 45.50,
        "longitude": -73.50,
        "fuel_type": "regular",
        "price": price,
        "fetched_at": ts,
    }


T1 = "2026-04-01T12:00:00"
T2 = "2026-04-02T12:00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def two_snapshot_db(tmp_path_factory: pytest.TempPathFactory) -> str:
    """SQLite DB with two snapshots: ST001 price changes, ST002 stays the same."""
    db_path = str(tmp_path_factory.mktemp("reader_sqlite") / "reader_test.db")
    init_db(db_path)
    save_snapshot([_station("ST001", 175.0, T1), _station("ST002", 176.0, T1)], db_path)
    save_snapshot([_station("ST001", 180.0, T2), _station("ST002", 176.0, T2)], db_path)
    return db_path


@pytest.fixture(scope="module")
def hudi_table(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Write a two-commit Hudi table with CDC enabled, return its path.

    Commit 1 — two inserts (ST001 @ 175c, ST002 @ 176c)
    Commit 2 — ST001 price moves to 180c, ST002 re-written at same price.

    Both commits use fetched_at as precombine field, so Hudi writes both
    rows in commit 2 (newer timestamp).  CDC records op="i" for commit 1
    and op="u" for commit 2, with full before/after structs.
    """
    from src.hudi_writer import HUDI_OPTIONS, _get_spark

    spark = _get_spark()
    path = str(tmp_path_factory.mktemp("reader_hudi"))
    opts = {**HUDI_OPTIONS, "hoodie.table.name": "test_fuel_prices"}

    df1 = spark.createDataFrame(
        pd.DataFrame([_station("ST001", 175.0, T1), _station("ST002", 176.0, T1)])
    )
    df1.write.format("hudi").options(**opts).mode("append").save(path)

    df2 = spark.createDataFrame(
        pd.DataFrame([_station("ST001", 180.0, T2), _station("ST002", 176.0, T2)])
    )
    df2.write.format("hudi").options(**opts).mode("append").save(path)

    return path


# ---------------------------------------------------------------------------
# TestGetReader — factory, pure Python
# ---------------------------------------------------------------------------

class TestGetReader:
    def test_sqlite_returns_sqlite_reader(self, tmp_path: Path) -> None:
        assert isinstance(get_reader("sqlite", str(tmp_path / "t.db")), SqliteReader)

    def test_hudi_returns_hudi_reader(self) -> None:
        assert isinstance(get_reader("hudi", "/data/hudi/test"), HudiReader)

    def test_spark_alias_returns_hudi_reader(self) -> None:
        assert isinstance(get_reader("spark", "/data/hudi/test"), HudiReader)

    def test_unknown_backend_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown backend"):
            get_reader("postgres")

    def test_backend_is_case_insensitive(self, tmp_path: Path) -> None:
        assert isinstance(get_reader("SQLite", str(tmp_path / "t.db")), SqliteReader)
        assert isinstance(get_reader("HUDI", "/data/hudi"), HudiReader)

    def test_default_backend_sqlite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.reader.PERSISTENCE_BACKEND", "sqlite")
        assert isinstance(get_reader(path=str(tmp_path / "t.db")), SqliteReader)

    def test_default_backend_hudi(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("src.reader.PERSISTENCE_BACKEND", "hudi")
        assert isinstance(get_reader(path="/data/hudi"), HudiReader)

    def test_custom_sqlite_path_stored(self, tmp_path: Path) -> None:
        assert get_reader("sqlite", str(tmp_path / "c.db"))._db_path == str(tmp_path / "c.db")

    def test_custom_hudi_path_stored(self) -> None:
        assert get_reader("hudi", "/custom/path")._table_path == "/custom/path"


# ---------------------------------------------------------------------------
# TestPriceReaderABC — abstract-class invariants, pure Python
# ---------------------------------------------------------------------------

class TestPriceReaderABC:
    def test_cannot_instantiate_abstract_class(self) -> None:
        with pytest.raises(TypeError):
            PriceReader()  # type: ignore[abstract]

    def test_is_abc_subclass(self) -> None:
        assert issubclass(PriceReader, ABC)

    def test_sqlite_reader_is_price_reader(self) -> None:
        assert issubclass(SqliteReader, PriceReader)

    def test_hudi_reader_is_price_reader(self) -> None:
        assert issubclass(HudiReader, PriceReader)

    def test_sqlite_reader_implements_all_abstract_methods(self) -> None:
        assert not getattr(SqliteReader.read_all, "__isabstractmethod__", False)
        assert not getattr(SqliteReader.read_changes, "__isabstractmethod__", False)

    def test_hudi_reader_implements_all_abstract_methods(self) -> None:
        assert not getattr(HudiReader.read_all, "__isabstractmethod__", False)
        assert not getattr(HudiReader.read_changes, "__isabstractmethod__", False)


# ---------------------------------------------------------------------------
# TestSqliteReaderReadAll — real SQLite, real Spark
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSqliteReaderReadAll:
    def test_returns_expected_columns(self, two_snapshot_db: str) -> None:
        df = SqliteReader(two_snapshot_db).read_all()
        expected = {"station_id", "station_name", "city", "region", "fuel_type", "price", "fetched_at"}
        assert expected.issubset(set(df.columns))

    def test_all_snapshots_are_returned(self, two_snapshot_db: str) -> None:
        assert SqliteReader(two_snapshot_db).read_all().count() == 4

    def test_price_schema_is_double(self, two_snapshot_db: str) -> None:
        from pyspark.sql.types import DoubleType
        field = {f.name: f for f in SqliteReader(two_snapshot_db).read_all().schema}["price"]
        assert isinstance(field.dataType, DoubleType)

    def test_rows_ordered_by_station_then_time(self, two_snapshot_db: str) -> None:
        rows = SqliteReader(two_snapshot_db).read_all().collect()
        pairs = [(r.station_id, r.fetched_at) for r in rows]
        assert pairs == sorted(pairs)


# ---------------------------------------------------------------------------
# TestSqliteReaderReadChanges — real LAG window result
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestSqliteReaderReadChanges:
    def test_only_changed_stations_are_returned(self, two_snapshot_db: str) -> None:
        assert SqliteReader(two_snapshot_db).read_changes().count() == 1

    def test_correct_station_returned(self, two_snapshot_db: str) -> None:
        row = SqliteReader(two_snapshot_db).read_changes().first()
        assert row.station_id == "ST001"

    def test_delta_is_correct(self, two_snapshot_db: str) -> None:
        row = SqliteReader(two_snapshot_db).read_changes().first()
        assert row.delta == pytest.approx(5.0)

    def test_prev_price_populated(self, two_snapshot_db: str) -> None:
        row = SqliteReader(two_snapshot_db).read_changes().first()
        assert row.prev_price == pytest.approx(175.0)

    def test_result_has_expected_columns(self, two_snapshot_db: str) -> None:
        df = SqliteReader(two_snapshot_db).read_changes()
        assert {"prev_price", "prev_fetched_at", "delta"}.issubset(set(df.columns))


# ---------------------------------------------------------------------------
# TestHudiReaderReadAll — real Hudi incremental query
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestHudiReaderReadAll:
    def test_returns_rows_from_all_commits(self, hudi_table: str) -> None:
        # Incremental query from "000" returns the latest version per record key
        # (upsert semantics): 2 stations → 2 rows regardless of commit count.
        assert HudiReader(hudi_table).read_all().count() == 2

    def test_returns_expected_columns(self, hudi_table: str) -> None:
        df = HudiReader(hudi_table).read_all()
        assert {"station_id", "fuel_type", "price", "fetched_at"}.issubset(set(df.columns))

    def test_both_stations_present(self, hudi_table: str) -> None:
        stations = {
            r.station_id
            for r in HudiReader(hudi_table).read_all().select("station_id").collect()
        }
        assert stations == {"ST001", "ST002"}


# ---------------------------------------------------------------------------
# TestHudiReaderReadChanges — real CDC query (before/after structs)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestHudiReaderReadChanges:
    def test_returns_expected_columns(self, hudi_table: str) -> None:
        df = HudiReader(hudi_table).read_changes()
        assert {"op", "station_id", "fuel_type", "price", "prev_price", "delta"}.issubset(
            set(df.columns)
        )

    def test_inserts_have_null_prev_price(self, hudi_table: str) -> None:
        from pyspark.sql import functions as F
        inserts = HudiReader(hudi_table).read_changes().filter(F.col("op") == "i").collect()
        assert len(inserts) == 2
        assert all(r.prev_price is None for r in inserts)

    def test_updates_have_non_null_prev_price(self, hudi_table: str) -> None:
        from pyspark.sql import functions as F
        updates = HudiReader(hudi_table).read_changes().filter(F.col("op") == "u").collect()
        assert len(updates) == 2
        assert all(r.prev_price is not None for r in updates)

    def test_st001_update_has_correct_delta(self, hudi_table: str) -> None:
        from pyspark.sql import functions as F
        row = (
            HudiReader(hudi_table)
            .read_changes()
            .filter((F.col("op") == "u") & (F.col("station_id") == "ST001"))
            .first()
        )
        assert row is not None
        assert row.prev_price == pytest.approx(175.0)
        assert row.price == pytest.approx(180.0)
        assert row.delta == pytest.approx(5.0)

    def test_total_cdc_events(self, hudi_table: str) -> None:
        # 2 inserts (commit 1) + 2 updates (commit 2) = 4 events
        assert HudiReader(hudi_table).read_changes().count() == 4


# ---------------------------------------------------------------------------
# TestBackwardCompat — free functions are real end-to-end calls
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBackwardCompat:
    def test_read_price_history_returns_all_rows(self, two_snapshot_db: str) -> None:
        assert read_price_history("sqlite", two_snapshot_db).count() == 4

    def test_read_price_changes_returns_only_changes(self, two_snapshot_db: str) -> None:
        assert read_price_changes("sqlite", two_snapshot_db).count() == 1

    def test_read_price_history_returns_spark_dataframe(self, two_snapshot_db: str) -> None:
        from pyspark.sql import DataFrame
        assert isinstance(read_price_history("sqlite", two_snapshot_db), DataFrame)

    def test_read_price_changes_returns_spark_dataframe(self, two_snapshot_db: str) -> None:
        from pyspark.sql import DataFrame
        assert isinstance(read_price_changes("sqlite", two_snapshot_db), DataFrame)
