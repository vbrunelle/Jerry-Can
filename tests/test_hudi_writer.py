"""Tests for the Hudi writer module.

PySpark and the Hudi bundle are heavy runtime dependencies that may not be
available in every environment.  These tests therefore **mock** the PySpark
layer and verify that ``hudi_writer`` calls the Spark API correctly.
"""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_spark():
    """Ensure the module-level ``_spark`` singleton is cleared between tests."""
    import src.hudi_writer as hw

    hw._spark = None
    yield
    hw._spark = None


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


# ---------------------------------------------------------------------------
# Helper – build a fake SparkSession that records method calls
# ---------------------------------------------------------------------------

def _mock_spark_session() -> MagicMock:
    """Return a MagicMock that mimics SparkSession for write/read paths."""
    session = MagicMock()

    # Write chain: spark.createDataFrame(…).write.format("hudi").options(…).mode(…).save(…)
    mock_df = MagicMock()
    session.createDataFrame.return_value = mock_df
    mock_df.write.format.return_value.options.return_value.mode.return_value.save = MagicMock()

    # Read chain: spark.read.format("hudi").option(…).load(…)
    session.read.format.return_value.option.return_value.load.return_value = MagicMock()
    session.read.format.return_value.option.return_value.option.return_value.load.return_value = (
        MagicMock()
    )

    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestInitHudi:
    """Verify that ``init_hudi`` creates a Spark session."""

    @patch("src.hudi_writer._get_spark")
    def test_init_calls_get_spark(self, mock_get: MagicMock) -> None:
        from src.hudi_writer import init_hudi

        init_hudi()
        mock_get.assert_called_once()


class TestSaveSnapshot:
    """Verify the ``save_snapshot`` write path."""

    def test_returns_zero_for_empty_list(self) -> None:
        import src.hudi_writer as hw

        hw._spark = _mock_spark_session()
        count = hw.save_snapshot([])
        assert count == 0

    def test_returns_record_count(self) -> None:
        import src.hudi_writer as hw

        hw._spark = _mock_spark_session()
        records = _sample_records(5)
        count = hw.save_snapshot(records, table_path="/tmp/test_hudi")
        assert count == 5

    def test_creates_dataframe_from_records(self) -> None:
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        records = _sample_records(2)

        hw.save_snapshot(records, table_path="/tmp/test_hudi")

        # createDataFrame must have been called once with a list of dicts
        mock_spark.createDataFrame.assert_called_once()
        data_arg = mock_spark.createDataFrame.call_args[0][0]
        assert len(data_arg) == 2
        assert data_arg[0]["station_id"] == "ST0001"
        assert data_arg[1]["station_id"] == "ST0002"

    def test_writes_in_hudi_format(self) -> None:
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        records = _sample_records(1)

        hw.save_snapshot(records, table_path="/tmp/test_hudi")

        mock_df = mock_spark.createDataFrame.return_value
        mock_df.write.format.assert_called_once_with("hudi")

    def test_saves_to_specified_path(self) -> None:
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        records = _sample_records(1)
        custom_path = "/custom/path/fuel_prices"

        hw.save_snapshot(records, table_path=custom_path)

        mock_df = mock_spark.createDataFrame.return_value
        writer = mock_df.write.format.return_value.options.return_value.mode.return_value
        writer.save.assert_called_once_with(custom_path)

    def test_normalises_none_region_to_empty(self) -> None:
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        records = _sample_records(1)
        records[0]["region"] = None

        hw.save_snapshot(records, table_path="/tmp/test_hudi")

        data_arg = mock_spark.createDataFrame.call_args[0][0]
        assert data_arg[0]["region"] == ""

    def test_normalises_none_coords_to_zero(self) -> None:
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        records = _sample_records(1)
        records[0]["latitude"] = None
        records[0]["longitude"] = None

        hw.save_snapshot(records, table_path="/tmp/test_hudi")

        data_arg = mock_spark.createDataFrame.call_args[0][0]
        assert data_arg[0]["latitude"] == 0.0
        assert data_arg[0]["longitude"] == 0.0


class TestGetSnapshotAt:
    """Verify the ``get_snapshot_at`` time-travel read path."""

    def test_reads_hudi_with_time_travel(self) -> None:
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        ts = "2026-04-01T12:00:00"

        hw.get_snapshot_at(ts, table_path="/tmp/test_hudi")

        mock_spark.read.format.assert_called_once_with("hudi")
        mock_spark.read.format.return_value.option.assert_called_once_with(
            "as.of.instant", ts,
        )
        mock_spark.read.format.return_value.option.return_value.load.assert_called_once_with(
            "/tmp/test_hudi",
        )


class TestGetChangesSince:
    """Verify the ``get_changes_since`` incremental-query read path."""

    def test_reads_hudi_incrementally(self) -> None:
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        ts = "2026-04-01T00:00:00"

        hw.get_changes_since(ts, table_path="/tmp/test_hudi")

        mock_spark.read.format.assert_called_once_with("hudi")
        # Two chained .option() calls
        first_option = mock_spark.read.format.return_value.option
        first_option.assert_called_once_with(
            "hoodie.datasource.query.type", "incremental",
        )
        second_option = first_option.return_value.option
        second_option.assert_called_once_with(
            "hoodie.datasource.read.begin.instanttime", ts,
        )
        second_option.return_value.load.assert_called_once_with("/tmp/test_hudi")


class TestStop:
    """Verify that ``stop`` tears down the Spark session."""

    def test_stops_spark_session(self) -> None:
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark

        hw.stop()

        mock_spark.stop.assert_called_once()
        assert hw._spark is None

    def test_stop_is_idempotent(self) -> None:
        import src.hudi_writer as hw

        hw._spark = None
        hw.stop()  # should not raise
        assert hw._spark is None
