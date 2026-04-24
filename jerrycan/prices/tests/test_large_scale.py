"""Large-scale TDD tests.

These tests define the requirements. Write them first; fix the implementation
until they all pass.

Target dataset
--------------
  4 000 stations × 4 fuel types × 2 500 snapshots (distinct 5-minute timestamps)
  = 40 000 000 price records, representative of the production database.

Requirements (one test per bullet)
-----------------------------------
1. The analysis detail page returns HTTP 200 regardless of whether an import
   or export task is currently running on that analysis.
2. A full import of ~40M prices must complete within IMPORT_TIME_BUDGET_SECONDS.
3. Every imported snapshot has a distinct timestamp.
4. The analysis detail page (4 000 stations, 3 000 snapshots) loads in under
   PAGE_LOAD_BUDGET_SECONDS even with a running import task.
"""
import gzip
import json
import os
import re
import shutil
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from datetime import timezone as dt_tz

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from prices.models import (
    Analysis,
    AnalysisTransferTask,
    Price,
    Snapshot,
    Station,
)

# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

# A correct streaming implementation should finish in minutes; 30 min is a
# generous CI budget that still catches catastrophic regressions.
IMPORT_TIME_BUDGET_SECONDS = 1800

# The nginx default proxy timeout is 60 s. The analysis page must respond
# well within it; 5 s leaves a 12× safety margin.
PAGE_LOAD_BUDGET_SECONDS = 5.0

# ---------------------------------------------------------------------------
# Module-level temp directory shared between test classes
# ---------------------------------------------------------------------------

_EXPORT_DIR = tempfile.mkdtemp(prefix="jerrycan_large_scale_")
_LARGE_EXPORT_PATH = os.path.join(_EXPORT_DIR, "large_40m.json.gz")


# ---------------------------------------------------------------------------
# Export-file builder (bypasses ORM — writes JSON directly for speed)
# ---------------------------------------------------------------------------

def _write_large_export_file(
    path,
    num_stations=4000,
    num_fuels=4,
    num_snapshots=2500,
):
    """Write a production-scale gzipped export file directly to disk.

    Generates exactly num_stations × num_fuels × num_snapshots price records
    without inserting a single row into the database, so that data generation
    does not dominate test setup time.

    Each snapshot receives a unique timestamp at 5-minute intervals starting
    from 2024-01-01 00:00 UTC.
    """
    fuel_names = ["Régulier", "Super", "Diesel", "Électrique"]
    fuels = [{"id": i + 1, "name": fuel_names[i]} for i in range(num_fuels)]

    stations = [
        {
            "id": i + 1,
            "name": f"Station-{i}",
            "city": f"City-{i % 200}",
            "region": "Montréal",
            "adress": f"{i} rue Test",
            "longitude": round(-73.0 + (i % 100) * 0.01, 4),
            "latitude": round(45.0 + (i % 100) * 0.01, 4),
        }
        for i in range(num_stations)
    ]

    base_ts = datetime(2024, 1, 1, tzinfo=dt_tz.utc)
    snapshots = [
        {
            "id": i + 1,
            "timestamp": (base_ts + timedelta(minutes=5 * i)).isoformat(),
            "status": "processed",
        }
        for i in range(num_snapshots)
    ]

    analysis_meta = {
        "update_frequency": 5,
        "data_source_url": "http://test.example.com/data.geojson.gz",
        "active": False,
        "run_automatically": False,
        "max_snapshots": 100,
    }

    with gzip.open(path, "wt", encoding="utf-8", compresslevel=1) as f:
        # Header
        f.write('{"version":1,')
        f.write('"analysis":')
        json.dump(analysis_meta, f)
        f.write(',"fuels":')
        json.dump(fuels, f)
        f.write(',"stations":')
        json.dump(stations, f)
        f.write(',"snapshots":')
        json.dump(snapshots, f)

        # Prices — streamed one record at a time to keep memory flat
        f.write(',"prices":[')
        first = True
        for snap_id in range(1, num_snapshots + 1):
            for station_id in range(1, num_stations + 1):
                for fuel_id in range(1, num_fuels + 1):
                    if not first:
                        f.write(",")
                    # Inline f-string is faster than json.dumps for fixed schema
                    f.write(
                        f'{{"station_id":{station_id},'
                        f'"fuel_id":{fuel_id},'
                        f'"price":149.9,'
                        f'"snapshot_id":{snap_id}}}'
                    )
                    first = False

        f.write("]}")


# ---------------------------------------------------------------------------
# In-DB dataset builder (for page-load tests — no actual Price rows needed)
# ---------------------------------------------------------------------------

def _build_analysis_in_db(num_stations=4000, num_snapshots=3000):
    """Create an Analysis with stations and snapshots directly in the DB.

    Price rows are intentionally omitted: the analysis detail page only reads
    snapshots for pagination, and uses the cached price_count field.
    Each snapshot has a distinct timestamp (5-minute intervals) and a
    realistic cached price_count (4 000 stations × 4 fuels = 16 000).
    """
    analysis = Analysis.objects.create(
        data_source_url="http://example.com/page-load-test.geojson.gz",
        update_frequency=5,
        active=False,
        run_automatically=False,
    )

    Station.objects.bulk_create(
        [
            Station(
                name=f"Station-{i}",
                city=f"City-{i % 200}",
                region="Montréal",
                adress=f"{i} rue Test",
                longitude=round(-73.0 + (i % 100) * 0.01, 4),
                latitude=round(45.0 + (i % 100) * 0.01, 4),
                analysis=analysis,
            )
            for i in range(num_stations)
        ],
        batch_size=2000,
    )

    base_ts = timezone.now()
    Snapshot.objects.bulk_create(
        [
            Snapshot(
                analysis=analysis,
                status=Snapshot.Status.PROCESSED,
                timestamp=base_ts - timedelta(minutes=5 * i),
                price_count=num_stations * 4,  # cached, no actual Price rows
            )
            for i in range(num_snapshots)
        ],
        batch_size=2000,
    )

    return analysis


# ===========================================================================
# Shared infrastructure for nginx integration tests
# ===========================================================================
#
# WHY NOT DJANGO TestClient?
# --------------------------
# Django's TestClient short-circuits the entire network stack: it calls the
# WSGI application in-process, uses an in-memory SQLite database, and never
# goes through nginx.  This means:
#
#   • The 60-second nginx proxy_read_timeout is never hit.
#   • The real on-disk SQLite file (with millions of rows) is never touched.
#   • "PRAGMA dbstat" — the slow query that causes the 504 — runs in ~0 ms on
#     an empty in-memory database instead of ~62 s on the production file.
#
# Tests written with TestClient would always report 200 < 5 s regardless of
# any performance regression in the view, giving false confidence (green when
# broken in production).
#
# THE REAL STACK
# --------------
# These tests exercise the complete production request chain:
#
#   test process
#     └─▶ urllib HTTP GET  (real TCP, same machine)
#           └─▶ nginx  (proxy_read_timeout 60 s)
#                 └─▶ gunicorn  (1 worker, 4 threads, timeout 120 s)
#                       └─▶ Django view
#                             └─▶ SQLite on-disk file  (the large DB)
#
# The only 504 the browser shows is reproduced here.
#
# DB STATE MANIPULATION
# ---------------------
# The test process cannot reach Django's ORM directly (different process).
# Instead, `_docker_exec_python()` runs short Python snippets inside the
# web container via `docker exec … manage.py shell -c "…"`.  Each snippet
# commits before the process exits, so the next HTTP request sees the change.
#
# HOW TO RUN
# ----------
# 1. Start the second Docker Compose stack:
#      docker compose -f docker-compose.second-instance.yml up -d
# 2. Export credentials (or put them in .env.test and source it):
#      export JERRYCAN_TEST_URL=http://localhost:8081
#      export JERRYCAN_TEST_CONTAINER=jerry-can-second-web-1
#      export JERRYCAN_TEST_USERNAME=<staff user>
#      export JERRYCAN_TEST_PASSWORD=<password>
# 3. Run the tests:
#      cd jerrycan
#      python manage.py test \
#        prices.tests.test_large_scale.AnalysisPageAccessDuringTransferTest \
#        prices.tests.test_large_scale.AnalysisDetailPageLoadTest \
#        --verbosity=2
#
# Required environment variables:
#   JERRYCAN_TEST_URL        — base URL of the running stack (default: http://localhost:8081)
#   JERRYCAN_TEST_CONTAINER  — Docker container name used for docker exec
#   JERRYCAN_TEST_USERNAME   — login username (must be a staff user)
#   JERRYCAN_TEST_PASSWORD   — login password
DOCKER_BASE_URL = os.environ.get("JERRYCAN_TEST_URL", "http://localhost:8081")
DOCKER_CONTAINER = os.environ.get("JERRYCAN_TEST_CONTAINER", "jerry-can-second-web-1")


class _NginxIntegrationTest(unittest.TestCase):
    """Base class for integration tests hitting the real nginx → gunicorn → Django stack.

    Handles session auth (form login with CSRF) and provides helpers for
    authenticated HTTP requests, analysis pk discovery, and DB state
    manipulation via docker exec.
    """

    def setUp(self):
        username = os.environ.get("JERRYCAN_TEST_USERNAME")
        password = os.environ.get("JERRYCAN_TEST_PASSWORD")
        if not username or not password:
            self.skipTest(
                "Set JERRYCAN_TEST_USERNAME and JERRYCAN_TEST_PASSWORD "
                "env vars to run integration tests against the Docker stack."
            )
        self.cookie_jar = urllib.request.HTTPCookieProcessor()
        self.opener = urllib.request.build_opener(self.cookie_jar)
        self._login(username, password)
        self._analysis_pk = self._find_analysis_pk()

    def _login(self, username, password):
        """POST credentials to the Django login form and store the session cookie."""
        login_url = f"{DOCKER_BASE_URL}/accounts/login/"
        with self.opener.open(login_url, timeout=10) as resp:
            body = resp.read().decode()
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
        if not m:
            self.fail("Could not find CSRF token on login page.")
        csrf = m.group(1)
        data = urllib.parse.urlencode({
            "csrfmiddlewaretoken": csrf,
            "username": username,
            "password": password,
            "next": "/analyses/",
        }).encode()
        req = urllib.request.Request(
            login_url, data=data,
            headers={"Referer": login_url, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with self.opener.open(req, timeout=10) as resp:
            final_url = resp.url
        if "/accounts/login/" in final_url:
            self.fail(f"Login failed for user '{username}'. Check credentials.")

    def _fetch(self, path, timeout=PAGE_LOAD_BUDGET_SECONDS + 2):
        """Authenticated GET through nginx. Returns (status_code, elapsed_seconds)."""
        url = f"{DOCKER_BASE_URL}{path}"
        t0 = time.perf_counter()
        try:
            with self.opener.open(url, timeout=timeout) as resp:
                resp.read()
                return resp.status, time.perf_counter() - t0
        except urllib.error.HTTPError as e:
            return e.code, time.perf_counter() - t0
        except Exception:
            return 0, time.perf_counter() - t0

    def _find_analysis_pk(self):
        """Scrape /analyses/ list (fast — no dbstat) and return the first pk."""
        url = f"{DOCKER_BASE_URL}/analyses/"
        try:
            with self.opener.open(url, timeout=10) as resp:
                if resp.status != 200:
                    self.skipTest(
                        f"Could not reach {url} (status {resp.status}). "
                        "Start the Docker Compose stack before running this test."
                    )
                body = resp.read().decode()
        except Exception as exc:
            self.skipTest(f"Could not reach {url}: {exc}")
        pks = [int(m) for m in re.findall(r'/analyses/(\d+)/', body)]
        if not pks:
            self.skipTest(
                f"No analyses found at {url}. "
                "Import data into the running stack before running this test."
            )
        return pks[0]

    def _docker_exec_python(self, code):
        """Run a Python snippet in the web container via docker exec."""
        import subprocess
        result = subprocess.run(
            ["docker", "exec", DOCKER_CONTAINER, "python", "manage.py", "shell", "-c", code],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            self.fail(
                f"docker exec failed (exit {result.returncode}):\n{result.stderr}"
            )
        return result.stdout


# ===========================================================================
# Test class 1 — Analysis page accessible during active import / export
# ===========================================================================

class AnalysisPageAccessDuringTransferTest(_NginxIntegrationTest):
    """The analysis detail page must respond in under PAGE_LOAD_BUDGET_SECONDS
    through nginx in every task state.

    State is set up in the container's DB via docker exec so that the real
    dbstat cost (on the large on-disk SQLite file) is incurred on every request.

    Expected RED with current code (dbstat fires → 60s → 504):
      - no task
      - pending import  (guard only skips when status=RUNNING)
      - running export  (guard only skips for task_type=IMPORT)
      - pending export  (guard only skips for task_type=IMPORT)

    Expected GREEN with current code (guard skips dbstat):
      - running import
      - concurrent running import + export
    """

    def setUp(self):
        super().setUp()
        self._cleanup_tasks()

    def tearDown(self):
        self._cleanup_tasks()

    def _cleanup_tasks(self):
        try:
            self._docker_exec_python(
                f"from prices.models import AnalysisTransferTask\n"
                f"AnalysisTransferTask.objects.filter(analysis_id={self._analysis_pk}).delete()"
            )
        except Exception:
            pass

    def _create_task(self, task_type, status):
        self._docker_exec_python(
            f"from prices.models import AnalysisTransferTask\n"
            f"AnalysisTransferTask.objects.create("
            f"analysis_id={self._analysis_pk},"
            f"task_type='{task_type}',"
            f"status='{status}',"
            f")"
        )

    def _assert_page_accessible(self):
        """Assert /analyses/<pk>/ responds 200 in under PAGE_LOAD_BUDGET_SECONDS."""
        status, elapsed = self._fetch(f"/analyses/{self._analysis_pk}/", timeout=75)
        self.assertNotEqual(
            status, 504,
            f"nginx returned 504 for /analyses/{self._analysis_pk}/ after {elapsed:.1f}s — "
            f"_analysis_storage_estimate is blocking the HTTP request.",
        )
        self.assertEqual(status, 200, f"Unexpected status {status} for /analyses/{self._analysis_pk}/")
        self.assertLess(
            elapsed,
            PAGE_LOAD_BUDGET_SECONDS,
            f"/analyses/{self._analysis_pk}/ took {elapsed:.3f}s through nginx — "
            f"exceeds {PAGE_LOAD_BUDGET_SECONDS}s budget.",
        )

    # --- baseline -----------------------------------------------------------

    def test_page_accessible_with_no_task(self):
        """No task → dbstat fires on large DB → 504. Should be: < 5s."""
        self._assert_page_accessible()

    # --- import states ------------------------------------------------------

    def test_page_accessible_during_running_import(self):
        """Running import → guard skips dbstat → fast. Expected: 200 < 5s."""
        self._create_task('import', 'running')
        self._assert_page_accessible()

    def test_page_accessible_during_pending_import(self):
        """Pending import → guard only covers RUNNING → dbstat fires → 504."""
        self._create_task('import', 'pending')
        self._assert_page_accessible()

    # --- export states ------------------------------------------------------

    def test_page_accessible_during_running_export(self):
        """Running export → guard only skips IMPORT type → dbstat fires → 504."""
        self._create_task('export', 'running')
        self._assert_page_accessible()

    def test_page_accessible_during_pending_export(self):
        """Pending export → guard only skips IMPORT type → dbstat fires → 504."""
        self._create_task('export', 'pending')
        self._assert_page_accessible()

    # --- concurrent import + export -----------------------------------------

    def test_page_accessible_with_concurrent_import_and_export(self):
        """Concurrent running import + export → guard fires → fast. Expected: 200 < 5s."""
        self._create_task('import', 'running')
        self._create_task('export', 'running')
        self._assert_page_accessible()


# ===========================================================================
# Test class 2 — Import of ~40M prices
# ===========================================================================

@override_settings(ANALYSIS_EXPORT_DIR=_EXPORT_DIR)
class LargeImportTest(TransactionTestCase):
    """Requirements for importing a production-scale dataset.

    4 000 stations × 4 fuels × 2 500 snapshots = 40 000 000 price records.

    The export file is generated once in setUpClass (file I/O only, no DB).
    Each test runs the import independently (TransactionTestCase flushes
    between tests).

    TDD note: if _do_import() loads the entire JSON into memory, these tests
    will fail with MemoryError or timeout — that is the expected red phase.
    The fix is to make _do_import() stream the price records rather than
    calling json.load() on the full file.
    """

    NUM_STATIONS = 4000
    NUM_FUELS = 4
    NUM_SNAPSHOTS = 2500  # 4000 × 4 × 2500 = 40 000 000 prices

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.makedirs(_EXPORT_DIR, exist_ok=True)
        if not os.path.exists(_LARGE_EXPORT_PATH):
            _write_large_export_file(
                _LARGE_EXPORT_PATH,
                num_stations=cls.NUM_STATIONS,
                num_fuels=cls.NUM_FUELS,
                num_snapshots=cls.NUM_SNAPSHOTS,
            )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(_EXPORT_DIR, ignore_errors=True)
        super().tearDownClass()

    def _run_import(self):
        """Create and run an import task synchronously; return (task, elapsed)."""
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            file_path=_LARGE_EXPORT_PATH,
        )
        t0 = time.perf_counter()
        thread = task.start_import()
        thread.join(timeout=IMPORT_TIME_BUDGET_SECONDS)
        elapsed = time.perf_counter() - t0
        task.refresh_from_db()
        return task, elapsed

    # --- functional correctness ---------------------------------------------

    def test_import_completes_with_correct_station_count(self):
        task, elapsed = self._run_import()
        self.assertEqual(
            task.status,
            AnalysisTransferTask.Status.COMPLETED,
            f"Import failed after {elapsed:.0f}s: {task.error_message}",
        )
        count = Station.objects.filter(analysis=task.analysis).count()
        self.assertEqual(count, self.NUM_STATIONS)

    def test_import_completes_with_correct_snapshot_count(self):
        task, elapsed = self._run_import()
        self.assertEqual(
            task.status,
            AnalysisTransferTask.Status.COMPLETED,
            f"Import failed after {elapsed:.0f}s: {task.error_message}",
        )
        count = Snapshot.objects.filter(analysis=task.analysis).count()
        self.assertEqual(count, self.NUM_SNAPSHOTS)

    def test_import_completes_with_correct_price_count(self):
        task, elapsed = self._run_import()
        self.assertEqual(
            task.status,
            AnalysisTransferTask.Status.COMPLETED,
            f"Import failed after {elapsed:.0f}s: {task.error_message}",
        )
        count = Price.objects.filter(snapshot__analysis=task.analysis).count()
        self.assertEqual(
            count,
            self.NUM_STATIONS * self.NUM_FUELS * self.NUM_SNAPSHOTS,
        )

    def test_all_snapshot_timestamps_are_distinct(self):
        """Every imported snapshot must have a unique timestamp."""
        task, elapsed = self._run_import()
        self.assertEqual(
            task.status,
            AnalysisTransferTask.Status.COMPLETED,
            f"Import failed after {elapsed:.0f}s: {task.error_message}",
        )
        analysis = task.analysis
        total = Snapshot.objects.filter(analysis=analysis).count()
        distinct = (
            Snapshot.objects.filter(analysis=analysis)
            .values("timestamp")
            .distinct()
            .count()
        )
        self.assertEqual(
            total,
            distinct,
            f"{total - distinct} duplicate timestamps found among {total} snapshots.",
        )

    def test_snapshot_price_counts_are_cached_after_import(self):
        """price_count on every snapshot must be populated after import."""
        task, elapsed = self._run_import()
        self.assertEqual(
            task.status,
            AnalysisTransferTask.Status.COMPLETED,
            f"Import failed after {elapsed:.0f}s: {task.error_message}",
        )
        zero_count = (
            Snapshot.objects.filter(analysis=task.analysis, price_count=0).count()
        )
        self.assertEqual(
            zero_count,
            0,
            f"{zero_count} snapshots have price_count=0 after import.",
        )

    # --- performance --------------------------------------------------------

    def test_import_completes_within_time_budget(self):
        """Import of ~40M prices must finish within IMPORT_TIME_BUDGET_SECONDS."""
        task, elapsed = self._run_import()
        self.assertEqual(
            task.status,
            AnalysisTransferTask.Status.COMPLETED,
            f"Import did not complete (status={task.status}) after {elapsed:.0f}s. "
            f"Budget is {IMPORT_TIME_BUDGET_SECONDS}s.\n{task.error_message}",
        )
        self.assertLess(
            elapsed,
            IMPORT_TIME_BUDGET_SECONDS,
            f"Import took {elapsed:.0f}s — exceeds {IMPORT_TIME_BUDGET_SECONDS}s budget.",
        )


# ===========================================================================
# Test class 3 — Analysis detail page response time (integration, via nginx)
# ===========================================================================

class AnalysisDetailPageLoadTest(_NginxIntegrationTest):
    """The analysis detail page must respond in under PAGE_LOAD_BUDGET_SECONDS
    through the real nginx → gunicorn → Django stack.

    Requires:
    - Docker Compose stack running at JERRYCAN_TEST_URL (default localhost:8081)
    - JERRYCAN_TEST_USERNAME and JERRYCAN_TEST_PASSWORD env vars set
    - At least one analysis with a large dataset in the running DB

    Will FAIL with the current code: _analysis_storage_estimate runs
    synchronously in the request → dbstat on the large SQLite file takes
    60s+ → nginx returns 504 before Django responds.
    """

    def test_page_loads_within_budget(self):
        """GET /analyses/<pk>/ must respond in < 5s through nginx.

        Will FAIL with the current code: _analysis_storage_estimate is
        called synchronously → dbstat on the large SQLite file takes
        60s+ → nginx returns 504 before Django responds.
        """
        pk = self._analysis_pk
        # Timeout must exceed nginx's proxy_read_timeout (60s) so we actually
        # receive the 504 instead of getting a local socket timeout.
        status, elapsed = self._fetch(f"/analyses/{pk}/", timeout=75)
        self.assertNotEqual(
            status, 504,
            f"nginx returned 504 for /analyses/{pk}/ after {elapsed:.1f}s — "
            f"_analysis_storage_estimate is blocking the HTTP request.",
        )
        self.assertEqual(status, 200, f"Unexpected status {status} for /analyses/{pk}/")
        self.assertLess(
            elapsed,
            PAGE_LOAD_BUDGET_SECONDS,
            f"/analyses/{pk}/ took {elapsed:.3f}s through nginx — "
            f"exceeds {PAGE_LOAD_BUDGET_SECONDS}s budget.",
        )

        """GET /analyses/<pk>/ must respond in < 5s through nginx.

        Will FAIL with the current code: _analysis_storage_estimate is
        called synchronously → dbstat on the large SQLite file takes
        60s+ → nginx returns 504 before Django responds.
        """
        pk = self._find_analysis_pk()
        # Timeout must exceed nginx's proxy_read_timeout (60s) so we actually
        # receive the 504 instead of getting a local socket timeout.
        status, elapsed = self._fetch(f"/analyses/{pk}/", timeout=75)
        self.assertNotEqual(
            status, 504,
            f"nginx returned 504 for /analyses/{pk}/ after {elapsed:.1f}s — "
            f"_analysis_storage_estimate is blocking the HTTP request.",
        )
        self.assertEqual(status, 200, f"Unexpected status {status} for /analyses/{pk}/")
        self.assertLess(
            elapsed,
            PAGE_LOAD_BUDGET_SECONDS,
            f"/analyses/{pk}/ took {elapsed:.3f}s through nginx — "
            f"exceeds {PAGE_LOAD_BUDGET_SECONDS}s budget.",
        )
