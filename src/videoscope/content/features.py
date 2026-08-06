"""Read-only structural feature providers shared by C planners."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from videoscope.content.errors import ContentCancelledError, ContentMappingError
from videoscope.content.models import (
    ContentModel,
    ContentProviderExecution,
    ContentProviderStatus,
    ContentSignalType,
    ContentTimeRange,
)
from videoscope.detectors.image_features import (
    average_hash,
    compute_luma_metrics,
    hash_distance,
    load_luma_image,
    mean_absolute_difference,
)
from videoscope.domain import VideoMetadata
from videoscope.processes import pinned_subprocess_options
from videoscope.scenes.models import (
    SceneDetectionConfig,
    SceneDetectionResult,
    VideoScene,
)
from videoscope.scenes.pyscenedetect import PySceneDetectAdapter
from videoscope.video.probe import probe_video
from videoscope.video.sampling import FrameSample, FrameSamplingResult, sample_frames

_SILENCE_START: Final = re.compile(
    r"\bsilence_start:\s*(?P<timestamp>-?(?:\d+(?:\.\d*)?|\.\d+))"
)
_SILENCE_END: Final = re.compile(
    r"\bsilence_end:\s*(?P<timestamp>-?(?:\d+(?:\.\d*)?|\.\d+))"
)
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_MAX_SILENCE_LOG_BYTES: Final = 32 * 1024 * 1024


class StructuralFeatureConfig(BaseModel):
    """All thresholds and resource bounds for structural observations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_fps: float = Field(default=2.0, gt=0, le=30, allow_inf_nan=False)
    thumbnail_max_edge: int = Field(default=640, ge=16, le=4096)
    maximum_samples: int = Field(default=1_000, ge=1, le=10_000)
    ffmpeg: str = Field(default="ffmpeg", min_length=1)
    ffprobe: str = Field(default="ffprobe", min_length=1)
    command_timeout_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    silence_noise_threshold_db: float = Field(
        default=-35.0, ge=-100, le=0, allow_inf_nan=False
    )
    silence_minimum_duration_seconds: float = Field(
        default=1.0, gt=0, allow_inf_nan=False
    )
    minimum_observation_duration_seconds: float = Field(
        default=1.0, gt=0, allow_inf_nan=False
    )
    near_black_mean_luma_threshold: float = Field(default=0.08, ge=0, le=1)
    near_black_dark_pixel_threshold: float = Field(default=0.10, ge=0, le=1)
    near_black_dark_pixel_ratio: float = Field(default=0.95, ge=0, le=1)
    repeated_max_pixel_difference: float = Field(default=0.01, ge=0, le=1)
    repeated_max_hash_distance: int = Field(default=2, ge=0, le=64)
    maximum_observations_per_provider: int = Field(default=10_000, ge=1, le=100_000)


class ContentObservation(ContentModel):
    """One observable, source-timed structural signal; never a deletion decision."""

    id: str = Field(pattern=r"^observation_[0-9a-f]{64}$")
    signal_type: ContentSignalType
    source_range: ContentTimeRange
    provider_id: str = Field(min_length=1)
    provider_version: str = Field(min_length=1)
    measurements: dict[str, JsonValue] = Field(default_factory=dict)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        *,
        input_hash: str,
        signal_type: ContentSignalType,
        source_range: ContentTimeRange,
        provider_id: str,
        provider_version: str,
        measurements: dict[str, JsonValue] | None = None,
        parameters: dict[str, JsonValue] | None = None,
        limitations: tuple[str, ...] = (),
    ) -> ContentObservation:
        measurement_values = measurements or {}
        parameter_values = parameters or {}
        identifier = make_observation_id(
            input_hash=input_hash,
            signal_type=signal_type,
            source_range=source_range,
            provider_id=provider_id,
            provider_version=provider_version,
            measurements=measurement_values,
            parameters=parameter_values,
        )
        return cls(
            id=identifier,
            signal_type=signal_type,
            source_range=source_range,
            provider_id=provider_id,
            provider_version=provider_version,
            measurements=measurement_values,
            parameters=parameter_values,
            limitations=limitations,
        )


class FeatureProviderResult(ContentModel):
    status: ContentProviderStatus
    observations: tuple[ContentObservation, ...] = ()
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.status is ContentProviderStatus.FAILED:
            raise ValueError("providers report failures through the coordinator")
        if self.status is ContentProviderStatus.SKIPPED and self.observations:
            raise ValueError("skipped provider cannot return observations")
        return self


@dataclass(frozen=True, slots=True)
class ContentFeatureContext:
    input_path: Path
    input_hash: str
    metadata: VideoMetadata
    scenes: tuple[VideoScene, ...]
    frame_samples: tuple[FrameSample, ...]
    frame_workspace: Path
    workspace: Path
    config: StructuralFeatureConfig


@dataclass(frozen=True, slots=True)
class ContentFeatureBundle:
    metadata: VideoMetadata
    scenes: tuple[VideoScene, ...]
    frame_samples: tuple[FrameSample, ...]
    frame_workspace: Path
    observations: tuple[ContentObservation, ...]
    executions: tuple[ContentProviderExecution, ...]
    warnings: tuple[str, ...]
    probe_passes: int = 1
    sampling_passes: int = 1


class ProbeFeatureProvider(Protocol):
    provider_id: str
    version: str

    def probe(self, path: Path, config: StructuralFeatureConfig) -> VideoMetadata: ...


class SceneFeatureProvider(Protocol):
    provider_id: str
    version: str

    def detect(
        self, path: Path, duration_seconds: float, config: StructuralFeatureConfig
    ) -> SceneDetectionResult: ...


class SamplingFeatureProvider(Protocol):
    provider_id: str
    version: str

    def sample(
        self,
        path: Path,
        duration_seconds: float,
        workspace: Path,
        config: StructuralFeatureConfig,
    ) -> FrameSamplingResult: ...


class ObservationFeatureProvider(Protocol):
    provider_id: str
    version: str

    def observe(self, context: ContentFeatureContext) -> FeatureProviderResult: ...


class ExistingProbeProvider:
    provider_id = "metadata"
    version = "1"

    def probe(self, path: Path, config: StructuralFeatureConfig) -> VideoMetadata:
        return probe_video(
            path,
            ffprobe=config.ffprobe,
            timeout_seconds=config.command_timeout_seconds,
        )


class ExistingSceneProvider:
    provider_id = "scenes"
    version = "1"

    def detect(
        self, path: Path, duration_seconds: float, config: StructuralFeatureConfig
    ) -> SceneDetectionResult:
        del config
        return PySceneDetectAdapter(SceneDetectionConfig()).detect(
            path, duration_seconds=duration_seconds
        )


class ExistingSamplingProvider:
    provider_id = "sampling"
    version = "1"

    def sample(
        self,
        path: Path,
        duration_seconds: float,
        workspace: Path,
        config: StructuralFeatureConfig,
    ) -> FrameSamplingResult:
        return sample_frames(
            path,
            sample_rate=config.sample_fps,
            max_edge=config.thumbnail_max_edge,
            image_format="jpeg",
            workspace_parent=workspace,
            ffmpeg=config.ffmpeg,
            ffprobe=config.ffprobe,
            timeout_seconds=config.command_timeout_seconds,
            max_samples=config.maximum_samples,
            timeline_duration_seconds=duration_seconds,
        )


class VisualStructureProvider:
    provider_id = "visual_structure"
    version = "1"

    def observe(self, context: ContentFeatureContext) -> FeatureProviderResult:
        if not context.frame_samples:
            return FeatureProviderResult(
                status=ContentProviderStatus.SKIPPED,
                warnings=("No sampled frames were available for visual structure.",),
            )
        lumas = [
            load_luma_image(context.frame_workspace, sample)
            for sample in context.frame_samples
        ]
        metrics = [
            compute_luma_metrics(
                luma,
                dark_pixel_threshold=context.config.near_black_dark_pixel_threshold,
            )
            for luma in lumas
        ]
        scene_groups = tuple(
            _scene_index(context.scenes, sample.timestamp_seconds)
            for sample in context.frame_samples
        )
        near_black_positions = tuple(
            index
            for index, value in enumerate(metrics)
            if value.mean_luma <= context.config.near_black_mean_luma_threshold
            and value.dark_pixel_ratio >= context.config.near_black_dark_pixel_ratio
        )
        observations: list[ContentObservation] = []
        for positions in _position_runs(near_black_positions, scene_groups):
            source_range = _sample_run_range(
                positions,
                context.frame_samples,
                context.metadata.duration_seconds,
            )
            if (
                source_range.duration_seconds
                < context.config.minimum_observation_duration_seconds
            ):
                continue
            observed = [metrics[position] for position in positions]
            observations.append(
                ContentObservation.create(
                    input_hash=context.input_hash,
                    signal_type=ContentSignalType.NEAR_BLACK,
                    source_range=source_range,
                    provider_id=self.provider_id,
                    provider_version=self.version,
                    measurements={
                        "mean_luma": sum(item.mean_luma for item in observed)
                        / len(observed),
                        "mean_dark_pixel_ratio": sum(
                            item.dark_pixel_ratio for item in observed
                        )
                        / len(observed),
                        "sample_count": len(observed),
                    },
                    parameters={
                        "mean_luma_threshold": (
                            context.config.near_black_mean_luma_threshold
                        ),
                        "dark_pixel_threshold": (
                            context.config.near_black_dark_pixel_threshold
                        ),
                        "dark_pixel_ratio": context.config.near_black_dark_pixel_ratio,
                    },
                    limitations=(
                        "The interval may be an intentional black field, fade, "
                        "or dark scene.",
                    ),
                )
            )
        hashes = [average_hash(luma) for luma in lumas]
        similar_edges: list[int] = []
        edge_measurements: dict[int, tuple[float, int]] = {}
        for left in range(len(lumas) - 1):
            right = left + 1
            if scene_groups[left] != scene_groups[right]:
                continue
            pixel_difference = mean_absolute_difference(lumas[left], lumas[right])
            perceptual_distance = hash_distance(hashes[left], hashes[right])
            if (
                pixel_difference <= context.config.repeated_max_pixel_difference
                and perceptual_distance <= context.config.repeated_max_hash_distance
            ):
                similar_edges.append(left)
                edge_measurements[left] = (pixel_difference, perceptual_distance)
        for edges in _edge_runs(tuple(similar_edges), scene_groups):
            positions = tuple(range(edges[0], edges[-1] + 2))
            source_range = _sample_run_range(
                positions,
                context.frame_samples,
                context.metadata.duration_seconds,
            )
            if (
                source_range.duration_seconds
                < context.config.minimum_observation_duration_seconds
            ):
                continue
            values = [edge_measurements[index] for index in edges]
            observations.append(
                ContentObservation.create(
                    input_hash=context.input_hash,
                    signal_type=ContentSignalType.REPEATED_FRAMES,
                    source_range=source_range,
                    provider_id=self.provider_id,
                    provider_version=self.version,
                    measurements={
                        "mean_pixel_difference": sum(item[0] for item in values)
                        / len(values),
                        "maximum_hash_distance": max(item[1] for item in values),
                        "pair_count": len(values),
                    },
                    parameters={
                        "max_pixel_difference": (
                            context.config.repeated_max_pixel_difference
                        ),
                        "max_hash_distance": context.config.repeated_max_hash_distance,
                    },
                    limitations=(
                        "A static shot can produce the same low-cost similarity "
                        "measurements.",
                    ),
                )
            )
        if len(observations) > context.config.maximum_observations_per_provider:
            raise ContentMappingError("visual observation limit exceeded")
        return FeatureProviderResult(
            status=ContentProviderStatus.OK,
            observations=tuple(sorted(observations, key=_observation_sort_key)),
        )


SilenceCommandRunner = Callable[[Sequence[str], Path, float], int]


class FFmpegSilenceProvider:
    provider_id = "silence"
    version = "1"

    def __init__(self, command_runner: SilenceCommandRunner | None = None) -> None:
        self._command_runner = command_runner or run_silence_command

    def observe(self, context: ContentFeatureContext) -> FeatureProviderResult:
        if not context.metadata.has_audio:
            return FeatureProviderResult(
                status=ContentProviderStatus.SKIPPED,
                warnings=("Audio stream unavailable; silence analysis was skipped.",),
            )
        context.workspace.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix="content-silence-", suffix=".log", dir=context.workspace
        )
        os.close(descriptor)
        log_path = Path(name)
        arguments = build_silence_command(
            context.input_path,
            ffmpeg=context.config.ffmpeg,
            noise_threshold_db=context.config.silence_noise_threshold_db,
            minimum_duration_seconds=context.config.silence_minimum_duration_seconds,
        )
        try:
            return_code = self._command_runner(
                arguments, log_path, context.config.command_timeout_seconds
            )
            if return_code != 0:
                raise ContentMappingError("FFmpeg silence observation failed")
            if log_path.stat().st_size > _MAX_SILENCE_LOG_BYTES:
                raise ContentMappingError(
                    "FFmpeg silence observation log exceeded limit"
                )
            with log_path.open(encoding="utf-8", errors="replace") as stream:
                observations = parse_silence_output(
                    stream,
                    input_hash=context.input_hash,
                    duration_seconds=context.metadata.duration_seconds,
                    provider_id=self.provider_id,
                    provider_version=self.version,
                    parameters={
                        "noise_threshold_db": context.config.silence_noise_threshold_db,
                        "minimum_duration_seconds": (
                            context.config.silence_minimum_duration_seconds
                        ),
                    },
                    maximum_observations=context.config.maximum_observations_per_provider,
                )
            return FeatureProviderResult(
                status=ContentProviderStatus.OK, observations=observations
            )
        finally:
            log_path.unlink(missing_ok=True)


def collect_content_features(
    input_path: Path,
    *,
    input_hash: str,
    workspace: Path,
    config: StructuralFeatureConfig,
    probe_provider: ProbeFeatureProvider | None = None,
    scene_provider: SceneFeatureProvider | None = None,
    sampling_provider: SamplingFeatureProvider | None = None,
    observation_providers: tuple[ObservationFeatureProvider, ...] | None = None,
    cancellation_callback: Callable[[], bool] | None = None,
) -> ContentFeatureBundle:
    """Collect core media context once and isolate optional feature failures."""
    if _SHA256_PATTERN.fullmatch(input_hash) is None:
        raise ContentMappingError("invalid input hash")
    source = Path(input_path)
    work = Path(workspace)
    work.mkdir(parents=True, exist_ok=True)
    probe = probe_provider or ExistingProbeProvider()
    scenes_provider = scene_provider or ExistingSceneProvider()
    sampler = sampling_provider or ExistingSamplingProvider()
    extras = observation_providers or (
        VisualStructureProvider(),
        FFmpegSilenceProvider(),
    )
    extra_ids = tuple(provider.provider_id for provider in extras)
    if len(extra_ids) != len(set(extra_ids)):
        raise ContentMappingError("duplicate optional feature provider")
    executions: list[ContentProviderExecution] = []
    warnings: list[str] = []
    _raise_if_cancelled(cancellation_callback)
    try:
        metadata = probe.probe(source, config)
        executions.append(_successful_execution(probe.provider_id, probe.version))
        _raise_if_cancelled(cancellation_callback)
        scene_result = scenes_provider.detect(source, metadata.duration_seconds, config)
        executions.append(
            _successful_execution(scenes_provider.provider_id, scenes_provider.version)
        )
        warnings.extend(scene_result.warnings)
        _raise_if_cancelled(cancellation_callback)
        sampling = sampler.sample(source, metadata.duration_seconds, work, config)
        executions.insert(
            1, _successful_execution(sampler.provider_id, sampler.version)
        )
    except ContentCancelledError:
        raise
    except Exception as exc:
        raise ContentMappingError(type(exc).__name__) from None
    context = ContentFeatureContext(
        input_path=source,
        input_hash=input_hash,
        metadata=metadata,
        scenes=scene_result.scenes,
        frame_samples=sampling.samples,
        frame_workspace=sampling.work_directory,
        workspace=work,
        config=config,
    )
    observations: list[ContentObservation] = []
    for provider in sorted(extras, key=lambda value: value.provider_id):
        _raise_if_cancelled(cancellation_callback)
        try:
            result = provider.observe(context)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            warning = (
                f"{provider.provider_id} feature provider failed "
                f"({type(exc).__name__})."
            )
            executions.append(
                ContentProviderExecution(
                    provider_id=provider.provider_id,
                    provider_version=provider.version,
                    status=ContentProviderStatus.FAILED,
                    warning=warning,
                )
            )
            warnings.append(warning)
            continue
        executions.append(
            ContentProviderExecution(
                provider_id=provider.provider_id,
                provider_version=provider.version,
                status=result.status,
                warning=result.warnings[0] if result.warnings else None,
            )
        )
        observations.extend(result.observations)
        warnings.extend(result.warnings)
    return ContentFeatureBundle(
        metadata=metadata,
        scenes=scene_result.scenes,
        frame_samples=sampling.samples,
        frame_workspace=sampling.work_directory,
        observations=tuple(sorted(observations, key=_observation_sort_key)),
        executions=tuple(executions),
        warnings=tuple(warnings),
    )


def build_silence_command(
    input_path: Path,
    *,
    ffmpeg: str,
    noise_threshold_db: float,
    minimum_duration_seconds: float,
) -> list[str]:
    """Build one local FFmpeg silence observation command as an argument list."""
    if not math.isfinite(noise_threshold_db) or not -100 <= noise_threshold_db <= 0:
        raise ValueError("silence noise threshold must be between -100 and 0 dB")
    if not math.isfinite(minimum_duration_seconds) or minimum_duration_seconds <= 0:
        raise ValueError("silence minimum duration must be finite and positive")
    noise = format(noise_threshold_db, ".15g")
    duration = format(minimum_duration_seconds, ".15g")
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-i",
        str(input_path),
        "-vn",
        "-af",
        f"silencedetect=noise={noise}dB:d={duration}",
        "-f",
        "null",
        "-",
    ]


def run_silence_command(
    arguments: Sequence[str], log_path: Path, timeout_seconds: float
) -> int:
    """Run FFmpeg with disk-backed diagnostics instead of an unbounded capture."""
    with Path(log_path).open("wb") as diagnostics:
        try:
            completed = subprocess.run(
                list(arguments),
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=diagnostics,
                shell=False,
                timeout=timeout_seconds,
                **pinned_subprocess_options(arguments),
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise ContentMappingError(type(exc).__name__) from None
    return completed.returncode


def parse_silence_output(
    lines: Iterable[str],
    *,
    input_hash: str,
    duration_seconds: float,
    provider_id: str,
    provider_version: str,
    parameters: dict[str, JsonValue],
    maximum_observations: int,
) -> tuple[ContentObservation, ...]:
    """Parse bounded silencedetect events into deterministic source intervals."""
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("media duration must be finite and positive")
    if maximum_observations <= 0:
        raise ValueError("maximum observations must be positive")
    pending_start: float | None = None
    ranges: list[ContentTimeRange] = []
    for raw_line in lines:
        line = str(raw_line)
        start_match = _SILENCE_START.search(line)
        if start_match is not None:
            value = float(start_match.group("timestamp"))
            if not math.isfinite(value) or value < 0 or pending_start is not None:
                raise ContentMappingError("invalid FFmpeg silence start event")
            pending_start = value
        end_match = _SILENCE_END.search(line)
        if end_match is None:
            continue
        value = float(end_match.group("timestamp"))
        if (
            pending_start is None
            or not math.isfinite(value)
            or value <= pending_start
            or value > duration_seconds
        ):
            raise ContentMappingError("invalid FFmpeg silence end event")
        ranges.append(ContentTimeRange(start_seconds=pending_start, end_seconds=value))
        pending_start = None
        if len(ranges) > maximum_observations:
            raise ContentMappingError("silence observation limit exceeded")
    if pending_start is not None:
        if pending_start >= duration_seconds:
            raise ContentMappingError("open silence event exceeds media duration")
        ranges.append(
            ContentTimeRange(start_seconds=pending_start, end_seconds=duration_seconds)
        )
    if len(ranges) > maximum_observations:
        raise ContentMappingError("silence observation limit exceeded")
    return tuple(
        ContentObservation.create(
            input_hash=input_hash,
            signal_type=ContentSignalType.SILENCE,
            source_range=item,
            provider_id=provider_id,
            provider_version=provider_version,
            measurements={"duration_seconds": item.duration_seconds},
            parameters=parameters,
            limitations=(
                "Silence can be meaningful and is never sufficient by itself "
                "to remove content.",
            ),
        )
        for item in ranges
    )


def make_observation_id(
    *,
    input_hash: str,
    signal_type: ContentSignalType,
    source_range: ContentTimeRange,
    provider_id: str,
    provider_version: str,
    measurements: dict[str, JsonValue],
    parameters: dict[str, JsonValue],
) -> str:
    if _SHA256_PATTERN.fullmatch(input_hash) is None:
        raise ValueError("input_hash must be a lowercase SHA-256 digest")
    payload = {
        "input_hash": input_hash,
        "measurements": measurements,
        "parameters": parameters,
        "provider_id": provider_id,
        "provider_version": provider_version,
        "signal_type": signal_type.value,
        "source_range": source_range.model_dump(mode="json"),
    }
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "observation_" + sha256(content.encode("utf-8")).hexdigest()


def _successful_execution(provider_id: str, version: str) -> ContentProviderExecution:
    return ContentProviderExecution(
        provider_id=provider_id,
        provider_version=version,
        status=ContentProviderStatus.OK,
    )


def _raise_if_cancelled(callback: Callable[[], bool] | None) -> None:
    if callback is not None and callback():
        raise ContentCancelledError("cancelled between feature stages")


def _scene_index(scenes: tuple[VideoScene, ...], timestamp_seconds: float) -> int:
    for scene in scenes:
        if scene.start_seconds <= timestamp_seconds < scene.end_seconds:
            return scene.scene_index
    return scenes[-1].scene_index if scenes else -1


def _position_runs(
    positions: tuple[int, ...], groups: tuple[int, ...]
) -> tuple[tuple[int, ...], ...]:
    if not positions:
        return ()
    runs: list[list[int]] = [[positions[0]]]
    for position in positions[1:]:
        previous = runs[-1][-1]
        if position == previous + 1 and groups[position] == groups[previous]:
            runs[-1].append(position)
        else:
            runs.append([position])
    return tuple(tuple(run) for run in runs)


def _edge_runs(
    edges: tuple[int, ...], groups: tuple[int, ...]
) -> tuple[tuple[int, ...], ...]:
    if not edges:
        return ()
    runs: list[list[int]] = [[edges[0]]]
    for edge in edges[1:]:
        previous = runs[-1][-1]
        if edge == previous + 1 and groups[edge] == groups[previous]:
            runs[-1].append(edge)
        else:
            runs.append([edge])
    return tuple(tuple(run) for run in runs)


def _sample_run_range(
    positions: tuple[int, ...],
    samples: tuple[FrameSample, ...],
    duration_seconds: float,
) -> ContentTimeRange:
    final_position = positions[-1]
    end_seconds = (
        samples[final_position + 1].timestamp_seconds
        if final_position + 1 < len(samples)
        else duration_seconds
    )
    return ContentTimeRange(
        start_seconds=samples[positions[0]].timestamp_seconds,
        end_seconds=end_seconds,
    )


def _observation_sort_key(
    value: ContentObservation,
) -> tuple[float, float, str, str, str]:
    return (
        value.source_range.start_seconds,
        value.source_range.end_seconds,
        value.signal_type.value,
        value.provider_id,
        value.id,
    )


__all__ = [
    "ContentFeatureBundle",
    "ContentFeatureContext",
    "ContentObservation",
    "ExistingProbeProvider",
    "ExistingSamplingProvider",
    "ExistingSceneProvider",
    "FFmpegSilenceProvider",
    "FeatureProviderResult",
    "ObservationFeatureProvider",
    "ProbeFeatureProvider",
    "SamplingFeatureProvider",
    "SceneFeatureProvider",
    "StructuralFeatureConfig",
    "VisualStructureProvider",
    "build_silence_command",
    "collect_content_features",
    "make_observation_id",
    "parse_silence_output",
    "run_silence_command",
]
