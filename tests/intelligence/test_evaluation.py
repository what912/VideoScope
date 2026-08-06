from videoscope.intelligence.evaluation import evaluate_grounded_ranges, temporal_iou
from videoscope.intelligence.models import (
    AIRange,
    AISourceEvidence,
    AISuggestion,
    SuggestionKind,
    make_suggestion_id,
)


def suggestion(kind: SuggestionKind, start: float, end: float) -> AISuggestion:
    evidence = AISourceEvidence(
        source_ranges=(AIRange(start_seconds=start, end_seconds=end),)
    )
    identifier = make_suggestion_id(
        "a" * 64, "b" * 64, "fake", "model", kind, "item", evidence
    )
    return AISuggestion(
        id=identifier,
        kind=kind,
        content="item",
        rationale="reference",
        evidence=evidence,
    )


def test_temporal_iou_and_per_kind_grounding_metrics() -> None:
    assert (
        temporal_iou(
            AIRange(start_seconds=0, end_seconds=4),
            AIRange(start_seconds=2, end_seconds=6),
        )
        == 1 / 3
    )
    metrics = evaluate_grounded_ranges(
        [
            suggestion(SuggestionKind.HIGHLIGHT, 1, 5),
            suggestion(SuggestionKind.HIGHLIGHT, 10, 12),
            suggestion(SuggestionKind.CHAPTER, 0, 8),
        ],
        {
            SuggestionKind.HIGHLIGHT: [AIRange(start_seconds=1, end_seconds=5)],
            SuggestionKind.CHAPTER: [AIRange(start_seconds=0, end_seconds=10)],
        },
    )
    by_kind = {item.kind: item for item in metrics}
    highlight = by_kind[SuggestionKind.HIGHLIGHT]
    assert highlight.event_precision == 0.5
    assert highlight.event_recall == 1.0
    assert highlight.reference_duration_coverage == 1.0
    chapter = by_kind[SuggestionKind.CHAPTER]
    assert chapter.mean_best_temporal_iou == 0.8
    assert chapter.event_f1 == 1.0


def test_grounding_metrics_do_not_create_global_score() -> None:
    metrics = evaluate_grounded_ranges([], {})
    assert {item.kind for item in metrics} == {
        SuggestionKind.CHAPTER,
        SuggestionKind.HIGHLIGHT,
    }
    assert all(item.event_f1 == 0 for item in metrics)
    assert all(not hasattr(item, "overall_score") for item in metrics)


def test_invalid_match_threshold_is_rejected() -> None:
    try:
        evaluate_grounded_ranges([], {}, match_iou=0)
    except ValueError as exc:
        assert "match_iou" in str(exc)
    else:
        raise AssertionError("invalid threshold was accepted")
