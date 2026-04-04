import csv
import os
import shutil
import sqlite3
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.models import DownloadRequest, InspectionCache
from dashboard.services import (
    cleanup_expired_downloads,
    generate_csv_for_user,
    get_inspection_data,
    refresh_inspection_cache,
)

User = get_user_model()


# ---------------------------------------------------------------------------
# Helper: create a minimal fuel_prices.db for service tests
# ---------------------------------------------------------------------------
def _create_test_db(db_path):
    """Create a test SQLite database with stations and prices tables."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE stations (
            id INTEGER PRIMARY KEY,
            name TEXT,
            address TEXT,
            city TEXT,
            region TEXT,
            latitude REAL,
            longitude REAL
        )
    """)
    conn.execute("""
        CREATE TABLE prices (
            id INTEGER PRIMARY KEY,
            station_id INTEGER,
            fuel_type TEXT,
            price REAL,
            fetched_at TEXT,
            FOREIGN KEY (station_id) REFERENCES stations(id)
        )
    """)
    conn.execute("""
        INSERT INTO stations (id, name, address, city, region, latitude, longitude)
        VALUES (1, 'Station A', '123 Rue Principale', 'Montréal', 'Québec', 45.5, -73.5)
    """)
    conn.execute("""
        INSERT INTO stations (id, name, address, city, region, latitude, longitude)
        VALUES (2, 'Station B', '456 Rue Second', 'Québec', 'Québec', 46.8, -71.2)
    """)
    conn.execute("""
        INSERT INTO prices (station_id, fuel_type, price, fetched_at)
        VALUES (1, 'regular', 1.55, '2024-01-01 10:00:00')
    """)
    conn.execute("""
        INSERT INTO prices (station_id, fuel_type, price, fetched_at)
        VALUES (2, 'regular', 1.60, '2024-01-01 10:00:00')
    """)
    conn.execute("""
        INSERT INTO prices (station_id, fuel_type, price, fetched_at)
        VALUES (1, 'regular', 1.58, '2024-01-02 10:00:00')
    """)
    conn.execute("""
        INSERT INTO prices (station_id, fuel_type, price, fetched_at)
        VALUES (2, 'regular', 1.57, '2024-01-02 10:00:00')
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Service tests — get_inspection_data
# ---------------------------------------------------------------------------
class GetInspectionDataTests(TestCase):
    """Tests for get_inspection_data service."""

    def test_returns_empty_data_when_db_does_not_exist(self):
        with patch("dashboard.services.DATABASE_PATH", "/nonexistent/path.db"):
            data = get_inspection_data()
        self.assertEqual(data["summary"]["station_count"], 0)
        self.assertEqual(data["summary"]["price_count"], 0)
        self.assertEqual(data["regions"], [])
        self.assertEqual(data["latest_prices"], [])
        self.assertEqual(data["snapshots"], [])

    def test_returns_proper_structure_with_data(self):
        db_dir = tempfile.mkdtemp(dir=settings.BASE_DIR)
        db_path = os.path.join(db_dir, "test_fuel.db")
        try:
            _create_test_db(db_path)
            with patch("dashboard.services.DATABASE_PATH", db_path):
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
        finally:
            shutil.rmtree(db_dir)


# ---------------------------------------------------------------------------
# Service tests — refresh_inspection_cache
# ---------------------------------------------------------------------------
class RefreshInspectionCacheTests(TestCase):
    """Tests for refresh_inspection_cache service."""

    def test_creates_inspection_cache_record(self):
        with patch("dashboard.services.DATABASE_PATH", "/nonexistent/path.db"):
            refresh_inspection_cache()
        self.assertEqual(InspectionCache.objects.count(), 1)
        cache = InspectionCache.objects.first()
        self.assertIn("summary", cache.data)

    def test_keeps_only_latest_cache_entry(self):
        with patch("dashboard.services.DATABASE_PATH", "/nonexistent/path.db"):
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

    def test_generates_csv_file(self):
        db_dir = tempfile.mkdtemp(dir=settings.BASE_DIR)
        db_path = os.path.join(db_dir, "test_fuel.db")
        try:
            _create_test_db(db_path)
            dr = DownloadRequest.objects.create(user=self.user)

            real_makedirs = os.makedirs
            real_open = open

            def mock_makedirs(path, **kwargs):
                if "/data/downloads/" in path:
                    path = path.replace("/data/downloads/", self.download_dir + "/")
                return real_makedirs(path, **kwargs)

            def mock_open(path, *args, **kwargs):
                if "/data/downloads/" in str(path):
                    path = path.replace("/data/downloads/", self.download_dir + "/")
                return real_open(path, *args, **kwargs)

            with patch("dashboard.services.DATABASE_PATH", db_path), \
                 patch("os.makedirs", side_effect=mock_makedirs), \
                 patch("builtins.open", side_effect=mock_open):
                generate_csv_for_user(dr.pk)

            dr.refresh_from_db()
            self.assertEqual(dr.status, "ready")
            self.assertIsNotNone(dr.completed_at)
            self.assertTrue(dr.file_path.endswith(".csv"))
        finally:
            shutil.rmtree(db_dir)

    def test_error_when_db_not_found(self):
        dr = DownloadRequest.objects.create(user=self.user)
        with patch("dashboard.services.DATABASE_PATH", "/nonexistent/path.db"):
            generate_csv_for_user(dr.pk)
        dr.refresh_from_db()
        self.assertEqual(dr.status, "error")
        self.assertTrue(dr.error_message)  # error message is set

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
    """Tests for the inspection view."""

    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(
            username="testuser",
            password="testpass123",
            must_change_password=False,
        )
        self.url = reverse("inspection")

    def test_requires_login(self):
        response = self.client.get(self.url)
        self.assertRedirects(response, f"{reverse('login')}?next={self.url}")

    def test_shows_data_from_cache(self):
        cache_data = {
            "summary": {"station_count": 5, "price_count": 100},
            "regions": [],
            "latest_prices": [],
            "snapshots": [],
            "price_variations": {"increases": [], "decreases": []},
        }
        InspectionCache.objects.create(data=cache_data)
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["data"], cache_data)
        self.assertIsNotNone(response.context["last_updated"])

    def test_shows_none_when_no_cache(self):
        self.client.login(username="testuser", password="testpass123")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["data"])


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
