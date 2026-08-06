"""Independent useful-content verification policy."""

from __future__ import annotations

from pydantic import JsonValue

from videoscope.content.models import (
    ContentConfig,
    ContentGoal,
    ContentMap,
    ContentMappingState,
    ContentOutcome,
    ContentPlan,
    ContentSegment,
    ContentSelectionEligibility,
    ContentSignal,
    ContentSignalType,
    ContentSourceMapping,
    ContentTimeRange,
    ContentTransition,
    ContentUserRange,
    ContentUserRangeKind,
    ContentVerificationReport,
    ContentVerificationStatus,
    make_content_map_digest,
    make_mapping_id,
    make_segment_id,
    make_user_range_id,
)
from videoscope.content.planner import build_content_plan, build_storyboard
from videoscope.content.verification import (
    ContentVerificationEvidence,
    verify_content_result,
)

INPUT_HASH = "a" * 64


def time_range(start: float, end: float) -> ContentTimeRange:
    return ContentTimeRange(start_seconds=start, end_seconds=end)


def user_range(
    kind: ContentUserRangeKind,
    start: float,
    end: float,
) -> ContentUserRange:
    source_range = time_range(start, end)
    return ContentUserRange(
        id=make_user_range_id(INPUT_HASH, kind, source_range),
        kind=kind,
        source_range=source_range,
    )


def make_plan(
    *,
    goal: ContentGoal = ContentGoal.CHAPTERED_FULL,
    ranges: tuple[ContentUserRange, ...] = (),
) -> ContentPlan:
    config = ContentConfig(
        goal=goal,
        minimum_chapter_duration_seconds=2,
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
                    INPUT_HASH, source_range, (ContentSignalType.SCENE,)
                ),
                source_range=source_range,
                source_order_index=index,
                signals=(signal,),
                selection_eligibility=ContentSelectionEligibility.MANUAL_ONLY,
                reason="Structural source interval.",
                user_range_ids=tuple(
                    item.id
                    for item in ranges
                    if item.source_range.start_seconds < end
                    and start < item.source_range.end_seconds
                ),
            )
        )
    payload: dict[str, JsonValue] = {
        "input_hash": INPUT_HASH,
        "duration_seconds": 10.0,
        "effective_config": config.model_dump(mode="json"),
        "provider_executions": [],
        "segments": [item.model_dump(mode="json") for item in segments],
        "user_ranges": [item.model_dump(mode="json") for item in ranges],
        "warnings": [],
    }
    payload["map_digest"] = make_content_map_digest(payload)
    mapped = ContentMap.model_validate(payload)
    storyboard = build_storyboard(mapped)
    return build_content_plan(mapped, storyboard, preview_identities={})


def mappings_for(plan: ContentPlan) -> tuple[ContentSourceMapping, ...]:
    kept = tuple(
        item for item in plan.storyboard.items if item.output_order_index is not None
    )
    cursor = 0.0
    mappings: list[ContentSourceMapping] = []
    for output_index, item in enumerate(kept):
        output_range = time_range(cursor, cursor + item.source_range.duration_seconds)
        mappings.append(
            ContentSourceMapping(
                id=make_mapping_id(
                    plan.input_hash,
                    output_range,
                    item.source_range,
                    output_index,
                ),
                output_range=output_range,
                source_range=item.source_range,
                source_order_index=item.source_order_index,
                output_order_index=output_index,
                transition=ContentTransition.HARD_JOIN,
                state=ContentMappingState.UNCHANGED,
                storyboard_item_id=item.id,
            )
        )
        cursor = output_range.end_seconds
    return tuple(mappings)


def passing_evidence(**updates: object) -> ContentVerificationEvidence:
    values: dict[str, object] = {
        "decodable": True,
        "output_duration_seconds": 10.0,
        "has_video": True,
        "has_audio": True,
        "expected_has_audio": True,
        "black_interval_regression": False,
        "repeated_frame_regression": False,
        "audio_continuity_ok": True,
        "av_sync_residual_seconds": 0.02,
        "chapter_timing_ok": True,
        "subtitle_timing_ok": True,
        "public_relative_paths": (
            "content-output/useful-content.mp4",
            "content-output/source-map.json",
            "content-output/changes.json",
            "content-output/technical-report.json",
            "content-output/report.html",
            "content-output/chapters.json",
        ),
        "source_hash_after": INPUT_HASH,
        "source_modified": False,
    }
    values.update(updates)
    return ContentVerificationEvidence(**values)  # type: ignore[arg-type]


def status(
    report: ContentVerificationReport,
    check_id: str,
) -> ContentVerificationStatus:
    return next(item.status for item in report.checks if item.check_id == check_id)


def test_every_required_independent_check_can_pass() -> None:
    plan = make_plan()
    report = verify_content_result(
        plan=plan,
        mappings=mappings_for(plan),
        evidence=passing_evidence(),
    )

    assert report.outcome is ContentOutcome.COMPLETED
    assert all(
        item.status is ContentVerificationStatus.PASSED for item in report.checks
    )


def test_required_failure_is_failed_not_needs_review() -> None:
    plan = make_plan()
    report = verify_content_result(
        plan=plan,
        mappings=mappings_for(plan),
        evidence=passing_evidence(decodable=False),
    )
    assert report.outcome is ContentOutcome.FAILED
    assert status(report, "decodable") is ContentVerificationStatus.FAILED


def test_inconclusive_required_measurement_needs_review() -> None:
    plan = make_plan()
    report = verify_content_result(
        plan=plan,
        mappings=mappings_for(plan),
        evidence=passing_evidence(black_interval_regression=None),
    )
    assert report.outcome is ContentOutcome.NEEDS_REVIEW
    assert status(report, "join_regression") is ContentVerificationStatus.NEEDS_REVIEW


def test_duration_stream_and_av_sync_fail_closed() -> None:
    plan = make_plan()
    report = verify_content_result(
        plan=plan,
        mappings=mappings_for(plan),
        evidence=passing_evidence(
            output_duration_seconds=8.0,
            has_audio=False,
            av_sync_residual_seconds=0.5,
        ),
    )
    assert status(report, "duration") is ContentVerificationStatus.FAILED
    assert status(report, "streams") is ContentVerificationStatus.FAILED
    assert status(report, "av_sync") is ContentVerificationStatus.FAILED


def test_source_map_rejects_missing_reordered_or_discontinuous_output() -> None:
    plan = make_plan()
    mapped = mappings_for(plan)
    forged = mapped[0].model_copy(
        update={
            "output_range": time_range(1, 11),
            "id": make_mapping_id(
                INPUT_HASH, time_range(1, 11), mapped[0].source_range, 0
            ),
        }
    )
    report = verify_content_result(
        plan=plan,
        mappings=(forged,),
        evidence=passing_evidence(output_duration_seconds=11),
    )
    assert status(report, "source_map") is ContentVerificationStatus.FAILED


def test_locked_keep_must_survive_and_locked_exclude_must_be_absent() -> None:
    keep = user_range(ContentUserRangeKind.LOCKED_KEEP, 2, 3)
    exclude = user_range(ContentUserRangeKind.LOCKED_EXCLUDE, 7, 8)
    plan = make_plan(ranges=(keep, exclude))
    full = mappings_for(plan)
    report = verify_content_result(
        plan=plan,
        mappings=full,
        evidence=passing_evidence(),
    )
    assert status(report, "locked_ranges") is ContentVerificationStatus.FAILED


def test_join_audio_regressions_and_source_change_each_fail() -> None:
    plan = make_plan()
    report = verify_content_result(
        plan=plan,
        mappings=mappings_for(plan),
        evidence=passing_evidence(
            repeated_frame_regression=True,
            audio_continuity_ok=False,
            source_hash_after="b" * 64,
            source_modified=True,
        ),
    )
    assert status(report, "join_regression") is ContentVerificationStatus.FAILED
    assert status(report, "audio_continuity") is ContentVerificationStatus.FAILED
    assert status(report, "source_read_only") is ContentVerificationStatus.FAILED


def test_unsafe_or_unreviewed_public_paths_fail_allowlist() -> None:
    plan = make_plan()
    for paths in (
        ("content-output/../private/transcript.json",),
        ("content-output/unreviewed.bin",),
        ("C:\\Users\\name\\file.mp4",),
    ):
        report = verify_content_result(
            plan=plan,
            mappings=mappings_for(plan),
            evidence=passing_evidence(public_relative_paths=paths),
        )
        assert status(report, "public_artifacts") is ContentVerificationStatus.FAILED


def test_chapter_timing_failure_is_not_hidden() -> None:
    plan = make_plan()
    report = verify_content_result(
        plan=plan,
        mappings=mappings_for(plan),
        evidence=passing_evidence(chapter_timing_ok=False),
    )
    assert status(report, "chapters_subtitles") is ContentVerificationStatus.FAILED
