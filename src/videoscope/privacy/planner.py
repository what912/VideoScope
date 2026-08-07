"""Deterministic, review-gated planning for Safe Sharing."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from typing import Any

from videoscope.domain import Severity
from videoscope.privacy.errors import PrivacyPlanError
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyAction,
    PrivacyActionKind,
    PrivacyDecision,
    PrivacyEffectiveConfig,
    PrivacyPlan,
    PrivacyReviewDecision,
    PrivacyRisk,
    PrivacyRiskMap,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_plan_digest,
)
from videoscope.privacy.profiles import ShareAudienceProfile

_ACTION_VERSION = "1.0.0"
_HIGH_RISK = frozenset({Severity.HIGH, Severity.CRITICAL})
_VISUAL_STYLES = frozenset(
    {
        RedactionStyle.BLUR,
        RedactionStyle.PIXELATE,
        RedactionStyle.SOLID_FILL,
    }
)


def build_privacy_plan(
    risk_map: PrivacyRiskMap,
    reviews: Sequence[PrivacyReviewDecision],
    profile: ShareAudienceProfile,
    config: PrivacyEffectiveConfig,
) -> PrivacyPlan:
    """Build one immutable plan from explicit reviews and profile policy."""
    if risk_map.profile != profile.id:
        raise PrivacyPlanError("risk map profile does not match selected profile")
    config = config.model_copy(
        update={
            "default_visual_style": profile.default_visual_style,
            "profile_version": profile.version,
            "qr_handling": profile.qr_handling,
        }
    )

    review_by_id = _index_reviews(risk_map, reviews)
    reviewed_risks = tuple(
        _apply_review(risk, review_by_id.get(risk.id), profile)
        for risk in risk_map.risks
    )
    unresolved = tuple(
        risk.id
        for risk in reviewed_risks
        if risk.severity in _HIGH_RISK and risk.decision is PrivacyDecision.UNREVIEWED
    )
    if unresolved:
        raise PrivacyPlanError(
            "unreviewed high-risk privacy observations: " + ", ".join(unresolved)
        )

    actions = _build_actions(reviewed_risks, profile, config, risk_map.duration_seconds)
    digest = make_privacy_plan_digest(
        risk_map.input_hash,
        profile.id,
        config,
        reviewed_risks,
        actions,
        (),
        duration_seconds=risk_map.duration_seconds,
    )
    return PrivacyPlan(
        input_hash=risk_map.input_hash,
        profile=profile.id,
        duration_seconds=risk_map.duration_seconds,
        effective_config=config,
        risks=reviewed_risks,
        actions=actions,
        artifacts=(),
        digest=digest,
    )


def _index_reviews(
    risk_map: PrivacyRiskMap,
    reviews: Sequence[PrivacyReviewDecision],
) -> dict[str, PrivacyReviewDecision]:
    known = {risk.id for risk in risk_map.risks}
    indexed: dict[str, PrivacyReviewDecision] = {}
    for review in reviews:
        if review.risk_id not in known:
            raise PrivacyPlanError("review references an unknown risk")
        if review.risk_id in indexed:
            raise PrivacyPlanError("duplicate review for one privacy risk")
        indexed[review.risk_id] = review
    return indexed


def _apply_review(
    risk: PrivacyRisk,
    review: PrivacyReviewDecision | None,
    profile: ShareAudienceProfile,
) -> PrivacyRisk:
    if review is None:
        if (
            risk.decision is PrivacyDecision.UNREVIEWED
            and risk.risk_type in {PrivacyRiskType.QR_CODE, PrivacyRiskType.BARCODE}
            and profile.qr_handling == "redact_by_default"
        ):
            recommended = risk.recommended_style
            style = (
                recommended
                if recommended in _VISUAL_STYLES
                else profile.default_visual_style
            )
            return risk.model_copy(
                update={
                    "decision": PrivacyDecision.REDACT,
                    "style": style,
                    "private_evidence": (),
                }
            )
        return risk.model_copy(update={"private_evidence": ()})
    if review.decision is PrivacyDecision.UNREVIEWED:
        return risk.model_copy(
            update={
                "decision": PrivacyDecision.UNREVIEWED,
                "style": None,
                "private_evidence": (),
            }
        )
    update: dict[str, Any] = {
        "decision": review.decision,
        "style": review.style,
        "private_evidence": (),
    }
    if review.edited_box is not None:
        if risk.box is None:
            raise PrivacyPlanError("review cannot add a visual box to a nonvisual risk")
        update["box"] = review.edited_box
    try:
        return risk.model_copy(update=update)
    except ValueError as exc:
        raise PrivacyPlanError(
            "review decision is not applicable to this risk"
        ) from exc


def _build_actions(
    risks: tuple[PrivacyRisk, ...],
    profile: ShareAudienceProfile,
    config: PrivacyEffectiveConfig,
    duration_seconds: float,
) -> tuple[PrivacyAction, ...]:
    actions: list[PrivacyAction] = []
    if profile.forbidden_metadata_categories:
        actions.append(
            _action(
                PrivacyActionKind.REMOVE_METADATA,
                0.0,
                duration_seconds,
                parameters={
                    "categories": list(profile.forbidden_metadata_categories),
                    "remove_chapters": True,
                    "remove_stream_metadata": True,
                },
                changes_semantics=False,
            )
        )

    missing_visual_box = tuple(
        risk.id
        for risk in risks
        if risk.decision is PrivacyDecision.REDACT
        and risk.style in {*_VISUAL_STYLES, RedactionStyle.CROP}
        and risk.box is None
    )
    if missing_visual_box:
        raise PrivacyPlanError(
            "visual redaction requires a normalized box: "
            + ", ".join(missing_visual_box)
        )

    crops = sorted(
        (
            risk
            for risk in risks
            if risk.decision is PrivacyDecision.REDACT
            and risk.style is RedactionStyle.CROP
        ),
        key=_risk_interval_key,
    )
    if crops and any(risk.box != crops[0].box for risk in crops[1:]):
        raise PrivacyPlanError("conflicting crop regions cannot form one plan")
    if crops:
        risk = crops[0]
        if (
            risk.box is None
            or risk.track_id is not None
            or risk.start_seconds != 0.0
            or risk.end_seconds != duration_seconds
        ):
            raise PrivacyPlanError(
                "crop requires one static full-duration reviewed box"
            )
        actions.append(
            _action(
                PrivacyActionKind.CROP,
                risk.start_seconds,
                risk.end_seconds,
                box=risk.box,
                parameters={
                    "risk_ids": [item.id for item in crops],
                    "style": RedactionStyle.CROP.value,
                },
                changes_semantics=True,
            )
        )

    visual_risks = sorted(
        (
            risk
            for risk in risks
            if risk.decision is PrivacyDecision.REDACT and risk.style in _VISUAL_STYLES
        ),
        key=_risk_interval_key,
    )
    for group in _merge_adjacent(visual_risks, require_same_box=True):
        first = group[0]
        assert first.box is not None
        assert first.style is not None
        parameters: dict[str, Any] = {
            "guard_pixels": config.guard_pixels,
            "risk_ids": [risk.id for risk in group],
            "style": first.style.value,
            "track_id": first.track_id,
        }
        keyframes = _public_visual_keyframes(group)
        if keyframes:
            parameters["keyframes"] = keyframes
        actions.append(
            _action(
                PrivacyActionKind.VISUAL_REDACTION,
                first.start_seconds,
                group[-1].end_seconds,
                box=first.box,
                parameters=parameters,
                changes_semantics=True,
            )
        )

    audio_risks = sorted(
        (
            risk
            for risk in risks
            if risk.decision is PrivacyDecision.REDACT
            and risk.risk_type is PrivacyRiskType.MANUAL_AUDIO
            and risk.style is RedactionStyle.MUTE
        ),
        key=_risk_interval_key,
    )
    for group in _merge_adjacent(audio_risks, require_same_box=False):
        actions.append(
            _action(
                PrivacyActionKind.AUDIO_MUTE,
                group[0].start_seconds,
                group[-1].end_seconds,
                parameters={
                    "risk_ids": [risk.id for risk in group],
                    "style": RedactionStyle.MUTE.value,
                },
                changes_semantics=True,
            )
        )

    actions.append(
        _action(
            PrivacyActionKind.REMUX,
            0.0,
            duration_seconds,
            parameters={"explicit_mapping": True, "strip_metadata": True},
            changes_semantics=False,
        )
    )
    actions.append(
        _action(
            PrivacyActionKind.VERIFY,
            0.0,
            duration_seconds,
            parameters={"checks": list(config.verification_policy)},
            changes_semantics=False,
        )
    )
    return tuple(actions)


def _merge_adjacent(
    risks: Iterable[PrivacyRisk],
    *,
    require_same_box: bool,
) -> tuple[tuple[PrivacyRisk, ...], ...]:
    groups: list[list[PrivacyRisk]] = []
    for risk in risks:
        if not groups or not _can_merge(groups[-1][-1], risk, require_same_box):
            groups.append([risk])
        else:
            groups[-1].append(risk)
    return tuple(tuple(group) for group in groups)


def _can_merge(
    previous: PrivacyRisk,
    current: PrivacyRisk,
    require_same_box: bool,
) -> bool:
    return (
        previous.end_seconds == current.start_seconds
        and previous.style is current.style
        and previous.track_id == current.track_id
        and (not require_same_box or previous.box == current.box)
    )


def _risk_interval_key(risk: PrivacyRisk) -> tuple[float, float, str]:
    return (risk.start_seconds, risk.end_seconds, risk.id)


def _public_visual_keyframes(
    risks: Sequence[PrivacyRisk],
) -> list[dict[str, Any]]:
    """Extract only deterministic timestamp/box pairs from public evidence."""
    by_timestamp: dict[float, NormalizedBox] = {}
    for risk in risks:
        for evidence in risk.evidence:
            timestamp = evidence.get("timestamp_seconds")
            raw_box = evidence.get("box")
            if (
                isinstance(timestamp, bool)
                or not isinstance(timestamp, (int, float))
                or not math.isfinite(float(timestamp))
                or float(timestamp) < risk.start_seconds
                or float(timestamp) > risk.end_seconds
                or not isinstance(raw_box, Mapping)
            ):
                continue
            try:
                box = NormalizedBox.model_validate(dict(raw_box))
            except ValueError:
                continue
            normalized_timestamp = float(timestamp)
            previous = by_timestamp.get(normalized_timestamp)
            if previous is not None and previous != box:
                raise PrivacyPlanError(
                    "visual evidence has conflicting boxes at one timestamp"
                )
            by_timestamp[normalized_timestamp] = box
    return [
        {
            "timestamp_seconds": timestamp,
            "box": by_timestamp[timestamp].model_dump(mode="json"),
        }
        for timestamp in sorted(by_timestamp)
    ]


def _action(
    kind: PrivacyActionKind,
    start_seconds: float,
    end_seconds: float,
    *,
    box: NormalizedBox | None = None,
    parameters: dict[str, Any],
    changes_semantics: bool,
) -> PrivacyAction:
    identity = {
        "box": box.model_dump(mode="json") if box is not None else None,
        "end_seconds": end_seconds,
        "kind": kind.value,
        "parameters": parameters,
        "start_seconds": start_seconds,
        "version": _ACTION_VERSION,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return PrivacyAction(
        id="privacy_action_" + sha256(encoded).hexdigest(),
        version=_ACTION_VERSION,
        kind=kind,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        box=box,
        parameters=parameters,
        changes_semantics=changes_semantics,
        requires_confirmation=changes_semantics,
    )


__all__ = ["build_privacy_plan"]
