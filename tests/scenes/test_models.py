"""Tests for scene domain models and deterministic normalization."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from videoscope.scenes import (
    SceneDetectionResult,
    VideoScene,
    fixed_window_scenes,
    scenes_from_cuts,
)


def test_video_scene_rejects_inconsistent_duration() -> None:
    with pytest.raises(ValidationError, match="duration_seconds"):
        VideoScene(
            scene_index=0,
            start_seconds=1.0,
            end_seconds=3.0,
            duration_seconds=1.0,
            representative_timestamp=2.0,
        )


def test_scene_boundaries_are_sorted_continuous_and_bounded() -> None:
    scenes = scenes_from_cuts(
        [4.0, 2.0, 2.0, -1.0, 8.0],
        duration_seconds=6.0,
    )

    assert [
        (scene.scene_index, scene.start_seconds, scene.end_seconds) for scene in scenes
    ] == [
        (0, 0.0, 2.0),
        (1, 2.0, 4.0),
        (2, 4.0, 6.0),
    ]
    assert all(
        left.end_seconds == right.start_seconds
        for left, right in zip(scenes, scenes[1:], strict=False)
    )
    assert scenes[-1].end_seconds == 6.0


def test_representative_timestamps_and_sorting_are_deterministic() -> None:
    first = scenes_from_cuts([4.0, 2.0], duration_seconds=6.0)
    repeated = scenes_from_cuts([2.0, 4.0], duration_seconds=6.0)

    assert first == repeated
    assert [scene.representative_timestamp for scene in first] == [1.0, 3.0, 5.0]


def test_short_scenes_merge_with_deterministic_neighbor_rule() -> None:
    scenes = scenes_from_cuts(
        [0.2, 2.0, 5.8],
        duration_seconds=6.0,
        minimum_duration_seconds=0.5,
    )

    assert [(scene.start_seconds, scene.end_seconds) for scene in scenes] == [
        (0.0, 2.0),
        (2.0, 6.0),
    ]
    assert [scene.scene_index for scene in scenes] == [0, 1]


def test_no_cuts_returns_one_scene_covering_video() -> None:
    scenes = scenes_from_cuts([], duration_seconds=6.0)

    assert len(scenes) == 1
    assert scenes[0].start_seconds == 0.0
    assert scenes[0].end_seconds == 6.0
    assert scenes[0].representative_timestamp == 3.0


def test_fixed_window_fallback_covers_full_duration() -> None:
    scenes = fixed_window_scenes(duration_seconds=5.0, window_seconds=2.0)

    assert [(scene.start_seconds, scene.end_seconds) for scene in scenes] == [
        (0.0, 2.0),
        (2.0, 4.0),
        (4.0, 5.0),
    ]
    result = SceneDetectionResult(
        source="fixed-window-fallback",
        scenes=scenes,
        warnings=("primary failed",),
    )
    assert result.warnings == ("primary failed",)
