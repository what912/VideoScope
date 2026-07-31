"""Vendor-neutral helpers for assigning samples to scene context."""

from __future__ import annotations

from videoscope.scenes import VideoScene


def scene_index_for_timestamp(
    scenes: tuple[VideoScene, ...],
    *,
    timestamp_seconds: float,
    fallback_index: int = 0,
) -> int:
    """Return the half-open scene containing a sample timestamp."""
    if not scenes:
        return 0
    for position, scene in enumerate(scenes):
        is_last = position == len(scenes) - 1
        if scene.start_seconds <= timestamp_seconds < scene.end_seconds or (
            is_last and timestamp_seconds == scene.end_seconds
        ):
            return scene.scene_index
    return len(scenes) + fallback_index


def scene_end_seconds(
    scenes: tuple[VideoScene, ...],
    *,
    scene_index: int,
    video_duration_seconds: float,
) -> float:
    """Return one scene end or the video end when scene context is absent."""
    for scene in scenes:
        if scene.scene_index == scene_index:
            return scene.end_seconds
    return video_duration_seconds


def internal_scene_boundaries(
    scenes: tuple[VideoScene, ...],
) -> tuple[float, ...]:
    """Return actual cut times, excluding video start and end."""
    return tuple(scene.start_seconds for scene in scenes[1:])


def is_inside_scene_boundary_guard(
    scenes: tuple[VideoScene, ...],
    *,
    timestamp_seconds: float,
    guard_seconds: float,
) -> bool:
    """Whether a timestamp lies in a symmetric guard around a scene cut."""
    if guard_seconds < 0:
        raise ValueError("guard_seconds must not be negative")
    return any(
        abs(timestamp_seconds - boundary) <= guard_seconds
        for boundary in internal_scene_boundaries(scenes)
    )
