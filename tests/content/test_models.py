"""Strict, deterministic models for Long Video to Useful Content."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from videoscope.content.models import (
    CONTENT_REQUIRED_VERIFICATION_CHECK_IDS,
    ContentAction,
    ContentActionKind,
    ContentArtifact,
    ContentArtifactRole,
    ContentChapter,
    ContentConfig,
    ContentConfirmation,
    ContentDecision,
    ContentDecisionSource,
    ContentGoal,
    ContentMap,
    ContentOutcome,
    ContentPlan,
    ContentSegment,
    ContentSelectionEligibility,
    ContentSignal,
    ContentSignalType,
    ContentTechnicalReport,
    ContentTimeRange,
    ContentVerificationCheck,
    ContentVerificationReport,
    ContentVerificationStatus,
    Storyboard,
    StoryboardItem,
    make_content_map_digest,
    make_content_plan_digest,
    make_segment_id,
    make_storyboard_digest,
    make_storyboard_item_id,
)

INPUT_HASH = "a" * 64


def make_segment() -> ContentSegment:
    signal = ContentSignal(
        signal_type=ContentSignalType.SCENE,
        provider_id="scene",
        provider_version="1",
        measurements={"change": 0.2},
    )
    time_range = ContentTimeRange(start_seconds=0.0, end_seconds=4.0)
    return ContentSegment(
        id=make_segment_id(INPUT_HASH, time_range, (signal.signal_type,)),
        source_range=time_range,
        source_order_index=0,
        signals=(signal,),
        selection_eligibility=ContentSelectionEligibility.MANUAL_ONLY,
        reason="This source interval is available for explicit review.",
    )


def make_content_map() -> ContentMap:
    payload: dict[str, JsonValue] = {
        "input_hash": INPUT_HASH,
        "duration_seconds": 4.0,
        "effective_config": ContentConfig().model_dump(mode="json"),
        "provider_executions": [],
        "segments": [make_segment().model_dump(mode="json")],
        "user_ranges": [],
        "warnings": [],
    }
    payload["map_digest"] = make_content_map_digest(payload)
    return ContentMap.model_validate(payload)


def make_storyboard() -> Storyboard:
    source_range = ContentTimeRange(start_seconds=0.0, end_seconds=4.0)
    item = StoryboardItem(
        id=make_storyboard_item_id(INPUT_HASH, source_range, 0),
        source_range=source_range,
        source_order_index=0,
        output_order_index=0,
        decision=ContentDecision.KEEP,
        decision_source=ContentDecisionSource.PROPOSAL,
        reason="Preserve the full source timeline.",
        segment_ids=(make_segment().id,),
    )
    payload: dict[str, JsonValue] = {
        "input_hash": INPUT_HASH,
        "goal": ContentGoal.CHAPTERED_FULL,
        "items": [item.model_dump(mode="json")],
        "chapters": [],
        "locked_ranges": [],
        "estimated_output_duration_seconds": 4.0,
        "estimated_source_coverage": 1.0,
        "reorder_acknowledged": False,
    }
    payload["storyboard_digest"] = make_storyboard_digest(payload)
    return Storyboard.model_validate(payload)


def make_plan() -> ContentPlan:
    storyboard = make_storyboard()
    action = ContentAction(
        id="action_" + "b" * 64,
        version="1",
        kind=ContentActionKind.RETAIN,
        description="Retain the complete source timeline.",
        source_ranges=(ContentTimeRange(start_seconds=0.0, end_seconds=4.0),),
        expected_output_ranges=(ContentTimeRange(start_seconds=0.0, end_seconds=4.0),),
        changes_content=False,
        requires_confirmation=False,
    )
    payload: dict[str, JsonValue] = {
        "input_hash": INPUT_HASH,
        "goal": ContentGoal.CHAPTERED_FULL,
        "effective_config": ContentConfig(goal=ContentGoal.CHAPTERED_FULL).model_dump(
            mode="json"
        ),
        "storyboard": storyboard.model_dump(mode="json"),
        "actions": [action.model_dump(mode="json")],
        "locked_ranges": [],
        "private_artifacts": ["content-review-private/storyboard.json"],
        "public_artifacts": [
            "content-output/useful-content.mp4",
            "content-output/source-map.json",
        ],
        "preview_identities": {},
        "verification_policy": list(CONTENT_REQUIRED_VERIFICATION_CHECK_IDS),
    }
    payload["plan_digest"] = make_content_plan_digest(payload)
    return ContentPlan.model_validate(payload)


def make_verification() -> ContentVerificationReport:
    return ContentVerificationReport(
        plan_digest=make_plan().plan_digest,
        checks=tuple(
            ContentVerificationCheck(
                check_id=check_id,
                version="1",
                required=True,
                status=ContentVerificationStatus.PASSED,
                message="The independent local check passed.",
            )
            for check_id in CONTENT_REQUIRED_VERIFICATION_CHECK_IDS
        ),
        outcome=ContentOutcome.COMPLETED,
    )


def test_segment_id_is_deterministic_and_signal_sensitive() -> None:
    time_range = ContentTimeRange(start_seconds=1.0, end_seconds=2.0)

    first = make_segment_id(
        INPUT_HASH, time_range, (ContentSignalType.SCENE, ContentSignalType.SILENCE)
    )
    second = make_segment_id(
        INPUT_HASH, time_range, (ContentSignalType.SCENE, ContentSignalType.SILENCE)
    )
    changed = make_segment_id(INPUT_HASH, time_range, (ContentSignalType.SCENE,))

    assert first == second
    assert first.startswith("segment_")
    assert changed != first


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("start", "end"),
    [(2.0, 2.0), (3.0, 2.0), (float("nan"), 2.0), (0.0, float("inf"))],
)
def test_content_range_rejects_non_positive_or_non_finite_time(
    start: float, end: float
) -> None:
    with pytest.raises(ValueError):
        ContentTimeRange(start_seconds=start, end_seconds=end)


def test_models_reject_unknown_fields_and_unsafe_paths() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ContentConfig.model_validate({"unknown": True})

    with pytest.raises(ValueError, match="normalized relative POSIX path"):
        ContentArtifact(
            role=ContentArtifactRole.MEDIA,
            relative_path="C:/Users/example/output.mp4",
            sha256="b" * 64,
            description="Output media",
        )


def test_content_map_rejects_wrong_segment_id_and_stale_digest() -> None:
    content_map = make_content_map()
    payload = cast(dict[str, JsonValue], content_map.model_dump(mode="json"))
    segments = cast(list[dict[str, JsonValue]], payload["segments"])
    segments[0]["id"] = "segment_" + "0" * 64
    payload["map_digest"] = make_content_map_digest(payload)

    with pytest.raises(ValueError, match="segment ID"):
        ContentMap.model_validate(payload)

    stale = cast(dict[str, JsonValue], content_map.model_dump(mode="json"))
    stale["warnings"] = ["Changed after digest creation."]
    with pytest.raises(ValueError, match="map_digest"):
        ContentMap.model_validate(stale)


def test_storyboard_rejects_unacknowledged_reorder() -> None:
    storyboard = make_storyboard()
    first_payload = storyboard.items[0].model_dump(mode="json")
    second_range = ContentTimeRange(start_seconds=4.0, end_seconds=8.0)
    second = StoryboardItem(
        id=make_storyboard_item_id(INPUT_HASH, second_range, 1),
        source_range=second_range,
        source_order_index=1,
        output_order_index=0,
        decision=ContentDecision.KEEP,
        decision_source=ContentDecisionSource.USER,
        reason="The user selected this range.",
    )
    first_payload["output_order_index"] = 1
    payload: dict[str, JsonValue] = {
        "input_hash": INPUT_HASH,
        "goal": ContentGoal.SELECTED_CLIPS,
        "items": [first_payload, second.model_dump(mode="json")],
        "chapters": [],
        "locked_ranges": [],
        "estimated_output_duration_seconds": 8.0,
        "estimated_source_coverage": 1.0,
        "reorder_acknowledged": False,
    }
    payload["storyboard_digest"] = make_storyboard_digest(payload)

    with pytest.raises(ValueError, match="reorder acknowledgement"):
        Storyboard.model_validate(payload)


def test_plan_digest_binds_config_storyboard_locks_and_previews() -> None:
    plan = make_plan()
    payload = cast(dict[str, JsonValue], plan.model_dump(mode="json"))
    config = cast(dict[str, JsonValue], payload["effective_config"])
    config["context_guard_seconds"] = 1.5

    with pytest.raises(ValueError, match="plan_digest"):
        ContentPlan.model_validate(payload)


def test_confirmation_must_accept_exact_content_changing_action_and_preview() -> None:
    plan = make_plan()
    confirmation = ContentConfirmation(
        input_hash=plan.input_hash,
        transcript_hash=plan.transcript_hash,
        plan_digest=plan.plan_digest,
        storyboard_digest=plan.storyboard.storyboard_digest,
        accepted_action_ids=(),
        preview_identities={},
        locked_range_ids=(),
        verification_policy=plan.verification_policy,
        reorder_acknowledged=False,
    )

    plan.validate_confirmation(confirmation)

    stale = confirmation.model_copy(update={"plan_digest": "0" * 64})
    with pytest.raises(ValueError, match="plan_digest"):
        plan.validate_confirmation(stale)


def test_verification_derives_failed_outcome_from_required_check() -> None:
    checks = list(make_verification().checks)
    checks[0] = checks[0].model_copy(update={"status": "failed"})

    report = ContentVerificationReport(
        plan_digest=make_plan().plan_digest,
        checks=tuple(checks),
        outcome=ContentOutcome.COMPLETED,
    )

    assert report.outcome is ContentOutcome.FAILED


def test_public_report_rejects_absolute_paths_in_runtime() -> None:
    verification = make_verification()
    with pytest.raises(ValueError, match="absolute path"):
        ContentTechnicalReport(
            input_hash=INPUT_HASH,
            goal=ContentGoal.CHAPTERED_FULL,
            outcome=ContentOutcome.COMPLETED,
            plan_digest=verification.plan_digest,
            artifacts=(),
            chapters=(
                ContentChapter(
                    id="chapter_" + "c" * 64,
                    source_range=ContentTimeRange(start_seconds=0.0, end_seconds=4.0),
                    title="Chapter 01",
                    title_source="neutral",
                    order_index=0,
                ),
            ),
            source_mappings=(),
            change_log=None,
            verification=verification,
            runtime={"workspace": "C:/Users/example/private"},
        )


def test_nested_json_is_frozen_after_digest_validation() -> None:
    plan = make_plan()

    with pytest.raises(TypeError, match="frozen"):
        plan.effective_config.provider_parameters["new"] = True
