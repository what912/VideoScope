"""Tests for deterministic, review-gated Safe Sharing planning."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from videoscope.domain import Severity
from videoscope.privacy.errors import PrivacyPlanError
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyActionKind,
    PrivacyDecision,
    PrivacyEffectiveConfig,
    PrivacyReviewDecision,
    PrivacyRisk,
    PrivacyRiskMap,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_plan_digest,
    make_privacy_risk_id,
)
from videoscope.privacy.planner import build_privacy_plan
from videoscope.privacy.profiles import get_share_audience_profile


def _risk(
    *,
    risk_type: PrivacyRiskType,
    start: float,
    end: float,
    box: NormalizedBox | None = None,
    severity: Severity = Severity.HIGH,
    decision: PrivacyDecision = PrivacyDecision.UNREVIEWED,
    style: RedactionStyle | None = None,
    scanner_id: str = "scanner",
    track_id: str | None = None,
    evidence: tuple[dict[str, Any], ...] | None = None,
) -> PrivacyRisk:
    risk_id = make_privacy_risk_id(
        "a" * 64,
        scanner_id,
        risk_type,
        start,
        end,
        box,
    )
    return PrivacyRisk(
        id=risk_id,
        scanner_id=scanner_id,
        scanner_version="1.0.0",
        risk_type=risk_type,
        title="Reviewable privacy observation",
        public_description="A local heuristic proposed this region for review.",
        severity=severity,
        confidence=0.8,
        start_seconds=start,
        end_seconds=end,
        box=box,
        track_id=track_id,
        recommended_style=(
            RedactionStyle.MUTE
            if risk_type is PrivacyRiskType.MANUAL_AUDIO
            else RedactionStyle.BLUR
        ),
        decision=decision,
        style=style,
        limitations=("This proposal may be incomplete.",),
        evidence=evidence or ({"timestamp_seconds": start},),
    )


def _risk_map(*risks: PrivacyRisk, profile: str = "public") -> PrivacyRiskMap:
    return PrivacyRiskMap(
        input_hash="a" * 64,
        profile=profile,
        duration_seconds=10.0,
        risks=risks,
    )


def _review(
    risk: PrivacyRisk,
    *,
    decision: PrivacyDecision = PrivacyDecision.REDACT,
    style: RedactionStyle | None = RedactionStyle.BLUR,
    edited_box: NormalizedBox | None = None,
) -> PrivacyReviewDecision:
    return PrivacyReviewDecision(
        risk_id=risk.id,
        decision=decision,
        style=style,
        edited_box=edited_box,
        reviewed_at=datetime(2026, 8, 2, tzinfo=UTC),
    )


def test_plan_rejects_unreviewed_high_risk() -> None:
    risk = _risk(
        risk_type=PrivacyRiskType.FACE_REGION,
        start=1.0,
        end=2.0,
        box=NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4),
    )

    with pytest.raises(PrivacyPlanError) as error:
        build_privacy_plan(
            risk_map=_risk_map(risk),
            reviews=(),
            profile=get_share_audience_profile("public"),
            config=PrivacyEffectiveConfig(),
        )

    assert "unreviewed high-risk" in (error.value.internal_message or "")


def test_plan_digest_covers_reviewed_regions_and_effective_config() -> None:
    original_box = NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4)
    edited_box = NormalizedBox(x_min=0.2, y_min=0.2, x_max=0.5, y_max=0.6)
    risk = _risk(
        risk_type=PrivacyRiskType.FACE_REGION,
        start=1.0,
        end=2.0,
        box=original_box,
    )
    plan = build_privacy_plan(
        risk_map=_risk_map(risk),
        reviews=(_review(risk, edited_box=edited_box),),
        profile=get_share_audience_profile("public"),
        config=PrivacyEffectiveConfig(),
    )
    changed_action = plan.actions[1].model_copy(update={"box": original_box})
    changed_actions = (plan.actions[0], changed_action, *plan.actions[2:])
    changed_config = plan.effective_config.model_copy(update={"guard_pixels": 24})

    changed_box_digest = make_privacy_plan_digest(
        plan.input_hash,
        plan.profile,
        plan.effective_config,
        plan.risks,
        changed_actions,
        plan.artifacts,
        duration_seconds=plan.duration_seconds,
    )
    changed_config_digest = make_privacy_plan_digest(
        plan.input_hash,
        plan.profile,
        changed_config,
        plan.risks,
        plan.actions,
        plan.artifacts,
        duration_seconds=plan.duration_seconds,
    )

    assert plan.digest != changed_box_digest
    assert plan.digest != changed_config_digest


def test_plan_orders_actions_and_merges_only_exactly_adjacent_regions() -> None:
    box = NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4)
    first = _risk(
        risk_type=PrivacyRiskType.MANUAL_VISUAL,
        start=1.0,
        end=2.0,
        box=box,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.PIXELATE,
        scanner_id="manual_visual_region",
    )
    adjacent = _risk(
        risk_type=PrivacyRiskType.MANUAL_VISUAL,
        start=2.0,
        end=3.0,
        box=box,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.PIXELATE,
        scanner_id="manual_visual_region",
    )
    separated = _risk(
        risk_type=PrivacyRiskType.MANUAL_VISUAL,
        start=3.25,
        end=4.0,
        box=box,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.PIXELATE,
        scanner_id="manual_visual_region",
    )
    audio = _risk(
        risk_type=PrivacyRiskType.MANUAL_AUDIO,
        start=5.0,
        end=6.0,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.MUTE,
        scanner_id="manual_audio_interval",
    )

    plan = build_privacy_plan(
        risk_map=_risk_map(separated, audio, adjacent, first),
        reviews=(),
        profile=get_share_audience_profile("public"),
        config=PrivacyEffectiveConfig(),
    )

    assert [action.kind for action in plan.actions] == [
        PrivacyActionKind.REMOVE_METADATA,
        PrivacyActionKind.VISUAL_REDACTION,
        PrivacyActionKind.VISUAL_REDACTION,
        PrivacyActionKind.AUDIO_MUTE,
        PrivacyActionKind.REMUX,
        PrivacyActionKind.VERIFY,
    ]
    assert (plan.actions[1].start_seconds, plan.actions[1].end_seconds) == (1.0, 3.0)
    assert plan.actions[1].model_dump(mode="json")["parameters"]["risk_ids"] == [
        first.id,
        adjacent.id,
    ]
    assert (plan.actions[2].start_seconds, plan.actions[2].end_seconds) == (
        3.25,
        4.0,
    )
    assert all(
        action.requires_confirmation
        for action in plan.actions
        if action.changes_semantics
    )


def test_visual_action_contains_sorted_public_keyframes_and_digest() -> None:
    union = NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.7, y_max=0.6)
    first = NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.3, y_max=0.4)
    second = NormalizedBox(x_min=0.5, y_min=0.3, x_max=0.7, y_max=0.6)
    risk = _risk(
        risk_type=PrivacyRiskType.FACE_REGION,
        start=1.0,
        end=2.0,
        box=union,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.BLUR,
        track_id="face_track_01",
        evidence=(
            {
                "timestamp_seconds": 2.0,
                "relative_path": "private/evidence.png",
                "box": second.model_dump(mode="json"),
            },
            {
                "timestamp_seconds": 1.0,
                "sample_index": 7,
                "box": first.model_dump(mode="json"),
            },
        ),
    )

    plan = build_privacy_plan(
        _risk_map(risk),
        (),
        get_share_audience_profile("public"),
        PrivacyEffectiveConfig(),
    )
    visual = next(
        action
        for action in plan.actions
        if action.kind is PrivacyActionKind.VISUAL_REDACTION
    )

    serialized_keyframes = visual.model_dump(mode="json")["parameters"]["keyframes"]
    assert serialized_keyframes == [
        {"timestamp_seconds": 1.0, "box": first.model_dump(mode="json")},
        {"timestamp_seconds": 2.0, "box": second.model_dump(mode="json")},
    ]
    assert "relative_path" not in str(serialized_keyframes)
    assert "sample_index" not in str(serialized_keyframes)

    changed = risk.model_copy(
        update={
            "evidence": (
                {
                    "timestamp_seconds": 1.0,
                    "box": second.model_dump(mode="json"),
                },
                {
                    "timestamp_seconds": 2.0,
                    "box": first.model_dump(mode="json"),
                },
            )
        }
    )
    changed_plan = build_privacy_plan(
        _risk_map(changed),
        (),
        get_share_audience_profile("public"),
        PrivacyEffectiveConfig(),
    )
    assert plan.digest != changed_plan.digest


def test_plan_rejects_review_for_unknown_risk() -> None:
    risk = _risk(
        risk_type=PrivacyRiskType.FACE_REGION,
        start=1.0,
        end=2.0,
        box=NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4),
    )
    unknown_review = _review(risk).model_copy(
        update={"risk_id": "privacy_risk_" + "b" * 64}
    )

    with pytest.raises(PrivacyPlanError) as error:
        build_privacy_plan(
            risk_map=_risk_map(risk),
            reviews=(unknown_review,),
            profile=get_share_audience_profile("public"),
            config=PrivacyEffectiveConfig(),
        )

    assert "unknown risk" in (error.value.internal_message or "")


def test_plan_rejects_conflicting_full_duration_crops() -> None:
    first_box = NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.9, y_max=0.9)
    second_box = NormalizedBox(x_min=0.2, y_min=0.1, x_max=0.8, y_max=0.9)
    first = _risk(
        risk_type=PrivacyRiskType.MANUAL_VISUAL,
        start=0.0,
        end=10.0,
        box=first_box,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.CROP,
        scanner_id="manual_crop_one",
    )
    second = _risk(
        risk_type=PrivacyRiskType.MANUAL_VISUAL,
        start=0.0,
        end=10.0,
        box=second_box,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.CROP,
        scanner_id="manual_crop_two",
    )

    with pytest.raises(PrivacyPlanError) as error:
        build_privacy_plan(
            _risk_map(first, second),
            (),
            get_share_audience_profile("public"),
            PrivacyEffectiveConfig(),
        )

    assert "conflicting crop" in (error.value.internal_message or "")


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "style",
    (
        RedactionStyle.BLUR,
        RedactionStyle.PIXELATE,
        RedactionStyle.SOLID_FILL,
        RedactionStyle.CROP,
    ),
)
def test_plan_rejects_visual_redaction_without_box(
    style: RedactionStyle,
) -> None:
    risk = _risk(
        risk_type=PrivacyRiskType.FACE_REGION,
        start=0.0 if style is RedactionStyle.CROP else 1.0,
        end=10.0 if style is RedactionStyle.CROP else 2.0,
    )

    with pytest.raises(PrivacyPlanError) as error:
        build_privacy_plan(
            _risk_map(risk),
            (_review(risk, style=style),),
            get_share_audience_profile("public"),
            PrivacyEffectiveConfig(),
        )

    assert "visual redaction requires a normalized box" in (
        error.value.internal_message or ""
    )


def test_qr_profile_policy_redacts_by_default_or_requires_review() -> None:
    box = NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.4, y_max=0.4)
    public_risk = _risk(
        risk_type=PrivacyRiskType.QR_CODE,
        start=1.0,
        end=2.0,
        box=box,
        scanner_id="qr_barcode_region",
    ).model_copy(update={"recommended_style": RedactionStyle.PIXELATE})
    public_plan = build_privacy_plan(
        _risk_map(public_risk),
        (),
        get_share_audience_profile("public"),
        PrivacyEffectiveConfig(),
    )

    assert public_plan.risks[0].decision is PrivacyDecision.REDACT
    assert public_plan.risks[0].style is RedactionStyle.PIXELATE
    assert public_plan.effective_config.qr_handling == "redact_by_default"
    assert any(
        action.kind is PrivacyActionKind.VISUAL_REDACTION
        and action.model_dump(mode="json")["parameters"]["risk_ids"] == [public_risk.id]
        for action in public_plan.actions
    )
    review_policy_config = public_plan.effective_config.model_copy(
        update={"qr_handling": "review"}
    )
    review_policy_digest = make_privacy_plan_digest(
        public_plan.input_hash,
        public_plan.profile,
        review_policy_config,
        public_plan.risks,
        public_plan.actions,
        public_plan.artifacts,
        duration_seconds=public_plan.duration_seconds,
    )
    assert public_plan.digest != review_policy_digest

    allowed_plan = build_privacy_plan(
        _risk_map(public_risk),
        (_review(public_risk, decision=PrivacyDecision.ALLOW, style=None),),
        get_share_audience_profile("public"),
        PrivacyEffectiveConfig(),
    )
    assert allowed_plan.risks[0].decision is PrivacyDecision.ALLOW
    allowed_risk_ids: list[str] = []
    for action in allowed_plan.actions:
        parameters = action.model_dump(mode="json")["parameters"]
        assert isinstance(parameters, dict)
        risk_ids = parameters.get("risk_ids", [])
        assert isinstance(risk_ids, list)
        allowed_risk_ids.extend(str(risk_id) for risk_id in risk_ids)
    assert public_risk.id not in allowed_risk_ids

    review_risk = public_risk.model_copy(update={"private_evidence": ()})
    with pytest.raises(PrivacyPlanError) as error:
        build_privacy_plan(
            _risk_map(review_risk, profile="work_client"),
            (),
            get_share_audience_profile("work_client"),
            PrivacyEffectiveConfig(),
        )

    assert "unreviewed high-risk" in (error.value.internal_message or "")
