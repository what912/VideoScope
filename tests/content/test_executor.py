"""Confirmed native useful-content execution into private pending staging."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue

from videoscope.content.errors import (
    ContentCancelledError,
    ContentConfirmationError,
    ContentMediaError,
)
from videoscope.content.executor import (
    ContentCommandResult,
    NativeContentExecutor,
)
from videoscope.content.models import (
    ContentConfig,
    ContentConfirmation,
    ContentGoal,
    ContentMap,
    ContentPlan,
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
from videoscope.content.preview import RetainedContentSource
from videoscope.video.hashing import compute_file_sha256


def time_range(start: float, end: float) -> ContentTimeRange:
    return ContentTimeRange(start_seconds=start, end_seconds=end)


def user_range(
    input_hash: str,
    kind: ContentUserRangeKind,
    start: float,
    end: float,
    label: str | None = None,
) -> ContentUserRange:
    source_range = time_range(start, end)
    return ContentUserRange(
        id=make_user_range_id(input_hash, kind, source_range),
        kind=kind,
        source_range=source_range,
        label=label,
    )


def mapped_content(
    input_hash: str,
    goal: ContentGoal,
    ranges: tuple[ContentUserRange, ...],
    *,
    allow_reorder: bool = False,
    export_clips: bool = False,
    transcript_hash: str | None = None,
) -> ContentMap:
    config = ContentConfig(
        goal=goal,
        allow_reorder=allow_reorder,
        export_clips=export_clips,
        minimum_chapter_duration_seconds=2.0,
    )
    boundaries = sorted(
        {
            0.0,
            10.0,
            *(
                value
                for item in ranges
                for value in (
                    item.source_range.start_seconds,
                    item.source_range.end_seconds,
                )
            ),
        }
    )
    segments: list[ContentSegment] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        source_range = time_range(start, end)
        signal = ContentSignal(
            signal_type=ContentSignalType.SCENE,
            provider_id="scenes",
            provider_version="1",
            measurements={"scene_index": 0},
        )
        segments.append(
            ContentSegment(
                id=make_segment_id(
                    input_hash,
                    source_range,
                    (ContentSignalType.SCENE,),
                ),
                source_range=source_range,
                source_order_index=index,
                signals=(signal,),
                selection_eligibility=ContentSelectionEligibility.MANUAL_ONLY,
                reason="Manual review interval.",
                user_range_ids=tuple(
                    item.id
                    for item in ranges
                    if item.source_range.start_seconds < end
                    and start < item.source_range.end_seconds
                ),
            )
        )
    payload: dict[str, JsonValue] = {
        "input_hash": input_hash,
        "transcript_hash": transcript_hash,
        "duration_seconds": 10.0,
        "effective_config": config.model_dump(mode="json"),
        "provider_executions": [],
        "segments": [item.model_dump(mode="json") for item in segments],
        "user_ranges": [item.model_dump(mode="json") for item in ranges],
        "warnings": [],
    }
    payload["map_digest"] = make_content_map_digest(payload)
    return ContentMap.model_validate(payload)


def plan_and_confirmation(
    mapped: ContentMap,
    *,
    selected_range_order: tuple[str, ...] = (),
    reorder_acknowledged: bool = False,
) -> tuple[ContentPlan, ContentConfirmation]:
    storyboard = build_storyboard(
        mapped,
        selected_range_order=selected_range_order,
        reorder_acknowledged=reorder_acknowledged,
    )
    preview_ids = required_preview_action_ids(mapped, storyboard)
    plan = build_content_plan(
        mapped,
        storyboard,
        preview_identities={item: f"preview-{item}" for item in preview_ids},
    )
    confirmation = ContentConfirmation(
        input_hash=plan.input_hash,
        transcript_hash=plan.transcript_hash,
        plan_digest=plan.plan_digest,
        storyboard_digest=plan.storyboard.storyboard_digest,
        accepted_action_ids=preview_ids,
        preview_identities=plan.preview_identities,
        locked_range_ids=tuple(item.id for item in plan.locked_ranges),
        verification_policy=plan.verification_policy,
        reorder_acknowledged=plan.storyboard.reorder_acknowledged,
    )
    return plan, confirmation


class WritingRunner:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.fail_at = fail_at

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> ContentCommandResult:
        del timeout_seconds, sensitive_paths
        self.commands.append(arguments)
        if self.fail_at == len(self.commands):
            return ContentCommandResult(1, stderr_summary="injected failure")
        Path(arguments[-1]).write_bytes(f"media-{len(self.commands)}".encode())
        return ContentCommandResult(0)


def retained(tmp_path: Path) -> tuple[RetainedContentSource, str]:
    path = tmp_path / "源 source with spaces.mp4"
    path.write_bytes(b"stable-source-bytes")
    digest = compute_file_sha256(path)
    return RetainedContentSource(path, expected_hash=digest), digest


def test_executor_renders_exact_kept_ranges_and_measured_source_map(
    tmp_path: Path,
) -> None:
    source, digest = retained(tmp_path)
    excluded = user_range(digest, ContentUserRangeKind.EXCLUDE, 2, 4)
    plan, confirmation = plan_and_confirmation(
        mapped_content(digest, ContentGoal.FAITHFUL_CLEAN, (excluded,))
    )
    runner = WritingRunner()
    try:
        result = NativeContentExecutor(
            runner=runner,
            duration_probe=lambda path: 2.0 if "0000" in path.name else 6.0,
        ).execute(
            plan=plan,
            confirmation=confirmation,
            source=source,
            transcript_hash=None,
            work_root=tmp_path / "work",
            has_audio=True,
        )
    finally:
        source.close()

    segment_commands = [item for item in runner.commands if "-ss" in item]
    assert [
        (item[item.index("-ss") + 1], item[item.index("-t") + 1])
        for item in segment_commands
    ] == [
        ("0.000000", "2.000000"),
        ("4.000000", "6.000000"),
    ]
    assert [item.source_range for item in result.source_mappings] == [
        time_range(0, 2),
        time_range(4, 10),
    ]
    assert [item.output_range for item in result.source_mappings] == [
        time_range(0, 2),
        time_range(2, 8),
    ]
    assert result.source_hash_before == result.source_hash_after == digest
    assert result.video_path.is_file() and result.source_map_path.is_file()


def test_stale_source_transcript_and_unaccepted_actions_are_rejected(
    tmp_path: Path,
) -> None:
    source, digest = retained(tmp_path)
    excluded = user_range(digest, ContentUserRangeKind.EXCLUDE, 2, 4)
    plan, confirmation = plan_and_confirmation(
        mapped_content(
            digest,
            ContentGoal.FAITHFUL_CLEAN,
            (excluded,),
            transcript_hash="c" * 64,
        )
    )
    executor = NativeContentExecutor(runner=WritingRunner(), duration_probe=lambda _: 1)
    try:
        with pytest.raises(ContentConfirmationError):
            executor.execute(
                plan=plan,
                confirmation=confirmation,
                source=source,
                transcript_hash=None,
                work_root=tmp_path / "work-a",
                has_audio=False,
            )
        forged = confirmation.model_copy(update={"accepted_action_ids": ()})
        with pytest.raises(ContentConfirmationError):
            executor.execute(
                plan=plan,
                confirmation=forged,
                source=source,
                transcript_hash="c" * 64,
                work_root=tmp_path / "work-b",
                has_audio=False,
            )
    finally:
        source.close()


def test_explicit_selected_clip_reorder_is_bound_and_exported(tmp_path: Path) -> None:
    source, digest = retained(tmp_path)
    first = user_range(digest, ContentUserRangeKind.KEEP, 1, 2, "First")
    second = user_range(digest, ContentUserRangeKind.KEEP, 7, 9, "Second")
    plan, confirmation = plan_and_confirmation(
        mapped_content(
            digest,
            ContentGoal.SELECTED_CLIPS,
            (first, second),
            allow_reorder=True,
            export_clips=True,
        ),
        selected_range_order=(second.id, first.id),
        reorder_acknowledged=True,
    )
    try:
        result = NativeContentExecutor(
            runner=WritingRunner(),
            duration_probe=lambda path: 2 if "0000" in path.name else 1,
        ).execute(
            plan=plan,
            confirmation=confirmation,
            source=source,
            transcript_hash=None,
            work_root=tmp_path / "work",
            has_audio=False,
        )
    finally:
        source.close()

    assert [item.source_range for item in result.source_mappings] == [
        time_range(7, 9),
        time_range(1, 2),
    ]
    assert len(result.clip_paths) == 2
    assert all(path.is_file() for path in result.clip_paths)


def test_command_failure_and_cancellation_clean_pending_tree(tmp_path: Path) -> None:
    source, digest = retained(tmp_path)
    excluded = user_range(digest, ContentUserRangeKind.EXCLUDE, 2, 4)
    plan, confirmation = plan_and_confirmation(
        mapped_content(digest, ContentGoal.FAITHFUL_CLEAN, (excluded,))
    )
    work = tmp_path / "work"
    try:
        with pytest.raises(ContentMediaError):
            NativeContentExecutor(
                runner=WritingRunner(fail_at=2),
                duration_probe=lambda _: 2,
            ).execute(
                plan=plan,
                confirmation=confirmation,
                source=source,
                transcript_hash=None,
                work_root=work,
                has_audio=False,
            )
        assert not (work / "content-pending").exists()

        with pytest.raises(ContentCancelledError):
            NativeContentExecutor(
                runner=WritingRunner(),
                duration_probe=lambda _: 2,
                is_cancelled=lambda: True,
            ).execute(
                plan=plan,
                confirmation=confirmation,
                source=source,
                transcript_hash=None,
                work_root=work,
                has_audio=False,
            )
        assert not (work / "content-pending").exists()
    finally:
        source.close()
