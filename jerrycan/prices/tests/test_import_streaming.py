"""TDD tests for streaming JSON import.

These tests describe the *desired* behaviour: the import must parse the archive
as a stream (one record at a time) instead of loading the entire file into RAM
at once.  They are written **before** the implementation so that they start RED
and turn GREEN once the fix is in place.

Root cause
----------
``AnalysisTransferTask._do_import`` currently calls ``json.load(f)`` which
deserialises the whole gzip archive – including every price record – into a
single Python dict.  On a machine limited to 2 GB RAM a dataset with several
million prices can push RSS above the container limit, causing Docker to
OOM-kill the process and restart the server.

Expected fix
------------
Replace ``json.load`` with ``ijson`` iterative parsing so that the prices
array is consumed one item at a time and never fully materialised in memory.
"""

import gzip
import json
import os
import shutil
import tempfile
import tracemalloc
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from prices.models import Analysis, AnalysisTransferTask, Fuel, Price, Snapshot, Station

EXPORT_DIR = tempfile.mkdtemp(prefix="jerrycan_streaming_tests_")

# Peak-memory budget for an import of LARGE_PRICE_COUNT prices.
# Measured baseline: json.load allocates ~54 MB for 200 000 prices.
# A correct streaming implementation keeps only one batch (~5 000 dicts)
# live at a time, so peak should stay well under 20 MB.
LARGE_PRICE_COUNT = 200_000
MEMORY_BUDGET_MB = 20


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_export_file(path, num_prices=1):
    """Write a minimal valid export archive to *path*."""
    payload = {
        "version": 1,
        "analysis": {
            "update_frequency": 5,
            "data_source_url": "http://example.com/data.geojson.gz",
            "active": False,
            "run_automatically": False,
            "max_snapshots": 10,
        },
        "fuels": [{"id": 1, "name": "Régulier"}],
        "stations": [
            {
                "id": 1, "name": "Station Test", "city": "Laval",
                "region": "Montréal", "adress": "1 Rue Test",
                "longitude": -73.5, "latitude": 45.5,
            }
        ],
        "snapshots": [
            {"id": 1, "timestamp": "2026-01-01T00:00:00+00:00", "status": "processed"}
        ],
        "prices": [
            {"station_id": 1, "fuel_id": 1, "price": 100.0 + i % 200, "snapshot_id": 1}
            for i in range(num_prices)
        ],
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(payload, f)
    return path


# ---------------------------------------------------------------------------
# Tests – these must be RED with the current json.load implementation
# ---------------------------------------------------------------------------

@override_settings(ANALYSIS_EXPORT_DIR=EXPORT_DIR)
class StreamingImportStructureTests(TransactionTestCase):
    """Structural tests: verify HOW the import parses its input.

    RED reason: the current code calls ``json.load(f)`` which loads everything
    at once.  These tests assert the opposite – they will therefore FAIL until
    the streaming implementation is in place.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.makedirs(EXPORT_DIR, exist_ok=True)
        cls.export_file = _make_export_file(
            os.path.join(EXPORT_DIR, "streaming_structure.json.gz"),
            num_prices=10,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
        super().tearDownClass()

    def _make_task(self):
        return AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=self.export_file,
        )

    # -- Test 1 ---------------------------------------------------------------

    def test_import_does_not_use_json_load(self):
        """_do_import must NOT call json.load (loads entire file into RAM).

        RED: current code calls json.load → mock.assert_not_called() raises.
        GREEN: after fix, ijson is used and json.load is no longer called.
        """
        task = self._make_task()
        with patch('prices.models.json.load') as mock_json_load:
            task._run_import()
        mock_json_load.assert_not_called()

    # -- Test 2 ---------------------------------------------------------------

    def test_import_uses_ijson_for_prices(self):
        """_do_import must use ijson to iterate prices as a stream.

        RED: current code doesn't import ijson at all in prices.models →
             the assertion below fails immediately with a clear message.
        GREEN: after fix, prices.models imports ijson and uses ijson.items.
        """
        import prices.models as models_module

        self.assertTrue(
            hasattr(models_module, 'ijson'),
            "_do_import must import ijson for streaming JSON parsing. "
            "Currently json.load is used which loads the entire archive "
            "into RAM at once, causing OOM on low-memory machines.",
        )


# ---------------------------------------------------------------------------
# Test – memory ceiling
# ---------------------------------------------------------------------------

@override_settings(ANALYSIS_EXPORT_DIR=EXPORT_DIR)
class StreamingImportMemoryTests(TransactionTestCase):
    """Memory-ceiling test: peak allocation during import must stay low.

    RED reason: with json.load, importing 200 000 prices allocates ~54 MB;
    the budget is 20 MB.
    GREEN: streaming with ijson keeps only one batch (~5 000 items) in memory,
    staying well under the budget.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.makedirs(EXPORT_DIR, exist_ok=True)
        cls.large_export_file = _make_export_file(
            os.path.join(EXPORT_DIR, "streaming_memory.json.gz"),
            num_prices=LARGE_PRICE_COUNT,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
        super().tearDownClass()

    def test_import_peak_memory_stays_under_budget(self):
        """Peak heap allocated during _do_import must be < MEMORY_BUDGET_MB.

        RED: json.load materialises all 200 000 price dicts simultaneously (~54 MB),
        exceeding the 20 MB budget.
        GREEN: ijson streaming keeps only one batch (~5 000 dicts) live at once.
        """
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=self.large_export_file,
        )

        tracemalloc.start()
        tracemalloc.clear_traces()
        task._run_import()
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        task.refresh_from_db()
        self.assertEqual(
            task.status, AnalysisTransferTask.Status.COMPLETED,
            f"Import failed: {task.error_message}",
        )

        peak_mb = peak_bytes / 1024 / 1024
        self.assertLess(
            peak_mb,
            MEMORY_BUDGET_MB,
            f"Peak memory {peak_mb:.1f} MB exceeded budget of {MEMORY_BUDGET_MB} MB. "
            f"The import is likely loading all prices into RAM at once (json.load).",
        )
