"""Tests for reusable time-series transformations."""

from __future__ import annotations

import pytest

from videoscope.detectors.intervals import IntervalCandidate
from videoscope.detectors.time_series import (
    TimeSeriesPoint,
    anomalous_points_to_intervals,
    centered_moving_average,
)


def test_anomalous_points_convert_to_numeric_intervals_and_groups() -> None:
    points = [
        TimeSeriesPoint(0.0, 0, False, 0),
        TimeSeriesPoint(0.5, 1, True, 0),
        TimeSeriesPoint(1.0, 2, True, 0),
        TimeSeriesPoint(1.5, 3, True, 1),
        TimeSeriesPoint(2.0, 4, True, 1),
    ]

    intervals = anomalous_points_to_intervals(
        points,
        merge_gap_seconds=0.25,
        min_duration_seconds=0.5,
    )

    assert intervals == [
        IntervalCandidate(0.5, 1.0, (1, 2), group_index=0),
        IntervalCandidate(1.5, 2.0, (3, 4), group_index=1),
    ]


def test_centered_trend_does_not_cross_scene_groups() -> None:
    trend = centered_moving_average(
        (0.0, 0.5, 1.0, 1.5),
        (0.0, 0.2, 0.8, 1.0),
        (0, 0, 1, 1),
        window_seconds=2.0,
    )

    assert trend == pytest.approx((0.1, 0.1, 0.9, 0.9))


def test_time_series_validation_rejects_bad_window_and_lengths() -> None:
    with pytest.raises(ValueError, match="matching lengths"):
        centered_moving_average((0.0,), (), (0,), window_seconds=1.0)
    with pytest.raises(ValueError, match="greater than zero"):
        centered_moving_average((), (), (), window_seconds=0)
