import numpy as np
from numpy.typing import NDArray

from videoscope.content.features import StructuralFeatureConfig
from videoscope.content.pipeline import (
    _bracketing_join_samples,
    _has_sustained_repeated_run_at_join,
)

FrameSeries = tuple[tuple[float, NDArray[np.uint8]], ...]


def test_join_sampling_requires_one_frame_on_each_side() -> None:
    frames: FrameSeries = tuple(
        (timestamp, np.full((2, 2), index, dtype=np.uint8))
        for index, timestamp in enumerate((3.5, 4.0, 4.5))
    )
    selected = _bracketing_join_samples(frames, 4.0)
    assert [timestamp for timestamp, _frame in selected] == [3.5, 4.0]


def test_join_sampling_does_not_substitute_a_same_side_sample() -> None:
    frames: FrameSeries = tuple(
        (timestamp, np.full((2, 2), index, dtype=np.uint8))
        for index, timestamp in enumerate((4.0, 4.5))
    )
    selected = _bracketing_join_samples(frames, 4.0)
    assert [timestamp for timestamp, _frame in selected] == [4.0]


def test_one_duplicate_pair_is_not_a_long_repeated_frame_regression() -> None:
    frames: FrameSeries = (
        (3.5, np.zeros((8, 8), dtype=np.uint8)),
        (4.0, np.zeros((8, 8), dtype=np.uint8)),
        (4.5, np.full((8, 8), 200, dtype=np.uint8)),
    )
    assert not _has_sustained_repeated_run_at_join(
        frames, 4.0, StructuralFeatureConfig(minimum_observation_duration_seconds=1)
    )


def test_configured_duration_repeated_run_crossing_join_is_detected() -> None:
    frames: FrameSeries = tuple(
        (timestamp, np.zeros((8, 8), dtype=np.uint8)) for timestamp in (3.5, 4.0, 4.5)
    )
    assert _has_sustained_repeated_run_at_join(
        frames, 4.0, StructuralFeatureConfig(minimum_observation_duration_seconds=1)
    )


def test_run_starting_inside_half_sample_guard_is_not_a_join_regression() -> None:
    frames: FrameSeries = tuple(
        (timestamp, np.zeros((8, 8), dtype=np.uint8)) for timestamp in (4.0, 4.5, 5.0)
    )
    assert not _has_sustained_repeated_run_at_join(
        frames,
        4.02,
        StructuralFeatureConfig(sample_fps=2, minimum_observation_duration_seconds=1),
    )
