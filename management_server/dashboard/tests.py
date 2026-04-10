import csv
import os
import shutil
import tempfile
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pandas as pd

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from dashboard.models import DownloadRequest, InspectionCache, SiteConfiguration
from dashboard.services import (
    _generate_csv_from_hudi,
    cleanup_expired_downloads,
    generate_csv_for_user,
    get_current_prices,
    get_inspection_data,
    get_snapshot_dates,
    get_snapshots_for_date,
    refresh_inspection_cache,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Sample data returned by the mocked Hudi helpers
# ---------------------------------------------------------------------------
_SAMPLE_SNAPSHOT_DATES = ["2024-01-02", "2024-01-01"]

_SAMPLE_SNAPSHOTS_JAN02 = [
    {"fetched_at": "2024-01-02 10:00:00", "record_count": 2},
]

_SAMPLE_SNAPSHOTS_JAN01 = [
    {"fetched_at": "2024-01-01 10:00:00", "record_count": 2},
]

_SAMPLE_CURRENT_PRICES = (
    [
        {"station_name": "Station A", "city": "Montréal", "region": "Québec", "fuel_type": "regular", "price": 1.58},
        {"station_name": "Station B", "city": "Québec", "region": "Québec", "fuel_type": "regular", "price": 1.57},
    ],
    "2024-01-02 10:00:00",
)

_SAMPLE_INSPECTION_DATA = {
    "summary": {
        "station_count": 2,
        "price_count": 4,
        "snapshot_count": 2,
        "first_snapshot": "2024-01-01 10:00:00",
        "last_snapshot": "2024-01-02 10:00:00",
    },
    "regions": [
        {"region": "Québec", "station_count": 2, "avg_prices": {"regular": 1.575}},
    ],
    "latest_prices": [
        {"station_name": "Station B", "city": "Québec", "region": "Québec", "fuel_type": "regular", "price": 1.57},
        {"station_name": "Station A", "city": "Montréal", "region": "Québec", "fuel_type": "regular", "price": 1.58},
    ],
    "snapshots": [
        {"fetched_at": "2024-01-02 10:00:00", "record_count": 2, "changes": 2},
        {"fetched_at": "2024-01-01 10:00:00", "record_count": 2, "changes": 2},
    ],
    "price_variations": {
        "increases": [{"station_name": "Station A", "city": "Montréal", "fuel_type": "regular", "prev_price": 1.5, "price": 1.6, "delta": 0.1}],
        "decreases": [],
    },
}


# ---------------------------------------------------------------------------
# Service tests — get_inspection_data
# ---------------------------------------------------------------------------
class GetInspectionDataTests(TestCase):
    """Tests for get_inspection_data service."""

    def test_returns_empty_data_when_hudi_dir_does_not_exist(self):
        with patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi"):
            data = get_inspection_data()
        self.assertEqual(data["summary"]["station_count"], 0)
        self.assertEqual(data["summary"]["price_count"], 0)
        self.assertEqual(data["regions"], [])
        self.assertEqual(data["latest_prices"], [])
        self.assertEqual(data["snapshots"], [])

    @patch("dashboard.services.os.path.isdir", return_value=True)
    @patch("price_history.get_inspection_data", return_value=_SAMPLE_INSPECTION_DATA)
    def test_returns_proper_structure_with_data(self, _mock_insp, _mock_isdir):
        data = get_inspection_data()

        self.assertEqual(data["summary"]["station_count"], 2)
        self.assertEqual(data["summary"]["price_count"], 4)
        self.assertIsNotNone(data["summary"]["first_snapshot"])
        self.assertIsNotNone(data["summary"]["last_snapshot"])
        self.assertGreater(len(data["regions"]), 0)
        self.assertGreater(len(data["latest_prices"]), 0)
        self.assertGreater(len(data["snapshots"]), 0)
        self.assertIn("increases", data["price_variations"])
        self.assertIn("decreases", data["price_variations"])


# ---------------------------------------------------------------------------
# Service tests — refresh_inspection_cache
# ---------------------------------------------------------------------------
class RefreshInspectionCacheTests(TestCase):
    """Tests for refresh_inspection_cache service."""

    def test_creates_inspection_cache_record(self):
        with patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi"):
            refresh_inspection_cache()
        self.assertEqual(InspectionCache.objects.count(), 1)
        cache = InspectionCache.objects.first()
        self.assertIn("summary", cache.data)

    def test_keeps_only_latest_cache_entry(self):
        with patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi"):
            refresh_inspection_cache()
            refresh_inspection_cache()
        self.assertEqual(InspectionCache.objects.count(), 1)


# ---------------------------------------------------------------------------
# Service tests — cleanup_expired_downloads
# ---------------------------------------------------------------------------
class CleanupExpiredDownloadsTests(TestCase):
    """Tests for cleanup_expired_downloads service."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", password="testpass123"
        )

    def test_marks_old_requests_as_expired(self):
        dr = DownloadRequest.objects.create(user=self.user, status="ready")
        # Backdate created_at to more than 24 hours ago
        DownloadRequest.objects.filter(pk=dr.pk).update(
            created_at=timezone.now() - timedelta(hours=25)
        )
        cleanup_expired_downloads()
        dr.refresh_from_db()
        self.assertEqual(dr.status, "expired")

    def test_does_not_expire_recent_requests(self):
        dr = DownloadRequest.objects.create(user=self.user, status="ready")
        cleanup_expired_downloads()
        dr.refresh_from_db()
        self.assertEqual(dr.status, "ready")


# ---------------------------------------------------------------------------
# Service tests — _generate_csv_from_hudi
# ---------------------------------------------------------------------------
_SAMPLE_CSV_DF = pd.DataFrame([
    {
        "region": "Québec",
        "city": "Montréal",
        "station_name": "Station A",
        "fuel_type": "regular",
        "fetched_at": "2024-01-02 10:00:00",
        "price": 1.58,
    },
])


class GenerateCsvFromHudiTests(TestCase):
    """Tests for _generate_csv_from_hudi — exercises the real import path."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("price_history.build_historicized_changes_pandas", return_value=_SAMPLE_CSV_DF)
    def test_writes_csv_file(self, _mock_build):
        file_path = os.path.join(self.tmp_dir, "test.csv")
        _generate_csv_from_hudi(file_path)
        self.assertTrue(os.path.exists(file_path))
        with open(file_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["station_name"], "Station A")

    @patch("price_history.build_historicized_changes_pandas", return_value=pd.DataFrame())
    def test_raises_when_no_data(self, _mock_build):
        file_path = os.path.join(self.tmp_dir, "test.csv")
        with self.assertRaises(ValueError):
            _generate_csv_from_hudi(file_path)


# ---------------------------------------------------------------------------
# Service tests — generate_csv_for_user
# ---------------------------------------------------------------------------
class GenerateCsvForUserTests(TestCase):
    """Tests for generate_csv_for_user service."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="testuser", password="testpass123"
        )
        self.download_dir = tempfile.mkdtemp(dir=settings.BASE_DIR)

    def tearDown(self):
        shutil.rmtree(self.download_dir, ignore_errors=True)

    @patch("dashboard.services._generate_csv_from_hudi")
    def test_generates_csv_file(self, mock_gen_csv):
        dr = DownloadRequest.objects.create(user=self.user)

        real_makedirs = os.makedirs

        def mock_makedirs(path, **kwargs):
            if "/data/downloads/" in path:
                path = path.replace("/data/downloads/", self.download_dir + "/")
            return real_makedirs(path, **kwargs)

        with patch("os.makedirs", side_effect=mock_makedirs):
            generate_csv_for_user(dr.pk)

        dr.refresh_from_db()
        self.assertEqual(dr.status, "ready")
        self.assertIsNotNone(dr.completed_at)
        self.assertTrue(dr.file_path.endswith(".csv"))

    @patch("dashboard.services._generate_csv_from_hudi", side_effect=ValueError("No data"))
    def test_error_when_no_hudi_data(self, _mock_gen):
        dr = DownloadRequest.objects.create(user=self.user)

        real_makedirs = os.makedirs

        def mock_makedirs(path, **kwargs):
            if "/data/downloads/" in path:
                path = path.replace("/data/downloads/", self.download_dir + "/")
            return real_makedirs(path, **kwargs)

        with patch("os.makedirs", side_effect=mock_makedirs):
            generate_csv_for_user(dr.pk)
        dr.refresh_from_db()
        self.assertEqual(dr.status, "error")
        self.assertTrue(dr.error_message)

    def test_nonexistent_request_id(self):
        # Should not raise — just return silently
        generate_csv_for_user(99999)


# ---------------------------------------------------------------------------
# View tests — home
# ---------------------------------------------------------------------------
class HomeViewTests(TestCase):
    """Tests for the home view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            must_change_password=False,
        )
        self.url = reverse("home")

    def test_requires_login(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_shows_page_to_authenticated_user(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


# ---------------------------------------------------------------------------
# View tests — inspection
# ---------------------------------------------------------------------------
class InspectionViewTests(TestCase):
    """Tests for the simplified inspection view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            must_change_password=False,
        )
        self.url = reverse("inspection")

    def _make_cache(self, snapshots=None, latest_prices=None, last_snapshot=None):
        """Helper: create an InspectionCache with test data."""
        data = {
            'summary': {
                'station_count': 2,
                'price_count': 4,
                'snapshot_count': len(snapshots or []),
                'first_snapshot': '2024-01-01 10:00:00',
                'last_snapshot': last_snapshot or '2024-01-02 10:00:00',
            },
            'regions': [],
            'latest_prices': latest_prices or [],
            'snapshots': snapshots or [],
            'price_variations': {'increases': [], 'decreases': []},
        }
        return InspectionCache.objects.create(data=data)

    def test_requires_login(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_shows_empty_state_when_no_cache(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['no_cache'])
        self.assertEqual(response.context['snapshots'], [])
        self.assertEqual(response.context['current_prices'], [])
        self.assertIsNone(response.context['selected_date'])

    def test_shows_snapshots_and_prices_with_data(self):
        self._make_cache(
            snapshots=[
                {'fetched_at': '2024-01-02 10:00:00', 'record_count': 2, 'changes': 1},
            ],
            latest_prices=_SAMPLE_CURRENT_PRICES[0],
        )
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context['no_cache'])
        self.assertGreater(len(response.context['snapshots']), 0)
        self.assertGreater(len(response.context['current_prices']), 0)
        self.assertIsNotNone(response.context['selected_date'])

    def test_date_pagination(self):
        self._make_cache(
            snapshots=[
                {'fetched_at': '2024-01-02 10:00:00', 'record_count': 2, 'changes': 1},
                {'fetched_at': '2024-01-01 10:00:00', 'record_count': 2, 'changes': 2},
            ],
        )
        self.client.login(username="testuser", password="testpass123")
        # Default: most recent date
        response = self.client.get(self.url)
        self.assertEqual(response.context['selected_date'], '2024-01-02')
        # Navigate to older date
        response = self.client.get(self.url + '?date=2024-01-01')
        self.assertEqual(response.context['selected_date'], '2024-01-01')
        self.assertEqual(response.context['next_date'], '2024-01-02')
        self.assertIsNone(response.context['prev_date'])


# ---------------------------------------------------------------------------
# Helper function tests — _get_inspection_interval / _next_cron_run
# ---------------------------------------------------------------------------
class InspectionIntervalHelperTests(TestCase):
    """Tests for inspection interval helper functions."""

    def test_default_inspection_interval(self):
        from dashboard.views import _get_inspection_interval
        self.assertEqual(_get_inspection_interval(), 5)

    @patch.dict(os.environ, {"INSPECTION_REFRESH_INTERVAL_MINUTES": "10"})
    def test_custom_inspection_interval(self):
        from dashboard.views import _get_inspection_interval
        self.assertEqual(_get_inspection_interval(), 10)

    @patch.dict(os.environ, {"INSPECTION_REFRESH_INTERVAL_MINUTES": "15"})
    def test_next_cron_run_uses_custom_interval(self):
        from dashboard.views import _next_cron_run
        now = timezone.now()
        next_run = _next_cron_run()
        # next_run should be within 15 minutes from now
        self.assertLessEqual(next_run, now + timedelta(minutes=15))
        # next_run minute should be a multiple of 15
        self.assertEqual(next_run.minute % 15, 0)


# ---------------------------------------------------------------------------
# View tests — request_download
# ---------------------------------------------------------------------------
class RequestDownloadViewTests(TestCase):
    """Tests for the request_download view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            must_change_password=False,
        )
        self.url = reverse("request_download")

    def test_requires_login(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_get_redirects_to_home(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("home"))

    @patch("dashboard.views.generate_csv_for_user")
    @patch("dashboard.views.threading.Thread")
    def test_post_creates_download_request(self, mock_thread_cls, mock_gen):
        mock_thread_instance = mock_thread_cls.return_value
        self.client.login(username="testuser", password="testpass123")
        response = self.client.post(self.url)
        self.assertEqual(DownloadRequest.objects.count(), 1)
        dr = DownloadRequest.objects.first()
        self.assertEqual(dr.user, self.user)
        self.assertEqual(dr.status, "pending")
        self.assertRedirects(
            response, reverse("download_status", args=[dr.pk])
        )
        mock_thread_instance.start.assert_called_once()


# ---------------------------------------------------------------------------
# View tests — download_status
# ---------------------------------------------------------------------------
class DownloadStatusViewTests(TestCase):
    """Tests for the download_status view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            must_change_password=False,
        )
        self.dr = DownloadRequest.objects.create(
            user=self.user, status="pending"
        )
        self.url = reverse("download_status", args=[self.dr.pk])

    def test_requires_login(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_returns_json_for_ajax(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(
            self.url, HTTP_ACCEPT="application/json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["error_message"], "")

    def test_returns_html_for_normal_request(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("download_request", response.context)

    def test_cannot_see_other_users_request(self):
        other = User.objects.create_user(
            username="other", password="otherpass123", must_change_password=False
        )
        self.client.login(username="other", password="otherpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# View tests — download_file
# ---------------------------------------------------------------------------
class DownloadFileViewTests(TestCase):
    """Tests for the download_file view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            must_change_password=False,
        )
        self.download_dir = tempfile.mkdtemp(dir=settings.BASE_DIR)
        self.client.login(username="testuser", password="testpass123")

    def tearDown(self):
        shutil.rmtree(self.download_dir, ignore_errors=True)

    def test_returns_redirect_for_non_ready_file(self):
        dr = DownloadRequest.objects.create(
            user=self.user, status="pending"
        )
        url = reverse("download_file", args=[dr.pk])
        response = self.client.get(url)
        self.assertRedirects(response, reverse("home"))

    def test_serves_file_when_ready(self):
        # Create a real CSV file
        csv_path = os.path.join(self.download_dir, "test.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["col1", "col2"])
            writer.writerow(["val1", "val2"])

        dr = DownloadRequest.objects.create(
            user=self.user, status="ready", file_path=csv_path
        )
        url = reverse("download_file", args=[dr.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="fuel_prices.csv"',
        )

    def test_marks_as_expired_after_24h(self):
        csv_path = os.path.join(self.download_dir, "old.csv")
        with open(csv_path, "w") as f:
            f.write("data")

        dr = DownloadRequest.objects.create(
            user=self.user, status="ready", file_path=csv_path
        )
        # Backdate created_at
        DownloadRequest.objects.filter(pk=dr.pk).update(
            created_at=timezone.now() - timedelta(hours=25)
        )
        url = reverse("download_file", args=[dr.pk])
        response = self.client.get(url)
        self.assertRedirects(response, reverse("home"))
        dr.refresh_from_db()
        self.assertEqual(dr.status, "expired")

    def test_cannot_download_other_users_file(self):
        other = User.objects.create_user(
            username="other", password="otherpass123", must_change_password=False
        )
        dr = DownloadRequest.objects.create(
            user=other, status="ready", file_path="/some/path.csv"
        )
        url = reverse("download_file", args=[dr.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)


# ---------------------------------------------------------------------------
# Model tests — SiteConfiguration
# ---------------------------------------------------------------------------
class SiteConfigurationModelTests(TestCase):
    """Tests for the SiteConfiguration singleton model."""

    def test_load_creates_default_instance(self):
        self.assertEqual(SiteConfiguration.objects.count(), 0)
        config = SiteConfiguration.load()
        self.assertEqual(SiteConfiguration.objects.count(), 1)
        self.assertIsNone(config.inspection_interval_minutes)
        self.assertFalse(config.manual_inspection_enabled)

    def test_load_returns_existing_instance(self):
        SiteConfiguration.objects.create(
            pk=1, inspection_interval_minutes=10, manual_inspection_enabled=True
        )
        config = SiteConfiguration.load()
        self.assertEqual(config.inspection_interval_minutes, 10)
        self.assertTrue(config.manual_inspection_enabled)

    def test_save_enforces_singleton(self):
        config1 = SiteConfiguration(inspection_interval_minutes=10)
        config1.save()
        config2 = SiteConfiguration(inspection_interval_minutes=20)
        config2.save()
        self.assertEqual(SiteConfiguration.objects.count(), 1)
        config = SiteConfiguration.objects.first()
        self.assertEqual(config.inspection_interval_minutes, 20)

    def test_str(self):
        config = SiteConfiguration.load()
        self.assertEqual(str(config), "SiteConfiguration")


# ---------------------------------------------------------------------------
# View tests — settings_view
# ---------------------------------------------------------------------------
class SettingsViewTests(TestCase):
    """Tests for the settings_view."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="admin",
            password="adminpass123",
            role="admin",
            must_change_password=False,
        )
        self.normal = User.objects.create_user(
            username="client",
            password="clientpass123",
            role="client",
            must_change_password=False,
        )
        self.url = reverse("settings")

    def test_requires_login(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_non_admin_redirected_to_home(self):
        self.client.login(username="client", password="clientpass123")
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("home"))

    def test_admin_can_view_settings(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)
        self.assertIn("config", response.context)
        self.assertIn("env_interval", response.context)

    def test_admin_can_save_interval(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url, {
            "inspection_interval_minutes": "15",
            "manual_inspection_enabled": "",
        })
        self.assertRedirects(response, reverse("settings"))
        config = SiteConfiguration.load()
        self.assertEqual(config.inspection_interval_minutes, 15)
        self.assertFalse(config.manual_inspection_enabled)

    def test_admin_can_enable_manual_mode(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url, {
            "inspection_interval_minutes": "",
            "manual_inspection_enabled": "on",
        })
        self.assertRedirects(response, reverse("settings"))
        config = SiteConfiguration.load()
        self.assertTrue(config.manual_inspection_enabled)

    def test_admin_can_clear_interval(self):
        SiteConfiguration.objects.create(
            pk=1, inspection_interval_minutes=15
        )
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url, {
            "inspection_interval_minutes": "",
            "manual_inspection_enabled": "",
        })
        self.assertRedirects(response, reverse("settings"))
        config = SiteConfiguration.load()
        self.assertIsNone(config.inspection_interval_minutes)

    def test_invalid_interval_shows_error(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url, {
            "inspection_interval_minutes": "0",
            "manual_inspection_enabled": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)


# ---------------------------------------------------------------------------
# View tests — trigger_inspection
# ---------------------------------------------------------------------------
class TriggerInspectionViewTests(TestCase):
    """Tests for the trigger_inspection view."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="admin",
            password="adminpass123",
            role="admin",
            must_change_password=False,
        )
        self.normal = User.objects.create_user(
            username="client",
            password="clientpass123",
            role="client",
            must_change_password=False,
        )
        self.url = reverse("trigger_inspection")

    def test_requires_login(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_get_redirects_to_inspection(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("inspection"))

    def test_non_admin_redirected_to_home(self):
        self.client.login(username="client", password="clientpass123")
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("home"))

    @patch("dashboard.views.refresh_inspection_cache")
    @patch("dashboard.views.threading.Thread")
    def test_admin_can_trigger_inspection(self, mock_thread_cls, mock_refresh):
        mock_thread_instance = mock_thread_cls.return_value
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("inspection"))
        mock_thread_instance.start.assert_called_once()


# ---------------------------------------------------------------------------
# Helper tests — _get_inspection_interval with DB override
# ---------------------------------------------------------------------------
class InspectionIntervalDBOverrideTests(TestCase):
    """Tests for _get_inspection_interval with database overrides."""

    def test_returns_env_default_when_no_db_setting(self):
        from dashboard.views import _get_inspection_interval
        self.assertEqual(_get_inspection_interval(), 5)

    def test_returns_db_value_when_set(self):
        from dashboard.views import _get_inspection_interval
        SiteConfiguration.objects.create(pk=1, inspection_interval_minutes=20)
        self.assertEqual(_get_inspection_interval(), 20)

    def test_returns_env_when_db_value_is_none(self):
        from dashboard.views import _get_inspection_interval
        SiteConfiguration.objects.create(pk=1, inspection_interval_minutes=None)
        self.assertEqual(_get_inspection_interval(), 5)

    @patch.dict(os.environ, {"INSPECTION_REFRESH_INTERVAL_MINUTES": "30"})
    def test_db_overrides_env(self):
        from dashboard.views import _get_inspection_interval
        SiteConfiguration.objects.create(pk=1, inspection_interval_minutes=10)
        self.assertEqual(_get_inspection_interval(), 10)


# ---------------------------------------------------------------------------
# Helper tests — _is_manual_inspection_enabled
# ---------------------------------------------------------------------------
class ManualInspectionEnabledTests(TestCase):
    """Tests for _is_manual_inspection_enabled."""

    def test_returns_false_by_default(self):
        from dashboard.views import _is_manual_inspection_enabled
        self.assertFalse(_is_manual_inspection_enabled())

    def test_returns_true_when_enabled(self):
        from dashboard.views import _is_manual_inspection_enabled
        SiteConfiguration.objects.create(pk=1, manual_inspection_enabled=True)
        self.assertTrue(_is_manual_inspection_enabled())


# ---------------------------------------------------------------------------
# Management command tests — refresh_cache with manual mode
# ---------------------------------------------------------------------------
class RefreshCacheManualModeTests(TestCase):
    """Tests for the refresh_cache command respecting manual mode."""

    @patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi")
    def test_skips_when_manual_mode_enabled(self):
        SiteConfiguration.objects.create(pk=1, manual_inspection_enabled=True)
        initial_count = InspectionCache.objects.count()
        out = StringIO()
        call_command("refresh_cache", stdout=out)
        self.assertEqual(InspectionCache.objects.count(), initial_count)
        self.assertIn("Manual inspection mode is enabled", out.getvalue())

    @patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi")
    def test_runs_when_manual_mode_disabled(self):
        SiteConfiguration.objects.create(pk=1, manual_inspection_enabled=False)
        call_command("refresh_cache")
        self.assertEqual(InspectionCache.objects.count(), 1)

    @patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi")
    def test_force_overrides_manual_mode(self):
        SiteConfiguration.objects.create(pk=1, manual_inspection_enabled=True)
        call_command("refresh_cache", force=True)
        self.assertEqual(InspectionCache.objects.count(), 1)


# ---------------------------------------------------------------------------
# View tests — inspection view renders with no data
# ---------------------------------------------------------------------------
class InspectionViewNoDatabaseTests(TestCase):
    """Tests that the inspection view renders gracefully without data."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            must_change_password=False,
        )
        self.url = reverse("inspection")

    def _make_cache(self, snapshots=None, latest_prices=None):
        data = {
            'summary': {
                'station_count': 2,
                'price_count': 4,
                'snapshot_count': len(snapshots or []),
                'first_snapshot': '2024-01-01 10:00:00',
                'last_snapshot': '2024-01-02 10:00:00',
            },
            'regions': [],
            'latest_prices': latest_prices or [],
            'snapshots': snapshots or [],
            'price_variations': {'increases': [], 'decreases': []},
        }
        return InspectionCache.objects.create(data=data)

    def test_renders_ok_with_no_cache(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['all_dates'], [])
        self.assertTrue(response.context['no_cache'])

    def test_invalid_date_param_falls_back_to_latest(self):
        self._make_cache(
            snapshots=[
                {'fetched_at': '2024-01-02 10:00:00', 'record_count': 2, 'changes': 1},
                {'fetched_at': '2024-01-01 10:00:00', 'record_count': 2, 'changes': 2},
            ],
        )
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url + "?date=9999-12-31")
        self.assertEqual(response.context["selected_date"], "2024-01-02")


# ---------------------------------------------------------------------------
# Service tests — get_snapshot_dates
# ---------------------------------------------------------------------------
class GetSnapshotDatesTests(TestCase):
    """Tests for get_snapshot_dates service."""

    def test_returns_empty_when_hudi_dir_does_not_exist(self):
        with patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi"):
            dates = get_snapshot_dates()
        self.assertEqual(dates, [])

    @patch("dashboard.services.os.path.isdir", return_value=True)
    @patch("price_history.get_snapshot_dates", return_value=_SAMPLE_SNAPSHOT_DATES)
    def test_returns_dates_in_descending_order(self, _mock_dates, _mock_isdir):
        dates = get_snapshot_dates()
        self.assertEqual(dates, ["2024-01-02", "2024-01-01"])


# ---------------------------------------------------------------------------
# Service tests — get_snapshots_for_date
# ---------------------------------------------------------------------------
class GetSnapshotsForDateTests(TestCase):
    """Tests for get_snapshots_for_date service."""

    def test_returns_empty_when_hudi_dir_does_not_exist(self):
        with patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi"):
            snaps = get_snapshots_for_date("2024-01-01")
        self.assertEqual(snaps, [])

    @patch("dashboard.services.os.path.isdir", return_value=True)
    @patch("price_history.get_snapshots_for_date", return_value=_SAMPLE_SNAPSHOTS_JAN01)
    def test_returns_snapshots_for_given_date(self, _mock_snaps, _mock_isdir):
        snaps = get_snapshots_for_date("2024-01-01")
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["fetched_at"], "2024-01-01 10:00:00")
        self.assertEqual(snaps[0]["record_count"], 2)

    @patch("dashboard.services.os.path.isdir", return_value=True)
    @patch("price_history.get_snapshots_for_date", return_value=[])
    def test_returns_empty_for_nonexistent_date(self, _mock_snaps, _mock_isdir):
        snaps = get_snapshots_for_date("2099-01-01")
        self.assertEqual(snaps, [])


# ---------------------------------------------------------------------------
# Service tests — get_current_prices
# ---------------------------------------------------------------------------
class GetCurrentPricesTests(TestCase):
    """Tests for get_current_prices service."""

    def test_returns_empty_when_hudi_dir_does_not_exist(self):
        with patch("dashboard.services.HUDI_TABLE_PATH", "/nonexistent/hudi"):
            prices, ts = get_current_prices()
        self.assertEqual(prices, [])
        self.assertIsNone(ts)

    @patch("dashboard.services.os.path.isdir", return_value=True)
    @patch("price_history.get_current_prices", return_value=_SAMPLE_CURRENT_PRICES)
    def test_returns_prices_from_latest_snapshot(self, _mock_prices, _mock_isdir):
        prices, ts = get_current_prices()
        self.assertEqual(len(prices), 2)
        self.assertEqual(ts, "2024-01-02 10:00:00")
        station_names = [p["station_name"] for p in prices]
        self.assertIn("Station A", station_names)
        self.assertIn("Station B", station_names)


# ---------------------------------------------------------------------------
# View tests — trigger_refresh_snapshots
# ---------------------------------------------------------------------------
class TriggerRefreshSnapshotsViewTests(TestCase):
    """Tests for the trigger_refresh_snapshots view."""

    def setUp(self):
        self.client = Client()
        self.admin = User.objects.create_user(
            username="admin",
            password="adminpass123",
            role="admin",
            must_change_password=False,
        )
        self.normal = User.objects.create_user(
            username="client",
            password="clientpass123",
            role="client",
            must_change_password=False,
        )
        self.url = reverse("refresh_snapshots")

    def test_requires_login(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_get_redirects_to_inspection(self):
        self.client.login(username="admin", password="adminpass123")
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse("inspection"))

    def test_non_admin_redirected_to_home(self):
        self.client.login(username="client", password="clientpass123")
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("home"))

    @patch("dashboard.views.refresh_snapshots")
    @patch("dashboard.views.threading.Thread")
    def test_admin_can_trigger_refresh(self, mock_thread_cls, mock_refresh):
        mock_thread_instance = mock_thread_cls.return_value
        self.client.login(username="admin", password="adminpass123")
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("inspection"))
        mock_thread_instance.start.assert_called_once()
