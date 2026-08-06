"""Half-open timeline arithmetic for useful-content transformations."""

from __future__ import annotations

import pytest

from videoscope.content.models import (
    ContentDecision,
    ContentDecisionSource,
    ContentTimeRange,
    StoryboardItem,
    make_storyboard_item_id,
)
from videoscope.content.timeline import (
    compose_source_mappings,
    subtract_ranges,
    total_duration,
    union_ranges,
    validate_ordered_disjoint,
)

INPUT_HASH = "a" * 64


def time_range(start: float, end: float) -> ContentTimeRange:
    return ContentTimeRange(start_seconds=start, end_seconds=end)


def kept_item(start: float, end: float, source: int, output: int) -> StoryboardItem:
    source_range = time_range(start, end)
    return StoryboardItem(
        id=make_storyboard_item_id(INPUT_HASH, source_range, source),
        source_range=source_range,
        source_order_index=source,
        output_order_index=output,
        decision=ContentDecision.KEEP,
        decision_source=ContentDecisionSource.USER,
        reason="Exact selected range.",
    )


def test_union_and_subtraction_use_exact_half_open_edges() -> None:
    removals = union_ranges((time_range(2, 4), time_range(4, 6), time_range(8, 9)))

    assert removals == (time_range(2, 6), time_range(8, 9))
    assert subtract_ranges(time_range(0, 10), removals) == (
        time_range(0, 2),
        time_range(6, 8),
        time_range(9, 10),
    )
    assert total_duration((*removals, time_range(3, 5))) == 5.0


def test_zero_length_range_is_rejected_by_domain_model() -> None:
    with pytest.raises(ValueError, match="positive duration"):
        time_range(3, 3)


def test_order_validation_rejects_overlap_but_accepts_exact_edges() -> None:
    validate_ordered_disjoint((time_range(0, 2), time_range(2, 4)))
    with pytest.raises(ValueError, match="ordered and non-overlapping"):
        validate_ordered_disjoint((time_range(0, 3), time_range(2, 4)))


def test_source_map_composition_conserves_duration_and_output_order() -> None:
    items = (kept_item(0, 2, 0, 1), kept_item(7, 10, 1, 0))

    mappings = compose_source_mappings(INPUT_HASH, items)

    assert [item.source_range for item in mappings] == [
        time_range(7, 10),
        time_range(0, 2),
    ]
    assert [item.output_range for item in mappings] == [
        time_range(0, 3),
        time_range(3, 5),
    ]
    assert sum(item.output_range.duration_seconds for item in mappings) == 5.0
