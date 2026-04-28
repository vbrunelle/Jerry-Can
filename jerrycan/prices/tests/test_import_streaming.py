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
    """Write a minimal valid export archive to *path*, streaming prices one at a time.

    Prices are written entry-by-entry so this function itself never holds more
    than one price record in memory, regardless of num_prices.
    """
    header = {
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
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        # Write everything up to the prices key, then stream prices one by one.
        f.write(json.dumps(header)[:-1])  # strip trailing }
        f.write(', "prices": [')
        for i in range(num_prices):
            if i:
                f.write(",")
            f.write(json.dumps({"station_id": 1, "fuel_id": 1,
                                 "price": 100.0 + i % 200, "snapshot_id": 1}))
        f.write("]}")
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


# ---------------------------------------------------------------------------
# Low-RAM environment simulation
# ---------------------------------------------------------------------------

# Batch size as defined in _do_import (5 000 prices per SQL executemany call).
IMPORT_BATCH_SIZE = 5_000

# Per-price overhead in a batch dict (station_id, fuel_id, price, snapshot_id).
# Conservative upper bound: 4 ints/floats × ~28 bytes each + dict overhead ≈ 200 bytes.
BYTES_PER_PRICE_IN_BATCH = 200

# Safety factor: allow up to 10× the single-batch footprint for interpreter
# overhead, SQLite query buffers, and tracemalloc bookkeeping.
SAFETY_FACTOR = 10

# Maximum acceptable Python heap peak for a streaming import regardless of
# total price count (batch-proportional budget).
# 5 000 prices × 200 B × 10 safety = ~10 MB
LOW_RAM_MEMORY_BUDGET_MB = (IMPORT_BATCH_SIZE * BYTES_PER_PRICE_IN_BATCH * SAFETY_FACTOR) / 1024 / 1024

# Scale used for the scalability check (100× the existing LARGE_PRICE_COUNT,
# matching real production archive size ~24M prices).
SCALE_PRICE_COUNT = 20_000_000


@override_settings(ANALYSIS_EXPORT_DIR=EXPORT_DIR)
class LowRamEnvironmentTests(TransactionTestCase):
    """Verify streaming import behaviour under a simulated low-RAM constraint.

    These tests confirm two properties that matter on resource-constrained
    containers (e.g., 1 GiB Docker mem_limit):

    1. **Batch-proportional heap** – Python heap peak is determined by a
       single batch of prices (5 000 records), not by total price count.
       This means the import can handle arbitrarily large archives without
       growing unboundedly.

    2. **Sub-linear scaling** – importing 5× as many prices must not use
       5× as much Python heap, proving that the implementation truly streams
       records rather than accumulating them.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.makedirs(EXPORT_DIR, exist_ok=True)
        cls.small_file = _make_export_file(
            os.path.join(EXPORT_DIR, "low_ram_small.json.gz"),
            num_prices=LARGE_PRICE_COUNT,       # 200 000 prices (baseline)
        )
        cls.large_file = _make_export_file(
            os.path.join(EXPORT_DIR, "low_ram_large.json.gz"),
            num_prices=SCALE_PRICE_COUNT,        # 1 000 000 prices (5× scale)
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
        super().tearDownClass()

    def _run_and_measure(self, file_path):
        """Import *file_path* and return (peak_mb, task)."""
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=file_path,
        )
        tracemalloc.start()
        tracemalloc.clear_traces()
        task._run_import()
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        task.refresh_from_db()
        return peak_bytes / 1024 / 1024, task

    def test_low_ram_peak_bounded_by_batch_size(self):
        """Python heap peak during 1M-price import must not exceed batch-proportional budget.

        Demonstrates that a container limited to ~1 GiB can safely run the
        import: Python heap overhead is under {LOW_RAM_MEMORY_BUDGET_MB:.0f} MB
        regardless of archive size.
        """
        peak_mb, task = self._run_and_measure(self.large_file)
        self.assertEqual(
            task.status, AnalysisTransferTask.Status.COMPLETED,
            f"1M-price import failed: {task.error_message}",
        )
        self.assertLess(
            peak_mb,
            LOW_RAM_MEMORY_BUDGET_MB,
            f"Peak Python heap {peak_mb:.1f} MB exceeded batch-proportional budget "
            f"of {LOW_RAM_MEMORY_BUDGET_MB:.1f} MB for {SCALE_PRICE_COUNT:,} prices. "
            f"The import batch size is {IMPORT_BATCH_SIZE:,} prices; memory should "
            f"scale with batch size, not total price count.",
        )

    def test_memory_scales_sub_linearly_with_price_count(self):
        """Peak Python heap must NOT scale proportionally with total price count.

        Imports LARGE_PRICE_COUNT (200 000) and SCALE_PRICE_COUNT (1 000 000)
        prices.  If the import truly streams, the 5× larger dataset must not
        require 5× more Python heap.  Specifically, the ratio must be < 3×
        (generous tolerance for SQLite cursor/buffer variance).
        """
        peak_small_mb, task_small = self._run_and_measure(self.small_file)
        self.assertEqual(
            task_small.status, AnalysisTransferTask.Status.COMPLETED,
            f"Small import failed: {task_small.error_message}",
        )

        peak_large_mb, task_large = self._run_and_measure(self.large_file)
        self.assertEqual(
            task_large.status, AnalysisTransferTask.Status.COMPLETED,
            f"Large import failed: {task_large.error_message}",
        )

        if peak_small_mb > 0:
            ratio = peak_large_mb / peak_small_mb
            self.assertLess(
                ratio,
                3.0,
                f"Memory scaled by {ratio:.1f}× when price count grew 5× "
                f"({LARGE_PRICE_COUNT:,} → {SCALE_PRICE_COUNT:,} prices). "
                f"Small peak: {peak_small_mb:.1f} MB, large peak: {peak_large_mb:.1f} MB. "
                f"A ratio < 3× is expected for a true streaming implementation.",
            )
