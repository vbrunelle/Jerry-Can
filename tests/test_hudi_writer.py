"""Tests for the Hudi writer module.

PySpark and the Hudi bundle are heavy runtime dependencies that may not be
available in every environment.  These tests therefore **mock** the PySpark
layer and verify that ``hudi_writer`` calls the Spark API correctly.
"""

import re
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


class TestHudiBundleCompatibility:
    """Verify that the Hudi bundle coordinates match the installed PySpark."""

    def test_bundle_scala_version_matches_pyspark(self) -> None:
        """The Scala version in the Hudi bundle must match PySpark's Scala."""
        import pyspark
        import src.hudi_writer as hw

        # Detect Scala version from PySpark's bundled JARs
        import pathlib
        jars_dir = pathlib.Path(pyspark.__file__).parent / "jars"
        scala_jars = list(jars_dir.glob("scala-library-*.jar"))
        assert scala_jars, "No scala-library JAR found in PySpark jars"
        # Extract e.g. "2.13" from "scala-library-2.13.17.jar"
        jar_name = scala_jars[0].name
        m = re.search(r"scala-library-(\d+\.\d+)", jar_name)
        assert m, f"Cannot parse Scala version from {jar_name}"
        pyspark_scala = m.group(1)

        # Extract Scala version from Hudi bundle coordinates
        # e.g. "org.apache.hudi:hudi-spark3.5-bundle_2.12:0.15.0" → "2.12"
        bundle = hw._HUDI_SPARK_BUNDLE
        m2 = re.search(r"_(\d+\.\d+):", bundle)
        assert m2, f"Cannot parse Scala version from bundle: {bundle}"
        bundle_scala = m2.group(1)

        assert pyspark_scala == bundle_scala, (
            f"Scala version mismatch: PySpark uses Scala {pyspark_scala} "
            f"but Hudi bundle targets Scala {bundle_scala} ({bundle})"
        )

    def test_bundle_spark_version_matches_pyspark(self) -> None:
        """The Spark major.minor in the Hudi bundle must match PySpark."""
        import pyspark
        import src.hudi_writer as hw

        pyspark_major_minor = ".".join(pyspark.__version__.split(".")[:2])
        bundle = hw._HUDI_SPARK_BUNDLE

        # e.g. "hudi-spark3.5-bundle" → "3.5"
        m = re.search(r"hudi-spark([\d.]+)-bundle", bundle)
        assert m, f"Cannot parse Spark version from bundle: {bundle}"
        bundle_spark = m.group(1)

        assert pyspark_major_minor == bundle_spark, (
            f"Spark version mismatch: PySpark is {pyspark_major_minor} "
            f"but Hudi bundle targets Spark {bundle_spark} ({bundle})"
        )


class TestGetSpark:
    """Verify ``_get_spark`` behaviour when SparkSession creation fails."""

    @patch("src.hudi_writer.SparkSession", create=True)
    def test_raises_when_spark_session_creation_fails(self, _mock_cls: MagicMock) -> None:
        """Simulates JAVA_HOME not set / Java gateway failure."""
        import src.hudi_writer as hw

        with patch.dict("sys.modules", {"pyspark.sql": MagicMock()}):
            # Make the lazy import of SparkSession raise on getOrCreate
            fake_builder = MagicMock()
            fake_builder.appName.return_value = fake_builder
            fake_builder.config.return_value = fake_builder
            fake_builder.getOrCreate.side_effect = RuntimeError(
                "Java gateway process exited before sending its port number."
            )
            mock_session_cls = MagicMock()
            mock_session_cls.builder = fake_builder
            with patch.dict("sys.modules", {"pyspark": MagicMock(), "pyspark.sql": MagicMock(SparkSession=mock_session_cls)}):
                hw._spark = None
                with pytest.raises(RuntimeError, match="Java gateway"):
                    hw._get_spark()


class TestInitHudi:
    """Verify that ``init_hudi`` creates a Spark session."""

    @patch("src.hudi_writer._get_spark")
    def test_init_calls_get_spark(self, mock_get: MagicMock) -> None:
        from src.hudi_writer import init_hudi

        init_hudi()
        mock_get.assert_called_once()

    @patch("src.hudi_writer._get_spark")
    def test_init_propagates_spark_error(self, mock_get: MagicMock) -> None:
        """When _get_spark fails (e.g. no Java), init_hudi must propagate."""
        from src.hudi_writer import init_hudi

        mock_get.side_effect = RuntimeError("Java gateway process exited")
        with pytest.raises(RuntimeError, match="Java gateway"):
            init_hudi()


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

    def test_normalises_none_string_fields_to_empty(self) -> None:
        """None values for string fields must become '' not 'None'."""
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        records = _sample_records(1)
        records[0]["station_name"] = None
        records[0]["address"] = None
        records[0]["city"] = None

        hw.save_snapshot(records, table_path="/tmp/test_hudi")

        data_arg = mock_spark.createDataFrame.call_args[0][0]
        assert data_arg[0]["station_name"] == ""
        assert data_arg[0]["address"] == ""
        assert data_arg[0]["city"] == ""

    def test_normalises_none_fuel_type_to_default(self) -> None:
        """None fuel_type must fall back to 'regular', not 'None'."""
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        records = _sample_records(1)
        records[0]["fuel_type"] = None

        hw.save_snapshot(records, table_path="/tmp/test_hudi")

        data_arg = mock_spark.createDataFrame.call_args[0][0]
        assert data_arg[0]["fuel_type"] == "regular"

    def test_normalises_none_fetched_at_to_now(self) -> None:
        """None fetched_at must fall back to current timestamp, not 'None'."""
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        records = _sample_records(1)
        records[0]["fetched_at"] = None

        hw.save_snapshot(records, table_path="/tmp/test_hudi")

        data_arg = mock_spark.createDataFrame.call_args[0][0]
        assert data_arg[0]["fetched_at"] != "None"
        assert "T" in data_arg[0]["fetched_at"]  # ISO-8601 format

    def test_raises_when_spark_write_fails(self) -> None:
        """When Hudi write fails, the error must propagate to the caller."""
        import src.hudi_writer as hw

        mock_spark = _mock_spark_session()
        hw._spark = mock_spark
        # Make the save() call raise
        mock_df = mock_spark.createDataFrame.return_value
        mock_df.write.format.return_value.options.return_value.mode.return_value.save.side_effect = (
            RuntimeError("Hudi write failed: disk full")
        )
        records = _sample_records(1)
        with pytest.raises(RuntimeError, match="Hudi write failed"):
            hw.save_snapshot(records, table_path="/tmp/test_hudi")


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
