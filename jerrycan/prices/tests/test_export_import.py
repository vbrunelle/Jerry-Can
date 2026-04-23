"""Tests for the analysis export / import feature.

Includes a data-generation helper that creates ~4000 stations,
4 fuel types and ~3000 snapshots (with prices) so that the
performance of bulk export/import can be validated.
"""
import gzip
import json
import os
import random
import shutil
import tempfile
import time
from unittest.mock import patch
from decimal import Decimal
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from prices.models import (
    Analysis, AnalysisTransferTask, Fuel, Price, Snapshot, Station,
    _get_export_dir,
)

# ---------------------------------------------------------------------------
# Random data generation helper
# ---------------------------------------------------------------------------

FUEL_NAMES = ["Régulier", "Super", "Diesel", "Électrique"]
REGIONS = ["Montréal", "Québec", "Laval", "Gatineau", "Sherbrooke"]


def generate_random_analysis(
    *,
    num_stations=4000,
    num_fuels=4,
    num_snapshots=3000,
    prices_per_snapshot=16,
):
    """Create an Analysis with random stations, fuels, snapshots and prices.

    ``prices_per_snapshot`` controls how many prices each snapshot gets,
    drawn from random station/fuel pairs.  The default (16) keeps runtime
    low while still producing a realistic volume.
    """
    analysis = Analysis.objects.create(
        data_source_url="http://example.com/test.geojson.gz",
        update_frequency=5,
        active=False,
        run_automatically=False,
    )

    # Fuels ----------------------------------------------------------------
    fuels = []
    for name in FUEL_NAMES[:num_fuels]:
        fuel, _ = Fuel.objects.get_or_create(name=name)
        fuels.append(fuel)

    # Stations – bulk create -----------------------------------------------
    station_objs = [
        Station(
            name=f"Station-{i}",
            city=f"City-{i % 200}",
            region=random.choice(REGIONS),
            adress=f"{random.randint(1, 9999)} rue Test",
            longitude=-73.0 + random.uniform(-2, 2),
            latitude=45.0 + random.uniform(-2, 2),
            analysis=analysis,
        )
        for i in range(num_stations)
    ]
    Station.objects.bulk_create(station_objs, batch_size=2000)
    station_ids = list(
        Station.objects.filter(analysis=analysis).values_list("id", flat=True)
    )

    # Snapshots – bulk create ----------------------------------------------
    snapshot_objs = [
        Snapshot(analysis=analysis, status=Snapshot.Status.PROCESSED)
        for _ in range(num_snapshots)
    ]
    Snapshot.objects.bulk_create(snapshot_objs, batch_size=2000)
    snapshot_ids = list(
        Snapshot.objects.filter(analysis=analysis).values_list("id", flat=True)
    )

    # Prices – bulk create in batches --------------------------------------
    fuel_ids = [f.pk for f in fuels]
    price_batch = []
    BATCH = 5000
    for snap_id in snapshot_ids:
        chosen_stations = random.sample(
            station_ids, min(prices_per_snapshot // len(fuel_ids), len(station_ids))
        )
        for sid in chosen_stations:
            for fid in fuel_ids:
                price_batch.append(
                    Price(
                        station_id=sid,
                        fuel_id=fid,
                        snapshot_id=snap_id,
                        price=Decimal(str(round(random.uniform(100, 300), 1))),
                    )
                )
                if len(price_batch) >= BATCH:
                    Price.objects.bulk_create(price_batch)
                    price_batch = []
    if price_batch:
        Price.objects.bulk_create(price_batch)

    return analysis


# ---------------------------------------------------------------------------
# Unit tests – export / import logic
# ---------------------------------------------------------------------------

EXPORT_DIR = tempfile.mkdtemp(prefix="jerrycan_test_exports_")


@override_settings(ANALYSIS_EXPORT_DIR=EXPORT_DIR)
class ExportImportTests(TransactionTestCase):
    """Functional tests for the export → import round-trip."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.makedirs(EXPORT_DIR, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.analysis = Analysis.objects.create(
            data_source_url="http://example.com/data.geojson.gz",
            update_frequency=10,
            active=True,
            run_automatically=False,
            max_snapshots=50,
        )
        # Small dataset for fast unit tests
        self.fuel1 = Fuel.objects.create(name="Régulier")
        self.fuel2 = Fuel.objects.create(name="Diesel")
        self.station = Station.objects.create(
            name="TestStation", city="Laval", region="Montréal",
            adress="123 rue A", longitude=-73.5, latitude=45.5,
            analysis=self.analysis,
        )
        self.snapshot = Snapshot.objects.create(
            analysis=self.analysis, status=Snapshot.Status.PROCESSED,
        )
        Price.objects.create(station=self.station, fuel=self.fuel1,
                            snapshot=self.snapshot, price=Decimal("199.9"))
        Price.objects.create(station=self.station, fuel=self.fuel2,
                            snapshot=self.snapshot, price=Decimal("220.0"))

    # --- Export tests -------------------------------------------------------

    def test_export_creates_file(self):
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            analysis=self.analysis,
        )
        task._run_export()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.COMPLETED)
        self.assertTrue(os.path.isfile(task.file_path))

    def test_export_json_structure(self):
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            analysis=self.analysis,
        )
        task._run_export()
        task.refresh_from_db()

        with gzip.open(task.file_path, "rt") as f:
            data = json.load(f)

        self.assertEqual(data["version"], 1)
        self.assertIn("analysis", data)
        self.assertEqual(data["analysis"]["update_frequency"], 10)
        self.assertEqual(len(data["stations"]), 1)
        self.assertEqual(data["stations"][0]["name"], "TestStation")
        self.assertEqual(len(data["snapshots"]), 1)
        self.assertEqual(len(data["prices"]), 2)

    def test_export_contains_fuels(self):
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            analysis=self.analysis,
        )
        task._run_export()
        task.refresh_from_db()
        with gzip.open(task.file_path, "rt") as f:
            data = json.load(f)
        fuel_names = {fl["name"] for fl in data["fuels"]}
        self.assertIn("Régulier", fuel_names)
        self.assertIn("Diesel", fuel_names)

    # --- Import tests -------------------------------------------------------

    def _do_export_import(self):
        """Helper: export current analysis synchronously, then import."""
        export_task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            analysis=self.analysis,
        )
        export_task._run_export()
        export_task.refresh_from_db()

        import_task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=export_task.file_path,
        )
        import_task._run_import()
        import_task.refresh_from_db()
        return import_task

    def test_import_creates_analysis(self):
        """Export then import should create a new analysis with same data."""
        import_task = self._do_export_import()
        self.assertEqual(import_task.status, AnalysisTransferTask.Status.COMPLETED)
        self.assertIsNotNone(import_task.analysis)
        self.assertNotEqual(import_task.analysis.pk, self.analysis.pk)

    def test_import_recreates_stations(self):
        import_task = self._do_export_import()
        new_analysis = import_task.analysis
        self.assertEqual(
            Station.objects.filter(analysis=new_analysis).count(), 1
        )
        station = Station.objects.get(analysis=new_analysis)
        self.assertEqual(station.name, "TestStation")
        self.assertEqual(station.city, "Laval")

    def test_import_recreates_snapshots(self):
        import_task = self._do_export_import()
        new_analysis = import_task.analysis
        self.assertEqual(
            Snapshot.objects.filter(analysis=new_analysis).count(), 1
        )

    def test_import_preserves_snapshot_timestamps_and_count(self):
        # Add a second snapshot with a distinct timestamp, then export/import.
        second_snapshot = Snapshot.objects.create(
            analysis=self.analysis,
            status=Snapshot.Status.PROCESSED,
        )
        ts1 = timezone.now() - timedelta(days=3, hours=2)
        ts2 = timezone.now() - timedelta(days=1, minutes=30)
        Snapshot.objects.filter(pk=self.snapshot.pk).update(timestamp=ts1)
        Snapshot.objects.filter(pk=second_snapshot.pk).update(timestamp=ts2)

        Price.objects.create(
            station=self.station,
            fuel=self.fuel1,
            snapshot=second_snapshot,
            price=Decimal("201.1"),
        )

        import_task = self._do_export_import()
        new_analysis = import_task.analysis
        imported = list(
            Snapshot.objects.filter(analysis=new_analysis)
            .order_by('timestamp')
            .values_list('timestamp', flat=True)
        )

        self.assertEqual(len(imported), 2)
        self.assertEqual(imported[0], ts1)
        self.assertEqual(imported[1], ts2)

    def test_import_recreates_prices(self):
        import_task = self._do_export_import()
        new_analysis = import_task.analysis
        new_prices = Price.objects.filter(snapshot__analysis=new_analysis)
        self.assertEqual(new_prices.count(), 2)
        price_vals = set(new_prices.values_list("price", flat=True))
        self.assertIn(Decimal("199.90"), price_vals)
        self.assertIn(Decimal("220.00"), price_vals)

    def test_import_sets_run_automatically_false(self):
        """Imported analysis should never auto-run."""
        self.analysis.run_automatically = False
        self.analysis.save()
        import_task = self._do_export_import()
        self.assertFalse(import_task.analysis.run_automatically)

    def test_export_error_sets_status(self):
        """If the analysis is missing, the task should error."""
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            analysis=None,
        )
        task._run_export()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.ERROR)

    # --- View / access tests ------------------------------------------------

    def test_export_view_requires_staff(self):
        response = self.client.post(
            f"/analyses/{self.analysis.pk}/export/", follow=False
        )
        # Should redirect to login
        self.assertIn(response.status_code, [302, 301])
        self.assertIn("login", response.url)

    def test_import_view_requires_staff(self):
        response = self.client.post("/analyses/import/", follow=False)
        self.assertIn(response.status_code, [302, 301])
        self.assertIn("login", response.url)

    def test_download_requires_staff(self):
        response = self.client.get("/analyses/export/999/download/", follow=False)
        self.assertIn(response.status_code, [302, 301])

    def test_status_api_requires_staff(self):
        response = self.client.get("/api/transfer/999/status/", follow=False)
        self.assertIn(response.status_code, [302, 301])

    def test_export_view_triggers_task(self):
        user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client.force_login(user)
        response = self.client.post(
            f"/analyses/{self.analysis.pk}/export/", follow=False
        )
        self.assertEqual(response.status_code, 302)
        task = AnalysisTransferTask.objects.filter(
            analysis=self.analysis,
            task_type=AnalysisTransferTask.TaskType.EXPORT,
        ).first()
        self.assertIsNotNone(task)
        # Wait for the background thread to complete so it doesn't
        # conflict with subsequent tests via SQLite locking.
        for _ in range(50):
            task.refresh_from_db()
            if task.status in (AnalysisTransferTask.Status.COMPLETED,
                               AnalysisTransferTask.Status.ERROR):
                break
            time.sleep(0.1)

    def test_status_api_returns_json(self):
        user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client.force_login(user)
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            analysis=self.analysis,
            status=AnalysisTransferTask.Status.RUNNING,
        )
        response = self.client.get(f"/api/transfer/{task.pk}/status/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["task_type"], "export")

    def test_analyses_page_includes_chunked_upload_ui(self):
        user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client.force_login(user)
        response = self.client.get("/analyses/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("id=\"import-btn\"", content)
        self.assertIn("/static/prices/chunked-upload.js", content)
        self.assertIn("/analyses/import/", content)

    def test_chunk_upload_endpoints_require_staff(self):
        file_chunk = SimpleUploadedFile("chunk.gz", b"abc", content_type="application/gzip")
        response = self.client.post(
            "/api/upload/chunk/",
            data={
                "uploadId": "u1",
                "chunkNumber": "0",
                "totalChunks": "1",
                "chunk": file_chunk,
            },
            follow=False,
        )
        self.assertIn(response.status_code, [302, 301])
        self.assertIn("login", response.url)

        response = self.client.post(
            "/api/upload/complete/",
            data=json.dumps({"uploadId": "u1"}),
            content_type="application/json",
            follow=False,
        )
        self.assertIn(response.status_code, [302, 301])
        self.assertIn("login", response.url)

    def test_chunk_upload_complete_creates_import_task(self):
        user = User.objects.create_superuser("admin", "a@b.com", "pass")
        self.client.force_login(user)

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
                    "id": 1,
                    "name": "Station A",
                    "city": "Montreal",
                    "region": "Montreal",
                    "adress": "1 Rue Test",
                    "longitude": -73.5,
                    "latitude": 45.5,
                }
            ],
            "snapshots": [
                {"id": 1, "timestamp": "2026-01-01T00:00:00+00:00", "status": "processed"}
            ],
            "prices": [{"station_id": 1, "fuel_id": 1, "price": 199.9, "snapshot_id": 1}],
        }
        gz_data = gzip.compress(json.dumps(payload).encode("utf-8"))
        mid = max(1, len(gz_data) // 2)
        upload_id = "test-upload-1"

        response = self.client.post(
            "/api/upload/chunk/",
            data={
                "uploadId": upload_id,
                "chunkNumber": "0",
                "totalChunks": "2",
                "chunk": SimpleUploadedFile("part0.gz", gz_data[:mid], content_type="application/gzip"),
            },
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            "/api/upload/chunk/",
            data={
                "uploadId": upload_id,
                "chunkNumber": "1",
                "totalChunks": "2",
                "chunk": SimpleUploadedFile("part1.gz", gz_data[mid:], content_type="application/gzip"),
            },
        )
        self.assertEqual(response.status_code, 200)

        with patch.object(AnalysisTransferTask, 'start_import', return_value=None):
            response = self.client.post(
                "/api/upload/complete/",
                data=json.dumps({"uploadId": upload_id}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "import_started")
        self.assertIn("taskId", data)

        task = AnalysisTransferTask.objects.get(pk=data["taskId"])
        self.assertEqual(task.task_type, AnalysisTransferTask.TaskType.IMPORT)
        self.assertTrue(task.file_path.endswith(".json.gz"))


# ---------------------------------------------------------------------------
# Performance test with large random data
# ---------------------------------------------------------------------------

@override_settings(ANALYSIS_EXPORT_DIR=EXPORT_DIR)
class ExportImportPerformanceTest(TransactionTestCase):
    """Generate ~4000 stations, 4 fuels, ~3000 snapshots and test
    that export + import completes within acceptable time."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.makedirs(EXPORT_DIR, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
        super().tearDownClass()

    def test_export_import_large_dataset(self):
        # Generate data
        t0 = time.time()
        analysis = generate_random_analysis(
            num_stations=4000,
            num_fuels=4,
            num_snapshots=3000,
            prices_per_snapshot=16,  # 4 stations × 4 fuels per snapshot
        )
        gen_time = time.time() - t0

        # Verify generated counts
        station_count = Station.objects.filter(analysis=analysis).count()
        snapshot_count = Snapshot.objects.filter(analysis=analysis).count()
        price_count = Price.objects.filter(snapshot__analysis=analysis).count()
        fuel_count = Fuel.objects.count()

        self.assertEqual(station_count, 4000)
        self.assertEqual(snapshot_count, 3000)
        self.assertEqual(fuel_count, 4)
        self.assertGreater(price_count, 0)

        print(f"\n=== Data Generation ===")
        print(f"  Stations: {station_count}")
        print(f"  Snapshots: {snapshot_count}")
        print(f"  Fuels: {fuel_count}")
        print(f"  Prices: {price_count}")
        print(f"  Time: {gen_time:.2f}s")

        # Export
        t0 = time.time()
        export_task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            analysis=analysis,
        )
        thread = export_task.start_export()
        thread.join(timeout=300)
        export_task.refresh_from_db()
        export_time = time.time() - t0

        self.assertEqual(export_task.status, AnalysisTransferTask.Status.COMPLETED,
                         f"Export failed: {export_task.error_message}")
        file_size = os.path.getsize(export_task.file_path)
        print(f"\n=== Export ===")
        print(f"  Status: {export_task.status}")
        print(f"  File size: {file_size / 1024 / 1024:.2f} MB")
        print(f"  Time: {export_time:.2f}s")

        # Import into a new analysis
        t0 = time.time()
        import_task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=export_task.file_path,
        )
        thread = import_task.start_import()
        thread.join(timeout=600)
        import_task.refresh_from_db()
        import_time = time.time() - t0

        self.assertEqual(import_task.status, AnalysisTransferTask.Status.COMPLETED,
                         f"Import failed: {import_task.error_message}")

        new_analysis = import_task.analysis
        new_station_count = Station.objects.filter(analysis=new_analysis).count()
        new_snapshot_count = Snapshot.objects.filter(analysis=new_analysis).count()
        new_price_count = Price.objects.filter(snapshot__analysis=new_analysis).count()

        print(f"\n=== Import ===")
        print(f"  Status: {import_task.status}")
        print(f"  Stations: {new_station_count}")
        print(f"  Snapshots: {new_snapshot_count}")
        print(f"  Prices: {new_price_count}")
        print(f"  Time: {import_time:.2f}s")

        # Verify data integrity
        self.assertEqual(new_station_count, station_count)
        self.assertEqual(new_snapshot_count, snapshot_count)
        self.assertEqual(new_price_count, price_count)

        # Verify analysis fields
        self.assertEqual(new_analysis.update_frequency, analysis.update_frequency)
        self.assertEqual(new_analysis.data_source_url, analysis.data_source_url)
        self.assertFalse(new_analysis.run_automatically)

        print(f"\n=== Summary ===")
        print(f"  Data generation: {gen_time:.2f}s")
        print(f"  Export: {export_time:.2f}s")
        print(f"  Import: {import_time:.2f}s")
        print(f"  Total: {gen_time + export_time + import_time:.2f}s")
        print(f"  All counts verified ✓")
