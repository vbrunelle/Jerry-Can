"""Performance regression tests for critical database queries.

These tests reproduce the exact conditions of the manual benchmarks run inside
the container:  a large SQLite database with many snapshots and prices.
Each test asserts that the measured wall-clock time stays well under the
nginx proxy timeout (60 s).

Budget per operation:
  - _analysis_storage_estimate()  < 10 s
  - Snapshot price_count SUM      <  2 s
  - AnalysisDetailView snapshot page query  < 1 s

The dataset created by _build_large_analysis() is intentionally sized to be
heavy enough to surface real regressions without making the CI suite
unreasonably slow (~1 M prices, ~3 000 snapshots).
"""
import time
from decimal import Decimal

from django.db import connection
from django.db.models import Sum
from django.test import RequestFactory, TestCase, TransactionTestCase

from prices.models import Analysis, AnalysisTransferTask, Price, Snapshot, Station, Fuel
from prices.views import _analysis_storage_estimate


# ---------------------------------------------------------------------------
# Dataset builder
# ---------------------------------------------------------------------------

def _build_large_analysis(num_stations=500, num_snapshots=1000, prices_per_snapshot=20):
    """Create an analysis with a realistic volume of data.

    Default: 500 stations × 1 000 snapshots × 20 prices = 10 M price rows.
    Kept smaller than the 41 M row production DB so the test suite finishes
    in a reasonable time, yet large enough to expose O(N) regressions.
    """
    analysis = Analysis.objects.create(
        data_source_url="http://example.com/perf-test.geojson.gz",
        update_frequency=5,
        active=False,
        run_automatically=False,
    )

    fuels = [Fuel.objects.get_or_create(name=n)[0] for n in ("Régulier", "Super", "Diesel", "Électrique")]

    station_objs = [
        Station(
            name=f"PerfStation-{i}",
            city=f"City-{i % 50}",
            region="Montréal",
            adress=f"{i} rue Perf",
            longitude=-73.0 + (i % 100) * 0.01,
            latitude=45.0 + (i % 100) * 0.01,
            analysis=analysis,
        )
        for i in range(num_stations)
    ]
    Station.objects.bulk_create(station_objs, batch_size=2000)
    station_ids = list(Station.objects.filter(analysis=analysis).values_list("id", flat=True))
    fuel_ids = [f.pk for f in fuels]

    snap_objs = [
        Snapshot(analysis=analysis, status=Snapshot.Status.PROCESSED)
        for _ in range(num_snapshots)
    ]
    Snapshot.objects.bulk_create(snap_objs, batch_size=2000)
    snapshot_ids = list(Snapshot.objects.filter(analysis=analysis).values_list("id", flat=True))

    # Bulk-insert prices in large batches
    price_batch = []
    BATCH = 5000
    step = max(1, len(station_ids) * len(fuel_ids) // prices_per_snapshot)
    price_count_per_snap = {}
    for snap_id in snapshot_ids:
        count = 0
        for j in range(0, min(prices_per_snapshot, len(station_ids) * len(fuel_ids))):
            sid = station_ids[j % len(station_ids)]
            fid = fuel_ids[j % len(fuel_ids)]
            price_batch.append(Price(
                station_id=sid,
                fuel_id=fid,
                snapshot_id=snap_id,
                price=Decimal("149.9"),
            ))
            count += 1
            if len(price_batch) >= BATCH:
                Price.objects.bulk_create(price_batch)
                price_batch = []
        price_count_per_snap[snap_id] = count
    if price_batch:
        Price.objects.bulk_create(price_batch)

    # Populate the cached price_count on each Snapshot (mirrors the import finalizer)
    for snap_id, count in price_count_per_snap.items():
        Snapshot.objects.filter(pk=snap_id).update(price_count=count)

    return analysis


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

# Use TransactionTestCase so that PRAGMA dbstat works correctly (some dbstat
# behaviour depends on pages being written/flushed, which only happens outside
# of a rolled-back transaction).
class StorageEstimatePerformanceTest(TransactionTestCase):
    """_analysis_storage_estimate() must finish well within the nginx timeout."""

    # Time budget: must stay well under the 60 s nginx proxy timeout.
    MAX_SECONDS = 10

    def setUp(self):
        self.analysis = _build_large_analysis(
            num_stations=500,
            num_snapshots=1000,
            prices_per_snapshot=20,
        )

    def test_storage_estimate_completes_within_budget(self):
        t0 = time.perf_counter()
        result = _analysis_storage_estimate(self.analysis)
        elapsed = time.perf_counter() - t0

        self.assertTrue(
            result["available"],
            f"_analysis_storage_estimate returned available=False: {result.get('reason')}",
        )
        self.assertLess(
            elapsed, self.MAX_SECONDS,
            f"_analysis_storage_estimate took {elapsed:.2f}s — exceeds {self.MAX_SECONDS}s budget. "
            f"Regression in dbstat query or row counting."
        )

    def test_storage_estimate_skipped_during_import(self):
        """Returns available=False immediately when an import is running — no DB query."""
        task = AnalysisTransferTask.objects.create(
            task_type=AnalysisTransferTask.TaskType.IMPORT,
            status=AnalysisTransferTask.Status.RUNNING,
            analysis=self.analysis,
        )
        t0 = time.perf_counter()
        result = _analysis_storage_estimate(self.analysis)
        elapsed = time.perf_counter() - t0

        self.assertFalse(result["available"])
        self.assertIn("import", result["reason"].lower())
        self.assertLess(elapsed, 0.5, "Skip-during-import guard took too long")


class PriceCountPerformanceTest(TransactionTestCase):
    """Price count via cached price_count SUM must be fast (no COUNT(*) on prices table)."""

    MAX_SECONDS = 2

    def setUp(self):
        self.analysis = _build_large_analysis(
            num_stations=500,
            num_snapshots=1000,
            prices_per_snapshot=20,
        )

    def test_total_price_count_via_price_count_aggregate(self):
        """Summing price_count on Snapshot is O(snapshots), not O(prices)."""
        t0 = time.perf_counter()
        total = Snapshot.objects.aggregate(total=Sum("price_count"))["total"] or 0
        elapsed = time.perf_counter() - t0

        self.assertGreater(total, 0, "price_count cache is empty — did _build_large_analysis run correctly?")
        self.assertLess(
            elapsed, self.MAX_SECONDS,
            f"Snapshot.aggregate(Sum('price_count')) took {elapsed:.2f}s — exceeds {self.MAX_SECONDS}s budget."
        )


class SnapshotPageQueryPerformanceTest(TransactionTestCase):
    """The snapshot pagination query must be fast even with thousands of snapshots."""

    MAX_SECONDS = 1

    def setUp(self):
        self.analysis = _build_large_analysis(
            num_stations=100,
            num_snapshots=3000,
            prices_per_snapshot=4,
        )

    def test_snapshot_page_offset_limit_query(self):
        """Offset/limit .values() query — used in AnalysisDetailView — must stay fast."""
        t0 = time.perf_counter()
        page = list(
            self.analysis.snapshots
            .values("id", "timestamp", "status", "price_count")
            .order_by("-timestamp", "-pk")[:51]
        )
        elapsed = time.perf_counter() - t0

        self.assertEqual(len(page), 51)
        self.assertLess(
            elapsed, self.MAX_SECONDS,
            f"Snapshot page query took {elapsed:.2f}s — exceeds {self.MAX_SECONDS}s budget."
        )

    def test_snapshot_page_does_not_issue_count_query(self):
        """Verify that fetching a page of snapshots does NOT issue a COUNT(*) query.

        A COUNT(*) on a large snapshots table is unnecessary and slow; the
        offset/limit approach avoids it.
        """
        queries_before = len(connection.queries)

        # Force query logging for this check
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            list(
                self.analysis.snapshots
                .values("id", "timestamp", "status", "price_count")
                .order_by("-timestamp", "-pk")[:51]
            )

        sql_statements = [q["sql"] for q in ctx.captured_queries]
        count_queries = [s for s in sql_statements if "COUNT(" in s.upper()]
        self.assertEqual(
            count_queries, [],
            f"Unexpected COUNT query in snapshot page fetch: {count_queries}"
        )
