"""Tests for deterministic temporal event matching metrics."""

from __future__ import annotations

import pytest

from videoscope.benchmarking import BenchmarkInterval, evaluate_intervals, temporal_iou


def _interval(start: float, end: float) -> BenchmarkInterval:
    return BenchmarkInterval(start_seconds=start, end_seconds=end)


def test_complete_match() -> None:
    result = evaluate_intervals([_interval(1, 3)], [_interval(1, 3)])

    assert result.metrics.true_positive_events == 1
    assert result.metrics.false_positive_events == 0
    assert result.metrics.false_negative_events == 0
    assert result.metrics.event_precision == 1
    assert result.metrics.event_recall == 1
    assert result.metrics.event_f1 == 1
    assert result.metrics.temporal_iou == 1
    assert result.metrics.start_time_error_seconds == 0
    assert result.metrics.end_time_error_seconds == 0


def test_partial_overlap_reports_iou_and_boundary_errors() -> None:
    expected = _interval(1, 3)
    predicted = _interval(2, 4)

    result = evaluate_intervals([expected], [predicted], minimum_iou=0.1)

    assert temporal_iou(expected, predicted) == pytest.approx(1 / 3)
    assert result.metrics.temporal_iou == pytest.approx(1 / 3)
    assert result.metrics.start_time_error_seconds == 1
    assert result.metrics.end_time_error_seconds == 1


def test_no_prediction_is_one_false_negative() -> None:
    result = evaluate_intervals([_interval(1, 2)], [])

    assert result.metrics.true_positive_events == 0
    assert result.metrics.false_positive_events == 0
    assert result.metrics.false_negative_events == 1
    assert result.metrics.event_precision == 0
    assert result.metrics.event_recall == 0
    assert result.metrics.event_f1 == 0
    assert result.metrics.start_time_error_seconds is None


def test_multiple_predictions_can_only_match_one_annotation() -> None:
    result = evaluate_intervals(
        [_interval(1, 3)],
        [_interval(1, 3), _interval(1.1, 2.9)],
    )

    assert len(result.matches) == 1
    assert result.matches[0].prediction_index == 0
    assert result.metrics.true_positive_events == 1
    assert result.metrics.false_positive_events == 1
    assert result.metrics.false_negative_events == 0
    assert result.metrics.event_precision == 0.5


def test_negative_sample_counts_false_positive_events_and_union_duration() -> None:
    result = evaluate_intervals(
        [],
        [_interval(1, 2), _interval(1.5, 3)],
    )

    assert result.metrics.false_positive_events == 2
    assert result.metrics.false_positive_duration_seconds == 2
    assert result.metrics.event_precision == 0
    assert result.metrics.event_recall == 1
    assert result.metrics.event_f1 == 0


def test_manifest_tolerance_can_match_close_boundaries() -> None:
    result = evaluate_intervals(
        [_interval(2, 2)],
        [_interval(2.05, 2.05)],
        minimum_iou=1,
        tolerance_seconds=0.1,
    )

    assert result.metrics.true_positive_events == 1
    assert result.metrics.temporal_iou == 0


def test_zero_iou_threshold_does_not_match_disjoint_events() -> None:
    result = evaluate_intervals(
        [_interval(0, 1)],
        [_interval(2, 3)],
        minimum_iou=0,
    )

    assert result.metrics.true_positive_events == 0
    assert result.metrics.false_positive_events == 1
    assert result.metrics.false_negative_events == 1
