"""Pure half-open timeline operations for useful-content planning."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from videoscope.content.models import (
    ContentMappingState,
    ContentSourceMapping,
    ContentTimeRange,
    ContentTransition,
    StoryboardItem,
    make_mapping_id,
)

_EPSILON = 1e-9


def union_ranges(ranges: Iterable[ContentTimeRange]) -> tuple[ContentTimeRange, ...]:
    """Return stable, disjoint ranges; touching half-open ranges are coalesced."""
    ordered = sorted(ranges, key=lambda item: (item.start_seconds, item.end_seconds))
    merged: list[ContentTimeRange] = []
    for item in ordered:
        if not merged or item.start_seconds > merged[-1].end_seconds + _EPSILON:
            merged.append(item)
            continue
        merged[-1] = ContentTimeRange(
            start_seconds=merged[-1].start_seconds,
            end_seconds=max(merged[-1].end_seconds, item.end_seconds),
        )
    return tuple(merged)


def subtract_ranges(
    source: ContentTimeRange,
    removals: Iterable[ContentTimeRange],
) -> tuple[ContentTimeRange, ...]:
    """Subtract exact half-open ranges from one source range."""
    clipped = union_ranges(
        ContentTimeRange(
            start_seconds=max(source.start_seconds, item.start_seconds),
            end_seconds=min(source.end_seconds, item.end_seconds),
        )
        for item in removals
        if item.start_seconds < source.end_seconds
        and source.start_seconds < item.end_seconds
    )
    kept: list[ContentTimeRange] = []
    cursor = source.start_seconds
    for item in clipped:
        if cursor < item.start_seconds - _EPSILON:
            kept.append(
                ContentTimeRange(start_seconds=cursor, end_seconds=item.start_seconds)
            )
        cursor = max(cursor, item.end_seconds)
    if cursor < source.end_seconds - _EPSILON:
        kept.append(
            ContentTimeRange(start_seconds=cursor, end_seconds=source.end_seconds)
        )
    return tuple(kept)


def intersect_ranges(
    left: ContentTimeRange,
    right: ContentTimeRange,
) -> ContentTimeRange | None:
    """Return the positive half-open intersection, if one exists."""
    start = max(left.start_seconds, right.start_seconds)
    end = min(left.end_seconds, right.end_seconds)
    if end - start <= _EPSILON:
        return None
    return ContentTimeRange(start_seconds=start, end_seconds=end)


def total_duration(ranges: Iterable[ContentTimeRange]) -> float:
    """Return the union duration without double-counting overlaps."""
    return sum(item.duration_seconds for item in union_ranges(ranges))


def ranges_cover(
    expected: ContentTimeRange,
    ranges: Iterable[ContentTimeRange],
) -> bool:
    """Return whether ranges cover an expected interval exactly."""
    combined = union_ranges(ranges)
    return len(combined) == 1 and _same_range(combined[0], expected)


def compose_source_mappings(
    input_hash: str,
    items: Sequence[StoryboardItem],
    *,
    transition: ContentTransition = ContentTransition.HARD_JOIN,
) -> tuple[ContentSourceMapping, ...]:
    """Map kept storyboard ranges into a continuous output timeline."""
    kept = sorted(
        (item for item in items if item.output_order_index is not None),
        key=lambda item: (
            item.output_order_index if item.output_order_index is not None else -1
        ),
    )
    output_cursor = 0.0
    mappings: list[ContentSourceMapping] = []
    for item in kept:
        output_range = ContentTimeRange(
            start_seconds=output_cursor,
            end_seconds=output_cursor + item.source_range.duration_seconds,
        )
        output_order_index = item.output_order_index
        if output_order_index is None:  # pragma: no cover - narrowed above
            raise ValueError("kept storyboard item has no output order")
        mappings.append(
            ContentSourceMapping(
                id=make_mapping_id(
                    input_hash,
                    output_range,
                    item.source_range,
                    output_order_index,
                ),
                output_range=output_range,
                source_range=item.source_range,
                source_order_index=item.source_order_index,
                output_order_index=output_order_index,
                transition=transition,
                state=ContentMappingState.UNCHANGED,
                storyboard_item_id=item.id,
            )
        )
        output_cursor = output_range.end_seconds
    return tuple(mappings)


def validate_ordered_disjoint(ranges: Sequence[ContentTimeRange]) -> None:
    """Reject ranges that overlap or are not in source order."""
    for left, right in zip(ranges, ranges[1:]):
        if right.start_seconds < left.end_seconds - _EPSILON:
            raise ValueError("timeline ranges must be ordered and non-overlapping")


def _same_range(left: ContentTimeRange, right: ContentTimeRange) -> bool:
    return math.isclose(
        left.start_seconds, right.start_seconds, rel_tol=0, abs_tol=_EPSILON
    ) and math.isclose(left.end_seconds, right.end_seconds, rel_tol=0, abs_tol=_EPSILON)


__all__ = [
    "compose_source_mappings",
    "intersect_ranges",
    "ranges_cover",
    "subtract_ranges",
    "total_duration",
    "union_ranges",
    "validate_ordered_disjoint",
]
