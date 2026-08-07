"""Deterministic useful-content storyboards and plans."""

from __future__ import annotations

import pytest
from pydantic import JsonValue

from videoscope.content.errors import ContentPlanError
from videoscope.content.models import (
    ContentConfig,
    ContentDecision,
    ContentGoal,
    ContentMap,
    ContentProviderExecution,
    ContentProviderStatus,
    ContentSegment,
    ContentSelectionEligibility,
    ContentSignal,
    ContentSignalType,
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    make_content_map_digest,
    make_segment_id,
    make_user_range_id,
)
from videoscope.content.planner import (
    build_content_plan,
    build_storyboard,
    required_preview_action_ids,
)

INPUT_HASH = "b" * 64


def time_range(start: float, end: float) -> ContentTimeRange:
    return ContentTimeRange(start_seconds=start, end_seconds=end)


def user_range(
    kind: ContentUserRangeKind,
    start: float,
    end: float,
    label: str | None = None,
) -> ContentUserRange:
    source_range = time_range(start, end)
    return ContentUserRange(
        id=make_user_range_id(INPUT_HASH, kind, source_range),
        kind=kind,
        source_range=source_range,
        label=label,
    )


def content_map(
    goal: ContentGoal,
    *,
    eligible: tuple[tuple[float, float], ...] = (),
    silence_only: tuple[tuple[float, float], ...] = (),
    ranges: tuple[ContentUserRange, ...] = (),
    allow_reorder: bool = False,
    export_clips: bool = False,
    target_duration_seconds: float | None = None,
) -> ContentMap:
    config = ContentConfig(
        goal=goal,
        context_guard_seconds=0.5,
        minimum_candidate_duration_seconds=0.5,
        minimum_chapter_duration_seconds=2.0,
        allow_reorder=allow_reorder,
        export_clips=export_clips,
        target_duration_seconds=target_duration_seconds,
    )
    boundaries = sorted(
        {
            0.0,
            10.0,
            *(value for pair in (*eligible, *silence_only) for value in pair),
            *(
                value
                for item in ranges
                for value in (
                    item.source_range.start_seconds,
                    item.source_range.end_seconds,
                )
            ),
            5.0,
        }
    )
    segments: list[ContentSegment] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        source_range = time_range(start, end)
        types = [ContentSignalType.SCENE]
        is_eligible = any(start >= left and end <= right for left, right in eligible)
        is_silence = any(start >= left and end <= right for left, right in silence_only)
        if is_eligible or is_silence:
            types.append(ContentSignalType.SILENCE)
        if is_eligible:
            types.append(ContentSignalType.LOW_VISUAL_CHANGE)
        signals = tuple(
            sorted(
                (
                    ContentSignal(
                        signal_type=signal_type,
                        provider_id=(
                            "scenes"
                            if signal_type is ContentSignalType.SCENE
                            else "structure"
                        ),
                        provider_version="1",
                        measurements=(
                            {"scene_index": 0 if start < 5 else 1}
                            if signal_type is ContentSignalType.SCENE
                            else {}
                        ),
                    )
                    for signal_type in types
                ),
                key=lambda item: item.signal_type.value,
            )
        )
        overlaps = tuple(
            item.id
            for item in ranges
            if item.source_range.start_seconds < end
            and start < item.source_range.end_seconds
        )
        segments.append(
            ContentSegment(
                id=make_segment_id(
                    INPUT_HASH,
                    source_range,
                    tuple(item.signal_type for item in signals),
                ),
                source_range=source_range,
                source_order_index=index,
                signals=signals,
                selection_eligibility=(
                    ContentSelectionEligibility.ELIGIBLE
                    if is_eligible
                    else ContentSelectionEligibility.MANUAL_ONLY
                ),
                reason="Observable structural interval.",
                user_range_ids=overlaps,
            )
        )
    payload: dict[str, JsonValue] = {
        "input_hash": INPUT_HASH,
        "duration_seconds": 10.0,
        "effective_config": config.model_dump(mode="json"),
        "provider_executions": [
            ContentProviderExecution(
                provider_id="structure",
                provider_version="1",
                status=ContentProviderStatus.OK,
            ).model_dump(mode="json")
        ],
        "segments": [item.model_dump(mode="json") for item in segments],
        "user_ranges": [item.model_dump(mode="json") for item in ranges],
        "warnings": [],
    }
    payload["map_digest"] = make_content_map_digest(payload)
    return ContentMap.model_validate(payload)


def test_faithful_clean_requires_corroboration_and_retains_guard_context() -> None:
    mapped = content_map(
        ContentGoal.FAITHFUL_CLEAN,
        eligible=((2.0, 6.0),),
        silence_only=((7.0, 9.0),),
    )

    storyboard = build_storyboard(mapped)

    removed = [
        item.source_range
        for item in storyboard.items
        if item.decision is ContentDecision.REMOVE
    ]
    assert removed == [time_range(2.5, 5.5)]
    assert time_range(7, 9) not in removed
    assert [item.source_order_index for item in storyboard.items] == list(
        range(len(storyboard.items))
    )
    assert storyboard.estimated_output_duration_seconds == 7.0


def test_faithful_clean_exact_locks_win_and_target_cannot_force_removal() -> None:
    locked_keep = user_range(ContentUserRangeKind.LOCKED_KEEP, 3, 4)
    locked_exclude = user_range(ContentUserRangeKind.LOCKED_EXCLUDE, 8, 9)
    mapped = content_map(
        ContentGoal.FAITHFUL_CLEAN,
        eligible=((2, 6),),
        ranges=(locked_keep, locked_exclude),
        target_duration_seconds=1.0,
    )

    storyboard = build_storyboard(mapped)
    removed = [
        item.source_range
        for item in storyboard.items
        if item.decision is ContentDecision.REMOVE
    ]

    assert time_range(3, 4) not in removed
    assert time_range(8, 9) in removed
    assert storyboard.estimated_output_duration_seconds > 1.0


def test_no_safe_shortening_returns_full_reviewable_storyboard() -> None:
    storyboard = build_storyboard(content_map(ContentGoal.FAITHFUL_CLEAN))

    assert len(storyboard.items) == 1
    assert storyboard.items[0].decision is ContentDecision.KEEP
    assert storyboard.estimated_output_duration_seconds == 10.0


def test_chaptered_full_preserves_source_and_uses_neutral_structural_titles() -> None:
    storyboard = build_storyboard(content_map(ContentGoal.CHAPTERED_FULL))

    assert [item.source_range for item in storyboard.items] == [time_range(0, 10)]
    assert [item.source_range for item in storyboard.chapters] == [
        time_range(0, 5),
        time_range(5, 10),
    ]
    assert [item.title for item in storyboard.chapters] == ["Chapter 01", "Chapter 02"]
    assert all(item.title_source == "neutral" for item in storyboard.chapters)


def test_user_chapter_names_are_preserved_and_overlap_fails() -> None:
    first = user_range(ContentUserRangeKind.CHAPTER, 0, 4, "Opening")
    second = user_range(ContentUserRangeKind.CHAPTER, 4, 10, "Discussion")
    storyboard = build_storyboard(
        content_map(ContentGoal.CHAPTERED_FULL, ranges=(first, second))
    )
    assert [item.title for item in storyboard.chapters] == ["Opening", "Discussion"]

    overlapping = user_range(ContentUserRangeKind.CHAPTER, 3, 5, "Overlap")
    with pytest.raises(ContentPlanError):
        build_storyboard(
            content_map(ContentGoal.CHAPTERED_FULL, ranges=(first, overlapping))
        )


def test_selected_clips_merge_overlap_and_preserve_source_order_and_labels() -> None:
    first = user_range(ContentUserRangeKind.KEEP, 1, 4, "Intro")
    second = user_range(ContentUserRangeKind.KEEP, 3, 6, "Overlap")
    storyboard = build_storyboard(
        content_map(
            ContentGoal.SELECTED_CLIPS, ranges=(first, second), export_clips=True
        )
    )

    assert [item.source_range for item in storyboard.items] == [time_range(1, 6)]
    assert storyboard.reorder_acknowledged is False


def test_selected_clips_empty_draft_is_valid_before_user_selection() -> None:
    storyboard = build_storyboard(content_map(ContentGoal.SELECTED_CLIPS))

    assert storyboard.items == ()
    assert storyboard.estimated_output_duration_seconds == 0.0
    assert storyboard.estimated_source_coverage == 0.0


def test_selected_clip_reorder_requires_acknowledgement_and_changes_digest() -> None:
    first = user_range(ContentUserRangeKind.KEEP, 1, 2, "First")
    second = user_range(ContentUserRangeKind.KEEP, 7, 9, "Second")
    mapped = content_map(
        ContentGoal.SELECTED_CLIPS, ranges=(first, second), allow_reorder=True
    )

    with pytest.raises(ContentPlanError) as error:
        build_storyboard(mapped, selected_range_order=(second.id, first.id))
    assert "acknowledgement" in (error.value.internal_message or "")

    ordered = build_storyboard(mapped)
    reordered = build_storyboard(
        mapped,
        selected_range_order=(second.id, first.id),
        reorder_acknowledged=True,
    )
    assert ordered.storyboard_digest != reordered.storyboard_digest
    assert [item.output_order_index for item in reordered.items] == [1, 0]

    ordered_ids = required_preview_action_ids(mapped, ordered)
    reordered_ids = required_preview_action_ids(mapped, reordered)
    ordered_plan = build_content_plan(
        mapped,
        ordered,
        preview_identities={item: f"preview-{item}" for item in ordered_ids},
    )
    reordered_plan = build_content_plan(
        mapped,
        reordered,
        preview_identities={item: f"preview-{item}" for item in reordered_ids},
    )
    assert ordered_plan.plan_digest != reordered_plan.plan_digest


def test_content_plan_requires_exact_preview_set_for_content_changes() -> None:
    excluded = user_range(ContentUserRangeKind.EXCLUDE, 2, 4)
    mapped = content_map(ContentGoal.FAITHFUL_CLEAN, ranges=(excluded,))
    storyboard = build_storyboard(mapped)

    with pytest.raises(ContentPlanError) as error:
        build_content_plan(mapped, storyboard, preview_identities={})
    assert "preview" in (error.value.internal_message or "")

    action_ids = required_preview_action_ids(mapped, storyboard)
    plan = build_content_plan(
        mapped,
        storyboard,
        preview_identities={item: f"preview-{item}" for item in action_ids},
    )
    assert tuple(plan.preview_identities) == action_ids
