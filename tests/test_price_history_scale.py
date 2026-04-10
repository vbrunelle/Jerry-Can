"""Scale/performance tests for price_history functions on a synthetic Hudi table.

These tests generate a real Hudi table with synthetic data using
``ScaleDatasetGenerator`` and exercise ``get_snapshots_data()`` and
``get_commit_instants()`` against it.

All tests are marked ``scale`` AND ``integration`` (require Spark + Hudi).
Run them with:

    pytest tests/test_price_history_scale.py -m "scale"

Skip them with:

    pytest -m "not scale"
"""

import sys
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixture: module-scoped synthetic Hudi table
# ---------------------------------------------------------------------------

_SCALE_STATIONS = 50
_SCALE_COMMITS = 30
_SCALE_SEED = 42
_SCALE_FUEL_TYPES = ["regular", "premium", "diesel"]


@pytest.fixture(scope="module")
def scale_hudi_table(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Generate a synthetic Hudi table for scale tests.

    Generated once per test-module session using ``ScaleDatasetGenerator``
    with reduced parameters suitable for automated testing:

    - stations: 50
    - commits: 30
    - seed: 42

    Returns the path to the generated Hudi table.
    """
    table_path = str(tmp_path_factory.mktemp("scale_hudi") / "fuel_prices_scale")

    # Import the generator from the scripts directory
    scripts_dir = str(Path(__file__).parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from generate_large_hudi_dataset import ScaleDatasetGenerator

    generator = ScaleDatasetGenerator(
        stations=_SCALE_STATIONS,
        commits=_SCALE_COMMITS,
        fuel_types=_SCALE_FUEL_TYPES,
        price_change_rate=0.15,
        seed=_SCALE_SEED,
        output_path=table_path,
    )
    generator.run()
    return table_path


# ---------------------------------------------------------------------------
# Scale tests
# ---------------------------------------------------------------------------


@pytest.mark.scale
@pytest.mark.integration
class TestGetCommitInstantsScale:
    """Verify that get_commit_instants() returns the expected number of instants."""

    def test_returns_expected_commit_count(self, scale_hudi_table: str) -> None:
        from src.price_history import get_commit_instants

        instants = get_commit_instants(scale_hudi_table)
        # The initial snapshot is commit 0, plus up to 30 incremental commits.
        # Some incremental commits may be skipped when no station changes price,
        # so we assert at least 1 (the initial snapshot) and at most 31 total.
        assert len(instants) >= 1, "Expected at least 1 commit (initial snapshot)"
        assert len(instants) <= _SCALE_COMMITS + 1, (
            f"Expected at most {_SCALE_COMMITS + 1} commits, got {len(instants)}"
        )

    def test_instants_are_sorted_chronologically(self, scale_hudi_table: str) -> None:
        from src.price_history import get_commit_instants

        instants = get_commit_instants(scale_hudi_table)
        assert instants == sorted(instants), "Commit instants must be chronologically sorted"

    def test_returns_exactly_31_instants(self, scale_hudi_table: str) -> None:
        """With price_change_rate=0.15 and seed=42 we expect most commits to land."""
        from src.price_history import get_commit_instants

        instants = get_commit_instants(scale_hudi_table)
        # With 50 stations and 15% change rate, each commit has ~7.5 stations changing.
        # The probability of zero changes in a commit is (0.85)^50 ≈ 0.0003 — extremely
        # unlikely.  So we expect the full 30 incremental commits + 1 initial = 31.
        assert len(instants) == _SCALE_COMMITS + 1, (
            f"Expected {_SCALE_COMMITS + 1} commits (initial + {_SCALE_COMMITS} incremental), "
            f"got {len(instants)}"
        )


@pytest.mark.scale
@pytest.mark.integration
class TestGetSnapshotsDataScale:
    """Verify get_snapshots_data() correctness and performance on a scale dataset."""

    def test_returns_non_empty_results(self, scale_hudi_table: str) -> None:
        from src.price_history import get_snapshots_data

        snaps = get_snapshots_data(scale_hudi_table)
        assert len(snaps) > 0, "Expected non-empty snapshot list on a table with > 2 commits"

    def test_result_structure(self, scale_hudi_table: str) -> None:
        """Each snapshot dict must have fetched_at, record_count and changes keys."""
        from src.price_history import get_snapshots_data

        snaps = get_snapshots_data(scale_hudi_table)
        assert len(snaps) > 0

        for snap in snaps:
            assert "fetched_at" in snap, f"Missing 'fetched_at' in {snap}"
            assert "record_count" in snap, f"Missing 'record_count' in {snap}"
            assert "changes" in snap, f"Missing 'changes' in {snap}"

    def test_fetched_at_is_string(self, scale_hudi_table: str) -> None:
        from src.price_history import get_snapshots_data

        snaps = get_snapshots_data(scale_hudi_table)
        for snap in snaps:
            assert isinstance(snap["fetched_at"], str), (
                f"Expected fetched_at to be str, got {type(snap['fetched_at'])}"
            )

    def test_record_count_is_positive(self, scale_hudi_table: str) -> None:
        from src.price_history import get_snapshots_data

        snaps = get_snapshots_data(scale_hudi_table)
        for snap in snaps:
            if snap["record_count"] is not None:
                assert snap["record_count"] > 0, (
                    f"Expected positive record_count, got {snap['record_count']}"
                )

    def test_duration_under_threshold(self, scale_hudi_table: str) -> None:
        """get_snapshots_data() must complete within 120 seconds for this dataset.

        Dataset: 50 stations × 3 fuel types × 30 commits.
        Threshold: 120 seconds — generous enough for slow CI environments.
        """
        from src.price_history import get_snapshots_data

        t0 = time.perf_counter()
        snaps = get_snapshots_data(scale_hudi_table)
        elapsed = time.perf_counter() - t0

        assert elapsed < 120.0, (
            f"get_snapshots_data() took {elapsed:.1f}s — expected < 120s "
            f"for {_SCALE_STATIONS} stations, {_SCALE_COMMITS} commits"
        )
        # Must also return results (not empty due to timeout/error)
        assert len(snaps) > 0, (
            "get_snapshots_data() returned empty results "
            f"(elapsed: {elapsed:.1f}s)"
        )
