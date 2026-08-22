"""Deterministic, review-gated planning for local Video Rescue."""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.rescue.action_roles import REMAINING_IMPROVEMENT_ACTION_KINDS
from videoscope.rescue.audio import AudioAssessment, FixedOffsetAssessment
from videoscope.rescue.capabilities import (
    ActionCapabilityDecision,
    action_verification_mode,
    capability_review_warning,
    evaluate_action_capabilities,
)
from videoscope.rescue.deblur import BlurKernelEstimate, DeblurConfig
from videoscope.rescue.errors import RescuePlanError
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    RescueSymptom,
    canonical_video_encode_contract,
    make_rescue_action_id,
    make_rescue_plan_digest,
    rescue_public_artifacts,
)
from videoscope.rescue.qualification import (
    SHARPEN_QUALIFICATION_LIMITATION,
    SHARPEN_QUALIFICATION_UNAVAILABLE_LIMITATION,
    SharpenQualificationEvidenceV1,
    apply_qualified_sharpen_profile,
    qualification_action_parameters,
)
from videoscope.rescue.stabilization import (
    StabilizationAssessment,
    StabilizationConfig,
    StabilizationQualificationEvidenceV1,
    stabilization_qualification_action_parameters,
)
from videoscope.rescue.timeline import timestamp_in_half_open_range
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    validate_tonal_profile_contracts,
)
from videoscope.rescue.tonal_qualification import (
    TONAL_ENCODED_QUALIFICATION_LIMITATION,
    TONAL_ENCODED_QUALIFICATION_UNAVAILABLE_LIMITATION,
    TonalEncodedQualificationEvidenceV3,
    qualified_tonal_action_parameters,
)
from videoscope.rescue.visual import (
    FlickerCorrectionPlan,
    LumaAdjustmentConfig,
    SharpenConfig,
    VisualAssessment,
    VisualAssessmentConfig,
    VisualEvidence,
    VisualMetrics,
    apply_luma_strength_limit,
    derive_visual_action_parameters,
    validate_luma_action_evidence,
    visual_action_parameters,
)

_ACTION_VERSION = "1"
_MINIMUM_PREVIEW_SECONDS = 1e-6
_ACTION_DAMAGE_KINDS: dict[RescueActionKind, frozenset[DamageKind]] = {
    RescueActionKind.REBUILD_TIMESTAMPS: frozenset(
        {DamageKind.TIMESTAMP_DISCONTINUITY}
    ),
    RescueActionKind.SELECT_TRACKS: frozenset({DamageKind.MISSING_STREAM}),
    RescueActionKind.SALVAGE_SEGMENTS: frozenset({DamageKind.UNDECODABLE}),
    RescueActionKind.TRIM_DAMAGED_EDGES: frozenset({DamageKind.UNDECODABLE}),
    RescueActionKind.CORRECT_FIXED_AV_OFFSET: frozenset({DamageKind.FIXED_AV_OFFSET}),
    RescueActionKind.ADJUST_LUMA: frozenset({DamageKind.DARK}),
    RescueActionKind.DENOISE_VIDEO: frozenset({DamageKind.VIDEO_NOISE}),
    RescueActionKind.SHARPEN: frozenset({DamageKind.SOFT_DETAIL}),
    RescueActionKind.DEFLICKER: frozenset({DamageKind.FLICKER}),
    RescueActionKind.STABILIZE: frozenset({DamageKind.SHAKE}),
    RescueActionKind.NORMALIZE_AUDIO: frozenset(
        {DamageKind.LOW_LOUDNESS, DamageKind.AUDIO_CLIPPING}
    ),
    RescueActionKind.DENOISE_AUDIO: frozenset({DamageKind.AUDIO_NOISE}),
    RescueActionKind.DEBLUR: frozenset({DamageKind.SOFT_DETAIL}),
}
_SYMPTOM_ACTION_KINDS: dict[RescueSymptom, frozenset[RescueActionKind]] = {
    RescueSymptom.AUDIO_VIDEO_OFFSET: frozenset(
        {RescueActionKind.CORRECT_FIXED_AV_OFFSET}
    ),
    RescueSymptom.DARK: frozenset({RescueActionKind.ADJUST_LUMA}),
    RescueSymptom.VIDEO_NOISE: frozenset({RescueActionKind.DENOISE_VIDEO}),
    RescueSymptom.SOFT_DETAIL: frozenset(
        {RescueActionKind.SHARPEN, RescueActionKind.DEBLUR}
    ),
    RescueSymptom.FLICKER: frozenset({RescueActionKind.DEFLICKER}),
    RescueSymptom.SHAKE: frozenset({RescueActionKind.STABILIZE}),
    RescueSymptom.LOW_LOUDNESS: frozenset({RescueActionKind.NORMALIZE_AUDIO}),
    RescueSymptom.AUDIO_NOISE: frozenset({RescueActionKind.DENOISE_AUDIO}),
}


@dataclass(frozen=True, slots=True)
class _PreviewObligation:
    action: RescueAction
    action_index: int
    range_index: int
    candidate_range: tuple[float, float]
    review_priority: int


@dataclass(frozen=True, slots=True)
class _PreviewCluster:
    representative_action: RescueAction
    actions: tuple[RescueAction, ...]
    shared_range: tuple[float, float]


def build_rescue_plan(
    *,
    metadata: VideoMetadata,
    damage_map: MediaDamageMap,
    strategy: RescueStrategy,
    config: RescueEffectiveConfig,
    locked_ranges: Sequence[tuple[float, float]] = (),
    requested_symptoms: tuple[RescueSymptom, ...] = (),
    assessment_parameters: dict[str, JsonValue] | None = None,
    assessment_limitations: tuple[str, ...] = (),
    assessment_warnings: tuple[str, ...] = (),
    visual_assessment: VisualAssessment | None = None,
    flicker_correction: FlickerCorrectionPlan | None = None,
    stabilization_assessment: StabilizationAssessment | None = None,
    audio_assessment: AudioAssessment | None = None,
    fixed_offset_assessment: FixedOffsetAssessment | None = None,
    sharpen_qualification: SharpenQualificationEvidenceV1 | None = None,
    require_sharpen_qualification: bool = False,
    tonal_qualification: TonalEncodedQualificationEvidenceV3 | None = None,
    require_tonal_qualification: bool = False,
    stabilization_qualification: StabilizationQualificationEvidenceV1 | None = None,
) -> RescuePlan:
    """Build a path-free plan with stable action and preview selection order."""
    if metadata.duration_seconds != damage_map.duration_seconds:
        raise RescuePlanError("metadata duration does not match the damage map")
    locks = _normalized_ranges((*config.locked_ranges, *locked_ranges))
    effective_config = config.model_copy(update={"locked_ranges": locks})
    proposed_actions = _build_actions(
        metadata,
        damage_map,
        strategy,
        effective_config,
        locks,
        visual_assessment,
        flicker_correction,
        stabilization_assessment,
        audio_assessment,
        fixed_offset_assessment,
        requested_symptoms,
        assessment_parameters or {},
    )
    had_draft_tonal = any(
        action.kind is RescueActionKind.DENOISE_AUDIO
        and action.parameters.get("interference_profiles")
        for action in proposed_actions
    )
    proposed_actions = _apply_tonal_encoded_qualification(
        proposed_actions,
        tonal_qualification,
        input_hash=damage_map.input_hash,
        required=require_tonal_qualification,
    )
    had_draft_sharpen = any(
        action.kind is RescueActionKind.SHARPEN for action in proposed_actions
    )
    proposed_actions = _apply_sharpen_qualification(
        proposed_actions,
        sharpen_qualification,
        input_hash=damage_map.input_hash,
        config=effective_config,
        required=require_sharpen_qualification,
    )
    proposed_actions = _apply_stabilization_qualification(
        proposed_actions,
        stabilization_qualification,
        input_hash=damage_map.input_hash,
        config=effective_config,
    )
    actions, preview_ranges, omitted = _capability_gated_actions(
        proposed_actions,
        damage_map=damage_map,
        config=effective_config,
        locked_ranges=locks,
    )
    final_warnings = tuple(
        dict.fromkeys(
            (
                *assessment_warnings,
                *(
                    capability_review_warning(action, decision)
                    for action, decision in omitted
                ),
            )
        )
    )
    final_limitations = tuple(
        dict.fromkeys(
            (
                *assessment_limitations,
                *(
                    (SHARPEN_QUALIFICATION_LIMITATION,)
                    if sharpen_qualification is not None
                    and sharpen_qualification.selected is None
                    else ()
                ),
                *(
                    (SHARPEN_QUALIFICATION_UNAVAILABLE_LIMITATION,)
                    if require_sharpen_qualification
                    and sharpen_qualification is None
                    and had_draft_sharpen
                    else ()
                ),
                *(
                    (TONAL_ENCODED_QUALIFICATION_LIMITATION,)
                    if tonal_qualification is not None
                    and not tonal_qualification.passed
                    else ()
                ),
                *(
                    (TONAL_ENCODED_QUALIFICATION_UNAVAILABLE_LIMITATION,)
                    if require_tonal_qualification
                    and tonal_qualification is None
                    and had_draft_tonal
                    else ()
                ),
            )
        )
    )
    plan_without_digest: dict[str, JsonValue] = {
        "input_hash": damage_map.input_hash,
        "strategy": strategy.value,
        "requested_symptoms": [item.value for item in requested_symptoms],
        "assessment_parameters": assessment_parameters or {},
        "assessment_limitations": list(final_limitations),
        "assessment_warnings": list(final_warnings),
        "effective_config": effective_config.model_dump(mode="json"),
        "actions": [action.model_dump(mode="json") for action in actions],
        "preview_ranges": [list(item) for item in preview_ranges],
        "private_artifacts": [],
        "public_artifacts": list(
            rescue_public_artifacts(include_improved=_supports_improved(actions))
        ),
        "damage_intervals": [
            interval.model_dump(mode="json") for interval in damage_map.intervals
        ],
    }
    plan_digest = make_rescue_plan_digest(plan_without_digest)
    if (
        had_draft_sharpen
        and sharpen_qualification is None
        and not require_sharpen_qualification
    ) or (
        had_draft_tonal
        and tonal_qualification is None
        and not require_tonal_qualification
    ):
        # The only unqualified SHARPEN object is an internal draft.  Public model
        # parsing and every command/preview/executor trust boundary reject it.
        return RescuePlan.model_construct(
            input_hash=damage_map.input_hash,
            strategy=strategy,
            requested_symptoms=requested_symptoms,
            assessment_parameters=assessment_parameters or {},
            assessment_limitations=final_limitations,
            assessment_warnings=final_warnings,
            effective_config=effective_config,
            actions=actions,
            preview_ranges=preview_ranges,
            public_artifacts=rescue_public_artifacts(
                include_improved=_supports_improved(actions)
            ),
            damage_intervals=damage_map.intervals,
            plan_digest=plan_digest,
        )
    return RescuePlan(
        input_hash=damage_map.input_hash,
        strategy=strategy,
        requested_symptoms=requested_symptoms,
        assessment_parameters=assessment_parameters or {},
        assessment_limitations=final_limitations,
        assessment_warnings=final_warnings,
        effective_config=effective_config,
        actions=actions,
        preview_ranges=preview_ranges,
        public_artifacts=rescue_public_artifacts(
            include_improved=_supports_improved(actions)
        ),
        damage_intervals=damage_map.intervals,
        plan_digest=plan_digest,
    )


def _apply_tonal_encoded_qualification(
    actions: tuple[RescueAction, ...],
    evidence: TonalEncodedQualificationEvidenceV3 | None,
    *,
    input_hash: str,
    required: bool = False,
) -> tuple[RescueAction, ...]:
    """Turn one raw-qualified tonal draft into its encoded final wire."""
    if evidence is None:
        return (
            tuple(
                action
                for action in actions
                if not (
                    action.kind is RescueActionKind.DENOISE_AUDIO
                    and action.parameters.get("interference_profiles")
                )
            )
            if required
            else actions
        )
    tonal_actions = tuple(
        action
        for action in actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
        and action.parameters.get("interference_profiles")
    )
    if len(tonal_actions) != 1:
        raise RescuePlanError("tonal qualification action inventory is invalid")
    draft = tonal_actions[0]
    if (
        evidence.input_hash != input_hash
        or evidence.draft_action_id != draft.id
        or evidence.draft_parameters != draft.parameters
        or evidence.source_ranges != draft.source_ranges
    ):
        raise RescuePlanError("tonal qualification is not bound to the draft plan")
    if not evidence.passed:
        return tuple(action for action in actions if action is not draft)
    try:
        replacement = _action(
            draft.kind,
            draft.description,
            draft.source_ranges,
            qualified_tonal_action_parameters(evidence),
            draft.strategy,
            changes_content=draft.changes_content,
        )
    except (TypeError, ValueError) as exc:
        raise RescuePlanError("tonal qualification parameters are invalid") from exc
    return tuple(replacement if action is draft else action for action in actions)


def _apply_sharpen_qualification(
    actions: tuple[RescueAction, ...],
    evidence: SharpenQualificationEvidenceV1 | None,
    *,
    input_hash: str,
    config: RescueEffectiveConfig,
    required: bool = False,
) -> tuple[RescueAction, ...]:
    """Turn one draft SHARPEN action into its qualified final wire."""
    if evidence is None:
        return (
            tuple(
                action
                for action in actions
                if action.kind is not RescueActionKind.SHARPEN
            )
            if required
            else actions
        )
    sharpen_actions = tuple(
        action for action in actions if action.kind is RescueActionKind.SHARPEN
    )
    if len(sharpen_actions) != 1:
        raise RescuePlanError("SHARPEN qualification action inventory is invalid")
    draft = sharpen_actions[0]
    expected_contract = canonical_video_encode_contract(config)
    if (
        evidence.input_hash != input_hash
        or evidence.draft_action_id != draft.id
        or evidence.draft_parameters != draft.parameters
        or evidence.source_ranges != draft.source_ranges
        or evidence.encode_contract != expected_contract
        or tuple(item.profile for item in evidence.profile_measurements)
        != config.sharpen_qualification_profiles
    ):
        raise RescuePlanError("SHARPEN qualification is not bound to the draft plan")
    selected = evidence.selected
    if selected is None:
        return tuple(action for action in actions if action is not draft)
    try:
        profiled = apply_qualified_sharpen_profile(draft.parameters, selected.profile)
        profiled.update(qualification_action_parameters(evidence))
        replacement = _action(
            draft.kind,
            draft.description,
            draft.source_ranges,
            profiled,
            draft.strategy,
            changes_content=draft.changes_content,
        )
    except (TypeError, ValueError) as exc:
        raise RescuePlanError("SHARPEN qualification parameters are invalid") from exc
    return tuple(replacement if action is draft else action for action in actions)


def _apply_stabilization_qualification(
    actions: tuple[RescueAction, ...],
    evidence: StabilizationQualificationEvidenceV1 | None,
    *,
    input_hash: str,
    config: RescueEffectiveConfig,
) -> tuple[RescueAction, ...]:
    """Optionally replace one transition action with the first qualified profile."""
    if evidence is None:
        return actions
    stabilization_actions = tuple(
        action
        for action in actions
        if action.kind is RescueActionKind.STABILIZE
        and action.parameters.get("method") == "transition_anchor_v1"
    )
    if len(stabilization_actions) != 1:
        raise RescuePlanError("stabilization qualification action inventory is invalid")
    draft = stabilization_actions[0]
    if (
        evidence.input_hash != input_hash
        or evidence.draft_action_id != draft.id
        or evidence.draft_parameters != draft.parameters
        or evidence.source_ranges != draft.source_ranges
        or evidence.encode_contract != canonical_video_encode_contract(config)
        or evidence.configured_profiles != config.stabilization_qualification_profiles
    ):
        raise RescuePlanError("stabilization qualification is not bound to the draft")
    selected = evidence.selected
    if selected is None:
        return actions
    parameters = dict(selected.action_parameters)
    parameters.update(stabilization_qualification_action_parameters(evidence))
    try:
        replacement = _action(
            draft.kind,
            draft.description,
            draft.source_ranges,
            parameters,
            draft.strategy,
            changes_content=draft.changes_content,
        )
    except (TypeError, ValueError) as exc:
        raise RescuePlanError(
            "stabilization qualification parameters are invalid"
        ) from exc
    return tuple(replacement if action is draft else action for action in actions)


def _capability_gated_actions(
    proposed_actions: tuple[RescueAction, ...],
    *,
    damage_map: MediaDamageMap,
    config: RescueEffectiveConfig,
    locked_ranges: tuple[tuple[float, float], ...],
) -> tuple[
    tuple[RescueAction, ...],
    tuple[tuple[float, float], ...],
    tuple[tuple[RescueAction, ActionCapabilityDecision], ...],
]:
    actions = proposed_actions
    preview_ranges = _preview_ranges_for_actions(actions, damage_map, config)
    omitted: dict[str, tuple[RescueAction, ActionCapabilityDecision]] = {}
    while True:
        decisions = evaluate_action_capabilities(
            actions,
            preview_ranges,
            duration_seconds=damage_map.duration_seconds,
            locked_ranges=locked_ranges,
        )
        retained: list[RescueAction] = []
        for action, decision in zip(actions, decisions, strict=True):
            if (
                not action.changes_content
                or decision.automatic
                or (
                    decision.preview_supported
                    and decision.preview_covered
                    and decision.range_exact
                )
            ):
                retained.append(action)
            else:
                omitted.setdefault(action.id, (action, decision))
        retained_actions = tuple(retained)
        retained_previews = _preview_ranges_for_actions(
            retained_actions, damage_map, config
        )
        if (
            tuple(action.id for action in retained_actions)
            == tuple(action.id for action in actions)
            and retained_previews == preview_ranges
        ):
            return retained_actions, retained_previews, tuple(omitted.values())
        actions = retained_actions
        preview_ranges = retained_previews


def _preview_ranges_for_actions(
    actions: tuple[RescueAction, ...],
    damage_map: MediaDamageMap,
    config: RescueEffectiveConfig,
) -> tuple[tuple[float, float], ...]:
    return _select_preview_ranges(
        damage_map.intervals,
        actions,
        damage_map.duration_seconds,
        config,
    )


def _build_actions(
    metadata: VideoMetadata,
    damage_map: MediaDamageMap,
    strategy: RescueStrategy,
    config: RescueEffectiveConfig,
    locked_ranges: tuple[tuple[float, float], ...],
    visual_assessment: VisualAssessment | None,
    flicker_correction: FlickerCorrectionPlan | None,
    stabilization_assessment: StabilizationAssessment | None,
    audio_assessment: AudioAssessment | None,
    fixed_offset_assessment: FixedOffsetAssessment | None,
    requested_symptoms: tuple[RescueSymptom, ...],
    assessment_parameters: dict[str, JsonValue],
) -> tuple[RescueAction, ...]:
    duration_range = ((0.0, damage_map.duration_seconds),)
    by_kind = {
        kind: tuple(item for item in damage_map.intervals if item.kind is kind)
        for kind in DamageKind
    }
    requested_action_kinds = (
        frozenset(
            action_kind
            for symptom in requested_symptoms
            for action_kind in _SYMPTOM_ACTION_KINDS.get(symptom, ())
        )
        if requested_symptoms
        else None
    )
    actions = [
        _action(
            RescueActionKind.REMUX,
            "Repackage available streams without applying a visual or audio "
            "enhancement.",
            duration_range,
            {"container": "matroska"},
            strategy,
            changes_content=False,
        )
    ]
    if by_kind[DamageKind.TIMESTAMP_DISCONTINUITY]:
        actions.append(
            _action_from_intervals(
                RescueActionKind.REBUILD_TIMESTAMPS,
                "Rebuild timeline timestamps around observed discontinuities.",
                by_kind[DamageKind.TIMESTAMP_DISCONTINUITY],
                strategy,
                changes_content=False,
            )
        )
    if by_kind[DamageKind.MISSING_STREAM]:
        actions.append(
            _action_from_intervals(
                RescueActionKind.SELECT_TRACKS,
                "Select the available streams after an observed missing stream.",
                by_kind[DamageKind.MISSING_STREAM],
                strategy,
                changes_content=True,
            )
        )
    if _has_nonzero_rotation(metadata):
        actions.append(
            _action(
                RescueActionKind.NORMALIZE_ROTATION,
                "Normalize the recorded display rotation.",
                duration_range,
                {"rotation_degrees": _rotation_degrees(metadata)},
                strategy,
                changes_content=True,
            )
        )
    salvage_candidates = tuple(
        item
        for item in by_kind[DamageKind.UNDECODABLE]
        if not _overlaps_any((item.start_seconds, item.end_seconds), locked_ranges)
    )
    if salvage_candidates:
        actions.append(
            _action_from_intervals(
                RescueActionKind.SALVAGE_SEGMENTS,
                "Preserve decodable segments around observed undecodable intervals.",
                salvage_candidates,
                strategy,
                changes_content=True,
            )
        )
    trim_candidates = tuple(
        item
        for item in by_kind[DamageKind.UNDECODABLE]
        if _is_damaged_edge(item, damage_map.duration_seconds)
        and not _overlaps_any((item.start_seconds, item.end_seconds), locked_ranges)
    )
    if trim_candidates:
        actions.append(
            _action_from_intervals(
                RescueActionKind.TRIM_DAMAGED_EDGES,
                "Trim observed undecodable content only at an unlocked source edge.",
                trim_candidates,
                strategy,
                changes_content=True,
            )
        )
    if (
        fixed_offset_assessment is not None
        and fixed_offset_assessment.offset_seconds is not None
        and fixed_offset_assessment.shift_seconds is not None
        and (
            requested_action_kinds is None
            or RescueActionKind.CORRECT_FIXED_AV_OFFSET in requested_action_kinds
        )
    ):
        actions.append(
            _action(
                RescueActionKind.CORRECT_FIXED_AV_OFFSET,
                "Apply one measured constant audio/video timing shift.",
                duration_range,
                {
                    "offset_seconds": fixed_offset_assessment.offset_seconds,
                    "audio_shift_seconds": fixed_offset_assessment.shift_seconds,
                    "correlation": fixed_offset_assessment.correlation,
                    "matched_event_count": fixed_offset_assessment.matched_event_count,
                    "agreement_seconds": fixed_offset_assessment.agreement_seconds
                    or 0.0,
                    "minimum_correlation": (
                        fixed_offset_assessment.config.minimum_correlation
                    ),
                    "minimum_event_count": (
                        fixed_offset_assessment.config.minimum_event_count
                    ),
                    "maximum_agreement_seconds": (
                        fixed_offset_assessment.config.maximum_agreement_seconds
                    ),
                    "maximum_absolute_offset_seconds": (
                        fixed_offset_assessment.config.maximum_absolute_offset_seconds
                    ),
                },
                strategy,
                changes_content=True,
            )
        )
    if strategy is RescueStrategy.BALANCED:
        actions.extend(
            _balanced_actions(
                by_kind,
                strategy,
                visual_assessment,
                flicker_correction,
                stabilization_assessment,
                audio_assessment,
                _metadata_audio_sample_rate(metadata),
                damage_map.duration_seconds,
                config,
                locked_ranges,
                requested_action_kinds,
                assessment_parameters,
            )
        )
    actions.append(
        _action(
            RescueActionKind.VERIFY,
            "Verify decodability, streams, duration, and source immutability.",
            duration_range,
            {"checks": list(config.verification_policy)},
            strategy,
            changes_content=False,
        )
    )
    if strategy is RescueStrategy.BALANCED:
        actions.extend(
            _measured_deblur_actions(
                assessment_parameters,
                strategy,
                config,
                locked_ranges,
                requested_action_kinds,
            )
        )
    contract = canonical_video_encode_contract(config).model_dump(mode="json")
    return tuple(
        _action(
            action.kind,
            action.description,
            action.source_ranges,
            {
                **action.parameters,
                "video_encode_contract": contract,
            },
            action.strategy,
            changes_content=True,
        )
        if action.changes_content
        else action
        for action in actions
    )


def _balanced_actions(
    by_kind: dict[DamageKind, tuple[DamageInterval, ...]],
    strategy: RescueStrategy,
    visual_assessment: VisualAssessment | None,
    flicker_correction: FlickerCorrectionPlan | None,
    stabilization_assessment: StabilizationAssessment | None,
    audio_assessment: AudioAssessment | None,
    audio_sample_rate_hz: int | None,
    duration_seconds: float,
    config: RescueEffectiveConfig,
    locked_ranges: tuple[tuple[float, float], ...],
    requested_action_kinds: frozenset[RescueActionKind] | None,
    assessment_parameters: dict[str, JsonValue],
) -> tuple[RescueAction, ...]:
    tonal_actions = _measured_tonal_actions(
        assessment_parameters,
        strategy,
        config,
        locked_ranges,
        requested_action_kinds,
    )
    deblur_ranges = tuple(
        source_range
        for action in _measured_deblur_actions(
            assessment_parameters,
            strategy,
            config,
            locked_ranges,
            requested_action_kinds,
        )
        for source_range in action.source_ranges
    )
    action_specs = (
        (
            RescueActionKind.ADJUST_LUMA,
            (DamageKind.DARK,),
            "Adjust luma for observed dark intervals.",
        ),
        (
            RescueActionKind.DENOISE_VIDEO,
            (DamageKind.VIDEO_NOISE,),
            "Reduce visible noise in observed intervals.",
        ),
        (
            RescueActionKind.SHARPEN,
            (DamageKind.SOFT_DETAIL,),
            "Sharpen observed soft-detail intervals.",
        ),
        (
            RescueActionKind.DEFLICKER,
            (DamageKind.FLICKER,),
            "Reduce observed frame-to-frame flicker.",
        ),
        (
            RescueActionKind.STABILIZE,
            (DamageKind.SHAKE,),
            "Stabilize observed shake intervals.",
        ),
    )
    actions: list[RescueAction] = []
    for kind, damage_kinds, description in action_specs:
        if requested_action_kinds is not None and kind not in requested_action_kinds:
            continue
        intervals = tuple(
            interval
            for damage_kind in damage_kinds
            for interval in by_kind[damage_kind]
        )
        if not intervals:
            continue
        source_ranges = _subtract_ranges(
            tuple(
                (interval.start_seconds, interval.end_seconds) for interval in intervals
            ),
            locked_ranges,
        )
        if kind is RescueActionKind.ADJUST_LUMA:
            source_ranges = _coalesce_overlapping_ranges(source_ranges)
        if kind is RescueActionKind.SHARPEN and deblur_ranges:
            source_ranges = _subtract_ranges(source_ranges, deblur_ranges)
        if not source_ranges:
            continue
        if kind in {
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
        } and (
            visual_assessment is None
            or kind not in visual_assessment.recommended_actions
        ):
            continue
        parameters: dict[str, JsonValue] = {
            "damage_ids": [interval.id for interval in intervals],
            "strength_limit": config.balanced_strength_limit,
        }
        action_evidence: tuple[VisualEvidence, ...] = ()
        action_metrics: VisualMetrics | None = None
        action_limitations: list[JsonValue] = (
            [item for item in visual_assessment.limitations]
            if visual_assessment is not None
            else []
        )
        if visual_assessment is not None and kind in {
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
        }:
            action_evidence = tuple(
                item for item in visual_assessment.evidence if item.action is kind
            )
            luma_config: LumaAdjustmentConfig | None = None
            if kind is RescueActionKind.ADJUST_LUMA:
                covered_source_ranges = tuple(
                    (start, end)
                    for start, end in source_ranges
                    if any(
                        start <= item.timestamp_seconds < end
                        for item in action_evidence
                    )
                )
                if not covered_source_ranges:
                    raise RescuePlanError("ADJUST_LUMA assessment evidence is invalid")
                if len(covered_source_ranges) < len(source_ranges):
                    action_limitations.append(
                        "ADJUST_LUMA source ranges without persisted range-bound "
                        "assessment evidence were omitted."
                    )
                source_ranges = covered_source_ranges
                action_evidence = tuple(
                    item
                    for item in action_evidence
                    if any(
                        start <= item.timestamp_seconds < end
                        for start, end in source_ranges
                    )
                )
                luma_config = _assessment_luma_config(assessment_parameters)
                try:
                    measured_p10, measured_p50 = validate_luma_action_evidence(
                        action_evidence,
                        luma_config,
                        source_ranges,
                    )
                except ValueError as exc:
                    raise RescuePlanError(
                        "ADJUST_LUMA assessment evidence is invalid"
                    ) from exc
                action_metrics = visual_assessment.metrics.model_copy(
                    update={"luma_p10": measured_p10, "luma_p50": measured_p50}
                )
            else:
                action_metrics = _action_metrics(visual_assessment, kind)
            baseline: float | None = None
            if kind is RescueActionKind.SHARPEN:
                baselines = tuple(
                    item.scene_baseline_sharpness
                    for item in visual_assessment.evidence
                    if item.action is kind and item.scene_baseline_sharpness is not None
                )
                if baselines:
                    baseline = float(statistics.median(baselines))
            parameters.update(
                derive_visual_action_parameters(
                    kind,
                    action_metrics,
                    luma_config=luma_config,
                    sharpen_config=(
                        _assessment_sharpen_config(assessment_parameters)
                        if kind is RescueActionKind.SHARPEN
                        else None
                    ),
                    scene_baseline_sharpness=baseline,
                )
            )
            if baseline is not None:
                parameters["scene_baseline_sharpness"] = baseline
        else:
            parameters.update(visual_action_parameters(kind))
        _apply_strength_limit(parameters, kind, config.balanced_strength_limit)
        if kind in {
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
        }:
            assert visual_assessment is not None
            assert action_metrics is not None
            parameters.update(
                {
                    "assessment_metrics": action_metrics.model_dump(mode="json"),
                    "assessment_evidence": [
                        item.model_dump(mode="json") for item in action_evidence
                    ],
                    "assessment_limitations": action_limitations,
                }
            )
        if kind is RescueActionKind.DEFLICKER:
            if flicker_correction is None or not flicker_correction.intervals:
                continue
            parameters.update(
                {
                    "affected_ranges": [
                        list(item) for item in flicker_correction.intervals
                    ],
                    "gain_curve": [list(item) for item in flicker_correction.gains],
                    "excluded_fade_ranges": [
                        list(item) for item in flicker_correction.excluded_fade_ranges
                    ],
                }
            )
            parameters["gain_curve"] = [
                [timestamp, 1.0 + (gain - 1.0) * config.balanced_strength_limit]
                for timestamp, gain in flicker_correction.gains
            ]
        if kind is RescueActionKind.STABILIZE:
            if (
                stabilization_assessment is None
                or not stabilization_assessment.recommended
            ):
                continue
            parameters.update(stabilization_assessment.parameters)
            anchor_corrections = bool(stabilization_assessment.transforms) and all(
                transform.semantics == "frame_correction"
                for transform in stabilization_assessment.transforms
            )
            if anchor_corrections:
                direct_method = parameters.get("method", "anchor_v1")
                if direct_method not in {"anchor_v1", "transition_anchor_v1"}:
                    continue
                raw_config = parameters.get("config")
                try:
                    if raw_config is not None:
                        stabilization_config = StabilizationConfig.model_validate_json(
                            json.dumps(raw_config, ensure_ascii=False)
                        )
                    else:
                        stabilization_config = StabilizationConfig.model_validate(
                            {
                                key: parameters[key]
                                for key in StabilizationConfig.model_fields
                                if key in parameters
                            }
                        )
                    parameters["config"] = stabilization_config.model_copy(
                        update={"accepted_ranges": source_ranges}
                    ).model_dump(mode="json")
                except (TypeError, ValueError):
                    continue
                retained_corrections: list[JsonValue] = [
                    cast(JsonValue, transform.model_dump(mode="json"))
                    for transform in stabilization_assessment.transforms
                    if any(
                        start <= transform.timestamp_seconds < end
                        for start, end in source_ranges
                    )
                ]
                if not retained_corrections:
                    continue
                parameters.update(
                    {
                        "method": direct_method,
                        "algorithm_version": (
                            config.anchor_stabilization_algorithm_version
                        ),
                        "motion_transforms": retained_corrections,
                    }
                )
            else:
                parameters["motion_transforms"] = [
                    _strength_limited_transform(
                        transform.model_dump(mode="json"),
                        config.balanced_strength_limit,
                    )
                    for transform in stabilization_assessment.transforms
                ]
                if "crop_ratio" in parameters:
                    parameters["crop_ratio"] = (
                        float(cast(float, parameters["crop_ratio"]))
                        * config.balanced_strength_limit
                    )
        actions.append(
            _action(
                kind,
                description,
                tuple(source_range for source_range in source_ranges),
                parameters,
                strategy,
                changes_content=True,
            )
        )
    if audio_assessment is not None:
        for kind in audio_assessment.recommended_actions:
            if kind not in {
                RescueActionKind.NORMALIZE_AUDIO,
                RescueActionKind.DENOISE_AUDIO,
            }:
                continue
            if kind is RescueActionKind.DENOISE_AUDIO and tonal_actions:
                continue
            if (
                requested_action_kinds is not None
                and kind not in requested_action_kinds
            ):
                continue
            audio_parameters = _strength_limited_audio_parameters(
                audio_assessment.parameters,
                kind,
                config.balanced_strength_limit,
            )
            if audio_sample_rate_hz is not None:
                audio_parameters["output_sample_rate_hz"] = audio_sample_rate_hz
            audio_ranges = (
                tuple(
                    (
                        max(
                            0.0,
                            item.start_seconds
                            - float(
                                audio_assessment.parameters.get(
                                    "noise_boundary_guard_seconds", 0.0
                                )
                            ),
                        ),
                        min(
                            duration_seconds,
                            item.end_seconds
                            + float(
                                audio_assessment.parameters.get(
                                    "noise_boundary_guard_seconds", 0.0
                                )
                            ),
                        ),
                    )
                    for item in audio_assessment.measurement.noise_intervals
                )
                if kind is RescueActionKind.DENOISE_AUDIO
                and audio_assessment.measurement.noise_intervals
                else ((0.0, duration_seconds),)
            )
            audio_ranges = _subtract_ranges(audio_ranges, locked_ranges)
            if not audio_ranges:
                continue
            if kind is RescueActionKind.DENOISE_AUDIO:
                noise_profiles: list[JsonValue] = []
                for interval in audio_assessment.measurement.noise_intervals:
                    for allowed_start, allowed_end in audio_ranges:
                        guard = float(
                            audio_assessment.parameters.get(
                                "noise_boundary_guard_seconds", 0.0
                            )
                        )
                        profile_start = max(
                            0.0, interval.start_seconds - guard, allowed_start
                        )
                        profile_end = min(
                            duration_seconds, interval.end_seconds + guard, allowed_end
                        )
                        if profile_end <= profile_start:
                            continue
                        noise_profiles.append(
                            {
                                **interval.model_dump(mode="json"),
                                "start_seconds": profile_start,
                                "end_seconds": profile_end,
                            }
                        )
                if noise_profiles:
                    audio_parameters["noise_profiles"] = noise_profiles
            actions.append(
                _action(
                    kind,
                    "Apply the bounded audio adjustment supported by measured values.",
                    audio_ranges,
                    audio_parameters,
                    strategy,
                    changes_content=True,
                )
            )
    actions.extend(tonal_actions)
    return tuple(actions)


def _assessment_sharpen_config(
    assessment_parameters: Mapping[str, JsonValue],
) -> SharpenConfig:
    raw_visual = assessment_parameters.get("visual_config")
    if raw_visual is None:
        return SharpenConfig()
    if not isinstance(raw_visual, Mapping):
        raise RescuePlanError("assessment visual config is invalid")
    raw_sharpen = raw_visual.get("sharpen")
    if not isinstance(raw_sharpen, Mapping):
        raise RescuePlanError("assessment SHARPEN config is missing")
    if set(raw_sharpen) != set(SharpenConfig.model_fields):
        raise RescuePlanError("assessment SHARPEN config fields are incomplete")
    try:
        return VisualAssessmentConfig.model_validate(raw_visual).sharpen
    except ValueError as exc:
        raise RescuePlanError("assessment SHARPEN config is invalid") from exc


def _assessment_luma_config(
    assessment_parameters: Mapping[str, JsonValue],
) -> LumaAdjustmentConfig:
    raw_visual = assessment_parameters.get("visual_config")
    if raw_visual is None:
        return LumaAdjustmentConfig()
    if not isinstance(raw_visual, Mapping):
        raise RescuePlanError("assessment visual config is invalid")
    raw_luma = raw_visual.get("luma")
    if not isinstance(raw_luma, Mapping):
        raise RescuePlanError("assessment LUMA config is missing")
    if set(raw_luma) != set(LumaAdjustmentConfig.model_fields):
        raise RescuePlanError("assessment LUMA config fields are incomplete")
    try:
        return VisualAssessmentConfig.model_validate(raw_visual).luma
    except ValueError as exc:
        raise RescuePlanError("assessment LUMA config is invalid") from exc


def _measured_deblur_actions(
    assessment_parameters: Mapping[str, JsonValue],
    strategy: RescueStrategy,
    config: RescueEffectiveConfig,
    locked_ranges: tuple[tuple[float, float], ...],
    requested_action_kinds: frozenset[RescueActionKind] | None,
) -> tuple[RescueAction, ...]:
    if requested_action_kinds is not None and (
        RescueActionKind.DEBLUR not in requested_action_kinds
    ):
        return ()
    profiles = assessment_parameters.get("deblur_measurements")
    if not isinstance(profiles, list):
        return ()
    operations: list[dict[str, JsonValue]] = []
    all_ranges: list[tuple[float, float]] = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        try:
            algorithm_version = profile["algorithm_version"]
            if algorithm_version != config.deblur_algorithm_version:
                continue
            estimate = BlurKernelEstimate.model_validate_json(
                json.dumps(profile["estimate"], ensure_ascii=False)
            )
            measured_config = DeblurConfig.model_validate_json(
                json.dumps(profile["config"], ensure_ascii=False)
            )
            source_ranges = _subtract_ranges(
                _json_ranges(profile.get("source_ranges")), locked_ranges
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not source_ranges:
            continue
        if _ranges_intersect(source_ranges, all_ranges):
            return ()
        for previous, current in zip(source_ranges, source_ranges[1:], strict=False):
            if current[0] < previous[1]:
                return ()
        all_ranges.extend(source_ranges)
        operations.append(
            {
                "source_ranges": [list(item) for item in source_ranges],
                "estimate": estimate.model_dump(mode="json"),
                "config": measured_config.model_dump(mode="json"),
            }
        )
    if not operations:
        return ()
    operations.sort(
        key=lambda item: (
            cast(list[list[float]], item["source_ranges"])[0][0],
            json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    )
    parameters: dict[str, JsonValue] = {
        "algorithm_version": config.deblur_algorithm_version,
    }
    if len(operations) == 1:
        parameters.update(
            {
                "estimate": operations[0]["estimate"],
                "config": operations[0]["config"],
            }
        )
    else:
        parameters["operations"] = cast(JsonValue, operations)
    return (
        _action(
            RescueActionKind.DEBLUR,
            "Apply measured bounded deconvolution to persistent soft detail.",
            _normalized_ranges(all_ranges),
            parameters,
            strategy,
            changes_content=True,
        ),
    )


def _measured_tonal_actions(
    assessment_parameters: Mapping[str, JsonValue],
    strategy: RescueStrategy,
    config: RescueEffectiveConfig,
    locked_ranges: tuple[tuple[float, float], ...],
    requested_action_kinds: frozenset[RescueActionKind] | None,
) -> tuple[RescueAction, ...]:
    if requested_action_kinds is not None and (
        RescueActionKind.DENOISE_AUDIO not in requested_action_kinds
    ):
        return ()
    profiles = assessment_parameters.get("tonal_interference_measurements")
    if not isinstance(profiles, list):
        return ()
    tones: list[JsonValue] = []
    ranges: list[tuple[float, float]] = []
    measured_config: TonalInterferenceConfig | None = None
    for profile in profiles:
        if not isinstance(profile, dict):
            return ()
        try:
            if profile["algorithm_version"] != config.tonal_algorithm_version:
                return ()
            candidate_config = TonalInterferenceConfig.model_validate_json(
                json.dumps(profile["config"], ensure_ascii=False)
            )
            if measured_config is not None and candidate_config != measured_config:
                return ()
            measured_config = candidate_config
            candidate_ranges = _subtract_ranges(
                _json_ranges(profile.get("source_ranges")), locked_ranges
            )
            raw_tones = profile["interference_profiles"]
            if not isinstance(raw_tones, list):
                return ()
            validated = tuple(
                InterferenceTone.model_validate_json(
                    json.dumps(item, ensure_ascii=False)
                )
                for item in raw_tones
            )
            validate_tonal_profile_contracts(validated, candidate_config)
        except (KeyError, TypeError, ValueError):
            return ()
        retained_tones: list[InterferenceTone] = []
        for item in validated:
            if any(
                allowed_start <= item.start_seconds and item.end_seconds <= allowed_end
                for allowed_start, allowed_end in candidate_ranges
            ):
                retained_tones.append(item)
        if retained_tones:
            try:
                validate_tonal_profile_contracts(
                    tuple(retained_tones), candidate_config
                )
            except ValueError:
                return ()
        ranges.extend((item.start_seconds, item.end_seconds) for item in retained_tones)
        tones.extend(item.model_dump(mode="json") for item in retained_tones)
    if measured_config is None or not tones or not ranges:
        return ()
    return (
        _action(
            RescueActionKind.DENOISE_AUDIO,
            "Reduce measured local narrowband interference.",
            _normalized_ranges(ranges),
            {
                "algorithm_version": config.tonal_algorithm_version,
                "interference_profiles": tones,
                "config": measured_config.model_dump(mode="json"),
            },
            strategy,
            changes_content=True,
        ),
    )


def _json_ranges(value: object) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list):
        raise ValueError("measured source ranges are invalid")
    parsed: list[tuple[float, float]] = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise ValueError("measured source ranges are invalid")
        start, end = item
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(end) <= float(start)
        ):
            raise ValueError("measured source ranges are invalid")
        parsed.append((float(start), float(end)))
    return _normalized_ranges(parsed)


def _metadata_audio_sample_rate(metadata: VideoMetadata) -> int | None:
    value = metadata.raw_probe.get("audio_sample_rate_hz")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 8000 <= value <= 384000
    ):
        return value
    return None


def _action_metrics(
    assessment: VisualAssessment, kind: RescueActionKind
) -> VisualMetrics:
    """Bind action strength to its own measured evidence, not fixture identity."""
    observed = tuple(
        item.observed for item in assessment.evidence if item.action is kind
    )
    if not observed:
        return assessment.metrics
    representative = float(statistics.median(observed))
    if kind is RescueActionKind.ADJUST_LUMA:
        return assessment.metrics.model_copy(update={"luma_p10": representative})
    if kind is RescueActionKind.SHARPEN:
        luma_values = tuple(
            item.context_luma_p50
            for item in assessment.evidence
            if item.action is kind and item.context_luma_p50 is not None
        )
        updates = {"sharpness": representative}
        if luma_values:
            updates["luma_p50"] = float(statistics.median(luma_values))
        return assessment.metrics.model_copy(update=updates)
    if kind is RescueActionKind.DENOISE_VIDEO:
        return assessment.metrics.model_copy(update={"noise_residual": representative})
    return assessment.metrics


def _action_from_intervals(
    kind: RescueActionKind,
    description: str,
    intervals: Iterable[DamageInterval],
    strategy: RescueStrategy,
    *,
    changes_content: bool,
) -> RescueAction:
    selected = tuple(intervals)
    return _action(
        kind,
        description,
        tuple((item.start_seconds, item.end_seconds) for item in selected),
        {"damage_ids": [item.id for item in selected]},
        strategy,
        changes_content=changes_content,
    )


def _action(
    kind: RescueActionKind,
    description: str,
    source_ranges: tuple[tuple[float, float], ...],
    parameters: dict[str, JsonValue],
    strategy: RescueStrategy,
    *,
    changes_content: bool,
) -> RescueAction:
    return RescueAction(
        id=make_rescue_action_id(
            kind=kind,
            parameters=parameters,
            source_ranges=source_ranges,
            strategy=strategy,
            version=_ACTION_VERSION,
        ),
        version=_ACTION_VERSION,
        kind=kind,
        description=description,
        source_ranges=source_ranges,
        parameters=parameters,
        changes_content=changes_content,
        requires_confirmation=changes_content,
        strategy=strategy,
    )


def _supports_improved(actions: Sequence[RescueAction]) -> bool:
    return any(action.kind in REMAINING_IMPROVEMENT_ACTION_KINDS for action in actions)


def _select_preview_ranges(
    intervals: Sequence[DamageInterval],
    actions: Sequence[RescueAction],
    duration_seconds: float,
    config: RescueEffectiveConfig,
) -> tuple[tuple[float, float], ...]:
    obligations = _preview_obligations(
        intervals,
        actions,
        duration_seconds=duration_seconds,
    )
    clusters = _shared_preview_clusters(obligations)[: config.max_preview_ranges]
    if not clusters:
        return ()

    remaining_budget = config.max_preview_total_seconds
    selected: list[tuple[float, float]] = []
    removed_ranges = tuple(
        source_range
        for action in actions
        if action.kind
        in {
            RescueActionKind.SALVAGE_SEGMENTS,
            RescueActionKind.TRIM_DAMAGED_EDGES,
        }
        for source_range in action.source_ranges
    )
    for index, cluster in enumerate(clusters):
        remaining_clusters = len(clusters) - index
        fair_share = remaining_budget / remaining_clusters
        available = (
            duration_seconds
            if cluster.representative_action.kind
            in {
                RescueActionKind.SALVAGE_SEGMENTS,
                RescueActionKind.TRIM_DAMAGED_EDGES,
            }
            else cluster.shared_range[1] - cluster.shared_range[0]
        )
        allocated = min(available, fair_share)
        if allocated < _MINIMUM_PREVIEW_SECONDS:
            continue
        if cluster.representative_action.kind in {
            RescueActionKind.SALVAGE_SEGMENTS,
            RescueActionKind.TRIM_DAMAGED_EDGES,
        }:
            candidate = _structural_action_preview_range(
                cluster.representative_action,
                cluster.shared_range,
                duration_seconds=duration_seconds,
                maximum_duration=allocated,
                locked_ranges=config.locked_ranges,
                removed_ranges=removed_ranges,
            )
        else:
            candidate = _action_preview_range(
                cluster.representative_action,
                cluster.shared_range,
                duration_seconds=duration_seconds,
                maximum_duration=allocated,
            )
        candidate = _snap_direct_stabilization_preview_range(
            cluster.actions,
            candidate,
            removed_ranges=removed_ranges,
        )
        if candidate[1] - candidate[0] < _MINIMUM_PREVIEW_SECONDS:
            continue
        selected.append(candidate)
        remaining_budget -= candidate[1] - candidate[0]

    return _merge_overlapping_preview_ranges(selected)


def _preview_obligations(
    intervals: Sequence[DamageInterval],
    actions: Sequence[RescueAction],
    *,
    duration_seconds: float,
) -> tuple[_PreviewObligation, ...]:
    obligations: list[_PreviewObligation] = []
    for action_index, action in enumerate(actions):
        if not action.requires_confirmation:
            continue
        if action_verification_mode(action.kind) == "needs_review":
            review_priority = 0
        elif action.kind in {
            RescueActionKind.SALVAGE_SEGMENTS,
            RescueActionKind.TRIM_DAMAGED_EDGES,
        }:
            review_priority = 1
        else:
            review_priority = 2
        for range_index, source_range in enumerate(action.source_ranges):
            candidate = _semantic_preview_candidate(
                action,
                source_range,
                intervals,
                duration_seconds=duration_seconds,
            )
            if candidate[1] - candidate[0] < _MINIMUM_PREVIEW_SECONDS:
                continue
            obligations.append(
                _PreviewObligation(
                    action=action,
                    action_index=action_index,
                    range_index=range_index,
                    candidate_range=candidate,
                    review_priority=review_priority,
                )
            )
    grouped: dict[int, list[_PreviewObligation]] = {}
    for obligation in sorted(
        obligations,
        key=lambda item: (
            item.review_priority,
            item.action_index,
            item.range_index,
            item.candidate_range,
            item.action.id,
        ),
    ):
        grouped.setdefault(obligation.action_index, []).append(obligation)
    action_order = tuple(
        sorted(
            grouped,
            key=lambda action_index: (
                grouped[action_index][0].review_priority,
                action_index,
            ),
        )
    )
    round_robin: list[_PreviewObligation] = []
    maximum_range_count = max(
        (len(grouped[index]) for index in action_order), default=0
    )
    for range_position in range(maximum_range_count):
        for action_index in action_order:
            action_obligations = grouped[action_index]
            if range_position < len(action_obligations):
                round_robin.append(action_obligations[range_position])
    return tuple(round_robin)


def _semantic_preview_candidate(
    action: RescueAction,
    source_range: tuple[float, float],
    intervals: Sequence[DamageInterval],
    *,
    duration_seconds: float,
) -> tuple[float, float]:
    """Crop only matching observed damage to the action's actual operation range."""
    source_start = max(0.0, source_range[0])
    source_end = min(duration_seconds, source_range[1])
    if source_end - source_start < _MINIMUM_PREVIEW_SECONDS:
        return source_start, source_start
    if action.kind is RescueActionKind.STABILIZE and (
        action.parameters.get("method") in {"anchor_v1", "transition_anchor_v1"}
    ):
        raw_transforms = cast(
            Iterable[JsonValue],
            action.parameters.get("motion_transforms") or (),
        )
        timestamps = tuple(
            float(timestamp)
            for transform in raw_transforms
            if isinstance(transform, dict)
            for timestamp in (transform.get("timestamp_seconds"),)
            if isinstance(timestamp, (int, float))
            and not isinstance(timestamp, bool)
            and math.isfinite(float(timestamp))
            and timestamp_in_half_open_range(
                float(timestamp),
                source_start,
                source_end,
            )
        )
        if not timestamps:
            return source_start, source_start
        source_start = min(timestamps)
    matching_kinds = _ACTION_DAMAGE_KINDS.get(action.kind)
    if not matching_kinds:
        return source_start, source_end
    intersections = tuple(
        (
            max(source_start, interval.start_seconds),
            min(source_end, interval.end_seconds),
        )
        for interval in intervals
        if interval.kind in matching_kinds
        and max(source_start, interval.start_seconds)
        < min(source_end, interval.end_seconds)
    )
    if not intersections:
        return source_start, source_end
    return min(
        intersections,
        key=lambda item: (-(item[1] - item[0]), item[0], item[1]),
    )


def _shared_preview_clusters(
    obligations: Sequence[_PreviewObligation],
) -> tuple[_PreviewCluster, ...]:
    remaining = list(obligations)
    clusters: list[_PreviewCluster] = []
    while remaining:
        seed = remaining.pop(0)
        shared_start, shared_end = seed.candidate_range
        actions = [seed.action]
        action_ids = {seed.action.id}
        index = 0
        while index < len(remaining):
            candidate = remaining[index].candidate_range
            intersection = (
                max(shared_start, candidate[0]),
                min(shared_end, candidate[1]),
            )
            if intersection[1] - intersection[0] < _MINIMUM_PREVIEW_SECONDS:
                index += 1
                continue
            shared_start, shared_end = intersection
            obligation = remaining.pop(index)
            if obligation.action.id not in action_ids:
                actions.append(obligation.action)
                action_ids.add(obligation.action.id)
        clusters.append(
            _PreviewCluster(
                representative_action=seed.action,
                actions=tuple(actions),
                shared_range=(shared_start, shared_end),
            )
        )
    return tuple(clusters)


def _snap_direct_stabilization_preview_range(
    actions: Sequence[RescueAction],
    candidate: tuple[float, float],
    *,
    removed_ranges: Sequence[tuple[float, float]],
) -> tuple[float, float]:
    """Snap internal direct-preview boundaries to shared serialized PTS."""
    direct_actions = tuple(
        action
        for action in actions
        if action.kind is RescueActionKind.STABILIZE
        and action.parameters.get("method") in {"anchor_v1", "transition_anchor_v1"}
    )
    if not direct_actions:
        return candidate
    candidate_start, candidate_end = candidate
    inventories: list[tuple[tuple[float, float], tuple[float, ...]]] = []
    for action in direct_actions:
        relevant_ranges = tuple(
            source_range
            for source_range in action.source_ranges
            if source_range[0] <= candidate_start and candidate_end <= source_range[1]
        )
        if len(relevant_ranges) != 1:
            return candidate_start, candidate_start
        source_range = relevant_ranges[0]
        timestamps = tuple(
            timestamp
            for timestamp in _direct_stabilization_timestamps(action)
            if timestamp_in_half_open_range(timestamp, *source_range)
            and not any(
                timestamp_in_half_open_range(timestamp, start, end)
                for start, end in removed_ranges
            )
        )
        if not timestamps:
            return candidate_start, candidate_start
        inventories.append((source_range, timestamps))

    common_timestamps = set(inventories[0][1])
    for _source_range, timestamps in inventories[1:]:
        common_timestamps.intersection_update(timestamps)

    snapped_start = candidate_start
    if not all(
        candidate_start == source_range[0] for source_range, _timestamps in inventories
    ):
        start_boundaries = tuple(
            timestamp
            for timestamp in common_timestamps
            if candidate_start <= timestamp < candidate_end
        )
        if not start_boundaries:
            return candidate_start, candidate_start
        snapped_start = min(start_boundaries)

    snapped_end = candidate_end
    if not all(
        candidate_end == source_range[1] for source_range, _timestamps in inventories
    ):
        end_boundaries = tuple(
            timestamp
            for timestamp in common_timestamps
            if snapped_start < timestamp <= candidate_end
        )
        if not end_boundaries:
            return snapped_start, snapped_start
        snapped_end = max(end_boundaries)

    if snapped_end - snapped_start < _MINIMUM_PREVIEW_SECONDS:
        return snapped_start, snapped_start
    if any(
        not any(
            timestamp_in_half_open_range(timestamp, snapped_start, snapped_end)
            for timestamp in timestamps
        )
        for _source_range, timestamps in inventories
    ):
        return snapped_start, snapped_start
    return snapped_start, snapped_end


def _direct_stabilization_timestamps(action: RescueAction) -> tuple[float, ...]:
    raw_transforms = action.parameters.get("motion_transforms")
    if not isinstance(raw_transforms, list):
        return ()
    timestamps: list[float] = []
    for transform in raw_transforms:
        if not isinstance(transform, dict):
            return ()
        value = transform.get("timestamp_seconds")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            return ()
        timestamps.append(float(value))
    if any(
        timestamps[index] <= timestamps[index - 1]
        for index in range(1, len(timestamps))
    ):
        return ()
    return tuple(timestamps)


def _merge_overlapping_preview_ranges(
    ranges: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if not merged or merged[-1][1] <= start:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return tuple(merged)


def _action_preview_range(
    action: RescueAction,
    source_range: tuple[float, float],
    *,
    duration_seconds: float,
    maximum_duration: float,
) -> tuple[float, float]:
    start, end = source_range
    if action.kind not in {
        RescueActionKind.SALVAGE_SEGMENTS,
        RescueActionKind.TRIM_DAMAGED_EDGES,
    }:
        return start, min(end, start + maximum_duration)
    target_duration = min(duration_seconds, maximum_duration)
    context = max(0.0, target_duration - (end - start))
    preview_start = max(0.0, start - context / 2.0)
    preview_end = min(duration_seconds, preview_start + target_duration)
    preview_start = max(0.0, preview_end - target_duration)
    return preview_start, preview_end


def _structural_action_preview_range(
    action: RescueAction,
    source_range: tuple[float, float],
    *,
    duration_seconds: float,
    maximum_duration: float,
    locked_ranges: Sequence[tuple[float, float]],
    removed_ranges: Sequence[tuple[float, float]],
) -> tuple[float, float]:
    preferred = _action_preview_range(
        action,
        source_range,
        duration_seconds=duration_seconds,
        maximum_duration=maximum_duration,
    )
    if _structural_candidate_is_reviewable(
        preferred,
        source_range=source_range,
        locked_ranges=locked_ranges,
        removed_ranges=removed_ranges,
    ):
        return preferred

    candidates: list[tuple[float, float]] = []
    unlocked_ranges = _subtract_ranges(((0.0, duration_seconds),), locked_ranges)
    for boundary in source_range:
        for unlocked_start, unlocked_end in unlocked_ranges:
            if not unlocked_start <= boundary <= unlocked_end:
                continue
            target_duration = min(maximum_duration, unlocked_end - unlocked_start)
            if target_duration < _MINIMUM_PREVIEW_SECONDS:
                continue
            start = max(
                unlocked_start,
                min(
                    boundary - target_duration / 2.0,
                    unlocked_end - target_duration,
                ),
            )
            candidate = start, start + target_duration
            if _structural_candidate_is_reviewable(
                candidate,
                source_range=source_range,
                locked_ranges=locked_ranges,
                removed_ranges=removed_ranges,
            ):
                candidates.append(candidate)
    if not candidates:
        return source_range[0], source_range[0]
    return min(
        candidates,
        key=lambda candidate: (
            -_retained_preview_duration(candidate, removed_ranges),
            candidate[0],
            candidate[1],
        ),
    )


def _structural_candidate_is_reviewable(
    candidate: tuple[float, float],
    *,
    source_range: tuple[float, float],
    locked_ranges: Sequence[tuple[float, float]],
    removed_ranges: Sequence[tuple[float, float]],
) -> bool:
    return bool(
        candidate[1] - candidate[0] >= _MINIMUM_PREVIEW_SECONDS
        and _ranges_intersect((candidate,), (source_range,))
        and not _overlaps_any(candidate, locked_ranges)
        and _retained_preview_duration(candidate, removed_ranges)
        >= _MINIMUM_PREVIEW_SECONDS
    )


def _retained_preview_duration(
    candidate: tuple[float, float],
    removed_ranges: Sequence[tuple[float, float]],
) -> float:
    intersections = sorted(
        (
            max(candidate[0], start),
            min(candidate[1], end),
        )
        for start, end in removed_ranges
        if max(candidate[0], start) < min(candidate[1], end)
    )
    merged: list[tuple[float, float]] = []
    for start, end in intersections:
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], end)
        else:
            merged.append((start, end))
    removed_duration = sum(end - start for start, end in merged)
    return max(0.0, candidate[1] - candidate[0] - removed_duration)


def _ranges_intersect(
    first: Sequence[tuple[float, float]],
    second: Sequence[tuple[float, float]],
) -> bool:
    return any(
        first_start < second_end and second_start < first_end
        for first_start, first_end in first
        for second_start, second_end in second
    )


def _normalized_ranges(
    ranges: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    normalized: list[tuple[float, float]] = []
    for start_seconds, end_seconds in ranges:
        if not all(
            math.isfinite(value) and value >= 0
            for value in (start_seconds, end_seconds)
        ):
            raise RescuePlanError("locked ranges must use finite non-negative seconds")
        if end_seconds < start_seconds:
            raise RescuePlanError(
                "locked range end_seconds must not be before start_seconds"
            )
        normalized.append((float(start_seconds), float(end_seconds)))
    return tuple(sorted(set(normalized)))


def _coalesce_overlapping_ranges(
    ranges: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    """Merge only true overlaps; adjacent half-open ranges remain distinct."""
    merged: list[tuple[float, float]] = []
    for start, end in sorted(set(ranges)):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _subtract_ranges(
    ranges: Sequence[tuple[float, float]],
    excluded: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    """Return deterministic source spans which do not overlap user locks."""
    remaining: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        pieces = [(float(start), float(end))]
        for lock_start, lock_end in excluded:
            next_pieces: list[tuple[float, float]] = []
            for piece_start, piece_end in pieces:
                if lock_end <= piece_start or lock_start >= piece_end:
                    next_pieces.append((piece_start, piece_end))
                    continue
                if piece_start < lock_start:
                    next_pieces.append((piece_start, lock_start))
                if lock_end < piece_end:
                    next_pieces.append((lock_end, piece_end))
            pieces = next_pieces
        remaining.extend(piece for piece in pieces if piece[1] > piece[0])
    return tuple(remaining)


def _apply_strength_limit(
    parameters: dict[str, JsonValue],
    kind: RescueActionKind,
    strength: float,
) -> None:
    if kind is RescueActionKind.ADJUST_LUMA:
        apply_luma_strength_limit(parameters, strength)
    elif kind is RescueActionKind.DENOISE_VIDEO:
        for key in (
            "luma_spatial",
            "chroma_spatial",
            "luma_temporal",
            "chroma_temporal",
        ):
            parameters[key] = float(cast(float, parameters[key])) * strength
    elif kind is RescueActionKind.SHARPEN:
        parameters["amount"] = float(cast(float, parameters["amount"])) * strength
        parameters["adaptive_strength"] = (
            float(cast(float, parameters["adaptive_strength"])) * strength
        )
        parameters["visibility_brightness"] = (
            float(cast(float, parameters["visibility_brightness"])) * strength
        )


def _strength_limited_audio_parameters(
    parameters: dict[str, float | int],
    kind: RescueActionKind,
    strength: float,
) -> dict[str, JsonValue]:
    bounded: dict[str, JsonValue] = dict(parameters)
    bounded["strength_limit"] = strength
    if kind is RescueActionKind.DENOISE_AUDIO and "maximum_reduction_db" in bounded:
        bounded["maximum_reduction_db"] = (
            float(cast(float, bounded["maximum_reduction_db"])) * strength
        )
    if kind is RescueActionKind.NORMALIZE_AUDIO and {
        "measured_I",
        "target_integrated_lufs",
    }.issubset(bounded):
        measured = float(cast(float, bounded["measured_I"]))
        target = float(cast(float, bounded["target_integrated_lufs"]))
        bounded["target_integrated_lufs"] = measured + (target - measured) * strength
    return bounded


def _strength_limited_transform(
    transform: dict[str, JsonValue], strength: float
) -> dict[str, JsonValue]:
    bounded = dict(transform)
    for key in ("rotation_degrees", "translation_x", "translation_y"):
        bounded[key] = float(cast(float, bounded[key])) * strength
    bounded["scale"] = 1.0 + (float(cast(float, bounded["scale"])) - 1.0) * strength
    return bounded


def _overlaps_any(
    candidate: tuple[float, float], ranges: Sequence[tuple[float, float]]
) -> bool:
    return any(candidate[0] < end and start < candidate[1] for start, end in ranges)


def _is_damaged_edge(interval: DamageInterval, duration_seconds: float) -> bool:
    return bool(
        float(interval.start_seconds) == 0.0
        or float(interval.end_seconds) == duration_seconds
    )


def _rotation_degrees(metadata: VideoMetadata) -> float:
    rotation = metadata.raw_probe.get("rotation")
    if isinstance(rotation, bool):
        return 0.0
    if isinstance(rotation, (int, float)) and math.isfinite(float(rotation)):
        return float(rotation)
    if isinstance(rotation, str):
        try:
            return float(rotation)
        except ValueError:
            return 0.0
    return 0.0


def _has_nonzero_rotation(metadata: VideoMetadata) -> bool:
    return _rotation_degrees(metadata) % 360 != 0


__all__ = ["build_rescue_plan"]
