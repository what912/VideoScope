"""Convert reviewed AI proposals into ordinary C user-owned inputs."""

from __future__ import annotations

from videoscope.content import (
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    make_user_range_id,
)
from videoscope.intelligence.models import (
    AIReviewManifest,
    AISuggestionBatch,
    SuggestionKind,
)
from videoscope.intelligence.service import accepted_suggestions


def reviewed_content_ranges(
    batch: AISuggestionBatch,
    review: AIReviewManifest,
) -> tuple[ContentUserRange, ...]:
    ranges: list[ContentUserRange] = []
    for suggestion, decision in accepted_suggestions(batch, review):
        if suggestion.kind not in {SuggestionKind.CHAPTER, SuggestionKind.HIGHLIGHT}:
            continue
        source = decision.edited_source_range
        if source is None:
            if len(suggestion.evidence.source_ranges) != 1:
                continue
            source = suggestion.evidence.source_ranges[0]
        content_range = ContentTimeRange(
            start_seconds=source.start_seconds,
            end_seconds=source.end_seconds,
        )
        kind = (
            ContentUserRangeKind.CHAPTER
            if suggestion.kind is SuggestionKind.CHAPTER
            else ContentUserRangeKind.KEEP
        )
        label = decision.edited_content or suggestion.content
        ranges.append(
            ContentUserRange(
                id=make_user_range_id(batch.input_hash, kind, content_range),
                kind=kind,
                source_range=content_range,
                label=label,
            )
        )
    return tuple(
        sorted(
            ranges,
            key=lambda item: (
                item.source_range.start_seconds,
                item.kind.value,
                item.id,
            ),
        )
    )


def reviewed_text_suggestions(
    batch: AISuggestionBatch,
    review: AIReviewManifest,
) -> dict[str, tuple[str, ...]]:
    result: dict[str, list[str]] = {"summary": [], "title": []}
    for suggestion, decision in accepted_suggestions(batch, review):
        if suggestion.kind not in {SuggestionKind.SUMMARY, SuggestionKind.TITLE}:
            continue
        value = decision.edited_content or suggestion.content
        result[suggestion.kind.value].append(value)
    return {key: tuple(values) for key, values in result.items()}


__all__ = ["reviewed_content_ranges", "reviewed_text_suggestions"]
