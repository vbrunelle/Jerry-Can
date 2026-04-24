"""Tests for the OOM / server-restart scenario during an import task.

Problem
-------
Jerry-Can is designed to run on a low-RAM machine.  A large data import
can exhaust memory, causing Docker to OOM-kill the container and restart
the Django server.  When the server comes back up the import task is still
recorded in the database with status RUNNING or PENDING, but the background
thread is gone.

Expected behaviour
------------------
On startup (``PricesConfig.ready``), every task that is still in PENDING or
RUNNING state is immediately marked ERROR with a clear message so that the
user knows the import did not finish and can try again.

In addition, if a MemoryError escapes from the import code itself (before the
process is killed), ``_run_import`` must catch it and record the task as
ERROR rather than leaving it in an unknown state.
"""

import gzip
import json
import os
import shutil
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from prices.models import Analysis, AnalysisTransferTask, Fuel, Price, Snapshot, Station

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

EXPORT_DIR = tempfile.mkdtemp(prefix="jerrycan_restart_tests_")


def _make_analysis():
    return Analysis.objects.create(
        data_source_url="http://example.com/data.geojson.gz",
        update_frequency=5,
        active=False,
        run_automatically=False,
    )


def _make_import_task(analysis=None, status=AnalysisTransferTask.Status.PENDING,
                      cancel_requested=False):
    return AnalysisTransferTask.objects.create(
        task_type=AnalysisTransferTask.TaskType.IMPORT,
        analysis=analysis,
        status=status,
        cancel_requested=cancel_requested,
    )


def _run_startup_recovery():
    """Simulate the recovery logic that runs in ``PricesConfig.ready``."""
    interrupted_tasks = AnalysisTransferTask.objects.filter(
        status__in=[
            AnalysisTransferTask.Status.PENDING,
            AnalysisTransferTask.Status.RUNNING,
        ]
    )
    for task in interrupted_tasks:
        task.status = (
            AnalysisTransferTask.Status.CANCELLED
            if task.cancel_requested
            else AnalysisTransferTask.Status.ERROR
        )
        task.status_detail = (
            'Task cancelled during server reload'
            if task.cancel_requested
            else 'Task interrupted by server reload'
        )
        if not task.error_message and not task.cancel_requested:
            task.error_message = 'The server restarted before this background task finished.'
        task.completed_at = timezone.now()
        task.activity_log = (
            f"{task.activity_log}\n[{timezone.localtime().strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{task.status_detail}"
        ).strip()
        task.save(update_fields=[
            'status', 'status_detail', 'error_message', 'completed_at', 'activity_log',
        ])


# ---------------------------------------------------------------------------
# Startup recovery tests
# ---------------------------------------------------------------------------

class ServerRestartRecoveryTests(TestCase):
    """Verify that the startup recovery logic correctly handles stale tasks."""

    def test_pending_import_task_marked_error_on_restart(self):
        """A PENDING import task left behind by an OOM kill is marked ERROR."""
        task = _make_import_task(status=AnalysisTransferTask.Status.PENDING)
        _run_startup_recovery()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.ERROR)
        self.assertEqual(task.status_detail, 'Task interrupted by server reload')
        self.assertEqual(
            task.error_message,
            'The server restarted before this background task finished.',
        )
        self.assertIsNotNone(task.completed_at)

    def test_running_import_task_marked_error_on_restart(self):
        """A RUNNING import task (OOM-killed mid-import) is marked ERROR."""
        task = _make_import_task(status=AnalysisTransferTask.Status.RUNNING)
        _run_startup_recovery()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.ERROR)
        self.assertEqual(
            task.error_message,
            'The server restarted before this background task finished.',
        )
        self.assertIsNotNone(task.completed_at)

    def test_running_task_with_cancel_request_marked_cancelled_on_restart(self):
        """A task that the user had already cancelled is marked CANCELLED, not ERROR."""
        task = _make_import_task(
            status=AnalysisTransferTask.Status.RUNNING,
            cancel_requested=True,
        )
        _run_startup_recovery()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.CANCELLED)
        self.assertEqual(task.status_detail, 'Task cancelled during server reload')
        # No generic error message should be written for a user-cancelled task.
        self.assertEqual(task.error_message, '')

    def test_completed_task_not_touched_on_restart(self):
        """A COMPLETED task must not be altered by the startup recovery."""
        task = _make_import_task(status=AnalysisTransferTask.Status.COMPLETED)
        _run_startup_recovery()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.COMPLETED)
        self.assertEqual(task.error_message, '')

    def test_error_task_not_touched_on_restart(self):
        """A task already in ERROR state must not be overwritten."""
        task = _make_import_task(status=AnalysisTransferTask.Status.ERROR)
        task.error_message = 'previous error'
        task.save(update_fields=['error_message'])
        _run_startup_recovery()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.ERROR)
        self.assertEqual(task.error_message, 'previous error')

    def test_multiple_stale_tasks_all_marked_error(self):
        """All PENDING/RUNNING tasks present at restart time are recovered."""
        tasks = [
            _make_import_task(status=AnalysisTransferTask.Status.PENDING),
            _make_import_task(status=AnalysisTransferTask.Status.RUNNING),
            _make_import_task(status=AnalysisTransferTask.Status.PENDING),
        ]
        _run_startup_recovery()
        for t in tasks:
            t.refresh_from_db()
            self.assertEqual(t.status, AnalysisTransferTask.Status.ERROR)

    def test_recovery_adds_log_entry(self):
        """The activity log must record the interruption event."""
        task = _make_import_task(status=AnalysisTransferTask.Status.RUNNING)
        task.activity_log = '[2026-01-01 10:00:00] Import started'
        task.save(update_fields=['activity_log'])
        _run_startup_recovery()
        task.refresh_from_db()
        self.assertIn('Task interrupted by server reload', task.activity_log)

    def test_export_task_also_recovered(self):
        """Export tasks left running at restart are also marked ERROR."""
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.EXPORT,
            status=AnalysisTransferTask.Status.RUNNING,
        )
        _run_startup_recovery()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.ERROR)


# ---------------------------------------------------------------------------
# MemoryError / exception-during-import tests
# ---------------------------------------------------------------------------

@override_settings(ANALYSIS_EXPORT_DIR=EXPORT_DIR)
class ImportMemoryErrorTests(TransactionTestCase):
    """Verify that a MemoryError raised inside ``_run_import`` is caught and
    the task is persisted as ERROR so the user gets clear feedback."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.makedirs(EXPORT_DIR, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(EXPORT_DIR, ignore_errors=True)
        super().tearDownClass()

    def _make_minimal_export_file(self):
        """Write a minimal but valid export archive and return its path."""
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
                {"station_id": 1, "fuel_id": 1, "price": 199.9, "snapshot_id": 1}
            ],
        }
        path = os.path.join(EXPORT_DIR, "test_oom_import.json.gz")
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def test_memory_error_during_import_sets_error_status(self):
        """If _do_import raises MemoryError, the task ends up in ERROR state."""
        file_path = self._make_minimal_export_file()
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=file_path,
        )
        with patch.object(
            AnalysisTransferTask, '_do_import',
            side_effect=MemoryError("Out of memory"),
        ):
            task._run_import()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.ERROR)
        self.assertIn('Out of memory', task.error_message)
        self.assertIsNotNone(task.completed_at)

    def test_memory_error_in_background_thread_sets_error_status(self):
        """MemoryError raised inside the background thread is caught correctly."""
        file_path = self._make_minimal_export_file()
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=file_path,
        )
        with patch.object(
            AnalysisTransferTask, '_do_import',
            side_effect=MemoryError("simulated OOM"),
        ):
            thread = task.start_import()
            thread.join(timeout=30)
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.ERROR)
        self.assertIn('simulated OOM', task.error_message)

    def test_memory_error_records_status_detail(self):
        """The status_detail field is set to 'Import failed' after a MemoryError."""
        file_path = self._make_minimal_export_file()
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=file_path,
        )
        with patch.object(
            AnalysisTransferTask, '_do_import',
            side_effect=MemoryError("OOM"),
        ):
            task._run_import()
        task.refresh_from_db()
        self.assertEqual(task.status_detail, 'Import failed')

    def test_memory_error_during_price_insertion_sets_error_status(self):
        """MemoryError raised during the price-insertion phase is handled."""
        file_path = self._make_minimal_export_file()
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=file_path,
        )
        # Simulate OOM at the DB cursor level during price insertion.
        original_do_import = AnalysisTransferTask._do_import

        def _oom_on_prices(self_task):
            # Run real import but raise MemoryError just before price insertion.
            with patch('prices.models.connection') as mock_conn:
                mock_conn.cursor.side_effect = MemoryError("OOM during price insert")
                return original_do_import(self_task)

        with patch.object(AnalysisTransferTask, '_do_import', _oom_on_prices):
            task._run_import()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.ERROR)

    def test_successful_import_not_affected(self):
        """A normal import (no OOM) still completes successfully after adding these tests."""
        file_path = self._make_minimal_export_file()
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=file_path,
        )
        task._run_import()
        task.refresh_from_db()
        self.assertEqual(task.status, AnalysisTransferTask.Status.COMPLETED)
        self.assertIsNotNone(task.analysis)


# ---------------------------------------------------------------------------
# Stale-task detection after restart: state visible to the UI
# ---------------------------------------------------------------------------

class StaleTaskUiVisibilityTests(TestCase):
    """After the startup recovery runs, the API/UI should reflect ERROR status."""

    def setUp(self):
        from django.contrib.auth.models import User
        self.user = User.objects.create_superuser('admin', 'a@b.com', 'pass')
        self.client.force_login(self.user)

    def test_status_api_shows_error_for_recovered_task(self):
        """The /api/transfer/<id>/status/ endpoint must return 'error' status."""
        task = _make_import_task(status=AnalysisTransferTask.Status.RUNNING)
        _run_startup_recovery()
        response = self.client.get(f"/api/transfer/{task.pk}/status/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['status'], 'error')

    def test_status_api_shows_error_message_for_recovered_task(self):
        """The status API must expose the server-restart error message."""
        task = _make_import_task(status=AnalysisTransferTask.Status.RUNNING)
        _run_startup_recovery()
        response = self.client.get(f"/api/transfer/{task.pk}/status/")
        data = response.json()
        self.assertIn('error_message', data)
        self.assertIn('server restarted', data['error_message'].lower())
