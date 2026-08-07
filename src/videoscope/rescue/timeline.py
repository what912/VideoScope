"""Shared source-to-output timeline construction for Video Rescue."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from videoscope.rescue.errors import RescueInputError, RescueMediaError
from videoscope.rescue.models import DamageKind, RescueActionKind, RescuePlan

# Native container measurements can differ slightly from source timestamps.
# This is the same parity tolerance used by Rescue verification.
DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS: Final = 0.25


@dataclass(frozen=True, slots=True)
class SourceMapping:
    """One exact retained source interval mapped onto a Rescue output."""

    source_start: float
    source_end: float
    output_start: float
    output_end: float
    output_relative_path: str


def retained_source_ranges(plan: RescuePlan) -> tuple[tuple[float, float], ...]:
    """Return source ranges retained by the plan's bound structural actions."""
    duration = _source_duration(plan)
    if not _has_segment_salvage(plan):
        return ((0.0, duration),)
    authorized_damage_ids = {
        value
        for action in plan.actions
        if action.kind
        in {
            RescueActionKind.SALVAGE_SEGMENTS,
            RescueActionKind.TRIM_DAMAGED_EDGES,
        }
        for values in (action.parameters.get("damage_ids"),)
        if isinstance(values, list)
        for value in values
        if isinstance(value, str)
    }
    damaged = sorted(
        (
            max(0.0, float(item.start_seconds)),
            min(duration, float(item.end_seconds)),
        )
        for item in plan.damage_intervals
        if item.kind is DamageKind.UNDECODABLE
        and item.id in authorized_damage_ids
        and item.end_seconds > item.start_seconds
    )
    merged: list[tuple[float, float]] = []
    for start, end in damaged:
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    retained: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if cursor < start:
            retained.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration:
        retained.append((cursor, duration))
    if not retained:
        raise RescueMediaError("confirmed plan retains no decodable source interval")
    return tuple(retained)


def mappings_for_ranges(
    ranges: Sequence[tuple[float, float]],
    output_relative_path: str,
) -> tuple[SourceMapping, ...]:
    """Map ordered source ranges affinely onto one contiguous output timeline."""
    cursor = 0.0
    result = []
    for start, end in ranges:
        result.append(
            SourceMapping(
                start, end, cursor, cursor + end - start, output_relative_path
            )
        )
        cursor += end - start
    return tuple(result)


def mappings_match_retained_ranges(
    mappings: Sequence[SourceMapping] | None,
    retained_ranges: Sequence[tuple[float, float]],
    *,
    duration_tolerance_seconds: float = DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS,
) -> bool:
    """Return whether mappings exactly cover the plan-bound retained timeline."""
    if (
        mappings is None
        or not mappings
        or not retained_ranges
        or not math.isfinite(duration_tolerance_seconds)
        or duration_tolerance_seconds < 0
    ):
        return False
    expected = tuple(retained_ranges)
    observed = tuple(sorted(mappings, key=lambda item: item.output_start))
    if len(observed) != len(expected):
        return False
    output_cursor = 0.0
    retained_duration = 0.0
    for mapping, (expected_start, expected_end) in zip(observed, expected, strict=True):
        values = (
            mapping.source_start,
            mapping.source_end,
            mapping.output_start,
            mapping.output_end,
            expected_start,
            expected_end,
        )
        if not all(math.isfinite(value) for value in values):
            return False
        if (
            mapping.source_end <= mapping.source_start
            or mapping.output_end <= mapping.output_start
            or not math.isclose(
                mapping.source_start, expected_start, rel_tol=0.0, abs_tol=1e-9
            )
            or not math.isclose(
                mapping.source_end, expected_end, rel_tol=0.0, abs_tol=1e-9
            )
            or not math.isclose(
                mapping.output_start, output_cursor, rel_tol=0.0, abs_tol=1e-9
            )
            or not math.isclose(
                mapping.source_end - mapping.source_start,
                mapping.output_end - mapping.output_start,
                rel_tol=0.0,
                abs_tol=duration_tolerance_seconds,
            )
        ):
            return False
        retained_duration += expected_end - expected_start
        output_cursor = mapping.output_end
    return math.isclose(
        retained_duration,
        output_cursor,
        rel_tol=0.0,
        abs_tol=duration_tolerance_seconds,
    )


def preview_source_mappings(
    plan: RescuePlan,
    window: tuple[float, float],
    output_relative_path: str,
) -> tuple[SourceMapping, ...]:
    """Intersect retained source time with a preview and rebase its output time."""
    window_start, window_end = window
    ranges = tuple(
        (max(start, window_start), min(end, window_end))
        for start, end in retained_source_ranges(plan)
        if min(end, window_end) > max(start, window_start)
    )
    return mappings_for_ranges(ranges, output_relative_path)


def _source_duration(plan: RescuePlan) -> float:
    candidates = [
        float(end)
        for action in plan.actions
        if action.kind is RescueActionKind.REMUX
        for _start, end in action.source_ranges
    ]
    if not candidates or not math.isfinite(max(candidates)) or max(candidates) <= 0:
        raise RescueInputError("confirmed plan has no positive source duration")
    return max(candidates)


def _has_segment_salvage(plan: RescuePlan) -> bool:
    return any(
        action.kind is RescueActionKind.SALVAGE_SEGMENTS for action in plan.actions
    )


__all__ = [
    "DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS",
    "SourceMapping",
    "mappings_for_ranges",
    "mappings_match_retained_ranges",
    "preview_source_mappings",
    "retained_source_ranges",
]
