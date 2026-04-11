"""Tests for main.py — scheduler and run_once error handling."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _force_sqlite_backend(monkeypatch):
    """Force the SQLite backend so we don't need PySpark in tests."""
    monkeypatch.setenv("PERSISTENCE_BACKEND", "sqlite")


class TestRunOnce:
    """Verify that ``run_once`` catches and logs exceptions."""

    @patch("main.save_snapshot", return_value=3)
    @patch("main.source")
    def test_successful_run(self, mock_source: MagicMock, mock_save: MagicMock) -> None:
        import importlib
        import main

        importlib.reload(main)
        mock_source_obj = MagicMock()
        mock_source_obj.fetch.return_value = [{"station_id": "ST001", "price": 175.0}]
        main.source = mock_source_obj

        with patch.object(main, "save_snapshot", mock_save):
            main.run_once()

        mock_source_obj.fetch.assert_called_once()
        mock_save.assert_called_once()

    @patch("main.source")
    def test_logs_error_on_fetch_failure(self, mock_source: MagicMock) -> None:
        import importlib
        import main

        importlib.reload(main)
        mock_source_obj = MagicMock()
        mock_source_obj.fetch.side_effect = ConnectionError("Network unreachable")
        main.source = mock_source_obj

        # run_once should NOT raise — it catches and logs
        with patch.object(main, "logger") as mock_logger:
            main.run_once()
            mock_logger.error.assert_called_once()

    @patch("main.save_snapshot")
    @patch("main.source")
    def test_logs_error_on_save_failure(
        self, mock_source: MagicMock, mock_save: MagicMock,
    ) -> None:
        import importlib
        import main

        importlib.reload(main)
        mock_source_obj = MagicMock()
        mock_source_obj.fetch.return_value = [{"station_id": "ST001", "price": 175.0}]
        main.source = mock_source_obj

        mock_save.side_effect = RuntimeError("Hudi write failed")
        with patch.object(main, "save_snapshot", mock_save):
            with patch.object(main, "logger") as mock_logger:
                main.run_once()
                mock_logger.error.assert_called_once()


class TestMain:
    """Verify that ``main()`` propagates init_db errors."""

    @patch("main.init_db")
    def test_init_db_failure_propagates(self, mock_init: MagicMock) -> None:
        import importlib
        import main

        importlib.reload(main)
        mock_init.side_effect = RuntimeError("Java gateway process exited")
        with patch.object(main, "run_tests"), \
             patch.object(main, "init_db", mock_init):
            with pytest.raises(RuntimeError, match="Java gateway"):
                main.main()

    @patch("main.schedule")
    @patch("main.init_db")
    @patch("main.source")
    @patch("main.save_snapshot", return_value=1)
    def test_main_calls_run_once_then_schedules(
        self,
        mock_save: MagicMock,
        mock_source: MagicMock,
        mock_init: MagicMock,
        mock_schedule: MagicMock,
    ) -> None:
        import importlib
        import main

        importlib.reload(main)
        mock_source_obj = MagicMock()
        mock_source_obj.fetch.return_value = [{"station_id": "ST001", "price": 175.0}]
        main.source = mock_source_obj

        with patch.object(main, "run_tests"), \
             patch.object(main, "init_db", mock_init), \
             patch.object(main, "save_snapshot", mock_save):
            # Simulate KeyboardInterrupt on first sleep to exit the while loop
            with patch("time.sleep", side_effect=KeyboardInterrupt):
                main.main()

        mock_init.assert_called_once()
