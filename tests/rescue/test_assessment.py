"""Production-style wiring tests for measured Rescue assessments."""

from __future__ import annotations

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, NoReturn, cast

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray

from videoscope.domain import VideoMetadata
from videoscope.rescue import assessment as assessment_module
from videoscope.rescue.assessment import (
    AssessmentCancellation,
    LocalRescueAssessmentService,
    MotionRefinementProvider,
    RescueAssessmentConfig,
    RescueSampledFrames,
    SyncEventMeasurements,
)
from videoscope.rescue.audio import LoudnessMeasurement
from videoscope.rescue.deblur import BlurKernelEstimate, DeblurConfig
from videoscope.rescue.errors import RescueCancelledError
from videoscope.rescue.executor import CommandResult
from videoscope.rescue.models import (
    MediaDamageMap,
    RescueActionKind,
    RescueEffectiveConfig,
    RescueStrategy,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    RescuePipelineDependencies,
    VideoRescuePipeline,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.preview import RescuePreviewSet, RescuePreviewVariant
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationAssessment,
    StabilizationConfig,
    estimate_anchor_corrections,
)
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
)
from videoscope.rescue.visual import (
    VisualActionInterval,
    VisualAssessment,
    VisualMetrics,
    VisualSample,
)
from videoscope.scenes import VideoScene


def _metadata(source: Path) -> VideoMetadata:
    return VideoMetadata(
        filename=source.name,
        container_format="mp4",
        codec="h264",
        width=32,
        height=32,
        duration_seconds=4.0,
        average_frame_rate=2.0,
        estimated_frame_count=8,
        has_audio=True,
        file_size_bytes=source.stat().st_size,
    )


def _sampled_frames() -> RescueSampledFrames:
    import cv2

    frames: list[np.ndarray] = []
    samples: list[VisualSample] = []
    rng = np.random.default_rng(501)
    texture = rng.integers(0, 256, size=(32, 32), dtype=np.uint8)
    for index in range(8):
        low = 0.03 if index % 2 == 0 else 0.16
        array = np.fromfunction(
            lambda row, column: low + ((row + column + index) % 2) * 0.05,
            (32, 32),
            dtype=int,
        ).astype(np.float64)
        frames.append(
            cv2.warpAffine(
                texture,
                np.array(((1.0, 0.0, index % 2), (0.0, 1.0, 0.0))),
                (32, 32),
                borderMode=cv2.BORDER_REFLECT,
            )
        )
        samples.append(
            VisualSample(
                timestamp_seconds=index * 0.5,
                luma=tuple(tuple(float(value) for value in row) for row in array),
            )
        )
    return RescueSampledFrames(
        visual_samples=tuple(samples),
        motion_frames=tuple(
            (sample.timestamp_seconds, frame)
            for sample, frame in zip(samples, frames, strict=True)
        ),
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=4.0,
                duration_seconds=4.0,
                representative_timestamp=2.0,
            ),
        ),
        sample_rate=2.0,
        decode_passes=1,
    )


def _service(
    *,
    motion_error: bool = False,
    motion_refinement_provider: MotionRefinementProvider | None = None,
) -> LocalRescueAssessmentService:
    def estimate(
        _left: np.ndarray, _right: np.ndarray
    ) -> tuple[float, float, float, float, float, float]:
        if motion_error:
            raise RuntimeError("motion component failed")
        return (0.0, 1.0, 1.0, 0.5, 0.95, 0.25)

    return LocalRescueAssessmentService(
        frame_provider=lambda *_args, **_kwargs: _sampled_frames(),
        loudness_provider=lambda *_args, **_kwargs: LoudnessMeasurement(
            input_i=-28.0,
            input_tp=-4.0,
            input_lra=5.0,
            input_thresh=-38.0,
            target_offset=0.0,
            noise_floor_dbfs=-30.0,
            noise_confidence=0.95,
            noise_event_count=5,
        ),
        sync_provider=lambda *_args, **_kwargs: SyncEventMeasurements(
            audio_events=((1.2, 0.95), (2.2, 0.95), (3.2, 0.95)),
            video_events=((1.0, 0.95), (2.0, 0.95), (3.0, 0.95)),
        ),
        motion_estimator=estimate,
        motion_refinement_provider=(
            motion_refinement_provider
            if motion_refinement_provider is not None
            else lambda _source, _ranges, _metadata, _config, _callback: (
                _sampled_frames().motion_frames
            )
        ),
    )


class _Scanner:
    def scan(
        self,
        _source: Path,
        source_hash: str,
        metadata: VideoMetadata,
        _config: object,
    ) -> MediaDamageMap:
        return MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=metadata.duration_seconds,
            scan_coverage=((0.0, metadata.duration_seconds),),
        )


class _Preview:
    def build(self, _plan: object, _source: Path, root: Path) -> RescuePreviewSet:
        empty = RescuePreviewVariant("source", (), ())
        return RescuePreviewSet(empty, RescuePreviewVariant("faithful", (), ()), None)


def test_pipeline_balanced_plan_comes_from_measured_assessment_bundle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "measured source.mp4"
    source.write_bytes(b"measured source")
    pipeline = VideoRescuePipeline(
        RescueConfig(tmp_path / "output", strategy=RescueStrategy.BALANCED),
        dependencies=RescuePipelineDependencies(
            probe=_metadata,
            scanner=_Scanner(),
            assessment_service=_service(),
            planner=build_rescue_plan,
            preview_builder=_Preview(),
        ),
    )

    preparation = pipeline.prepare(source)

    kinds = {action.kind for action in preparation.plan.actions}
    assert {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.DEFLICKER,
        RescueActionKind.NORMALIZE_AUDIO,
        RescueActionKind.DENOISE_AUDIO,
        RescueActionKind.CORRECT_FIXED_AV_OFFSET,
    }.issubset(kinds)
    assert RescueActionKind.STABILIZE in kinds
    assert "preview_renderer_unavailable" not in " ".join(
        preparation.plan.assessment_warnings
    )
    assert preparation.assessments.parameters["frame_decode_passes"] == 2
    assert preparation.assessments.warnings == ()
    assert preparation.damage_map.input_hash == sha256(source.read_bytes()).hexdigest()
    assert preparation.plan.effective_config == RescueEffectiveConfig()


def test_failed_motion_assessment_is_isolated_from_other_measured_actions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    service = _service(motion_error=True)
    base = MediaDamageMap(
        input_hash=sha256(source.read_bytes()).hexdigest(),
        duration_seconds=4.0,
        scan_coverage=((0.0, 4.0),),
    )

    bundle = service.assess(
        source,
        base.input_hash,
        _metadata(source),
        base,
        tmp_path / "workspace",
        lambda: False,
    )

    assert any(warning.component == "stabilization" for warning in bundle.warnings)
    assert bundle.visual_assessment is not None
    assert bundle.audio_assessment is not None
    assert bundle.stabilization_assessment is None
    merged = bundle.merge_damage_map(base)
    plan = build_rescue_plan(
        metadata=_metadata(source),
        damage_map=merged,
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=bundle.visual_assessment,
        flicker_correction=bundle.flicker_correction,
        stabilization_assessment=bundle.stabilization_assessment,
        audio_assessment=bundle.audio_assessment,
        fixed_offset_assessment=bundle.fixed_offset_assessment,
    )
    kinds = {action.kind for action in plan.actions}
    assert RescueActionKind.STABILIZE not in kinds
    assert RescueActionKind.ADJUST_LUMA in kinds
    assert RescueActionKind.NORMALIZE_AUDIO in kinds


def test_assessment_emits_exact_input_driven_perceptual_measurements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches plans that invent Task 2-5 parameters instead of copying measures."""
    source = tmp_path / "输入 视频 ü.mp4"
    source.write_bytes(b"measured perceptual source")
    deblur_config = DeblurConfig(candidate_radii=(2,))
    tonal_config = TonalInterferenceConfig(minimum_confidence=0.8)
    estimate = BlurKernelEstimate(
        kernel_kind="box",
        radius=2,
        regularization=0.003,
        confidence=0.93,
        edge_width_before=4.0,
        predicted_edge_width_after=2.0,
        edge_continuity_ratio=0.9,
        reblur_error_ratio=0.01,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )
    tone = InterferenceTone(
        start_seconds=1.25,
        end_seconds=2.75,
        center_frequency_hz=913.0,
        confidence=0.91,
        baseline_before_dbfs=-58.0,
        baseline_after_dbfs=-57.0,
        peak_dbfs=-13.0,
        local_peak_over_baseline_db=44.0,
        persistence_window_count=24,
        frequency_standard_deviation_hz=1.25,
        channel_indices=(0,),
        attenuation_target_db=24.0,
        render_qualification=TonalRenderQualification(
            boundary_mode="full_interval_v1",
            notch_q=8.0,
            complete_window_count=30,
            minimum_target_reduction_db=25.0,
            maximum_non_target_attenuation_db=0.1,
            maximum_boundary_energy_jump_db=0.1,
            maximum_boundary_crest_jump_db=0.1,
            maximum_boundary_adjacent_delta=0.01,
        ),
    )
    visual = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.1,
            luma_p50=0.4,
            luma_p90=0.8,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.01,
            sharpness=0.002,
        ),
        recommended_actions=(RescueActionKind.SHARPEN,),
        action_intervals=(
            VisualActionInterval(
                action=RescueActionKind.SHARPEN,
                start_seconds=0.5,
                end_seconds=2.0,
            ),
        ),
        preview_required=True,
        public_explanation="Persistent local soft detail was measured.",
    )
    monkeypatch.setattr(assessment_module, "assess_visual_samples", lambda *_: visual)
    measured_frames: list[tuple[tuple[int, ...], DeblurConfig]] = []

    def measure_deblur(
        frames: Sequence[NDArray[np.uint8]], config: DeblurConfig
    ) -> BlurKernelEstimate | None:
        measured_frames.append((tuple(int(frame[0, 0]) for frame in frames), config))
        return estimate

    service = LocalRescueAssessmentService(
        config=RescueAssessmentConfig(deblur=deblur_config, tonal=tonal_config),
        frame_provider=lambda *_args, **_kwargs: _sampled_frames(),
        loudness_provider=lambda *_args, **_kwargs: LoudnessMeasurement(
            input_i=-20.0,
            input_tp=-4.0,
            input_lra=5.0,
            input_thresh=-30.0,
            target_offset=0.0,
        ),
        deblur_estimator=measure_deblur,
        tonal_provider=lambda *_args, **_kwargs: (tone,),
        motion_estimator=lambda *_args: None,
    )
    source_hash = sha256(source.read_bytes()).hexdigest()
    base = MediaDamageMap(
        input_hash=source_hash,
        duration_seconds=4.0,
        scan_coverage=((0.0, 4.0),),
    )

    first = service.assess(
        source,
        source_hash,
        _metadata(source),
        base,
        tmp_path / "工作 区",
        lambda: False,
    )
    second = service.assess(
        source,
        source_hash,
        _metadata(source),
        base,
        tmp_path / "工作 区二",
        lambda: False,
    )

    assert first.parameters == second.parameters
    assert measured_frames
    deblur = cast(list[dict[str, object]], first.parameters["deblur_measurements"])
    tonal = cast(
        list[dict[str, object]], first.parameters["tonal_interference_measurements"]
    )
    assert deblur[0]["source_ranges"] == [[0.5, 2.0]]
    assert deblur[0]["estimate"] == estimate.model_dump(mode="json")
    assert deblur[0]["config"] == deblur_config.model_dump(mode="json")
    assert tonal[0]["source_ranges"] == [[1.25, 2.75]]
    assert tonal[0]["interference_profiles"] == [tone.model_dump(mode="json")]
    assert tonal[0]["config"] == tonal_config.model_dump(mode="json")
    plan = build_rescue_plan(
        metadata=_metadata(source),
        damage_map=first.merge_damage_map(base),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters=first.parameters,
        assessment_limitations=first.limitations,
        visual_assessment=first.visual_assessment,
    )
    kinds = tuple(action.kind for action in plan.actions)
    assert RescueActionKind.DEBLUR in kinds
    assert RescueActionKind.SHARPEN not in kinds


def test_inconclusive_deblur_measurement_fails_closed_with_limitation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "soft source.mp4"
    source.write_bytes(b"soft source")
    visual = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.1,
            luma_p50=0.4,
            luma_p90=0.8,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.01,
            sharpness=0.002,
        ),
        recommended_actions=(RescueActionKind.SHARPEN,),
        action_intervals=(
            VisualActionInterval(
                action=RescueActionKind.SHARPEN,
                start_seconds=0.5,
                end_seconds=2.0,
            ),
        ),
        preview_required=True,
        public_explanation="Persistent local soft detail was measured.",
    )
    monkeypatch.setattr(assessment_module, "assess_visual_samples", lambda *_: visual)
    service = LocalRescueAssessmentService(
        frame_provider=lambda *_args, **_kwargs: _sampled_frames(),
        loudness_provider=lambda *_args, **_kwargs: LoudnessMeasurement(
            input_i=-20.0,
            input_tp=-4.0,
            input_lra=5.0,
            input_thresh=-30.0,
            target_offset=0.0,
        ),
        deblur_estimator=lambda *_args: None,
        tonal_provider=lambda *_args, **_kwargs: (),
        motion_estimator=lambda *_args: None,
    )
    source_hash = sha256(source.read_bytes()).hexdigest()
    base = MediaDamageMap(
        input_hash=source_hash,
        duration_seconds=4.0,
        scan_coverage=((0.0, 4.0),),
    )

    bundle = service.assess(
        source, source_hash, _metadata(source), base, tmp_path / "work", lambda: False
    )

    assert bundle.parameters["deblur_measurements"] == []
    assert any("deblur" in limitation.lower() for limitation in bundle.limitations)


def test_unqualified_tonal_profile_fails_closed_with_limitation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "tonal source.mp4"
    source.write_bytes(b"tonal source")
    unqualified = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=117.84618303542793,
        confidence=0.95,
        baseline_before_dbfs=-58.0,
        baseline_after_dbfs=-57.0,
        peak_dbfs=-13.0,
        local_peak_over_baseline_db=44.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=1.0,
        channel_indices=(0,),
        attenuation_target_db=24.0,
    )
    service = LocalRescueAssessmentService(
        frame_provider=lambda *_args, **_kwargs: _sampled_frames(),
        loudness_provider=lambda *_args, **_kwargs: LoudnessMeasurement(
            input_i=-20.0,
            input_tp=-4.0,
            input_lra=5.0,
            input_thresh=-30.0,
            target_offset=0.0,
        ),
        deblur_estimator=lambda *_args: None,
        tonal_provider=lambda *_args, **_kwargs: (unqualified,),
        motion_estimator=lambda *_args: None,
    )
    source_hash = sha256(source.read_bytes()).hexdigest()
    base = MediaDamageMap(
        input_hash=source_hash,
        duration_seconds=4.0,
        scan_coverage=((0.0, 4.0),),
    )

    bundle = service.assess(
        source,
        source_hash,
        _metadata(source),
        base,
        tmp_path / "work",
        lambda: False,
    )

    assert bundle.parameters["tonal_interference_measurements"] == []
    assert any(
        "without one renderer passing every complete 50 ms" in limitation
        for limitation in bundle.limitations
    )


def test_detected_shake_is_refined_at_higher_temporal_resolution(
    tmp_path: Path,
) -> None:
    """Catches trusting a 2 fps motion curve for high-frequency shake repair."""
    source = tmp_path / "high frequency shake.mp4"
    source.write_bytes(b"measured source")
    calls: list[tuple[tuple[float, float], ...]] = []

    def refine(
        _source: Path,
        ranges: tuple[tuple[float, float], ...],
        _metadata: VideoMetadata,
        _config: RescueAssessmentConfig,
        _callback: AssessmentCancellation,
        **_kwargs: object,
    ) -> tuple[tuple[float, NDArray[np.uint8]], ...]:
        import cv2

        calls.append(ranges)
        refined: list[tuple[float, NDArray[np.uint8]]] = []
        rng = np.random.default_rng(502)
        texture = rng.integers(0, 256, size=(32, 32), dtype=np.uint8)
        for index in range(33):
            frame = cv2.warpAffine(
                texture,
                np.array(((1.0, 0.0, 3.0 if index % 2 else -3.0), (0.0, 1.0, 0.0))),
                (32, 32),
                borderMode=cv2.BORDER_REFLECT,
            )
            refined.append((index / 8.0, frame))
        return tuple(refined)

    service = _service(
        motion_refinement_provider=cast(MotionRefinementProvider, refine)
    )
    metadata = _metadata(source).model_copy(
        update={"average_frame_rate": 8.0, "estimated_frame_count": 32}
    )
    base = MediaDamageMap(
        input_hash=sha256(source.read_bytes()).hexdigest(),
        duration_seconds=4.0,
        scan_coverage=((0.0, 4.0),),
    )

    bundle = service.assess(
        source,
        base.input_hash,
        metadata,
        base,
        tmp_path / "workspace",
        lambda: False,
    )

    assert calls == [((0.0, 4.0),)]
    assert bundle.stabilization_assessment is not None
    assert len(bundle.stabilization_assessment.transforms) == 32
    assert bundle.stabilization_assessment.parameters["method"] == "anchor_v1"
    assert bundle.stabilization_assessment.parameters["algorithm_version"] == "1"
    assert bundle.stabilization_assessment.parameters["frame_rate_inventory"] == {
        "source_rate_fps": 8.0,
        "frame_count": 32,
        "complete": True,
    }
    stabilization_config = cast(
        dict[str, object], bundle.stabilization_assessment.parameters["config"]
    )
    assert stabilization_config["accepted_ranges"] == [[0.0, 4.0]]
    assert bundle.parameters["motion_refinement_sample_rate"] == 8.0
    assert bundle.parameters["motion_refinement_frame_count"] == 33


def test_motion_refinement_uses_capped_source_rate_and_bounded_inventory(
    tmp_path: Path,
) -> None:
    """Catches silently returning to 8 fps or decoding beyond the configured cap."""
    import cv2

    source = tmp_path / "源 video 24fps.mp4"
    writer = cv2.VideoWriter(
        str(source),
        cast(int, getattr(cv2, "VideoWriter_fourcc")(*"mp4v")),
        24.0,
        (32, 32),
    )
    assert writer.isOpened()
    try:
        for index in range(24):
            writer.write(np.full((32, 32, 3), index, dtype=np.uint8))
    finally:
        writer.release()
    metadata = VideoMetadata(
        filename=source.name,
        container_format="mp4",
        codec="mpeg4",
        width=32,
        height=32,
        duration_seconds=1.0,
        average_frame_rate=24.0,
        estimated_frame_count=24,
        has_audio=False,
        file_size_bytes=source.stat().st_size,
    )
    config = RescueAssessmentConfig(maximum_motion_refinement_frames=30)

    frames = assessment_module._sample_motion_ranges(  # noqa: SLF001
        source,
        ((0.0, 1.0),),
        metadata,
        config,
        lambda: False,
        timestamp_provider=lambda *_args: tuple(index / 24.0 for index in range(24)),
        frame_decoder=lambda _source, _ranges, timestamps, _config, _callback: tuple(
            np.zeros((32, 32), dtype=np.uint8) for _timestamp in timestamps
        ),
    )

    assert len(frames) == 24
    assert tuple(timestamp for timestamp, _frame in frames) == pytest.approx(
        tuple(index / 24.0 for index in range(24))
    )


@pytest.mark.parametrize(
    ("timestamps", "decoded_count", "error"),
    [
        ((5.125, 5.161, 5.205), 3, None),
        ((5.125, 5.161, 5.205), 2, "inventory does not match"),
        ((5.125, 6.5), 2, "timestamp gap"),
        ((4.99, 5.1), 2, "outside requested ranges"),
    ],
)
def test_motion_refinement_pairs_actual_vfr_pts_with_bounded_decode(
    tmp_path: Path,
    timestamps: tuple[float, ...],
    decoded_count: int,
    error: str | None,
) -> None:
    """No frame ordinal, nominal FPS, or seek target may become source PTS."""
    source = tmp_path / "actual pts source.mp4"
    source.write_bytes(b"provider seam")
    metadata = _metadata(source).model_copy(
        update={
            "duration_seconds": 8.0,
            "average_frame_rate": 24.0,
            "estimated_frame_count": 192,
        }
    )
    config = RescueAssessmentConfig(maximum_motion_refinement_frames=60)
    timestamp_calls: list[tuple[tuple[float, float], ...]] = []

    def actual_pts(
        _source: Path,
        ranges: tuple[tuple[float, float], ...],
        _config: RescueAssessmentConfig,
        _callback: AssessmentCancellation,
    ) -> tuple[float, ...]:
        timestamp_calls.append(ranges)
        return timestamps

    def bounded_decode(
        _source: Path,
        _ranges: tuple[tuple[float, float], ...],
        _timestamps: tuple[float, ...],
        _config: RescueAssessmentConfig,
        _callback: AssessmentCancellation,
    ) -> tuple[NDArray[np.uint8], ...]:
        return tuple(
            np.full((32, 32), index, dtype=np.uint8) for index in range(decoded_count)
        )

    if error is not None:
        with pytest.raises(ValueError, match=error):
            assessment_module._sample_motion_ranges(  # noqa: SLF001
                source,
                ((5.0, 7.0),),
                metadata,
                config,
                lambda: False,
                timestamp_provider=actual_pts,
                frame_decoder=bounded_decode,
            )
        return
    frames = assessment_module._sample_motion_ranges(  # noqa: SLF001
        source,
        ((5.0, 7.0),),
        metadata,
        config,
        lambda: False,
        timestamp_provider=actual_pts,
        frame_decoder=bounded_decode,
    )

    assert timestamp_calls == [((5.0, 7.0),)]
    assert tuple(timestamp for timestamp, _frame in frames) == timestamps


def test_native_motion_pts_probe_normalizes_measured_nonzero_stream_start(
    tmp_path: Path,
) -> None:
    source = tmp_path / "nonzero start source.mp4"
    source.write_bytes(b"probe seam")
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        if "stream=start_time" in arguments:
            return CommandResult(0, "", "stream|5.000000\n")
        return CommandResult(
            0,
            "",
            "frame|37.000000\nframe|37.041000\nframe|37.125000\nframe|37.200000\n",
        )

    timestamps = assessment_module._probe_motion_range_timestamps(  # noqa: SLF001
        source,
        ((32.0, 32.2),),
        RescueAssessmentConfig(maximum_motion_refinement_frames=8),
        lambda: False,
        ffprobe="ffprobe-custom",
        runner=cast(Any, runner),
    )

    assert timestamps == pytest.approx((32.0, 32.041, 32.125))
    assert calls[0][0] == calls[1][0] == "ffprobe-custom"
    interval_index = calls[1].index("-read_intervals") + 1
    raw_start, raw_end = (float(value) for value in calls[1][interval_index].split("%"))
    assert (raw_start, raw_end) == pytest.approx((37.0, 37.2))


def test_motion_refinement_fails_before_decoding_inventory_overflow(
    tmp_path: Path,
) -> None:
    source = tmp_path / "bounded.mp4"
    source.write_bytes(b"not opened because inventory fails first")
    metadata = VideoMetadata(
        filename=source.name,
        container_format="mp4",
        codec="h264",
        width=32,
        height=32,
        duration_seconds=2.0,
        average_frame_rate=30.0,
        estimated_frame_count=60,
        has_audio=False,
        file_size_bytes=source.stat().st_size,
    )

    with pytest.raises(ValueError, match="inventory"):
        assessment_module._sample_motion_ranges(  # noqa: SLF001
            source,
            ((0.0, 2.0),),
            metadata,
            RescueAssessmentConfig(maximum_motion_refinement_frames=59),
            lambda: False,
        )


def test_production_default_refines_only_local_source_rate_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long source must refine the measured local range, not its full runtime."""
    source = tmp_path / "long source.mp4"
    source.write_bytes(b"measured source")
    calls: list[tuple[tuple[float, float], ...]] = []

    def local_refinement(
        _source: Path,
        ranges: tuple[tuple[float, float], ...],
        _metadata: VideoMetadata,
        _config: RescueAssessmentConfig,
        _callback: AssessmentCancellation,
        **_kwargs: object,
    ) -> tuple[tuple[float, NDArray[np.uint8]], ...]:
        calls.append(ranges)
        texture = np.random.default_rng(902).integers(
            0, 256, size=(32, 32), dtype=np.uint8
        )
        return tuple((32.0 + index / 24.0, texture) for index in range(96))

    monkeypatch.setattr(assessment_module, "_sample_motion_ranges", local_refinement)

    def estimate(
        _left: np.ndarray, _right: np.ndarray
    ) -> tuple[float, float, float, float, float, float]:
        return (0.0, 1.0, 1.0, 0.5, 0.95, 0.25)

    coarse = _sampled_frames()
    late_motion = tuple(
        (32.0 + index, frame)
        for index, (_timestamp, frame) in enumerate(coarse.motion_frames[:4])
    )
    late_samples = tuple(
        VisualSample(
            timestamp_seconds=32.0 + index,
            luma=sample.luma,
        )
        for index, sample in enumerate(coarse.visual_samples[:4])
    )
    late_frames = RescueSampledFrames(
        visual_samples=late_samples,
        motion_frames=late_motion,
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=42.0,
                duration_seconds=42.0,
                representative_timestamp=21.0,
            ),
        ),
        sample_rate=2.0,
        decode_passes=1,
    )
    service = LocalRescueAssessmentService(
        frame_provider=lambda *_args, **_kwargs: late_frames,
        loudness_provider=lambda *_args, **_kwargs: LoudnessMeasurement(
            input_i=-20.0,
            input_tp=-3.0,
            input_lra=4.0,
            input_thresh=-30.0,
            target_offset=0.0,
            noise_floor_dbfs=-50.0,
            noise_confidence=0.0,
            noise_event_count=0,
        ),
        motion_estimator=estimate,
    )
    metadata = _metadata(source).model_copy(
        update={
            "duration_seconds": 42.0,
            "average_frame_rate": 24.0,
            "estimated_frame_count": 1008,
            "has_audio": False,
        }
    )
    base = MediaDamageMap(
        input_hash=sha256(source.read_bytes()).hexdigest(),
        duration_seconds=42.0,
        scan_coverage=((0.0, 42.0),),
    )

    bundle = service.assess(
        source,
        base.input_hash,
        metadata,
        base,
        tmp_path / "workspace",
        lambda: False,
    )

    assert calls == [((32.0, 36.0),)]
    assert bundle.parameters["motion_refinement_frame_count"] == 96
    assert bundle.parameters["frame_decode_passes"] == 2


def test_assessment_refines_only_bounded_run_after_over_budget_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a rejected early run preventing a later source-rate refinement."""
    source = tmp_path / "mixed motion source.mp4"
    source.write_bytes(b"measured source")
    calls: list[tuple[tuple[float, float], ...]] = []
    coarse_transforms = tuple(
        [
            MotionTransform(
                timestamp_seconds=1.0,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=100.0,
                translation_y=0.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
            ),
            MotionTransform(
                timestamp_seconds=1.5,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=-100.0,
                translation_y=0.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
            ),
            MotionTransform(
                timestamp_seconds=2.0,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=0.0,
                translation_y=0.0,
                inlier_ratio=0.1,
                residual_pixels=8.0,
            ),
        ]
        + [
            MotionTransform(
                timestamp_seconds=timestamp,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=value,
                translation_y=0.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
            )
            for timestamp, value in (
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
            MotionTransform(
                timestamp_seconds=6.5,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=0.0,
                translation_y=0.0,
                inlier_ratio=0.1,
                residual_pixels=8.0,
            )
        ]
    )

    monkeypatch.setattr(
        assessment_module,
        "estimate_motion_transforms",
        lambda *_args, **_kwargs: coarse_transforms,
    )

    def refine(
        _source: Path,
        ranges: tuple[tuple[float, float], ...],
        _metadata: VideoMetadata,
        _config: RescueAssessmentConfig,
        _callback: AssessmentCancellation,
    ) -> tuple[tuple[float, NDArray[np.uint8]], ...]:
        calls.append(ranges)
        frame: NDArray[np.uint8] = np.zeros((360, 640), dtype=np.uint8)
        return tuple((3.0 + index * 0.5, frame) for index in range(6))

    monkeypatch.setattr(
        assessment_module,
        "estimate_anchor_corrections",
        lambda frames, *_args, **_kwargs: tuple(
            MotionTransform(
                timestamp_seconds=timestamp,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=4.0 if index % 2 else -4.0,
                translation_y=0.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
                semantics="frame_correction",
            )
            for index, (timestamp, _frame) in enumerate(frames)
        ),
    )
    base_samples = _sampled_frames()
    sampled = RescueSampledFrames(
        visual_samples=base_samples.visual_samples,
        motion_frames=tuple(
            (index * 0.5, np.zeros((360, 640), dtype=np.uint8)) for index in range(20)
        ),
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=10.0,
                duration_seconds=10.0,
                representative_timestamp=5.0,
            ),
        ),
        sample_rate=2.0,
        decode_passes=1,
    )
    service = LocalRescueAssessmentService(
        frame_provider=lambda *_args, **_kwargs: sampled,
        loudness_provider=lambda *_args, **_kwargs: LoudnessMeasurement(
            input_i=-20.0,
            input_tp=-3.0,
            input_lra=4.0,
            input_thresh=-30.0,
            target_offset=0.0,
            noise_floor_dbfs=-50.0,
            noise_confidence=0.0,
            noise_event_count=0,
        ),
        motion_refinement_provider=cast(MotionRefinementProvider, refine),
    )
    metadata = _metadata(source).model_copy(
        update={
            "width": 640,
            "height": 360,
            "duration_seconds": 10.0,
            "average_frame_rate": 24.0,
            "estimated_frame_count": 240,
            "has_audio": False,
        }
    )
    base = MediaDamageMap(
        input_hash=sha256(source.read_bytes()).hexdigest(),
        duration_seconds=10.0,
        scan_coverage=((0.0, 10.0),),
    )

    bundle = service.assess(
        source,
        base.input_hash,
        metadata,
        base,
        tmp_path / "workspace",
        lambda: False,
    )

    assert calls == [((3.0, 6.0),)]
    assert bundle.stabilization_assessment is not None
    assert bundle.stabilization_assessment.recommended is True
    assert bundle.stabilization_assessment.parameters["method"] == "anchor_v1"
    assert bundle.stabilization_assessment.crop_ratio <= 0.12
    raw_stabilization_config = bundle.stabilization_assessment.parameters["config"]
    assert isinstance(raw_stabilization_config, dict)
    assert raw_stabilization_config["accepted_ranges"] == [[3.0, 6.0]]
    coarse_runs = bundle.stabilization_assessment.parameters["coarse_run_assessments"]
    assert isinstance(coarse_runs, list)
    typed_coarse_runs = cast(list[dict[str, object]], coarse_runs)
    assert any(
        item["accepted"] is False and item["reason"] == "crop_budget_exceeded"
        for item in typed_coarse_runs
    )
    assert any(
        item["accepted"] is True
        and item["start_seconds"] == 3.0
        and item["end_seconds"] == 6.0
        for item in typed_coarse_runs
    )
    assert bundle.parameters["motion_refinement_frame_count"] == 6
    assert any("crop budget" in limitation.lower() for limitation in bundle.limitations)
    assert any(
        "excluded frames are not claimed as corrected" in limitation.lower()
        for limitation in bundle.limitations
    )


@pytest.mark.parametrize(
    "transition_outcome",
    (
        "success",
        "empty",
        "missing_pts",
        "inventory",
        "opencv",
        "cancelled",
        "base_exception",
    ),
)
def test_assessment_merges_one_measured_transition_with_following_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transition_outcome: str,
) -> None:
    """Catches retaining an anchor-only gap after strict transition evidence."""
    source = tmp_path / "transition source.mp4"
    source.write_bytes(b"measured source")
    coarse = StabilizationAssessment(
        recommended=True,
        reason="measured_affine_motion",
        crop_ratio=0.02,
        transforms=(),
        parameters={
            "affected_ranges": [[33.0, 36.0]],
            "run_assessments": [
                {
                    "accepted": False,
                    "reason": "crop_budget_exceeded",
                    "start_seconds": 6.0,
                    "end_seconds": 32.0,
                    "start_boundary_limited": True,
                    "end_boundary_limited": True,
                },
                {
                    "accepted": True,
                    "reason": "accepted",
                    "start_seconds": 33.0,
                    "end_seconds": 36.0,
                    "start_boundary_limited": True,
                    "end_boundary_limited": True,
                },
            ],
        },
    )
    monkeypatch.setattr(
        assessment_module, "estimate_motion_transforms", lambda *_args, **_kwargs: ()
    )
    monkeypatch.setattr(
        assessment_module, "assess_stabilization", lambda *_args, **_kwargs: coarse
    )
    calls: list[tuple[tuple[float, float], ...]] = []

    def refine(
        _source: Path,
        ranges: tuple[tuple[float, float], ...],
        _metadata: VideoMetadata,
        _config: RescueAssessmentConfig,
        _callback: AssessmentCancellation,
    ) -> tuple[tuple[float, NDArray[np.uint8]], ...]:
        calls.append(ranges)
        return tuple(
            (32.0 + index / 24.0, np.zeros((32, 32), dtype=np.uint8))
            for index in range(96)
        )

    def corrections(
        frames: Sequence[tuple[float, NDArray[np.uint8]]],
        *_args: object,
        **_kwargs: object,
    ) -> tuple[MotionTransform, ...]:
        return tuple(
            MotionTransform(
                timestamp_seconds=timestamp,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=2.0 if index % 2 else -2.0,
                translation_y=0.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
                semantics="frame_correction",
            )
            for index, (timestamp, _frame) in enumerate(frames)
        )

    monkeypatch.setattr(assessment_module, "estimate_anchor_corrections", corrections)

    def transition(
        frames: Sequence[tuple[float, NDArray[np.uint8]]],
        _config: StabilizationConfig,
        *,
        transition_range: tuple[float, float],
        following_anchor_corrections: Sequence[MotionTransform],
    ) -> tuple[MotionTransform, ...]:
        assert transition_range == (32.0, 33.0)
        assert len(frames) == 96
        assert len(following_anchor_corrections) == 72
        if transition_outcome == "success":
            return corrections(frames)
        if transition_outcome == "empty":
            return ()
        if transition_outcome == "missing_pts":
            raise ValueError("frame timestamps must be finite")
        if transition_outcome == "inventory":
            raise ValueError("transition candidate frame inventory exceeds its maximum")
        if transition_outcome == "opencv":
            raise cv2.error("OpenCV transition measurement failed")
        if transition_outcome == "cancelled":
            raise RescueCancelledError("cancel transition measurement")
        raise SystemExit("base transition measurement failure")

    monkeypatch.setattr(
        assessment_module, "estimate_transition_anchor_corrections", transition
    )
    service = LocalRescueAssessmentService(
        frame_provider=lambda *_args, **_kwargs: _sampled_frames(),
        loudness_provider=lambda *_args, **_kwargs: LoudnessMeasurement(
            input_i=-20.0,
            input_tp=-3.0,
            input_lra=4.0,
            input_thresh=-30.0,
            target_offset=0.0,
            noise_floor_dbfs=-50.0,
            noise_confidence=0.0,
            noise_event_count=0,
        ),
        motion_refinement_provider=cast(MotionRefinementProvider, refine),
    )
    metadata = _metadata(source).model_copy(
        update={
            "duration_seconds": 42.0,
            "average_frame_rate": 24.0,
            "estimated_frame_count": 1008,
            "has_audio": False,
        }
    )
    base = MediaDamageMap(
        input_hash=sha256(source.read_bytes()).hexdigest(),
        duration_seconds=42.0,
        scan_coverage=((0.0, 42.0),),
    )

    if transition_outcome in {"cancelled", "base_exception"}:
        error = (
            RescueCancelledError if transition_outcome == "cancelled" else SystemExit
        )
        with pytest.raises(error):
            service.assess(
                source,
                base.input_hash,
                metadata,
                base,
                tmp_path / "workspace",
                lambda: False,
            )
        return
    bundle = service.assess(
        source,
        base.input_hash,
        metadata,
        base,
        tmp_path / "workspace",
        lambda: False,
    )

    assert calls == [((32.0, 36.0),)]
    assert bundle.stabilization_assessment is not None
    assert bundle.stabilization_assessment.recommended is True
    transition_success = transition_outcome == "success"
    expected_method = "transition_anchor_v1" if transition_success else "anchor_v1"
    expected_range = [32.0, 36.0] if transition_success else [33.0, 36.0]
    expected_count = 96 if transition_success else 72
    assert bundle.stabilization_assessment.parameters["method"] == expected_method
    assert bundle.stabilization_assessment.parameters["affected_ranges"] == [
        expected_range
    ]
    assert len(bundle.stabilization_assessment.transforms) == expected_count
    assert bundle.stabilization_assessment.parameters["frame_rate_inventory"] == {
        "source_rate_fps": 24.0,
        "frame_count": expected_count,
        "complete": True,
    }
    assert any(
        "excluded frames are not claimed" in limitation.lower()
        for limitation in bundle.limitations
    ) is (not transition_success)
    if transition_outcome not in {"success", "empty"}:
        assert any(
            "transition stabilization evidence was inconclusive" in limitation.lower()
            for limitation in bundle.limitations
        )


def test_single_motion_sample_is_rejected_without_service_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an empty recommended range being converted into a warning."""
    source = tmp_path / "single motion source.mp4"
    source.write_bytes(b"measured source")
    monkeypatch.setattr(
        assessment_module,
        "estimate_motion_transforms",
        lambda *_args, **_kwargs: (
            MotionTransform(
                timestamp_seconds=1.0,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=4.0,
                translation_y=0.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
            ),
        ),
    )

    def unexpected_refinement(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("single inactive sample must not request refinement")

    service = LocalRescueAssessmentService(
        frame_provider=lambda *_args, **_kwargs: _sampled_frames(),
        loudness_provider=lambda *_args, **_kwargs: LoudnessMeasurement(
            input_i=-20.0,
            input_tp=-3.0,
            input_lra=4.0,
            input_thresh=-30.0,
            target_offset=0.0,
            noise_floor_dbfs=-50.0,
            noise_confidence=0.0,
            noise_event_count=0,
        ),
        motion_refinement_provider=unexpected_refinement,
    )
    base = MediaDamageMap(
        input_hash=sha256(source.read_bytes()).hexdigest(),
        duration_seconds=4.0,
        scan_coverage=((0.0, 4.0),),
    )

    bundle = service.assess(
        source,
        base.input_hash,
        _metadata(source),
        base,
        tmp_path / "workspace",
        lambda: False,
    )

    assert bundle.stabilization_assessment is not None
    assert bundle.stabilization_assessment.recommended is False
    assert bundle.stabilization_assessment.reason == "insufficient_active_corrections"
    assert not any(warning.component == "stabilization" for warning in bundle.warnings)
    assert bundle.parameters["motion_refinement_frame_count"] == 0


def test_complete_motion_inventory_preflight_uses_visual_not_motion_sample_cap() -> (
    None
):
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_sample_count=6,
        maximum_motion_refinement_frames=100,
    )

    assert assessment_module._complete_motion_inventory_fits(  # noqa: SLF001
        2.0, 10.0, config
    )
    assert not assessment_module._complete_motion_inventory_fits(  # noqa: SLF001
        12.0, 10.0, config
    )


def test_local_refinement_scene_boundaries_ignore_historical_cuts() -> None:
    boundaries = assessment_module._scene_boundaries_for_inventory(  # noqa: SLF001
        (10.0, 31.0, 34.0, 40.0),
        (32.0, 33.0, 34.0, 35.0, 36.0),
        ((32.0, 36.5),),
    )

    assert boundaries == (34.0,)


def test_disjoint_local_refinement_ranges_do_not_share_an_anchor() -> None:
    """Catches measuring one anchor across an unobserved refinement gap."""
    ranges = ((2.0, 4.0), (20.0, 22.0))
    timestamps = (2.0, 2.5, 3.0, 3.5, 20.0, 20.5, 21.0, 21.5)
    boundaries = assessment_module._scene_boundaries_for_inventory(  # noqa: SLF001
        (), timestamps, ranges
    )
    rng = np.random.default_rng(906)
    texture = rng.integers(0, 256, size=(96, 96), dtype=np.uint8)
    frames = []
    for index, timestamp in enumerate(timestamps):
        frame = np.roll(texture, index % 2, axis=1).copy()
        frame[0, 0] = 10 if timestamp < 4.0 else 240
        frames.append((timestamp, frame))
    estimator_calls: list[tuple[bool, bool]] = []

    def measure(
        left: NDArray[np.uint8], right: NDArray[np.uint8]
    ) -> tuple[float, float, float, float, float, float]:
        estimator_calls.append((bool(left[0, 0] > 128), bool(right[0, 0] > 128)))
        return (0.0, 1.0, 0.0, 0.0, 1.0, 0.0)

    corrections = estimate_anchor_corrections(
        tuple(frames),
        StabilizationConfig(frame_width=96, frame_height=96),
        scene_boundaries=boundaries,
        estimator=measure,
    )

    assert boundaries == (20.0,)
    assert len(corrections) == len(timestamps)
    assert corrections[4].scene_boundary
    assert estimator_calls
    assert all(
        left_is_later == right_is_later
        for left_is_later, right_is_later in estimator_calls
    )


def test_refined_motion_keeps_neutral_timeline_anchors_outside_shake() -> None:
    """Catches rendering a full video with only a local refinement timeline."""
    coarse = StabilizationAssessment(
        recommended=True,
        reason="coarse",
        crop_ratio=0.02,
        transforms=tuple(
            MotionTransform(
                timestamp_seconds=float(timestamp),
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=0.0,
                translation_y=0.0,
                inlier_ratio=1.0,
                residual_pixels=0.0,
                semantics="frame_correction",
            )
            for timestamp in (0.5, 1.0, 2.0, 3.0, 4.0)
        ),
        parameters={"affected_ranges": [[1.0, 3.0]]},
    )
    refined = StabilizationAssessment(
        recommended=True,
        reason="refined",
        crop_ratio=0.03,
        transforms=tuple(
            MotionTransform(
                timestamp_seconds=float(timestamp),
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=0.0,
                translation_y=2.0,
                inlier_ratio=1.0,
                residual_pixels=0.0,
                semantics="frame_correction",
            )
            for timestamp in (1.0, 1.5, 2.0, 2.5, 3.0)
        ),
        parameters={"affected_ranges": [[1.0, 3.0]]},
    )

    merged = assessment_module._merge_stabilization_assessments(  # noqa: SLF001
        coarse, refined, ((1.0, 3.0),)
    )

    timestamps = tuple(item.timestamp_seconds for item in merged.transforms)
    assert timestamps == (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
    assert merged.transforms[0].translation_y == 0.0
    assert merged.transforms[-1].translation_y == 0.0


def test_assessment_contract_is_available_from_public_rescue_package() -> None:
    from videoscope.rescue import (  # noqa: PLC0415
        LocalRescueAssessmentService as PublicService,
    )
    from videoscope.rescue import (
        RescueAssessmentBundle as PublicBundle,  # noqa: PLC0415
    )

    assert PublicService is LocalRescueAssessmentService
    assert PublicBundle.__name__ == "RescueAssessmentBundle"
