"""Shared, bounded CPU assessment service for measured Video Rescue planning."""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from videoscope.domain import VideoMetadata
from videoscope.rescue.audio import (
    AudioAssessment,
    AudioDenoiseConfig,
    FixedOffsetAssessment,
    FixedOffsetConfig,
    LoudnessConfig,
    LoudnessMeasurement,
    assess_audio,
    measure_fixed_av_offset,
)
from videoscope.rescue.errors import RescueCancelledError
from videoscope.rescue.executor import NativeRescueExecutor
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueActionKind,
    make_damage_id,
)
from videoscope.rescue.stabilization import (
    FeatureEstimator,
    StabilizationAssessment,
    StabilizationConfig,
    assess_stabilization,
    estimate_motion_transforms,
)
from videoscope.rescue.visual import (
    FlickerConfig,
    FlickerCorrectionPlan,
    VisualAssessment,
    VisualAssessmentConfig,
    VisualSample,
    assess_visual_samples,
    plan_flicker_correction,
)
from videoscope.scenes import VideoScene, scenes_from_cuts
from videoscope.video.sampling import MAX_FRAME_INDEX_SELECTIONS, sample_frames

AssessmentCancellation = Callable[[], bool]


class _AssessmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RescueAssessmentWarning(_AssessmentModel):
    """One sanitized component failure that cannot become positive evidence."""

    component: str = Field(min_length=1)
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1)


class SyncEventMeasurements(_AssessmentModel):
    """Repeated local A/V events available to the fixed-offset assessor."""

    audio_events: tuple[tuple[float, float], ...]
    video_events: tuple[tuple[float, float], ...]


class RescueAssessmentConfig(_AssessmentModel):
    """Every tunable threshold used by the shared assessment pass."""

    sample_rate: float = Field(default=2.0, gt=0, le=12, allow_inf_nan=False)
    maximum_frame_edge: int = Field(default=640, ge=32, le=1280)
    maximum_sample_count: int = Field(default=120, ge=1, le=MAX_FRAME_INDEX_SELECTIONS)
    scene_cut_difference_threshold: float = Field(
        default=0.22, gt=0, le=1, allow_inf_nan=False
    )
    visual: VisualAssessmentConfig = Field(default_factory=VisualAssessmentConfig)
    flicker: FlickerConfig = Field(default_factory=FlickerConfig)
    loudness: LoudnessConfig = Field(default_factory=LoudnessConfig)
    audio_denoise: AudioDenoiseConfig = Field(default_factory=AudioDenoiseConfig)
    fixed_offset: FixedOffsetConfig = Field(default_factory=FixedOffsetConfig)


class RescueSampledFrames:
    """One shared video decode represented as bounded sampled frame data."""

    def __init__(
        self,
        *,
        visual_samples: tuple[VisualSample, ...],
        motion_frames: tuple[tuple[float, NDArray[np.uint8]], ...],
        scenes: tuple[VideoScene, ...],
        sample_rate: float,
        decode_passes: int,
        truncated: bool = False,
    ) -> None:
        if not visual_samples or len(visual_samples) != len(motion_frames):
            raise ValueError("shared frame samples must be non-empty and aligned")
        if not math.isfinite(sample_rate) or sample_rate <= 0:
            raise ValueError("sample rate must be finite and positive")
        if decode_passes != 1:
            raise ValueError("assessment must use exactly one sampled-frame decode")
        self.visual_samples = visual_samples
        self.motion_frames = motion_frames
        self.scenes = scenes
        self.sample_rate = sample_rate
        self.decode_passes = decode_passes
        self.truncated = truncated


class FrameAssessmentProvider(Protocol):
    def __call__(
        self,
        source: Path,
        workspace: Path,
        metadata: VideoMetadata,
        config: RescueAssessmentConfig,
        cancellation_callback: AssessmentCancellation,
    ) -> RescueSampledFrames: ...


class LoudnessAssessmentProvider(Protocol):
    def __call__(
        self,
        source: Path,
        workspace: Path,
        config: LoudnessConfig,
        cancellation_callback: AssessmentCancellation,
    ) -> LoudnessMeasurement: ...


class SyncAssessmentProvider(Protocol):
    def __call__(
        self,
        source: Path,
        workspace: Path,
        metadata: VideoMetadata,
        cancellation_callback: AssessmentCancellation,
    ) -> SyncEventMeasurements | None: ...


class RescueAssessmentBundle(_AssessmentModel):
    """Typed measured inputs and neutral failures consumed by planning."""

    assessment_version: str = "1"
    visual_assessment: VisualAssessment | None = None
    flicker_correction: FlickerCorrectionPlan | None = None
    stabilization_assessment: StabilizationAssessment | None = None
    audio_assessment: AudioAssessment | None = None
    fixed_offset_assessment: FixedOffsetAssessment | None = None
    evidence_intervals: tuple[DamageInterval, ...] = ()
    warnings: tuple[RescueAssessmentWarning, ...] = ()
    limitations: tuple[str, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    def merge_damage_map(self, base: MediaDamageMap) -> MediaDamageMap:
        """Merge measured evidence by stable ID without weakening base scan facts."""
        intervals = {interval.id: interval for interval in base.intervals}
        intervals.update(
            {interval.id: interval for interval in self.evidence_intervals}
        )
        return MediaDamageMap(
            input_hash=base.input_hash,
            duration_seconds=base.duration_seconds,
            scanner_version=f"{base.scanner_version}+assessment-{self.assessment_version}",
            scan_coverage=base.scan_coverage,
            intervals=tuple(intervals.values()),
        )


class RescueAssessmentService(Protocol):
    def assess(
        self,
        source: Path,
        source_hash: str,
        metadata: VideoMetadata,
        base_damage_map: MediaDamageMap,
        workspace: Path,
        cancellation_callback: AssessmentCancellation,
    ) -> RescueAssessmentBundle: ...


class LocalRescueAssessmentService:
    """Run one shared sampled-frame pass and isolated local CPU assessments."""

    def __init__(
        self,
        *,
        config: RescueAssessmentConfig | None = None,
        frame_provider: FrameAssessmentProvider | None = None,
        loudness_provider: LoudnessAssessmentProvider | None = None,
        sync_provider: SyncAssessmentProvider | None = None,
        motion_estimator: FeatureEstimator | None = None,
    ) -> None:
        self._config = config or RescueAssessmentConfig()
        self._frame_provider = frame_provider or _sample_frames_once
        self._loudness_provider = loudness_provider or _measure_loudness
        self._sync_provider = sync_provider
        self._motion_estimator = motion_estimator

    def assess(
        self,
        source: Path,
        source_hash: str,
        metadata: VideoMetadata,
        base_damage_map: MediaDamageMap,
        workspace: Path,
        cancellation_callback: AssessmentCancellation,
    ) -> RescueAssessmentBundle:
        del base_damage_map
        _check_cancelled(cancellation_callback)
        warnings: list[RescueAssessmentWarning] = []
        limitations: list[str] = []
        frames: RescueSampledFrames | None = None
        try:
            frames = self._frame_provider(
                Path(source),
                Path(workspace),
                metadata,
                self._config,
                cancellation_callback,
            )
        except RescueCancelledError:
            raise
        except Exception as exc:
            for component in ("visual", "flicker", "stabilization"):
                warnings.append(_warning(component, exc))

        visual: VisualAssessment | None = None
        flicker: FlickerCorrectionPlan | None = None
        stabilization: StabilizationAssessment | None = None
        if frames is not None:
            if frames.truncated:
                limitations.append(
                    "Frame assessment used the configured bounded sample limit; "
                    "samples remained distributed across the full source duration."
                )
            try:
                visual = assess_visual_samples(
                    frames.visual_samples, frames.scenes, self._config.visual
                )
                limitations.extend(visual.limitations)
            except RescueCancelledError:
                raise
            except Exception as exc:
                warnings.append(_warning("visual", exc))
            _check_cancelled(cancellation_callback)
            try:
                brightness = tuple(
                    (
                        sample.timestamp_seconds,
                        float(np.mean(np.asarray(sample.luma, dtype=np.float64))),
                    )
                    for sample in frames.visual_samples
                )
                flicker = plan_flicker_correction(
                    brightness, frames.scenes, self._config.flicker
                )
            except RescueCancelledError:
                raise
            except Exception as exc:
                warnings.append(_warning("flicker", exc))
            _check_cancelled(cancellation_callback)
            try:
                first_frame = frames.motion_frames[0][1]
                height, width = first_frame.shape[:2]
                stabilization_config = StabilizationConfig(
                    frame_width=int(width),
                    frame_height=int(height),
                )
                transforms = estimate_motion_transforms(
                    frames.motion_frames,
                    stabilization_config,
                    scene_boundaries=tuple(
                        scene.start_seconds
                        for scene in frames.scenes
                        if scene.start_seconds > 0
                    ),
                    estimator=self._motion_estimator,
                )
                stabilization = assess_stabilization(transforms, stabilization_config)
                if not stabilization.recommended:
                    limitations.append(
                        "Stabilization was omitted because measured motion did not "
                        f"meet the bounded policy ({stabilization.reason})."
                    )
            except RescueCancelledError:
                raise
            except Exception as exc:
                warnings.append(_warning("stabilization", exc))

        audio: AudioAssessment | None = None
        fixed_offset: FixedOffsetAssessment | None = None
        if metadata.has_audio:
            _check_cancelled(cancellation_callback)
            try:
                measurement = self._loudness_provider(
                    Path(source),
                    Path(workspace),
                    self._config.loudness,
                    cancellation_callback,
                )
                audio = assess_audio(
                    measurement.model_dump(mode="python"),
                    self._config.loudness,
                    self._config.audio_denoise,
                )
                limitations.extend(audio.limitations)
            except RescueCancelledError:
                raise
            except Exception as exc:
                warnings.append(_warning("audio", exc))
            _check_cancelled(cancellation_callback)
            if self._sync_provider is None:
                limitations.append(
                    "Fixed A/V offset was not assessed because repeated paired "
                    "events were unavailable."
                )
            else:
                try:
                    events = self._sync_provider(
                        Path(source),
                        Path(workspace),
                        metadata,
                        cancellation_callback,
                    )
                    if events is not None:
                        fixed_offset = measure_fixed_av_offset(
                            events.audio_events,
                            events.video_events,
                            self._config.fixed_offset,
                        )
                        if fixed_offset.offset_seconds is None:
                            limitations.append(
                                "Fixed A/V offset correction was omitted because "
                                f"measurement was inconclusive ({fixed_offset.reason})."
                            )
                except RescueCancelledError:
                    raise
                except Exception as exc:
                    warnings.append(_warning("sync", exc))

        intervals = _assessment_intervals(
            source_hash,
            metadata.duration_seconds,
            frames,
            visual,
            flicker,
            stabilization,
            audio,
            fixed_offset,
        )
        parameters: dict[str, JsonValue] = {
            "sample_rate": self._config.sample_rate,
            "effective_sample_rate": frames.sample_rate if frames else 0.0,
            "sampled_frame_count": len(frames.visual_samples) if frames else 0,
            "sample_limit": self._config.maximum_sample_count,
            "sample_truncated": frames.truncated if frames else False,
            "maximum_frame_edge": self._config.maximum_frame_edge,
            "scene_cut_difference_threshold": (
                self._config.scene_cut_difference_threshold
            ),
            "frame_decode_passes": frames.decode_passes if frames else 0,
            "visual_config": self._config.visual.model_dump(mode="json"),
            "flicker_config": self._config.flicker.model_dump(mode="json"),
            "loudness_config": self._config.loudness.model_dump(mode="json"),
            "audio_denoise_config": self._config.audio_denoise.model_dump(mode="json"),
            "fixed_offset_config": self._config.fixed_offset.model_dump(mode="json"),
        }
        return RescueAssessmentBundle(
            visual_assessment=visual,
            flicker_correction=flicker,
            stabilization_assessment=stabilization,
            audio_assessment=audio,
            fixed_offset_assessment=fixed_offset,
            evidence_intervals=intervals,
            warnings=tuple(warnings),
            limitations=tuple(dict.fromkeys(limitations)),
            parameters=parameters,
        )


def _sample_frames_once(
    source: Path,
    workspace: Path,
    metadata: VideoMetadata,
    config: RescueAssessmentConfig,
    cancellation_callback: AssessmentCancellation,
) -> RescueSampledFrames:
    _check_cancelled(cancellation_callback)
    result = sample_frames(
        source,
        sample_rate=config.sample_rate,
        max_edge=config.maximum_frame_edge,
        image_format="png",
        workspace_parent=workspace,
        max_samples=config.maximum_sample_count,
        timeline_duration_seconds=metadata.duration_seconds,
    )
    timeline_duration = result.timeline_duration_seconds or metadata.duration_seconds
    effective_sample_rate = min(
        config.sample_rate,
        config.maximum_sample_count / timeline_duration,
    )
    visual_samples: list[VisualSample] = []
    motion_frames: list[tuple[float, NDArray[np.uint8]]] = []
    cuts: list[float] = []
    previous: NDArray[np.float64] | None = None
    for sample in result.samples:
        _check_cancelled(cancellation_callback)
        path = result.work_directory / sample.relative_path
        with Image.open(path) as image:
            gray = np.asarray(image.convert("L"), dtype=np.uint8)
        normalized = gray.astype(np.float64) / 255.0
        if previous is not None and float(np.mean(np.abs(normalized - previous))) >= (
            config.scene_cut_difference_threshold
        ):
            cuts.append(sample.timestamp_seconds)
        previous = normalized
        visual_samples.append(
            VisualSample(
                timestamp_seconds=sample.timestamp_seconds,
                luma=tuple(tuple(float(value) for value in row) for row in normalized),
            )
        )
        motion_frames.append((sample.timestamp_seconds, gray))
    scenes = scenes_from_cuts(cuts, duration_seconds=timeline_duration)
    return RescueSampledFrames(
        visual_samples=tuple(visual_samples),
        motion_frames=tuple(motion_frames),
        scenes=scenes,
        sample_rate=effective_sample_rate,
        decode_passes=result.decode_passes,
        truncated=result.truncated,
    )


def _measure_loudness(
    source: Path,
    workspace: Path,
    config: LoudnessConfig,
    cancellation_callback: AssessmentCancellation,
) -> LoudnessMeasurement:
    return NativeRescueExecutor().measure_loudness(
        source, workspace, config, cancellation_callback
    )


def _warning(component: str, error: Exception) -> RescueAssessmentWarning:
    return RescueAssessmentWarning(
        component=component,
        error_type=type(error).__name__,
        message=f"The local {component} assessment was unavailable.",
    )


def _check_cancelled(callback: AssessmentCancellation) -> None:
    if callback():
        raise RescueCancelledError("Rescue assessment was cancelled")


def _assessment_intervals(
    source_hash: str,
    duration: float,
    frames: RescueSampledFrames | None,
    visual: VisualAssessment | None,
    flicker: FlickerCorrectionPlan | None,
    stabilization: StabilizationAssessment | None,
    audio: AudioAssessment | None,
    fixed_offset: FixedOffsetAssessment | None,
) -> tuple[DamageInterval, ...]:
    intervals: list[DamageInterval] = []
    visual_kinds = {
        RescueActionKind.ADJUST_LUMA: DamageKind.DARK,
        RescueActionKind.DENOISE_VIDEO: DamageKind.VIDEO_NOISE,
        RescueActionKind.SHARPEN: DamageKind.SOFT_DETAIL,
    }
    if visual is not None:
        for action in visual.recommended_actions:
            kind = visual_kinds.get(action)
            if kind is None:
                continue
            evidence = tuple(item for item in visual.evidence if item.action is action)
            start, end = _evidence_range(evidence, frames, duration)
            intervals.append(
                _interval(
                    source_hash,
                    "video:0",
                    kind,
                    start,
                    end,
                    "Sampled visual measurements support a preview-only adjustment.",
                    {
                        "metrics": visual.metrics.model_dump(mode="json"),
                        "evidence": [item.model_dump(mode="json") for item in evidence],
                    },
                )
            )
    if flicker is not None:
        for start, end in flicker.intervals:
            intervals.append(
                _interval(
                    source_hash,
                    "video:0",
                    DamageKind.FLICKER,
                    start,
                    min(duration, end),
                    "Repeated scene-internal sampled luma residuals were observed.",
                    {"gain_samples": len(flicker.gains)},
                )
            )
    if stabilization is not None and stabilization.recommended:
        timestamps = tuple(
            item.timestamp_seconds
            for item in stabilization.transforms
            if not item.scene_boundary
        )
        if timestamps:
            intervals.append(
                _interval(
                    source_hash,
                    "video:0",
                    DamageKind.SHAKE,
                    max(0.0, min(timestamps) - _sample_step(frames)),
                    min(duration, max(timestamps) + _sample_step(frames)),
                    "Reliable scene-local affine motion was measured.",
                    {
                        "crop_ratio": stabilization.crop_ratio,
                        "transform_count": len(stabilization.transforms),
                    },
                )
            )
    if audio is not None:
        if RescueActionKind.NORMALIZE_AUDIO in audio.recommended_actions:
            intervals.append(
                _interval(
                    source_hash,
                    "audio:0",
                    DamageKind.LOW_LOUDNESS,
                    0.0,
                    duration,
                    "Measured loudness differs from the configured local target.",
                    audio.measurement.model_dump(mode="json"),
                )
            )
        if RescueActionKind.DENOISE_AUDIO in audio.recommended_actions:
            intervals.append(
                _interval(
                    source_hash,
                    "audio:0",
                    DamageKind.AUDIO_NOISE,
                    0.0,
                    duration,
                    "Repeated confident audio noise measurements were observed.",
                    audio.measurement.model_dump(mode="json"),
                )
            )
        if audio.clipping_detected:
            intervals.append(
                _interval(
                    source_hash,
                    "audio:0",
                    DamageKind.AUDIO_CLIPPING,
                    0.0,
                    duration,
                    "Measured peaks crossed the configured clipping guard.",
                    audio.measurement.model_dump(mode="json"),
                )
            )
    if fixed_offset is not None and fixed_offset.offset_seconds is not None:
        intervals.append(
            _interval(
                source_hash,
                "audio:0",
                DamageKind.FIXED_AV_OFFSET,
                0.0,
                duration,
                "Repeated paired events support one constant A/V offset.",
                fixed_offset.model_dump(mode="json"),
            )
        )
    return tuple(intervals)


def _interval(
    source_hash: str,
    stream_id: str,
    kind: DamageKind,
    start: float,
    end: float,
    description: str,
    measurements: dict[str, JsonValue],
) -> DamageInterval:
    return DamageInterval(
        id=make_damage_id(source_hash, stream_id, kind, start, end),
        stream_id=stream_id,
        kind=kind,
        start_seconds=start,
        end_seconds=end,
        description=description,
        measurements=measurements,
    )


def _sample_step(frames: RescueSampledFrames | None) -> float:
    return 1.0 / frames.sample_rate if frames is not None else 0.0


def _evidence_range(
    evidence: tuple[object, ...],
    frames: RescueSampledFrames | None,
    duration: float,
) -> tuple[float, float]:
    timestamps = tuple(float(getattr(item, "timestamp_seconds")) for item in evidence)
    if not timestamps:
        return 0.0, duration
    step = _sample_step(frames)
    return max(0.0, min(timestamps) - step / 2), min(
        duration, max(timestamps) + step / 2
    )


__all__ = [
    "LocalRescueAssessmentService",
    "RescueAssessmentBundle",
    "RescueAssessmentConfig",
    "RescueAssessmentService",
    "RescueAssessmentWarning",
    "RescueSampledFrames",
    "SyncEventMeasurements",
]
