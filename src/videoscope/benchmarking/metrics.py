"""Deterministic temporal event matching and metrics."""

from __future__ import annotations

from collections.abc import Sequence

from videoscope.benchmarking.models import (
    BenchmarkInterval,
    EventEvaluation,
    EventMatch,
    EventMetrics,
)


def temporal_iou(left: BenchmarkInterval, right: BenchmarkInterval) -> float:
    """Compute set-based temporal intersection over union."""
    intersection = max(
        0.0,
        min(left.end_seconds, right.end_seconds)
        - max(left.start_seconds, right.start_seconds),
    )
    union = left.duration_seconds + right.duration_seconds - intersection
    if union > 0:
        return intersection / union
    return 1.0 if left.start_seconds == right.start_seconds else 0.0


def evaluate_intervals(
    expected: Sequence[BenchmarkInterval],
    predicted: Sequence[BenchmarkInterval],
    *,
    minimum_iou: float = 0.1,
    tolerance_seconds: float = 0.0,
) -> EventEvaluation:
    """Match events one-to-one and calculate deterministic metrics."""
    if not 0 <= minimum_iou <= 1:
        raise ValueError("minimum_iou must be in [0, 1]")
    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds must be >= 0")

    candidates: list[tuple[float, float, int, int, EventMatch]] = []
    for prediction_index, prediction in enumerate(predicted):
        for annotation_index, annotation in enumerate(expected):
            iou = temporal_iou(prediction, annotation)
            start_error = abs(prediction.start_seconds - annotation.start_seconds)
            end_error = abs(prediction.end_seconds - annotation.end_seconds)
            within_tolerance = (
                start_error <= tolerance_seconds and end_error <= tolerance_seconds
            )
            if (iou <= 0 or iou < minimum_iou) and not within_tolerance:
                continue
            match = EventMatch(
                prediction_index=prediction_index,
                annotation_index=annotation_index,
                temporal_iou=iou,
                start_time_error_seconds=start_error,
                end_time_error_seconds=end_error,
            )
            candidates.append(
                (
                    -iou,
                    start_error + end_error,
                    prediction_index,
                    annotation_index,
                    match,
                )
            )

    matched_predictions: set[int] = set()
    matched_annotations: set[int] = set()
    matches: list[EventMatch] = []
    for _, _, prediction_index, annotation_index, match in sorted(candidates):
        if prediction_index in matched_predictions:
            continue
        if annotation_index in matched_annotations:
            continue
        matched_predictions.add(prediction_index)
        matched_annotations.add(annotation_index)
        matches.append(match)
    matches.sort(key=lambda item: (item.annotation_index, item.prediction_index))

    false_positive_intervals = [
        interval
        for index, interval in enumerate(predicted)
        if index not in matched_predictions
    ]
    metrics = _metrics_from_counts(
        true_positive=len(matches),
        false_positive=len(predicted) - len(matches),
        false_negative=len(expected) - len(matches),
        matches=matches,
        false_positive_duration=_union_duration(false_positive_intervals),
    )
    return EventEvaluation(matches=matches, metrics=metrics)


def aggregate_evaluations(
    evaluations: Sequence[EventEvaluation],
) -> EventMetrics:
    """Aggregate event counts and matched-event means without case averaging."""
    matches = [match for evaluation in evaluations for match in evaluation.matches]
    return _metrics_from_counts(
        true_positive=sum(item.metrics.true_positive_events for item in evaluations),
        false_positive=sum(item.metrics.false_positive_events for item in evaluations),
        false_negative=sum(item.metrics.false_negative_events for item in evaluations),
        matches=matches,
        false_positive_duration=sum(
            item.metrics.false_positive_duration_seconds for item in evaluations
        ),
    )


def _metrics_from_counts(
    *,
    true_positive: int,
    false_positive: int,
    false_negative: int,
    matches: Sequence[EventMatch],
    false_positive_duration: float,
) -> EventMetrics:
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    precision = (
        true_positive / precision_denominator
        if precision_denominator
        else (1.0 if recall_denominator == 0 else 0.0)
    )
    recall = true_positive / recall_denominator if recall_denominator else 1.0
    f1 = 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0
    match_count = len(matches)
    return EventMetrics(
        true_positive_events=true_positive,
        false_positive_events=false_positive,
        false_negative_events=false_negative,
        event_precision=precision,
        event_recall=recall,
        event_f1=f1,
        temporal_iou=(
            sum(item.temporal_iou for item in matches) / match_count
            if match_count
            else 0.0
        ),
        start_time_error_seconds=(
            sum(item.start_time_error_seconds for item in matches) / match_count
            if match_count
            else None
        ),
        end_time_error_seconds=(
            sum(item.end_time_error_seconds for item in matches) / match_count
            if match_count
            else None
        ),
        false_positive_duration_seconds=false_positive_duration,
    )


def _union_duration(intervals: Sequence[BenchmarkInterval]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(
        intervals,
        key=lambda item: (item.start_seconds, item.end_seconds),
    )
    total = 0.0
    start = ordered[0].start_seconds
    end = ordered[0].end_seconds
    for interval in ordered[1:]:
        if interval.start_seconds <= end:
            end = max(end, interval.end_seconds)
            continue
        total += end - start
        start = interval.start_seconds
        end = interval.end_seconds
    return total + end - start
