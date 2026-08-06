"""Per-capability evaluation for grounded AI suggestions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import AIRange, AISuggestion, SuggestionKind


@dataclass(frozen=True, slots=True)
class GroundingMetrics:
    kind: SuggestionKind
    predicted_events: int
    reference_events: int
    matched_predictions: int
    matched_references: int
    event_precision: float
    event_recall: float
    event_f1: float
    reference_duration_coverage: float
    mean_best_temporal_iou: float


def temporal_iou(left: AIRange, right: AIRange) -> float:
    intersection = max(
        0.0,
        min(left.end_seconds, right.end_seconds)
        - max(left.start_seconds, right.start_seconds),
    )
    union = max(left.end_seconds, right.end_seconds) - min(
        left.start_seconds, right.start_seconds
    )
    return intersection / union if union > 0 else 0.0


def evaluate_grounded_ranges(
    suggestions: Sequence[AISuggestion],
    references: Mapping[SuggestionKind, Sequence[AIRange]],
    *,
    match_iou: float = 0.5,
) -> tuple[GroundingMetrics, ...]:
    """Evaluate chapter/highlight ranges independently; never aggregate them."""

    if not 0 < match_iou <= 1:
        raise ValueError("match_iou must be in (0, 1]")
    results: list[GroundingMetrics] = []
    for kind in (SuggestionKind.CHAPTER, SuggestionKind.HIGHLIGHT):
        predicted = [
            source_range
            for item in suggestions
            if item.kind is kind
            for source_range in item.evidence.source_ranges[:1]
        ]
        expected = list(references.get(kind, ()))
        matches = _greedy_matches(predicted, expected, match_iou)
        matched_predictions = len({left for left, _right, _iou in matches})
        matched_references = len({right for _left, right, _iou in matches})
        precision = matched_predictions / len(predicted) if predicted else 0.0
        recall = matched_references / len(expected) if expected else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision + recall > 0
            else 0.0
        )
        expected_duration = sum(
            item.end_seconds - item.start_seconds for item in expected
        )
        covered_duration = sum(_covered_duration(item, predicted) for item in expected)
        best_ious = [
            max((temporal_iou(item, candidate) for candidate in predicted), default=0.0)
            for item in expected
        ]
        results.append(
            GroundingMetrics(
                kind=kind,
                predicted_events=len(predicted),
                reference_events=len(expected),
                matched_predictions=matched_predictions,
                matched_references=matched_references,
                event_precision=precision,
                event_recall=recall,
                event_f1=f1,
                reference_duration_coverage=covered_duration / expected_duration
                if expected_duration > 0
                else 0.0,
                mean_best_temporal_iou=sum(best_ious) / len(best_ious)
                if best_ious
                else 0.0,
            )
        )
    return tuple(results)


def _greedy_matches(
    predicted: Sequence[AIRange], expected: Sequence[AIRange], threshold: float
) -> list[tuple[int, int, float]]:
    candidates = sorted(
        (
            (left_index, right_index, temporal_iou(left, right))
            for left_index, left in enumerate(predicted)
            for right_index, right in enumerate(expected)
        ),
        key=lambda item: (-item[2], item[0], item[1]),
    )
    used_left: set[int] = set()
    used_right: set[int] = set()
    matches: list[tuple[int, int, float]] = []
    for left_index, right_index, iou in candidates:
        if iou < threshold:
            break
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        matches.append((left_index, right_index, iou))
    return matches


def _covered_duration(reference: AIRange, predictions: Sequence[AIRange]) -> float:
    intervals = sorted(
        (
            max(reference.start_seconds, item.start_seconds),
            min(reference.end_seconds, item.end_seconds),
        )
        for item in predictions
        if min(reference.end_seconds, item.end_seconds)
        > max(reference.start_seconds, item.start_seconds)
    )
    merged: list[tuple[float, float]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return sum(end - start for start, end in merged)


__all__ = ["GroundingMetrics", "evaluate_grounded_ranges", "temporal_iou"]
