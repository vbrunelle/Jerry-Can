"""Services for data inspection and CSV generation."""

import logging
import os
import sys
import uuid
from datetime import timedelta

from django.utils import timezone


HUDI_TABLE_PATH = os.getenv('HUDI_TABLE_PATH', '/data/hudi/fuel_prices')

logger = logging.getLogger(__name__)

# Add the ``src/`` directory to sys.path so ``price_history`` can be
# imported directly (``import price_history``) regardless of the working
# directory.  In production Docker images the module lives next to
# ``manage.py`` (already on ``sys.path``), so the extra entry is only
# needed during local development where it resides under ``src/``.
_SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
if os.path.isdir(_SRC_DIR) and _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


def _empty_inspection_data():
    """Return empty inspection data when no database is available."""
    return {
        'summary': {
            'station_count': 0,
            'price_count': 0,
            'snapshot_count': 0,
            'first_snapshot': None,
            'last_snapshot': None,
        },
        'regions': [],
        'latest_prices': [],
        'snapshots': [],
        'price_variations': {'increases': [], 'decreases': []},
    }


def get_snapshot_dates():
    """Return a list of distinct snapshot dates (YYYY-MM-DD), most recent first."""
    if not os.path.isdir(HUDI_TABLE_PATH):
        return []
    try:
        from price_history import get_snapshot_dates as _hudi_get_snapshot_dates
        return _hudi_get_snapshot_dates(HUDI_TABLE_PATH)
    except Exception:
        logger.debug("Failed to read snapshot dates from Hudi.", exc_info=True)
        return []


def get_snapshots_for_date(date_str):
    """Return snapshots for a given date (YYYY-MM-DD), most recent first."""
    if not os.path.isdir(HUDI_TABLE_PATH):
        return []
    try:
        from price_history import get_snapshots_for_date as _hudi_get_snapshots
        return _hudi_get_snapshots(HUDI_TABLE_PATH, date_str)
    except Exception:
        logger.debug("Failed to read snapshots for date from Hudi.", exc_info=True)
        return []


def get_current_prices():
    """Return current prices for all stations from the latest snapshot."""
    if not os.path.isdir(HUDI_TABLE_PATH):
        return [], None
    try:
        from price_history import get_current_prices as _hudi_get_prices
        return _hudi_get_prices(HUDI_TABLE_PATH)
    except Exception:
        logger.debug("Failed to read current prices from Hudi.", exc_info=True)
        return [], None


def get_inspection_data():
    """Return a dict with inspection data read from the Hudi table."""
    if not os.path.isdir(HUDI_TABLE_PATH):
        return _empty_inspection_data()
    try:
        from price_history import get_inspection_data as _hudi_get_inspection
        return _hudi_get_inspection(HUDI_TABLE_PATH)
    except Exception:
        logger.debug("Failed to read inspection data from Hudi.", exc_info=True)
        return _empty_inspection_data()


def generate_csv_for_user(download_request_id):
    """Generate a CSV file with the full historicized price-change data.

    Reads directly from the Hudi parquet files (via ``price_history``).
    """
    from dashboard.models import DownloadRequest

    try:
        dr = DownloadRequest.objects.get(pk=download_request_id)
    except DownloadRequest.DoesNotExist:
        return

    dr.status = 'processing'
    dr.save(update_fields=['status'])

    try:
        download_dir = f"/data/downloads/{dr.user_id}"
        os.makedirs(download_dir, exist_ok=True)
        filename = f"{uuid.uuid4()}.csv"
        file_path = os.path.join(download_dir, filename)

        _generate_csv_from_hudi(file_path)

        dr.status = 'ready'
        dr.file_path = file_path
        dr.completed_at = timezone.now()
        dr.save(update_fields=['status', 'file_path', 'completed_at'])

    except Exception as exc:
        logger.exception("CSV generation failed for request %s", download_request_id)
        dr.status = 'error'
        dr.error_message = str(exc)
        dr.save(update_fields=['status', 'error_message'])


def _generate_csv_from_hudi(file_path):
    """Export the full historicized price-change history from Hudi parquet."""
    from price_history import build_historicized_changes_pandas

    df = build_historicized_changes_pandas(HUDI_TABLE_PATH)
    if df.empty:
        raise ValueError("No price data found in Hudi table.")

    df = df.sort_values(
        ["region", "city", "station_name", "fuel_type", "fetched_at"],
    ).reset_index(drop=True)
    df.to_csv(file_path, index=False)


def cleanup_expired_downloads():
    """Remove download files older than 24 hours and mark as expired."""
    from dashboard.models import DownloadRequest

    cutoff = timezone.now() - timedelta(hours=24)
    expired_requests = DownloadRequest.objects.filter(
        status='ready',
        created_at__lt=cutoff,
    )
    for dr in expired_requests:
        if dr.file_path and os.path.exists(dr.file_path):
            try:
                os.remove(dr.file_path)
            except OSError:
                pass
        dr.status = 'expired'
        dr.save(update_fields=['status'])


_REFRESH_RUNNING_FILE = '/tmp/refresh_cache_running'


def refresh_inspection_cache():
    """Fetch inspection data and save to InspectionCache."""
    import time
    from dashboard.models import InspectionCache

    # Write start timestamp so the view can detect an in-progress refresh
    started_at = time.time()
    try:
        with open(_REFRESH_RUNNING_FILE, 'w') as f:
            f.write(str(started_at))
    except OSError:
        pass

    try:
        t0 = time.time()
        data = get_inspection_data()
        duration = round(time.time() - t0, 1)

        InspectionCache.objects.create(data=data, duration_seconds=duration)
        # Keep only the latest cache entry
        latest = InspectionCache.objects.order_by('-created_at').first()
        if latest:
            InspectionCache.objects.exclude(pk=latest.pk).delete()
    finally:
        try:
            import os as _os
            _os.remove(_REFRESH_RUNNING_FILE)
        except OSError:
            pass


def refresh_snapshots():
    """Re-read snapshot data from the Hudi table and update the inspection cache.

    Forces a refresh of the snapshot list from the authoritative Hudi
    data, replacing any stale cache entries.
    """
    refresh_inspection_cache()
