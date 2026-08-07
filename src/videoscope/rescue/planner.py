"""Deterministic, review-gated planning for local Video Rescue."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from hashlib import sha256
from typing import cast

from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.rescue.audio import AudioAssessment, FixedOffsetAssessment
from videoscope.rescue.capabilities import (
    ActionCapabilityDecision,
    capability_review_warning,
    evaluate_action_capabilities,
)
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
    make_rescue_plan_digest,
    rescue_public_artifacts,
)
from videoscope.rescue.stabilization import StabilizationAssessment
from videoscope.rescue.visual import (
    FlickerCorrectionPlan,
    VisualAssessment,
    visual_action_parameters,
)

_ACTION_VERSION = "1"
_NON_DAMAGE_KINDS = frozenset(
    {DamageKind.DECODABLE, DamageKind.UNCERTAIN, DamageKind.MISSING_INFORMATION}
)
_PREVIEW_PRIORITY = {
    DamageKind.UNDECODABLE: 5,
    DamageKind.TIMESTAMP_DISCONTINUITY: 4,
    DamageKind.MISSING_STREAM: 4,
    DamageKind.FIXED_AV_OFFSET: 3,
    DamageKind.AUDIO_CLIPPING: 3,
    DamageKind.DARK: 2,
    DamageKind.VIDEO_NOISE: 2,
    DamageKind.SOFT_DETAIL: 2,
    DamageKind.FLICKER: 2,
    DamageKind.SHAKE: 2,
    DamageKind.LOW_LOUDNESS: 2,
    DamageKind.AUDIO_NOISE: 2,
}


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
) -> RescuePlan:
    """Build a path-free plan with stable action and preview selection order."""
    if metadata.duration_seconds != damage_map.duration_seconds:
        raise RescuePlanError("metadata duration does not match the damage map")
    locks = _normalized_ranges((*config.locked_ranges, *locked_ranges))
    proposed_actions = _build_actions(
        metadata,
        damage_map,
        strategy,
        config,
        locks,
        visual_assessment,
        flicker_correction,
        stabilization_assessment,
        audio_assessment,
        fixed_offset_assessment,
    )
    actions, preview_ranges, omitted = _capability_gated_actions(
        proposed_actions,
        damage_map=damage_map,
        config=config,
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
    plan_without_digest: dict[str, JsonValue] = {
        "input_hash": damage_map.input_hash,
        "strategy": strategy.value,
        "requested_symptoms": [item.value for item in requested_symptoms],
        "assessment_parameters": assessment_parameters or {},
        "assessment_limitations": list(assessment_limitations),
        "assessment_warnings": list(final_warnings),
        "effective_config": config.model_dump(mode="json"),
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
    return RescuePlan(
        input_hash=damage_map.input_hash,
        strategy=strategy,
        requested_symptoms=requested_symptoms,
        assessment_parameters=assessment_parameters or {},
        assessment_limitations=assessment_limitations,
        assessment_warnings=final_warnings,
        effective_config=config,
        actions=actions,
        preview_ranges=preview_ranges,
        public_artifacts=rescue_public_artifacts(
            include_improved=_supports_improved(actions)
        ),
        damage_intervals=damage_map.intervals,
        plan_digest=make_rescue_plan_digest(plan_without_digest),
    )


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
            if not action.changes_content or decision.automatic:
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
    preview_ranges = _select_preview_ranges(
        damage_map.intervals,
        actions,
        damage_map.duration_seconds,
        config,
    )
    if not preview_ranges and any(action.requires_confirmation for action in actions):
        return (
            (
                0.0,
                min(damage_map.duration_seconds, config.max_preview_total_seconds),
            ),
        )
    return preview_ranges


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
) -> tuple[RescueAction, ...]:
    duration_range = ((0.0, damage_map.duration_seconds),)
    by_kind = {
        kind: tuple(item for item in damage_map.intervals if item.kind is kind)
        for kind in DamageKind
    }
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
                damage_map.duration_seconds,
                config,
                locked_ranges,
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
    return tuple(actions)


def _balanced_actions(
    by_kind: dict[DamageKind, tuple[DamageInterval, ...]],
    strategy: RescueStrategy,
    visual_assessment: VisualAssessment | None,
    flicker_correction: FlickerCorrectionPlan | None,
    stabilization_assessment: StabilizationAssessment | None,
    audio_assessment: AudioAssessment | None,
    duration_seconds: float,
    config: RescueEffectiveConfig,
    locked_ranges: tuple[tuple[float, float], ...],
) -> tuple[RescueAction, ...]:
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
        parameters.update(visual_action_parameters(kind))
        _apply_strength_limit(parameters, kind, config.balanced_strength_limit)
        if kind in {
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
        }:
            assert visual_assessment is not None
            parameters.update(
                {
                    "assessment_metrics": visual_assessment.metrics.model_dump(
                        mode="json"
                    ),
                    "assessment_evidence": [
                        item.model_dump(mode="json")
                        for item in visual_assessment.evidence
                        if item.action is kind
                    ],
                    "assessment_limitations": list(visual_assessment.limitations),
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
    if audio_assessment is not None and not locked_ranges:
        for kind in audio_assessment.recommended_actions:
            if kind not in {
                RescueActionKind.NORMALIZE_AUDIO,
                RescueActionKind.DENOISE_AUDIO,
            }:
                continue
            actions.append(
                _action(
                    kind,
                    "Apply the bounded audio adjustment supported by measured values.",
                    ((0.0, duration_seconds),),
                    _strength_limited_audio_parameters(
                        audio_assessment.parameters,
                        kind,
                        config.balanced_strength_limit,
                    ),
                    strategy,
                    changes_content=True,
                )
            )
    return tuple(actions)


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
    identity = {
        "kind": kind.value,
        "parameters": parameters,
        "source_ranges": source_ranges,
        "strategy": strategy.value,
        "version": _ACTION_VERSION,
    }
    encoded = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return RescueAction(
        id="rescue_action_" + sha256(encoded.encode("utf-8")).hexdigest(),
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
    improvement_kinds = {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.SHARPEN,
        RescueActionKind.DEFLICKER,
        RescueActionKind.STABILIZE,
        RescueActionKind.NORMALIZE_AUDIO,
        RescueActionKind.DENOISE_AUDIO,
    }
    return any(action.kind in improvement_kinds for action in actions)


def _select_preview_ranges(
    intervals: Sequence[DamageInterval],
    actions: Sequence[RescueAction],
    duration_seconds: float,
    config: RescueEffectiveConfig,
) -> tuple[tuple[float, float], ...]:
    remaining = config.max_preview_total_seconds
    selected: list[tuple[float, float]] = []
    for action in actions:
        if not action.requires_confirmation or _ranges_intersect(
            action.source_ranges, selected
        ):
            continue
        action_ranges = sorted(
            action.source_ranges,
            key=lambda item: (-(item[1] - item[0]), item[0], item[1]),
        )
        for source_range in action_ranges:
            candidate = _action_preview_range(
                action,
                source_range,
                duration_seconds=duration_seconds,
                maximum_duration=remaining,
            )
            if candidate[1] - candidate[0] < 1e-6 or _overlaps_any(
                candidate, tuple(selected)
            ):
                continue
            selected.append(candidate)
            remaining -= candidate[1] - candidate[0]
            break
        if len(selected) >= config.max_preview_ranges or remaining < 1e-6:
            return tuple(sorted(selected))
    candidates = sorted(
        (item for item in intervals if item.kind not in _NON_DAMAGE_KINDS),
        key=lambda item: (
            -_PREVIEW_PRIORITY.get(item.kind, 1),
            -(item.end_seconds - item.start_seconds),
            item.start_seconds,
            item.end_seconds,
            item.kind.value,
            item.id,
        ),
    )
    for item in candidates:
        if len(selected) >= config.max_preview_ranges or remaining < 1e-6:
            break
        candidate = (
            item.start_seconds,
            min(item.end_seconds, item.start_seconds + remaining),
        )
        if candidate[1] - candidate[0] < 1e-6 or _overlaps_any(
            candidate, tuple(selected)
        ):
            continue
        selected.append(candidate)
        remaining -= candidate[1] - candidate[0]
    return tuple(sorted(selected))


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
        parameters["brightness"] = (
            float(cast(float, parameters["brightness"])) * strength
        )
        parameters["contrast"] = (
            1.0 + (float(cast(float, parameters["contrast"])) - 1.0) * strength
        )
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
