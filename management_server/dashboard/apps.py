from django.apps import AppConfig


class DashboardConfig(AppConfig):
    name = 'dashboard'

    def ready(self):
        import atexit
        import threading

        from dashboard.services import cleanup_expired_downloads, refresh_inspection_cache

        _stop_event = threading.Event()

        def _background_loop():
            """Run cache refresh every 15 minutes and cleanup every hour."""
            iteration = 0
            while not _stop_event.is_set():
                try:
                    refresh_inspection_cache()
                except Exception:
                    pass  # logged inside refresh_inspection_cache

                # Cleanup runs every 4th iteration (4 × 15 min = 60 min)
                iteration += 1
                if iteration % 4 == 0:
                    try:
                        cleanup_expired_downloads()
                    except Exception:
                        pass

                _stop_event.wait(timeout=900)  # 15 minutes

        t = threading.Thread(target=_background_loop, daemon=True, name='cache-refresh')
        t.start()
        atexit.register(_stop_event.set)
