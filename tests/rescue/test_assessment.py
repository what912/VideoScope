"""Production-style wiring tests for measured Rescue assessments."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import numpy as np

from videoscope.domain import VideoMetadata
from videoscope.rescue.assessment import (
    LocalRescueAssessmentService,
    RescueSampledFrames,
    SyncEventMeasurements,
)
from videoscope.rescue.audio import LoudnessMeasurement
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
from videoscope.rescue.visual import VisualSample
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
    frames: list[np.ndarray] = []
    samples: list[VisualSample] = []
    for index in range(8):
        low = 0.03 if index % 2 == 0 else 0.16
        array = np.fromfunction(
            lambda row, column: low + ((row + column + index) % 2) * 0.05,
            (32, 32),
            dtype=int,
        ).astype(np.float64)
        frames.append((array * 255).astype(np.uint8))
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


def _service(*, motion_error: bool = False) -> LocalRescueAssessmentService:
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
    assert RescueActionKind.STABILIZE not in kinds
    assert "preview_renderer_unavailable" in " ".join(
        preparation.plan.assessment_warnings
    )
    assert preparation.assessments.parameters["frame_decode_passes"] == 1
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


def test_assessment_contract_is_available_from_public_rescue_package() -> None:
    from videoscope.rescue import (  # noqa: PLC0415
        LocalRescueAssessmentService as PublicService,
    )
    from videoscope.rescue import (
        RescueAssessmentBundle as PublicBundle,  # noqa: PLC0415
    )

    assert PublicService is LocalRescueAssessmentService
    assert PublicBundle.__name__ == "RescueAssessmentBundle"
