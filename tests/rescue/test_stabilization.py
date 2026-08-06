"""Tests for bounded, deterministic flicker and motion stabilization helpers."""

from __future__ import annotations

import math
import shutil
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
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
    assess_stabilization,
    estimate_motion_transforms,
    motion_correction_at_timestamp,
    motion_corrections_for_timestamps,
    render_stabilized_video,
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


def _mp4v_fourcc() -> int:
    import cv2

    return cast(int, cast(Any, cv2).VideoWriter_fourcc(*"mp4v"))


def _real_rescue_fixture(filename: str) -> Path:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
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
    assert calls[0][calls[0].index("-fps_mode") + 1] == "passthrough"
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
        transforms = tuple(
            _correction(index / 10.0, 1.0 if index % 2 else -1.0)
            for index in range(1, 11)
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


def test_real_shake_never_claims_measured_crop_without_native_evidence(
    tmp_path: Path,
) -> None:
    """Catches a measured crop estimate bypassing the native preview safety gate."""
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
        assert all(
            action.kind is not RescueActionKind.STABILIZE
            for action in preparation.plan.actions
        )
        assert "preview_renderer_unavailable" in " ".join(
            preparation.plan.assessment_warnings
        )
        assert preparation.previews is not None
        assert preparation.previews.improved is None
        assert preparation.previews.source.paths
        assert preparation.previews.faithful.paths
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
        _transform(0.1, 1.0),
        _transform(0.2, 0.0, scene_boundary=True),
        _transform(0.3, -1.0),
    )

    result = assess_stabilization(
        transforms, StabilizationConfig(frame_width=100, frame_height=100)
    )

    assert result.recommended is True
    assert result.transforms[1].scene_boundary is True
    assert result.transforms[1].semantics == "frame_correction"
    assert result.transforms[1].translation_x == 0.0


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


def test_renderer_interpolates_sampled_corrections_for_decoded_pixels(
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
    corrections = (
        _correction(0.0, 0.0),
        _correction(0.25, -4.0),
        _correction(0.5, 0.0),
        _correction(0.75, -4.0),
        _correction(1.0, 0.0),
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
    assert calls[0][calls[0].index("-fps_mode") + 1] == "passthrough"


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


def test_planner_review_gates_motion_curve_without_preview_support() -> None:
    """Catches a measured motion curve bypassing the preview capability gate."""
    transforms = (_transform(0.1, 1.0), _transform(0.2, -1.0))
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

    assert RescueActionKind.STABILIZE not in {action.kind for action in plan.actions}
    assert (
        "Automatic stabilize action needs review: preview_renderer_unavailable."
        in plan.assessment_warnings
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
            (_correction(0.125, 0.0),),
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
            (_correction(0.125, 0.0),),
            StabilizationConfig(frame_width=16, frame_height=16, queue_capacity=1),
            runner=cancelled_runner,  # type: ignore[arg-type]
            cancellation_callback=lambda: False,
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
        transforms=(_correction(0.125, 0.0),),
        config=StabilizationConfig(frame_width=16, frame_height=16),
        cancellation_callback=lambda: False,
    )

    assert output.read_bytes() == b"muxed"
    assert len(calls) == 1
    assert calls[0][calls[0].index("-map") + 1] == "0:v:0"
    assert "1:a?" in calls[0]


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
