"""Tests for bounded, deterministic flicker and motion stabilization helpers."""

from __future__ import annotations

import math
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import ValidationError

from videoscope.domain import VideoMetadata
from videoscope.rescue.assessment import RescueAssessmentBundle
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueMediaError,
)
from videoscope.rescue.executor import (
    CommandResult,
    NativeRescueExecutor,
    SourceMapping,
)
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueActionKind,
    RescueEffectiveConfig,
    RescueStrategy,
    make_damage_id,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    RescuePipelineDependencies,
    VideoRescuePipeline,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationAssessment,
    StabilizationConfig,
    assess_anchor_corrections,
    assess_stabilization,
    estimate_anchor_corrections,
    estimate_motion_transforms,
    estimate_transition_anchor_corrections,
    measure_transition_source_consensus,
    motion_correction_at_timestamp,
    motion_corrections_for_timestamps,
    render_stabilized_video,
    select_stable_anchor,
    smooth_motion_transforms,
)
from videoscope.rescue.visual import (
    FlickerConfig,
    FlickerCorrectionPlan,
    filter_fragment_from_action,
    flicker_filter_fragment,
    flicker_gains_for_timestamps,
    plan_flicker_correction,
    remap_flicker_correction,
    render_deflickered_video,
)
from videoscope.scenes.models import VideoScene

REAL_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "generated"
LOCAL_FFMPEG_ROOT = (
    Path.home()
    / "AppData"
    / "Local"
    / "VideoScope"
    / "tools"
    / "ffmpeg-8.1.2"
    / "ffmpeg-8.1.2-essentials_build"
    / "bin"
)


def _mp4v_fourcc() -> int:
    import cv2

    return cast(int, cast(Any, cv2).VideoWriter_fourcc(*"mp4v"))


def _real_rescue_fixture(filename: str) -> Path:
    ffmpeg = shutil.which("ffmpeg") or str(LOCAL_FFMPEG_ROOT / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(LOCAL_FFMPEG_ROOT / "ffprobe.exe")
    if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        pytest.skip(
            "FFmpeg and ffprobe on PATH are required for real stabilization acceptance"
        )
    source = REAL_FIXTURE_ROOT / filename
    if not source.is_file():
        pytest.skip(
            "run `python scripts/generate_test_videos.py --force` before real "
            "stabilization acceptance"
        )
    return source


def _scenes_with_cut() -> tuple[VideoScene, ...]:
    return (
        VideoScene(
            scene_index=0,
            start_seconds=0.0,
            end_seconds=2.0,
            duration_seconds=2.0,
            representative_timestamp=1.0,
        ),
        VideoScene(
            scene_index=1,
            start_seconds=2.0,
            end_seconds=4.0,
            duration_seconds=2.0,
            representative_timestamp=3.0,
        ),
    )


def _flicker_config() -> FlickerConfig:
    return FlickerConfig(
        low_frequency_window_samples=5,
        scene_guard_seconds=0.25,
        minimum_repetitions=3,
        residual_threshold=0.04,
        maximum_gain=1.08,
    )


def test_anchor_corrections_remove_every_24fps_periodic_translation() -> None:
    """Catches accumulating adjacent estimates or dropping source-rate frames."""
    import cv2

    fps = 24.0
    rng = np.random.default_rng(814)
    background = rng.integers(0, 256, size=(120, 192), dtype=np.uint8)
    frames: list[tuple[float, np.ndarray]] = []
    expected_offsets: list[tuple[float, float]] = []
    for index in range(96):
        timestamp = index / fps
        x = 14.0 * math.sin(2.0 * math.pi * 2.0 * timestamp)
        y = 7.0 * math.sin(2.0 * math.pi * 1.5 * timestamp)
        affine = np.array(((1.0, 0.0, x), (0.0, 1.0, y)), dtype=np.float32)
        frame = cv2.warpAffine(
            background,
            affine,
            (background.shape[1], background.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        frames.append((timestamp, frame))
        expected_offsets.append((x, y))

    config = StabilizationConfig(
        frame_width=192,
        frame_height=120,
        source_rate_cap_fps=30.0,
        maximum_frame_inventory=120,
        minimum_background_coverage=0.05,
        minimum_anchor_inlier_ratio=0.65,
        maximum_anchor_residual_pixels=1.0,
        maximum_rotation_degrees=1.0,
        maximum_scale_excursion=0.02,
        maximum_intentional_trend_pixels_per_frame=1.0,
        maximum_consecutive_low_confidence_frames=1,
    )

    anchor_index = select_stable_anchor(frames, config)
    corrections = estimate_anchor_corrections(frames, config)

    assert anchor_index is not None
    assert len(corrections) == len(frames)
    assert tuple(item.timestamp_seconds for item in corrections) == tuple(
        timestamp for timestamp, _frame in frames
    )
    assert {item.semantics for item in corrections} == {"frame_correction"}

    anchor_x, anchor_y = expected_offsets[anchor_index]
    residuals = np.asarray(
        [
            math.hypot(
                source_x + correction.translation_x - anchor_x,
                source_y + correction.translation_y - anchor_y,
            )
            for (source_x, source_y), correction in zip(
                expected_offsets, corrections, strict=True
            )
        ],
        dtype=np.float64,
    )
    assert float(np.median(residuals)) <= config.residual_goal_median_pixels
    assert float(np.percentile(residuals, 90)) <= config.residual_goal_p90_pixels


def _crossfade_transition_case() -> tuple[
    tuple[tuple[float, NDArray[np.uint8]], ...],
    tuple[tuple[float, float], ...],
    StabilizationConfig,
]:
    import cv2

    fps = 24.0
    rng = np.random.default_rng(20260815)
    old = rng.integers(0, 256, size=(96, 160), dtype=np.uint8)
    new = rng.integers(0, 256, size=(96, 160), dtype=np.uint8)
    frames: list[tuple[float, NDArray[np.uint8]]] = []
    offsets: list[tuple[float, float]] = []
    for index in range(48):
        timestamp = index / fps
        alpha = min(1.0, index / 23.0)
        composite = cv2.addWeighted(old, 1.0 - alpha, new, alpha, 0.0)
        x = 3.0 * math.sin(2.0 * math.pi * 3.0 * timestamp)
        y = 2.0 * math.sin(2.0 * math.pi * 2.0 * timestamp)
        shifted = cv2.warpAffine(
            composite,
            np.array(((1.0, 0.0, x), (0.0, 1.0, y)), dtype=np.float32),
            (160, 96),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT,
        )
        frames.append((timestamp, shifted))
        offsets.append((x, y))

    return (
        tuple(frames),
        tuple(offsets),
        StabilizationConfig(
            frame_width=160,
            frame_height=96,
            maximum_frame_inventory=60,
            source_rate_cap_fps=24.0,
            max_crop_ratio=0.12,
        ),
    )


def test_transition_anchor_stabilizes_crossfade_and_post_composite_jitter() -> None:
    """Catches omitting measured global shake during a crossfade."""
    frames, offsets, config = _crossfade_transition_case()
    anchor = estimate_anchor_corrections(frames[24:], config)
    corrections = estimate_transition_anchor_corrections(
        frames,
        config,
        transition_range=(0.0, 1.0),
        following_anchor_corrections=anchor,
    )

    assert len(anchor) == 24
    assert len(corrections) == len(frames)
    assert tuple(item.timestamp_seconds for item in corrections) == tuple(
        timestamp for timestamp, _frame in frames
    )
    corrected = np.asarray(
        [
            (x + correction.translation_x, y + correction.translation_y)
            for (x, y), correction in zip(offsets, corrections, strict=True)
        ],
        dtype=np.float64,
    )
    center = np.median(corrected, axis=0)
    residuals = np.linalg.norm(corrected - center, axis=1)
    assert float(np.percentile(residuals, 90)) < 0.25


def test_transition_anchor_rejects_a_hard_cut() -> None:
    """Catches bridging unrelated appearances as one camera path."""
    import cv2

    frames, _offsets, config = _crossfade_transition_case()
    rng = np.random.default_rng(20260816)
    unrelated = rng.integers(0, 256, size=(96, 160), dtype=np.uint8)
    changed = list(frames)
    for index in range(12):
        timestamp = frames[index][0]
        x = 3.0 * math.sin(2.0 * math.pi * 3.0 * timestamp)
        y = 2.0 * math.sin(2.0 * math.pi * 2.0 * timestamp)
        changed[index] = (
            timestamp,
            cv2.warpAffine(
                unrelated,
                np.asarray(((1.0, 0.0, x), (0.0, 1.0, y)), dtype=np.float32),
                (160, 96),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT,
            ),
        )
    anchor = estimate_anchor_corrections(frames[24:], config)

    assert (
        estimate_transition_anchor_corrections(
            tuple(changed),
            config,
            transition_range=(0.0, 1.0),
            following_anchor_corrections=anchor,
        )
        == ()
    )


def test_transition_anchor_rejects_independent_layer_motion() -> None:
    """Catches flattening opposing foreground/background motion."""
    import cv2

    fps = 24.0
    rng = np.random.default_rng(20260817)
    left_layer = rng.integers(0, 256, size=(96, 160), dtype=np.uint8)
    right_layer = rng.integers(0, 256, size=(96, 160), dtype=np.uint8)
    frames: list[tuple[float, NDArray[np.uint8]]] = []
    for index in range(48):
        timestamp = index / fps
        if index < 24:
            distance = 4.0 * math.sin(2.0 * math.pi * 2.0 * timestamp)
            left = cv2.warpAffine(
                left_layer,
                np.asarray(((1.0, 0.0, distance), (0.0, 1.0, 0.0))),
                (160, 96),
                borderMode=cv2.BORDER_REFLECT,
            )
            right = cv2.warpAffine(
                right_layer,
                np.asarray(((1.0, 0.0, -distance), (0.0, 1.0, 0.0))),
                (160, 96),
                borderMode=cv2.BORDER_REFLECT,
            )
            frame = np.concatenate((left[:, :80], right[:, 80:]), axis=1)
        else:
            frame = right_layer
        frames.append((timestamp, frame))
    following = tuple(_correction(index / fps, 0.0) for index in range(24, 48))

    assert (
        estimate_transition_anchor_corrections(
            tuple(frames),
            StabilizationConfig(
                frame_width=160,
                frame_height=96,
                maximum_frame_inventory=60,
                source_rate_cap_fps=24.0,
            ),
            transition_range=(0.0, 1.0),
            following_anchor_corrections=following,
        )
        == ()
    )


def test_transition_anchor_rejects_insufficient_regional_texture() -> None:
    """Catches treating one textured tile as global evidence."""
    frames, _offsets, config = _crossfade_transition_case()
    sparse = []
    for timestamp, frame in frames:
        limited = np.zeros_like(frame)
        limited[:32, :40] = frame[:32, :40]
        sparse.append((timestamp, limited))
    following = tuple(_correction(item[0], 0.0) for item in frames[24:])

    assert (
        estimate_transition_anchor_corrections(
            tuple(sparse),
            config,
            transition_range=(0.0, 1.0),
            following_anchor_corrections=following,
        )
        == ()
    )


def test_transition_anchor_requires_persistent_lk_survivors() -> None:
    """Catches substituting adjacent short-lived tracks for persistent tracks."""
    frames, _offsets, config = _crossfade_transition_case()
    anchor = estimate_anchor_corrections(frames[24:], config)

    assert (
        estimate_transition_anchor_corrections(
            frames,
            config.model_copy(update={"minimum_transition_lk_track_ratio": 0.95}),
            transition_range=(0.0, 1.0),
            following_anchor_corrections=anchor,
        )
        == ()
    )


def test_transition_anchor_rejects_dense_flow_disagreement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches accepting phase/LK evidence when dense flow opposes it."""
    frames, _offsets, config = _crossfade_transition_case()
    anchor = estimate_anchor_corrections(frames[24:], config)

    def opposing_dense(
        previous: NDArray[np.uint8],
        _current: NDArray[np.uint8],
        *_args: object,
        **_kwargs: object,
    ) -> NDArray[np.float32]:
        dense = np.zeros((*previous.shape, 2), dtype=np.float32)
        dense[..., 0] = -5.0
        return dense

    import cv2

    monkeypatch.setattr(cv2, "calcOpticalFlowFarneback", opposing_dense)
    assert (
        estimate_transition_anchor_corrections(
            frames,
            config,
            transition_range=(0.0, 1.0),
            following_anchor_corrections=anchor,
        )
        == ()
    )


def test_transition_anchor_rejects_missing_or_gapped_pts() -> None:
    """Catches ordinal inference and interpolation over absent source frames."""
    frames, _offsets, config = _crossfade_transition_case()
    anchor = estimate_anchor_corrections(frames[24:], config)
    missing = list(frames)
    missing[4] = (None, missing[4][1])  # type: ignore[assignment]
    with pytest.raises(ValueError, match="timestamps"):
        estimate_transition_anchor_corrections(
            tuple(missing),  # type: ignore[arg-type]
            config,
            transition_range=(0.0, 1.0),
            following_anchor_corrections=anchor,
        )

    gapped = frames[:10] + frames[11:]
    assert (
        estimate_transition_anchor_corrections(
            gapped,
            config,
            transition_range=(0.0, 1.0),
            following_anchor_corrections=anchor,
        )
        == ()
    )


def test_transition_anchor_bounds_inventory_crop_and_seam() -> None:
    """Catches unbounded work, crop overrun, and a corrupt anchor seam."""
    frames, _offsets, config = _crossfade_transition_case()
    anchor = estimate_anchor_corrections(frames[24:], config)
    with pytest.raises(ValueError, match="candidate frame inventory"):
        estimate_transition_anchor_corrections(
            frames,
            config.model_copy(update={"maximum_transition_candidate_frames": 24}),
            transition_range=(0.0, 1.0),
            following_anchor_corrections=anchor,
        )
    oversized = tuple(
        (timestamp, np.repeat(np.repeat(frame, 2, axis=0), 2, axis=1))
        for timestamp, frame in frames
    )
    with pytest.raises(ValueError, match="configured frame dimensions"):
        estimate_transition_anchor_corrections(
            oversized,
            config,
            transition_range=(0.0, 1.0),
            following_anchor_corrections=anchor,
        )
    assert (
        estimate_transition_anchor_corrections(
            frames,
            config.model_copy(update={"max_crop_ratio": 0.005}),
            transition_range=(0.0, 1.0),
            following_anchor_corrections=anchor,
        )
        == ()
    )
    corrupt = list(anchor)
    corrupt[0] = corrupt[0].model_copy(
        update={"translation_x": corrupt[0].translation_x + 0.5}
    )
    assert (
        estimate_transition_anchor_corrections(
            frames,
            config,
            transition_range=(0.0, 1.0),
            following_anchor_corrections=tuple(corrupt),
        )
        == ()
    )


def test_transition_anchor_rejects_intentional_pan_and_is_deterministic() -> None:
    """Catches flattening a pan and nondeterministic correction inventories."""
    import cv2

    frames, _offsets, config = _crossfade_transition_case()
    anchor = estimate_anchor_corrections(frames[24:], config)
    first = estimate_transition_anchor_corrections(
        frames,
        config,
        transition_range=(0.0, 1.0),
        following_anchor_corrections=anchor,
    )
    second = estimate_transition_anchor_corrections(
        frames,
        config,
        transition_range=(0.0, 1.0),
        following_anchor_corrections=anchor,
    )
    assert first == second

    rng = np.random.default_rng(20260818)
    background = rng.integers(0, 256, size=(96, 160), dtype=np.uint8)
    pan_frames = tuple(
        (
            index / 24.0,
            cv2.warpAffine(
                background,
                np.asarray(
                    ((1.0, 0.0, min(index, 24) * 1.5), (0.0, 1.0, 0.0)),
                    dtype=np.float32,
                ),
                (160, 96),
                borderMode=cv2.BORDER_REFLECT,
            ),
        )
        for index in range(48)
    )
    following = tuple(_correction(item[0], 0.0) for item in pan_frames[24:])
    assert (
        estimate_transition_anchor_corrections(
            pan_frames,
            config,
            transition_range=(0.0, 1.0),
            following_anchor_corrections=following,
        )
        == ()
    )


def test_transition_consensus_honors_cancellation_during_dense_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after decode interrupts the fresh dense evidence loop."""
    import cv2

    frames, _offsets, config = _crossfade_transition_case()
    original_dense = cv2.calcOpticalFlowFarneback
    entered_dense = False

    def observed_dense(*args: Any, **kwargs: Any) -> NDArray[np.float32]:
        nonlocal entered_dense
        result = original_dense(*args, **kwargs)
        entered_dense = True
        return cast(NDArray[np.float32], result)

    monkeypatch.setattr(cv2, "calcOpticalFlowFarneback", observed_dense)

    with pytest.raises(RescueCancelledError) as captured:
        measure_transition_source_consensus(
            frames,
            config,
            cancellation_callback=lambda: entered_dense,
        )

    assert entered_dense is True
    assert captured.value.internal_message == (
        "transition source consensus measurement was cancelled"
    )


def test_anchor_corrections_fail_closed_for_dominant_local_object_motion() -> None:
    """Catches treating a moving foreground as global camera shake."""
    rng = np.random.default_rng(915)
    background = rng.integers(0, 256, size=(120, 192), dtype=np.uint8)
    frames: list[tuple[float, np.ndarray]] = []
    for index in range(48):
        frame = background.copy()
        left = 8 + (index * 5) % 96
        frame[30:100, left : left + 72] = np.roll(
            background[30:100, 8:80], index * 3, axis=1
        )
        frames.append((index / 24.0, frame))

    corrections = estimate_anchor_corrections(
        frames,
        StabilizationConfig(
            frame_width=192,
            frame_height=120,
            maximum_frame_inventory=60,
            source_rate_cap_fps=30.0,
        ),
    )

    assert (
        corrections == ()
        or max(
            math.hypot(item.translation_x, item.translation_y) for item in corrections
        )
        < 1.0
    )


def test_anchor_corrections_reject_intentional_monotonic_pan() -> None:
    """Catches flattening a deliberate one-direction camera move."""
    import cv2

    rng = np.random.default_rng(916)
    background = rng.integers(0, 256, size=(120, 256), dtype=np.uint8)
    frames = tuple(
        (
            index / 24.0,
            cv2.warpAffine(
                background,
                np.array(((1.0, 0.0, index * 1.5), (0.0, 1.0, 0.0))),
                (background.shape[1], background.shape[0]),
                borderMode=cv2.BORDER_REFLECT,
            ),
        )
        for index in range(48)
    )

    assert (
        estimate_anchor_corrections(
            frames,
            StabilizationConfig(
                frame_width=256,
                frame_height=120,
                maximum_frame_inventory=60,
                source_rate_cap_fps=30.0,
                maximum_intentional_trend_pixels_per_frame=0.75,
            ),
        )
        == ()
    )


def test_anchor_corrections_fail_closed_for_cut_and_low_texture() -> None:
    """Catches fabricating one motion path across a cut or featureless frames."""
    rng = np.random.default_rng(917)
    left = rng.integers(0, 256, size=(96, 160), dtype=np.uint8)
    right = rng.integers(0, 256, size=(96, 160), dtype=np.uint8)
    cut_frames = tuple(
        (index / 24.0, left if index < 12 else right) for index in range(24)
    )
    config = StabilizationConfig(
        frame_width=160,
        frame_height=96,
        maximum_frame_inventory=30,
        source_rate_cap_fps=30.0,
    )

    cut_corrections = estimate_anchor_corrections(
        cut_frames, config, scene_boundaries=(0.5,)
    )
    low_texture: tuple[tuple[float, NDArray[np.uint8]], ...] = tuple(
        (index / 24.0, np.full((96, 160), 127, dtype=np.uint8)) for index in range(24)
    )

    assert len(cut_corrections) == len(cut_frames)
    boundary = cut_corrections[12]
    assert boundary.timestamp_seconds == pytest.approx(0.5)
    assert boundary.scene_boundary is True
    assert boundary.translation_x == boundary.translation_y == 0.0
    assert estimate_anchor_corrections(low_texture, config) == ()


def test_anchor_inventory_and_config_boundaries_fail_before_estimation() -> None:
    """Catches unbounded inventory/rate allocation and impossible residual gates."""
    frames: tuple[tuple[float, NDArray[np.uint8]], ...] = tuple(
        (index / 24.0, np.full((32, 32), index, dtype=np.uint8)) for index in range(5)
    )
    with pytest.raises(ValueError, match="inventory"):
        estimate_anchor_corrections(
            frames,
            StabilizationConfig(maximum_frame_inventory=4),
            estimator=lambda _left, _right: (0.0, 1.0, 0.0, 0.0, 1.0, 0.0),
        )
    over_rate: tuple[tuple[float, NDArray[np.uint8]], ...] = tuple(
        (index / 30.01, np.full((32, 32), index, dtype=np.uint8)) for index in range(5)
    )
    with pytest.raises(ValueError, match="source-rate"):
        estimate_anchor_corrections(over_rate, StabilizationConfig())
    with pytest.raises(ValidationError):
        StabilizationConfig(
            maximum_anchor_residual_pixels=0.5,
            residual_goal_p90_pixels=1.0,
        )


def test_anchor_assessment_bridges_one_isolated_low_confidence_correction() -> None:
    corrections = (
        _correction(0.0, -3.0),
        _correction(0.1, 0.0).model_copy(
            update={"inlier_ratio": 0.0, "residual_pixels": 4096.0}
        ),
        _correction(0.2, 3.0),
    )
    config = StabilizationConfig(
        frame_width=100,
        frame_height=100,
        maximum_bridged_low_confidence_samples=1,
        max_crop_ratio=0.2,
    )

    result = assess_anchor_corrections(
        corrections, config, affected_ranges=((0.0, 0.3),)
    )

    assert result.recommended is True
    assert result.transforms[1].translation_x == pytest.approx(0.0)
    assert result.transforms[1].inlier_ratio == pytest.approx(
        config.minimum_anchor_inlier_ratio
    )
    assert result.parameters["bridged_low_confidence_samples"] == 1


@pytest.mark.parametrize(
    "case",
    ("two_weak", "scene_cut", "timestamp_gap"),
)
def test_anchor_assessment_rejects_multiple_cut_or_gapped_weak_corrections(
    case: str,
) -> None:
    def correction(
        timestamp: float,
        tx: float,
        *,
        inlier_ratio: float = 0.9,
        scene_boundary: bool = False,
    ) -> MotionTransform:
        return MotionTransform(
            timestamp_seconds=timestamp,
            rotation_degrees=0.0,
            scale=1.0,
            translation_x=tx,
            translation_y=0.0,
            inlier_ratio=inlier_ratio,
            residual_pixels=0.2,
            scene_boundary=scene_boundary,
            semantics="frame_correction",
        )

    corrections = {
        "two_weak": (
            correction(0.0, -3.0),
            correction(0.1, -1.0, inlier_ratio=0.1),
            correction(0.2, 1.0, inlier_ratio=0.1),
            correction(0.3, 3.0),
        ),
        "scene_cut": (
            correction(0.0, -3.0),
            correction(0.1, 0.0, inlier_ratio=0.1, scene_boundary=True),
            correction(0.2, 3.0),
        ),
        "timestamp_gap": (
            correction(0.0, -3.0),
            correction(0.1, 0.0, inlier_ratio=0.1),
            correction(0.5, 3.0),
        ),
    }[case]
    result = assess_anchor_corrections(
        corrections,
        StabilizationConfig(
            frame_width=100,
            frame_height=100,
            maximum_bridged_low_confidence_samples=1,
            maximum_timeline_gap_seconds=0.2,
            max_crop_ratio=0.2,
        ),
        affected_ranges=((0.0, 0.6),),
    )

    if case == "scene_cut":
        assert result.recommended is True
        assert result.transforms[1].scene_boundary is True
        assert result.transforms[1].inlier_ratio == pytest.approx(0.1)
        assert result.parameters["bridged_low_confidence_samples"] == 0
    else:
        assert result.recommended is False
        assert result.reason == "unreliable_anchor_correction"


def test_deflicker_curve_is_remapped_after_middle_deletion() -> None:
    """Catches preview/execution disagreement at a compacted deletion seam."""
    correction = FlickerCorrectionPlan(
        intervals=((1.0, 2.0), (3.0, 4.0)),
        gains=((1.0, 1.08), (2.0, 1.08), (3.0, 0.93), (4.0, 0.93)),
        excluded_fade_ranges=((1.5, 3.5),),
    )

    mapped = remap_flicker_correction(
        correction,
        ((1.0, 4.0),),
        (
            SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),
            SourceMapping(3.0, 6.0, 2.0, 5.0, "faithful-rescue.mp4"),
        ),
    )

    assert mapped is not None
    assert mapped.intervals == ((1.0, 2.0), (2.0, 3.0))
    assert [time for time, _gain in mapped.gains] == [1.0, 2.0, 3.0]
    assert mapped.excluded_fade_ranges == ((1.5, 2.0), (2.0, 2.5))
    assert mapped.interval_gains[0][-1][1] == pytest.approx(1.08)
    assert mapped.interval_gains[1][0][1] == pytest.approx(0.93)
    assert flicker_gains_for_timestamps(mapped, (2.0, 3.0)) == pytest.approx(
        (0.93, 1.0)
    )
    filter_text = flicker_filter_fragment(mapped)
    assert filter_text is not None
    assert "val*0.93" in filter_text
    assert "gte(t,2)*lt(t,3)" in filter_text


def test_deflicker_adjacent_seam_is_half_open_and_later_interval_wins() -> None:
    """Catches an end-boundary frame receiving the preceding interval's gain."""
    correction = FlickerCorrectionPlan(
        intervals=((0.0, 1.0), (1.0, 2.0)),
        gains=((0.0, 1.08), (1.0, 1.08), (2.0, 0.92)),
        interval_gains=(
            ((0.0, 1.08), (1.0, 1.08)),
            ((1.0, 0.92), (2.0, 0.92)),
        ),
    )

    assert flicker_gains_for_timestamps(correction, (0.999, 1.0, 2.0)) == (
        pytest.approx(1.08),
        pytest.approx(0.92),
        pytest.approx(1.0),
    )
    filter_text = flicker_filter_fragment(correction)
    assert filter_text is not None
    assert "gte(t,0)*lt(t,1)" in filter_text
    assert "gte(t,1)*lt(t,2)" in filter_text
    assert "between(t" not in filter_text


def test_deflicker_curve_inserts_interpolated_clipped_boundary_gains() -> None:
    """Catches clipping a curve without a hand-derived gain at the new boundary."""
    correction = FlickerCorrectionPlan(
        intervals=((1.0, 4.0),),
        gains=((1.0, 1.0), (3.0, 1.2), (4.0, 1.0)),
    )

    mapped = remap_flicker_correction(
        correction,
        ((1.5, 2.5),),
        (SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
    )

    assert mapped is not None
    assert mapped.intervals == ((1.5, 2.5),)
    assert [timestamp for timestamp, _gain in mapped.gains] == [1.5, 2.5]
    assert [gain for _timestamp, gain in mapped.gains] == pytest.approx((1.05, 1.15))


def test_scene_cut_is_not_smoothed_as_flicker() -> None:
    """Catches treating a one-time scene-level luma change as flicker."""
    result = plan_flicker_correction(
        ((0.0, 0.2), (0.5, 0.2), (1.0, 0.2), (1.5, 0.2), (2.0, 0.8), (2.5, 0.8)),
        _scenes_with_cut(),
        _flicker_config(),
    )

    assert result.intervals == ()
    assert result.gains == ()


def test_repeated_high_frequency_flicker_has_bounded_deterministic_curve() -> None:
    """Catches a missing curve, unstable ordering, or an unbounded correction."""
    measurements = tuple(
        (index * 0.25, 0.50 + (0.08 if index % 2 else -0.08)) for index in range(12)
    )
    scenes = (
        VideoScene(
            scene_index=0,
            start_seconds=0.0,
            end_seconds=3.0,
            duration_seconds=3.0,
            representative_timestamp=1.5,
        ),
    )
    first = plan_flicker_correction(measurements, scenes, _flicker_config())
    second = plan_flicker_correction(measurements, scenes, _flicker_config())

    assert first == second
    # The centred trend deliberately leaves incomplete end windows neutral.
    assert first.intervals == ((0.25, 2.75),)
    assert len(first.gains) == len(measurements)
    assert all(1 / 1.08 <= gain <= 1.08 for _time, gain in first.gains)
    assert first.gains[0][1] == 1.0
    assert first.gains[-1][1] == 1.0


def test_slow_fade_is_not_corrected_as_flicker() -> None:
    """Catches flattening a low-frequency fade instead of preserving its trend."""
    measurements = tuple((index * 0.25, 0.2 + index * 0.025) for index in range(12))
    scenes = (
        VideoScene(
            scene_index=0,
            start_seconds=0.0,
            end_seconds=3.0,
            duration_seconds=3.0,
            representative_timestamp=1.5,
        ),
    )
    assert (
        plan_flicker_correction(measurements, scenes, _flicker_config()).intervals == ()
    )


def test_fade_with_high_frequency_residuals_stays_neutral() -> None:
    """Catches repeated residuals converting an intentional fade into an action."""
    measurements = tuple(
        (
            index * 0.25,
            0.18 + index * 0.045 + (0.045 if index % 2 else -0.045),
        )
        for index in range(12)
    )
    scenes = (
        VideoScene(
            scene_index=0,
            start_seconds=0.0,
            end_seconds=3.0,
            duration_seconds=3.0,
            representative_timestamp=1.5,
        ),
    )

    result = plan_flicker_correction(measurements, scenes, _flicker_config())

    assert result.intervals == ()
    assert result.excluded_fade_ranges == ((0.0, 2.75),)
    assert all(gain == 1.0 for _timestamp, gain in result.gains)


def test_deflicker_action_has_a_curve_bound_executable_filter() -> None:
    """Catches a reviewed correction curve being metadata-only."""
    fragment = filter_fragment_from_action(
        RescueActionKind.DEFLICKER,
        {
            "affected_ranges": [[0.2, 0.4]],
            "gain_curve": [[0.0, 1.0], [0.25, 1.08], [0.5, 1.0]],
        },
    )

    assert fragment is not None
    assert "lutyuv" in fragment
    assert "gte(t,0.2)*lt(t,0.25)" in fragment
    assert "gte(t,0.25)*lt(t,0.4)" in fragment


def test_streaming_deflicker_changes_only_accepted_luma_range(tmp_path: Path) -> None:
    """Catches a renderer that ignores the curve or changes neutral frames."""
    import cv2

    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 4.0, (32, 32))
    assert writer.isOpened()
    try:
        for _index in range(4):
            writer.write(np.full((32, 32, 3), 80, dtype=np.uint8))
    finally:
        writer.release()
    output = tmp_path / "deflickered.mp4"
    correction = FlickerCorrectionPlan(
        intervals=((0.2, 0.3),),
        gains=((0.0, 1.0), (0.25, 1.2), (0.5, 1.0), (0.75, 1.0)),
    )
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        first_input = arguments.index("-i") + 1
        shutil.copyfile(arguments[first_input], arguments[-1])
        return CommandResult(returncode=0, stderr_summary="")

    render_deflickered_video(
        source,
        output,
        correction,
        runner=runner,
        cancellation_callback=lambda: False,
        frame_timestamps=(0.0, 0.25, 0.5, 0.75),
    )
    capture = cv2.VideoCapture(str(output))
    means: list[float] = []
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            means.append(float(np.mean(frame)))
    finally:
        capture.release()

    assert len(means) == 4
    assert means[1] > means[0] + 8.0
    assert means[0] == pytest.approx(means[2], abs=3.0)
    assert means[0] == pytest.approx(means[3], abs=3.0)
    assert calls[0][calls[0].index("-c:v:0") + 1] == "libx264"
    assert calls[0][calls[0].index("-profile:v:0") + 1] == "high"
    assert calls[0][calls[0].index("-level:v:0") + 1] == "3.1"
    assert calls[0][calls[0].index("-pix_fmt:v:0") + 1] == "yuv420p"
    assert calls[0][calls[0].index("-fps_mode:v:0") + 1] == "cfr"
    assert calls[0][calls[0].index("-video_track_timescale") + 1] == "120000"
    assert "-r" not in calls[0]


def test_irregular_source_timestamps_select_the_bound_flicker_range() -> None:
    """Catches selecting gains from frame ordinal instead of actual source PTS."""
    correction = FlickerCorrectionPlan(
        intervals=((0.35, 0.45),),
        gains=((0.0, 1.0), (0.4, 1.2), (0.8, 1.0)),
    )

    gains = flicker_gains_for_timestamps(correction, (0.0, 0.1, 0.4, 0.9))

    assert gains == pytest.approx((1.0, 1.0, 1.2, 1.0))


@pytest.mark.parametrize(
    "timestamps",
    [
        (0.0, 0.1, 0.4, 0.5),
        (0.0, 0.25, 0.5),
        (0.0, 0.25, 0.5, 0.75, 1.0),
    ],
)
def test_deflicker_rejects_vfr_or_timestamp_cardinality_mismatch(
    tmp_path: Path, timestamps: tuple[float, ...]
) -> None:
    """Catches silently publishing CFR output against irregular or missing PTS."""
    import cv2

    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 4.0, (16, 16))
    assert writer.isOpened()
    try:
        for _index in range(4):
            writer.write(np.full((16, 16, 3), 80, dtype=np.uint8))
    finally:
        writer.release()
    called = False

    def runner(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("timing-invalid media must not be muxed")

    with pytest.raises(RescueMediaError):
        render_deflickered_video(
            source,
            tmp_path / "output.mp4",
            FlickerCorrectionPlan(
                intervals=((0.2, 0.3),),
                gains=((0.0, 1.0), (0.25, 1.1), (0.5, 1.0)),
            ),
            runner=runner,  # type: ignore[arg-type]
            cancellation_callback=lambda: False,
            frame_timestamps=timestamps,
        )

    assert called is False
    assert (tmp_path / "output.mp4").exists() is False


def _transform(
    timestamp: float,
    tx: float,
    *,
    inlier_ratio: float = 0.9,
    residual: float = 0.2,
    scene_boundary: bool = False,
) -> MotionTransform:
    return MotionTransform(
        timestamp_seconds=timestamp,
        rotation_degrees=0.0,
        scale=1.0,
        translation_x=tx,
        translation_y=0.0,
        inlier_ratio=inlier_ratio,
        residual_pixels=residual,
        scene_boundary=scene_boundary,
    )


def _correction(timestamp: float, tx: float) -> MotionTransform:
    return _transform(timestamp, tx).model_copy(
        update={"semantics": "frame_correction"}
    )


class _MeasuredShakeAssessment:
    def assess(
        self,
        _source: Path,
        source_hash: str,
        metadata: VideoMetadata,
        _base_damage_map: object,
        _workspace: Path,
        _cancellation_callback: object,
    ) -> RescueAssessmentBundle:
        interval = DamageInterval(
            id=make_damage_id(
                source_hash,
                "video:0",
                DamageKind.SHAKE,
                0.0,
                metadata.duration_seconds,
            ),
            stream_id="video:0",
            kind=DamageKind.SHAKE,
            start_seconds=0.0,
            end_seconds=metadata.duration_seconds,
        )
        fps = min(metadata.average_frame_rate, 30.0)
        frame_count = min(
            int(math.ceil(metadata.duration_seconds * fps)),
            900,
        )
        transforms = tuple(
            _correction(index / fps, 1.0 if index % 2 else -1.0)
            for index in range(frame_count)
        )
        return RescueAssessmentBundle(
            stabilization_assessment=StabilizationAssessment(
                recommended=True,
                reason="Measured deterministic shake correction.",
                crop_ratio=0.02,
                transforms=transforms,
                parameters={
                    "crop_ratio": 0.02,
                    "frame_width": metadata.width,
                    "frame_height": metadata.height,
                    "maximum_timeline_gap_seconds": 1.0,
                    "smoothing_window_samples": 5,
                },
            ),
            evidence_intervals=(interval,),
            parameters={"fixture_evidence": "measured_shake_curve_v1"},
        )


def test_real_shake_uses_native_preview_evidence_without_mutating_source(
    tmp_path: Path,
) -> None:
    """Runs the real FFmpeg/OpenCV preview path with full source-rate coverage."""
    source = _real_rescue_fixture("rescue_shake.mp4")
    source_hash = sha256(source.read_bytes()).hexdigest()
    pipeline = VideoRescuePipeline(
        RescueConfig(tmp_path / "shake 中文", strategy=RescueStrategy.BALANCED),
        dependencies=RescuePipelineDependencies(
            assessment_service=_MeasuredShakeAssessment()
        ),
    )

    preparation = pipeline.prepare(source)
    try:
        stabilize = next(
            action
            for action in preparation.plan.actions
            if action.kind is RescueActionKind.STABILIZE
        )
        assert "preview_renderer_unavailable" not in " ".join(
            preparation.plan.assessment_warnings
        )
        assert preparation.previews is not None
        assert preparation.previews.improved is None
        assert preparation.previews.source.paths
        assert preparation.previews.faithful.paths
        assert stabilize.id in preparation.previews.previewed_action_ids
        assert all(path.is_file() for path in preparation.previews.all_paths())
        assert sha256(source.read_bytes()).hexdigest() == source_hash
    finally:
        pipeline.abort(preparation)


def test_stabilization_is_skipped_when_required_crop_exceeds_budget() -> None:
    """Catches accepting stabilizing transforms that exceed the crop cap."""
    result = assess_stabilization(
        tuple(
            _transform(index * 0.1, 20.0 if index % 2 else -20.0) for index in range(8)
        ),
        StabilizationConfig(max_crop_ratio=0.08, frame_width=100, frame_height=100),
    )

    assert result.recommended is False
    assert result.reason == "crop_budget_exceeded"


def test_over_budget_transition_run_does_not_hide_later_bounded_shake() -> None:
    """Catches combining independent run extrema into one global crop veto."""
    transforms = tuple(
        [
            _transform(1.0, 100.0),
            _transform(1.5, -100.0),
            _transform(2.0, 0.0, inlier_ratio=0.1, residual=8.0),
        ]
        + [
            _transform(timestamp, translation)
            for timestamp, translation in (
                (3.0, -4.0),
                (3.5, 4.0),
                (4.0, -4.0),
                (4.5, 4.0),
                (5.0, -4.0),
                (5.5, 4.0),
                (6.0, -4.0),
            )
        ]
        + [_transform(6.5, 0.0, inlier_ratio=0.1, residual=8.0)]
    )

    result = assess_stabilization(
        transforms,
        StabilizationConfig(frame_width=640, frame_height=360),
    )

    assert result.recommended is True
    assert result.crop_ratio <= 0.12
    assert result.parameters["affected_ranges"] == [[3.0, 6.0]]
    run_assessments = result.parameters["run_assessments"]
    assert isinstance(run_assessments, list)
    typed_run_assessments = cast(list[dict[str, object]], run_assessments)
    assert any(
        item["accepted"] is False and item["reason"] == "crop_budget_exceeded"
        for item in typed_run_assessments
    )
    assert any(
        item["accepted"] is True and item["reason"] == "accepted"
        for item in typed_run_assessments
    )
    assert all(
        abs(item.translation_x) < 1e-9
        for item in result.transforms
        if item.timestamp_seconds < 3.0 or item.timestamp_seconds >= 6.0
    )


def test_scene_boundaries_cap_motion_range_without_crossing_guard_samples() -> None:
    """Catches padding a reliable run into observed scene-boundary transforms."""
    transforms = tuple(
        [_transform(2.0, 0.0, inlier_ratio=0.0, residual=4096.0, scene_boundary=True)]
        + [
            _transform(timestamp, translation)
            for timestamp, translation in (
                (3.0, -4.0),
                (3.5, 4.0),
                (4.0, -4.0),
                (4.5, 4.0),
                (5.0, -4.0),
                (5.5, 4.0),
                (6.0, -4.0),
            )
        ]
        + [
            _transform(
                6.5,
                0.0,
                inlier_ratio=0.0,
                residual=4096.0,
                scene_boundary=True,
            )
        ]
    )

    result = assess_stabilization(
        transforms,
        StabilizationConfig(frame_width=640, frame_height=360),
    )

    assert result.recommended is True
    assert result.parameters["affected_ranges"] == [[3.0, 6.0]]
    assert all(
        abs(item.translation_x) < 1e-9
        for item in result.transforms
        if item.timestamp_seconds < 3.0 or item.timestamp_seconds >= 6.0
    )


def test_unbounded_motion_run_retains_configured_guard_padding() -> None:
    """Catches removing configured padding when no unsafe boundary was observed."""
    transforms = tuple(
        _transform(timestamp, translation)
        for timestamp, translation in (
            (3.0, -4.0),
            (3.5, 4.0),
            (4.0, -4.0),
            (4.5, 4.0),
            (5.0, -4.0),
            (5.5, 4.0),
            (6.0, -4.0),
        )
    )

    result = assess_stabilization(
        transforms,
        StabilizationConfig(frame_width=640, frame_height=360),
    )

    assert result.recommended is True
    assert result.parameters["affected_ranges"] == [[2.0, 7.0]]


def test_single_measurement_run_is_not_recommended_without_active_range() -> None:
    """Catches recommending a range that cannot contain a motion correction."""
    result = assess_stabilization(
        (_transform(1.0, 4.0),),
        StabilizationConfig(frame_width=640, frame_height=360),
    )

    assert result.recommended is False
    assert result.reason == "insufficient_active_corrections"
    assert result.parameters == {}


def test_subthreshold_corrections_are_not_recommended_as_empty_range() -> None:
    """Catches accepting a run whose computed corrections are all inactive."""
    result = assess_stabilization(
        (_transform(1.0, 1.0), _transform(1.5, 1.0)),
        StabilizationConfig(frame_width=640, frame_height=360),
    )

    assert result.recommended is False
    assert result.reason == "insufficient_active_corrections"
    assert result.parameters == {}


@pytest.mark.parametrize(
    ("transforms", "reason"),
    [
        (
            tuple(_transform(index * 0.1, 1.0, inlier_ratio=0.2) for index in range(4)),
            "low_inlier_ratio",
        ),
        (
            tuple(_transform(index * 0.1, 1.0, residual=8.0) for index in range(4)),
            "high_residual",
        ),
        (
            tuple(
                _transform(index * 0.1, 1.0, scene_boundary=True) for index in range(4)
            ),
            "scene_boundary",
        ),
    ],
)
def test_unreliable_motion_is_rejected_with_a_specific_neutral_reason(
    transforms: tuple[MotionTransform, ...], reason: str
) -> None:
    """Catches proposing stabilization when the measured motion is unreliable."""
    result = assess_stabilization(
        transforms, StabilizationConfig(frame_width=100, frame_height=100)
    )

    assert result.recommended is False
    assert result.reason == reason
    assert result.parameters == {}


def test_scene_boundary_transform_is_excluded_without_rejecting_reliable_scenes() -> (
    None
):
    """Catches one cut disabling otherwise reliable within-scene stabilization."""
    transforms = (
        _transform(0.1, 4.0),
        _transform(0.15, -4.0),
        _transform(0.2, 0.0, scene_boundary=True),
        _transform(0.3, -4.0),
        _transform(0.35, 4.0),
    )

    result = assess_stabilization(
        transforms, StabilizationConfig(frame_width=100, frame_height=100)
    )

    assert result.recommended is True
    assert result.transforms[2].scene_boundary is True
    assert result.transforms[2].semantics == "frame_correction"
    assert result.transforms[2].translation_x == 0.0


def test_unreliable_transition_samples_do_not_hide_later_shake_run() -> None:
    """Catches one crossfade sample disabling a measurable oscillating segment."""
    transforms = tuple(
        [
            _transform(30.0, 0.1),
            _transform(30.5, 0.0),
            _transform(31.0, -0.1),
            _transform(32.0, 0.0, inlier_ratio=0.1, residual=8.0),
        ]
        + [
            MotionTransform(
                timestamp_seconds=timestamp,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=0.0,
                translation_y=value,
                inlier_ratio=0.95,
                residual_pixels=0.2,
            )
            for timestamp, value in (
                (33.0, -4.0),
                (33.5, 4.0),
                (34.0, -4.0),
                (34.5, 4.0),
                (35.0, -4.0),
                (35.5, 4.0),
                (36.0, -4.0),
            )
        ]
        + [_transform(36.5, 0.0, inlier_ratio=0.1, residual=8.0)]
    )

    result = assess_stabilization(
        transforms,
        StabilizationConfig(frame_width=640, frame_height=360),
    )

    assert result.recommended is True
    assert result.parameters["affected_ranges"] == [[33.0, 36.0]]
    assert any(abs(item.translation_y) > 0.5 for item in result.transforms)
    assert all(
        abs(item.translation_y) < 1e-9
        for item in result.transforms
        if item.timestamp_seconds < 33.0 or item.timestamp_seconds >= 36.0
    )


def test_smoothing_is_deterministic_and_preserves_scene_boundaries() -> None:
    """Catches smoothing through a cut or allowing a caller to mutate results."""
    transforms = (
        _transform(0.0, 0.0),
        _transform(0.1, 4.0),
        _transform(0.2, -4.0),
        _transform(0.3, 10.0, scene_boundary=True),
        _transform(0.4, 14.0),
    )

    first = smooth_motion_transforms(transforms, window_size=3)
    second = smooth_motion_transforms(transforms, window_size=3)

    assert first == second
    assert first[3].scene_boundary is True
    assert first[3].semantics == "frame_correction"
    assert first[3].translation_x == pytest.approx(0.0)
    assert first[1].translation_x == pytest.approx(-4.0)


def test_smoothing_uses_cumulative_camera_path_not_adjacent_motion_median() -> None:
    """Catches treating adjacent deltas as an already cumulative camera path."""
    transforms = tuple(
        _transform((index + 1) * 0.25, value)
        for index, value in enumerate((2.0, -2.0, 2.0, -2.0, 2.0))
    )

    corrections = smooth_motion_transforms(transforms, window_size=3)

    assert corrections[1].translation_x == pytest.approx(2.0)
    assert corrections[2].translation_x == pytest.approx(-2.0)


def test_timestamp_lookup_interpolates_sampled_path_and_rejects_gaps() -> None:
    """Catches assigning transform N to decoded frame N regardless of timestamps."""
    corrections = (
        _correction(0.25, -2.0),
        _correction(0.5, 2.0),
        _correction(0.75, -2.0),
    )

    middle = motion_correction_at_timestamp(corrections, 0.375, maximum_gap_seconds=0.3)

    assert middle.translation_x == pytest.approx(0.0)
    with pytest.raises(RescueMediaError):
        motion_correction_at_timestamp(
            (_correction(0.25, 1.0), _correction(1.0, -1.0)),
            0.5,
            maximum_gap_seconds=0.3,
        )


def test_irregular_source_timestamps_select_timestamped_motion_corrections() -> None:
    """Catches correction selection from decoded-frame ordinal on VFR input."""
    corrections = (
        _correction(0.0, 0.0),
        _correction(0.4, -4.0),
        _correction(0.8, 0.0),
    )

    selected = motion_corrections_for_timestamps(
        corrections,
        (0.0, 0.1, 0.4, 0.9),
        maximum_gap_seconds=0.5,
    )

    assert tuple(item.translation_x for item in selected) == pytest.approx(
        (0.0, -1.0, -4.0, 0.0)
    )


def test_renderer_uses_exact_source_rate_corrections_for_decoded_pixels(
    tmp_path: Path,
) -> None:
    """Catches decoded frames consuming sampled corrections by ordinal."""
    import cv2

    source = tmp_path / "sampled-motion.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 8.0, (48, 32))
    assert writer.isOpened()
    source_positions = (8, 10, 12, 10, 8, 10, 12, 10)
    try:
        for position in source_positions:
            frame: Any = np.zeros((32, 48, 3), dtype=np.uint8)
            frame[12:20, position : position + 6] = 255
            writer.write(frame)
    finally:
        writer.release()
    output = tmp_path / "stabilized.mp4"
    corrections = tuple(
        _correction(index / 8, -float(position - 8))
        for index, position in enumerate(source_positions)
    )
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        first_input = arguments.index("-i") + 1
        shutil.copyfile(arguments[first_input], arguments[-1])
        return CommandResult(returncode=0, stderr_summary="")

    render_stabilized_video(
        source,
        output,
        corrections,
        StabilizationConfig(
            frame_width=48,
            frame_height=32,
            maximum_timeline_gap_seconds=0.3,
        ),
        runner=runner,
        cancellation_callback=lambda: False,
        frame_timestamps=tuple(index / 8 for index in range(8)),
    )

    def centroids(path: Path) -> list[float]:
        capture = cv2.VideoCapture(str(path))
        result: list[float] = []
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                weights = np.mean(frame, axis=2)
                columns = np.sum(weights, axis=0)
                result.append(
                    float(np.sum(columns * np.arange(columns.size)) / np.sum(columns))
                )
        finally:
            capture.release()
        return result

    before = centroids(source)
    after = centroids(output)
    assert len(before) == len(after) == 8
    assert float(np.std(after)) < float(np.std(before)) * 0.35
    assert calls[0][calls[0].index("-fps_mode:v:0") + 1] == "cfr"


def test_renderer_scales_sampled_pixel_motion_to_native_resolution(
    tmp_path: Path,
) -> None:
    """Catches applying half-resolution analysis pixels to full-size output."""
    import cv2

    source = tmp_path / "native-resolution-motion.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 8.0, (96, 64))
    assert writer.isOpened()
    source_positions = (16, 24, 16, 24, 16, 24, 16, 24)
    try:
        for position in source_positions:
            frame: Any = np.zeros((64, 96, 3), dtype=np.uint8)
            frame[24:40, position : position + 12] = 255
            writer.write(frame)
    finally:
        writer.release()
    output = tmp_path / "stabilized.mp4"

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        shutil.copyfile(arguments[arguments.index("-i") + 1], arguments[-1])
        return CommandResult(returncode=0, stderr_summary="")

    render_stabilized_video(
        source,
        output,
        (
            _correction(0.0, 0.0),
            _correction(0.125, -4.0),
            _correction(0.25, 0.0),
            _correction(0.375, -4.0),
            _correction(0.5, 0.0),
            _correction(0.625, -4.0),
            _correction(0.75, 0.0),
            _correction(0.875, -4.0),
        ),
        # Motion was measured on a 48x32 analysis frame.  The native frame is
        # exactly twice as large, so a -4 analysis-pixel correction must move
        # the native frame by -8 pixels.
        StabilizationConfig(
            frame_width=48,
            frame_height=32,
            maximum_timeline_gap_seconds=0.2,
        ),
        runner=runner,
        cancellation_callback=lambda: False,
        frame_timestamps=tuple(index / 8 for index in range(8)),
    )

    def centroids(path: Path) -> list[float]:
        capture = cv2.VideoCapture(str(path))
        result: list[float] = []
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                weights = np.mean(frame, axis=2)
                columns = np.sum(weights, axis=0)
                result.append(
                    float(np.sum(columns * np.arange(columns.size)) / np.sum(columns))
                )
        finally:
            capture.release()
        return result

    before = centroids(source)
    after = centroids(output)
    assert len(before) == len(after) == 8
    assert float(np.std(after)) < float(np.std(before)) * 0.2


def test_renderer_leaves_neutral_frames_outside_exact_correction_range_unchanged(
    tmp_path: Path,
) -> None:
    """Catches padding an accepted half-open correction into neighboring frames."""
    import cv2

    source = tmp_path / "exact range.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 4.0, (40, 32))
    assert writer.isOpened()
    try:
        for index in range(4):
            frame: NDArray[np.uint8] = np.zeros((32, 40, 3), dtype=np.uint8)
            frame[12:20, 8 + index : 14 + index] = 255
            writer.write(frame)
    finally:
        writer.release()
    output = tmp_path / "stabilized.mp4"

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        shutil.copyfile(arguments[arguments.index("-i") + 1], arguments[-1])
        return CommandResult(returncode=0, stderr_summary="")

    corrections = (
        _correction(0.0, 0.0),
        _correction(0.25, 0.0),
        _correction(0.5, -2.0),
        _correction(0.75, 0.0),
    )
    render_stabilized_video(
        source,
        output,
        corrections,
        StabilizationConfig(
            frame_width=40,
            frame_height=32,
            minimum_motion_amplitude_pixels=1.0,
            range_padding_seconds=0.0,
            accepted_ranges=((0.5, 0.75),),
            maximum_timeline_gap_seconds=0.3,
        ),
        runner=runner,
        cancellation_callback=lambda: False,
        frame_timestamps=(0.0, 0.25, 0.5, 0.75),
    )

    def decoded(path: Path) -> tuple[np.ndarray, ...]:
        capture = cv2.VideoCapture(str(path))
        frames: list[np.ndarray] = []
        try:
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                frames.append(frame)
        finally:
            capture.release()
        return tuple(frames)

    before, after = decoded(source), decoded(output)
    assert len(before) == len(after) == 4
    assert np.array_equal(before[0], after[0])
    assert np.array_equal(before[1], after[1])
    assert not np.array_equal(before[2], after[2])
    assert np.array_equal(before[3], after[3])


def test_renderer_atomic_no_clobber_preserves_racing_output(tmp_path: Path) -> None:
    """Catches replace() overwriting a file created while native mux was running."""
    import cv2

    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 4.0, (16, 16))
    assert writer.isOpened()
    try:
        writer.write(np.full((16, 16, 3), 80, dtype=np.uint8))
    finally:
        writer.release()
    output = tmp_path / "racing.mp4"

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        shutil.copyfile(arguments[arguments.index("-i") + 1], arguments[-1])
        output.write_bytes(b"racer")
        return CommandResult(returncode=0, stderr_summary="")

    with pytest.raises(RescueArtifactError):
        render_stabilized_video(
            source,
            output,
            (_correction(0.0, 2.0),),
            StabilizationConfig(frame_width=16, frame_height=16),
            runner=runner,
            cancellation_callback=lambda: False,
            frame_timestamps=(0.0,),
        )

    assert output.read_bytes() == b"racer"


def test_isolated_low_inlier_sample_is_bridged_inside_reliable_motion_run() -> None:
    """Catches one low-texture frame splitting an otherwise continuous shake run."""
    transforms = (
        _transform(0.1, -4.0),
        _transform(0.2, 4.0, inlier_ratio=0.1, residual=0.05),
        _transform(0.3, -4.0),
        _transform(0.4, 4.0),
        _transform(0.5, -4.0),
        _transform(0.6, 4.0),
    )

    result = assess_stabilization(
        transforms,
        StabilizationConfig(
            frame_width=100,
            frame_height=100,
            maximum_timeline_gap_seconds=0.2,
            maximum_bridged_low_confidence_samples=1,
        ),
    )

    assert result.recommended is True
    bridged = next(item for item in result.transforms if item.timestamp_seconds == 0.2)
    assert abs(bridged.translation_x) >= 1.0
    assert result.parameters["bridged_low_confidence_samples"] == 1


def test_renderer_applies_confirmed_safe_crop_as_centered_zoom(tmp_path: Path) -> None:
    """Catches computing a crop budget without applying it to avoid black borders."""
    import cv2

    source = tmp_path / "bordered-motion.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 4.0, (40, 40))
    assert writer.isOpened()
    try:
        frame: np.ndarray = np.zeros((40, 40, 3), dtype=np.uint8)
        frame[16:24, 16:24] = 255
        writer.write(frame)
    finally:
        writer.release()
    output = tmp_path / "stabilized.mp4"

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        shutil.copyfile(arguments[arguments.index("-i") + 1], arguments[-1])
        return CommandResult(returncode=0, stderr_summary="")

    render_stabilized_video(
        source,
        output,
        (
            MotionTransform(
                timestamp_seconds=0.0,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=4.0,
                translation_y=0.0,
                inlier_ratio=1.0,
                residual_pixels=0.0,
                semantics="frame_correction",
            ),
        ),
        StabilizationConfig(frame_width=40, frame_height=40, max_crop_ratio=0.12),
        runner=runner,
        cancellation_callback=lambda: False,
        frame_timestamps=(0.0,),
    )

    capture = cv2.VideoCapture(str(output))
    try:
        ok, rendered = capture.read()
    finally:
        capture.release()
    assert ok
    assert rendered is not None
    mask = np.mean(rendered, axis=2) > 100
    columns = np.flatnonzero(np.any(mask, axis=0))
    rows = np.flatnonzero(np.any(mask, axis=1))
    assert columns[-1] - columns[0] + 1 >= 9
    assert rows[-1] - rows[0] + 1 >= 9


def test_stabilization_rejects_surplus_source_timestamps_before_mux(
    tmp_path: Path,
) -> None:
    """Catches publishing four decoded frames against five supplied source PTS."""
    import cv2

    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 4.0, (16, 16))
    assert writer.isOpened()
    try:
        for _index in range(4):
            writer.write(np.full((16, 16, 3), 80, dtype=np.uint8))
    finally:
        writer.release()
    output = tmp_path / "output.mp4"
    called = False

    def runner(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("timestamp-cardinality-invalid media must not be muxed")

    with pytest.raises(RescueMediaError):
        render_stabilized_video(
            source,
            output,
            tuple(_correction(index / 4, 0.0) for index in range(5)),
            StabilizationConfig(
                frame_width=16,
                frame_height=16,
                maximum_timeline_gap_seconds=0.3,
            ),
            runner=runner,  # type: ignore[arg-type]
            cancellation_callback=lambda: False,
            frame_timestamps=(0.0, 0.25, 0.5, 0.75, 1.0),
        )

    assert called is False
    assert output.exists() is False


def test_renderer_rejects_missing_exact_source_rate_corrections(
    tmp_path: Path,
) -> None:
    import cv2

    source = tmp_path / "source-rate.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 4.0, (16, 16))
    assert writer.isOpened()
    try:
        for index in range(4):
            writer.write(np.full((16, 16, 3), index * 20, dtype=np.uint8))
    finally:
        writer.release()
    output = tmp_path / "must-not-exist.mp4"
    called = False

    def runner(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("incomplete correction coverage must not be muxed")

    with pytest.raises(RescueMediaError) as captured:
        render_stabilized_video(
            source,
            output,
            (_correction(0.0, 0.0), _correction(0.5, 2.0)),
            StabilizationConfig(frame_width=16, frame_height=16),
            runner=runner,  # type: ignore[arg-type]
            cancellation_callback=lambda: False,
            frame_timestamps=(0.0, 0.25, 0.5, 0.75),
        )

    assert "every source timestamp" in captured.value.internal_message
    assert called is False
    assert output.exists() is False


def test_injectable_motion_estimator_marks_scene_boundary_without_tracking() -> None:
    """Catches tracking features over an explicit scene cut or hiding RANSAC inputs."""
    calls: list[tuple[tuple[int, int], tuple[int, int]]] = []

    def estimator(
        previous: np.ndarray, current: np.ndarray
    ) -> tuple[float, float, float, float, float, float]:
        calls.append((previous.shape, current.shape))
        return (1.0, 1.0, 2.0, -1.0, 0.8, 0.3)

    transforms = estimate_motion_transforms(
        (
            (0.0, np.zeros((4, 4), dtype=np.uint8)),
            (1.0, np.ones((4, 4), dtype=np.uint8)),
            (2.0, np.ones((4, 4), dtype=np.uint8)),
        ),
        StabilizationConfig(frame_width=100, frame_height=100),
        scene_boundaries=(1.0,),
        estimator=estimator,
    )

    assert len(calls) == 1
    assert transforms[0].scene_boundary is True
    assert transforms[0].inlier_ratio == 0.0
    assert transforms[1].translation_x == 2.0


def test_planner_records_exact_flicker_curve_parameters() -> None:
    """Catches replacing an evidence-derived curve with an unbounded generic filter."""
    correction = plan_flicker_correction(
        tuple(
            (index * 0.25, 0.50 + (0.08 if index % 2 else -0.08)) for index in range(12)
        ),
        (
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=3.0,
                duration_seconds=3.0,
                representative_timestamp=1.5,
            ),
        ),
        _flicker_config(),
    )
    interval = DamageInterval(
        id=make_damage_id("b" * 64, "video:0", DamageKind.FLICKER, 0.25, 2.75),
        stream_id="video:0",
        kind=DamageKind.FLICKER,
        start_seconds=0.25,
        end_seconds=2.75,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=100,
            height=100,
            duration_seconds=3.0,
            average_frame_rate=4.0,
            estimated_frame_count=12,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash="b" * 64,
            duration_seconds=3.0,
            scan_coverage=((0.0, 3.0),),
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        flicker_correction=correction,
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.DEFLICKER
    )
    assert action.parameters["affected_ranges"] == [[0.25, 2.75]]
    assert action.parameters["gain_curve"] == [list(item) for item in correction.gains]


def test_planner_omits_unreliable_stabilization_in_favor_of_neutral_fallback() -> None:
    """Catches planning a content-changing motion pass after crop rejection."""
    assessment = assess_stabilization(
        tuple(
            _transform(index * 0.1, 20.0 if index % 2 else -20.0) for index in range(8)
        ),
        StabilizationConfig(max_crop_ratio=0.08, frame_width=100, frame_height=100),
    )
    interval = DamageInterval(
        id=make_damage_id("c" * 64, "video:0", DamageKind.SHAKE, 0.0, 1.0),
        stream_id="video:0",
        kind=DamageKind.SHAKE,
        start_seconds=0.0,
        end_seconds=1.0,
    )

    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=100,
            height=100,
            duration_seconds=1.0,
            average_frame_rate=8.0,
            estimated_frame_count=8,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash="c" * 64,
            duration_seconds=1.0,
            scan_coverage=((0.0, 1.0),),
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        stabilization_assessment=assessment,
    )

    assert RescueActionKind.STABILIZE not in {action.kind for action in plan.actions}


def test_planner_keeps_motion_curve_with_native_preview_support() -> None:
    """Measured motion remains executable when native preview is supported."""
    transforms = (_transform(0.1, 4.0), _transform(0.2, -4.0))
    assessment = assess_stabilization(
        transforms, StabilizationConfig(frame_width=100, frame_height=100)
    )
    interval = DamageInterval(
        id=make_damage_id("d" * 64, "video:0", DamageKind.SHAKE, 0.0, 1.0),
        stream_id="video:0",
        kind=DamageKind.SHAKE,
        start_seconds=0.0,
        end_seconds=1.0,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=100,
            height=100,
            duration_seconds=1.0,
            average_frame_rate=10.0,
            estimated_frame_count=10,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash="d" * 64,
            duration_seconds=1.0,
            scan_coverage=((0.0, 1.0),),
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        stabilization_assessment=assessment,
    )

    assert RescueActionKind.STABILIZE in {action.kind for action in plan.actions}
    assert all(
        "preview_renderer_unavailable" not in warning
        for warning in plan.assessment_warnings
    )


def test_streaming_renderer_honors_cancellation_before_mux(tmp_path: Path) -> None:
    """Catches buffering a full video or invoking mux after cancellation."""
    import cv2

    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 8.0, (16, 16))
    assert writer.isOpened()
    try:
        writer.write(np.zeros((16, 16, 3), dtype=np.uint8))
    finally:
        writer.release()
    called = False

    def runner(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("audio mux must not run after cancellation")

    with pytest.raises(RescueCancelledError):
        render_stabilized_video(
            source,
            tmp_path / "output.mp4",
            (_correction(0.0, 0.0),),
            StabilizationConfig(frame_width=16, frame_height=16, queue_capacity=1),
            runner=runner,  # type: ignore[arg-type]
            cancellation_callback=lambda: True,
        )

    assert called is False


def test_renderer_does_not_publish_partial_mux_output_after_cancellation(
    tmp_path: Path,
) -> None:
    """Catches a cancelled child leaving a partial file at the public output path."""
    import cv2

    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 8.0, (16, 16))
    assert writer.isOpened()
    try:
        writer.write(np.zeros((16, 16, 3), dtype=np.uint8))
    finally:
        writer.release()
    output = tmp_path / "output.mp4"

    def cancelled_runner(arguments: tuple[str, ...], **_kwargs: object) -> object:
        Path(arguments[-1]).write_bytes(b"partial")
        raise RescueCancelledError("cancelled during mux")

    with pytest.raises(RescueCancelledError):
        render_stabilized_video(
            source,
            output,
            (_correction(0.0, 0.0),),
            StabilizationConfig(frame_width=16, frame_height=16, queue_capacity=1),
            runner=cancelled_runner,  # type: ignore[arg-type]
            cancellation_callback=lambda: False,
            frame_timestamps=(0.0,),
        )

    assert output.exists() is False


def test_renderer_rejects_existing_output_that_aliases_source(tmp_path: Path) -> None:
    """Catches FFmpeg overwriting the source through a pre-existing hard link."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    output.hardlink_to(source)

    with pytest.raises(RescueArtifactError) as captured:
        render_stabilized_video(
            source,
            output,
            (_correction(0.125, 0.0),),
            StabilizationConfig(frame_width=16, frame_height=16),
            runner=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
            cancellation_callback=lambda: False,
        )
    assert captured.value.internal_message == (
        "stabilization output must not alias the source"
    )


def test_native_executor_uses_shared_runner_for_stabilized_audio_mux(
    tmp_path: Path,
) -> None:
    """Catches stabilization bypassing the bounded executor command boundary."""
    import cv2

    source = tmp_path / "source.mp4"
    writer = cv2.VideoWriter(str(source), _mp4v_fourcc(), 8.0, (16, 16))
    assert writer.isOpened()
    try:
        writer.write(np.zeros((16, 16, 3), dtype=np.uint8))
    finally:
        writer.release()
    output = tmp_path / "stabilized.mp4"
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        Path(arguments[-1]).write_bytes(b"muxed")
        return CommandResult(returncode=0, stderr_summary="")

    executor = NativeRescueExecutor(runner=runner)
    executor.execute_stabilized(
        source=source,
        output=output,
        transforms=(_correction(0.0, 0.0),),
        config=StabilizationConfig(frame_width=16, frame_height=16),
        cancellation_callback=lambda: False,
        frame_timestamps=(0.0,),
    )

    assert output.read_bytes() == b"muxed"
    assert len(calls) == 1
    first_input = calls[0][calls[0].index("-i") + 1]
    assert first_input.endswith("video-only-lossless.avi")
    assert calls[0][calls[0].index("-map") + 1] == "0:v:0"
    assert "1:a?" in calls[0]
    assert calls[0][calls[0].index("-preset:v:0") + 1] == "medium"
    assert calls[0][calls[0].index("-crf:v:0") + 1] == "16"
    assert calls[0][calls[0].index("-profile:v:0") + 1] == "high"
    assert calls[0][calls[0].index("-video_track_timescale") + 1] == "120000"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_configs_and_transforms_reject_non_finite_values(value: float) -> None:
    """Catches non-finite numbers entering deterministic correction plans."""
    with pytest.raises(ValidationError):
        FlickerConfig(residual_threshold=value)
    with pytest.raises(ValidationError):
        StabilizationConfig(max_crop_ratio=value)
    with pytest.raises(ValidationError):
        _transform(0.0, value)


def test_flicker_plan_rejects_gain_beyond_global_safety_cap() -> None:
    """Catches a forged correction plan bypassing the configured gain ceiling."""
    with pytest.raises(ValidationError):
        FlickerCorrectionPlan(
            intervals=((0.0, 1.0),),
            gains=((0.0, 1.26),),
        )
