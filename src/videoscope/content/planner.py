"""Deterministic, review-gated planners for useful-content goals."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, cast

from pydantic import JsonValue

from videoscope.content.errors import ContentPlanError
from videoscope.content.models import (
    ContentAction,
    ContentActionKind,
    ContentChapter,
    ContentDecision,
    ContentDecisionSource,
    ContentGoal,
    ContentMap,
    ContentPlan,
    ContentSelectionEligibility,
    ContentTimeRange,
    ContentUserRange,
    ContentUserRangeKind,
    Storyboard,
    StoryboardItem,
    make_action_id,
    make_chapter_id,
    make_content_plan_digest,
    make_storyboard_digest,
    make_storyboard_item_id,
)
from videoscope.content.timeline import (
    intersect_ranges,
    subtract_ranges,
    union_ranges,
    validate_ordered_disjoint,
)

_ACTION_VERSION = "1"


def build_storyboard(
    content_map: ContentMap,
    *,
    selected_range_order: tuple[str, ...] = (),
    reorder_acknowledged: bool = False,
) -> Storyboard:
    """Build one reviewable storyboard from the configured content goal."""
    try:
        if content_map.effective_config.goal is ContentGoal.FAITHFUL_CLEAN:
            items = _faithful_clean_items(content_map)
            chapters: tuple[ContentChapter, ...] = ()
        elif content_map.effective_config.goal is ContentGoal.CHAPTERED_FULL:
            items = _full_source_item(content_map)
            chapters = _chapters(content_map)
        else:
            items = _selected_clip_items(
                content_map,
                selected_range_order=selected_range_order,
                reorder_acknowledged=reorder_acknowledged,
            )
            chapters = ()
    except ValueError as exc:
        raise ContentPlanError(str(exc)) from exc
    locked = tuple(
        item
        for item in content_map.user_ranges
        if item.kind
        in {ContentUserRangeKind.LOCKED_KEEP, ContentUserRangeKind.LOCKED_EXCLUDE}
    )
    estimated_duration = sum(
        (
            item.source_range.duration_seconds
            for item in items
            if item.decision is ContentDecision.KEEP
        ),
        0.0,
    )
    payload: dict[str, JsonValue] = {
        "input_hash": content_map.input_hash,
        "transcript_hash": content_map.transcript_hash,
        "goal": content_map.effective_config.goal.value,
        "items": [item.model_dump(mode="json") for item in items],
        "chapters": [item.model_dump(mode="json") for item in chapters],
        "locked_ranges": [item.model_dump(mode="json") for item in locked],
        "estimated_output_duration_seconds": estimated_duration,
        "estimated_source_coverage": estimated_duration / content_map.duration_seconds,
        "reorder_acknowledged": reorder_acknowledged,
    }
    payload["storyboard_digest"] = make_storyboard_digest(payload)
    try:
        return Storyboard.model_validate(payload)
    except ValueError as exc:
        raise ContentPlanError(str(exc)) from exc


def build_content_plan(
    content_map: ContentMap,
    storyboard: Storyboard,
    *,
    preview_identities: Mapping[str, str],
) -> ContentPlan:
    """Bind a storyboard and already-created private previews into a plan."""
    _validate_storyboard_identity(content_map, storyboard)
    actions = _actions_for_storyboard(content_map, storyboard)
    public_artifacts = _public_artifacts(content_map, storyboard)
    payload: dict[str, JsonValue] = {
        "input_hash": content_map.input_hash,
        "transcript_hash": content_map.transcript_hash,
        "goal": content_map.effective_config.goal.value,
        "effective_config": content_map.effective_config.model_dump(mode="json"),
        "storyboard": storyboard.model_dump(mode="json"),
        "actions": [item.model_dump(mode="json") for item in actions],
        "locked_ranges": [
            item.model_dump(mode="json") for item in storyboard.locked_ranges
        ],
        "private_artifacts": [
            "content-review-private/content-map.json",
            "content-review-private/storyboard.json",
        ],
        "public_artifacts": list(public_artifacts),
        "preview_identities": cast(JsonValue, dict(sorted(preview_identities.items()))),
        "verification_policy": list(content_map.effective_config.verification_policy),
    }
    payload["plan_digest"] = make_content_plan_digest(payload)
    try:
        return ContentPlan.model_validate(payload)
    except ValueError as exc:
        raise ContentPlanError(str(exc)) from exc


def required_preview_action_ids(
    content_map: ContentMap,
    storyboard: Storyboard,
) -> tuple[str, ...]:
    """Return the stable action IDs that Task 7 must preview."""
    return tuple(
        item.id
        for item in build_content_actions(content_map, storyboard)
        if item.changes_content and item.requires_confirmation
    )


def build_content_actions(
    content_map: ContentMap,
    storyboard: Storyboard,
) -> tuple[ContentAction, ...]:
    """Expose the stable draft action sequence before private previews exist."""
    _validate_storyboard_identity(content_map, storyboard)
    return _actions_for_storyboard(content_map, storyboard)


def revise_storyboard(
    content_map: ContentMap,
    *,
    selected_range_order: tuple[str, ...] = (),
    reorder_acknowledged: bool = False,
    chapter_titles: Mapping[str, str] | None = None,
) -> Storyboard:
    """Rebuild a storyboard from current evidence and bounded user edits."""
    storyboard = build_storyboard(
        content_map,
        selected_range_order=selected_range_order,
        reorder_acknowledged=reorder_acknowledged,
    )
    titles = dict(chapter_titles or {})
    chapter_ids = {item.id for item in storyboard.chapters}
    if set(titles) - chapter_ids:
        raise ContentPlanError("chapter title revision names an unknown chapter")
    if not titles:
        return storyboard
    payload = storyboard.model_dump(mode="json", exclude={"storyboard_digest"})
    chapters = cast(list[dict[str, JsonValue]], payload["chapters"])
    for chapter in chapters:
        identifier = cast(str, chapter["id"])
        if identifier in titles:
            title = titles[identifier].strip()
            if not title:
                raise ContentPlanError("chapter title cannot be empty")
            chapter["title"] = title
            chapter["title_source"] = "user"
    payload["storyboard_digest"] = make_storyboard_digest(payload)
    try:
        return Storyboard.model_validate(payload)
    except ValueError as exc:
        raise ContentPlanError(str(exc)) from exc


def _faithful_clean_items(content_map: ContentMap) -> tuple[StoryboardItem, ...]:
    config = content_map.effective_config
    explicit_remove = union_ranges(
        item.source_range
        for item in content_map.user_ranges
        if item.kind
        in {ContentUserRangeKind.EXCLUDE, ContentUserRangeKind.LOCKED_EXCLUDE}
    )
    automatic_candidates = union_ranges(
        segment.source_range
        for segment in content_map.segments
        if segment.selection_eligibility is ContentSelectionEligibility.ELIGIBLE
        and not segment.user_range_ids
    )
    guarded_automatic = tuple(
        ContentTimeRange(
            start_seconds=item.start_seconds + config.context_guard_seconds,
            end_seconds=item.end_seconds - config.context_guard_seconds,
        )
        for item in automatic_candidates
        if item.duration_seconds > 2 * config.context_guard_seconds
    )
    locked_keep = union_ranges(
        item.source_range
        for item in content_map.user_ranges
        if item.kind is ContentUserRangeKind.LOCKED_KEEP
    )
    proposed = union_ranges((*explicit_remove, *guarded_automatic))
    removals = tuple(
        piece
        for candidate in proposed
        for piece in subtract_ranges(candidate, locked_keep)
    )
    return _partition_items(content_map, union_ranges(removals))


def _partition_items(
    content_map: ContentMap,
    removals: tuple[ContentTimeRange, ...],
) -> tuple[StoryboardItem, ...]:
    boundaries = {0.0, content_map.duration_seconds}
    for item in removals:
        boundaries.update((item.start_seconds, item.end_seconds))
    ranges = tuple(
        ContentTimeRange(start_seconds=start, end_seconds=end)
        for start, end in zip(sorted(boundaries), sorted(boundaries)[1:])
    )
    kept_index = 0
    items: list[StoryboardItem] = []
    for source_index, source_range in enumerate(ranges):
        removed = any(
            intersect_ranges(source_range, item) == source_range for item in removals
        )
        user_ranges = _overlapping_user_ranges(content_map, source_range)
        if removed:
            locked = any(
                item.kind is ContentUserRangeKind.LOCKED_EXCLUDE for item in user_ranges
            )
            source = (
                ContentDecisionSource.LOCK
                if locked
                else (
                    ContentDecisionSource.USER
                    if any(
                        item.kind is ContentUserRangeKind.EXCLUDE
                        for item in user_ranges
                    )
                    else ContentDecisionSource.PROPOSAL
                )
            )
            reason = (
                "An exact locked exclude range is omitted."
                if locked
                else (
                    "The exact reviewed low-information interval is proposed "
                    "for removal."
                )
            )
            output_index = None
            decision = ContentDecision.REMOVE
        else:
            source = (
                ContentDecisionSource.LOCK
                if any(
                    item.kind is ContentUserRangeKind.LOCKED_KEEP
                    for item in user_ranges
                )
                else ContentDecisionSource.PROPOSAL
            )
            reason = (
                "A locked keep range protects this source interval."
                if source is ContentDecisionSource.LOCK
                else "Source context is retained in original order."
            )
            output_index = kept_index
            kept_index += 1
            decision = ContentDecision.KEEP
        items.append(
            _storyboard_item(
                content_map,
                source_range,
                source_index=source_index,
                output_index=output_index,
                decision=decision,
                decision_source=source,
                reason=reason,
            )
        )
    return tuple(items)


def _full_source_item(content_map: ContentMap) -> tuple[StoryboardItem, ...]:
    source_range = ContentTimeRange(
        start_seconds=0.0, end_seconds=content_map.duration_seconds
    )
    return (
        _storyboard_item(
            content_map,
            source_range,
            source_index=0,
            output_index=0,
            decision=ContentDecision.KEEP,
            decision_source=ContentDecisionSource.PROPOSAL,
            reason="The complete source timeline is retained for chapter navigation.",
        ),
    )


def _chapters(content_map: ContentMap) -> tuple[ContentChapter, ...]:
    user_chapters = tuple(
        item
        for item in content_map.user_ranges
        if item.kind is ContentUserRangeKind.CHAPTER
    )
    if user_chapters:
        ranges = tuple(item.source_range for item in user_chapters)
        validate_ordered_disjoint(ranges)
        definitions: tuple[
            tuple[ContentTimeRange, str, Literal["neutral", "user", "transcript"]],
            ...,
        ] = tuple(
            (item.source_range, item.label or f"Chapter {index + 1:02d}", "user")
            for index, item in enumerate(user_chapters)
        )
    else:
        scene_ranges = _scene_ranges(content_map)
        definitions = tuple(
            (source_range, f"Chapter {index + 1:02d}", "neutral")
            for index, source_range in enumerate(scene_ranges)
        )
    if len(definitions) > content_map.effective_config.maximum_chapters:
        raise ContentPlanError("chapter count exceeds configured maximum")
    return tuple(
        ContentChapter(
            id=make_chapter_id(content_map.input_hash, source_range, index),
            source_range=source_range,
            output_range=source_range,
            title=title,
            title_source=source,
            order_index=index,
        )
        for index, (source_range, title, source) in enumerate(definitions)
    )


def _scene_ranges(content_map: ContentMap) -> tuple[ContentTimeRange, ...]:
    config = content_map.effective_config
    boundaries = {0.0, content_map.duration_seconds}
    last_scene: object = None
    for segment in content_map.segments:
        scene_signal = next(
            (item for item in segment.signals if item.signal_type.value == "scene"),
            None,
        )
        scene_index = (
            scene_signal.measurements.get("scene_index") if scene_signal else None
        )
        if last_scene is not None and scene_index != last_scene:
            boundaries.add(segment.source_range.start_seconds)
        last_scene = scene_index
    ordered = sorted(boundaries)
    raw = [
        ContentTimeRange(start_seconds=start, end_seconds=end)
        for start, end in zip(ordered, ordered[1:])
    ]
    grouped: list[ContentTimeRange] = []
    for item in raw:
        if (
            grouped
            and grouped[-1].duration_seconds < config.minimum_chapter_duration_seconds
        ):
            grouped[-1] = ContentTimeRange(
                start_seconds=grouped[-1].start_seconds,
                end_seconds=item.end_seconds,
            )
        else:
            grouped.append(item)
    if (
        len(grouped) > 1
        and grouped[-1].duration_seconds < config.minimum_chapter_duration_seconds
    ):
        tail = grouped.pop()
        grouped[-1] = ContentTimeRange(
            start_seconds=grouped[-1].start_seconds,
            end_seconds=tail.end_seconds,
        )
    return tuple(grouped)


def _selected_clip_items(
    content_map: ContentMap,
    *,
    selected_range_order: tuple[str, ...],
    reorder_acknowledged: bool,
) -> tuple[StoryboardItem, ...]:
    selected = tuple(
        item
        for item in content_map.user_ranges
        if item.kind in {ContentUserRangeKind.KEEP, ContentUserRangeKind.LOCKED_KEEP}
    )
    if not selected:
        return ()
    excluded = union_ranges(
        item.source_range
        for item in content_map.user_ranges
        if item.kind
        in {ContentUserRangeKind.EXCLUDE, ContentUserRangeKind.LOCKED_EXCLUDE}
    )
    ranges = union_ranges(
        piece
        for item in selected
        for piece in subtract_ranges(item.source_range, excluded)
    )
    if not ranges:
        return ()
    source_items = sorted(ranges, key=lambda item: item.start_seconds)
    range_to_user = {
        (item.source_range.start_seconds, item.source_range.end_seconds): item
        for item in selected
    }
    range_ids_list: list[str] = []
    for item in source_items:
        matching = range_to_user.get((item.start_seconds, item.end_seconds))
        range_ids_list.append(matching.id if matching is not None else "")
    range_ids = tuple(range_ids_list)
    if selected_range_order:
        if set(selected_range_order) != {item for item in range_ids if item}:
            raise ContentPlanError(
                "selected range order must name every exact keep range"
            )
        if not content_map.effective_config.allow_reorder or not reorder_acknowledged:
            raise ContentPlanError(
                "explicit reorder requires configuration and acknowledgement"
            )
        order = {
            identifier: index for index, identifier in enumerate(selected_range_order)
        }
        output_indices = tuple(order[identifier] for identifier in range_ids)
    else:
        output_indices = tuple(range(len(source_items)))
        if reorder_acknowledged:
            raise ContentPlanError("reorder acknowledgement requires an explicit order")
    return tuple(
        _storyboard_item(
            content_map,
            source_range,
            source_index=index,
            output_index=output_indices[index],
            decision=ContentDecision.KEEP,
            decision_source=ContentDecisionSource.USER,
            reason="The exact user-selected source interval is retained.",
            label=(
                range_to_user[
                    (source_range.start_seconds, source_range.end_seconds)
                ].label
                if (source_range.start_seconds, source_range.end_seconds)
                in range_to_user
                else None
            ),
        )
        for index, source_range in enumerate(source_items)
    )


def _storyboard_item(
    content_map: ContentMap,
    source_range: ContentTimeRange,
    *,
    source_index: int,
    output_index: int | None,
    decision: ContentDecision,
    decision_source: ContentDecisionSource,
    reason: str,
    label: str | None = None,
) -> StoryboardItem:
    return StoryboardItem(
        id=make_storyboard_item_id(content_map.input_hash, source_range, source_index),
        source_range=source_range,
        source_order_index=source_index,
        output_order_index=output_index,
        decision=decision,
        decision_source=decision_source,
        reason=reason,
        label=label,
        segment_ids=tuple(
            item.id
            for item in content_map.segments
            if intersect_ranges(item.source_range, source_range) is not None
        ),
    )


def _actions_for_storyboard(
    content_map: ContentMap,
    storyboard: Storyboard,
) -> tuple[ContentAction, ...]:
    actions: list[ContentAction] = []
    for item in storyboard.items:
        kind = (
            ContentActionKind.RETAIN
            if item.decision is ContentDecision.KEEP
            else ContentActionKind.REMOVE
        )
        changes = kind is ContentActionKind.REMOVE
        actions.append(
            _action(
                content_map,
                kind,
                (item.source_range,),
                len(actions),
                description=item.reason,
                changes_content=changes,
                requires_confirmation=changes,
                evidence_segment_ids=item.segment_ids,
            )
        )
    if storyboard.goal is ContentGoal.CHAPTERED_FULL:
        actions.append(
            _action(
                content_map,
                ContentActionKind.CHAPTER,
                tuple(item.source_range for item in storyboard.chapters),
                len(actions),
                description=(
                    "Attach the reviewed chapter structure without dropping "
                    "source media."
                ),
                changes_content=False,
                requires_confirmation=False,
            )
        )
    if storyboard.goal is ContentGoal.SELECTED_CLIPS and storyboard.items:
        output_items = sorted(
            storyboard.items,
            key=lambda item: (
                item.output_order_index if item.output_order_index is not None else -1
            ),
        )
        actions.append(
            _action(
                content_map,
                ContentActionKind.CONCATENATE,
                tuple(item.source_range for item in output_items),
                len(actions),
                description=(
                    "Join only the exact selected source intervals in reviewed order."
                ),
                changes_content=True,
                requires_confirmation=True,
                evidence_segment_ids=tuple(
                    segment_id
                    for item in output_items
                    for segment_id in item.segment_ids
                ),
                parameters={"reorder_acknowledged": storyboard.reorder_acknowledged},
            )
        )
    return tuple(actions)


def _action(
    content_map: ContentMap,
    kind: ContentActionKind,
    source_ranges: tuple[ContentTimeRange, ...],
    order_index: int,
    *,
    description: str,
    changes_content: bool,
    requires_confirmation: bool,
    evidence_segment_ids: tuple[str, ...] = (),
    parameters: dict[str, JsonValue] | None = None,
) -> ContentAction:
    return ContentAction(
        id=make_action_id(content_map.input_hash, kind, source_ranges, order_index),
        version=_ACTION_VERSION,
        kind=kind,
        description=description,
        source_ranges=source_ranges,
        parameters=parameters or {},
        changes_content=changes_content,
        requires_confirmation=requires_confirmation,
        evidence_segment_ids=tuple(dict.fromkeys(evidence_segment_ids)),
    )


def _public_artifacts(
    content_map: ContentMap,
    storyboard: Storyboard,
) -> tuple[str, ...]:
    values = [
        "content-output/useful-content.mp4",
        "content-output/source-map.json",
        "content-output/changes.json",
        "content-output/technical-report.json",
    ]
    if content_map.effective_config.generate_html_report:
        values.append("content-output/report.html")
    if content_map.effective_config.goal is ContentGoal.CHAPTERED_FULL:
        values.append("content-output/chapters.json")
    if content_map.effective_config.export_subtitles:
        values.append("content-output/subtitles.srt")
    if content_map.effective_config.export_clips:
        values.append("content-output/clips/manifest.json")
        kept = sorted(
            (item for item in storyboard.items if item.output_order_index is not None),
            key=lambda item: (
                item.output_order_index if item.output_order_index is not None else -1
            ),
        )
        values.extend(
            f"content-output/clips/clip-{index + 1:04d}.mp4"
            for index, _item in enumerate(kept)
        )
    return tuple(values)


def _validate_storyboard_identity(
    content_map: ContentMap, storyboard: Storyboard
) -> None:
    if (
        storyboard.input_hash != content_map.input_hash
        or storyboard.transcript_hash != content_map.transcript_hash
        or storyboard.goal is not content_map.effective_config.goal
    ):
        raise ContentPlanError("storyboard identity does not match the content map")
    if len(storyboard.items) > content_map.effective_config.maximum_storyboard_items:
        raise ContentPlanError("storyboard item count exceeds configured maximum")


def _overlapping_user_ranges(
    content_map: ContentMap,
    source_range: ContentTimeRange,
) -> tuple[ContentUserRange, ...]:
    return tuple(
        item
        for item in content_map.user_ranges
        if intersect_ranges(item.source_range, source_range) is not None
    )


__all__ = [
    "build_content_actions",
    "build_content_plan",
    "build_storyboard",
    "required_preview_action_ids",
    "revise_storyboard",
]
