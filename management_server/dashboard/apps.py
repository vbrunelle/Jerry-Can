import os
import threading
import time

from django.apps import AppConfig

_background_started = False
_stop_event = threading.Event()

REFRESH_INTERVAL = 900   # 15 minutes
CLEANUP_INTERVAL = 3600  # 1 hour
RETRY_INTERVAL = 30      # retry every 30s until data is available


def _has_data():
    """Return True if the inspection cache contains at least one price record."""
    from dashboard.models import InspectionCache
    cache = InspectionCache.objects.order_by('-created_at').first()
    return bool(cache and cache.data.get('summary', {}).get('price_count', 0) > 0)


def _background_worker():
    """Periodically refresh cache and clean up expired downloads."""
    from dashboard.services import cleanup_expired_downloads, refresh_inspection_cache

    # Initial load — retry every RETRY_INTERVAL seconds until jerry-can has
    # written at least one snapshot to fuel_prices.db (it may still be
    # running unit tests or the first Hudi/SQLite write when Django boots).
    while not _stop_event.is_set():
        try:
            refresh_inspection_cache()
        except Exception:
            pass
        if _has_data() or _stop_event.is_set():
            break
        _stop_event.wait(timeout=RETRY_INTERVAL)

    last_cleanup = time.monotonic()

    while not _stop_event.is_set():
        _stop_event.wait(timeout=REFRESH_INTERVAL)
        if _stop_event.is_set():
            break

        try:
            refresh_inspection_cache()
        except Exception:
            pass

        if time.monotonic() - last_cleanup >= CLEANUP_INTERVAL:
            try:
                cleanup_expired_downloads()
            except Exception:
                pass
            last_cleanup = time.monotonic()


class DashboardConfig(AppConfig):
    name = 'dashboard'

    def ready(self):
        global _background_started
        if _background_started:
            return

        run_main = os.environ.get('RUN_MAIN')
        settings_module = os.environ.get('DJANGO_SETTINGS_MODULE')
        if not (run_main or settings_module):
            return

        _background_started = True
        thread = threading.Thread(target=_background_worker, daemon=True)
        thread.start()
