"""Deterministic execution capability policy for private Rescue planning."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Literal

from videoscope.rescue.errors import RescueMediaError
from videoscope.rescue.models import RescueAction, RescueActionKind, RescuePlan
from videoscope.rescue.timeline import (
    SourceMapping,
    mappings_for_ranges,
    mappings_match_retained_ranges,
    retained_source_ranges,
    timestamp_in_half_open_range,
)

_MINIMUM_PRIVATE_PREVIEW_SECONDS: Final = 1e-6


class ActionCapabilityReason(StrEnum):
    """Finite reasons why a proposed action is or is not automatically eligible."""

    ELIGIBLE = "eligible"
    PREVIEW_RENDERER_UNAVAILABLE = "preview_renderer_unavailable"
    PREVIEW_RANGE_UNCOVERED = "preview_range_uncovered"
    LOCKED_RANGE_CONFLICT = "locked_range_conflict"
    RANGE_MAPPING_UNAVAILABLE = "range_mapping_unavailable"


@dataclass(frozen=True, slots=True)
class ActionCapabilityDecision:
    """Private, reproducible capability facts for one proposed action."""

    action_id: str
    preview_supported: bool
    preview_covered: bool
    range_exact: bool
    verification_mode: Literal["native", "needs_review"]
    automatic: bool
    reason: ActionCapabilityReason


@dataclass(frozen=True, slots=True)
class _ActionCapabilityProfile:
    preview_supported: bool
    verification_mode: Literal["native", "needs_review"]
    range_mode: Literal["local", "full_duration_unlocked"]


_ACTION_CAPABILITY_PROFILES: Final[
    Mapping[RescueActionKind, _ActionCapabilityProfile]
] = MappingProxyType(
    {
        RescueActionKind.REMUX: _ActionCapabilityProfile(True, "native", "local"),
        RescueActionKind.REBUILD_TIMESTAMPS: _ActionCapabilityProfile(
            True, "native", "local"
        ),
        RescueActionKind.SELECT_TRACKS: _ActionCapabilityProfile(
            False, "native", "local"
        ),
        RescueActionKind.NORMALIZE_ROTATION: _ActionCapabilityProfile(
            True, "native", "full_duration_unlocked"
        ),
        RescueActionKind.SALVAGE_SEGMENTS: _ActionCapabilityProfile(
            True, "native", "local"
        ),
        RescueActionKind.TRIM_DAMAGED_EDGES: _ActionCapabilityProfile(
            True, "native", "local"
        ),
        RescueActionKind.CORRECT_FIXED_AV_OFFSET: _ActionCapabilityProfile(
            True, "native", "full_duration_unlocked"
        ),
        RescueActionKind.ADJUST_LUMA: _ActionCapabilityProfile(True, "native", "local"),
        RescueActionKind.DENOISE_VIDEO: _ActionCapabilityProfile(
            True, "native", "local"
        ),
        RescueActionKind.SHARPEN: _ActionCapabilityProfile(True, "native", "local"),
        RescueActionKind.DEFLICKER: _ActionCapabilityProfile(True, "native", "local"),
        RescueActionKind.STABILIZE: _ActionCapabilityProfile(True, "native", "local"),
        RescueActionKind.NORMALIZE_AUDIO: _ActionCapabilityProfile(
            True, "native", "local"
        ),
        RescueActionKind.DENOISE_AUDIO: _ActionCapabilityProfile(
            True, "native", "local"
        ),
        RescueActionKind.VERIFY: _ActionCapabilityProfile(True, "native", "local"),
        RescueActionKind.DEBLUR: _ActionCapabilityProfile(
            True, "needs_review", "local"
        ),
    }
)


def evaluate_action_capabilities(
    actions: Sequence[RescueAction],
    preview_ranges: Sequence[tuple[float, float]],
    *,
    duration_seconds: float,
    locked_ranges: Sequence[tuple[float, float]],
) -> tuple[ActionCapabilityDecision, ...]:
    """Evaluate proposed actions without changing their order or public models."""
    decisions: list[ActionCapabilityDecision] = []
    for action in actions:
        profile = _ACTION_CAPABILITY_PROFILES.get(action.kind)
        if profile is None:
            decisions.append(
                ActionCapabilityDecision(
                    action_id=action.id,
                    preview_supported=False,
                    preview_covered=False,
                    range_exact=False,
                    verification_mode="needs_review",
                    automatic=False,
                    reason=ActionCapabilityReason.PREVIEW_RENDERER_UNAVAILABLE,
                )
            )
            continue
        if not action.changes_content:
            decisions.append(
                ActionCapabilityDecision(
                    action_id=action.id,
                    preview_supported=True,
                    preview_covered=True,
                    range_exact=True,
                    verification_mode="native",
                    automatic=True,
                    reason=ActionCapabilityReason.ELIGIBLE,
                )
            )
            continue

        preview_supported = profile.preview_supported
        correction_timestamps = _anchor_correction_timestamps(action)
        if correction_timestamps is not None:
            preview_covered = _anchor_ranges_have_private_preview(
                action.source_ranges,
                preview_ranges,
                correction_timestamps,
            )
        elif action.kind in {
            RescueActionKind.SALVAGE_SEGMENTS,
            RescueActionKind.TRIM_DAMAGED_EDGES,
        }:
            preview_covered = _structural_ranges_have_retained_preview(
                action.source_ranges, preview_ranges
            )
        else:
            preview_covered = bool(action.source_ranges) and all(
                _range_has_private_preview(source_range, preview_ranges)
                for source_range in action.source_ranges
            )
        range_exact = bool(action.source_ranges) and all(
            start_seconds < end_seconds <= duration_seconds
            for start_seconds, end_seconds in action.source_ranges
        )
        if correction_timestamps is not None and not (
            _anchor_ranges_have_corrections(
                action.source_ranges,
                correction_timestamps,
            )
        ):
            range_exact = False
        range_reason = ActionCapabilityReason.RANGE_MAPPING_UNAVAILABLE
        if locked_ranges and (
            profile.range_mode == "full_duration_unlocked"
            or _ranges_intersect(action.source_ranges, locked_ranges)
        ):
            range_exact = False
            range_reason = ActionCapabilityReason.LOCKED_RANGE_CONFLICT
        elif (
            profile.range_mode == "full_duration_unlocked"
            and action.source_ranges != ((0.0, duration_seconds),)
        ):
            range_exact = False

        verification_mode = profile.verification_mode
        if not preview_supported:
            reason = ActionCapabilityReason.PREVIEW_RENDERER_UNAVAILABLE
        elif not range_exact:
            reason = range_reason
        elif not preview_covered:
            reason = ActionCapabilityReason.PREVIEW_RANGE_UNCOVERED
        elif verification_mode == "needs_review":
            reason = ActionCapabilityReason.RANGE_MAPPING_UNAVAILABLE
        else:
            reason = ActionCapabilityReason.ELIGIBLE
        automatic = reason is ActionCapabilityReason.ELIGIBLE
        decisions.append(
            ActionCapabilityDecision(
                action_id=action.id,
                preview_supported=preview_supported,
                preview_covered=preview_covered,
                range_exact=range_exact,
                verification_mode=verification_mode,
                automatic=automatic,
                reason=reason,
            )
        )
    return tuple(decisions)


def capability_review_warning(
    action: RescueAction, decision: ActionCapabilityDecision
) -> str:
    """Return one path-free, deterministic review warning for an omitted action."""
    return (
        f"Automatic {action.kind.value} action needs review: {decision.reason.value}."
    )


def action_verification_mode(
    kind: RescueActionKind,
) -> Literal["native", "needs_review"]:
    """Return the explicit preview-allocation priority class for an action kind."""
    profile = _ACTION_CAPABILITY_PROFILES.get(kind)
    if profile is None:
        return "needs_review"
    return profile.verification_mode


def require_executable_action_scopes(
    plan: RescuePlan, mappings: Sequence[SourceMapping] | None
) -> None:
    """Reject forged global or stabilization actions before media execution."""
    duration = max(
        (
            end
            for action in plan.actions
            if action.kind is RescueActionKind.REMUX
            for _start, end in action.source_ranges
        ),
        default=0.0,
    )
    retained_ranges = retained_source_ranges(plan)
    resolved_mappings = mappings
    if resolved_mappings is None:
        if retained_ranges != ((0.0, duration),):
            raise RescueMediaError("confirmed faithful source mapping is required")
        resolved_mappings = mappings_for_ranges(retained_ranges, "faithful-rescue.mp4")
    if any(action.changes_content for action in plan.actions) and not (
        mappings_match_retained_ranges(resolved_mappings, retained_ranges)
    ):
        raise RescueMediaError("confirmed Rescue action scope is not executable")
    protected_kinds = {
        RescueActionKind.NORMALIZE_ROTATION,
        RescueActionKind.CORRECT_FIXED_AV_OFFSET,
        RescueActionKind.STABILIZE,
    }
    protected = tuple(
        action for action in plan.actions if action.kind in protected_kinds
    )
    if not protected:
        return
    decisions = evaluate_action_capabilities(
        protected,
        plan.preview_ranges,
        duration_seconds=duration,
        locked_ranges=plan.effective_config.locked_ranges,
    )
    for action, decision in zip(protected, decisions, strict=True):
        if not decision.automatic:
            raise RescueMediaError("confirmed Rescue action scope is not executable")
        # Rotation needs the complete original timeline.  A constant A/V offset,
        # however, is applied after concatenating every retained segment: a
        # deliberate damaged-source deletion must not turn that otherwise exact
        # output-time correction into an unsafe no-op.  It still requires every
        # retained source range to be represented exactly.
        requires_full_source = action.kind is RescueActionKind.NORMALIZE_ROTATION
        requires_all_retained_ranges = (
            action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
        )
        if requires_full_source and not _mappings_cover_full_output(
            resolved_mappings, duration
        ):
            raise RescueMediaError("confirmed Rescue action scope is not executable")
        if requires_all_retained_ranges and not _mappings_cover_retained_ranges(
            resolved_mappings, retained_ranges
        ):
            raise RescueMediaError("confirmed Rescue action scope is not executable")


def _mappings_cover_full_output(
    mappings: Sequence[SourceMapping] | None, duration: float
) -> bool:
    if mappings is None or not mappings or not math.isfinite(duration) or duration <= 0:
        return False
    source_cursor = 0.0
    output_cursor = 0.0
    for mapping in sorted(mappings, key=lambda item: item.output_start):
        values = (
            mapping.source_start,
            mapping.source_end,
            mapping.output_start,
            mapping.output_end,
        )
        if not all(math.isfinite(value) for value in values):
            return False
        if (
            abs(mapping.source_start - source_cursor) > 1e-9
            or abs(mapping.output_start - output_cursor) > 1e-9
            or mapping.source_end <= mapping.source_start
            or mapping.output_end <= mapping.output_start
        ):
            return False
        source_cursor = mapping.source_end
        output_cursor = mapping.output_end
    return abs(source_cursor - duration) <= 1e-9


def _mappings_cover_retained_ranges(
    mappings: Sequence[SourceMapping] | None,
    retained_ranges: Sequence[tuple[float, float]],
) -> bool:
    """Compatibility wrapper for the shared plan-bound mapping predicate."""
    return bool(mappings_match_retained_ranges(mappings, retained_ranges))


def _ranges_intersect(
    first: Sequence[tuple[float, float]],
    second: Sequence[tuple[float, float]],
) -> bool:
    return bool(
        any(
            first_start < second_end and second_start < first_end
            for first_start, first_end in first
            for second_start, second_end in second
        )
    )


def _range_has_private_preview(
    source_range: tuple[float, float],
    preview_ranges: Sequence[tuple[float, float]],
) -> bool:
    """Require positive representative coverage for one half-open operation range."""
    source_start, source_end = source_range
    return any(
        min(source_end, preview_end) - max(source_start, preview_start)
        >= _MINIMUM_PRIVATE_PREVIEW_SECONDS
        for preview_start, preview_end in preview_ranges
    )


def _anchor_correction_timestamps(action: RescueAction) -> tuple[float, ...] | None:
    if action.kind is not RescueActionKind.STABILIZE or (
        action.parameters.get("method") not in {"anchor_v1", "transition_anchor_v1"}
    ):
        return None
    raw_transforms = action.parameters.get("motion_transforms")
    if not isinstance(raw_transforms, list):
        return ()
    timestamps: list[float] = []
    for transform in raw_transforms:
        if not isinstance(transform, dict):
            return ()
        timestamp = transform.get("timestamp_seconds")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(float(timestamp))
        ):
            return ()
        timestamps.append(float(timestamp))
    return tuple(timestamps)


def _anchor_ranges_have_corrections(
    source_ranges: Sequence[tuple[float, float]],
    correction_timestamps: Sequence[float],
) -> bool:
    return bool(source_ranges) and all(
        any(
            timestamp_in_half_open_range(
                timestamp,
                start,
                end,
            )
            for timestamp in correction_timestamps
        )
        for start, end in source_ranges
    )


def _anchor_ranges_have_private_preview(
    source_ranges: Sequence[tuple[float, float]],
    preview_ranges: Sequence[tuple[float, float]],
    correction_timestamps: Sequence[float],
) -> bool:
    return bool(source_ranges) and all(
        any(
            timestamp_in_half_open_range(
                timestamp,
                source_start,
                source_end,
            )
            and timestamp_in_half_open_range(
                timestamp,
                preview_start,
                preview_end,
            )
            for timestamp in correction_timestamps
            for preview_start, preview_end in preview_ranges
        )
        for source_start, source_end in source_ranges
    )


def _structural_ranges_have_retained_preview(
    source_ranges: Sequence[tuple[float, float]],
    preview_ranges: Sequence[tuple[float, float]],
) -> bool:
    if not source_ranges:
        return False
    for source_range in source_ranges:
        represented = False
        for preview_range in preview_ranges:
            if not _range_has_private_preview(source_range, (preview_range,)):
                continue
            preview_start, preview_end = preview_range
            removed_overlap = sum(
                max(0.0, min(preview_end, end) - max(preview_start, start))
                for start, end in source_ranges
            )
            if (
                preview_end - preview_start - removed_overlap
                >= _MINIMUM_PRIVATE_PREVIEW_SECONDS
            ):
                represented = True
                break
        if not represented:
            return False
    return True


__all__ = [
    "ActionCapabilityDecision",
    "ActionCapabilityReason",
    "action_verification_mode",
    "capability_review_warning",
    "evaluate_action_capabilities",
    "require_executable_action_scopes",
]
