"""Public Rescue report contracts."""

from __future__ import annotations

import pytest

from videoscope.domain import VideoMetadata
from videoscope.rescue.executor import SourceMapping
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueActionExecution,
    RescueActionExecutionStatus,
    RescueArtifact,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueSymptom,
    RescueTechnicalReport,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    make_damage_id,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.report import render_rescue_report


def _report_models() -> tuple[RescuePlan, RescueTechnicalReport]:
    digest = "a" * 64
    interval = DamageInterval(
        id=make_damage_id(digest, "video:0", DamageKind.DARK, 0.0, 1.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=0.0,
        end_seconds=1.0,
        description="Observable low-luma interval.",
    )
    damage_map = MediaDamageMap(
        input_hash=digest,
        duration_seconds=1.0,
        intervals=(interval,),
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="private.mp4",
            container_format="mp4",
            codec="h264",
            width=2,
            height=2,
            duration_seconds=1.0,
            average_frame_rate=1.0,
            estimated_frame_count=1,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=damage_map,
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(),
        requested_symptoms=(RescueSymptom.DARK,),
        assessment_parameters={"sample_rate": 2.0, "frame_decode_passes": 1},
        assessment_limitations=("Sampled frames are bounded evidence.",),
        assessment_warnings=("The local stabilization assessment was unavailable.",),
    )
    checks = tuple(
        RescueVerificationCheck(
            check_id=check_id,
            status=RescueVerificationStatus.PASSED,
            message="measured locally",
        )
        for check_id in ("decodable", "duration", "streams", "source_read_only")
    )
    verification = RescueVerificationReport(
        plan_digest=plan.plan_digest,
        faithful_status=RescueVerificationStatus.PASSED,
        checks=checks,
        outcome=RescueOutcome.COMPLETED,
    )
    return plan, RescueTechnicalReport(
        plan_digest=plan.plan_digest,
        outcome=RescueOutcome.COMPLETED,
        damage_map=damage_map,
        verification=verification,
        requested_symptoms=plan.requested_symptoms,
        assessment_parameters=plan.assessment_parameters,
        assessment_limitations=plan.assessment_limitations,
        assessment_warnings=plan.assessment_warnings,
        action_executions=tuple(
            RescueActionExecution(
                action_id=action.id,
                kind=action.kind,
                status=RescueActionExecutionStatus.SUCCEEDED,
                artifact_role="faithful",
            )
            for action in plan.actions
        ),
        limitations=("No source video is embedded.",),
    )


def test_report_has_no_remote_or_source_video() -> None:
    plan, technical = _report_models()

    html = render_rescue_report(plan, technical)

    assert "http://" not in html
    assert "https://" not in html
    assert "<video" not in html
    assert "private.mp4" not in html


def test_report_revalidates_tampered_plan_before_rendering() -> None:
    plan, technical = _report_models()
    object.__setattr__(
        plan.actions[0], "description", "<script>alert('not markup')</script>"
    )

    with pytest.raises(ValueError):
        render_rescue_report(plan, technical)


def test_report_renderer_is_available_without_loading_media_runtime() -> None:
    assert callable(render_rescue_report)


def test_report_contains_evidence_mappings_and_relative_downloads() -> None:
    plan, technical = _report_models()
    mapping = SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4")

    html = render_rescue_report(plan, technical, (mapping,))

    assert "Observable low-luma interval." in html
    assert plan.actions[0].description in html
    assert "measured locally" in html
    assert "faithful-rescue.mp4" in html
    assert "dark" in html
    assert "frame_decode_passes" in html
    assert "Sampled frames are bounded evidence." in html
    assert "The local stabilization assessment was unavailable." in html
    assert "0.0" in html and "1.0" in html
    assert "C:\\" not in html


def test_report_renders_actual_action_execution_states() -> None:
    """A proposed action must not be presented as executed after it failed."""
    plan, technical = _report_models()
    succeeded, failed = plan.actions[:2]
    report = technical.model_copy(
        update={
            "action_executions": (
                RescueActionExecution(
                    action_id=succeeded.id,
                    kind=succeeded.kind,
                    status=RescueActionExecutionStatus.SUCCEEDED,
                    artifact_role="faithful",
                ),
                RescueActionExecution(
                    action_id=failed.id,
                    kind=failed.kind,
                    status=RescueActionExecutionStatus.FAILED,
                    artifact_role="faithful",
                    reason="Observed local execution did not complete.",
                ),
            )
        }
    )

    html = render_rescue_report(plan, report)

    assert f"{succeeded.id}</code>: executed" in html
    assert f"{failed.id}</code>: failed" in html
    assert "Observed local execution did not complete." in html


def test_report_labels_missing_legacy_action_ledger_as_unknown() -> None:
    """Legacy absence cannot be rendered as a successful execution record."""
    plan, technical = _report_models()
    legacy_payload = technical.model_dump(mode="json")
    legacy_payload.pop("action_executions")
    legacy = RescueTechnicalReport.model_validate(legacy_payload)

    html = render_rescue_report(plan, legacy)

    assert legacy.action_execution_state_known is False
    assert "Execution state unknown" in html
    assert "All actions succeeded" not in html


def test_report_labels_needs_review_media_without_calling_it_verified() -> None:
    """Review-gated media may be downloadable but is not a verified result."""
    plan, technical = _report_models()
    faithful = RescueArtifact(
        artifact_role="faithful",
        relative_path="faithful-rescue.mp4",
        sha256="b" * 64,
        description="Measured faithful candidate.",
    )
    improved = RescueArtifact(
        artifact_role="improved",
        relative_path="improved-viewing.mp4",
        sha256="c" * 64,
        description="Candidate requiring review.",
    )
    improved_checks = tuple(
        check.model_copy(
            update={
                "artifact": "improved",
                "status": (
                    RescueVerificationStatus.NEEDS_REVIEW
                    if index == 0
                    else RescueVerificationStatus.PASSED
                ),
            }
        )
        for index, check in enumerate(technical.verification.checks)
    )
    verification = RescueVerificationReport(
        plan_digest=plan.plan_digest,
        faithful_status=RescueVerificationStatus.PASSED,
        improved_status=RescueVerificationStatus.NEEDS_REVIEW,
        checks=(*technical.verification.checks, *improved_checks),
        artifacts=(faithful, improved),
        outcome=RescueOutcome.NEEDS_REVIEW,
    )
    report = technical.model_copy(
        update={
            "verification": verification,
            "outcome": RescueOutcome.NEEDS_REVIEW,
            "artifacts": (faithful, improved),
            "manual_review_reasons": ("Review the improved candidate.",),
        }
    )

    html = render_rescue_report(plan, report)

    assert "Verified downloads" not in html
    assert 'href="improved-viewing.mp4"' in html
    assert "Verification status: needs_review" in html


def test_report_escapes_observable_text_and_rejects_remote_content() -> None:
    plan, technical = _report_models()
    interval = technical.damage_map.intervals[0]
    escaped = interval.model_copy(update={"description": "<script>alert & observable"})
    damage_map = technical.damage_map.model_copy(update={"intervals": (escaped,)})
    report = technical.model_copy(update={"damage_map": damage_map})

    html = render_rescue_report(plan, report)

    assert "&lt;script&gt;alert &amp; observable" in html
    assert "<script>alert" not in html

    remote = escaped.model_copy(update={"description": "https://tracker.invalid/pixel"})
    remote_map = damage_map.model_copy(update={"intervals": (remote,)})
    with pytest.raises(ValueError):
        render_rescue_report(plan, report.model_copy(update={"damage_map": remote_map}))


def test_report_does_not_link_an_artifact_without_eligible_verification() -> None:
    plan, technical = _report_models()
    failed_candidate = RescueArtifact(
        artifact_role="improved",
        relative_path="improved-viewing.mp4",
        sha256="b" * 64,
        description="Candidate that did not pass verification.",
    )
    report = technical.model_copy(
        update={"artifacts": technical.artifacts + (failed_candidate,)}
    )

    html = render_rescue_report(plan, report)

    assert 'href="improved-viewing.mp4"' not in html


@pytest.mark.parametrize(
    "mapping",
    [
        SourceMapping(0.0, 1.0, 0.0, 1.0, "C:/private/faithful.mp4"),
        SourceMapping(1.0, 0.0, 0.0, 1.0, "faithful-rescue.mp4"),
        SourceMapping(0.0, 1.0, 1.0, 0.0, "faithful-rescue.mp4"),
        SourceMapping(0.0, 1.0, 0.0, 1.0, "../faithful-rescue.mp4"),
    ],
)
def test_report_rejects_invalid_or_private_source_mappings(
    mapping: SourceMapping,
) -> None:
    plan, technical = _report_models()

    with pytest.raises(ValueError):
        render_rescue_report(plan, technical, (mapping,))
