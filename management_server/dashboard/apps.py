import os
import threading
import time

from django.apps import AppConfig

_background_started = False
_stop_event = threading.Event()

REFRESH_INTERVAL = 900   # 15 minutes
CLEANUP_INTERVAL = 3600  # 1 hour


def _background_worker():
    """Periodically refresh cache and clean up expired downloads."""
    from dashboard.services import cleanup_expired_downloads, refresh_inspection_cache

    try:
        refresh_inspection_cache()
    except Exception:
        pass

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
