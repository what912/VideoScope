"""Shared, bounded CPU assessment service for measured Video Rescue planning."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from videoscope.domain import VideoMetadata
from videoscope.rescue.audio import (
    AudioAssessment,
    AudioDenoiseConfig,
    AudioNoiseInterval,
    FixedOffsetAssessment,
    FixedOffsetConfig,
    LoudnessConfig,
    LoudnessMeasurement,
    assess_audio,
    measure_fixed_av_offset,
)
from videoscope.rescue.deblur import (
    BlurKernelEstimate,
    DeblurConfig,
    estimate_blur_kernel,
)
from videoscope.rescue.errors import RescueCancelledError
from videoscope.rescue.executor import (
    ExternalCommandRunner,
    NativeRescueExecutor,
    run_external_command,
)
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
    assess_anchor_corrections,
    assess_stabilization,
    estimate_anchor_corrections,
    estimate_motion_transforms,
    estimate_transition_anchor_corrections,
)
from videoscope.rescue.tonal import InterferenceTone, TonalInterferenceConfig
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
_MOTION_REFINEMENT_TIMEOUT_SECONDS = 120.0
_MOTION_TIMESTAMP_OUTPUT_BYTES = 64 * 1024


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
    maximum_motion_refinement_sample_rate: float = Field(
        default=30.0, gt=2, le=30, allow_inf_nan=False
    )
    maximum_motion_refinement_frames: int = Field(default=240, ge=2, le=1000)
    maximum_motion_refinement_gap_seconds: float = Field(
        default=1.0, gt=0, le=10, allow_inf_nan=False
    )
    scene_cut_difference_threshold: float = Field(
        default=0.22, gt=0, le=1, allow_inf_nan=False
    )
    visual: VisualAssessmentConfig = Field(default_factory=VisualAssessmentConfig)
    flicker: FlickerConfig = Field(default_factory=FlickerConfig)
    loudness: LoudnessConfig = Field(default_factory=LoudnessConfig)
    audio_denoise: AudioDenoiseConfig = Field(default_factory=AudioDenoiseConfig)
    fixed_offset: FixedOffsetConfig = Field(default_factory=FixedOffsetConfig)
    deblur: DeblurConfig = Field(default_factory=DeblurConfig)
    tonal: TonalInterferenceConfig = Field(default_factory=TonalInterferenceConfig)


MotionTimestampProvider = Callable[
    [
        Path,
        tuple[tuple[float, float], ...],
        RescueAssessmentConfig,
        AssessmentCancellation,
    ],
    tuple[float, ...],
]
MotionFrameDecoder = Callable[
    [
        Path,
        tuple[tuple[float, float], ...],
        tuple[float, ...],
        RescueAssessmentConfig,
        AssessmentCancellation,
    ],
    tuple[NDArray[np.uint8], ...],
]


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
        motion_sample_rate: float | None = None,
        motion_inventory_complete: bool = False,
    ) -> None:
        if not visual_samples or not motion_frames:
            raise ValueError("shared frame samples must be non-empty")
        if not math.isfinite(sample_rate) or sample_rate <= 0:
            raise ValueError("sample rate must be finite and positive")
        if decode_passes != 1:
            raise ValueError("assessment must use exactly one sampled-frame decode")
        effective_motion_rate = motion_sample_rate or sample_rate
        if not math.isfinite(effective_motion_rate) or effective_motion_rate <= 0:
            raise ValueError("motion sample rate must be finite and positive")
        self.visual_samples = visual_samples
        self.motion_frames = motion_frames
        self.scenes = scenes
        self.sample_rate = sample_rate
        self.decode_passes = decode_passes
        self.truncated = truncated
        self.motion_sample_rate = effective_motion_rate
        self.motion_inventory_complete = motion_inventory_complete


class FrameAssessmentProvider(Protocol):
    def __call__(
        self,
        source: Path,
        workspace: Path,
        metadata: VideoMetadata,
        config: RescueAssessmentConfig,
        cancellation_callback: AssessmentCancellation,
    ) -> RescueSampledFrames: ...


class MotionRefinementProvider(Protocol):
    def __call__(
        self,
        source: Path,
        ranges: tuple[tuple[float, float], ...],
        metadata: VideoMetadata,
        config: RescueAssessmentConfig,
        cancellation_callback: AssessmentCancellation,
    ) -> tuple[tuple[float, NDArray[np.uint8]], ...]: ...


class LoudnessAssessmentProvider(Protocol):
    def __call__(
        self,
        source: Path,
        workspace: Path,
        config: LoudnessConfig,
        cancellation_callback: AssessmentCancellation,
    ) -> LoudnessMeasurement: ...


class AudioNoiseAssessmentProvider(Protocol):
    def __call__(
        self,
        source: Path,
        workspace: Path,
        config: AudioDenoiseConfig,
        cancellation_callback: AssessmentCancellation,
    ) -> tuple[AudioNoiseInterval, ...]: ...


class SyncAssessmentProvider(Protocol):
    def __call__(
        self,
        source: Path,
        workspace: Path,
        metadata: VideoMetadata,
        cancellation_callback: AssessmentCancellation,
    ) -> SyncEventMeasurements | None: ...


class DeblurAssessmentProvider(Protocol):
    def __call__(
        self,
        frames: Sequence[NDArray[np.uint8]],
        config: DeblurConfig,
    ) -> BlurKernelEstimate | None: ...


class TonalAssessmentProvider(Protocol):
    def __call__(
        self,
        source: Path,
        workspace: Path,
        metadata: VideoMetadata,
        config: TonalInterferenceConfig,
        cancellation_callback: AssessmentCancellation,
    ) -> tuple[InterferenceTone, ...]: ...


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
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        frame_provider: FrameAssessmentProvider | None = None,
        loudness_provider: LoudnessAssessmentProvider | None = None,
        audio_noise_provider: AudioNoiseAssessmentProvider | None = None,
        sync_provider: SyncAssessmentProvider | None = None,
        motion_estimator: FeatureEstimator | None = None,
        motion_refinement_provider: MotionRefinementProvider | None = None,
        deblur_estimator: DeblurAssessmentProvider | None = None,
        tonal_provider: TonalAssessmentProvider | None = None,
    ) -> None:
        self._config = config or RescueAssessmentConfig()
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._frame_provider = frame_provider or (
            lambda source, workspace, metadata, cfg, callback: _sample_frames_once(
                source,
                workspace,
                metadata,
                cfg,
                callback,
                ffmpeg=self._ffmpeg,
                ffprobe=self._ffprobe,
            )
        )
        self._loudness_provider = loudness_provider or (
            lambda source, workspace, cfg, callback: _measure_loudness(
                source,
                workspace,
                cfg,
                callback,
                ffmpeg=self._ffmpeg,
                ffprobe=self._ffprobe,
            )
        )
        # Tests and third-party integrations that inject a complete loudness
        # measurement must not unexpectedly invoke FFmpeg a second time.  Native
        # operation still enables the independent interval detector by default.
        if audio_noise_provider is not None:
            self._audio_noise_provider = audio_noise_provider
        elif loudness_provider is not None:
            self._audio_noise_provider = _no_audio_noise
        else:
            self._audio_noise_provider = lambda source, workspace, cfg, callback: (
                _measure_audio_noise(
                    source,
                    workspace,
                    cfg,
                    callback,
                    ffmpeg=self._ffmpeg,
                    ffprobe=self._ffprobe,
                )
            )
        self._sync_provider = sync_provider
        self._motion_estimator = motion_estimator
        self._motion_refinement_provider = motion_refinement_provider
        self._deblur_estimator = deblur_estimator or estimate_blur_kernel
        if tonal_provider is not None:
            self._tonal_provider = tonal_provider
        elif loudness_provider is not None:
            self._tonal_provider = _no_tonal_interference
        else:
            self._tonal_provider = lambda source, workspace, metadata, cfg, callback: (
                _measure_tonal_interference(
                    source,
                    workspace,
                    metadata,
                    cfg,
                    callback,
                    ffmpeg=self._ffmpeg,
                    ffprobe=self._ffprobe,
                )
            )

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
        deblur_measurements: list[JsonValue] = []
        tonal_measurements: list[JsonValue] = []
        motion_refinement_frame_count = 0
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
                    source_rate_cap_fps=min(
                        metadata.average_frame_rate,
                        self._config.maximum_motion_refinement_sample_rate,
                    ),
                    maximum_frame_inventory=(
                        self._config.maximum_motion_refinement_frames
                    ),
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
                coarse_run_assessments = stabilization.parameters.get("run_assessments")
                rejected_crop_runs = (
                    tuple(
                        item
                        for item in coarse_run_assessments
                        if isinstance(item, dict)
                        and item.get("accepted") is False
                        and item.get("reason") == "crop_budget_exceeded"
                    )
                    if isinstance(coarse_run_assessments, list)
                    else ()
                )
                if stabilization.recommended and rejected_crop_runs:
                    limitations.append(
                        "Some coarse motion runs were omitted because their required "
                        "crop exceeded the configured crop budget."
                    )
                boundary_limited_runs = (
                    tuple(
                        item
                        for item in coarse_run_assessments
                        if isinstance(item, dict)
                        and item.get("accepted") is True
                        and (
                            item.get("start_boundary_limited") is True
                            or item.get("end_boundary_limited") is True
                        )
                    )
                    if isinstance(coarse_run_assessments, list)
                    else ()
                )
                boundary_limitation = (
                    "Stabilization ranges exclude observed low-confidence or "
                    "scene-boundary transition frames; excluded frames are not "
                    "claimed as corrected."
                )
                if stabilization.recommended and boundary_limited_runs:
                    limitations.append(boundary_limitation)
                if stabilization.recommended:
                    refinement_ranges = tuple(
                        (max(0.0, start), min(metadata.duration_seconds, end))
                        for start, end in _stabilization_ranges(stabilization)
                        if min(metadata.duration_seconds, end) > max(0.0, start)
                    )
                    transition_candidate = _transition_anchor_candidate(
                        coarse_run_assessments,
                        refinement_ranges,
                        stabilization_config,
                    )
                    provider_ranges = (
                        (
                            (
                                transition_candidate[0][0],
                                transition_candidate[1][1],
                            ),
                        )
                        if transition_candidate is not None
                        else refinement_ranges
                    )
                    if self._motion_refinement_provider is not None:
                        refined_frames = self._motion_refinement_provider(
                            Path(source),
                            provider_ranges,
                            metadata,
                            self._config,
                            cancellation_callback,
                        )
                    elif frames.motion_inventory_complete:
                        refined_frames = tuple(
                            item
                            for item in frames.motion_frames
                            if any(
                                start <= item[0] < end for start, end in provider_ranges
                            )
                        )
                    else:
                        refined_frames = _sample_motion_ranges(
                            Path(source),
                            provider_ranges,
                            metadata,
                            self._config,
                            cancellation_callback,
                            ffmpeg=self._ffmpeg,
                            ffprobe=self._ffprobe,
                        )
                    motion_refinement_frame_count = len(refined_frames)
                    anchor_frames = tuple(
                        item
                        for item in refined_frames
                        if any(
                            start <= item[0] < end for start, end in refinement_ranges
                        )
                    )
                    refined_scene_boundaries = _scene_boundaries_for_inventory(
                        tuple(
                            scene.start_seconds
                            for scene in frames.scenes
                            if scene.start_seconds > 0
                        ),
                        tuple(timestamp for timestamp, _frame in anchor_frames),
                        refinement_ranges,
                    )
                    refined_corrections = estimate_anchor_corrections(
                        anchor_frames,
                        stabilization_config,
                        scene_boundaries=refined_scene_boundaries,
                        estimator=self._motion_estimator,
                    )
                    refined_stabilization = assess_anchor_corrections(
                        refined_corrections,
                        stabilization_config,
                        affected_ranges=refinement_ranges,
                    )
                    if refined_stabilization.recommended:
                        if transition_candidate is not None:
                            transition_range, anchor_range = transition_candidate
                            try:
                                transition_corrections = (
                                    estimate_transition_anchor_corrections(
                                        refined_frames,
                                        stabilization_config,
                                        transition_range=transition_range,
                                        following_anchor_corrections=(
                                            refined_stabilization.transforms
                                        ),
                                    )
                                )
                            except RescueCancelledError:
                                raise
                            except Exception:
                                transition_corrections = ()
                                limitations.append(
                                    "Transition stabilization evidence was "
                                    "inconclusive; the accepted source-rate "
                                    "anchor range was retained."
                                )
                            if transition_corrections:
                                union_range = (
                                    transition_range[0],
                                    anchor_range[1],
                                )
                                transition_stabilization = assess_anchor_corrections(
                                    transition_corrections,
                                    stabilization_config,
                                    affected_ranges=(union_range,),
                                )
                                if transition_stabilization.recommended:
                                    transition_parameters = dict(
                                        transition_stabilization.parameters
                                    )
                                    transition_parameters.update(
                                        {
                                            "algorithm_version": (
                                                "transition_anchor_v1"
                                            ),
                                            "transition_range": [
                                                transition_range[0],
                                                transition_range[1],
                                            ],
                                            "following_anchor_range": [
                                                anchor_range[0],
                                                anchor_range[1],
                                            ],
                                            "transition_correction_count": len(
                                                transition_corrections
                                            ),
                                        }
                                    )
                                    refined_stabilization = (
                                        transition_stabilization.model_copy(
                                            update={
                                                "reason": (
                                                    "measured_transition_anchor_motion"
                                                ),
                                                "parameters": transition_parameters,
                                            }
                                        )
                                    )
                                    if boundary_limitation in limitations:
                                        limitations.remove(boundary_limitation)
                        refined_parameters = dict(refined_stabilization.parameters)
                        if coarse_run_assessments is not None:
                            refined_parameters["coarse_run_assessments"] = (
                                coarse_run_assessments
                            )
                        stabilization = refined_stabilization.model_copy(
                            update={"parameters": refined_parameters}
                        )
                    else:
                        stabilization = refined_stabilization
                        limitations.append(
                            "Stabilization was omitted because the source-rate "
                            "scene-anchor "
                            "measurement was inconclusive."
                        )
                if stabilization.recommended:
                    accepted_ranges = _stabilization_ranges(stabilization)
                    render_config = StabilizationConfig.model_validate(
                        {
                            **stabilization_config.model_dump(mode="python"),
                            "accepted_ranges": accepted_ranges,
                        }
                    )
                    stabilization_parameters = dict(stabilization.parameters)
                    estimator_algorithm = stabilization_parameters.get(
                        "algorithm_version"
                    )
                    transition_method = estimator_algorithm == "transition_anchor_v1"
                    correction_inventory_count = (
                        len(refined_frames) if transition_method else len(anchor_frames)
                    )
                    stabilization_parameters.update(
                        {
                            "method": (
                                "transition_anchor_v1"
                                if transition_method
                                else "anchor_v1"
                            ),
                            "algorithm_version": "1",
                            "estimator_algorithm_version": estimator_algorithm,
                            "config": render_config.model_dump(mode="json"),
                            "frame_rate_inventory": {
                                "source_rate_fps": min(
                                    metadata.average_frame_rate,
                                    self._config.maximum_motion_refinement_sample_rate,
                                ),
                                "frame_count": len(stabilization.transforms),
                                "complete": bool(
                                    stabilization.transforms
                                    and len(stabilization.transforms)
                                    == correction_inventory_count
                                    and all(
                                        transform.semantics == "frame_correction"
                                        for transform in stabilization.transforms
                                    )
                                ),
                            },
                        }
                    )
                    stabilization = stabilization.model_copy(
                        update={"parameters": stabilization_parameters}
                    )
                if not stabilization.recommended:
                    limitations.append(
                        "Stabilization was omitted because measured motion did not "
                        f"meet the bounded policy ({stabilization.reason})."
                    )
            except RescueCancelledError:
                raise
            except Exception as exc:
                warnings.append(_warning("stabilization", exc))

            if visual is not None:
                for interval in visual.action_intervals:
                    if interval.action is not RescueActionKind.SHARPEN:
                        continue
                    measured_frames = tuple(
                        frame
                        for timestamp, frame in frames.motion_frames
                        if interval.start_seconds <= timestamp < interval.end_seconds
                    )
                    if not measured_frames:
                        limitations.append(
                            "Deblur was omitted because measured source frames were "
                            "unavailable for one soft-detail interval."
                        )
                        continue
                    try:
                        estimate = self._deblur_estimator(
                            measured_frames, self._config.deblur
                        )
                    except RescueCancelledError:
                        raise
                    except Exception as exc:
                        warnings.append(_warning("deblur", exc))
                        continue
                    if estimate is None:
                        limitations.append(
                            "Deblur was omitted because the measured soft-detail "
                            "interval did not pass all conservative acceptance gates."
                        )
                        continue
                    deblur_measurements.append(
                        {
                            "algorithm_version": "1",
                            "source_ranges": [
                                [interval.start_seconds, interval.end_seconds]
                            ],
                            "estimate": estimate.model_dump(mode="json"),
                            "config": self._config.deblur.model_dump(mode="json"),
                        }
                    )

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
                try:
                    noise_intervals = self._audio_noise_provider(
                        Path(source),
                        Path(workspace),
                        self._config.audio_denoise,
                        cancellation_callback,
                    )
                except RescueCancelledError:
                    raise
                except Exception as exc:
                    noise_intervals = ()
                    warnings.append(_warning("audio_noise", exc))
                if noise_intervals:
                    reliable = tuple(
                        interval
                        for interval in noise_intervals
                        if float(getattr(interval, "confidence"))
                        >= self._config.audio_denoise.minimum_confidence
                    )
                    measurement = measurement.model_copy(
                        update={
                            "noise_floor_dbfs": (
                                float(
                                    np.median(
                                        [
                                            float(getattr(item, "rms_dbfs"))
                                            for item in reliable
                                        ]
                                    )
                                )
                                if reliable
                                else None
                            ),
                            "noise_confidence": (
                                float(
                                    np.median(
                                        [
                                            float(getattr(item, "confidence"))
                                            for item in reliable
                                        ]
                                    )
                                )
                                if reliable
                                else 0.0
                            ),
                            "noise_event_count": sum(
                                max(
                                    1,
                                    round(
                                        (item.end_seconds - item.start_seconds)
                                        / (
                                            self._config.audio_denoise.analysis_window_seconds
                                        )
                                    ),
                                )
                                for item in reliable
                            ),
                            "noise_intervals": reliable,
                        }
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
            try:
                tones = self._tonal_provider(
                    Path(source),
                    Path(workspace),
                    metadata,
                    self._config.tonal,
                    cancellation_callback,
                )
                accepted_tones = tuple(
                    tone
                    for tone in tones
                    if tone.confidence >= self._config.tonal.minimum_confidence
                    and tone.render_qualification is not None
                )
                unqualified_tones = tuple(
                    tone
                    for tone in tones
                    if tone.confidence >= self._config.tonal.minimum_confidence
                    and tone.render_qualification is None
                )
                if accepted_tones:
                    tonal_measurements.append(
                        {
                            "algorithm_version": "1",
                            "source_ranges": [
                                [tone.start_seconds, tone.end_seconds]
                                for tone in accepted_tones
                            ],
                            "interference_profiles": [
                                tone.model_dump(mode="json") for tone in accepted_tones
                            ],
                            "config": self._config.tonal.model_dump(mode="json"),
                        }
                    )
                if unqualified_tones:
                    limitations.append(
                        "Tonal interference reduction was omitted for measured "
                        "profiles without one renderer passing every complete "
                        "50 ms target, preservation, and boundary gate."
                    )
                elif tones and not accepted_tones:
                    limitations.append(
                        "Tonal interference reduction was omitted because measured "
                        "confidence did not pass the configured gate."
                    )
            except RescueCancelledError:
                raise
            except Exception as exc:
                warnings.append(_warning("tonal_interference", exc))
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
        intervals = _merge_interval_evidence(
            intervals,
            _perceptual_measurement_intervals(
                source_hash,
                deblur_measurements,
                tonal_measurements,
            ),
        )
        parameters: dict[str, JsonValue] = {
            "sample_rate": self._config.sample_rate,
            "effective_sample_rate": frames.sample_rate if frames else 0.0,
            "sampled_frame_count": len(frames.visual_samples) if frames else 0,
            "sample_limit": self._config.maximum_sample_count,
            "motion_refinement_sample_rate": (
                min(
                    metadata.average_frame_rate,
                    self._config.maximum_motion_refinement_sample_rate,
                )
                if motion_refinement_frame_count
                else 0.0
            ),
            "motion_refinement_frame_count": motion_refinement_frame_count,
            "frame_decode_passes": (
                (frames.decode_passes if frames else 0)
                + (
                    1
                    if motion_refinement_frame_count
                    and (
                        self._motion_refinement_provider is not None
                        or (frames is not None and not frames.motion_inventory_complete)
                    )
                    else 0
                )
            ),
            "sample_truncated": frames.truncated if frames else False,
            "maximum_frame_edge": self._config.maximum_frame_edge,
            "scene_cut_difference_threshold": (
                self._config.scene_cut_difference_threshold
            ),
            "visual_config": self._config.visual.model_dump(mode="json"),
            "flicker_config": self._config.flicker.model_dump(mode="json"),
            "loudness_config": self._config.loudness.model_dump(mode="json"),
            "audio_denoise_config": self._config.audio_denoise.model_dump(mode="json"),
            "fixed_offset_config": self._config.fixed_offset.model_dump(mode="json"),
            "deblur_config": self._config.deblur.model_dump(mode="json"),
            "tonal_config": self._config.tonal.model_dump(mode="json"),
            "deblur_measurements": deblur_measurements,
            "tonal_interference_measurements": tonal_measurements,
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
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> RescueSampledFrames:
    _check_cancelled(cancellation_callback)
    motion_sample_rate = min(
        metadata.average_frame_rate,
        config.maximum_motion_refinement_sample_rate,
    )
    motion_inventory_preflight = _complete_motion_inventory_fits(
        metadata.duration_seconds, motion_sample_rate, config
    )
    result = sample_frames(
        source,
        sample_rate=config.sample_rate,
        max_edge=config.maximum_frame_edge,
        image_format="png",
        workspace_parent=workspace,
        max_samples=config.maximum_sample_count,
        timeline_duration_seconds=metadata.duration_seconds,
        motion_sample_rate=(motion_sample_rate if motion_inventory_preflight else None),
        maximum_motion_samples=(
            config.maximum_motion_refinement_frames
            if motion_inventory_preflight
            else None
        ),
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        cancellation_check=lambda: _check_cancelled(cancellation_callback),
    )
    timeline_duration = result.timeline_duration_seconds or metadata.duration_seconds
    effective_sample_rate = min(
        config.sample_rate,
        config.maximum_sample_count / timeline_duration,
    )
    requested_visual_count = max(
        1, math.ceil(timeline_duration * config.sample_rate - 1e-9)
    )
    visual_count = min(config.maximum_sample_count, requested_visual_count)
    visual_targets = (
        (0.0,)
        if visual_count == 1
        else (
            tuple(
                position * timeline_duration / (visual_count - 1)
                for position in range(visual_count)
            )
            if requested_visual_count > config.maximum_sample_count
            else tuple(
                position / config.sample_rate for position in range(visual_count)
            )
        )
    )
    decoded: list[tuple[float, NDArray[np.uint8], NDArray[np.float64]]] = []
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
        decoded.append((sample.timestamp_seconds, gray, normalized))
    motion_frames: list[tuple[float, NDArray[np.uint8]]] = []
    motion_source_samples = (
        result.motion_samples if motion_inventory_preflight else result.samples
    )
    for sample in motion_source_samples:
        _check_cancelled(cancellation_callback)
        path = result.work_directory / sample.relative_path
        with Image.open(path) as image:
            gray = np.asarray(image.convert("L"), dtype=np.uint8)
        motion_frames.append((sample.timestamp_seconds, gray))
    visual_samples = tuple(
        VisualSample(
            timestamp_seconds=selected[0],
            luma=tuple(tuple(float(value) for value in row) for row in selected[2]),
        )
        for target in visual_targets
        for selected in (
            min(decoded, key=lambda item: (abs(item[0] - target), item[0])),
        )
    )
    scenes = scenes_from_cuts(cuts, duration_seconds=timeline_duration)
    return RescueSampledFrames(
        visual_samples=visual_samples,
        motion_frames=tuple(motion_frames),
        scenes=scenes,
        sample_rate=effective_sample_rate,
        decode_passes=result.decode_passes,
        truncated=requested_visual_count > config.maximum_sample_count,
        motion_sample_rate=(
            motion_sample_rate if motion_inventory_preflight else effective_sample_rate
        ),
        motion_inventory_complete=(
            motion_inventory_preflight and not result.motion_truncated
        ),
    )


def _complete_motion_inventory_fits(
    duration_seconds: float,
    motion_sample_rate: float,
    config: RescueAssessmentConfig,
) -> bool:
    """Preflight a complete source-rate inventory against its dedicated bound."""
    requested_motion_count = max(
        1, math.ceil(duration_seconds * motion_sample_rate - 1e-9)
    )
    return requested_motion_count <= config.maximum_motion_refinement_frames


def _probe_motion_range_timestamps(
    source: Path,
    ranges: tuple[tuple[float, float], ...],
    config: RescueAssessmentConfig,
    cancellation_callback: AssessmentCancellation,
    *,
    ffprobe: str,
    runner: ExternalCommandRunner,
) -> tuple[float, ...]:
    """Read bounded actual frame PTS and normalize only by measured stream start."""
    origin_result = runner(
        (
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=start_time",
            "-of",
            "compact=p=1:nk=1",
            str(source),
        ),
        timeout_seconds=_MOTION_REFINEMENT_TIMEOUT_SECONDS,
        sensitive_paths=(source,),
        cancellation_callback=cancellation_callback,
    )
    if origin_result.returncode != 0:
        raise ValueError("motion refinement stream-start probe failed")
    origin_lines = origin_result.stdout_summary.splitlines()
    if len(origin_lines) != 1 or not origin_lines[0].startswith("stream|"):
        raise ValueError("motion refinement stream start is missing")
    try:
        origin = float(origin_lines[0].removeprefix("stream|"))
    except ValueError as exc:
        raise ValueError("motion refinement stream start is invalid") from exc
    if not math.isfinite(origin):
        raise ValueError("motion refinement stream start is invalid")

    normalized: list[float] = []
    for start, end in ranges:
        raw_start, raw_end = origin + start, origin + end
        result = runner(
            (
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                f"{raw_start:.17g}%{raw_end:.17g}",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "compact=p=1:nk=1",
                str(source),
            ),
            timeout_seconds=_MOTION_REFINEMENT_TIMEOUT_SECONDS,
            sensitive_paths=(source,),
            cancellation_callback=cancellation_callback,
        )
        stdout = result.stdout_summary
        if (
            result.returncode != 0
            or not stdout
            or not stdout.endswith(("\n", "\r"))
            or len(stdout.encode("utf-8")) > _MOTION_TIMESTAMP_OUTPUT_BYTES
        ):
            raise ValueError("motion refinement frame PTS probe failed")
        for line in stdout.splitlines():
            if not line.startswith("frame|"):
                raise ValueError("motion refinement frame PTS inventory is malformed")
            try:
                timestamp = float(line.removeprefix("frame|")) - origin
            except ValueError as exc:
                raise ValueError(
                    "motion refinement frame PTS inventory is malformed"
                ) from exc
            if not math.isfinite(timestamp) or timestamp < start - 1e-6:
                raise ValueError("motion refinement frame PTS escaped its range")
            if timestamp >= end:
                continue
            normalized.append(timestamp)
            if len(normalized) > config.maximum_motion_refinement_frames:
                raise ValueError(
                    "motion refinement frame inventory exceeds the configured maximum"
                )
    return tuple(normalized)


def _decode_motion_range_frames(
    source: Path,
    ranges: tuple[tuple[float, float], ...],
    timestamps: tuple[float, ...],
    config: RescueAssessmentConfig,
    cancellation_callback: AssessmentCancellation,
    *,
    ffmpeg: str,
    runner: ExternalCommandRunner,
) -> tuple[NDArray[np.uint8], ...]:
    """Decode each requested interval once and bind it to the probed PTS count."""
    frames: list[NDArray[np.uint8]] = []
    with tempfile.TemporaryDirectory(prefix="videoscope-motion-refine-") as raw:
        root = Path(raw)
        for range_index, (start, end) in enumerate(ranges):
            expected = sum(1 for value in timestamps if start <= value < end)
            if expected < 1:
                raise ValueError("motion refinement range has no actual PTS")
            output_pattern = root / f"range-{range_index:03d}-%06d.png"
            scale = (
                f"scale=w='min(iw,{config.maximum_frame_edge})':"
                f"h='min(ih,{config.maximum_frame_edge})':"
                "force_original_aspect_ratio=decrease"
            )
            result = runner(
                (
                    ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-ss",
                    f"{start:.17g}",
                    "-to",
                    f"{end:.17g}",
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-fps_mode",
                    "passthrough",
                    "-vf",
                    scale,
                    "-frames:v",
                    str(expected + 1),
                    str(output_pattern),
                ),
                timeout_seconds=_MOTION_REFINEMENT_TIMEOUT_SECONDS,
                sensitive_paths=(source, root),
                cancellation_callback=cancellation_callback,
            )
            if result.returncode != 0:
                raise ValueError("motion refinement bounded decode failed")
            decoded_paths = sorted(root.glob(f"range-{range_index:03d}-*.png"))
            if len(decoded_paths) != expected:
                raise ValueError(
                    "motion refinement decoded inventory does not match actual PTS "
                    "inventory"
                )
            for path in decoded_paths:
                _check_cancelled(cancellation_callback)
                with Image.open(path) as image:
                    gray = np.asarray(image.convert("L"), dtype=np.uint8)
                frames.append(gray)
    return tuple(frames)


def _sample_motion_ranges(
    source: Path,
    ranges: tuple[tuple[float, float], ...],
    metadata: VideoMetadata,
    config: RescueAssessmentConfig,
    cancellation_callback: AssessmentCancellation,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    runner: ExternalCommandRunner = run_external_command,
    timestamp_provider: MotionTimestampProvider | None = None,
    frame_decoder: MotionFrameDecoder | None = None,
) -> tuple[tuple[float, NDArray[np.uint8]], ...]:
    """Pair bounded decoded frames with measured, never inferred, source PTS."""
    source_rate = min(
        metadata.average_frame_rate,
        config.maximum_motion_refinement_sample_rate,
    )
    if not math.isfinite(source_rate) or source_rate <= 0:
        raise ValueError("motion refinement source rate is invalid")
    estimated_count = sum(
        math.ceil((end * source_rate) - 1e-9) - math.ceil((start * source_rate) - 1e-9)
        for start, end in ranges
    )
    if estimated_count > config.maximum_motion_refinement_frames:
        raise ValueError(
            "motion refinement frame inventory exceeds the configured maximum"
        )
    _check_cancelled(cancellation_callback)
    provide_timestamps = timestamp_provider or (
        lambda path, requested, cfg, callback: _probe_motion_range_timestamps(
            path,
            requested,
            cfg,
            callback,
            ffprobe=ffprobe,
            runner=runner,
        )
    )
    decode_frames = frame_decoder or (
        lambda path, requested, pts, cfg, callback: _decode_motion_range_frames(
            path,
            requested,
            pts,
            cfg,
            callback,
            ffmpeg=ffmpeg,
            runner=runner,
        )
    )
    timestamps = tuple(
        float(value)
        for value in provide_timestamps(source, ranges, config, cancellation_callback)
    )
    if (
        not timestamps
        or len(timestamps) > config.maximum_motion_refinement_frames
        or any(not math.isfinite(value) or value < 0 for value in timestamps)
        or any(
            current <= previous
            for previous, current in zip(timestamps, timestamps[1:], strict=False)
        )
    ):
        raise ValueError("motion refinement actual PTS inventory is invalid")
    if any(
        not any(start <= timestamp < end for start, end in ranges)
        for timestamp in timestamps
    ):
        raise ValueError("motion refinement actual PTS are outside requested ranges")
    for start, end in ranges:
        local = tuple(value for value in timestamps if start <= value < end)
        if not local:
            raise ValueError("motion refinement range has no actual PTS")
        if any(
            current - previous > config.maximum_motion_refinement_gap_seconds
            for previous, current in zip(local, local[1:], strict=False)
        ):
            raise ValueError("motion refinement actual PTS contain a timestamp gap")
    decoded = decode_frames(source, ranges, timestamps, config, cancellation_callback)
    if len(decoded) != len(timestamps):
        raise ValueError(
            "motion refinement decoded inventory does not match actual PTS inventory"
        )
    result: list[tuple[float, NDArray[np.uint8]]] = []
    for timestamp, frame in zip(timestamps, decoded, strict=True):
        _check_cancelled(cancellation_callback)
        gray = np.asarray(frame, dtype=np.uint8)
        if gray.ndim != 2 or gray.size == 0 or not np.isfinite(gray).all():
            raise ValueError("motion refinement decoded frame is invalid")
        if max(gray.shape) > config.maximum_frame_edge:
            raise ValueError("motion refinement decoded frame exceeds its pixel bound")
        result.append((timestamp, gray))
    return tuple(result)


def _scene_boundaries_for_inventory(
    scene_boundaries: tuple[float, ...],
    inventory_timestamps: tuple[float, ...],
    refinement_ranges: tuple[tuple[float, float], ...],
) -> tuple[float, ...]:
    """Split local inventory at known cuts and every unobserved range gap."""
    if not inventory_timestamps:
        return ()
    first, last = inventory_timestamps[0], inventory_timestamps[-1]
    boundaries = {
        float(boundary)
        for boundary in scene_boundaries
        if math.isfinite(boundary) and first < boundary <= last
    }
    ordered_ranges = sorted(
        {
            (float(start), float(end))
            for start, end in refinement_ranges
            if math.isfinite(start) and math.isfinite(end) and start < end
        }
    )
    if ordered_ranges:
        previous_end = ordered_ranges[0][1]
        for start, end in ordered_ranges[1:]:
            if start > previous_end and first < start <= last:
                boundaries.add(start)
            previous_end = max(previous_end, end)
    return tuple(sorted(boundaries))


def _measure_loudness(
    source: Path,
    workspace: Path,
    config: LoudnessConfig,
    cancellation_callback: AssessmentCancellation,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> LoudnessMeasurement:
    return NativeRescueExecutor(ffmpeg=ffmpeg, ffprobe=ffprobe).measure_loudness(
        source, workspace, config, cancellation_callback
    )


def _measure_audio_noise(
    source: Path,
    workspace: Path,
    config: AudioDenoiseConfig,
    cancellation_callback: AssessmentCancellation,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> tuple[AudioNoiseInterval, ...]:
    result = NativeRescueExecutor(ffmpeg=ffmpeg, ffprobe=ffprobe).measure_audio_noise(
        source, workspace, config, cancellation_callback
    )
    return tuple(result)


def _measure_tonal_interference(
    source: Path,
    workspace: Path,
    metadata: VideoMetadata,
    config: TonalInterferenceConfig,
    cancellation_callback: AssessmentCancellation,
    *,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
) -> tuple[InterferenceTone, ...]:
    return NativeRescueExecutor(
        ffmpeg=ffmpeg, ffprobe=ffprobe
    ).measure_tonal_interference(
        source,
        workspace,
        metadata,
        config,
        cancellation_callback,
    )


def _no_audio_noise(
    source: Path,
    workspace: Path,
    config: AudioDenoiseConfig,
    cancellation_callback: AssessmentCancellation,
) -> tuple[AudioNoiseInterval, ...]:
    del source, workspace, config, cancellation_callback
    return ()


def _no_tonal_interference(
    source: Path,
    workspace: Path,
    metadata: VideoMetadata,
    config: TonalInterferenceConfig,
    cancellation_callback: AssessmentCancellation,
) -> tuple[InterferenceTone, ...]:
    del source, workspace, metadata, config, cancellation_callback
    return ()


def _warning(component: str, error: Exception) -> RescueAssessmentWarning:
    return RescueAssessmentWarning(
        component=component,
        error_type=type(error).__name__,
        message=f"The local {component} assessment was unavailable.",
    )


def _check_cancelled(callback: AssessmentCancellation) -> None:
    if callback():
        raise RescueCancelledError("Rescue assessment was cancelled")


def _merge_interval_evidence(
    existing: tuple[DamageInterval, ...],
    measured: tuple[DamageInterval, ...],
) -> tuple[DamageInterval, ...]:
    by_id = {interval.id: interval for interval in existing}
    by_id.update({interval.id: interval for interval in measured})
    return tuple(by_id.values())


def _perceptual_measurement_intervals(
    source_hash: str,
    deblur_measurements: Sequence[JsonValue],
    tonal_measurements: Sequence[JsonValue],
) -> tuple[DamageInterval, ...]:
    intervals: list[DamageInterval] = []
    for profile in deblur_measurements:
        if not isinstance(profile, dict):
            continue
        raw_ranges = profile.get("source_ranges")
        if not isinstance(raw_ranges, list):
            continue
        for raw_range in raw_ranges:
            if not isinstance(raw_range, list) or len(raw_range) != 2:
                continue
            raw_start, raw_end = raw_range
            if (
                isinstance(raw_start, bool)
                or isinstance(raw_end, bool)
                or not isinstance(raw_start, (int, float))
                or not isinstance(raw_end, (int, float))
            ):
                continue
            start, end = float(raw_start), float(raw_end)
            intervals.append(
                DamageInterval(
                    id=make_damage_id(
                        source_hash, "video:0", DamageKind.SOFT_DETAIL, start, end
                    ),
                    stream_id="video:0",
                    kind=DamageKind.SOFT_DETAIL,
                    start_seconds=start,
                    end_seconds=end,
                    description=(
                        "Persistent local soft detail passed bounded deblur "
                        "measurement gates."
                    ),
                    measurements=profile,
                )
            )
    for profile in tonal_measurements:
        if not isinstance(profile, dict):
            continue
        raw_ranges = profile.get("source_ranges")
        if not isinstance(raw_ranges, list):
            continue
        for raw_range in raw_ranges:
            if not isinstance(raw_range, list) or len(raw_range) != 2:
                continue
            raw_start, raw_end = raw_range
            if (
                isinstance(raw_start, bool)
                or isinstance(raw_end, bool)
                or not isinstance(raw_start, (int, float))
                or not isinstance(raw_end, (int, float))
            ):
                continue
            start, end = float(raw_start), float(raw_end)
            intervals.append(
                DamageInterval(
                    id=make_damage_id(
                        source_hash, "audio:0", DamageKind.AUDIO_NOISE, start, end
                    ),
                    stream_id="audio:0",
                    kind=DamageKind.AUDIO_NOISE,
                    start_seconds=start,
                    end_seconds=end,
                    description=(
                        "Local narrowband interference passed bounded spectral "
                        "measurement gates."
                    ),
                    measurements=profile,
                )
            )
    return tuple(intervals)


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
            measured_ranges = tuple(
                (item.start_seconds, min(duration, item.end_seconds))
                for item in visual.action_intervals
                if item.action is action and item.start_seconds < duration
            ) or (_evidence_range(evidence, frames, duration),)
            for start, end in measured_ranges:
                intervals.append(
                    _interval(
                        source_hash,
                        "video:0",
                        kind,
                        start,
                        end,
                        (
                            "Sampled visual measurements support a preview-only "
                            "adjustment."
                        ),
                        {
                            "metrics": visual.metrics.model_dump(mode="json"),
                            "evidence": [
                                item.model_dump(mode="json") for item in evidence
                            ],
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
        raw_ranges = stabilization.parameters.get("affected_ranges")
        affected_ranges: list[tuple[float, float]] = []
        if isinstance(raw_ranges, (list, tuple)):
            for item in raw_ranges:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    continue
                try:
                    raw_start, raw_end = item
                    if isinstance(raw_start, bool) or isinstance(raw_end, bool):
                        continue
                    if not isinstance(raw_start, (int, float, str)) or not isinstance(
                        raw_end, (int, float, str)
                    ):
                        continue
                    start = float(raw_start)
                    end = float(raw_end)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(start) and math.isfinite(end) and end > start:
                    affected_ranges.append((max(0.0, start), min(duration, end)))
        if not affected_ranges:
            timestamps = tuple(
                item.timestamp_seconds
                for item in stabilization.transforms
                if not item.scene_boundary
            )
            if timestamps:
                affected_ranges.append(
                    (
                        max(0.0, min(timestamps) - _sample_step(frames)),
                        min(duration, max(timestamps) + _sample_step(frames)),
                    )
                )
        if affected_ranges:
            merged_ranges = _merge_ranges(tuple(affected_ranges))
            for start, end in merged_ranges:
                if end <= start:
                    continue
                intervals.append(
                    _interval(
                        source_hash,
                        "video:0",
                        DamageKind.SHAKE,
                        start,
                        end,
                        "Reliable scene-local affine motion was measured.",
                        {
                            "crop_ratio": stabilization.crop_ratio,
                            "transform_count": len(stabilization.transforms),
                            "affected_ranges": [list(item) for item in merged_ranges],
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
            noise_ranges = audio.measurement.noise_intervals
            for observed in noise_ranges or (None,):
                start = observed.start_seconds if observed is not None else 0.0
                end = observed.end_seconds if observed is not None else duration
                intervals.append(
                    _interval(
                        source_hash,
                        "audio:0",
                        DamageKind.AUDIO_NOISE,
                        start,
                        end,
                        "Sustained stationary noise-like audio was measured.",
                        (
                            observed.model_dump(mode="json")
                            if observed is not None
                            else audio.measurement.model_dump(mode="json")
                        ),
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


def _merge_ranges(
    ranges: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return tuple((start, end) for start, end in merged)


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


def _stabilization_ranges(
    assessment: StabilizationAssessment,
) -> tuple[tuple[float, float], ...]:
    raw = assessment.parameters.get("affected_ranges")
    ranges: list[tuple[float, float]] = []
    if isinstance(raw, (list, tuple)):
        for item in raw:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            start, end = item
            if (
                isinstance(start, bool)
                or isinstance(end, bool)
                or not isinstance(start, (int, float))
                or not isinstance(end, (int, float))
            ):
                continue
            if math.isfinite(start) and math.isfinite(end) and end > start:
                ranges.append((float(start), float(end)))
    if not ranges:
        raise ValueError("recommended stabilization must expose affected ranges")
    return tuple(ranges)


def _transition_anchor_candidate(
    run_assessments: object,
    accepted_ranges: Sequence[tuple[float, float]],
    config: StabilizationConfig,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """Find one bounded rejected-to-anchor adjacency, or fail closed."""
    if not isinstance(run_assessments, list):
        return None
    typed = tuple(item for item in run_assessments if isinstance(item, dict))
    accepted_set = frozenset(
        (float(start), float(end)) for start, end in accepted_ranges
    )
    candidates: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for rejected, accepted in zip(typed, typed[1:], strict=False):
        if (
            rejected.get("accepted") is not False
            or rejected.get("reason") != "crop_budget_exceeded"
            or rejected.get("end_boundary_limited") is not True
            or accepted.get("accepted") is not True
            or accepted.get("start_boundary_limited") is not True
        ):
            continue
        values = (
            rejected.get("end_seconds"),
            accepted.get("start_seconds"),
            accepted.get("end_seconds"),
        )
        if any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            for value in values
        ):
            continue
        numeric_values = cast(tuple[int | float, int | float, int | float], values)
        transition_start, transition_end, anchor_end = (
            float(value) for value in numeric_values
        )
        transition = (transition_start, transition_end)
        anchor = (transition_end, anchor_end)
        if (
            transition_end <= transition_start
            or anchor_end <= transition_end
            or transition_end - transition_start
            > config.maximum_transition_duration_seconds
            or anchor not in accepted_set
        ):
            continue
        candidates.append((transition, anchor))
    return candidates[0] if len(candidates) == 1 else None


def _merge_stabilization_assessments(
    coarse: StabilizationAssessment,
    refined: StabilizationAssessment,
    refinement_ranges: tuple[tuple[float, float], ...],
) -> StabilizationAssessment:
    """Overlay a high-rate local curve while retaining full-timeline anchors."""
    refined_by_time = {item.timestamp_seconds: item for item in refined.transforms}
    for item in coarse.transforms:
        inside_refinement = any(
            start <= item.timestamp_seconds <= end for start, end in refinement_ranges
        )
        if not inside_refinement:
            refined_by_time.setdefault(item.timestamp_seconds, item)
    parameters = dict(refined.parameters)
    parameters["coarse_timeline_anchor_count"] = len(coarse.transforms)
    parameters["refined_motion_sample_count"] = len(refined.transforms)
    return refined.model_copy(
        update={
            "reason": "measured_affine_motion_refined",
            "transforms": tuple(
                refined_by_time[timestamp] for timestamp in sorted(refined_by_time)
            ),
            "parameters": parameters,
        }
    )


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
