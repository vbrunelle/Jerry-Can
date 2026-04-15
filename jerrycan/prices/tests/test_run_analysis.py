"""Tests for Analysis._run_analysis() scheduling and status transitions.

These tests cover the bugs that were previously undetected:
  - _run_analysis re-reads Analysis from DB on each iteration (not stale Python attr)
  - status transitions: downloading → processing → processed (or error)
  - the loop stops when run_automatically=False in the DB
  - _fetch_data errors set status=error and continue looping
  - populate() errors set status=error and continue looping
  - run_automatically=True triggers thread start on Analysis.save()
"""
from unittest.mock import patch

import pandas as pd
from django.test import TestCase

from prices.models import Analysis, Snapshot


def _empty_df():
    return pd.DataFrame(columns=["Name", "Address", "Region", "Prices", "longitude", "latitude"])


class TestRunAnalysisStatusTransitions(TestCase):
    """_run_analysis sets snapshot statuses correctly."""

    def setUp(self):
        self.analysis = Analysis.objects.create(
            data_source_url="http://fake.example.com/",
            update_frequency=5,
            active=True,
            run_automatically=True,
        )

    def _run_one_iteration(self, fetch_side_effect=None, populate_side_effect=None):
        """
        Runs _run_analysis for exactly one iteration then stops.
        Stops by setting run_automatically=False in DB after the first sleep.
        Returns the first Snapshot created.
        """
        def fake_sleep(seconds):
            Analysis.objects.filter(pk=self.analysis.pk).update(run_automatically=False)

        with patch('prices.models.time.sleep', side_effect=fake_sleep):
            if fetch_side_effect:
                with patch.object(Analysis, '_fetch_data', side_effect=fetch_side_effect):
                    self.analysis._run_analysis()
            elif populate_side_effect:
                with patch.object(Analysis, '_fetch_data', return_value=_empty_df()):
                    with patch.object(Snapshot, 'populate', side_effect=populate_side_effect):
                        self.analysis._run_analysis()
            else:
                with patch.object(Analysis, '_fetch_data', return_value=_empty_df()):
                    with patch.object(Snapshot, 'populate', return_value=None):
                        self.analysis._run_analysis()

        return Snapshot.objects.filter(analysis=self.analysis).first()

    def test_successful_run_sets_status_processed(self):
        snap = self._run_one_iteration()
        snap.refresh_from_db()
        self.assertEqual(snap.status, Snapshot.Status.PROCESSED)

    def test_fetch_error_sets_status_error(self):
        snap = self._run_one_iteration(fetch_side_effect=RuntimeError("network error"))
        snap.refresh_from_db()
        self.assertEqual(snap.status, Snapshot.Status.ERROR)

    def test_populate_error_sets_status_error(self):
        snap = self._run_one_iteration(populate_side_effect=RuntimeError("db error"))
        snap.refresh_from_db()
        self.assertEqual(snap.status, Snapshot.Status.ERROR)

    def test_snapshot_begins_as_downloading(self):
        """The snapshot must be persisted with status=downloading before any work starts."""
        observed_statuses = []

        def fake_fetch():
            snap = Snapshot.objects.filter(analysis=self.analysis).first()
            if snap:
                snap.refresh_from_db()
                observed_statuses.append(snap.status)
            return _empty_df()

        def fake_sleep(seconds):
            Analysis.objects.filter(pk=self.analysis.pk).update(run_automatically=False)

        with patch('prices.models.time.sleep', side_effect=fake_sleep):
            with patch.object(Analysis, '_fetch_data', side_effect=fake_fetch):
                with patch.object(Snapshot, 'populate', return_value=None):
                    self.analysis._run_analysis()

        self.assertEqual(observed_statuses[0], Snapshot.Status.DOWNLOADING)


class TestRunAnalysisReadsDBState(TestCase):
    """_run_analysis re-reads run_automatically and update_frequency from DB each iteration."""

    def setUp(self):
        self.analysis = Analysis.objects.create(
            data_source_url="http://fake.example.com/",
            update_frequency=5,
            active=True,
            run_automatically=True,
        )

    def test_stops_when_run_automatically_set_false_in_db(self):
        """Setting run_automatically=False in DB stops the loop after the current iteration."""
        sleep_count = 0

        def fake_sleep(seconds):
            nonlocal sleep_count
            sleep_count += 1
            Analysis.objects.filter(pk=self.analysis.pk).update(run_automatically=False)

        with patch('prices.models.time.sleep', side_effect=fake_sleep):
            with patch.object(Analysis, '_fetch_data', return_value=_empty_df()):
                with patch.object(Snapshot, 'populate', return_value=None):
                    self.analysis._run_analysis()

        self.assertEqual(sleep_count, 1)
        self.assertEqual(Snapshot.objects.filter(analysis=self.analysis).count(), 1)

    def test_update_frequency_read_from_db_each_iteration(self):
        """sleep() is called with the current DB value of update_frequency * 60."""
        sleep_calls = []
        iteration = 0

        def fake_sleep(seconds):
            nonlocal iteration
            sleep_calls.append(seconds)
            iteration += 1
            if iteration == 1:
                Analysis.objects.filter(pk=self.analysis.pk).update(update_frequency=10)
            elif iteration == 2:
                Analysis.objects.filter(pk=self.analysis.pk).update(run_automatically=False)

        with patch('prices.models.time.sleep', side_effect=fake_sleep):
            with patch.object(Analysis, '_fetch_data', return_value=_empty_df()):
                with patch.object(Snapshot, 'populate', return_value=None):
                    self.analysis._run_analysis()

        self.assertEqual(sleep_calls[0], 5 * 60)
        self.assertEqual(sleep_calls[1], 10 * 60)

    def test_loop_exits_when_analysis_deleted(self):
        """If the Analysis row is deleted, the loop exits cleanly."""
        def fake_sleep(seconds):
            Analysis.objects.filter(pk=self.analysis.pk).delete()

        with patch('prices.models.time.sleep', side_effect=fake_sleep):
            with patch.object(Analysis, '_fetch_data', return_value=_empty_df()):
                with patch.object(Snapshot, 'populate', return_value=None):
                    self.analysis._run_analysis()  # should not raise

    def test_fetch_error_does_not_stop_loop(self):
        """A _fetch_data error sets status=error but the loop continues."""
        call_count = 0

        def fake_fetch():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient network error")
            return _empty_df()

        def fake_sleep(seconds):
            if call_count >= 2:
                Analysis.objects.filter(pk=self.analysis.pk).update(run_automatically=False)

        with patch('prices.models.time.sleep', side_effect=fake_sleep):
            with patch.object(Analysis, '_fetch_data', side_effect=fake_fetch):
                with patch.object(Snapshot, 'populate', return_value=None):
                    self.analysis._run_analysis()

        self.assertEqual(call_count, 2)
        snapshots = list(Snapshot.objects.filter(analysis=self.analysis).order_by('pk'))
        self.assertEqual(snapshots[0].status, Snapshot.Status.ERROR)
        self.assertEqual(snapshots[1].status, Snapshot.Status.PROCESSED)


class TestRunAutomatically(TestCase):
    """Tests for the run_automatically field behaviour."""

    def test_default_is_false(self):
        analysis = Analysis.objects.create(
            data_source_url="http://fake.example.com/",
            update_frequency=5,
        )
        self.assertFalse(analysis.run_automatically)

    def test_loop_does_not_run_when_run_automatically_false(self):
        """_run_analysis exits immediately if run_automatically is False."""
        analysis = Analysis.objects.create(
            data_source_url="http://fake.example.com/",
            update_frequency=5,
            run_automatically=False,
        )
        with patch.object(Analysis, '_fetch_data', return_value=_empty_df()) as mock_fetch:
            analysis._run_analysis()
        mock_fetch.assert_not_called()
        self.assertEqual(Snapshot.objects.filter(analysis=analysis).count(), 0)

    def test_save_starts_thread_when_run_automatically_enabled(self):
        """Flipping run_automatically=True on an existing analysis starts a thread."""
        analysis = Analysis.objects.create(
            data_source_url="http://fake.example.com/",
            update_frequency=5,
            run_automatically=False,
        )
        with patch.object(Analysis, 'run_analysis') as mock_run:
            analysis.run_automatically = True
            analysis.save()
        mock_run.assert_called_once()

    def test_save_does_not_start_thread_when_already_true(self):
        """Saving again with run_automatically still True does not restart the thread."""
        analysis = Analysis.objects.create(
            data_source_url="http://fake.example.com/",
            update_frequency=5,
            run_automatically=True,
        )
        with patch.object(Analysis, 'run_analysis') as mock_run:
            analysis.update_frequency = 10
            analysis.save()
        mock_run.assert_not_called()

    def test_save_does_not_start_thread_when_false_to_false(self):
        """Saving an analysis that stays run_automatically=False does not start a thread."""
        analysis = Analysis.objects.create(
            data_source_url="http://fake.example.com/",
            update_frequency=5,
            run_automatically=False,
        )
        with patch.object(Analysis, 'run_analysis') as mock_run:
            analysis.update_frequency = 10
            analysis.save()
        mock_run.assert_not_called()
