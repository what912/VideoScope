"""Contract tests for versioned Safe Sharing domain models."""

from __future__ import annotations

import warnings
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import JsonValue, ValidationError

from videoscope.domain import Severity
from videoscope.privacy.models import (
    PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS,
    PRIVACY_SCHEMA_VERSION,
    NormalizedBox,
    PrivacyAction,
    PrivacyActionKind,
    PrivacyArtifact,
    PrivacyChangeLog,
    PrivacyDecision,
    PrivacyEffectiveConfig,
    PrivacyJobOutcome,
    PrivacyPlan,
    PrivacyReviewDecision,
    PrivacyRisk,
    PrivacyRiskMap,
    PrivacyRiskType,
    PrivacyTechnicalReport,
    PrivacyVerificationCheck,
    PrivacyVerificationReport,
    RedactionStyle,
    VerificationStatus,
    make_privacy_plan_digest,
    make_privacy_risk_id,
)


def make_passed_verification(plan_digest: str) -> PrivacyVerificationReport:
    return PrivacyVerificationReport(
        plan_digest=plan_digest,
        status=PrivacyJobOutcome.COMPLETED,
        checks=tuple(
            PrivacyVerificationCheck(
                check_id=check_id,
                status=VerificationStatus.PASSED,
                message="The check passed.",
            )
            for check_id in PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS
        ),
    )


def make_risk(
    *,
    input_hash: str = "a" * 64,
    scanner_id: str = "qr_barcode_region",
    risk_type: PrivacyRiskType = PrivacyRiskType.QR_CODE,
    start_seconds: float = 1.25,
    end_seconds: float = 2.5,
    box: NormalizedBox | None = None,
    decision: PrivacyDecision = PrivacyDecision.UNREVIEWED,
    style: RedactionStyle | None = None,
    private_evidence: tuple[dict[str, JsonValue], ...] = (),
) -> PrivacyRisk:
    """Build a hand-specified risk independently from model ordering."""
    effective_box = box or NormalizedBox(
        x_min=0.1,
        y_min=0.2,
        x_max=0.4,
        y_max=0.5,
    )
    return PrivacyRisk(
        id=make_privacy_risk_id(
            input_hash,
            scanner_id,
            risk_type,
            start_seconds,
            end_seconds,
            effective_box,
        ),
        scanner_id=scanner_id,
        scanner_version="1.0.0",
        risk_type=risk_type,
        title="QR code visible",
        public_description="A QR-code-like region is visible for review.",
        severity=Severity.HIGH,
        confidence=0.8,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        box=effective_box,
        track_id=None,
        metadata_scope=None,
        metadata_key=None,
        recommended_style=RedactionStyle.PIXELATE,
        decision=decision,
        style=style,
        limitations=("This local heuristic can miss regions.",),
        evidence=({"timestamp_seconds": 1.25},),
        private_evidence=private_evidence,
    )


def make_unsorted_risk_map_payload() -> dict[str, object]:
    """Build deliberately unordered risks with hand-checked expected IDs."""
    later = make_risk(start_seconds=3.0, end_seconds=3.5)
    earlier = make_risk(
        scanner_id="manual_visual_region",
        risk_type=PrivacyRiskType.MANUAL_VISUAL,
        start_seconds=1.0,
        end_seconds=1.5,
    )
    return {
        "schema_version": PRIVACY_SCHEMA_VERSION,
        "input_hash": "a" * 64,
        "profile": "public",
        "duration_seconds": 4.0,
        "risks": [later.model_dump(mode="python"), earlier.model_dump(mode="python")],
    }


def expected_sorted_ids() -> list[str]:
    """Return the literal ordering consequence of the hand-specified fixture."""
    return [
        make_risk(
            scanner_id="manual_visual_region",
            risk_type=PrivacyRiskType.MANUAL_VISUAL,
            start_seconds=1.0,
            end_seconds=1.5,
        ).id,
        make_risk(start_seconds=3.0, end_seconds=3.5).id,
    ]


def make_plan_with_nested_parameters() -> PrivacyPlan:
    """Build a confirmed plan with nested JSON parameters for mutation tests."""
    risk = make_risk(
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.PIXELATE,
    )
    config = PrivacyEffectiveConfig(preview_seconds=3.0, guard_pixels=12)
    action = PrivacyAction(
        id="redact-qr",
        version="1.0.0",
        kind=PrivacyActionKind.VISUAL_REDACTION,
        start_seconds=risk.start_seconds,
        end_seconds=risk.end_seconds,
        box=risk.box,
        parameters={
            "redaction": {
                "style": "pixelate",
                "channels": ["video"],
            }
        },
        changes_semantics=True,
        requires_confirmation=True,
    )
    artifact = PrivacyArtifact(
        relative_path="share-safe.mp4",
        sha256="b" * 64,
        description="Shared copy",
    )
    return PrivacyPlan(
        input_hash="a" * 64,
        profile="public",
        effective_config=config,
        risks=(risk,),
        actions=(action,),
        artifacts=(artifact,),
        digest=make_privacy_plan_digest(
            "a" * 64,
            "public",
            config,
            (risk,),
            (action,),
            (artifact,),
        ),
    )


def test_privacy_risk_id_is_deterministic() -> None:
    """Risk identity must be stable for the same input and observed region."""
    box = NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.5)
    first = make_privacy_risk_id(
        input_hash="a" * 64,
        scanner_id="qr_barcode_region",
        risk_type=PrivacyRiskType.QR_CODE,
        start_seconds=1.25,
        end_seconds=2.5,
        box=box,
    )
    second = make_privacy_risk_id(
        input_hash="a" * 64,
        scanner_id="qr_barcode_region",
        risk_type=PrivacyRiskType.QR_CODE,
        start_seconds=1.25,
        end_seconds=2.5,
        box=box,
    )

    assert first == second
    assert first.startswith("privacy_risk_")


def test_privacy_risk_id_normalizes_equivalent_numeric_seconds() -> None:
    """Equivalent int, float, zero, and negative-zero times share one identity."""
    box = NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.5)

    integer_seconds = make_privacy_risk_id(
        "a" * 64,
        "qr_barcode_region",
        PrivacyRiskType.QR_CODE,
        1,
        2,
        box,
    )
    float_seconds = make_privacy_risk_id(
        "a" * 64,
        "qr_barcode_region",
        PrivacyRiskType.QR_CODE,
        1.0,
        2.0,
        box,
    )
    negative_zero = make_privacy_risk_id(
        "a" * 64,
        "qr_barcode_region",
        PrivacyRiskType.QR_CODE,
        -0.0,
        2.0,
        box,
    )
    positive_zero = make_privacy_risk_id(
        "a" * 64,
        "qr_barcode_region",
        PrivacyRiskType.QR_CODE,
        0.0,
        2.0,
        box,
    )

    assert integer_seconds == float_seconds
    assert negative_zero == positive_zero


def test_confirmation_plan_rejects_top_level_and_nested_reassignment() -> None:
    """A confirmed digest cannot be invalidated by assigning model fields."""
    plan = make_plan_with_nested_parameters()

    with pytest.raises(ValidationError, match="frozen"):
        plan.profile = "work"
    with pytest.raises(ValidationError, match="frozen"):
        plan.effective_config.guard_pixels = 99


def test_confirmation_plan_rejects_deep_json_mutation() -> None:
    """Nested dict and list values cannot change after digest confirmation."""
    plan = make_plan_with_nested_parameters()
    parameters = cast(dict[str, Any], plan.actions[0].parameters)
    redaction = cast(dict[str, Any], parameters["redaction"])
    channels = cast(list[str], redaction["channels"])

    with pytest.raises(TypeError):
        parameters["new"] = True
    with pytest.raises(TypeError):
        redaction["style"] = "blur"
    with pytest.raises(TypeError):
        channels[0] = "audio"

    assert plan.digest == make_privacy_plan_digest(
        plan.input_hash,
        plan.profile,
        plan.effective_config,
        plan.risks,
        plan.actions,
        plan.artifacts,
    )


def test_model_copy_revalidates_deeply_mutable_updates() -> None:
    """Validated copy updates cannot reintroduce mutable JSON containers."""
    plan = make_plan_with_nested_parameters()
    copied = plan.actions[0].model_copy(
        update={"parameters": {"redaction": {"channels": ["video"]}}}
    )
    parameters = cast(dict[str, Any], copied.parameters)
    redaction = cast(dict[str, Any], parameters["redaction"])
    channels = cast(list[str], redaction["channels"])

    with pytest.raises(TypeError):
        channels[0] = "audio"


def test_deeply_immutable_plan_serializes_without_warnings() -> None:
    """Frozen runtime containers retain clean canonical JSON serialization."""
    plan = make_plan_with_nested_parameters()

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        dumped = plan.model_dump(mode="json")

    assert dumped["actions"][0]["parameters"]["redaction"]["channels"] == ["video"]


def test_artifact_path_rejects_current_directory() -> None:
    """The current-directory marker is not an artifact file path."""
    with pytest.raises(ValidationError, match="normalized relative POSIX path"):
        PrivacyArtifact(
            relative_path=".",
            sha256="b" * 64,
            description="Invalid artifact",
        )


def test_normalized_box_rejects_inverted_coordinates() -> None:
    """Inverted coordinates must not create a zero or negative-area region."""
    with pytest.raises(ValueError):
        NormalizedBox(x_min=0.6, y_min=0.2, x_max=0.4, y_max=0.5)


def test_risk_map_sorts_risks_deterministically() -> None:
    """Caller order cannot change the public risk-map order."""
    risk_map = PrivacyRiskMap.model_validate(make_unsorted_risk_map_payload())

    assert [risk.id for risk in risk_map.risks] == expected_sorted_ids()


def test_public_risk_map_removes_private_evidence() -> None:
    """Public summaries must never carry source-sensitive review evidence."""
    risk_map = PrivacyRiskMap(
        input_hash="a" * 64,
        profile="public",
        duration_seconds=4.0,
        risks=(make_risk(private_evidence=({"ocr_text": "person@example.com"},)),),
    )

    public = risk_map.public_summary()

    assert public.risks[0].private_evidence == ()


def test_allow_decision_forbids_redaction_style() -> None:
    """An allowed risk cannot retain a conflicting redaction instruction."""
    with pytest.raises(ValidationError, match="ALLOW"):
        make_risk(decision=PrivacyDecision.ALLOW, style=RedactionStyle.PIXELATE)


def test_redact_decision_requires_an_applicable_style() -> None:
    """A redaction decision without a rendering instruction is incomplete."""
    with pytest.raises(ValidationError, match="REDACT"):
        make_risk(decision=PrivacyDecision.REDACT)


def test_public_artifact_path_cannot_escape_share_package() -> None:
    """A public artifact must use an output-root-relative POSIX path."""
    with pytest.raises(ValidationError, match="normalized relative POSIX path"):
        PrivacyArtifact(
            relative_path="C:/Users/example/share-safe.mp4",
            sha256="b" * 64,
            description="Shared copy",
        )


def test_top_level_documents_bind_the_deterministic_plan_digest() -> None:
    """Plan, change log, and technical report use one complete public contract."""
    risk_map = PrivacyRiskMap(
        input_hash="a" * 64,
        profile="public",
        duration_seconds=4.0,
        risks=(
            make_risk(decision=PrivacyDecision.REDACT, style=RedactionStyle.PIXELATE),
        ),
    )
    config = PrivacyEffectiveConfig(preview_seconds=3.0, guard_pixels=12)
    action = PrivacyAction(
        id="redact-qr",
        version="1.0.0",
        kind=PrivacyActionKind.VISUAL_REDACTION,
        start_seconds=1.25,
        end_seconds=2.5,
        box=risk_map.risks[0].box,
        parameters={"style": "pixelate"},
        changes_semantics=True,
        requires_confirmation=True,
    )
    artifact = PrivacyArtifact(
        relative_path="share-safe.mp4",
        sha256="b" * 64,
        description="Redacted sharing copy",
    )
    digest = make_privacy_plan_digest(
        input_hash=risk_map.input_hash,
        profile=risk_map.profile,
        effective_config=config,
        risks=risk_map.risks,
        actions=(action,),
        artifacts=(artifact,),
    )
    plan = PrivacyPlan(
        input_hash=risk_map.input_hash,
        profile=risk_map.profile,
        effective_config=config,
        risks=risk_map.risks,
        actions=(action,),
        artifacts=(artifact,),
        digest=digest,
    )
    review = PrivacyReviewDecision(
        risk_id=risk_map.risks[0].id,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.PIXELATE,
        edited_box=risk_map.risks[0].box,
        reviewed_at=datetime(2026, 8, 2, tzinfo=UTC),
    )
    change_log = PrivacyChangeLog(plan_digest=plan.digest, actions=plan.actions)
    verification = make_passed_verification(plan.digest)
    report = PrivacyTechnicalReport(
        plan_digest=plan.digest,
        verification=verification,
        artifacts=(artifact,),
    )

    assert review.reviewed_at.tzinfo is UTC
    assert change_log.actions == (action,)
    assert report.verification.status is PrivacyJobOutcome.COMPLETED


def test_crop_action_requires_a_matching_static_full_duration_risk() -> None:
    """A partial crop action must not bypass the full-duration crop constraint."""
    box = NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.5)
    risk_map = PrivacyRiskMap(
        input_hash="a" * 64,
        profile="public",
        duration_seconds=4.0,
        risks=(
            make_risk(
                start_seconds=0.0,
                end_seconds=4.0,
                box=box,
                decision=PrivacyDecision.REDACT,
                style=RedactionStyle.CROP,
            ),
        ),
    )
    config = PrivacyEffectiveConfig()
    partial_crop = PrivacyAction(
        id="crop",
        version="1.0.0",
        kind=PrivacyActionKind.CROP,
        start_seconds=1.0,
        end_seconds=3.0,
        box=box,
        parameters={},
        changes_semantics=True,
        requires_confirmation=True,
    )
    artifact = PrivacyArtifact(
        relative_path="share-safe.mp4",
        sha256="b" * 64,
        description="Shared copy",
    )

    with pytest.raises(ValidationError, match="static full-duration"):
        PrivacyPlan(
            input_hash=risk_map.input_hash,
            profile=risk_map.profile,
            effective_config=config,
            risks=risk_map.risks,
            actions=(partial_crop,),
            artifacts=(artifact,),
            digest=make_privacy_plan_digest(
                risk_map.input_hash,
                risk_map.profile,
                config,
                risk_map.risks,
                (partial_crop,),
                (artifact,),
            ),
        )


def test_privacy_plan_rejects_private_review_evidence() -> None:
    """A serializable plan cannot carry evidence reserved for private review."""
    risk = make_risk(private_evidence=({"ocr_text": "person@example.com"},))
    config = PrivacyEffectiveConfig()
    artifact = PrivacyArtifact(
        relative_path="share-safe.mp4",
        sha256="b" * 64,
        description="Shared copy",
    )

    with pytest.raises(ValidationError, match="private evidence"):
        PrivacyPlan(
            input_hash="a" * 64,
            profile="public",
            effective_config=config,
            risks=(risk,),
            actions=(),
            artifacts=(artifact,),
            digest=make_privacy_plan_digest(
                "a" * 64,
                "public",
                config,
                (risk,),
                (),
                (artifact,),
            ),
        )


def test_plan_rejects_crop_shorter_than_explicit_source_duration() -> None:
    """A direct plan cannot bypass the full-duration crop contract."""
    box = NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.5)
    crop_risk = make_risk(
        start_seconds=0.0,
        end_seconds=3.0,
        box=box,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.CROP,
    )
    config = PrivacyEffectiveConfig()
    crop_action = PrivacyAction(
        id="crop",
        version="1.0.0",
        kind=PrivacyActionKind.CROP,
        start_seconds=0.0,
        end_seconds=3.0,
        box=box,
        parameters={},
        changes_semantics=True,
        requires_confirmation=True,
    )
    artifact = PrivacyArtifact(
        relative_path="share-safe.mp4",
        sha256="b" * 64,
        description="Shared copy",
    )

    with pytest.raises(ValidationError, match="static full-duration"):
        PrivacyPlan(
            input_hash="a" * 64,
            profile="public",
            duration_seconds=4.0,
            effective_config=config,
            risks=(crop_risk,),
            actions=(crop_action,),
            artifacts=(artifact,),
            digest=make_privacy_plan_digest(
                "a" * 64,
                "public",
                config,
                (crop_risk,),
                (crop_action,),
                (artifact,),
            ),
        )


def test_top_level_documents_reject_unknown_schema_versions() -> None:
    """A future document version cannot silently be treated as schema 0.1."""
    payload = make_unsorted_risk_map_payload()
    payload["schema_version"] = "0.2"

    with pytest.raises(ValidationError, match="0.1"):
        PrivacyRiskMap.model_validate(payload)


def test_technical_report_requires_matching_verification_plan_digest() -> None:
    """A public technical report cannot combine records from two plans."""
    verification = make_passed_verification("c" * 64)

    with pytest.raises(ValidationError, match="plan digest"):
        PrivacyTechnicalReport(
            plan_digest="b" * 64,
            verification=verification,
        )


def test_change_log_cannot_claim_that_the_source_was_modified() -> None:
    """Safe Sharing must never serialize a source-modifying execution."""
    with pytest.raises(ValidationError, match="False"):
        PrivacyChangeLog.model_validate(
            {
                "plan_digest": "a" * 64,
                "source_modified": True,
            }
        )
