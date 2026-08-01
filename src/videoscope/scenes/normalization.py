"""Deterministic conversion of cut boundaries into scene intervals."""

from __future__ import annotations

import math
from collections.abc import Iterable

from videoscope.scenes.models import VideoScene

_BOUNDARY_TOLERANCE = 1e-9


def _make_scene(
    *,
    scene_index: int,
    start_seconds: float,
    end_seconds: float,
) -> VideoScene:
    return VideoScene(
        scene_index=scene_index,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        duration_seconds=end_seconds - start_seconds,
        representative_timestamp=(start_seconds + end_seconds) / 2.0,
    )


def _normalized_boundaries(
    cut_seconds: Iterable[float],
    *,
    duration_seconds: float,
) -> list[float]:
    interior = sorted(
        {
            float(boundary)
            for boundary in cut_seconds
            if math.isfinite(boundary)
            and boundary > _BOUNDARY_TOLERANCE
            and boundary < duration_seconds - _BOUNDARY_TOLERANCE
        }
    )
    return [0.0, *interior, duration_seconds]


def _merge_short_intervals(
    intervals: list[tuple[float, float]],
    *,
    minimum_duration_seconds: float,
) -> list[tuple[float, float]]:
    merged = intervals.copy()
    while len(merged) > 1:
        short_index = next(
            (
                index
                for index, (start, end) in enumerate(merged)
                if end - start < minimum_duration_seconds
            ),
            None,
        )
        if short_index is None:
            break
        if short_index == 0:
            current_start, _ = merged[0]
            _, next_end = merged[1]
            merged[0:2] = [(current_start, next_end)]
        else:
            previous_start, _ = merged[short_index - 1]
            _, current_end = merged[short_index]
            merged[short_index - 1 : short_index + 1] = [(previous_start, current_end)]
    return merged


def scenes_from_cuts(
    cut_seconds: Iterable[float],
    *,
    duration_seconds: float,
    minimum_duration_seconds: float = 0.0,
) -> tuple[VideoScene, ...]:
    """Create sorted, continuous scenes and merge short intervals."""
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise ValueError("duration_seconds must be finite and non-negative")
    if not math.isfinite(minimum_duration_seconds) or minimum_duration_seconds < 0:
        raise ValueError("minimum_duration_seconds must be finite and non-negative")

    boundaries = _normalized_boundaries(
        cut_seconds,
        duration_seconds=duration_seconds,
    )
    intervals = list(zip(boundaries, boundaries[1:], strict=False))
    if not intervals:
        intervals = [(0.0, 0.0)]
    intervals = _merge_short_intervals(
        intervals,
        minimum_duration_seconds=minimum_duration_seconds,
    )
    return tuple(
        _make_scene(
            scene_index=scene_index,
            start_seconds=start,
            end_seconds=end,
        )
        for scene_index, (start, end) in enumerate(intervals)
    )


def fixed_window_scenes(
    *,
    duration_seconds: float,
    window_seconds: float,
) -> tuple[VideoScene, ...]:
    """Create deterministic fallback windows spanning the full duration."""
    if not math.isfinite(window_seconds) or window_seconds <= 0:
        raise ValueError("window_seconds must be finite and greater than zero")
    cuts: list[float] = []
    boundary = window_seconds
    while boundary < duration_seconds:
        cuts.append(boundary)
        boundary += window_seconds
    return scenes_from_cuts(cuts, duration_seconds=duration_seconds)
