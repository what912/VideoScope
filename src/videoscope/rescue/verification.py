"""Deterministic, independent verification gates for Rescue outputs."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
import wave
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol, cast

import cv2
import numpy as np
from numpy.typing import NDArray
from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.processes import PinnedDescriptorError, pinned_subprocess_options
from videoscope.rescue.action_roles import action_artifact_role
from videoscope.rescue.audio import (
    AudioDenoiseConfig,
    AudioNoiseInterval,
    LoudnessConfig,
    parse_loudnorm_measurement,
)
from videoscope.rescue.commands import (
    build_faithful_concat_command,
    build_faithful_remux_command,
    build_faithful_segment_command,
    build_ffprobe_version_command,
    build_loudnorm_measurement_command,
    build_packet_timestamp_probe_command,
)
from videoscope.rescue.deblur import BlurKernelEstimate, DeblurConfig
from videoscope.rescue.errors import RescueCancelledError
from videoscope.rescue.executor import (
    ExternalCommandRunner,
    SourceMapping,
    run_external_command,
)
from videoscope.rescue.models import (
    RESCUE_ACTION_VERIFICATION_CHECK_IDS,
    RescueAction,
    RescueActionKind,
    RescueArtifact,
    RescueOutcome,
    RescuePlan,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    canonical_video_encode_contract,
    required_verification_check_ids_for_plan,
    validate_rescue_plan_identity_contract,
)
from videoscope.rescue.qualification import (
    RuntimeVerificationControlHandle,
    SharpenQualificationEvidenceV1,
    SharpenVerificationControlHandle,
    TonalVerificationControlHandle,
    VerificationControlHandle,
    validate_plan_sharpen_qualification_contracts,
)
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationConfig,
    measure_transition_source_consensus,
    validate_plan_stabilization_qualification_contracts,
)
from videoscope.rescue.timeline import (
    ACTUAL_VIDEO_STREAM_START_TOLERANCE_SECONDS,
    DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS,
    mappings_match_retained_ranges,
    normalize_actual_video_timestamps,
    retained_source_ranges,
)
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    validate_plan_tonal_action_contracts,
    validate_tonal_render_qualification,
)
from videoscope.rescue.tonal_metrics import source_relative_tonal_boundary_metrics
from videoscope.rescue.tonal_qualification import (
    TonalEncodedQualificationEvidenceV3,
)
from videoscope.rescue.visual import SharpenConfig, luma_action_wire_from_parameters
from videoscope.video.errors import sanitize_diagnostic
from videoscope.video.probe import probe_video

_SUPPLEMENTARY_IDS = (
    "artifact_integrity",
    "audio_loudness",
    "audio_peak",
    "audio_sample_rate",
    "black_regression",
    "fixed_av_offset",
    "flicker_regression",
    "freeze_regression",
    "luma_chroma_side_effects",
    "luma_clipping",
    "noise_side_effects",
    "perceptible_audio_noise_reduction",
    "perceptible_luma_improvement",
    "perceptible_sharpness_improvement",
    "perceptible_stabilization_improvement",
    "sharpness_side_effects",
    "source_mapping",
    "stabilization_crop",
)
_DEFAULT_MAX_CLIP_INCREASE = 0.0
_CODEC_LUMA_QUANTIZATION_TOLERANCE = 1.0 / 255.0
_CODEC_EVENT_COUNT_TOLERANCE = 2
# Codec-aligned reference renders can differ by one 8-bit luma code value across
# FFmpeg builds.  This bound only applies when no explicit denoise action supplies
# its stricter, user-visible ``maximum_residual_increase`` parameter.
_DEFAULT_MAX_NOISE_INCREASE = 1.0 / 255.0
_DEFAULT_MAX_SHARPNESS_LOSS_RATIO = 0.1
_DEFAULT_UNMODIFIED_FRAME_MAE_TOLERANCE = 1.0 / 255.0
_DEFAULT_MAX_CROP_RATIO = 0.12
_DEFAULT_LOUDNESS_TOLERANCE_LU = 1.0
_DEFAULT_TRUE_PEAK_LIMIT_DBTP = -1.5
_DEFAULT_AV_OFFSET_TOLERANCE_SECONDS = 0.04
_DEFAULT_MEDIA_TIMEOUT_SECONDS = 120.0
_STABILIZATION_VERIFICATION_SAMPLE_RATE = 4.0
_STABILIZATION_MINIMUM_INLIER_RATIO = 0.55
_STABILIZATION_MAXIMUM_RESIDUAL_PIXELS = 3.0
_PACKET_TIMESTAMP_METHOD = "first_usable_packet_timestamp"
_PERCEPTUAL_FPS_TOLERANCE = 1e-3
_PERCEPTUAL_TIME_TOLERANCE_SECONDS = 1e-6
_PERCEPTUAL_MAX_FRAME_INVENTORY = 4096
_PERCEPTUAL_MAX_TIMESTAMP_OUTPUT_BYTES = 60 * 1024
_INDEPENDENT_AFFINE_MINIMUM_INLIER_RATIO = 0.55
_INDEPENDENT_AFFINE_MAXIMUM_RESIDUAL_PIXELS = 3.0
_TONAL_VERIFICATION_WINDOW_SECONDS = 0.05
_TONAL_MINIMUM_TARGET_MARGIN_DB = 0.0
_FAITHFUL_PARAMETER_ACTIONS = (
    RescueActionKind.REMUX,
    RescueActionKind.REBUILD_TIMESTAMPS,
    RescueActionKind.SELECT_TRACKS,
    RescueActionKind.NORMALIZE_ROTATION,
    RescueActionKind.SALVAGE_SEGMENTS,
    RescueActionKind.TRIM_DAMAGED_EDGES,
    RescueActionKind.CORRECT_FIXED_AV_OFFSET,
    RescueActionKind.DENOISE_VIDEO,
    RescueActionKind.SHARPEN,
    RescueActionKind.DENOISE_AUDIO,
)


class _LumaMeasurementError(ValueError):
    """Stable internal category for sanitized luma measurement failures."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_IMPROVED_PARAMETER_ACTIONS = (
    RescueActionKind.ADJUST_LUMA,
    RescueActionKind.DENOISE_VIDEO,
    RescueActionKind.SHARPEN,
    RescueActionKind.DEFLICKER,
    RescueActionKind.STABILIZE,
    RescueActionKind.NORMALIZE_AUDIO,
    RescueActionKind.DENOISE_AUDIO,
)


@dataclass(frozen=True, slots=True)
class ReferenceRenderOptions:
    """Explicit codec settings required to compare faithful visual output."""

    preserve_packet_origin: bool = False


class MediaMeasurementProvider(Protocol):
    """Own all actual media measurements used by the verification decision."""

    def measure(
        self,
        path: Path,
        relative_path: str,
        cancellation_callback: Callable[[], bool],
    ) -> MediaVerificationSnapshot: ...

    def measure_mapped_reference(
        self,
        path: Path,
        mappings: tuple[SourceMapping, ...],
        render_mode: Literal[
            "stream_copy", "single_reencode", "segment_concat_reencode"
        ],
        reference_options: ReferenceRenderOptions,
        cancellation_callback: Callable[[], bool],
    ) -> MediaVerificationSnapshot: ...

    def measure_ranges(
        self,
        path: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, float]: ...

    def compare_ranges(
        self,
        reference: Path,
        candidate: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, float]: ...

    def measure_audio_noise(
        self,
        path: Path,
        config: AudioDenoiseConfig,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[AudioNoiseInterval, ...]: ...

    def measure_stabilization(
        self,
        reference: Path,
        candidate: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, float]: ...


class _PerceptualMeasurementProvider(Protocol):
    """Optional capability used only by action-specific restoration gates."""

    def measure_perceptual_restoration(
        self,
        kind: RescueActionKind,
        source: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
        *,
        boundary_reference: Path | None = None,
    ) -> dict[str, float]: ...

    def inspect_tonal_audio_topology(
        self,
        path: Path,
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]: ...

    def inspect_tonal_audio_timeline(
        self,
        path: Path,
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]: ...


class _StabilizationFreezeMeasurementProvider(Protocol):
    """Optional independent expected-warp evidence for confirmed stabilization."""

    def measure_stabilization_freeze_attribution(
        self,
        source: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, float]: ...

    def measure_stabilization_freeze_attribution_with_control(
        self,
        source: Path,
        parent: Path,
        identity_control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]: ...


class _SharpenMeasurementProvider(Protocol):
    """Optional exact-PTS SHARPEN evidence against the faithful control."""

    def measure_sharpen_improvement(
        self,
        source: Path,
        control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]: ...

    def measure_sharpen_qualification(
        self,
        baseline: Path,
        visibility_control: Path,
        candidate: Path,
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]: ...


class _LumaMeasurementProvider(Protocol):
    """Optional exact-PTS luma evidence against the faithful control."""

    def measure_luma_adjustment(
        self,
        source: Path,
        control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]: ...


@dataclass(frozen=True, slots=True)
class MediaVerificationSnapshot:
    """Bounded measurements; the verifier never retains decoded media frames."""

    path: Path
    relative_path: str
    duration_seconds: float
    video_stream_count: int
    audio_stream_count: int
    complete_decode: bool
    sha256: str
    audio_sample_rate_hz: int | None = None
    black_events: int = 0
    freeze_events: int = 0
    flicker_events: int = 0
    clipping_ratio: float = 0.0
    noise_residual: float = 0.0
    sharpness: float = 0.0
    crop_ratio: float | None = None
    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    av_offset_seconds: float | None = None
    av_offset_method: str | None = None
    av_offset_tool_version: str | None = None

    def __post_init__(self) -> None:
        if not _safe_relative(self.relative_path):
            # Keep construction possible for hostile-boundary tests; the integrity
            # check will reject the value without ever placing it in public JSON.
            pass
        finite_non_negative = (
            self.duration_seconds,
            self.clipping_ratio,
            self.noise_residual,
            self.sharpness,
        )
        optional_finite = (
            self.crop_ratio,
            self.integrated_lufs,
            self.true_peak_dbtp,
            self.av_offset_seconds,
        )
        if (
            any(not math.isfinite(value) or value < 0 for value in finite_non_negative)
            or any(
                value < 0
                for value in (self.video_stream_count, self.audio_stream_count)
            )
            or (
                self.audio_sample_rate_hz is not None
                and not 8000 <= self.audio_sample_rate_hz <= 384000
            )
            or any(
                value < 0
                for value in (
                    self.black_events,
                    self.freeze_events,
                    self.flicker_events,
                )
            )
            or any(
                value is not None and not math.isfinite(value)
                for value in optional_finite
            )
            or self.clipping_ratio > 1
            or (
                self.crop_ratio is not None
                and (self.crop_ratio < 0 or self.crop_ratio > 1)
            )
        ):
            raise ValueError("verification media measurements are invalid")
        if len(self.sha256) != 64 or self.sha256 != self.sha256.lower():
            raise ValueError("verification media hash must be lowercase SHA-256")
        try:
            bytes.fromhex(self.sha256)
        except ValueError as exc:
            raise ValueError(
                "verification media hash must be lowercase SHA-256"
            ) from exc


class NativeMediaMeasurementProvider:
    """Measure local media with ffprobe, full FFmpeg decode, and streamed frames."""

    def __init__(
        self,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        timeout_seconds: float = _DEFAULT_MEDIA_TIMEOUT_SECONDS,
        probe: Callable[[Path], VideoMetadata] | None = None,
        command_runner: ExternalCommandRunner = run_external_command,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("media measurement timeout must be finite and positive")
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._timeout = timeout_seconds
        self._probe = probe or (lambda path: probe_video(path, ffprobe=self._ffprobe))
        self._runner = command_runner
        self._ffprobe_version_loaded = False
        self._cached_ffprobe_version: str | None = None

    def measure(
        self,
        path: Path,
        relative_path: str,
        cancellation_callback: Callable[[], bool],
    ) -> MediaVerificationSnapshot:
        candidate = Path(path)
        digest = _stream_hash(candidate)
        if digest is None:
            raise ValueError("media candidate is not a readable regular file")
        try:
            metadata = self._probe(candidate)
            (
                video_count,
                audio_count,
                audio_sample_rate_hz,
                av_offset,
                av_offset_method,
                av_offset_tool_version,
            ) = self._stream_inventory(candidate, cancellation_callback)
        except RescueCancelledError:
            raise
        except Exception:
            return MediaVerificationSnapshot(
                path=candidate,
                relative_path=relative_path,
                duration_seconds=0.0,
                video_stream_count=0,
                audio_stream_count=0,
                complete_decode=False,
                sha256=digest,
            )
        decode = self._runner(
            tuple(
                [
                    self._ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-xerror",
                    "-nostdin",
                    "-i",
                    str(candidate),
                    "-map",
                    "0",
                    "-f",
                    "null",
                    "-",
                ]
            ),
            timeout_seconds=self._timeout,
            sensitive_paths=(candidate,),
            cancellation_callback=cancellation_callback,
        )
        visual = _measure_visual_stream(candidate, cancellation_callback)
        loudness, peak = self._audio_measurements(
            candidate, audio_count, cancellation_callback
        )
        return MediaVerificationSnapshot(
            path=candidate,
            relative_path=relative_path,
            duration_seconds=metadata.duration_seconds,
            video_stream_count=video_count,
            audio_stream_count=audio_count,
            audio_sample_rate_hz=audio_sample_rate_hz,
            complete_decode=decode.returncode == 0,
            sha256=digest,
            black_events=visual[0],
            freeze_events=visual[1],
            flicker_events=visual[2],
            clipping_ratio=visual[3],
            noise_residual=visual[4],
            sharpness=visual[5],
            integrated_lufs=loudness,
            true_peak_dbtp=peak,
            av_offset_seconds=av_offset,
            av_offset_method=av_offset_method,
            av_offset_tool_version=av_offset_tool_version,
        )

    def measure_mapped_reference(
        self,
        path: Path,
        mappings: tuple[SourceMapping, ...],
        render_mode: Literal[
            "stream_copy", "single_reencode", "segment_concat_reencode"
        ],
        reference_options: ReferenceRenderOptions,
        cancellation_callback: Callable[[], bool],
    ) -> MediaVerificationSnapshot:
        """Render and measure a private codec-aligned retained-range reference."""
        candidate = Path(path)
        digest = _stream_hash(candidate)
        if digest is None:
            raise ValueError("mapped source reference is not a readable regular file")
        if not mappings:
            raise ValueError("mapped source reference requires retained ranges")
        if render_mode == "stream_copy":
            raise ValueError("stream-copy output cannot omit retained source ranges")
        with tempfile.TemporaryDirectory(prefix="videoscope-rescue-reference-") as raw:
            root = Path(raw)
            reference = root / "mapped-reference.mp4"
            if render_mode == "single_reencode":
                result = self._runner(
                    tuple(
                        build_faithful_remux_command(
                            candidate,
                            reference,
                            stream_copy=False,
                            preserve_packet_origin=(
                                reference_options.preserve_packet_origin
                            ),
                            ffmpeg=self._ffmpeg,
                        )
                    ),
                    timeout_seconds=self._timeout,
                    sensitive_paths=(candidate, root),
                    cancellation_callback=cancellation_callback,
                )
            else:
                segments: list[Path] = []
                for index, mapping in enumerate(mappings):
                    segment = root / f"segment-{index:03d}.mp4"
                    result = self._runner(
                        tuple(
                            build_faithful_segment_command(
                                candidate,
                                segment,
                                start_seconds=mapping.source_start,
                                end_seconds=mapping.source_end,
                                ffmpeg=self._ffmpeg,
                            )
                        ),
                        timeout_seconds=self._timeout,
                        sensitive_paths=(candidate, root),
                        cancellation_callback=cancellation_callback,
                    )
                    if result.returncode != 0 or not segment.is_file():
                        raise ValueError("mapped source reference segment failed")
                    segments.append(segment)
                manifest = root / "segments.ffconcat"
                manifest.write_text(
                    "ffconcat version 1.0\n"
                    + "".join(f"file '{segment.name}'\n" for segment in segments),
                    encoding="utf-8",
                    newline="\n",
                )
                result = self._runner(
                    tuple(
                        build_faithful_concat_command(
                            manifest,
                            reference,
                            preserve_packet_origin=(
                                reference_options.preserve_packet_origin
                            ),
                            ffmpeg=self._ffmpeg,
                        )
                    ),
                    timeout_seconds=self._timeout,
                    sensitive_paths=(candidate, root),
                    cancellation_callback=cancellation_callback,
                )
            if result.returncode != 0 or not reference.is_file():
                raise ValueError("mapped source reference concat failed")
            visual, frame_count = _measure_visual_stream_with_count(
                reference,
                cancellation_callback,
            )
            if frame_count == 0:
                raise ValueError("mapped source reference contains no decodable frames")
            return MediaVerificationSnapshot(
                path=candidate,
                relative_path="mapped-source-reference",
                duration_seconds=sum(
                    mapping.source_end - mapping.source_start for mapping in mappings
                ),
                video_stream_count=1,
                audio_stream_count=0,
                complete_decode=True,
                sha256=digest,
                black_events=visual[0],
                freeze_events=visual[1],
                flicker_events=visual[2],
                clipping_ratio=visual[3],
                noise_residual=visual[4],
                sharpness=visual[5],
            )

    def measure_perceptual_restoration(
        self,
        kind: RescueActionKind,
        source: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
        *,
        boundary_reference: Path | None = None,
    ) -> dict[str, float]:
        """Measure one confirmed restoration independently from executor state."""
        if kind is RescueActionKind.DEBLUR:
            return _measure_deblur_outcome(
                source,
                candidate,
                source_ranges,
                output_ranges,
                parameters,
                self._ffprobe,
                self._runner,
                self._timeout,
                cancellation_callback,
            )
        if kind is RescueActionKind.DENOISE_AUDIO:
            return _measure_tonal_outcome(
                source,
                candidate,
                source_ranges,
                output_ranges,
                parameters,
                self._ffmpeg,
                self._runner,
                self._timeout,
                cancellation_callback,
                boundary_reference=boundary_reference,
            )
        if kind is RescueActionKind.STABILIZE:
            return _measure_anchor_outcome(
                source,
                candidate,
                source_ranges,
                output_ranges,
                parameters,
                self._ffprobe,
                self._runner,
                self._timeout,
                cancellation_callback,
            )
        raise ValueError("unsupported perceptual restoration kind")

    def inspect_tonal_audio_topology(
        self,
        path: Path,
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]:
        """Return one strict, canonical AAC topology for qualification evidence."""
        candidate = Path(path)
        from videoscope.rescue.tonal_qualification import (
            tonal_audio_topology_probe_arguments,
        )

        result = self._runner(
            tonal_audio_topology_probe_arguments(candidate, ffprobe=self._ffprobe),
            timeout_seconds=self._timeout,
            sensitive_paths=(candidate,),
            cancellation_callback=cancellation_callback,
        )
        if result.returncode != 0:
            raise ValueError("tonal audio topology probe failed")
        try:
            from videoscope.rescue.tonal_qualification import (
                audio_topology_from_ffprobe_stdout,
            )

            return audio_topology_from_ffprobe_stdout(result.stdout_summary).model_dump(
                mode="json"
            )
        except ValueError as exc:
            raise ValueError("tonal audio topology probe is incomplete") from exc

    def inspect_tonal_audio_timeline(
        self,
        path: Path,
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]:
        """Return one strict normalized AAC packet inventory."""
        candidate = Path(path)
        from videoscope.rescue.tonal_qualification import (
            audio_timeline_from_ffprobe_stdout,
            tonal_audio_timeline_probe_arguments,
        )

        result = self._runner(
            tonal_audio_timeline_probe_arguments(candidate, ffprobe=self._ffprobe),
            timeout_seconds=self._timeout,
            sensitive_paths=(candidate,),
            cancellation_callback=cancellation_callback,
        )
        if result.returncode != 0:
            raise ValueError("tonal audio timeline probe failed")
        try:
            return audio_timeline_from_ffprobe_stdout(result.stdout_summary).model_dump(
                mode="json"
            )
        except ValueError as exc:
            raise ValueError("tonal audio timeline probe is incomplete") from exc

    def measure_ranges(
        self,
        path: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, float]:
        """Measure only confirmed output-time ranges for perceptibility gates."""
        return _measure_visual_ranges(Path(path), ranges, cancellation_callback)

    def compare_ranges(
        self,
        reference: Path,
        candidate: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, float]:
        """Compare aligned decoded frames only outside confirmed changes."""
        return _compare_visual_ranges(
            Path(reference), Path(candidate), ranges, cancellation_callback
        )

    def measure_audio_noise(
        self,
        path: Path,
        config: AudioDenoiseConfig,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[AudioNoiseInterval, ...]:
        from videoscope.rescue.executor import NativeRescueExecutor

        with tempfile.TemporaryDirectory(prefix="videoscope-audio-verify-") as raw:
            result = NativeRescueExecutor(
                runner=self._runner,
                ffmpeg=self._ffmpeg,
                ffprobe=self._ffprobe,
                timeout_seconds=self._timeout,
            ).measure_audio_noise(Path(path), Path(raw), config, cancellation_callback)
            return tuple(result)

    def measure_stabilization(
        self,
        reference: Path,
        candidate: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, float]:
        return _measure_stabilization(
            Path(reference), Path(candidate), ranges, cancellation_callback
        )

    def measure_stabilization_freeze_attribution(
        self,
        source: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, float]:
        return _measure_stabilization_freeze_attribution(
            Path(source),
            Path(candidate),
            source_ranges,
            output_ranges,
            parameters,
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )

    def measure_stabilization_freeze_attribution_with_control(
        self,
        source: Path,
        parent: Path,
        identity_control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]:
        parent_inventory = _probe_video_timestamp_inventory(
            Path(parent),
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )
        control_inventory = _probe_video_timestamp_inventory(
            Path(identity_control),
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )
        candidate_inventory = _probe_video_timestamp_inventory(
            Path(candidate),
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )
        if not (
            parent_inventory.timestamps
            == control_inventory.timestamps
            == candidate_inventory.timestamps
        ):
            raise ValueError("stabilization control/candidate PTS inventory differs")
        _control_topology, control_topology_digest = _probe_sharpen_video_topology(
            Path(identity_control),
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )
        _parent_topology, parent_topology_digest = _probe_sharpen_video_topology(
            Path(parent),
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )
        _candidate_topology, candidate_topology_digest = _probe_sharpen_video_topology(
            Path(candidate),
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )
        if control_topology_digest != candidate_topology_digest:
            raise ValueError("stabilization control/candidate topology differs")
        pts_digest = sha256(
            json.dumps(
                control_inventory.timestamps,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        measured: dict[str, JsonValue] = dict(
            _measure_stabilization_freeze_attribution(
                Path(source),
                Path(candidate),
                source_ranges,
                output_ranges,
                parameters,
                self._ffprobe,
                self._runner,
                self._timeout,
                cancellation_callback,
                outside_control=Path(identity_control),
            )
        )
        measured.update(
            {
                "control_normalized_pts_digest": pts_digest,
                "control_stream_topology_digest": control_topology_digest,
                "control_frame_count": len(control_inventory.timestamps),
                "parent_normalized_pts_digest": pts_digest,
                "parent_stream_topology_digest": parent_topology_digest,
                "parent_frame_count": len(parent_inventory.timestamps),
                "candidate_normalized_pts_digest": pts_digest,
                "candidate_stream_topology_digest": candidate_topology_digest,
                "candidate_frame_count": len(candidate_inventory.timestamps),
            }
        )
        return measured

    def measure_sharpen_improvement(
        self,
        source: Path,
        control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]:
        return _measure_sharpen_improvement(
            Path(source),
            Path(control),
            Path(candidate),
            source_ranges,
            output_ranges,
            parameters,
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )

    def measure_sharpen_qualification(
        self,
        baseline: Path,
        visibility_control: Path,
        candidate: Path,
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]:
        """Use the final core with a decoded same-domain recovery baseline."""
        return _measure_sharpen_improvement(
            Path(baseline),
            Path(visibility_control),
            Path(candidate),
            output_ranges,
            output_ranges,
            parameters,
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
            decoded_source_baseline=True,
        )

    def measure_luma_adjustment(
        self,
        source: Path,
        control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool],
    ) -> dict[str, JsonValue]:
        return _measure_luma_adjustment(
            Path(source),
            Path(control),
            Path(candidate),
            source_ranges,
            output_ranges,
            parameters,
            self._ffmpeg,
            self._ffprobe,
            self._runner,
            self._timeout,
            cancellation_callback,
        )

    def _stream_inventory(
        self, path: Path, cancellation_callback: Callable[[], bool]
    ) -> tuple[int, int, int | None, float | None, str | None, str | None]:
        command = tuple(
            build_packet_timestamp_probe_command(path, ffprobe=self._ffprobe)
        )
        result = self._runner(
            command,
            timeout_seconds=self._timeout,
            sensitive_paths=(path,),
            cancellation_callback=cancellation_callback,
        )
        if result.returncode != 0:
            raise ValueError("ffprobe stream inventory failed")
        payload = json.loads(result.stdout_summary)
        streams = payload.get("streams", []) if isinstance(payload, dict) else []
        packets = payload.get("packets", []) if isinstance(payload, dict) else []
        if not isinstance(streams, list):
            streams = []
        if not isinstance(packets, list):
            packets = []
        video = [item for item in streams if _stream_type(item) == "video"]
        audio = [item for item in streams if _stream_type(item) == "audio"]
        video_indexes = _stream_indexes(video)
        audio_indexes = _stream_indexes(audio)
        audio_sample_rates = _stream_sample_rates(audio)
        unique_rates = {int(rate) for rate in audio_sample_rates.values()}
        sample_rate = next(iter(unique_rates)) if len(unique_rates) == 1 else None
        if (
            not video_indexes
            or not audio_indexes
            or video_indexes.intersection(audio_indexes)
        ):
            return len(video), len(audio), sample_rate, None, None, None
        video_start = _first_packet_timestamp(packets, video_indexes, {})
        audio_start = _first_packet_timestamp(
            packets, audio_indexes, audio_sample_rates
        )
        if video_start is None or audio_start is None:
            return len(video), len(audio), sample_rate, None, None, None
        tool_version = self._get_ffprobe_version(cancellation_callback)
        return (
            len(video),
            len(audio),
            sample_rate,
            audio_start - video_start,
            _PACKET_TIMESTAMP_METHOD,
            tool_version,
        )

    def _get_ffprobe_version(
        self, cancellation_callback: Callable[[], bool]
    ) -> str | None:
        if self._ffprobe_version_loaded:
            return self._cached_ffprobe_version
        result = self._runner(
            tuple(build_ffprobe_version_command(ffprobe=self._ffprobe)),
            timeout_seconds=self._timeout,
            sensitive_paths=(),
            cancellation_callback=cancellation_callback,
        )
        version_lines = result.stdout_summary.splitlines()
        first_line = version_lines[0].strip() if version_lines else ""
        executable_path = Path(self._ffprobe)
        version_sensitive_paths = (
            (executable_path,) if executable_path.is_absolute() else ()
        )
        self._cached_ffprobe_version = (
            sanitize_diagnostic(
                first_line,
                sensitive_paths=version_sensitive_paths,
            )
            if result.returncode == 0 and first_line
            else None
        )
        self._ffprobe_version_loaded = True
        return self._cached_ffprobe_version

    def _audio_measurements(
        self,
        path: Path,
        audio_count: int,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[float | None, float | None]:
        if audio_count == 0:
            return None, None
        result = self._runner(
            tuple(
                build_loudnorm_measurement_command(
                    path, LoudnessConfig(), ffmpeg=self._ffmpeg
                )
            ),
            timeout_seconds=self._timeout,
            sensitive_paths=(path,),
            cancellation_callback=cancellation_callback,
        )
        if result.returncode != 0:
            return None, None
        try:
            measurement = parse_loudnorm_measurement(result.stderr_summary)
        except ValueError:
            return None, None
        return measurement.input_i, measurement.input_tp


class RescueVerifier:
    """Compare measured facts without synthesizing a global quality score."""

    def __init__(
        self,
        *,
        duration_tolerance_seconds: float = DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS,
        measurement_provider: MediaMeasurementProvider | None = None,
    ) -> None:
        if (
            not math.isfinite(duration_tolerance_seconds)
            or duration_tolerance_seconds < 0
        ):
            raise ValueError("duration tolerance must be finite and non-negative")
        self._duration_tolerance = duration_tolerance_seconds
        self._measurement_provider = (
            measurement_provider or NativeMediaMeasurementProvider()
        )

    def verify(
        self,
        source: Path,
        faithful: Path,
        improved: Path | None,
        plan: RescuePlan,
        mappings: Sequence[SourceMapping],
        cancellation_callback: Callable[[], bool] = lambda: False,
        *,
        faithful_render_mode: Literal[
            "stream_copy", "single_reencode", "segment_concat_reencode"
        ] = "stream_copy",
        verification_controls: Sequence[RuntimeVerificationControlHandle] = (),
        _allow_unqualified_sharpen_draft: bool = False,
    ) -> RescueVerificationReport:
        """Verify faithful and improved artifacts independently."""
        validate_rescue_plan_identity_contract(plan)
        validate_plan_sharpen_qualification_contracts(
            plan, allow_unqualified_draft=_allow_unqualified_sharpen_draft
        )
        validate_plan_tonal_action_contracts(plan)
        validate_plan_stabilization_qualification_contracts(plan)
        source_measurement = self._measurement_provider.measure(
            Path(source), "source", cancellation_callback
        )
        mappings_tuple = tuple(mappings)
        reference_options = ReferenceRenderOptions(
            preserve_packet_origin=any(
                action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
                for action in plan.actions
            )
        )
        visual_reference: MediaVerificationSnapshot | None = source_measurement
        visual_reference_name = "whole_source"
        visual_reference_reason: str | None = None
        mappings_cover_source = _mappings_cover_full_source(
            mappings_tuple, source_measurement.duration_seconds
        )
        if faithful_render_mode != "stream_copy" or not mappings_cover_source:
            visual_reference_name = (
                "codec_aligned_source"
                if mappings_cover_source
                else "retained_source_ranges"
            )
            try:
                visual_reference = self._measurement_provider.measure_mapped_reference(
                    Path(source),
                    mappings_tuple,
                    faithful_render_mode,
                    reference_options,
                    cancellation_callback,
                )
            except RescueCancelledError:
                raise
            except Exception:
                visual_reference = None
                visual_reference_reason = (
                    "codec_aligned_source_reference_unavailable"
                    if mappings_cover_source
                    else "retained_source_reference_unavailable"
                )
        faithful_measurement = self._measurement_provider.measure(
            Path(faithful), "faithful-rescue.mp4", cancellation_callback
        )
        improved_measurement = (
            self._measurement_provider.measure(
                Path(improved), "improved-viewing.mp4", cancellation_callback
            )
            if improved is not None
            else None
        )
        checks = list(
            self._checks_for(
                "faithful",
                source_measurement,
                faithful_measurement,
                plan,
                mappings_tuple,
                visual_reference,
                visual_reference_name,
                visual_reference_reason,
                allow_codec_quantization=(
                    faithful_render_mode != "stream_copy"
                    and any(
                        action.kind
                        in {
                            RescueActionKind.DENOISE_VIDEO,
                            RescueActionKind.SHARPEN,
                        }
                        for action in plan.actions
                    )
                ),
            )
        )
        faithful_restoration = self._perceptible_faithful_restoration_checks(
            source_measurement,
            faithful_measurement,
            plan,
            mappings_tuple,
            cancellation_callback,
        )
        if faithful_restoration:
            replacements = {check.check_id: check for check in faithful_restoration}
            checks = [
                replacements[check.check_id]
                if check.artifact == "faithful" and check.check_id in replacements
                else check
                for check in checks
            ]
        faithful_stabilization = self._stabilization_improvement_checks(
            source_measurement,
            faithful_measurement,
            "faithful",
            plan,
            mappings_tuple,
            cancellation_callback,
        )
        if faithful_stabilization:
            replacements = {check.check_id: check for check in faithful_stabilization}
            checks = [
                replacements[check.check_id]
                if check.artifact == "faithful" and check.check_id in replacements
                else check
                for check in checks
            ]
        faithful_freeze = self._stabilization_freeze_check(
            source_measurement,
            faithful_measurement,
            "faithful",
            plan,
            mappings_tuple,
            next(
                check
                for check in checks
                if check.artifact == "faithful"
                and check.check_id == "freeze_regression"
            ),
            cancellation_callback,
            verification_controls=tuple(verification_controls),
        )
        if faithful_freeze is not None:
            checks = [
                faithful_freeze
                if check.artifact == "faithful"
                and check.check_id == "freeze_regression"
                else check
                for check in checks
            ]
        checks.extend(
            self._perceptual_restoration_checks(
                source_measurement,
                faithful_measurement,
                "faithful",
                plan,
                mappings_tuple,
                cancellation_callback,
                verification_controls=tuple(verification_controls),
            )
        )
        if improved_measurement is not None:
            checks.extend(
                self._checks_for(
                    "improved",
                    source_measurement,
                    improved_measurement,
                    plan,
                    mappings_tuple,
                    faithful_measurement,
                    "faithful_restored_baseline",
                    None,
                )
            )
            perceptible = self._perceptible_improvement_checks(
                source_measurement,
                faithful_measurement,
                improved_measurement,
                plan,
                mappings_tuple,
                cancellation_callback,
                verification_controls=tuple(verification_controls),
            )
            if perceptible:
                replacements = {check.check_id: check for check in perceptible}
                checks = [
                    replacements[check.check_id]
                    if check.artifact == "improved" and check.check_id in replacements
                    else check
                    for check in checks
                ]
            stabilization_checks = self._stabilization_improvement_checks(
                source_measurement,
                improved_measurement,
                "improved",
                plan,
                mappings_tuple,
                cancellation_callback,
            )
            if stabilization_checks:
                replacements = {check.check_id: check for check in stabilization_checks}
                checks = [
                    replacements[check.check_id]
                    if check.artifact == "improved" and check.check_id in replacements
                    else check
                    for check in checks
                ]
            inherited_audio = self._perceptible_audio_noise_check(
                source_measurement,
                improved_measurement,
                "improved",
                plan,
                cancellation_callback,
            )
            if inherited_audio is not None:
                checks = [
                    inherited_audio
                    if check.artifact == "improved"
                    and check.check_id == inherited_audio.check_id
                    else check
                    for check in checks
                ]
            regressions = self._unmodified_range_regression_checks(
                faithful_measurement,
                improved_measurement,
                plan,
                mappings_tuple,
                cancellation_callback,
            )
            if regressions:
                replacements = {check.check_id: check for check in regressions}
                checks = [
                    replacements[check.check_id]
                    if check.artifact == "improved" and check.check_id in replacements
                    else check
                    for check in checks
                ]
            checks.extend(
                self._perceptual_restoration_checks(
                    source_measurement,
                    improved_measurement,
                    "improved",
                    plan,
                    mappings_tuple,
                    cancellation_callback,
                    verification_controls=tuple(verification_controls),
                )
            )
        artifact_measurements: tuple[
            tuple[
                Literal["faithful", "improved"],
                MediaVerificationSnapshot | None,
            ],
            ...,
        ] = (("faithful", faithful_measurement), ("improved", improved_measurement))
        # The model derives statuses and outcome from the check matrix.
        return RescueVerificationReport(
            plan_digest=plan.plan_digest,
            faithful_status=RescueVerificationStatus.PASSED,
            improved_status=RescueVerificationStatus.PASSED if improved else None,
            checks=tuple(checks),
            artifacts=tuple(
                RescueArtifact(
                    artifact_role=artifact_role,
                    relative_path=measurement.relative_path,
                    sha256=measurement.sha256,
                    description="Independently measured Rescue candidate.",
                )
                for artifact_role, measurement in artifact_measurements
                if measurement is not None
            ),
            outcome=RescueOutcome.COMPLETED,
            required_check_ids=(required_verification_check_ids_for_plan(plan)),
        )

    def _perceptual_restoration_checks(
        self,
        source: MediaVerificationSnapshot,
        candidate: MediaVerificationSnapshot,
        artifact: Literal["faithful", "improved"],
        plan: RescuePlan,
        mappings: tuple[SourceMapping, ...],
        cancellation_callback: Callable[[], bool],
        *,
        verification_controls: tuple[RuntimeVerificationControlHandle, ...],
    ) -> tuple[RescueVerificationCheck, ...]:
        checks: list[RescueVerificationCheck] = []
        for action in _perceptual_actions(plan):
            output_ranges = _map_source_ranges_to_output(action.source_ranges, mappings)
            try:
                provider = cast(
                    _PerceptualMeasurementProvider, self._measurement_provider
                )
                boundary_reference: Path | None = None
                if (
                    action.kind is RescueActionKind.DENOISE_AUDIO
                    and action.parameters.get("encoded_qualification_version") == "3"
                ):
                    controls = tuple(
                        handle
                        for handle in verification_controls
                        if isinstance(handle, TonalVerificationControlHandle)
                        and handle.recipe.action_id == action.id
                    )
                    if len(controls) != 1:
                        raise ValueError("tonal verification control is unavailable")
                    control = controls[0]
                    from videoscope.rescue.tonal_qualification import (
                        TonalEncodedQualificationEvidenceV3,
                    )

                    evidence = TonalEncodedQualificationEvidenceV3.model_validate_json(
                        json.dumps(
                            action.parameters.get("encoded_candidate_qualification"),
                            ensure_ascii=False,
                        )
                    )
                    combined_topology = evidence.combined_audio_topology
                    combined_timeline = evidence.combined_audio_timeline
                    if combined_topology is None or combined_timeline is None:
                        raise ValueError("tonal verification evidence is incomplete")
                    observed_control_topology = provider.inspect_tonal_audio_topology(
                        control.path, cancellation_callback
                    )
                    observed_candidate_topology = provider.inspect_tonal_audio_topology(
                        candidate.path, cancellation_callback
                    )
                    observed_control_timeline = provider.inspect_tonal_audio_timeline(
                        control.path, cancellation_callback
                    )
                    observed_candidate_timeline = provider.inspect_tonal_audio_timeline(
                        candidate.path, cancellation_callback
                    )
                    if (
                        control.recipe.plan_digest != plan.plan_digest
                        or control.recipe.source_ranges != action.source_ranges
                        or control.recipe.output_ranges != output_ranges
                        or control.recipe.parent_sha256 != evidence.parent_sha256
                        or control.recipe.control_sha256
                        != evidence.boundary_control_sha256
                        or control.recipe.qualified_candidate_sha256
                        != evidence.combined_candidate_sha256
                        or control.recipe.encode_contract
                        != evidence.audio_encode_contract.model_dump(mode="json")
                        or control.recipe.control_audio_topology
                        != evidence.boundary_control_audio_topology.model_dump(
                            mode="json"
                        )
                        or control.recipe.candidate_audio_topology
                        != combined_topology.model_dump(mode="json")
                        or control.recipe.control_audio_timeline
                        != evidence.boundary_control_audio_timeline.model_dump(
                            mode="json"
                        )
                        or control.recipe.candidate_audio_timeline
                        != combined_timeline.model_dump(mode="json")
                        or _stream_hash(control.path)
                        != evidence.boundary_control_sha256
                        or observed_control_topology
                        != evidence.boundary_control_audio_topology.model_dump(
                            mode="json"
                        )
                        or observed_candidate_topology
                        != combined_topology.model_dump(mode="json")
                        or observed_control_timeline
                        != evidence.boundary_control_audio_timeline.model_dump(
                            mode="json"
                        )
                        or observed_candidate_timeline
                        != combined_timeline.model_dump(mode="json")
                    ):
                        raise ValueError("tonal verification control differs")
                    boundary_reference = control.path
                if boundary_reference is None:
                    measured = provider.measure_perceptual_restoration(
                        action.kind,
                        source.path,
                        candidate.path,
                        action.source_ranges,
                        output_ranges,
                        dict(action.parameters),
                        cancellation_callback,
                    )
                else:
                    measured = provider.measure_perceptual_restoration(
                        action.kind,
                        source.path,
                        candidate.path,
                        action.source_ranges,
                        output_ranges,
                        dict(action.parameters),
                        cancellation_callback,
                        boundary_reference=boundary_reference,
                    )
            except RescueCancelledError:
                raise
            except Exception:
                measured = {}
            if action.kind is RescueActionKind.DEBLUR:
                checks.extend(_deblur_verification_checks(artifact, action, measured))
            elif action.kind is RescueActionKind.DENOISE_AUDIO:
                checks.extend(_tonal_verification_checks(artifact, action, measured))
            else:
                checks.extend(_anchor_verification_checks(artifact, action, measured))
        order = {
            check_id: index
            for index, check_id in enumerate(RESCUE_ACTION_VERIFICATION_CHECK_IDS)
        }
        return tuple(sorted(checks, key=lambda check: order[check.check_id]))

    def _stabilization_freeze_check(
        self,
        source: MediaVerificationSnapshot,
        candidate: MediaVerificationSnapshot,
        artifact: Literal["faithful", "improved"],
        plan: RescuePlan,
        mappings: tuple[SourceMapping, ...],
        base_check: RescueVerificationCheck,
        cancellation_callback: Callable[[], bool],
        *,
        verification_controls: tuple[RuntimeVerificationControlHandle, ...],
    ) -> RescueVerificationCheck | None:
        actions = tuple(
            action
            for action in plan.actions
            if action.kind is RescueActionKind.STABILIZE
        )
        if not actions:
            return None
        if len(actions) != 1:
            return self._optional(
                "freeze_regression",
                artifact,
                False,
                "Confirmed stabilization freeze evidence is ambiguous.",
                {"applicable": True, "reason": "action_inventory_ambiguous"},
            )
        action = actions[0]
        output_ranges = _map_source_ranges_to_output(action.source_ranges, mappings)
        try:
            matching_controls = tuple(
                handle
                for handle in verification_controls
                if isinstance(handle, VerificationControlHandle)
                if handle.recipe.action_id == action.id
            )
            if len(matching_controls) != 1:
                raise ValueError("stabilization verification control is unavailable")
            handle = matching_controls[0]
            recipe = handle.recipe
            if (
                recipe.plan_digest != plan.plan_digest
                or recipe.source_ranges != action.source_ranges
                or recipe.encode_contract
                != canonical_video_encode_contract(plan.effective_config)
                or not handle.parent_path.is_file()
                or _stream_hash(handle.parent_path) != recipe.parent_sha256
                or not handle.path.is_file()
                or _stream_hash(handle.path) != recipe.control_sha256
                or candidate.sha256 != recipe.candidate_sha256
                or _stream_hash(candidate.path) != recipe.candidate_sha256
            ):
                raise ValueError("stabilization verification control binding differs")
            provider = cast(
                _StabilizationFreezeMeasurementProvider,
                self._measurement_provider,
            )
            measured = provider.measure_stabilization_freeze_attribution_with_control(
                source.path,
                handle.parent_path,
                handle.path,
                candidate.path,
                action.source_ranges,
                output_ranges,
                dict(action.parameters),
                cancellation_callback,
            )
            recipe_valid = bool(
                measured.get("control_normalized_pts_digest")
                == recipe.normalized_pts_digest
                and measured.get("control_stream_topology_digest")
                == recipe.stream_topology_digest
                and measured.get("control_frame_count") == recipe.frame_count
                and measured.get("parent_normalized_pts_digest")
                == recipe.parent_normalized_pts_digest
                and measured.get("parent_stream_topology_digest")
                == recipe.parent_stream_topology_digest
                and measured.get("parent_frame_count") == recipe.parent_frame_count
                and measured.get("candidate_normalized_pts_digest")
                == recipe.candidate_normalized_pts_digest
                and measured.get("candidate_stream_topology_digest")
                == recipe.candidate_stream_topology_digest
                and measured.get("candidate_frame_count")
                == recipe.candidate_frame_count
            )
            measured = {**measured, "control_recipe_valid": float(recipe_valid)}
        except RescueCancelledError:
            raise
        except Exception:
            measured = {}
        return _stabilization_freeze_verification_check(
            artifact,
            action,
            measured,
            base_check,
        )

    def _perceptible_improvement_checks(
        self,
        source: MediaVerificationSnapshot,
        faithful: MediaVerificationSnapshot,
        improved: MediaVerificationSnapshot,
        plan: RescuePlan,
        mappings: tuple[SourceMapping, ...],
        cancellation_callback: Callable[[], bool],
        *,
        verification_controls: tuple[RuntimeVerificationControlHandle, ...],
    ) -> tuple[RescueVerificationCheck, ...]:
        luma_actions = tuple(
            action
            for action in plan.actions
            if action.kind is RescueActionKind.ADJUST_LUMA
        )
        sharpen_actions = tuple(
            action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
        )
        if not luma_actions and not sharpen_actions:
            return ()
        checks: list[RescueVerificationCheck] = []
        if luma_actions:
            luma_ranges: tuple[tuple[float, float], ...] = ()
            try:
                if len(luma_actions) != 1:
                    raise ValueError("luma action inventory is ambiguous")
                action = luma_actions[0]
                if _ranges_intersect(
                    action.source_ranges, plan.effective_config.locked_ranges
                ):
                    raise ValueError("luma action overlaps a locked source range")
                luma_ranges = _exact_action_output_ranges(
                    action.source_ranges, mappings
                )
                luma_provider = cast(
                    _LumaMeasurementProvider, self._measurement_provider
                )
                measured = luma_provider.measure_luma_adjustment(
                    source.path,
                    faithful.path,
                    improved.path,
                    action.source_ranges,
                    luma_ranges,
                    dict(action.parameters),
                    cancellation_callback,
                )
            except RescueCancelledError:
                raise
            except Exception as exc:
                measured = {
                    "measurement_error": (
                        exc.code
                        if isinstance(exc, _LumaMeasurementError)
                        else "luma_measurement_failed"
                    )
                }
            checks.extend(
                _luma_adjustment_verification_checks(
                    luma_actions[0],
                    mappings,
                    plan.effective_config.locked_ranges,
                    measured,
                    expected_control_sha256=faithful.sha256,
                    expected_candidate_sha256=improved.sha256,
                )
            )
        if sharpen_actions:
            sharp_ranges: tuple[tuple[float, float], ...] = ()
            expected_sharpen_control_sha256 = faithful.sha256
            try:
                if len(sharpen_actions) != 1:
                    raise ValueError("sharpness action inventory is ambiguous")
                action = sharpen_actions[0]
                if _ranges_intersect(
                    action.source_ranges, plan.effective_config.locked_ranges
                ):
                    raise ValueError("sharpness action overlaps a locked source range")
                sharp_ranges = _exact_action_output_ranges(
                    action.source_ranges, mappings
                )
                selected_qualification = None
                if action.parameters.get("qualification") is not None:
                    qualification = SharpenQualificationEvidenceV1.model_validate(
                        action.parameters.get("qualification")
                    )
                    if qualification.output_ranges != sharp_ranges:
                        raise ValueError("SHARPEN qualification output ranges differ")
                    selected_qualification = qualification.selected
                    if selected_qualification is None:
                        raise ValueError(
                            "SHARPEN qualification has no selected profile"
                        )
                sharpen_provider = cast(
                    _SharpenMeasurementProvider, self._measurement_provider
                )
                matching_controls = tuple(
                    handle
                    for handle in verification_controls
                    if isinstance(handle, SharpenVerificationControlHandle)
                    and handle.recipe.action_id == action.id
                )
                if len(matching_controls) != 1:
                    raise ValueError("SHARPEN runtime controls are unavailable")
                handle = matching_controls[0]
                recipe = handle.recipe
                expected_sharpen_control_sha256 = recipe.visibility_control_sha256
                if (
                    recipe.plan_digest != plan.plan_digest
                    or recipe.source_ranges != action.source_ranges
                    or recipe.output_ranges != sharp_ranges
                    or recipe.encode_contract
                    != canonical_video_encode_contract(plan.effective_config)
                    or not handle.baseline_path.is_file()
                    or not handle.visibility_path.is_file()
                    or _stream_hash(handle.baseline_path) != recipe.baseline_sha256
                    or _stream_hash(handle.visibility_path)
                    != recipe.visibility_control_sha256
                    or improved.sha256 != recipe.candidate_sha256
                ):
                    raise ValueError("SHARPEN runtime control binding differs")
                measured = sharpen_provider.measure_sharpen_qualification(
                    handle.baseline_path,
                    handle.visibility_path,
                    improved.path,
                    sharp_ranges,
                    dict(action.parameters),
                    cancellation_callback,
                )
                selected_binding_valid = bool(
                    selected_qualification is None
                    or (
                        recipe.baseline_sha256 == selected_qualification.baseline_sha256
                        and recipe.visibility_control_sha256
                        == selected_qualification.visibility_control_sha256
                        and recipe.candidate_sha256
                        == selected_qualification.candidate_sha256
                        and recipe.normalized_pts_digest
                        == selected_qualification.normalized_pts_digest
                        and recipe.stream_topology_digest
                        == selected_qualification.stream_topology_digest
                        and recipe.inventory_frame_count
                        == selected_qualification.inventory_frame_count
                        and _sharpen_measurement_matches_selected_qualification(
                            measured, selected_qualification.metrics
                        )
                    )
                )
                recipe_valid = bool(
                    measured.get("baseline_sha256") == recipe.baseline_sha256
                    and measured.get("control_sha256")
                    == recipe.visibility_control_sha256
                    and measured.get("candidate_sha256") == recipe.candidate_sha256
                    and measured.get("normalized_pts_digest")
                    == recipe.normalized_pts_digest
                    and measured.get("candidate_topology_sha256")
                    == recipe.stream_topology_digest
                    and measured.get("baseline_topology_sha256")
                    == recipe.stream_topology_digest
                    and measured.get("control_topology_sha256")
                    == recipe.stream_topology_digest
                    and measured.get("inventory_frame_count")
                    == recipe.inventory_frame_count
                )
                measured = {
                    **measured,
                    "runtime_control_recipe_valid": recipe_valid,
                    "selected_qualification_binding_valid": selected_binding_valid,
                }
            except RescueCancelledError:
                raise
            except Exception:
                measured = {}
            checks.append(
                _codec_aligned_sharpness_verification_check(
                    sharpen_actions[0],
                    mappings,
                    plan.effective_config.locked_ranges,
                    measured,
                    plan_digest=plan.plan_digest,
                    expected_control_sha256=expected_sharpen_control_sha256,
                    expected_candidate_sha256=improved.sha256,
                )
            )
        return tuple(checks)

    def _perceptible_faithful_restoration_checks(
        self,
        source: MediaVerificationSnapshot,
        faithful: MediaVerificationSnapshot,
        plan: RescuePlan,
        mappings: tuple[SourceMapping, ...],
        cancellation_callback: Callable[[], bool],
    ) -> tuple[RescueVerificationCheck, ...]:
        checks: list[RescueVerificationCheck] = []
        sharpen_actions = tuple(
            action
            for action in plan.actions
            if action.kind is RescueActionKind.SHARPEN
            and action_artifact_role(action.kind) == "faithful"
        )
        if sharpen_actions:
            source_ranges = tuple(
                source_range
                for action in sharpen_actions
                for source_range in action.source_ranges
            )
            output_ranges = _map_source_ranges_to_output(source_ranges, mappings)
            try:
                before = self._measurement_provider.measure_ranges(
                    source.path, source_ranges, cancellation_callback
                )
                after = self._measurement_provider.measure_ranges(
                    faithful.path, output_ranges, cancellation_callback
                )
            except RescueCancelledError:
                raise
            except Exception:
                checks.append(
                    self._optional(
                        "perceptible_sharpness_improvement",
                        "faithful",
                        False,
                        "Confirmed sharpness ranges could not be measured.",
                        {"applicable": True, "reason": "measurement_unavailable"},
                    )
                )
            else:
                checks.append(
                    self._sharpness_improvement_check(
                        before,
                        after,
                        output_ranges,
                        sharpen_actions,
                        artifact="faithful",
                    )
                )
        audio_check = self._perceptible_audio_noise_check(
            source,
            faithful,
            "faithful",
            plan,
            cancellation_callback,
        )
        if audio_check is not None:
            checks.append(audio_check)
        return tuple(checks)

    def _perceptible_audio_noise_check(
        self,
        source: MediaVerificationSnapshot,
        candidate: MediaVerificationSnapshot,
        artifact: Literal["faithful", "improved"],
        plan: RescuePlan,
        cancellation_callback: Callable[[], bool],
    ) -> RescueVerificationCheck | None:
        actions = tuple(
            action
            for action in plan.actions
            if action.kind is RescueActionKind.DENOISE_AUDIO
        )
        if not actions:
            return None
        broadband_actions = tuple(
            action
            for action in actions
            if not action.parameters.get("interference_profiles")
        )
        if not broadband_actions:
            return self._optional(
                "perceptible_audio_noise_reduction",
                artifact,
                True,
                "Generic broadband-noise reduction is not applicable to confirmed "
                "tonal-only restoration.",
                {
                    "applicable": False,
                    "reason": "tonal_only_action_covered_by_required_checks",
                },
            )
        measure = getattr(self._measurement_provider, "measure_audio_noise", None)
        if not callable(measure):
            return self._optional(
                "perceptible_audio_noise_reduction",
                artifact,
                False,
                "Confirmed audio-noise ranges could not be remeasured.",
                {"applicable": True, "reason": "measurement_unavailable"},
            )
        config = AudioDenoiseConfig(
            noise_floor_threshold_dbfs=min(
                _parameter(action.parameters, "noise_floor_threshold_dbfs", -45.0)
                for action in broadband_actions
            ),
            minimum_confidence=max(
                _parameter(action.parameters, "minimum_noise_confidence", 0.8)
                for action in broadband_actions
            ),
            minimum_event_count=max(
                int(_parameter(action.parameters, "minimum_noise_event_count", 3))
                for action in broadband_actions
            ),
            analysis_window_seconds=max(
                _parameter(action.parameters, "noise_analysis_window_seconds", 0.5)
                for action in broadband_actions
            ),
            minimum_interval_seconds=max(
                _parameter(action.parameters, "noise_minimum_interval_seconds", 1.0)
                for action in broadband_actions
            ),
            merge_gap_seconds=max(
                _parameter(action.parameters, "noise_merge_gap_seconds", 0.5)
                for action in broadband_actions
            ),
            relative_level_increase_db=max(
                _parameter(action.parameters, "noise_relative_level_increase_db", 4.0)
                for action in broadband_actions
            ),
            maximum_stationary_centroid_hz=min(
                _parameter(
                    action.parameters,
                    "noise_maximum_stationary_centroid_hz",
                    350.0,
                )
                for action in broadband_actions
            ),
            minimum_noise_reduction_db=max(
                _parameter(action.parameters, "minimum_noise_reduction_db", 3.0)
                for action in broadband_actions
            ),
        )
        try:
            before = measure(source.path, config, cancellation_callback)
            after = measure(candidate.path, config, cancellation_callback)
        except RescueCancelledError:
            raise
        except Exception:
            return self._optional(
                "perceptible_audio_noise_reduction",
                artifact,
                False,
                "Confirmed audio-noise ranges could not be remeasured.",
                {"applicable": True, "reason": "measurement_unavailable"},
            )
        source_delta = max(
            (item.relative_level_delta_db for item in before), default=0.0
        )
        output_delta = max(
            (item.relative_level_delta_db for item in after), default=0.0
        )
        reduction = source_delta - output_delta
        passed = bool(before) and (
            not after or reduction >= config.minimum_noise_reduction_db
        )
        return self._optional(
            "perceptible_audio_noise_reduction",
            artifact,
            passed,
            "Measured stationary interference is lower in confirmed ranges.",
            {
                "applicable": True,
                "source_interval_count": len(before),
                "output_interval_count": len(after),
                "source_max_relative_level_db": source_delta,
                "output_max_relative_level_db": output_delta,
                "observed_reduction_db": reduction,
                "minimum_reduction_db": config.minimum_noise_reduction_db,
            },
        )

    def _stabilization_improvement_checks(
        self,
        reference: MediaVerificationSnapshot,
        candidate: MediaVerificationSnapshot,
        artifact: Literal["faithful", "improved"],
        plan: RescuePlan,
        mappings: tuple[SourceMapping, ...],
        cancellation_callback: Callable[[], bool],
    ) -> tuple[RescueVerificationCheck, ...]:
        actions = tuple(
            action
            for action in plan.actions
            if action.kind is RescueActionKind.STABILIZE
        )
        if not actions:
            return ()
        output_ranges = _map_source_ranges_to_output(
            tuple(item for action in actions for item in action.source_ranges), mappings
        )
        measure = getattr(self._measurement_provider, "measure_stabilization", None)
        try:
            if not callable(measure):
                raise ValueError("measurement unavailable")
            measured = measure(
                reference.path,
                candidate.path,
                output_ranges,
                cancellation_callback,
            )
        except RescueCancelledError:
            raise
        except Exception:
            measured = {}
        minimum_reduction = max(
            _parameter(action.parameters, "minimum_motion_reduction_ratio", 0.5)
            for action in actions
        )
        source_median = measured.get("source_motion_median_pixels")
        output_median = measured.get("output_motion_median_pixels")
        source_p90 = measured.get("source_motion_p90_pixels")
        output_p90 = measured.get("output_motion_p90_pixels")
        enough = (
            measured.get("source_reliable_transforms", 0.0) >= 3
            and measured.get("output_reliable_transforms", 0.0) >= 3
        )
        passed = bool(
            enough
            and source_median is not None
            and output_median is not None
            and source_p90 is not None
            and output_p90 is not None
            and source_median > 0
            and source_p90 > 0
            and output_median / source_median <= 1.0 - minimum_reduction
            and output_p90 / source_p90 <= 1.0 - minimum_reduction
        )
        crop_ratio = measured.get("crop_ratio")
        max_crop = min(
            _parameter(action.parameters, "max_crop_ratio", _DEFAULT_MAX_CROP_RATIO)
            for action in actions
        )
        return (
            self._optional(
                "perceptible_stabilization_improvement",
                artifact,
                passed,
                "Confirmed stabilization ranges show lower measured frame motion.",
                {
                    "applicable": True,
                    "output_ranges": [list(item) for item in output_ranges],
                    "minimum_reduction_ratio": minimum_reduction,
                    **measured,
                },
            ),
            self._optional(
                "stabilization_crop",
                artifact,
                crop_ratio is not None and crop_ratio <= max_crop,
                "Measured stabilization crop is within the confirmed bound.",
                {
                    "applicable": True,
                    "observed_ratio": crop_ratio,
                    "maximum_ratio": max_crop,
                    "reason": (
                        "measured"
                        if crop_ratio is not None
                        else "native_crop_measurement_unavailable"
                    ),
                },
            ),
        )

    def _unmodified_range_regression_checks(
        self,
        faithful: MediaVerificationSnapshot,
        improved: MediaVerificationSnapshot,
        plan: RescuePlan,
        mappings: tuple[SourceMapping, ...],
        cancellation_callback: Callable[[], bool],
    ) -> tuple[RescueVerificationCheck, ...]:
        visual_kinds = {
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
            RescueActionKind.DEFLICKER,
            RescueActionKind.STABILIZE,
        }
        changed_source_ranges = tuple(
            source_range
            for action in plan.actions
            if action.kind in visual_kinds
            for source_range in action.source_ranges
        )
        changed_output_ranges = _map_source_ranges_to_output(
            changed_source_ranges, mappings
        )
        output_duration = max((mapping.output_end for mapping in mappings), default=0.0)
        unchanged_ranges = _range_complement(changed_output_ranges, output_duration)
        check_ids = (
            ("black_regression", "black_events"),
            ("flicker_regression", "flicker_events"),
            ("freeze_regression", "freeze_events"),
        )
        if not unchanged_ranges:
            return tuple(
                self._optional(
                    check_id,
                    "improved",
                    True,
                    "No unmodified output range exists for regression measurement.",
                    {
                        "applicable": False,
                        "reason": "no_unmodified_output_ranges",
                    },
                )
                for check_id, _metric in check_ids
            )
        try:
            before = self._measurement_provider.measure_ranges(
                faithful.path, unchanged_ranges, cancellation_callback
            )
            after = self._measurement_provider.measure_ranges(
                improved.path, unchanged_ranges, cancellation_callback
            )
            unavailable = None
        except RescueCancelledError:
            raise
        except Exception:
            before = {}
            after = {}
            unavailable = "measurement_unavailable"
        if unavailable is not None:
            return tuple(
                self._optional(
                    check_id,
                    "improved",
                    False,
                    "Unmodified output ranges could not be measured.",
                    {"applicable": True, "reason": unavailable},
                )
                for check_id, _metric in check_ids
            )
        return tuple(
            self._optional(
                check_id,
                "improved",
                after[metric]
                <= before[metric]
                + (_CODEC_EVENT_COUNT_TOLERANCE if metric == "freeze_events" else 0),
                "Unmodified output ranges contain no new measured quality event.",
                {
                    "applicable": True,
                    "comparison": "unmodified_output_ranges",
                    "output_ranges": [list(item) for item in unchanged_ranges],
                    "source_events": before[metric],
                    "output_events": after[metric],
                    "codec_event_tolerance": (
                        _CODEC_EVENT_COUNT_TOLERANCE if metric == "freeze_events" else 0
                    ),
                },
            )
            for check_id, metric in check_ids
        )

    def _measure_improvement_ranges(
        self,
        faithful: MediaVerificationSnapshot,
        improved: MediaVerificationSnapshot,
        output_ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool],
    ) -> tuple[dict[str, float] | None, dict[str, float] | None, str | None]:
        if not output_ranges:
            return None, None, "output_ranges_unavailable"
        try:
            before = self._measurement_provider.measure_ranges(
                faithful.path, output_ranges, cancellation_callback
            )
            after = self._measurement_provider.measure_ranges(
                improved.path, output_ranges, cancellation_callback
            )
        except RescueCancelledError:
            raise
        except Exception:
            return None, None, "measurement_unavailable"
        return before, after, None

    def _sharpness_improvement_check(
        self,
        before: Mapping[str, float],
        after: Mapping[str, float],
        output_ranges: tuple[tuple[float, float], ...],
        sharpen_actions: Sequence[RescueAction],
        *,
        artifact: Literal["faithful", "improved"] = "improved",
    ) -> RescueVerificationCheck:
        minimum_gain = max(
            _parameter(
                action.parameters,
                "minimum_perceptible_sharpness_gain_ratio",
                0.01,
            )
            for action in sharpen_actions
        )
        observed_gain = (
            (after["sharpness"] - before["sharpness"]) / before["sharpness"]
            if before["sharpness"] > 0
            else 0.0
        )
        required_scene_baseline = max(
            _parameter(action.parameters, "scene_baseline_sharpness", 0.0)
            * _parameter(
                action.parameters,
                "minimum_recovered_baseline_ratio",
                0.0,
            )
            for action in sharpen_actions
        )
        return self._optional(
            "perceptible_sharpness_improvement",
            artifact,
            observed_gain >= minimum_gain
            and after["sharpness"] >= required_scene_baseline
            and after["noise_residual"]
            <= before["noise_residual"]
            + max(
                _parameter(action.parameters, "maximum_noise_increase", 0.02)
                for action in sharpen_actions
            ),
            "Confirmed soft-detail ranges show a measurable sharpness lift.",
            {
                "applicable": True,
                "output_ranges": [list(item) for item in output_ranges],
                "source_sharpness": before["sharpness"],
                "output_sharpness": after["sharpness"],
                "observed_gain_ratio": observed_gain,
                "minimum_gain_ratio": minimum_gain,
                "required_scene_baseline_sharpness": required_scene_baseline,
                "source_noise_residual": before["noise_residual"],
                "output_noise_residual": after["noise_residual"],
            },
        )

    def _checks_for(
        self,
        artifact: Literal["faithful", "improved"],
        source: MediaVerificationSnapshot,
        candidate: MediaVerificationSnapshot,
        plan: RescuePlan,
        mappings: Sequence[SourceMapping],
        visual_reference: MediaVerificationSnapshot | None,
        visual_reference_name: str,
        visual_reference_reason: str | None,
        *,
        allow_codec_quantization: bool = False,
    ) -> tuple[RescueVerificationCheck, ...]:
        expected_duration = sum(
            item.output_end - item.output_start for item in mappings
        )
        faithful_parameters = _action_parameters(plan, _FAITHFUL_PARAMETER_ACTIONS)
        improvement_parameters = (
            _action_parameters(plan, _IMPROVED_PARAMETER_ACTIONS)
            if artifact == "improved"
            else {}
        )
        normalization_parameters = (
            _action_parameters(plan, (RescueActionKind.NORMALIZE_AUDIO,))
            if artifact == "improved"
            else {}
        )
        fixed_offset_parameters = _action_parameters(
            plan, (RescueActionKind.CORRECT_FIXED_AV_OFFSET,)
        )
        parameters = (
            faithful_parameters if artifact == "faithful" else improvement_parameters
        )
        maximum_noise_increase = _parameter(
            parameters, "maximum_residual_increase", _DEFAULT_MAX_NOISE_INCREASE
        )
        visual_comparison_applicable = visual_reference is not None
        comparison = visual_reference or source
        event_tolerance = (
            _CODEC_EVENT_COUNT_TOLERANCE if allow_codec_quantization else 0
        )
        clipping_tolerance = max(
            _parameter(parameters, "maximum_clip_increase", _DEFAULT_MAX_CLIP_INCREASE),
            (_CODEC_LUMA_QUANTIZATION_TOLERANCE if allow_codec_quantization else 0.0),
        )
        source_hash = _stream_hash(source.path)
        required = (
            self._check(
                "decodable",
                artifact,
                candidate.complete_decode,
                "Complete local decode completed.",
                {"complete_decode": candidate.complete_decode},
            ),
            self._check(
                "duration",
                artifact,
                abs(candidate.duration_seconds - expected_duration)
                <= self._duration_tolerance,
                "Measured output duration matches source mappings.",
                {
                    "expected_seconds": expected_duration,
                    "observed_seconds": candidate.duration_seconds,
                    "tolerance_seconds": self._duration_tolerance,
                },
            ),
            self._check(
                "streams",
                artifact,
                candidate.video_stream_count == source.video_stream_count
                and candidate.audio_stream_count == source.audio_stream_count
                and candidate.video_stream_count > 0,
                "Output stream inventory matches the confirmed source inventory.",
                {
                    "expected_video_streams": source.video_stream_count,
                    "observed_video_streams": candidate.video_stream_count,
                    "expected_audio_streams": source.audio_stream_count,
                    "observed_audio_streams": candidate.audio_stream_count,
                },
            ),
            self._check(
                "source_read_only",
                artifact,
                source_hash is not None
                and source_hash == source.sha256
                and source.sha256 == plan.input_hash,
                "Source bytes still match the confirmed plan.",
                {
                    "snapshot_matches_plan": source.sha256 == plan.input_hash,
                    "current_matches_snapshot": source_hash == source.sha256,
                },
            ),
        )
        mapping_ok = mappings_match_retained_ranges(
            mappings, retained_source_ranges(plan)
        ) and _mappings_are_complete(mappings, candidate.duration_seconds)
        expected_relative_path = (
            "faithful-rescue.mp4" if artifact == "faithful" else "improved-viewing.mp4"
        )
        relative_path_ok = candidate.relative_path == expected_relative_path
        supplementary = (
            self._optional(
                "artifact_integrity",
                artifact,
                _artifact_integrity(candidate) and relative_path_ok,
                "Artifact hash and output-root-relative path are valid.",
                {
                    "hash_matches": _stream_hash(candidate.path) == candidate.sha256,
                    "relative_path_valid": _safe_relative(candidate.relative_path)
                    and relative_path_ok,
                },
                failure_status=RescueVerificationStatus.FAILED,
            ),
            self._optional(
                "audio_loudness",
                artifact,
                _loudness_ok(candidate, normalization_parameters),
                "Measured integrated loudness is within the confirmed bound.",
                _loudness_values(candidate, normalization_parameters),
            ),
            self._optional(
                "audio_peak",
                artifact,
                _peak_ok(candidate, normalization_parameters),
                "Measured true peak is within the confirmed bound.",
                _peak_values(candidate, normalization_parameters),
            ),
            self._optional(
                "audio_sample_rate",
                artifact,
                _audio_sample_rate_ok(source, candidate, improvement_parameters),
                "Filtered audio preserves the measured source sample rate.",
                _audio_sample_rate_values(source, candidate, improvement_parameters),
            ),
            self._optional(
                "black_regression",
                artifact,
                visual_comparison_applicable
                and candidate.black_events <= comparison.black_events + event_tolerance,
                "No additional black-event observations were measured.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_events": comparison.black_events,
                        "output_events": candidate.black_events,
                        "codec_event_tolerance": event_tolerance,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
            ),
            self._optional(
                "fixed_av_offset",
                artifact,
                _fixed_offset_ok(candidate, fixed_offset_parameters),
                "Native packet-timestamp residual is within confirmed tolerance.",
                _offset_values(candidate, fixed_offset_parameters),
            ),
            self._optional(
                "flicker_regression",
                artifact,
                visual_comparison_applicable
                and candidate.flicker_events
                <= comparison.flicker_events + event_tolerance,
                "No additional flicker-event observations were measured.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_events": comparison.flicker_events,
                        "output_events": candidate.flicker_events,
                        "codec_event_tolerance": event_tolerance,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
            ),
            self._optional(
                "freeze_regression",
                artifact,
                visual_comparison_applicable
                and candidate.freeze_events
                <= comparison.freeze_events + event_tolerance,
                "No additional freeze-event observations were measured.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_events": comparison.freeze_events,
                        "output_events": candidate.freeze_events,
                        "codec_event_tolerance": event_tolerance,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
            ),
            self._optional(
                "luma_chroma_side_effects",
                artifact,
                True,
                "Decoded chroma shift is evaluated on exact confirmed luma ranges.",
                {
                    "applicable": False,
                    "reason": "evaluated_after_both_artifacts",
                },
            ),
            self._optional(
                "luma_clipping",
                artifact,
                visual_comparison_applicable
                and candidate.clipping_ratio - comparison.clipping_ratio
                <= clipping_tolerance,
                "No excessive luma clipping was introduced.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_ratio": comparison.clipping_ratio,
                        "output_ratio": candidate.clipping_ratio,
                        "maximum_increase": clipping_tolerance,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
            ),
            self._optional(
                "noise_side_effects",
                artifact,
                visual_comparison_applicable
                and candidate.noise_residual - comparison.noise_residual
                <= maximum_noise_increase,
                "Noise residual increase remains within the applicable measured "
                "tolerance.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_residual": comparison.noise_residual,
                        "output_residual": candidate.noise_residual,
                        "maximum_residual_increase": maximum_noise_increase,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
            ),
            self._optional(
                "perceptible_audio_noise_reduction",
                artifact,
                True,
                "Audio-noise reduction is evaluated only when denoise is confirmed.",
                {"applicable": False, "reason": "no_confirmed_audio_denoise"},
            ),
            self._optional(
                "perceptible_luma_improvement",
                artifact,
                True,
                "Local luma improvement is evaluated only for the improved artifact.",
                {"applicable": False, "reason": "evaluated_after_both_artifacts"},
            ),
            self._optional(
                "perceptible_sharpness_improvement",
                artifact,
                True,
                "Local sharpness improvement is evaluated only for the improved "
                "artifact.",
                {"applicable": False, "reason": "evaluated_after_both_artifacts"},
            ),
            self._optional(
                "perceptible_stabilization_improvement",
                artifact,
                True,
                "Motion improvement is evaluated only after both artifacts exist.",
                {"applicable": False, "reason": "evaluated_after_both_artifacts"},
            ),
            self._optional(
                "sharpness_side_effects",
                artifact,
                visual_comparison_applicable
                and _sharpness_ok(comparison, candidate, parameters),
                "Measured sharpness loss remains within the configured bound.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_sharpness": comparison.sharpness,
                        "output_sharpness": candidate.sharpness,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
            ),
            self._optional(
                "source_mapping",
                artifact,
                mapping_ok,
                "Source mappings are complete, contiguous, and output-root-relative.",
                {"mapping_count": len(mappings), "complete": mapping_ok},
                failure_status=RescueVerificationStatus.FAILED,
            ),
            self._optional(
                "stabilization_crop",
                artifact,
                "max_crop_ratio" not in improvement_parameters
                or (
                    candidate.crop_ratio is not None
                    and candidate.crop_ratio
                    <= _parameter(
                        improvement_parameters,
                        "max_crop_ratio",
                        _DEFAULT_MAX_CROP_RATIO,
                    )
                ),
                "Measured stabilization crop is within the confirmed bound.",
                {
                    "applicable": "max_crop_ratio" in improvement_parameters,
                    "observed_ratio": candidate.crop_ratio,
                    "maximum_ratio": _parameter(
                        improvement_parameters,
                        "max_crop_ratio",
                        _DEFAULT_MAX_CROP_RATIO,
                    ),
                    "reason": (
                        "native_crop_measurement_unavailable"
                        if "max_crop_ratio" in improvement_parameters
                        and candidate.crop_ratio is None
                        else "measured"
                    ),
                },
            ),
        )
        assert tuple(check.check_id for check in supplementary) == _SUPPLEMENTARY_IDS
        return required + supplementary

    @staticmethod
    def _check(
        check_id: str,
        artifact: Literal["faithful", "improved"],
        passed: bool,
        message: str,
        measured: dict[str, JsonValue],
    ) -> RescueVerificationCheck:
        return RescueVerificationCheck(
            check_id=check_id,
            artifact=artifact,
            status=RescueVerificationStatus.PASSED
            if passed
            else RescueVerificationStatus.FAILED,
            message=message,
            measured=measured,
        )

    @staticmethod
    def _optional(
        check_id: str,
        artifact: Literal["faithful", "improved"],
        passed: bool,
        message: str,
        measured: dict[str, JsonValue],
        *,
        failure_status: RescueVerificationStatus = (
            RescueVerificationStatus.NEEDS_REVIEW
        ),
    ) -> RescueVerificationCheck:
        return RescueVerificationCheck(
            check_id=check_id,
            artifact=artifact,
            status=RescueVerificationStatus.PASSED if passed else failure_status,
            message=message,
            measured=measured,
            required=False,
        )


def _perceptual_actions(plan: RescuePlan) -> tuple[RescueAction, ...]:
    actions: list[RescueAction] = []
    for kind in (
        RescueActionKind.DEBLUR,
        RescueActionKind.DENOISE_AUDIO,
        RescueActionKind.STABILIZE,
    ):
        matching = tuple(action for action in plan.actions if action.kind is kind)
        if len(matching) != 1:
            continue
        action = matching[0]
        if kind is RescueActionKind.DENOISE_AUDIO and not action.parameters.get(
            "interference_profiles"
        ):
            continue
        if kind is RescueActionKind.STABILIZE and action.parameters.get("method") != (
            "anchor_v1"
        ):
            if action.parameters.get("method") != "transition_anchor_v1":
                continue
        actions.append(action)
    return tuple(actions)


def _finite_metric(measured: Mapping[str, object], key: str) -> float | None:
    value = measured.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        return None
    return float(value)


def _is_sha256_digest(value: str) -> bool:
    if len(value) != 64 or value != value.lower():
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _at_or_below_with_ulp(value: float, maximum: float) -> bool:
    """Accept only binary representation noise at an inclusive configured limit."""
    return value <= maximum or math.isclose(
        value,
        maximum,
        rel_tol=0.0,
        abs_tol=max(math.ulp(value), math.ulp(maximum)),
    )


def _independent_deblur_pair_metrics(
    source_gray: np.ndarray, candidate_gray: np.ndarray
) -> dict[str, float]:
    """Measure edge recovery without using estimator or renderer evidence."""
    source = _verification_gray(source_gray)
    candidate = _verification_gray(candidate_gray)
    if source.shape != candidate.shape:
        raise ValueError("deblur comparison frame shapes differ")
    width_ratios: list[float] = []
    continuity_ratios: list[float] = []
    base_edges: NDArray[np.bool_] | None = None
    base_mask: NDArray[np.bool_] | None = None
    for sigma in (0.0, 1.0, 2.0):
        source_scale = (
            source if sigma == 0.0 else cv2.GaussianBlur(source, (0, 0), sigma)
        )
        candidate_scale = (
            candidate if sigma == 0.0 else cv2.GaussianBlur(candidate, (0, 0), sigma)
        )
        source_gradient = cv2.magnitude(
            cv2.Sobel(source_scale, cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(source_scale, cv2.CV_32F, 0, 1, ksize=3),
        )
        candidate_gradient = cv2.magnitude(
            cv2.Sobel(candidate_scale, cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(candidate_scale, cv2.CV_32F, 0, 1, ksize=3),
        )
        positive = source_gradient[source_gradient > 0]
        if positive.size < 16:
            raise ValueError("deblur comparison contains too few measurable edges")
        edge_threshold = max(8.0, float(np.percentile(positive, 65)))
        source_edges = source_gradient >= edge_threshold
        if int(np.count_nonzero(source_edges)) < 16:
            raise ValueError("deblur comparison contains too few measurable edges")
        mask = cv2.dilate(
            source_edges.astype(np.uint8), np.ones((3, 3), np.uint8)
        ).astype(bool)
        source_strength = float(np.percentile(source_gradient[source_edges], 75))
        candidate_strength = float(np.percentile(candidate_gradient[mask], 75))
        width_ratios.append(source_strength / max(candidate_strength, 1e-9))
        candidate_edges = candidate_gradient >= edge_threshold
        represented = source_edges & cv2.dilate(
            candidate_edges.astype(np.uint8), np.ones((3, 3), np.uint8)
        ).astype(bool)
        continuity_ratios.append(
            float(np.count_nonzero(represented))
            / max(1, int(np.count_nonzero(source_edges)))
        )
        if sigma == 0.0:
            base_edges = cast(NDArray[np.bool_], source_edges)
            base_mask = cast(NDArray[np.bool_], mask)
    assert base_edges is not None and base_mask is not None
    source_edges = base_edges
    mask = base_mask
    edge_width_ratio = float(np.median(width_ratios))
    edge_continuity_ratio = min(continuity_ratios)

    local_min = cv2.erode(source, np.ones((11, 11), np.uint8))
    local_max = cv2.dilate(source, np.ones((11, 11), np.uint8))
    ringing = mask & ((candidate < local_min - 8.0) | (candidate > local_max + 8.0))
    ringing_ratio = float(np.count_nonzero(ringing)) / max(
        1, int(np.count_nonzero(mask))
    )

    non_edge = ~cv2.dilate(
        source_edges.astype(np.uint8), np.ones((7, 7), np.uint8)
    ).astype(bool)
    source_high = source - cv2.GaussianBlur(source, (3, 3), 0)
    candidate_high = candidate - cv2.GaussianBlur(candidate, (3, 3), 0)
    if int(np.count_nonzero(non_edge)) < 16:
        noise_gain_ratio = 1.0
    else:
        source_noise = float(np.mean(np.abs(source_high[non_edge]), dtype=np.float64))
        candidate_noise = float(
            np.mean(np.abs(candidate_high[non_edge]), dtype=np.float64)
        )
        noise_gain_ratio = candidate_noise / max(source_noise, 1.0 / 255.0)
    return {
        "edge_width_ratio": edge_width_ratio,
        "edge_continuity_ratio": edge_continuity_ratio,
        "ringing_ratio": ringing_ratio,
        "noise_gain_ratio": noise_gain_ratio,
    }


def _independent_tonal_window_metrics(
    source_samples: np.ndarray,
    candidate_samples: np.ndarray,
    sample_rate_hz: int,
    *,
    target_frequency_hz: float,
    window_seconds: float,
    boundary_transition_seconds: float = 0.0,
) -> dict[str, float]:
    """Compare target and non-target spectra in independent 50 ms windows."""
    source = np.asarray(source_samples, dtype=np.float64).reshape(-1)
    candidate = np.asarray(candidate_samples, dtype=np.float64).reshape(-1)
    if source.shape != candidate.shape or source.size == 0:
        raise ValueError("tonal comparison samples are not aligned")
    if (
        sample_rate_hz <= 0
        or not math.isfinite(target_frequency_hz)
        or not math.isfinite(boundary_transition_seconds)
        or boundary_transition_seconds < 0.0
    ):
        raise ValueError("tonal comparison parameters are invalid")
    if not math.isclose(
        window_seconds,
        _TONAL_VERIFICATION_WINDOW_SECONDS,
        rel_tol=0.0,
        abs_tol=1e-12,
    ):
        raise ValueError("tonal verification requires fixed 50 ms windows")
    window_size = max(8, round(window_seconds * sample_rate_hz))
    if source.size < window_size:
        raise ValueError("tonal comparison contains no complete window")
    transition_size = math.ceil(boundary_transition_seconds * sample_rate_hz)
    window = np.hanning(window_size)
    frequencies = np.fft.rfftfreq(window_size, d=1.0 / sample_rate_hz)
    target_index = int(np.argmin(np.abs(frequencies - target_frequency_hz)))
    exclusion = np.abs(frequencies - target_frequency_hz) <= max(
        2.0 * sample_rate_hz / window_size, 40.0
    )
    epsilon = float(np.finfo(np.float64).tiny)
    reductions: list[float] = []
    preservation: list[float] = []
    starts = range(0, source.size - window_size + 1, window_size)
    selected_starts = tuple(
        start
        for start in starts
        if start >= transition_size
        and start + window_size <= source.size - transition_size
    )
    if not selected_starts:
        raise ValueError("tonal comparison contains no full-weight complete window")
    total_window_count = 1 + (source.size - window_size) // window_size
    for start in selected_starts:
        source_spectrum = np.abs(
            np.fft.rfft(source[start : start + window_size] * window)
        )
        candidate_spectrum = np.abs(
            np.fft.rfft(candidate[start : start + window_size] * window)
        )
        reductions.append(
            20.0
            * math.log10(
                max(float(source_spectrum[target_index]), epsilon)
                / max(float(candidate_spectrum[target_index]), epsilon)
            )
        )
        source_non_target = float(np.linalg.norm(source_spectrum[~exclusion]))
        candidate_non_target = float(np.linalg.norm(candidate_spectrum[~exclusion]))
        preservation.append(
            max(
                0.0,
                20.0
                * math.log10(
                    max(source_non_target, epsilon) / max(candidate_non_target, epsilon)
                ),
            )
        )
    return {
        "target_reduction_db": float(min(reductions)),
        "non_target_attenuation_db": float(max(preservation)),
        "window_count": float(len(reductions)),
        "excluded_transition_window_count": float(total_window_count - len(reductions)),
    }


def _independent_affine_measurement(
    source_gray: np.ndarray, candidate_gray: np.ndarray
) -> dict[str, float] | None:
    """Estimate a robust frame-to-frame affine transform from decoded pixels."""
    source: NDArray[np.uint8] = _verification_gray(source_gray).astype(np.uint8)
    candidate: NDArray[np.uint8] = _verification_gray(candidate_gray).astype(np.uint8)
    if source.shape != candidate.shape:
        return None
    points = cv2.goodFeaturesToTrack(
        source, maxCorners=500, qualityLevel=0.01, minDistance=5
    )
    if points is None or len(points) < 8:
        return None
    tracked, status, _errors = cv2.calcOpticalFlowPyrLK(source, candidate, points, None)
    if tracked is None or status is None:
        return None
    selected = status.reshape(-1).astype(bool)
    if int(np.count_nonzero(selected)) < 8:
        return None
    before = points[selected].reshape(-1, 2)
    after = tracked[selected].reshape(-1, 2)
    matrix, inliers = cv2.estimateAffinePartial2D(
        before,
        after,
        method=cv2.RANSAC,
        ransacReprojThreshold=_INDEPENDENT_AFFINE_MAXIMUM_RESIDUAL_PIXELS,
    )
    if matrix is None or inliers is None:
        return None
    selected_inliers = inliers.reshape(-1).astype(bool)
    inlier_ratio = float(np.mean(selected_inliers))
    if inlier_ratio < _INDEPENDENT_AFFINE_MINIMUM_INLIER_RATIO:
        return None
    homogeneous = np.column_stack((before, np.ones(before.shape[0])))
    projected = homogeneous @ matrix.T
    residuals = np.linalg.norm(projected - after, axis=1)
    dx, dy = float(matrix[0, 2]), float(matrix[1, 2])
    scale = float(math.hypot(float(matrix[0, 0]), float(matrix[1, 0])))
    rotation_degrees = math.degrees(
        math.atan2(float(matrix[1, 0]), float(matrix[0, 0]))
    )
    return {
        "translation_pixels": float(math.hypot(dx, dy)),
        "translation_x": dx,
        "translation_y": dy,
        "scale": scale,
        "rotation_degrees": rotation_degrees,
        "inlier_ratio": inlier_ratio,
        "fit_residual_pixels": float(np.median(residuals[selected_inliers])),
    }


def _verification_gray(frame: np.ndarray) -> NDArray[np.float32]:
    array = np.asarray(frame)
    if array.ndim == 3:
        array = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
    if array.ndim != 2 or min(array.shape) < 8:
        raise ValueError("verification frame shape is invalid")
    return cast(NDArray[np.float32], array.astype(np.float32))


def _bounded_transition_evidence_pair(
    source_frame: np.ndarray,
    candidate_frame: np.ndarray,
    config: StabilizationConfig,
    *,
    require_exact_aspect: bool,
) -> tuple[NDArray[np.uint8], NDArray[np.uint8]]:
    """Convert one aligned pair only after a bounded analysis resize."""
    source_array = np.asarray(source_frame)
    candidate_array = np.asarray(candidate_frame)
    source_shape = tuple(int(value) for value in source_array.shape)
    candidate_shape = tuple(int(value) for value in candidate_array.shape)
    if (
        source_array.ndim not in {2, 3}
        or candidate_array.ndim != source_array.ndim
        or source_shape != candidate_shape
        or len(source_shape) not in {2, 3}
        or min(source_shape[:2]) < 8
        or (
            source_array.ndim == 3
            and (source_shape[2] not in {3, 4} or candidate_shape[2] != source_shape[2])
        )
    ):
        raise ValueError("transition verification frame shapes are invalid")
    source_height, source_width = source_shape[:2]
    target_width = config.frame_width
    target_height = config.frame_height
    target_pixels = target_width * target_height
    if (
        target_pixels <= 0
        or target_pixels > 4096 * 4096
        or (
            require_exact_aspect
            and source_width * target_height != source_height * target_width
        )
    ):
        raise ValueError("transition verification frame aspect mapping is unsafe")
    interpolation = (
        cv2.INTER_AREA
        if source_width >= target_width and source_height >= target_height
        else cv2.INTER_LINEAR
    )

    def bounded_gray(frame: np.ndarray) -> NDArray[np.uint8]:
        resized = cv2.resize(
            frame,
            (target_width, target_height),
            interpolation=interpolation,
        )
        if resized.ndim == 3:
            conversion = (
                cv2.COLOR_BGR2GRAY if resized.shape[2] == 3 else cv2.COLOR_BGRA2GRAY
            )
            resized = cv2.cvtColor(resized, conversion)
        if resized.shape != (target_height, target_width):
            raise ValueError(
                "transition verification resize did not honor its pixel budget"
            )
        return cast(
            NDArray[np.uint8],
            np.clip(resized, 0, 255).astype(np.uint8),
        )

    bounded_source = bounded_gray(source_array)
    bounded_candidate = bounded_gray(candidate_array)
    if (
        bounded_source.shape != (target_height, target_width)
        or bounded_candidate.shape != (target_height, target_width)
        or bounded_source.size + bounded_candidate.size > target_pixels * 2
    ):
        raise ValueError(
            "transition verification resize did not honor its pixel budget"
        )
    return bounded_source, bounded_candidate


def _paired_perceptual_ranges(
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float, float, float], ...]:
    if not source_ranges or len(source_ranges) != len(output_ranges):
        raise ValueError("perceptual source/output ranges do not align")
    pairs: list[tuple[float, float, float, float]] = []
    for (source_start, source_end), (output_start, output_end) in zip(
        source_ranges, output_ranges, strict=True
    ):
        values = (source_start, source_end, output_start, output_end)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("perceptual ranges must be finite and non-negative")
        source_duration = source_end - source_start
        output_duration = output_end - output_start
        if source_duration <= 0 or not math.isclose(
            source_duration,
            output_duration,
            rel_tol=0.0,
            abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
        ):
            raise ValueError("perceptual source/output range durations differ")
        pairs.append((source_start, source_end, output_start, output_end))
    return tuple(pairs)


def _uniform_timestamp_cadence(timestamps: tuple[float, ...]) -> float:
    if len(timestamps) < 2 or any(
        not math.isfinite(value) or value < 0 for value in timestamps
    ):
        raise ValueError("perceptual video timestamps are incomplete")
    decimal_timestamps = tuple(Decimal(str(value)) for value in timestamps)
    deltas = tuple(
        current - previous
        for previous, current in zip(
            decimal_timestamps[:-1], decimal_timestamps[1:], strict=True
        )
    )
    if any(delta <= 0 for delta in deltas):
        raise ValueError("perceptual video timestamps are not strictly ordered")
    ordered_deltas = tuple(sorted(deltas))
    midpoint = len(ordered_deltas) // 2
    cadence = (
        ordered_deltas[midpoint]
        if len(ordered_deltas) % 2
        else (ordered_deltas[midpoint - 1] + ordered_deltas[midpoint]) / 2
    )
    tolerance = Decimal(str(_PERCEPTUAL_TIME_TOLERANCE_SECONDS))
    if any(abs(delta - cadence) > tolerance for delta in deltas):
        raise ValueError("perceptual video requires uniform decoded timestamps")
    return float(cadence)


def _aligned_timestamp_index_pairs(
    source_timestamps: tuple[float, ...],
    candidate_timestamps: tuple[float, ...],
    range_pair: tuple[float, float, float, float],
) -> tuple[tuple[int, int], ...]:
    """Select exact half-open decoded PTS and prove CFR correspondence."""
    source_start, source_end, output_start, output_end = range_pair
    source_cadence = _uniform_timestamp_cadence(source_timestamps)
    candidate_cadence = _uniform_timestamp_cadence(candidate_timestamps)
    if not math.isclose(
        source_cadence,
        candidate_cadence,
        rel_tol=0.0,
        abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
    ):
        raise ValueError("perceptual source/output timestamp cadences differ")
    source_terminal = source_timestamps[-1] + source_cadence
    candidate_terminal = candidate_timestamps[-1] + candidate_cadence
    if (
        source_start
        < source_timestamps[0] - ACTUAL_VIDEO_STREAM_START_TOLERANCE_SECONDS
        or output_start
        < candidate_timestamps[0] - ACTUAL_VIDEO_STREAM_START_TOLERANCE_SECONDS
        or source_end > source_terminal + _PERCEPTUAL_TIME_TOLERANCE_SECONDS
        or output_end > candidate_terminal + _PERCEPTUAL_TIME_TOLERANCE_SECONDS
    ):
        raise ValueError("perceptual ranges escape actual timestamp coverage")
    source_indices = tuple(
        index
        for index, timestamp in enumerate(source_timestamps)
        if source_start <= timestamp < source_end
    )
    candidate_indices = tuple(
        index
        for index, timestamp in enumerate(candidate_timestamps)
        if output_start <= timestamp < output_end
    )
    if not source_indices or len(source_indices) != len(candidate_indices):
        raise ValueError("perceptual timestamp inventories differ")
    aligned = tuple(zip(source_indices, candidate_indices, strict=True))
    if any(
        not math.isclose(
            source_timestamps[source_index] - source_start,
            candidate_timestamps[candidate_index] - output_start,
            rel_tol=0.0,
            abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
        )
        for source_index, candidate_index in aligned
    ):
        raise ValueError("perceptual source/output timestamps do not correspond")
    return aligned


@dataclass(frozen=True, slots=True)
class _VideoTimestampInventory:
    timestamps: tuple[float, ...]
    stream_start_seconds: float


def _probe_video_timestamps(
    path: Path,
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> tuple[float, ...]:
    return _probe_video_timestamp_inventory(
        path,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    ).timestamps


def _probe_video_timestamp_inventory(
    path: Path,
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> _VideoTimestampInventory:
    result = runner(
        (
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_frames",
            "-show_streams",
            "-show_entries",
            (
                "frame=best_effort_timestamp_time:stream=start_time,nb_read_frames:"
                "frame_side_data=:stream_tags=:stream_disposition=:"
                "stream_side_data="
            ),
            "-of",
            "compact=p=1:nk=1",
            str(path),
        ),
        timeout_seconds=timeout_seconds,
        sensitive_paths=(path,),
        cancellation_callback=cancellation_callback,
    )
    if result.returncode != 0:
        raise ValueError("perceptual frame timestamp probe failed")
    stdout = result.stdout_summary
    try:
        if (
            not stdout
            or not stdout.endswith(("\n", "\r"))
            or len(stdout.encode("utf-8")) > _PERCEPTUAL_MAX_TIMESTAMP_OUTPUT_BYTES
        ):
            raise ValueError
        lines = stdout.splitlines()
        if len(lines) < 3:
            raise ValueError
        frame_lines = lines[:-1]
        if len(frame_lines) > _PERCEPTUAL_MAX_FRAME_INVENTORY:
            raise ValueError
        stream_parts = lines[-1].split("|")
        if (
            len(stream_parts) != 3
            or stream_parts[0] != "stream"
            or not stream_parts[2].isdecimal()
        ):
            raise ValueError
        stream_start_seconds = float(stream_parts[1])
        if not math.isfinite(stream_start_seconds) or stream_start_seconds < 0:
            raise ValueError
        reported_count = int(stream_parts[2])
        if reported_count != len(frame_lines):
            raise ValueError
        timestamps_list: list[float] = []
        for line in frame_lines:
            fields = line.split("|")
            if (
                len(fields) not in {2, 3}
                or fields[0] != "frame"
                or (len(fields) == 3 and fields[2] != "")
            ):
                raise ValueError
            timestamp = float(fields[1])
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError
            if timestamps_list and timestamp <= timestamps_list[-1]:
                raise ValueError
            timestamps_list.append(timestamp)
        timestamps = normalize_actual_video_timestamps(
            timestamps_list,
            stream_start_seconds,
        )
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise ValueError("perceptual frame timestamp probe is incomplete") from exc
    _uniform_timestamp_cadence(timestamps)
    return _VideoTimestampInventory(
        timestamps=timestamps,
        stream_start_seconds=stream_start_seconds,
    )


def _iter_video_frames_by_index(
    path: Path,
    indices: tuple[int, ...],
    cancellation_callback: Callable[[], bool],
) -> Iterator[NDArray[np.generic]]:
    if not indices or any(
        current <= previous for previous, current in zip(indices, indices[1:])
    ):
        raise ValueError("perceptual frame indices must be strictly ordered")
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        capture.release()
        raise ValueError("perceptual video could not be opened")
    target_position = 0
    frame_index = 0
    try:
        while target_position < len(indices):
            if cancellation_callback():
                raise RescueCancelledError("perceptual video decode was cancelled")
            ok, frame = capture.read()
            if not ok:
                raise ValueError("perceptual range decode ended early")
            if frame_index == indices[target_position]:
                yield cast(NDArray[np.generic], frame)
                target_position += 1
            frame_index += 1
    finally:
        capture.release()


def _iter_aligned_perceptual_frames(
    source: Path,
    candidate: Path,
    range_pairs: tuple[tuple[float, float, float, float], ...],
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> Iterator[tuple[int, int, int, float, NDArray[np.generic], NDArray[np.generic]]]:
    source_timestamps = _probe_video_timestamps(
        source, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    candidate_timestamps = _probe_video_timestamps(
        candidate, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    inventory: list[tuple[int, int, int, int, int]] = []
    for range_index, range_pair in enumerate(range_pairs):
        aligned = _aligned_timestamp_index_pairs(
            source_timestamps, candidate_timestamps, range_pair
        )
        inventory.extend(
            (range_index, len(aligned), offset, source_index, candidate_index)
            for offset, (source_index, candidate_index) in enumerate(aligned)
        )
    source_indices = tuple(item[3] for item in inventory)
    candidate_indices = tuple(item[4] for item in inventory)
    source_frames = _iter_video_frames_by_index(
        source, source_indices, cancellation_callback
    )
    candidate_frames = _iter_video_frames_by_index(
        candidate, candidate_indices, cancellation_callback
    )
    for metadata, source_frame, candidate_frame in zip(
        inventory, source_frames, candidate_frames, strict=True
    ):
        if source_frame.shape != candidate_frame.shape:
            raise ValueError("perceptual video frame shapes differ")
        range_index, range_count, offset, _source_index, _candidate_index = metadata
        yield (
            range_index,
            range_count,
            offset,
            source_timestamps[metadata[3]],
            source_frame,
            candidate_frame,
        )


def _measure_deblur_pairs(
    source: Path,
    candidate: Path,
    pairs: tuple[tuple[float, float, float, float], ...],
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> dict[str, float]:
    widths: list[float] = []
    continuities: list[float] = []
    ringing: list[float] = []
    noise: list[float] = []
    temporal: list[float] = []
    expected = 0
    compared = 0
    previous_range = -1
    previous_residual: np.ndarray | None = None
    for (
        range_index,
        range_count,
        _offset,
        _source_timestamp,
        source_frame,
        candidate_frame,
    ) in _iter_aligned_perceptual_frames(
        Path(source),
        Path(candidate),
        pairs,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    ):
        if range_index != previous_range:
            expected += range_count
            previous_residual = None
            previous_range = range_index
        source_gray = _verification_gray(source_frame)
        candidate_gray = _verification_gray(candidate_frame)
        metrics = _independent_deblur_pair_metrics(source_gray, candidate_gray)
        widths.append(metrics["edge_width_ratio"])
        continuities.append(metrics["edge_continuity_ratio"])
        ringing.append(metrics["ringing_ratio"])
        noise.append(metrics["noise_gain_ratio"])
        residual = (candidate_gray - source_gray) / 255.0
        if previous_residual is not None:
            temporal.append(
                float(np.mean(np.abs(residual - previous_residual), dtype=np.float64))
            )
        previous_residual = residual
        compared += 1
    if compared < 2 or expected <= 0 or not temporal:
        raise ValueError("deblur ranges contain insufficient aligned frames")
    return {
        "range_coverage_ratio": compared / expected,
        "compared_frames": float(compared),
        "edge_width_ratio": float(np.median(widths)),
        "edge_continuity_ratio": float(np.percentile(continuities, 10)),
        "ringing_ratio": float(np.percentile(ringing, 95)),
        "noise_gain_ratio": float(np.percentile(noise, 95)),
        "temporal_change_ratio": float(np.percentile(temporal, 95)),
    }


def _measure_deblur_outcome(
    source: Path,
    candidate: Path,
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
    parameters: Mapping[str, JsonValue],
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> dict[str, float]:
    action_pairs = _paired_perceptual_ranges(source_ranges, output_ranges)
    operations: tuple[tuple[tuple[tuple[float, float], ...], DeblurConfig], ...]
    try:
        if parameters.get("algorithm_version") != "1":
            raise ValueError
        raw_operations = parameters.get("operations")
        if raw_operations is None:
            BlurKernelEstimate.model_validate_json(
                json.dumps(parameters["estimate"], ensure_ascii=False)
            )
            operations = (
                (
                    source_ranges,
                    DeblurConfig.model_validate_json(
                        json.dumps(parameters["config"], ensure_ascii=False)
                    ),
                ),
            )
        else:
            if not isinstance(raw_operations, (list, tuple)) or not raw_operations:
                raise ValueError
            parsed_operations: list[
                tuple[tuple[tuple[float, float], ...], DeblurConfig]
            ] = []
            for raw_operation in raw_operations:
                if not isinstance(raw_operation, Mapping) or set(raw_operation) != {
                    "source_ranges",
                    "estimate",
                    "config",
                }:
                    raise ValueError
                raw_ranges = raw_operation["source_ranges"]
                if not isinstance(raw_ranges, (list, tuple)) or not raw_ranges:
                    raise ValueError
                parsed_ranges: list[tuple[float, float]] = []
                for raw_range in raw_ranges:
                    if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
                        raise ValueError
                    start, end = raw_range
                    if (
                        not isinstance(start, (int, float))
                        or isinstance(start, bool)
                        or not isinstance(end, (int, float))
                        or isinstance(end, bool)
                    ):
                        raise ValueError
                    parsed_ranges.append((float(start), float(end)))
                BlurKernelEstimate.model_validate_json(
                    json.dumps(raw_operation["estimate"], ensure_ascii=False)
                )
                parsed_operations.append(
                    (
                        tuple(parsed_ranges),
                        DeblurConfig.model_validate_json(
                            json.dumps(raw_operation["config"], ensure_ascii=False)
                        ),
                    )
                )
            operations = tuple(parsed_operations)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("deblur verification parameters are invalid") from exc
    flattened = tuple(value for ranges, _config in operations for value in ranges)
    if (
        flattened != tuple(sorted(flattened))
        or any(
            current[0] < previous[1] - _PERCEPTUAL_TIME_TOLERANCE_SECONDS
            for previous, current in zip(flattened, flattened[1:])
        )
        or not _intervals_match_exactly(
            _interval_union(flattened), _interval_union(source_ranges)
        )
    ):
        raise ValueError("deblur operations do not exactly partition confirmed ranges")
    measurements: list[tuple[dict[str, float], DeblurConfig]] = []
    for ranges, config in operations:
        operation_pairs = tuple(
            (
                start,
                end,
                *_map_source_interval_to_output(start, end, action_pairs),
            )
            for start, end in ranges
        )
        measurements.append(
            (
                _measure_deblur_pairs(
                    source,
                    candidate,
                    operation_pairs,
                    ffprobe,
                    runner,
                    timeout_seconds,
                    cancellation_callback,
                ),
                config,
            )
        )
    edge_passed = sum(
        measured["edge_width_ratio"] <= config.maximum_edge_width_ratio
        and measured["edge_continuity_ratio"] >= config.minimum_edge_continuity_ratio
        for measured, config in measurements
    )
    ringing_passed = sum(
        measured["ringing_ratio"] <= config.maximum_ringing_ratio
        and measured["noise_gain_ratio"] <= config.maximum_noise_gain_ratio
        for measured, config in measurements
    )
    temporal_passed = sum(
        measured["temporal_change_ratio"] <= config.maximum_temporal_change_ratio
        for measured, config in measurements
    )
    return {
        "range_coverage_ratio": float(
            min(measured["range_coverage_ratio"] for measured, _config in measurements)
        ),
        "compared_frames": float(
            sum(measured["compared_frames"] for measured, _config in measurements)
        ),
        "operation_count": float(len(measurements)),
        "edge_recovery_passed_operations": float(edge_passed),
        "ringing_passed_operations": float(ringing_passed),
        "temporal_passed_operations": float(temporal_passed),
        "edge_width_ratio": float(
            max(measured["edge_width_ratio"] for measured, _config in measurements)
        ),
        "edge_continuity_ratio": float(
            min(measured["edge_continuity_ratio"] for measured, _config in measurements)
        ),
        "ringing_ratio": float(
            max(measured["ringing_ratio"] for measured, _config in measurements)
        ),
        "noise_gain_ratio": float(
            max(measured["noise_gain_ratio"] for measured, _config in measurements)
        ),
        "temporal_change_ratio": float(
            max(measured["temporal_change_ratio"] for measured, _config in measurements)
        ),
    }


def _affine_correction_residual_pixels(
    observed: Mapping[str, float],
    expected: MotionTransform,
    *,
    width: int,
    height: int,
    analysis_width: int,
    analysis_height: int,
    safe_crop_ratio: float,
) -> float:
    """Measure observed source-to-candidate affine against one bound correction."""

    def matrix(
        scale: float,
        rotation_degrees: float,
        translation_x: float,
        translation_y: float,
    ) -> NDArray[np.float64]:
        angle = math.radians(rotation_degrees)
        cosine = scale * math.cos(angle)
        sine = scale * math.sin(angle)
        return np.asarray(
            (
                (cosine, -sine, translation_x),
                (sine, cosine, translation_y),
                (0.0, 0.0, 1.0),
            ),
            dtype=np.float64,
        )

    observed_matrix = matrix(
        observed["scale"],
        observed["rotation_degrees"],
        observed["translation_x"],
        observed["translation_y"],
    )
    expected_matrix = matrix(
        expected.scale,
        expected.rotation_degrees,
        expected.translation_x * width / analysis_width,
        expected.translation_y * height / analysis_height,
    )
    if safe_crop_ratio > 0:
        zoom = 1.0 / max(1e-9, 1.0 - safe_crop_ratio)
        center_x = (width - 1) / 2.0
        center_y = (height - 1) / 2.0
        centered_zoom = np.asarray(
            (
                (zoom, 0.0, center_x * (1.0 - zoom)),
                (0.0, zoom, center_y * (1.0 - zoom)),
                (0.0, 0.0, 1.0),
            ),
            dtype=np.float64,
        )
        expected_matrix = centered_zoom @ expected_matrix
    points = np.asarray(
        (
            (0.0, 0.0, 1.0),
            (float(width - 1), 0.0, 1.0),
            (0.0, float(height - 1), 1.0),
            (float(width - 1), float(height - 1), 1.0),
            (float(width - 1) / 2.0, float(height - 1) / 2.0, 1.0),
        ),
        dtype=np.float64,
    )
    observed_points = (points @ observed_matrix.T)[:, :2]
    expected_points = (points @ expected_matrix.T)[:, :2]
    return float(np.max(np.linalg.norm(observed_points - expected_points, axis=1)))


def _anchor_safe_crop_ratio(
    corrections: tuple[MotionTransform, ...], config: StabilizationConfig
) -> float:
    active = tuple(
        correction
        for correction in corrections
        if max(
            abs(correction.translation_x),
            abs(correction.translation_y),
            abs(correction.rotation_degrees) * 2.0,
            abs(correction.scale - 1.0) * 100.0,
        )
        >= config.minimum_motion_amplitude_pixels
    )
    if not active:
        return 0.0
    translation = max(
        max(abs(item.translation_x) for item in active) / config.frame_width,
        max(abs(item.translation_y) for item in active) / config.frame_height,
    )
    rotation = max(abs(item.rotation_degrees) for item in active) / 180.0
    scale = max(abs(item.scale - 1.0) for item in active)
    return float(min(0.999999, translation + rotation + scale))


def _independent_expected_stabilization_frame(
    source_gray: NDArray[np.uint8],
    correction: MotionTransform,
    safe_crop_ratio: float,
) -> NDArray[np.uint8]:
    """Apply the confirmed affine definition without calling renderer helpers."""
    height, width = source_gray.shape
    radians = math.radians(correction.rotation_degrees)
    cosine = math.cos(radians) * correction.scale
    sine = math.sin(radians) * correction.scale
    matrix = np.asarray(
        [
            [cosine, -sine, correction.translation_x],
            [sine, cosine, correction.translation_y],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    if safe_crop_ratio > 0:
        zoom = 1.0 / (1.0 - safe_crop_ratio)
        center_x = (width - 1) / 2.0
        center_y = (height - 1) / 2.0
        matrix = (
            np.asarray(
                [
                    [zoom, 0.0, center_x * (1.0 - zoom)],
                    [0.0, zoom, center_y * (1.0 - zoom)],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            )
            @ matrix
        )
    expected = cv2.warpAffine(
        source_gray,
        np.asarray(matrix[:2, :], dtype=np.float32),
        (width, height),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return cast(NDArray[np.uint8], expected)


def _measure_outside_stabilization_freezes(
    source: Path,
    candidate: Path,
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> dict[str, float]:
    """Measure the exact actual-PTS complement instead of subtracting event totals."""
    source_timestamps = _probe_video_timestamps(
        source, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    candidate_timestamps = _probe_video_timestamps(
        candidate, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    if len(source_timestamps) != len(candidate_timestamps):
        raise ValueError("stabilization outside timestamp inventories differ")
    source_cadence = _uniform_timestamp_cadence(source_timestamps)
    candidate_cadence = _uniform_timestamp_cadence(candidate_timestamps)
    if not math.isclose(
        source_cadence,
        candidate_cadence,
        rel_tol=0.0,
        abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
    ):
        raise ValueError("stabilization outside timestamp cadences differ")
    source_origin = source_timestamps[0]
    candidate_origin = candidate_timestamps[0]
    if any(
        not math.isclose(
            source_timestamp - source_origin,
            candidate_timestamp - candidate_origin,
            rel_tol=0.0,
            abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
        )
        for source_timestamp, candidate_timestamp in zip(
            source_timestamps, candidate_timestamps, strict=True
        )
    ):
        raise ValueError("stabilization outside timestamps do not correspond")
    indices = tuple(range(len(source_timestamps)))
    source_frames = _iter_video_frames_by_index(source, indices, cancellation_callback)
    candidate_frames = _iter_video_frames_by_index(
        candidate, indices, cancellation_callback
    )
    source_events = 0
    candidate_events = 0
    candidate_duplicates = 0
    outside_expected = 0
    outside_compared = 0
    source_in_freeze = False
    candidate_in_freeze = False
    previous_source: NDArray[np.uint8] | None = None
    previous_candidate: NDArray[np.uint8] | None = None
    for (
        source_timestamp,
        candidate_timestamp,
        source_frame,
        candidate_frame,
    ) in zip(
        source_timestamps,
        candidate_timestamps,
        source_frames,
        candidate_frames,
        strict=True,
    ):
        source_inside = any(
            start <= source_timestamp < end for start, end in source_ranges
        )
        candidate_inside = any(
            start <= candidate_timestamp < end for start, end in output_ranges
        )
        if source_inside != candidate_inside:
            raise ValueError("stabilization outside range membership differs")
        if source_inside:
            previous_source = None
            previous_candidate = None
            source_in_freeze = False
            candidate_in_freeze = False
            continue
        source_gray = _verification_gray(source_frame).astype(np.uint8)
        candidate_gray = _verification_gray(candidate_frame).astype(np.uint8)
        outside_expected += 1
        outside_compared += 1
        if previous_source is not None and previous_candidate is not None:
            source_low = (
                float(
                    np.mean(
                        cv2.absdiff(source_gray, previous_source),
                        dtype=np.float64,
                    )
                )
                <= 0.5
            )
            candidate_low = (
                float(
                    np.mean(
                        cv2.absdiff(candidate_gray, previous_candidate),
                        dtype=np.float64,
                    )
                )
                <= 0.5
            )
            if source_low and not source_in_freeze:
                source_events += 1
            if candidate_low and not candidate_in_freeze:
                candidate_events += 1
            if candidate_low and np.array_equal(candidate_gray, previous_candidate):
                candidate_duplicates += 1
            source_in_freeze = source_low
            candidate_in_freeze = candidate_low
        previous_source = source_gray
        previous_candidate = candidate_gray
    return {
        "outside_range_coverage_ratio": (
            outside_compared / outside_expected if outside_expected else 1.0
        ),
        "outside_expected_frames": float(outside_expected),
        "outside_compared_frames": float(outside_compared),
        "source_outside_freeze_events": float(source_events),
        "candidate_outside_freeze_events": float(candidate_events),
        "outside_exact_duplicate_pairs": float(candidate_duplicates),
    }


def _measure_stabilization_freeze_attribution(
    source: Path,
    candidate: Path,
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
    parameters: Mapping[str, JsonValue],
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
    *,
    outside_control: Path | None = None,
) -> dict[str, float]:
    pairs = _paired_perceptual_ranges(source_ranges, output_ranges)
    try:
        config = StabilizationConfig.model_validate_json(
            json.dumps(parameters.get("config"), ensure_ascii=False)
        )
        raw_transforms = parameters.get("motion_transforms")
        if not isinstance(raw_transforms, (list, tuple)) or not raw_transforms:
            raise ValueError
        corrections = tuple(
            MotionTransform.model_validate_json(json.dumps(item, ensure_ascii=False))
            for item in raw_transforms
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("stabilization freeze parameters are invalid") from exc
    if (
        any(item.semantics != "frame_correction" for item in corrections)
        or any(
            current.timestamp_seconds <= previous.timestamp_seconds
            for previous, current in zip(corrections, corrections[1:], strict=False)
        )
        or not _intervals_match_exactly(
            _interval_union(config.accepted_ranges), _interval_union(source_ranges)
        )
    ):
        raise ValueError("stabilization freeze correction inventory is invalid")
    safe_crop_ratio = _anchor_safe_crop_ratio(corrections, config)
    candidate_near_static_threshold = config.residual_goal_median_pixels
    expected_near_static_threshold = config.residual_goal_p90_pixels
    expected_count = 0
    compared = 0
    previous_range = -1
    previous_full_source: NDArray[np.uint8] | None = None
    previous_full_candidate: NDArray[np.uint8] | None = None
    previous_candidate: NDArray[np.uint8] | None = None
    previous_expected: NDArray[np.uint8] | None = None
    previous_residual: float | None = None
    full_source_in_freeze = False
    full_candidate_in_freeze = False
    bounded_candidate_in_freeze = False
    source_events = 0
    candidate_events = 0
    attributed_candidate_events = 0
    explained_events = 0
    unexplained_pairs = 0
    exact_duplicates = 0
    candidate_expected_mae: list[float] = []
    for (
        range_index,
        range_count,
        _offset,
        source_timestamp,
        source_frame,
        candidate_frame,
    ) in _iter_aligned_perceptual_frames(
        source,
        candidate,
        pairs,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    ):
        if range_index != previous_range:
            expected_count += range_count
            previous_full_source = None
            previous_full_candidate = None
            previous_candidate = None
            previous_expected = None
            previous_residual = None
            full_source_in_freeze = False
            full_candidate_in_freeze = False
            bounded_candidate_in_freeze = False
            previous_range = range_index
        full_source = _verification_gray(source_frame).astype(np.uint8)
        full_candidate = _verification_gray(candidate_frame).astype(np.uint8)
        bounded_source, bounded_candidate = _bounded_transition_evidence_pair(
            source_frame,
            candidate_frame,
            config,
            require_exact_aspect=True,
        )
        matching = tuple(
            correction
            for correction in corrections
            if math.isclose(
                correction.timestamp_seconds,
                source_timestamp,
                rel_tol=0.0,
                abs_tol=config.exact_timestamp_tolerance_seconds,
            )
        )
        if len(matching) != 1:
            raise ValueError("stabilization freeze correction PTS is incomplete")
        correction = matching[0]
        if (
            correction.inlier_ratio < config.minimum_anchor_inlier_ratio
            or correction.residual_pixels > config.maximum_anchor_residual_pixels
        ):
            raise ValueError("stabilization freeze correction evidence is unreliable")
        expected_frame = _independent_expected_stabilization_frame(
            bounded_source,
            correction,
            safe_crop_ratio,
        )
        residual = float(
            np.mean(
                cv2.absdiff(bounded_candidate, expected_frame),
                dtype=np.float64,
            )
        )
        candidate_expected_mae.append(residual)
        if (
            previous_candidate is not None
            and previous_expected is not None
            and previous_residual is not None
        ):
            candidate_pair = float(
                np.mean(
                    cv2.absdiff(bounded_candidate, previous_candidate),
                    dtype=np.float64,
                )
            )
            expected_pair = float(
                np.mean(
                    cv2.absdiff(expected_frame, previous_expected),
                    dtype=np.float64,
                )
            )
            assert previous_full_source is not None
            assert previous_full_candidate is not None
            full_source_pair = float(
                np.mean(
                    cv2.absdiff(full_source, previous_full_source),
                    dtype=np.float64,
                )
            )
            full_candidate_pair = float(
                np.mean(
                    cv2.absdiff(full_candidate, previous_full_candidate),
                    dtype=np.float64,
                )
            )
            source_low = full_source_pair <= candidate_near_static_threshold
            full_candidate_low = full_candidate_pair <= candidate_near_static_threshold
            candidate_low = candidate_pair <= candidate_near_static_threshold
            expected_low = expected_pair <= expected_near_static_threshold
            if source_low and not full_source_in_freeze:
                source_events += 1
            if full_candidate_low and not full_candidate_in_freeze:
                candidate_events += 1
            candidate_event_start = candidate_low and not bounded_candidate_in_freeze
            if candidate_event_start:
                attributed_candidate_events += 1
            exact_duplicate = full_candidate_low and np.array_equal(
                full_candidate, previous_full_candidate
            )
            if exact_duplicate:
                exact_duplicates += 1
            explained = (
                candidate_low
                and expected_low
                and not exact_duplicate
                and residual <= config.maximum_transition_dense_residual_pixels
                and previous_residual <= config.maximum_transition_dense_residual_pixels
            )
            if candidate_event_start and explained:
                explained_events += 1
            if candidate_low and not explained:
                unexplained_pairs += 1
            full_source_in_freeze = source_low
            full_candidate_in_freeze = full_candidate_low
            bounded_candidate_in_freeze = candidate_low
        previous_full_source = full_source
        previous_full_candidate = full_candidate
        previous_candidate = bounded_candidate
        previous_expected = expected_frame
        previous_residual = residual
        compared += 1
    if (
        expected_count < 2
        or compared != expected_count
        or len(corrections) != expected_count
        or not candidate_expected_mae
    ):
        raise ValueError("stabilization freeze ranges lack exact frame coverage")
    outside_reference = outside_control if outside_control is not None else source
    outside_reference_ranges = (
        output_ranges if outside_control is not None else source_ranges
    )
    outside = _measure_outside_stabilization_freezes(
        outside_reference,
        candidate,
        outside_reference_ranges,
        output_ranges,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    )
    return {
        "range_coverage_ratio": compared / expected_count,
        "expected_frames": float(expected_count),
        "compared_frames": float(compared),
        "source_freeze_events": float(source_events),
        "candidate_freeze_events": float(candidate_events),
        "attributed_candidate_freeze_events": float(attributed_candidate_events),
        "explained_freeze_events": float(explained_events),
        "unexplained_near_static_pairs": float(unexplained_pairs),
        "exact_duplicate_pairs": float(exact_duplicates),
        "maximum_candidate_expected_mae": max(candidate_expected_mae),
        **outside,
    }


_SHARPEN_TOPOLOGY_FIELDS = (
    "avg_frame_rate",
    "chroma_location",
    "codec_name",
    "codec_tag_string",
    "color_primaries",
    "color_range",
    "color_space",
    "color_transfer",
    "field_order",
    "height",
    "level",
    "pix_fmt",
    "profile",
    "r_frame_rate",
    "sample_aspect_ratio",
    "time_base",
    "width",
)


def _probe_sharpen_video_topology(
    path: Path,
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> tuple[dict[str, JsonValue], str]:
    result = runner(
        (
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=" + ",".join(_SHARPEN_TOPOLOGY_FIELDS),
            "-of",
            "json",
            str(path),
        ),
        timeout_seconds=timeout_seconds,
        sensitive_paths=(path,),
        cancellation_callback=cancellation_callback,
    )
    if result.returncode != 0:
        raise ValueError("sharpness topology probe failed")
    try:
        payload = json.loads(result.stdout_summary)
        streams = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(streams, list) or len(streams) != 1:
            raise ValueError
        stream = streams[0]
        if not isinstance(stream, dict):
            raise ValueError
        topology: dict[str, JsonValue] = {
            field: cast(JsonValue, stream.get(field))
            for field in _SHARPEN_TOPOLOGY_FIELDS
        }
        required = (
            "codec_name",
            "pix_fmt",
            "width",
            "height",
            "time_base",
            "avg_frame_rate",
            "r_frame_rate",
        )
        if any(topology[field] in {None, "", 0} for field in required):
            raise ValueError
        encoded = json.dumps(
            topology,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("sharpness topology probe is incomplete") from exc
    return topology, sha256(encoded).hexdigest()


def _sharpen_frame_metrics(gray: NDArray[np.float32]) -> tuple[float, float, float]:
    clipped = float(np.count_nonzero((gray <= 1.0) | (gray >= 254.0))) / gray.size
    noise = (
        float(
            np.mean(
                cv2.absdiff(gray, cv2.GaussianBlur(gray, (3, 3), 0)),
                dtype=np.float64,
            )
        )
        / 255.0
    )
    sharpness = float(cv2.Laplacian(gray, cv2.CV_32F).var()) / (255.0 * 255.0)
    return sharpness, noise, clipped


@dataclass(frozen=True)
class _SharpenEdgeLocalMetrics:
    """Independent edge-local side effects relative to source/control evidence."""

    noise_increase: float
    overshoot_ratio: float
    overshoot_amplitude: float
    ringing_ratio: float


def _sharpen_edge_local_metrics(
    source_gray: NDArray[np.float32],
    control_gray: NDArray[np.float32],
    candidate_gray: NDArray[np.float32],
    config: SharpenConfig,
) -> _SharpenEdgeLocalMetrics:
    source = source_gray / 255.0
    control = control_gray / 255.0
    candidate = candidate_gray / 255.0
    gradients = []
    for reference in (source, control):
        gradient_x = cv2.Sobel(reference, cv2.CV_32F, 1, 0, ksize=3) / 4.0
        gradient_y = cv2.Sobel(reference, cv2.CV_32F, 0, 1, ksize=3) / 4.0
        gradients.append(cv2.magnitude(gradient_x, gradient_y))
    edge_core = np.maximum(gradients[0], gradients[1]) >= (
        config.edge_gradient_threshold
    )
    kernel_size = (2 * config.edge_neighborhood_radius) + 1
    kernel: NDArray[np.uint8] = np.ones((kernel_size, kernel_size), dtype=np.uint8)
    edge_band = cv2.dilate(edge_core.astype(np.uint8), kernel) > 0
    non_edge = ~edge_band
    if not np.any(non_edge):
        raise ValueError("sharpness measurement lacks non-edge noise samples")

    control_high_pass = np.abs(control - cv2.GaussianBlur(control, (3, 3), 0))
    candidate_high_pass = np.abs(candidate - cv2.GaussianBlur(candidate, (3, 3), 0))
    noise_increase = max(
        0.0,
        float(np.mean(candidate_high_pass[non_edge], dtype=np.float64))
        - float(np.mean(control_high_pass[non_edge], dtype=np.float64)),
    )

    edge_pixel_count = int(np.count_nonzero(edge_band))
    overshoot_ratio = 0.0
    overshoot_amplitude = 0.0
    ringing_ratio = 0.0
    if edge_pixel_count:
        reference_low = np.minimum(source, control)
        reference_high = np.maximum(source, control)
        local_low = cv2.erode(reference_low, kernel)
        local_high = cv2.dilate(reference_high, kernel)
        excursion = np.maximum(
            np.maximum(local_low - candidate, candidate - local_high), 0.0
        )
        edge_excursion = excursion[edge_band]
        overshoot_amplitude = float(np.max(edge_excursion, initial=0.0))
        overshoot_ratio = float(
            np.count_nonzero(edge_excursion >= config.edge_overshoot_minimum_amplitude)
        ) / float(edge_pixel_count)

        residual = candidate - control
        strong_residual = np.abs(residual) >= config.ringing_minimum_amplitude
        alternating = 0
        adjacent_edge_pairs = 0
        for first, second, first_edge, second_edge, first_strong, second_strong in (
            (
                residual[:, :-1],
                residual[:, 1:],
                edge_band[:, :-1],
                edge_band[:, 1:],
                strong_residual[:, :-1],
                strong_residual[:, 1:],
            ),
            (
                residual[:-1, :],
                residual[1:, :],
                edge_band[:-1, :],
                edge_band[1:, :],
                strong_residual[:-1, :],
                strong_residual[1:, :],
            ),
        ):
            edge_pairs = first_edge & second_edge
            adjacent_edge_pairs += int(np.count_nonzero(edge_pairs))
            alternating += int(
                np.count_nonzero(
                    edge_pairs & first_strong & second_strong & ((first * second) < 0.0)
                )
            )
        if adjacent_edge_pairs:
            ringing_ratio = alternating / float(adjacent_edge_pairs)
    return _SharpenEdgeLocalMetrics(
        noise_increase=noise_increase,
        overshoot_ratio=overshoot_ratio,
        overshoot_amplitude=overshoot_amplitude,
        ringing_ratio=ringing_ratio,
    )


def _luma_frame_metrics(frame: NDArray[np.uint8]) -> tuple[float, float, float]:
    gray = _verification_gray(frame)
    normalized = gray.astype(np.float64) / 255.0
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    return (
        float(np.percentile(normalized, 50)),
        float(np.mean(cv2.absdiff(gray, blurred), dtype=np.float64)) / 255.0,
        float(np.count_nonzero((gray <= 1) | (gray >= 254))) / float(gray.size),
    )


def _chroma_mae(first: NDArray[np.uint8], second: NDArray[np.uint8]) -> float:
    first_chroma = cv2.cvtColor(first, cv2.COLOR_BGR2YCrCb)[:, :, 1:]
    second_chroma = cv2.cvtColor(second, cv2.COLOR_BGR2YCrCb)[:, :, 1:]
    return float(np.mean(cv2.absdiff(first_chroma, second_chroma))) / 255.0


_NATIVE_CHROMA_TOPOLOGY_FIELDS = (
    "width",
    "height",
    "pix_fmt",
    "chroma_location",
    "color_range",
    "color_space",
    "color_primaries",
    "color_transfer",
)
# FFmpeg's metadata filter accepts one exact key, not a portable two-key
# selector that preserves one frame block. Accept only this frozen signalstats
# namespace and extract UAVG/VAVG; all other metadata remains fail-closed.
_NATIVE_SIGNALSTATS_KEYS = frozenset(
    {
        "BRNG",
        "HUEMED",
        "HUEAVG",
        "SATAVG",
        "SATHIGH",
        "SATLOW",
        "SATMAX",
        "SATMIN",
        "TOUT",
        "UAVG",
        "UBITDEPTH",
        "UDIF",
        "UHIGH",
        "ULOW",
        "UMAX",
        "UMIN",
        "VAVG",
        "VBITDEPTH",
        "VDIF",
        "VHIGH",
        "VLOW",
        "VMAX",
        "VMIN",
        "VREP",
        "YAVG",
        "YBITDEPTH",
        "YDIF",
        "YHIGH",
        "YLOW",
        "YMAX",
        "YMIN",
    }
)


def _measure_native_chroma_ranges(
    first: Path,
    second: Path,
    paired_ranges: tuple[tuple[float, float, float, float], ...],
    ffmpeg: str,
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> tuple[float, ...]:
    """Measure aligned native YUV420 U/V plane deltas without a BGR round-trip."""

    def number(value: float) -> str:
        return format(value, ".15g")

    if not paired_ranges:
        raise _LumaMeasurementError("native_chroma_ranges_invalid")
    try:
        first_topology, _first_topology_sha256 = _probe_sharpen_video_topology(
            first, ffprobe, runner, timeout_seconds, cancellation_callback
        )
        second_topology, _second_topology_sha256 = _probe_sharpen_video_topology(
            second, ffprobe, runner, timeout_seconds, cancellation_callback
        )
    except RescueCancelledError:
        raise
    except Exception as exc:
        raise _LumaMeasurementError("native_chroma_topology_invalid") from exc
    if (
        first_topology.get("pix_fmt") != "yuv420p"
        or second_topology.get("pix_fmt") != "yuv420p"
        or any(
            first_topology.get(field) != second_topology.get(field)
            for field in _NATIVE_CHROMA_TOPOLOGY_FIELDS
        )
    ):
        raise _LumaMeasurementError("native_chroma_topology_invalid")
    try:
        first_inventory = _probe_video_timestamp_inventory(
            first, ffprobe, runner, timeout_seconds, cancellation_callback
        )
        second_inventory = _probe_video_timestamp_inventory(
            second, ffprobe, runner, timeout_seconds, cancellation_callback
        )
    except RescueCancelledError:
        raise
    except Exception as exc:
        raise _LumaMeasurementError("native_chroma_timestamps_invalid") from exc
    measured: list[float] = []
    for paired_range in paired_ranges:
        try:
            aligned = _aligned_timestamp_index_pairs(
                first_inventory.timestamps,
                second_inventory.timestamps,
                paired_range,
            )
        except ValueError as exc:
            raise _LumaMeasurementError("native_chroma_timestamps_invalid") from exc
        first_start, first_end, second_start, second_end = paired_range
        graph = (
            f"[0:v]setpts=PTS-{number(first_inventory.stream_start_seconds)}/TB,"
            f"trim=start={number(first_start)}:end={number(first_end)},"
            f"setpts=PTS-{number(first_start)}/TB[first];"
            f"[1:v]setpts=PTS-{number(second_inventory.stream_start_seconds)}/TB,"
            f"trim=start={number(second_start)}:end={number(second_end)},"
            f"setpts=PTS-{number(second_start)}/TB[second];"
            "[first][second]blend=all_mode=difference,signalstats,"
            "metadata=mode=print:file=-[out]"
        )
        try:
            result = runner(
                (
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-i",
                    str(first),
                    "-i",
                    str(second),
                    "-filter_complex",
                    graph,
                    "-map",
                    "[out]",
                    "-an",
                    "-fps_mode",
                    "passthrough",
                    "-f",
                    "null",
                    "-",
                ),
                timeout_seconds=timeout_seconds,
                sensitive_paths=(first, second),
                cancellation_callback=cancellation_callback,
            )
        except RescueCancelledError:
            raise
        except Exception as exc:
            raise _LumaMeasurementError("native_chroma_command_failed") from exc
        if result.returncode != 0:
            raise _LumaMeasurementError("native_chroma_command_failed")
        stdout = result.stdout_summary
        try:
            if (
                not stdout
                or not stdout.endswith(("\n", "\r"))
                or len(stdout.encode("utf-8")) > _PERCEPTUAL_MAX_TIMESTAMP_OUTPUT_BYTES
            ):
                raise ValueError
            frames: list[tuple[float, float, float]] = []
            current_pts: float | None = None
            current_u: float | None = None
            current_v: float | None = None
            seen_keys: set[str] = set()
            for line in stdout.splitlines():
                if line.startswith("frame:"):
                    if current_pts is not None:
                        if current_u is None or current_v is None:
                            raise ValueError
                        frames.append((current_pts, current_u, current_v))
                    fields = line.split()
                    pts_fields = [
                        field.removeprefix("pts_time:")
                        for field in fields
                        if field.startswith("pts_time:")
                    ]
                    if len(pts_fields) != 1:
                        raise ValueError
                    current_pts = float(pts_fields[0])
                    current_u = None
                    current_v = None
                    seen_keys = set()
                elif line.startswith("lavfi.signalstats."):
                    prefix, separator, raw_value = line.partition("=")
                    key = prefix.removeprefix("lavfi.signalstats.")
                    if (
                        current_pts is None
                        or separator != "="
                        or key not in _NATIVE_SIGNALSTATS_KEYS
                        or key in seen_keys
                    ):
                        raise ValueError
                    value = float(raw_value)
                    if not math.isfinite(value):
                        raise ValueError
                    seen_keys.add(key)
                    if key == "UAVG":
                        current_u = value
                    elif key == "VAVG":
                        current_v = value
                else:
                    raise ValueError
            if current_pts is None or current_u is None or current_v is None:
                raise ValueError
            frames.append((current_pts, current_u, current_v))
            if len(frames) != len(aligned):
                raise ValueError
            expected_pts = tuple(
                first_inventory.timestamps[first_index] - first_start
                for first_index, _second_index in aligned
            )
            if any(
                not all(math.isfinite(value) for value in frame)
                or frame[1] < 0
                or frame[1] > 255
                or frame[2] < 0
                or frame[2] > 255
                or not math.isclose(
                    frame[0],
                    expected,
                    rel_tol=0.0,
                    abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
                )
                for frame, expected in zip(frames, expected_pts, strict=True)
            ):
                raise ValueError
        except (UnicodeEncodeError, TypeError, ValueError) as exc:
            raise _LumaMeasurementError("native_chroma_metadata_invalid") from exc
        measured.append(max((u + v) / (2.0 * 255.0) for _pts, u, v in frames))
    return tuple(measured)


def _measure_luma_adjustment(
    source: Path,
    control: Path,
    candidate: Path,
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
    parameters: Mapping[str, JsonValue],
    ffmpeg: str,
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> dict[str, JsonValue]:
    luma_action_wire_from_parameters(parameters)
    source_control_pairs = _paired_perceptual_ranges(source_ranges, output_ranges)
    control_candidate_pairs = _paired_perceptual_ranges(output_ranges, output_ranges)
    _control_topology, control_topology_sha256 = _probe_sharpen_video_topology(
        control, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    _candidate_topology, candidate_topology_sha256 = _probe_sharpen_video_topology(
        candidate, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    if control_topology_sha256 != candidate_topology_sha256:
        raise ValueError("luma control/candidate topology differs")
    control_sha256 = _stream_hash(control)
    candidate_sha256 = _stream_hash(candidate)
    if control_sha256 is None or candidate_sha256 is None:
        raise ValueError("luma artifact identity is unavailable")
    source_control_chroma = _measure_native_chroma_ranges(
        source,
        control,
        source_control_pairs,
        ffmpeg,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    )
    control_candidate_chroma = _measure_native_chroma_ranges(
        control,
        candidate,
        control_candidate_pairs,
        ffmpeg,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    )
    expected = 0
    compared = 0
    previous_range = -1
    per_range: list[dict[str, list[float]]] = []
    source_control_frames = _iter_aligned_perceptual_frames(
        source,
        control,
        source_control_pairs,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    )
    control_candidate_frames = _iter_aligned_perceptual_frames(
        control,
        candidate,
        control_candidate_pairs,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    )
    for source_control, control_candidate in zip(
        source_control_frames, control_candidate_frames, strict=True
    ):
        range_index, range_count, offset, _source_pts, source_frame, first_control = (
            source_control
        )
        (
            candidate_range_index,
            candidate_range_count,
            candidate_offset,
            _control_pts,
            second_control,
            candidate_frame,
        ) = control_candidate
        if (
            candidate_range_index != range_index
            or candidate_range_count != range_count
            or candidate_offset != offset
            or not np.array_equal(first_control, second_control)
        ):
            raise ValueError("luma triple-frame inventory differs")
        if range_index != previous_range:
            if range_index != len(per_range):
                raise ValueError("luma range inventory is not ordered")
            expected += range_count
            per_range.append(
                {
                    "source_luma": [],
                    "control_luma": [],
                    "candidate_luma": [],
                    "control_noise": [],
                    "candidate_noise": [],
                    "control_clipping": [],
                    "candidate_clipping": [],
                }
            )
            previous_range = range_index
        source_values = _luma_frame_metrics(source_frame)
        control_values = _luma_frame_metrics(first_control)
        candidate_values = _luma_frame_metrics(candidate_frame)
        inventory = per_range[range_index]
        inventory["source_luma"].append(source_values[0])
        inventory["control_luma"].append(control_values[0])
        inventory["candidate_luma"].append(candidate_values[0])
        inventory["control_noise"].append(control_values[1])
        inventory["candidate_noise"].append(candidate_values[1])
        inventory["control_clipping"].append(control_values[2])
        inventory["candidate_clipping"].append(candidate_values[2])
        compared += 1
    if compared != expected or len(per_range) != len(source_ranges):
        raise ValueError("luma ranges lack exact frame coverage")
    measurements: list[dict[str, float]] = []
    for range_index, inventory in enumerate(per_range):
        frame_count = len(inventory["control_luma"])
        if frame_count == 0 or any(
            len(values) != frame_count for values in inventory.values()
        ):
            raise ValueError("luma range measurement is incomplete")
        control_luma = float(np.median(inventory["control_luma"]))
        candidate_luma = float(np.median(inventory["candidate_luma"]))
        measurements.append(
            {
                "frame_count": float(frame_count),
                "source_luma_p50": float(np.median(inventory["source_luma"])),
                "control_luma_p50": control_luma,
                "candidate_luma_p50": candidate_luma,
                "luma_delta": candidate_luma - control_luma,
                "noise_increase": float(np.mean(inventory["candidate_noise"]))
                - float(np.mean(inventory["control_noise"])),
                "clipping_increase": float(np.mean(inventory["candidate_clipping"]))
                - float(np.mean(inventory["control_clipping"])),
                "source_control_chroma_shift": source_control_chroma[range_index],
                "control_candidate_chroma_shift": control_candidate_chroma[range_index],
            }
        )
    payload: dict[str, JsonValue] = {
        "range_coverage_ratio": compared / expected,
        "expected_frames": float(expected),
        "compared_frames": float(compared),
        "range_count": float(len(measurements)),
        "minimum_luma_delta": min(value["luma_delta"] for value in measurements),
        "maximum_luma_delta": max(value["luma_delta"] for value in measurements),
        "maximum_noise_increase": max(
            value["noise_increase"] for value in measurements
        ),
        "maximum_clipping_increase": max(
            value["clipping_increase"] for value in measurements
        ),
        "maximum_chroma_shift": max(
            value["control_candidate_chroma_shift"] for value in measurements
        ),
        "maximum_source_control_chroma_shift": max(
            value["source_control_chroma_shift"] for value in measurements
        ),
        "control_sha256": control_sha256,
        "candidate_sha256": candidate_sha256,
        "control_topology_sha256": control_topology_sha256,
        "candidate_topology_sha256": candidate_topology_sha256,
    }
    for index, measurement in enumerate(measurements):
        for key, value in measurement.items():
            payload[f"range_{index}_{key}"] = value
    return payload


def _measure_sharpen_improvement(
    source: Path,
    control: Path,
    candidate: Path,
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
    parameters: Mapping[str, JsonValue],
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
    *,
    decoded_source_baseline: bool = False,
) -> dict[str, JsonValue]:
    minimum_gain = _number(parameters.get("minimum_perceptible_sharpness_gain_ratio"))
    minimum_recovery = _number(parameters.get("minimum_recovered_baseline_ratio"))
    scene_baseline = _number(parameters.get("scene_baseline_sharpness"))
    maximum_noise = _number(parameters.get("maximum_noise_increase"))
    required_values = (
        minimum_gain,
        minimum_recovery,
        maximum_noise,
        *((scene_baseline,) if not decoded_source_baseline else ()),
    )
    if any(value is None for value in required_values):
        raise ValueError("sharpness verification parameters are incomplete")
    assert minimum_gain is not None and minimum_recovery is not None
    if not decoded_source_baseline:
        assert scene_baseline is not None
    assert maximum_noise is not None
    if (
        not decoded_source_baseline
        and scene_baseline is not None
        and scene_baseline <= 0
    ):
        raise ValueError("sharpness decoded scene baseline is invalid")
    config_values = {
        key: value
        for key, value in parameters.items()
        if key in SharpenConfig.model_fields
    }
    config = SharpenConfig.model_validate(config_values)
    source_control_pairs = _paired_perceptual_ranges(
        source_ranges,
        output_ranges,
    )
    control_candidate_pairs = _paired_perceptual_ranges(
        output_ranges,
        output_ranges,
    )
    source_topology, source_topology_sha256 = _probe_sharpen_video_topology(
        source, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    _control_topology, control_topology_sha256 = _probe_sharpen_video_topology(
        control, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    _candidate_topology, candidate_topology_sha256 = _probe_sharpen_video_topology(
        candidate, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    if control_topology_sha256 != candidate_topology_sha256 or (
        decoded_source_baseline and source_topology_sha256 != control_topology_sha256
    ):
        raise ValueError("sharpness control/candidate topology differs")
    source_inventory = _probe_video_timestamp_inventory(
        source, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    control_inventory = _probe_video_timestamp_inventory(
        control, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    candidate_inventory = _probe_video_timestamp_inventory(
        candidate, ffprobe, runner, timeout_seconds, cancellation_callback
    )
    if decoded_source_baseline and not (
        source_inventory.timestamps
        == control_inventory.timestamps
        == candidate_inventory.timestamps
    ):
        raise ValueError("sharpness qualification PTS inventory differs")
    control_sha256 = _stream_hash(control)
    candidate_sha256 = _stream_hash(candidate)
    if control_sha256 is None or candidate_sha256 is None:
        raise ValueError("sharpness artifact identity is unavailable")
    expected = 0
    compared = 0
    previous_range = -1
    per_range: list[dict[str, list[float]]] = []
    source_control_frames = _iter_aligned_perceptual_frames(
        source,
        control,
        source_control_pairs,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    )
    control_candidate_frames = _iter_aligned_perceptual_frames(
        control,
        candidate,
        control_candidate_pairs,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    )
    for source_control, control_candidate in zip(
        source_control_frames, control_candidate_frames, strict=True
    ):
        (
            range_index,
            range_count,
            offset,
            _source_timestamp,
            source_frame,
            first_control_frame,
        ) = source_control
        (
            candidate_range_index,
            candidate_range_count,
            candidate_offset,
            _control_timestamp,
            second_control_frame,
            candidate_frame,
        ) = control_candidate
        if (
            candidate_range_index != range_index
            or candidate_range_count != range_count
            or candidate_offset != offset
            or not np.array_equal(first_control_frame, second_control_frame)
        ):
            raise ValueError("sharpness triple-frame inventory differs")
        if range_index != previous_range:
            if range_index != len(per_range):
                raise ValueError("sharpness range inventory is not ordered")
            expected += range_count
            per_range.append(
                {
                    "source_sharpness": [],
                    "control_sharpness": [],
                    "candidate_sharpness": [],
                    "noise_increase": [],
                    "edge_overshoot_ratio": [],
                    "edge_overshoot_amplitude": [],
                    "ringing_ratio": [],
                }
            )
            previous_range = range_index
        source_gray = _verification_gray(source_frame)
        control_gray = _verification_gray(first_control_frame)
        candidate_gray = _verification_gray(candidate_frame)
        source_values = _sharpen_frame_metrics(source_gray)
        control_values = _sharpen_frame_metrics(control_gray)
        candidate_values = _sharpen_frame_metrics(candidate_gray)
        side_effects = _sharpen_edge_local_metrics(
            source_gray, control_gray, candidate_gray, config
        )
        inventory = per_range[range_index]
        inventory["source_sharpness"].append(source_values[0])
        inventory["control_sharpness"].append(control_values[0])
        inventory["candidate_sharpness"].append(candidate_values[0])
        inventory["noise_increase"].append(side_effects.noise_increase)
        inventory["edge_overshoot_ratio"].append(side_effects.overshoot_ratio)
        inventory["edge_overshoot_amplitude"].append(side_effects.overshoot_amplitude)
        inventory["ringing_ratio"].append(side_effects.ringing_ratio)
        compared += 1
    if compared != expected or len(per_range) != len(source_ranges):
        raise ValueError("sharpness ranges lack exact frame coverage")
    measurements: list[dict[str, float]] = []
    for inventory in per_range:
        frame_count = len(inventory["control_sharpness"])
        if frame_count == 0 or any(
            len(values) != frame_count for values in inventory.values()
        ):
            raise ValueError("sharpness range measurement is incomplete")
        source_mean = float(np.mean(inventory["source_sharpness"]))
        control_mean = float(np.mean(inventory["control_sharpness"]))
        candidate_mean = float(np.mean(inventory["candidate_sharpness"]))
        aggregate_gain = (
            (candidate_mean - control_mean) / control_mean if control_mean > 0 else -1.0
        )
        improved_frames = sum(
            candidate_value >= control_value * (1.0 + minimum_gain)
            for control_value, candidate_value in zip(
                inventory["control_sharpness"],
                inventory["candidate_sharpness"],
                strict=True,
            )
        )
        frame_fraction = improved_frames / frame_count
        recovery_baseline = source_mean if decoded_source_baseline else scene_baseline
        assert recovery_baseline is not None
        recovered_baseline_ratio = candidate_mean / recovery_baseline
        noise_increase = max(inventory["noise_increase"])
        overshoot_ratio = max(inventory["edge_overshoot_ratio"])
        overshoot_amplitude = max(inventory["edge_overshoot_amplitude"])
        ringing_ratio = max(inventory["ringing_ratio"])
        measurements.append(
            {
                "frame_count": float(frame_count),
                "source_sharpness": source_mean,
                "control_sharpness": control_mean,
                "candidate_sharpness": candidate_mean,
                "aggregate_gain_ratio": aggregate_gain,
                "recovered_baseline_ratio": recovered_baseline_ratio,
                "improved_frame_fraction": frame_fraction,
                "noise_increase": noise_increase,
                "edge_overshoot_ratio": overshoot_ratio,
                "edge_overshoot_amplitude": overshoot_amplitude,
                "ringing_ratio": ringing_ratio,
            }
        )
    passing_ranges = sum(
        value["aggregate_gain_ratio"] >= minimum_gain
        and value["recovered_baseline_ratio"] >= minimum_recovery
        and value["improved_frame_fraction"] >= config.minimum_improved_frame_fraction
        and value["noise_increase"] <= maximum_noise
        and value["edge_overshoot_ratio"] <= config.maximum_edge_overshoot_ratio
        and value["edge_overshoot_amplitude"] <= config.maximum_edge_overshoot_amplitude
        and value["ringing_ratio"] <= config.maximum_ringing_ratio
        for value in measurements
    )
    normalized_pts_digest = sha256(
        json.dumps(
            source_inventory.timestamps,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    payload: dict[str, JsonValue] = {
        "range_coverage_ratio": compared / expected,
        "expected_frames": float(expected),
        "compared_frames": float(compared),
        "range_count": float(len(measurements)),
        "passing_range_count": float(passing_ranges),
        "minimum_aggregate_gain_ratio": min(
            value["aggregate_gain_ratio"] for value in measurements
        ),
        "minimum_recovered_baseline_ratio": min(
            value["recovered_baseline_ratio"] for value in measurements
        ),
        "minimum_improved_frame_fraction": min(
            value["improved_frame_fraction"] for value in measurements
        ),
        "maximum_noise_increase": max(
            value["noise_increase"] for value in measurements
        ),
        "maximum_edge_overshoot_ratio": max(
            value["edge_overshoot_ratio"] for value in measurements
        ),
        "maximum_edge_overshoot_amplitude": max(
            value["edge_overshoot_amplitude"] for value in measurements
        ),
        "maximum_ringing_ratio": max(value["ringing_ratio"] for value in measurements),
        "control_sha256": control_sha256,
        "candidate_sha256": candidate_sha256,
        "control_topology_sha256": control_topology_sha256,
        "candidate_topology_sha256": candidate_topology_sha256,
        "baseline_sha256": _stream_hash(source),
        "baseline_topology_sha256": source_topology_sha256,
        "baseline_normalized_pts_digest": normalized_pts_digest,
        "control_normalized_pts_digest": normalized_pts_digest,
        "candidate_normalized_pts_digest": normalized_pts_digest,
        "baseline_frame_count": len(source_inventory.timestamps),
        "control_frame_count": len(control_inventory.timestamps),
        "candidate_frame_count": len(candidate_inventory.timestamps),
        "decoded_width": source_topology.get("width"),
        "decoded_height": source_topology.get("height"),
        "normalized_pts_digest": normalized_pts_digest,
        "inventory_frame_count": len(source_inventory.timestamps),
    }
    for index, range_measurement in enumerate(measurements):
        for key, metric in range_measurement.items():
            payload[f"range_{index}_{key}"] = metric
    return payload


def _measure_anchor_outcome(
    source: Path,
    candidate: Path,
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
    parameters: Mapping[str, JsonValue],
    ffprobe: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> dict[str, float]:
    pairs = _paired_perceptual_ranges(source_ranges, output_ranges)
    transition_bounds: tuple[float, float] | None = None
    try:
        if (
            parameters.get("method") not in {"anchor_v1", "transition_anchor_v1"}
            or parameters.get("algorithm_version") != "1"
        ):
            raise ValueError
        config = StabilizationConfig.model_validate_json(
            json.dumps(parameters.get("config"), ensure_ascii=False)
        )
        raw_transforms = parameters.get("motion_transforms")
        if not isinstance(raw_transforms, (list, tuple)) or not raw_transforms:
            raise ValueError
        corrections = tuple(
            MotionTransform.model_validate_json(json.dumps(item, ensure_ascii=False))
            for item in raw_transforms
        )
        if any(item.semantics != "frame_correction" for item in corrections):
            raise ValueError
        if parameters.get("method") == "transition_anchor_v1":
            transition = parameters.get("transition_range")
            following = parameters.get("following_anchor_range")
            declared_count = parameters.get("transition_correction_count")
            if (
                parameters.get("estimator_algorithm_version") != "transition_anchor_v1"
                or not isinstance(transition, (list, tuple))
                or len(transition) != 2
                or not isinstance(following, (list, tuple))
                or len(following) != 2
                or isinstance(declared_count, bool)
                or not isinstance(declared_count, int)
                or declared_count != len(corrections)
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in (*transition, *following)
                )
            ):
                raise ValueError
            numeric_transition = cast(Sequence[int | float], transition)
            numeric_following = cast(Sequence[int | float], following)
            transition_start, transition_end = (
                float(value) for value in numeric_transition
            )
            following_start, following_end = (
                float(value) for value in numeric_following
            )
            if (
                not all(
                    math.isfinite(value)
                    for value in (
                        transition_start,
                        transition_end,
                        following_start,
                        following_end,
                    )
                )
                or transition_start >= transition_end
                or following_start >= following_end
                or not math.isclose(
                    transition_end,
                    following_start,
                    rel_tol=0.0,
                    abs_tol=config.exact_timestamp_tolerance_seconds,
                )
                or not _intervals_match_exactly(
                    ((transition_start, following_end),), source_ranges
                )
            ):
                raise ValueError
            transition_bounds = (transition_start, transition_end)
    except (TypeError, ValueError) as exc:
        raise ValueError("anchor verification parameters are invalid") from exc
    if not _intervals_match_exactly(
        _interval_union(config.accepted_ranges), _interval_union(source_ranges)
    ):
        raise ValueError("anchor config does not exactly cover confirmed ranges")
    safe_crop_ratio = _anchor_safe_crop_ratio(corrections, config)
    expected = 0
    compared = 0
    reliable = 0
    previous_range = -1
    residuals: list[float] = []
    crop_ratios: list[float] = []
    transition_pixels: list[tuple[float, NDArray[np.uint8], NDArray[np.uint8]]] = []
    transition_pixel_budget = (
        config.maximum_transition_candidate_frames
        * config.frame_width
        * config.frame_height
        * 2
    )
    retained_transition_pixels = 0
    for (
        range_index,
        range_count,
        _offset,
        source_timestamp,
        source_frame,
        candidate_frame,
    ) in _iter_aligned_perceptual_frames(
        Path(source),
        Path(candidate),
        pairs,
        ffprobe,
        runner,
        timeout_seconds,
        cancellation_callback,
    ):
        if range_index != previous_range:
            expected += range_count
            previous_range = range_index
        bounded_source, bounded_candidate = _bounded_transition_evidence_pair(
            source_frame,
            candidate_frame,
            config,
            require_exact_aspect=transition_bounds is not None,
        )
        source_gray = _verification_gray(bounded_source)
        candidate_gray = _verification_gray(bounded_candidate)
        if transition_bounds is not None:
            transition_start, transition_end = transition_bounds
            if transition_start <= source_timestamp < transition_end or (
                source_timestamp >= transition_end
                and not any(
                    timestamp >= transition_end
                    for timestamp, _source, _candidate in transition_pixels
                )
            ):
                pair_pixels = config.frame_width * config.frame_height * 2
                if retained_transition_pixels + pair_pixels > transition_pixel_budget:
                    raise ValueError(
                        "transition verification pixel inventory exceeds its maximum"
                    )
                transition_pixels.append(
                    (source_timestamp, bounded_source, bounded_candidate)
                )
                retained_transition_pixels += pair_pixels
        source_alignment = _independent_affine_measurement(source_gray, candidate_gray)
        compared += 1
        if source_alignment is None:
            continue
        scale = source_alignment["scale"]
        if not math.isfinite(scale) or scale <= 0:
            continue
        matching = tuple(
            correction
            for correction in corrections
            if math.isclose(
                correction.timestamp_seconds,
                source_timestamp,
                rel_tol=0.0,
                abs_tol=config.exact_timestamp_tolerance_seconds,
            )
        )
        if len(matching) != 1:
            continue
        correction_scale = matching[0].scale
        observed_zoom = scale / correction_scale
        if not math.isfinite(observed_zoom) or observed_zoom <= 0:
            continue
        crop_ratios.append(
            max(0.0, 1.0 - 1.0 / observed_zoom) if observed_zoom >= 1.0 else 0.0
        )
        residual = _affine_correction_residual_pixels(
            source_alignment,
            matching[0],
            width=source_gray.shape[1],
            height=source_gray.shape[0],
            analysis_width=config.frame_width,
            analysis_height=config.frame_height,
            safe_crop_ratio=safe_crop_ratio,
        )
        residuals.append(residual)
        reliable += 1
    if (
        expected < 2
        or compared != expected
        or reliable != expected
        or len(corrections) != expected
        or not crop_ratios
    ):
        raise ValueError("anchor ranges lack exact reliable frame coverage")
    measured = {
        "range_coverage_ratio": compared / expected,
        "expected_frames": float(expected),
        "reliable_transforms": float(reliable),
        "residual_median_pixels": float(np.median(residuals)),
        "residual_p90_pixels": float(np.percentile(residuals, 90)),
        "crop_ratio": float(np.percentile(crop_ratios, 95)),
        "expected_crop_ratio": safe_crop_ratio,
        "crop_error_ratio": float(
            max(abs(crop_ratio - safe_crop_ratio) for crop_ratio in crop_ratios)
        ),
    }
    if transition_bounds is not None:
        transition_start, transition_end = transition_bounds
        expected_transition = tuple(
            correction
            for correction in corrections
            if transition_start <= correction.timestamp_seconds < transition_end
        )
        measured_transition = tuple(
            item for item in transition_pixels if item[0] < transition_end
        )
        following_pixels = tuple(
            item for item in transition_pixels if item[0] >= transition_end
        )
        if (
            not expected_transition
            or len(measured_transition) != len(expected_transition)
            or len(following_pixels) != 1
        ):
            raise ValueError("transition verification lacks exact source-rate evidence")
        source_evidence_frames = tuple(
            (timestamp, source_gray)
            for timestamp, source_gray, _candidate_gray in (
                *measured_transition,
                following_pixels[0],
            )
        )
        source_consensus = measure_transition_source_consensus(
            source_evidence_frames,
            config,
            cancellation_callback=cancellation_callback,
        )
        if len(source_consensus) != len(expected_transition):
            raise ValueError("transition source consensus is incomplete")
        correction_residuals: list[float] = []
        for step in source_consensus:
            previous = tuple(
                item
                for item in corrections
                if math.isclose(
                    item.timestamp_seconds,
                    step.previous_timestamp_seconds,
                    rel_tol=0.0,
                    abs_tol=config.exact_timestamp_tolerance_seconds,
                )
            )
            current = tuple(
                item
                for item in corrections
                if math.isclose(
                    item.timestamp_seconds,
                    step.current_timestamp_seconds,
                    rel_tol=0.0,
                    abs_tol=config.exact_timestamp_tolerance_seconds,
                )
            )
            if len(previous) != 1 or len(current) != 1:
                raise ValueError("transition correction consensus is incomplete")
            correction_residuals.append(
                math.hypot(
                    (previous[0].translation_x - current[0].translation_x)
                    - step.translation_x,
                    (previous[0].translation_y - current[0].translation_y)
                    - step.translation_y,
                )
            )
        seam_alignment = _independent_affine_measurement(
            measured_transition[-1][2], following_pixels[0][2]
        )
        if seam_alignment is None:
            raise ValueError("transition output seam measurement is inconclusive")
        accepted_consensus = sum(
            residual <= config.maximum_transition_vector_disagreement_pixels
            for residual in correction_residuals
        )
        boundary_source = source_consensus[-1]
        measured.update(
            {
                "transition_consensus_coverage_ratio": (
                    accepted_consensus / len(expected_transition)
                ),
                "transition_consensus_p90_pixels": float(
                    np.percentile(
                        [step.residual_pixels for step in source_consensus],
                        90,
                    )
                ),
                "transition_boundary_source_translation_x": (
                    boundary_source.translation_x
                ),
                "transition_boundary_source_translation_y": (
                    boundary_source.translation_y
                ),
                "transition_seam_residual_pixels": seam_alignment["translation_pixels"],
                "transition_expected_frames": float(len(expected_transition)),
                "transition_reliable_frames": float(len(source_consensus)),
            }
        )
    return measured


def _decode_audio_segment(
    path: Path,
    start_seconds: float,
    end_seconds: float,
    ffmpeg: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
) -> tuple[np.ndarray, int]:
    if end_seconds <= start_seconds:
        raise ValueError("audio verification range is empty")
    with tempfile.TemporaryDirectory(prefix="videoscope-tonal-verify-") as raw:
        root = Path(raw)
        output = root / "segment.wav"
        audio_filter = (
            f"atrim=start={start_seconds:.9f}:end={end_seconds:.9f},"
            "asetpts=PTS-STARTPTS"
        )
        result = runner(
            (
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-xerror",
                "-nostdin",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-af",
                audio_filter,
                "-c:a",
                "pcm_s16le",
                str(output),
            ),
            timeout_seconds=timeout_seconds,
            sensitive_paths=(Path(path), root),
            cancellation_callback=cancellation_callback,
        )
        if result.returncode != 0 or not output.is_file():
            raise ValueError("audio verification decode failed")
        with wave.open(str(output), "rb") as handle:
            channels = handle.getnchannels()
            sample_rate = handle.getframerate()
            sample_width = handle.getsampwidth()
            frame_count = handle.getnframes()
            payload = handle.readframes(frame_count)
        if channels <= 0 or sample_rate <= 0 or sample_width != 2 or frame_count <= 0:
            raise ValueError("audio verification PCM inventory is invalid")
        pcm = np.frombuffer(payload, dtype="<i2")
        if pcm.size != frame_count * channels:
            raise ValueError("audio verification PCM payload is truncated")
        return pcm.reshape(frame_count, channels).astype(
            np.float64
        ) / 32768.0, sample_rate


def _map_source_interval_to_output(
    start_seconds: float,
    end_seconds: float,
    range_pairs: tuple[tuple[float, float, float, float], ...],
) -> tuple[float, float]:
    for source_start, source_end, output_start, output_end in range_pairs:
        if (
            start_seconds >= source_start - _PERCEPTUAL_TIME_TOLERANCE_SECONDS
            and end_seconds <= source_end + _PERCEPTUAL_TIME_TOLERANCE_SECONDS
        ):
            scale = (output_end - output_start) / (source_end - source_start)
            return (
                output_start + (start_seconds - source_start) * scale,
                output_start + (end_seconds - source_start) * scale,
            )
    raise ValueError("tonal event falls outside confirmed ranges")


def _boundary_observation(
    samples: np.ndarray, boundary_index: int, window_size: int
) -> tuple[float, float, float]:
    if boundary_index < window_size or boundary_index + window_size > samples.size:
        raise ValueError("tonal boundary lacks bilateral 50 ms windows")
    left = samples[boundary_index - window_size : boundary_index]
    right = samples[boundary_index : boundary_index + window_size]
    epsilon = float(np.finfo(np.float64).tiny)

    def rms(values: np.ndarray) -> float:
        return math.sqrt(float(np.mean(np.square(values), dtype=np.float64)))

    left_rms, right_rms = rms(left), rms(right)
    energy_jump = abs(
        20.0 * math.log10(max(right_rms, epsilon) / max(left_rms, epsilon))
    )
    left_crest = float(np.max(np.abs(left))) / max(left_rms, epsilon)
    right_crest = float(np.max(np.abs(right))) / max(right_rms, epsilon)
    crest_jump = abs(
        20.0 * math.log10(max(right_crest, epsilon) / max(left_crest, epsilon))
    )
    adjacent_delta = abs(
        float(samples[boundary_index]) - float(samples[boundary_index - 1])
    )
    return energy_jump, crest_jump, adjacent_delta


def _source_relative_tonal_boundary_metrics(
    source_samples: np.ndarray,
    candidate_samples: np.ndarray,
    boundary_index: int,
    window_size: int,
    sample_rate_hz: int,
    target_frequency_hz: float,
    *,
    boundary_side: Literal["start", "end"],
    boundary_transition_seconds: float,
    derivative_numerical_floor: float,
) -> dict[str, float]:
    """Measure new defects after projecting the declared smooth tone envelope."""
    source = np.asarray(source_samples, dtype=np.float64).reshape(-1)
    candidate = np.asarray(candidate_samples, dtype=np.float64).reshape(-1)
    if (
        source.shape != candidate.shape
        or not np.all(np.isfinite(source))
        or not np.all(np.isfinite(candidate))
        or sample_rate_hz <= 0
        or not math.isfinite(target_frequency_hz)
        or not 0.0 < target_frequency_hz < sample_rate_hz / 2.0
        or boundary_side not in {"start", "end"}
        or not math.isfinite(boundary_transition_seconds)
        or boundary_transition_seconds <= 0.0
        or not math.isfinite(derivative_numerical_floor)
        or derivative_numerical_floor <= 0.0
    ):
        raise ValueError("tonal boundary comparison parameters are invalid")
    if boundary_index < window_size or boundary_index + window_size > source.size:
        raise ValueError("tonal boundary lacks aligned bilateral 50 ms windows")

    pair_start = boundary_index - window_size
    pair_end = boundary_index + window_size
    source_pair = source[pair_start:pair_end]
    candidate_pair = candidate[pair_start:pair_end]
    pair_boundary = window_size
    defect_window_size = max(8, window_size // 10)
    defect_start = pair_boundary - defect_window_size
    defect_end = pair_boundary + defect_window_size
    residual = candidate_pair - source_pair
    relative_times = (
        np.arange(-window_size, window_size, dtype=np.float64) / sample_rate_hz
    )
    envelope = np.zeros(relative_times.size, dtype=np.float64)
    if boundary_side == "start":
        active = relative_times >= 0.0
        distance = relative_times[active]
    else:
        active = relative_times < 0.0
        distance = -relative_times[active]
    envelope[active] = 0.5 - 0.5 * np.cos(
        np.pi
        * np.minimum(distance, boundary_transition_seconds)
        / boundary_transition_seconds
    )
    phase = 2.0 * np.pi * target_frequency_hz * relative_times
    tonal_basis = np.column_stack((envelope * np.sin(phase), envelope * np.cos(phase)))
    normalized_time = relative_times / float(np.max(np.abs(relative_times)))
    design = np.column_stack(
        (
            np.ones(relative_times.size, dtype=np.float64),
            normalized_time,
            tonal_basis,
        )
    )
    singular_values = np.linalg.svd(design, compute_uv=False)
    if singular_values.size != 4 or not np.all(np.isfinite(singular_values)):
        raise ValueError("tonal boundary projection basis is invalid")
    basis_tolerance = np.finfo(np.float64).eps * max(design.shape) * singular_values[0]
    if singular_values[0] <= 0.0 or singular_values[-1] <= basis_tolerance:
        raise ValueError("tonal boundary projection basis is ill-conditioned")
    coefficients, _residuals, rank, _singular = np.linalg.lstsq(
        design, residual, rcond=None
    )
    if rank != 4 or not np.all(np.isfinite(coefficients)):
        raise ValueError("tonal boundary projection basis is ill-conditioned")
    observed_peak = max(
        float(np.max(np.abs(source_pair))),
        float(np.max(np.abs(candidate_pair))),
    )
    coefficient_limit = 2.0 * observed_peak
    tonal_coefficients = coefficients[2:].copy()
    coefficient_norm = float(np.linalg.norm(tonal_coefficients))
    if coefficient_norm > coefficient_limit and coefficient_norm > 0.0:
        tonal_coefficients *= coefficient_limit / coefficient_norm
    remaining_residual = residual - tonal_basis @ tonal_coefficients
    source_difference = np.diff(source_pair)
    residual_difference = np.diff(remaining_residual)
    crossing_difference_index = pair_boundary - 1
    derivative_windows = (
        slice(
            crossing_difference_index - defect_window_size,
            crossing_difference_index,
        ),
        slice(pair_boundary, pair_boundary + defect_window_size),
    )
    epsilon = float(np.finfo(np.float64).tiny)

    def derivative_rms(values: np.ndarray) -> float:
        return math.sqrt(float(np.mean(np.square(values), dtype=np.float64)))

    corrected_difference = source_difference + residual_difference
    energy_excesses: list[float] = []
    crest_excesses: list[float] = []
    for derivative_window in derivative_windows:
        source_values = source_difference[derivative_window]
        candidate_values = corrected_difference[derivative_window]
        source_rms = max(derivative_rms(source_values), derivative_numerical_floor)
        candidate_rms = max(
            derivative_rms(candidate_values), derivative_numerical_floor
        )
        source_peak = max(
            float(np.max(np.abs(source_values))), derivative_numerical_floor
        )
        candidate_peak = max(
            float(np.max(np.abs(candidate_values))), derivative_numerical_floor
        )
        source_crest = source_peak / max(source_rms, epsilon)
        candidate_crest = candidate_peak / max(candidate_rms, epsilon)
        energy_excesses.append(max(0.0, 20.0 * math.log10(candidate_rms / source_rms)))
        crest_excesses.append(
            max(0.0, 20.0 * math.log10(candidate_crest / source_crest))
        )
    energy_jump = max(energy_excesses)
    crest_jump = max(crest_excesses)
    cleaned_defect = remaining_residual[defect_start:defect_end]
    exact_residual_delta = abs(
        float(remaining_residual[pair_boundary])
        - float(remaining_residual[pair_boundary - 1])
    )
    maximum_residual_first_difference = float(np.max(np.abs(np.diff(cleaned_defect))))
    return {
        "energy_jump_db": energy_jump,
        "crest_jump_db": crest_jump,
        "adjacent_delta": max(
            exact_residual_delta,
            maximum_residual_first_difference,
        ),
    }


def _interval_union(
    ranges: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...]:
    """Return one strict ordered union for exact action-coverage comparison."""
    ordered = sorted(ranges)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
        ):
            raise ValueError("verification ranges must be finite and positive")
        if not merged or start > merged[-1][1] + _PERCEPTUAL_TIME_TOLERANCE_SECONDS:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return tuple(merged)


def _intervals_match_exactly(
    left: tuple[tuple[float, float], ...],
    right: tuple[tuple[float, float], ...],
) -> bool:
    return len(left) == len(right) and all(
        math.isclose(
            left_start,
            right_start,
            rel_tol=0.0,
            abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
        )
        and math.isclose(
            left_end,
            right_end,
            rel_tol=0.0,
            abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
        )
        for (left_start, left_end), (right_start, right_end) in zip(
            left, right, strict=True
        )
    )


def _measure_tonal_outcome(
    source: Path,
    candidate: Path,
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
    parameters: Mapping[str, JsonValue],
    ffmpeg: str,
    runner: ExternalCommandRunner,
    timeout_seconds: float,
    cancellation_callback: Callable[[], bool],
    *,
    boundary_reference: Path | None = None,
) -> dict[str, float]:
    pairs = _paired_perceptual_ranges(source_ranges, output_ranges)
    try:
        config = TonalInterferenceConfig.model_validate_json(
            json.dumps(parameters.get("config"), ensure_ascii=False)
        )
        raw_profiles = parameters.get("interference_profiles")
        if not isinstance(raw_profiles, (list, tuple)) or not raw_profiles:
            raise ValueError("tonal verification profiles are unavailable")
        profiles = tuple(
            InterferenceTone.model_validate_json(
                json.dumps(profile, ensure_ascii=False)
            )
            for profile in raw_profiles
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("tonal verification parameters are invalid") from exc
    profile_ranges = _interval_union(
        tuple((profile.start_seconds, profile.end_seconds) for profile in profiles)
    )
    action_ranges = _interval_union(
        tuple((start, end) for start, end, _output_start, _output_end in pairs)
    )
    if not _intervals_match_exactly(profile_ranges, action_ranges):
        raise ValueError("tonal profiles do not exactly cover confirmed action ranges")
    reductions: list[float] = []
    target_margins: list[float] = []
    non_target_losses: list[float] = []
    boundary_energy: list[float] = []
    boundary_crest: list[float] = []
    boundary_adjacent: list[float] = []
    measured_windows = 0.0
    excluded_transition_windows = 0.0
    profile_evidence: dict[str, float] = {}
    for profile_index, profile in enumerate(profiles):
        qualification = validate_tonal_render_qualification(profile, config)
        output_start, output_end = _map_source_interval_to_output(
            profile.start_seconds, profile.end_seconds, pairs
        )
        margin = max(
            _TONAL_VERIFICATION_WINDOW_SECONDS,
            config.boundary_transition_seconds,
        )
        source_decode_start = max(0.0, profile.start_seconds - margin)
        source_decode_end = profile.end_seconds + margin
        output_decode_start = max(0.0, output_start - margin)
        output_decode_end = output_end + margin
        source_pcm, source_rate = _decode_audio_segment(
            Path(source),
            source_decode_start,
            source_decode_end,
            ffmpeg,
            runner,
            timeout_seconds,
            cancellation_callback,
        )
        candidate_pcm, candidate_rate = _decode_audio_segment(
            Path(candidate),
            output_decode_start,
            output_decode_end,
            ffmpeg,
            runner,
            timeout_seconds,
            cancellation_callback,
        )
        boundary_pcm = source_pcm
        boundary_rate = source_rate
        if boundary_reference is not None:
            boundary_pcm, boundary_rate = _decode_audio_segment(
                Path(boundary_reference),
                output_decode_start,
                output_decode_end,
                ffmpeg,
                runner,
                timeout_seconds,
                cancellation_callback,
            )
        if (
            source_rate != candidate_rate
            or boundary_rate != candidate_rate
            or source_pcm.shape[1] != candidate_pcm.shape[1]
            or boundary_pcm.shape[1] != candidate_pcm.shape[1]
            or source_pcm.shape[1] > config.maximum_channels
        ):
            raise ValueError("tonal source/output PCM inventories differ")
        window_size = max(8, round(_TONAL_VERIFICATION_WINDOW_SECONDS * source_rate))
        source_event_start = round(
            (profile.start_seconds - source_decode_start) * source_rate
        )
        source_event_end = round(
            (profile.end_seconds - source_decode_start) * source_rate
        )
        output_event_start = round((output_start - output_decode_start) * source_rate)
        output_event_end = round((output_end - output_decode_start) * source_rate)
        source_count = source_event_end - source_event_start
        output_count = output_event_end - output_event_start
        if source_count <= 0 or abs(source_count - output_count) > 1:
            raise ValueError("tonal event sample inventories differ")
        count = min(source_count, output_count)
        profile_reductions: list[float] = []
        profile_non_target_losses: list[float] = []
        profile_boundary_energy: list[float] = []
        profile_boundary_crest: list[float] = []
        profile_boundary_adjacent: list[float] = []
        profile_measured_windows = 0.0
        profile_excluded_windows = 0.0
        for channel_index in profile.channel_indices:
            if channel_index >= source_pcm.shape[1]:
                raise ValueError("tonal channel falls outside decoded inventory")
            source_event = source_pcm[
                source_event_start : source_event_start + count, channel_index
            ]
            candidate_event = candidate_pcm[
                output_event_start : output_event_start + count, channel_index
            ]
            spectral = _independent_tonal_window_metrics(
                source_event,
                candidate_event,
                source_rate,
                target_frequency_hz=profile.center_frequency_hz,
                window_seconds=_TONAL_VERIFICATION_WINDOW_SECONDS,
                boundary_transition_seconds=0.0,
            )
            reductions.append(spectral["target_reduction_db"])
            profile_reductions.append(spectral["target_reduction_db"])
            target_margins.append(
                spectral["target_reduction_db"] - profile.attenuation_target_db
            )
            non_target_losses.append(spectral["non_target_attenuation_db"])
            profile_non_target_losses.append(spectral["non_target_attenuation_db"])
            measured_windows += spectral["window_count"]
            excluded_transition_windows += spectral["excluded_transition_window_count"]
            profile_measured_windows += spectral["window_count"]
            profile_excluded_windows += spectral["excluded_transition_window_count"]
            boundaries: tuple[tuple[int, int, Literal["start", "end"]], ...] = (
                (source_event_start, output_event_start, "start"),
                (source_event_start + count, output_event_start + count, "end"),
            )
            for source_boundary, output_boundary, boundary_side in boundaries:
                boundary_pair = boundary_pcm[
                    output_boundary - window_size : output_boundary + window_size,
                    channel_index,
                ]
                candidate_pair = candidate_pcm[
                    output_boundary - window_size : output_boundary + window_size,
                    channel_index,
                ]
                boundary_values = source_relative_tonal_boundary_metrics(
                    boundary_pair,
                    candidate_pair,
                    window_size,
                    window_size,
                    source_rate,
                    profile.center_frequency_hz,
                    boundary_side=boundary_side,
                    boundary_mode=qualification.boundary_mode,
                    boundary_transition_seconds=config.boundary_transition_seconds,
                    derivative_numerical_floor=config.max_boundary_adjacent_delta,
                )
                boundary_energy.append(boundary_values["energy_jump_db"])
                boundary_crest.append(boundary_values["crest_jump_db"])
                boundary_adjacent.append(boundary_values["adjacent_delta"])
                profile_boundary_energy.append(boundary_values["energy_jump_db"])
                profile_boundary_crest.append(boundary_values["crest_jump_db"])
                profile_boundary_adjacent.append(boundary_values["adjacent_delta"])
        profile_key = f"profile_{profile_index}"
        profile_evidence.update(
            {
                f"{profile_key}_range_coverage_ratio": 1.0,
                f"{profile_key}_measured_windows": profile_measured_windows,
                f"{profile_key}_excluded_transition_windows": (
                    profile_excluded_windows
                ),
                f"{profile_key}_minimum_target_reduction_db": float(
                    min(profile_reductions)
                ),
                f"{profile_key}_minimum_target_margin_db": float(
                    min(profile_reductions) - profile.attenuation_target_db
                ),
                f"{profile_key}_maximum_non_target_attenuation_db": float(
                    max(profile_non_target_losses)
                ),
                f"{profile_key}_maximum_boundary_energy_jump_db": float(
                    max(profile_boundary_energy)
                ),
                f"{profile_key}_maximum_boundary_crest_jump_db": float(
                    max(profile_boundary_crest)
                ),
                f"{profile_key}_maximum_boundary_adjacent_delta": float(
                    max(profile_boundary_adjacent)
                ),
            }
        )
    if (
        not reductions
        or not target_margins
        or measured_windows <= 0
        or not boundary_energy
    ):
        raise ValueError("tonal verification produced no independent measurements")
    return {
        "range_coverage_ratio": 1.0,
        "measured_windows": measured_windows,
        "excluded_transition_windows": excluded_transition_windows,
        "minimum_target_reduction_db": float(min(reductions)),
        "minimum_target_margin_db": float(min(target_margins)),
        "maximum_non_target_attenuation_db": float(max(non_target_losses)),
        "maximum_boundary_energy_jump_db": float(max(boundary_energy)),
        "maximum_boundary_crest_jump_db": float(max(boundary_crest)),
        "maximum_boundary_adjacent_delta": float(max(boundary_adjacent)),
        **profile_evidence,
    }


def _measurement_payload(
    measured: Mapping[str, float],
    thresholds: Mapping[str, float],
    *,
    valid: bool,
) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {
        "measurement_valid": valid,
        "reason": "measured" if valid else "independent_measurement_unavailable",
    }
    for key, value in measured.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        finite = float(value)
        if math.isfinite(finite):
            payload[key] = finite
    payload["thresholds"] = {key: float(value) for key, value in thresholds.items()}
    return payload


def _required_measurement_check(
    check_id: str,
    artifact: Literal["faithful", "improved"],
    passed: bool,
    message: str,
    measured: dict[str, JsonValue],
) -> RescueVerificationCheck:
    return RescueVerificationCheck(
        check_id=check_id,
        artifact=artifact,
        status=(
            RescueVerificationStatus.PASSED
            if passed
            else RescueVerificationStatus.NEEDS_REVIEW
        ),
        message=message,
        measured=measured,
        required=True,
    )


def _deblur_configs(action: RescueAction) -> tuple[DeblurConfig, ...] | None:
    try:
        if action.parameters.get("algorithm_version") != "1":
            return None
        raw_operations = action.parameters.get("operations")
        if not isinstance(raw_operations, (list, tuple)) or not raw_operations:
            raw_config = action.parameters.get("config")
            return (
                DeblurConfig.model_validate_json(
                    json.dumps(raw_config, ensure_ascii=False)
                ),
            )
        configs: list[DeblurConfig] = []
        for operation in raw_operations:
            if not isinstance(operation, Mapping):
                return None
            configs.append(
                DeblurConfig.model_validate_json(
                    json.dumps(operation.get("config"), ensure_ascii=False)
                )
            )
        return tuple(configs)
    except (TypeError, ValueError):
        return None


def _deblur_verification_checks(
    artifact: Literal["faithful", "improved"],
    action: RescueAction,
    measured: Mapping[str, float],
) -> tuple[RescueVerificationCheck, ...]:
    configs = _deblur_configs(action)
    thresholds = (
        {
            "maximum_edge_width_ratio": min(
                config.maximum_edge_width_ratio for config in configs
            ),
            "minimum_edge_continuity_ratio": max(
                config.minimum_edge_continuity_ratio for config in configs
            ),
            "maximum_ringing_ratio": min(
                config.maximum_ringing_ratio for config in configs
            ),
            "maximum_noise_gain_ratio": min(
                config.maximum_noise_gain_ratio for config in configs
            ),
            "maximum_temporal_change_ratio": min(
                config.maximum_temporal_change_ratio for config in configs
            ),
        }
        if configs
        else {}
    )
    coverage = _finite_metric(measured, "range_coverage_ratio")
    compared = _finite_metric(measured, "compared_frames")
    operation_count = _finite_metric(measured, "operation_count")
    edge_passed_operations = _finite_metric(measured, "edge_recovery_passed_operations")
    ringing_passed_operations = _finite_metric(measured, "ringing_passed_operations")
    temporal_passed_operations = _finite_metric(measured, "temporal_passed_operations")
    width = _finite_metric(measured, "edge_width_ratio")
    continuity = _finite_metric(measured, "edge_continuity_ratio")
    ringing = _finite_metric(measured, "ringing_ratio")
    noise = _finite_metric(measured, "noise_gain_ratio")
    temporal = _finite_metric(measured, "temporal_change_ratio")
    expected_operation_count = float(len(configs)) if configs else None
    common_valid = (
        bool(configs)
        and coverage is not None
        and math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-9)
        and compared is not None
        and compared >= 2.0
        and operation_count is not None
        and operation_count == expected_operation_count
    )
    edge_valid = (
        common_valid
        and width is not None
        and continuity is not None
        and edge_passed_operations is not None
    )
    side_effect_valid = (
        common_valid
        and ringing is not None
        and noise is not None
        and ringing_passed_operations is not None
    )
    temporal_valid = (
        common_valid and temporal is not None and temporal_passed_operations is not None
    )
    payload = _measurement_payload(
        measured,
        thresholds,
        valid=edge_valid and side_effect_valid and temporal_valid,
    )
    edge_passed = False
    if edge_valid:
        assert operation_count is not None and edge_passed_operations is not None
        edge_passed = edge_passed_operations == operation_count
    side_effect_passed = False
    if side_effect_valid:
        assert operation_count is not None and ringing_passed_operations is not None
        side_effect_passed = ringing_passed_operations == operation_count
    temporal_passed = False
    if temporal_valid:
        assert operation_count is not None and temporal_passed_operations is not None
        temporal_passed = temporal_passed_operations == operation_count
    return (
        _required_measurement_check(
            "deblur_edge_recovery",
            artifact,
            edge_passed,
            "Independent multi-scale edges improved across the confirmed range.",
            payload,
        ),
        _required_measurement_check(
            "deblur_ringing",
            artifact,
            side_effect_passed,
            "Independent ringing and noise measurements remain within bounds.",
            payload,
        ),
        _required_measurement_check(
            "deblur_temporal_consistency",
            artifact,
            temporal_passed,
            "Independent temporal consistency remains within the confirmed bound.",
            payload,
        ),
    )


def _tonal_verification_checks(
    artifact: Literal["faithful", "improved"],
    action: RescueAction,
    measured: Mapping[str, float],
) -> tuple[RescueVerificationCheck, ...]:
    evidence: TonalEncodedQualificationEvidenceV3 | None = None
    try:
        config = TonalInterferenceConfig.model_validate_json(
            json.dumps(action.parameters.get("config"), ensure_ascii=False)
        )
        raw_profiles = action.parameters.get("interference_profiles")
        if not isinstance(raw_profiles, (list, tuple)) or not raw_profiles:
            raise ValueError("tonal verification profiles are unavailable")
        profiles = tuple(
            InterferenceTone.model_validate_json(json.dumps(item, ensure_ascii=False))
            for item in raw_profiles
        )
        evidence = TonalEncodedQualificationEvidenceV3.model_validate_json(
            json.dumps(
                action.parameters.get("encoded_candidate_qualification"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError):
        config = None
        profiles = ()
    thresholds = (
        {
            "minimum_target_margin_db": _TONAL_MINIMUM_TARGET_MARGIN_DB,
            "configured_maximum_attenuation_target_db": max(
                profile.attenuation_target_db for profile in profiles
            ),
            "configured_maximum_non_target_attenuation_db": (
                config.max_non_target_band_attenuation_db
            ),
            "configured_maximum_boundary_energy_jump_db": (
                config.max_boundary_energy_jump_db
            ),
            "configured_maximum_boundary_crest_jump_db": (
                config.max_boundary_crest_jump_db
            ),
            "configured_maximum_boundary_adjacent_delta": (
                config.max_boundary_adjacent_delta
            ),
        }
        if config is not None and profiles
        else {}
    )
    coverage = _finite_metric(measured, "range_coverage_ratio")
    windows = _finite_metric(measured, "measured_windows")
    target = _finite_metric(measured, "minimum_target_reduction_db")
    target_margin = _finite_metric(measured, "minimum_target_margin_db")
    non_target = _finite_metric(measured, "maximum_non_target_attenuation_db")
    energy = _finite_metric(measured, "maximum_boundary_energy_jump_db")
    crest = _finite_metric(measured, "maximum_boundary_crest_jump_db")
    adjacent = _finite_metric(measured, "maximum_boundary_adjacent_delta")
    excluded = _finite_metric(measured, "excluded_transition_windows")
    profile_suffixes = (
        "range_coverage_ratio",
        "measured_windows",
        "excluded_transition_windows",
        "minimum_target_reduction_db",
        "minimum_target_margin_db",
        "maximum_non_target_attenuation_db",
        "maximum_boundary_energy_jump_db",
        "maximum_boundary_crest_jump_db",
        "maximum_boundary_adjacent_delta",
    )
    profile_metric_suffixes = (
        "range_coverage_ratio",
        "minimum_target_reduction_db",
        "minimum_target_margin_db",
        "maximum_non_target_attenuation_db",
        "maximum_boundary_energy_jump_db",
        "maximum_boundary_crest_jump_db",
        "maximum_boundary_adjacent_delta",
    )
    expected_profile_keys = {
        f"profile_{profile_index}_{suffix}"
        for profile_index in range(len(profiles))
        for suffix in profile_suffixes
    }
    observed_profile_keys = {key for key in measured if key.startswith("profile_")}
    reference_counts: tuple[int, ...] = ()
    reference_profile_values: tuple[dict[str, float], ...] = ()
    reference_valid = bool(
        config is not None
        and profiles
        and evidence is not None
        and evidence.passed
        and tuple(evidence.selected_profiles) == profiles
        and len(evidence.profile_qualifications) == len(profiles)
        and len(evidence.combined_metrics) == len(profiles)
    )
    if reference_valid:
        assert evidence is not None
        counts: list[int] = []
        reference_values_list: list[dict[str, float]] = []
        for profile, qualification, combined in zip(
            profiles,
            evidence.profile_qualifications,
            evidence.combined_metrics,
            strict=True,
        ):
            selected_attempts = tuple(
                attempt
                for attempt in qualification.attempts
                if attempt.notch_q == qualification.selected_notch_q
                and attempt.notch_pass_count == qualification.selected_notch_pass_count
            )
            render = profile.render_qualification
            selected = selected_attempts[0].metrics if selected_attempts else None
            if (
                selected is None
                or render is None
                or selected.measured_windows != combined.measured_windows
                or combined.measured_windows != render.complete_window_count
                or selected.excluded_transition_windows != 0
                or combined.excluded_transition_windows != 0
                or any(
                    not math.isclose(
                        float(getattr(selected, suffix)),
                        float(getattr(combined, suffix)),
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    for suffix in profile_metric_suffixes
                )
            ):
                reference_valid = False
                break
            counts.append(combined.measured_windows)
            reference_values_list.append(
                {
                    suffix: float(getattr(combined, suffix))
                    for suffix in profile_metric_suffixes
                }
            )
        if reference_valid:
            reference_counts = tuple(counts)
            reference_profile_values = tuple(reference_values_list)

    profile_values: list[dict[str, float]] = []
    inventory_valid = bool(
        reference_valid and observed_profile_keys == expected_profile_keys
    )
    if inventory_valid:
        for profile_index, (profile, expected_windows, reference_values) in enumerate(
            zip(
                profiles,
                reference_counts,
                reference_profile_values,
                strict=True,
            )
        ):
            values = {
                suffix: _finite_metric(measured, f"profile_{profile_index}_{suffix}")
                for suffix in profile_suffixes
            }
            if any(value is None for value in values.values()):
                inventory_valid = False
                break
            finite_values = cast(dict[str, float], values)
            profile_windows = finite_values["measured_windows"]
            profile_excluded = finite_values["excluded_transition_windows"]
            profile_target = finite_values["minimum_target_reduction_db"]
            if (
                not math.isclose(
                    finite_values["range_coverage_ratio"],
                    1.0,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or not profile_windows.is_integer()
                or profile_windows != expected_windows
                or not profile_excluded.is_integer()
                or profile_excluded != 0.0
                or not math.isclose(
                    finite_values["minimum_target_margin_db"],
                    profile_target - profile.attenuation_target_db,
                    rel_tol=0.0,
                    abs_tol=1e-9,
                )
                or any(
                    not math.isclose(
                        finite_values[suffix],
                        reference_values[suffix],
                        rel_tol=0.0,
                        abs_tol=1e-9,
                    )
                    for suffix in profile_metric_suffixes
                )
            ):
                inventory_valid = False
                break
            profile_values.append(finite_values)

    global_values = (
        coverage,
        windows,
        excluded,
        target,
        target_margin,
        non_target,
        energy,
        crest,
        adjacent,
    )
    if inventory_valid:
        inventory_valid = bool(
            all(value is not None for value in global_values)
            and coverage is not None
            and math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-9)
            and windows is not None
            and windows.is_integer()
            and windows == sum(reference_counts)
            and excluded is not None
            and excluded.is_integer()
            and excluded == 0.0
            and target is not None
            and math.isclose(
                target,
                min(item["minimum_target_reduction_db"] for item in profile_values),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                target,
                min(
                    item["minimum_target_reduction_db"]
                    for item in reference_profile_values
                ),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and target_margin is not None
            and math.isclose(
                target_margin,
                min(item["minimum_target_margin_db"] for item in profile_values),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                target_margin,
                min(
                    item["minimum_target_margin_db"]
                    for item in reference_profile_values
                ),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and non_target is not None
            and math.isclose(
                non_target,
                max(
                    item["maximum_non_target_attenuation_db"] for item in profile_values
                ),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                non_target,
                max(
                    item["maximum_non_target_attenuation_db"]
                    for item in reference_profile_values
                ),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and energy is not None
            and math.isclose(
                energy,
                max(item["maximum_boundary_energy_jump_db"] for item in profile_values),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                energy,
                max(
                    item["maximum_boundary_energy_jump_db"]
                    for item in reference_profile_values
                ),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and crest is not None
            and math.isclose(
                crest,
                max(item["maximum_boundary_crest_jump_db"] for item in profile_values),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                crest,
                max(
                    item["maximum_boundary_crest_jump_db"]
                    for item in reference_profile_values
                ),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and adjacent is not None
            and math.isclose(
                adjacent,
                max(item["maximum_boundary_adjacent_delta"] for item in profile_values),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and math.isclose(
                adjacent,
                max(
                    item["maximum_boundary_adjacent_delta"]
                    for item in reference_profile_values
                ),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        )
    reduction_valid = inventory_valid
    boundary_valid = inventory_valid
    payload = _measurement_payload(
        measured, thresholds, valid=reduction_valid and boundary_valid
    )
    reduction_passed = False
    if reduction_valid:
        reduction_passed = all(
            item["minimum_target_margin_db"] >= thresholds["minimum_target_margin_db"]
            and item["maximum_non_target_attenuation_db"]
            <= thresholds["configured_maximum_non_target_attenuation_db"]
            for item in profile_values
        )
    boundary_passed = False
    if boundary_valid:
        boundary_passed = all(
            _at_or_below_with_ulp(
                item["maximum_boundary_energy_jump_db"],
                thresholds["configured_maximum_boundary_energy_jump_db"],
            )
            and _at_or_below_with_ulp(
                item["maximum_boundary_crest_jump_db"],
                thresholds["configured_maximum_boundary_crest_jump_db"],
            )
            and _at_or_below_with_ulp(
                item["maximum_boundary_adjacent_delta"],
                thresholds["configured_maximum_boundary_adjacent_delta"],
            )
            for item in profile_values
        )
    return (
        _required_measurement_check(
            "tonal_interference_reduction",
            artifact,
            reduction_passed,
            "Independent 50 ms spectra confirm target reduction and preservation.",
            payload,
        ),
        _required_measurement_check(
            "tonal_boundary_transient",
            artifact,
            boundary_passed,
            "Independent 50 ms boundary windows contain no new transient.",
            payload,
        ),
    )


def _anchor_verification_checks(
    artifact: Literal["faithful", "improved"],
    action: RescueAction,
    measured: Mapping[str, float],
) -> tuple[RescueVerificationCheck, ...]:
    try:
        config = StabilizationConfig.model_validate_json(
            json.dumps(action.parameters.get("config"), ensure_ascii=False)
        )
    except (TypeError, ValueError):
        config = None
    thresholds = (
        {
            "maximum_residual_median_pixels": config.residual_goal_median_pixels,
            "maximum_residual_p90_pixels": config.residual_goal_p90_pixels,
            "maximum_crop_ratio": config.max_crop_ratio,
        }
        if config is not None
        else {}
    )
    coverage = _finite_metric(measured, "range_coverage_ratio")
    expected = _finite_metric(measured, "expected_frames")
    reliable = _finite_metric(measured, "reliable_transforms")
    median = _finite_metric(measured, "residual_median_pixels")
    p90 = _finite_metric(measured, "residual_p90_pixels")
    crop = _finite_metric(measured, "crop_ratio")
    valid = (
        config is not None
        and coverage is not None
        and math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-9)
        and expected is not None
        and expected >= 2.0
        and reliable is not None
        and reliable >= expected
        and median is not None
        and p90 is not None
        and crop is not None
    )
    payload = _measurement_payload(measured, thresholds, valid=valid)
    passed = False
    if valid:
        assert median is not None and p90 is not None and crop is not None
        passed = (
            median <= thresholds["maximum_residual_median_pixels"]
            and p90 <= thresholds["maximum_residual_p90_pixels"]
            and crop <= thresholds["maximum_crop_ratio"]
        )
    checks = (
        _required_measurement_check(
            "anchor_stabilization_residual",
            artifact,
            passed,
            "Independent frame-aligned residual and crop measurements meet bounds.",
            payload,
        ),
    )
    if action.parameters.get("method") != "transition_anchor_v1":
        return checks
    return checks + _transition_anchor_verification_checks(
        artifact, action, measured, config
    )


def _stabilization_freeze_verification_check(
    artifact: Literal["faithful", "improved"],
    action: RescueAction,
    measured: Mapping[str, JsonValue],
    base_check: RescueVerificationCheck,
) -> RescueVerificationCheck:
    try:
        config = StabilizationConfig.model_validate_json(
            json.dumps(action.parameters.get("config"), ensure_ascii=False)
        )
    except (TypeError, ValueError):
        config = None
    coverage = _finite_metric(measured, "range_coverage_ratio")
    expected = _finite_metric(measured, "expected_frames")
    compared = _finite_metric(measured, "compared_frames")
    source_inside = _finite_metric(measured, "source_freeze_events")
    candidate_inside = _finite_metric(measured, "candidate_freeze_events")
    attributed_candidate_inside = _finite_metric(
        measured, "attributed_candidate_freeze_events"
    )
    explained = _finite_metric(measured, "explained_freeze_events")
    unexplained = _finite_metric(measured, "unexplained_near_static_pairs")
    duplicates = _finite_metric(measured, "exact_duplicate_pairs")
    maximum_expected_mae = _finite_metric(measured, "maximum_candidate_expected_mae")
    outside_coverage = _finite_metric(measured, "outside_range_coverage_ratio")
    outside_expected = _finite_metric(measured, "outside_expected_frames")
    outside_compared = _finite_metric(measured, "outside_compared_frames")
    source_outside = _finite_metric(measured, "source_outside_freeze_events")
    output_outside = _finite_metric(measured, "candidate_outside_freeze_events")
    outside_duplicates = _finite_metric(measured, "outside_exact_duplicate_pairs")
    control_recipe_valid = _finite_metric(measured, "control_recipe_valid")
    base_applicable = base_check.measured.get("applicable") is True
    base_source = _number(base_check.measured.get("source_events"))
    base_output = _number(base_check.measured.get("output_events"))
    codec_tolerance = _number(base_check.measured.get("codec_event_tolerance"))
    counts = (
        source_inside,
        candidate_inside,
        attributed_candidate_inside,
        explained,
        unexplained,
        duplicates,
        source_outside,
        output_outside,
        outside_duplicates,
    )
    valid = (
        config is not None
        and control_recipe_valid == 1.0
        and base_applicable
        and coverage is not None
        and math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-9)
        and expected is not None
        and expected >= 2.0
        and compared is not None
        and math.isclose(compared, expected, rel_tol=0.0, abs_tol=1e-9)
        and all(
            value is not None and value.is_integer() and value >= 0 for value in counts
        )
        and maximum_expected_mae is not None
        and outside_coverage is not None
        and math.isclose(outside_coverage, 1.0, rel_tol=0.0, abs_tol=1e-9)
        and outside_expected is not None
        and outside_expected.is_integer()
        and outside_expected >= 0
        and outside_compared is not None
        and outside_compared.is_integer()
        and math.isclose(outside_compared, outside_expected, rel_tol=0.0, abs_tol=1e-9)
        and base_source is not None
        and base_output is not None
        and codec_tolerance is not None
        and codec_tolerance >= 0
    )
    passed = False
    if valid:
        assert config is not None
        assert source_inside is not None and candidate_inside is not None
        assert attributed_candidate_inside is not None
        assert explained is not None and unexplained is not None
        assert duplicates is not None and maximum_expected_mae is not None
        assert source_outside is not None and output_outside is not None
        assert outside_duplicates is not None
        assert codec_tolerance is not None
        valid = 0 <= explained <= attributed_candidate_inside
        passed = bool(
            valid
            and unexplained == 0
            and duplicates == 0
            and outside_duplicates == 0
            and explained == attributed_candidate_inside
            and maximum_expected_mae <= config.maximum_transition_dense_residual_pixels
            and output_outside <= source_outside + codec_tolerance
        )
    payload: dict[str, JsonValue] = {
        "applicable": True,
        "valid": valid,
        "reference": "identity_generation_control_and_confirmed_affine_warp",
        "source_outside_events": source_outside,
        "output_outside_events": output_outside,
        "codec_event_tolerance": codec_tolerance,
        "near_static_pair_mae_threshold": (
            config.residual_goal_median_pixels if config is not None else None
        ),
        "expected_near_static_pair_mae_threshold": (
            config.residual_goal_p90_pixels if config is not None else None
        ),
        "maximum_candidate_expected_mae_threshold": (
            config.maximum_transition_dense_residual_pixels
            if config is not None
            else None
        ),
        **{key: value for key, value in measured.items()},
    }
    return RescueVerificationCheck(
        check_id="freeze_regression",
        artifact=artifact,
        status=(
            RescueVerificationStatus.PASSED
            if passed
            else RescueVerificationStatus.NEEDS_REVIEW
        ),
        message=(
            "Confirmed stabilization ranges contain no unexplained freeze regression."
            if passed
            else "Stabilization freeze evidence is incomplete or exceeds the "
            "confirmed bound; manual review is required."
        ),
        measured=payload,
        required=False,
    )


def _luma_adjustment_verification_checks(
    action: RescueAction,
    mappings: tuple[SourceMapping, ...],
    locked_ranges: tuple[tuple[float, float], ...],
    measured: Mapping[str, JsonValue],
    *,
    expected_control_sha256: str,
    expected_candidate_sha256: str,
) -> tuple[RescueVerificationCheck, ...]:
    try:
        wire = luma_action_wire_from_parameters(action.parameters)
        output_ranges = _exact_action_output_ranges(action.source_ranges, mappings)
        mapping_valid = not _ranges_intersect(action.source_ranges, locked_ranges)
    except ValueError:
        wire = None
        output_ranges = ()
        mapping_valid = False
    coverage = _finite_metric(measured, "range_coverage_ratio")
    expected = _finite_metric(measured, "expected_frames")
    compared = _finite_metric(measured, "compared_frames")
    range_count = _finite_metric(measured, "range_count")
    minimum_luma = _finite_metric(measured, "minimum_luma_delta")
    maximum_luma = _finite_metric(measured, "maximum_luma_delta")
    maximum_noise = _finite_metric(measured, "maximum_noise_increase")
    maximum_clipping = _finite_metric(measured, "maximum_clipping_increase")
    maximum_chroma = _finite_metric(measured, "maximum_chroma_shift")
    identity_valid = (
        measured.get("control_sha256") == expected_control_sha256
        and measured.get("candidate_sha256") == expected_candidate_sha256
        and measured.get("control_topology_sha256")
        == measured.get("candidate_topology_sha256")
        and isinstance(measured.get("control_topology_sha256"), str)
    )
    common_valid = (
        wire is not None
        and mapping_valid
        and len(output_ranges) == len(action.source_ranges)
        and coverage is not None
        and math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-9)
        and expected is not None
        and expected.is_integer()
        and expected > 0
        and compared is not None
        and compared == expected
        and range_count is not None
        and range_count == float(len(action.source_ranges))
        and minimum_luma is not None
        and maximum_luma is not None
        and maximum_noise is not None
        and maximum_clipping is not None
        and maximum_chroma is not None
        and identity_valid
    )
    thresholds: dict[str, JsonValue] = {
        "minimum_luma_delta": (
            wire.minimum_perceptible_luma_delta if wire is not None else None
        ),
        "maximum_luma_delta": (
            wire.maximum_luma_improvement_delta if wire is not None else None
        ),
        "maximum_noise_increase": (
            wire.maximum_residual_increase if wire is not None else None
        ),
        "maximum_clipping_increase": (
            wire.maximum_clip_increase if wire is not None else None
        ),
        "maximum_chroma_shift": (
            wire.maximum_chroma_shift if wire is not None else None
        ),
    }
    payload: dict[str, JsonValue] = {
        "applicable": True,
        "measurement_valid": common_valid,
        "output_ranges": [list(item) for item in output_ranges],
        "thresholds": thresholds,
        **dict(measured),
    }
    luma_passed = False
    noise_passed = False
    chroma_passed = False
    if common_valid:
        assert wire is not None
        assert minimum_luma is not None and maximum_luma is not None
        assert maximum_noise is not None and maximum_clipping is not None
        assert maximum_chroma is not None
        luma_passed = (
            minimum_luma >= wire.minimum_perceptible_luma_delta
            and _at_or_below_with_ulp(maximum_luma, wire.maximum_luma_improvement_delta)
            and _at_or_below_with_ulp(maximum_clipping, wire.maximum_clip_increase)
        )
        noise_passed = _at_or_below_with_ulp(
            maximum_noise, wire.maximum_residual_increase
        )
        chroma_passed = _at_or_below_with_ulp(maximum_chroma, wire.maximum_chroma_shift)

    unavailable_message = (
        "Exact confirmed range measurement is unavailable or invalid; manual review "
        "is required."
    )

    def check(
        check_id: str,
        passed: bool,
        passed_message: str,
        review_message: str,
    ) -> RescueVerificationCheck:
        return RescueVerificationCheck(
            check_id=check_id,
            artifact="improved",
            status=(
                RescueVerificationStatus.PASSED
                if passed
                else RescueVerificationStatus.NEEDS_REVIEW
            ),
            message=(
                passed_message
                if passed
                else review_message
                if common_valid
                else unavailable_message
            ),
            measured=payload,
            required=False,
        )

    return (
        check(
            "perceptible_luma_improvement",
            luma_passed,
            "Exact confirmed ranges meet both luma lift and upper-bound limits.",
            "Observed luma change or clipping falls outside the configured bounds; "
            "manual review is required.",
        ),
        check(
            "noise_side_effects",
            noise_passed,
            "Exact confirmed ranges introduce no noise above the bound.",
            "Observed noise increase exceeds the configured bound; manual review is "
            "required.",
        ),
        check(
            "luma_chroma_side_effects",
            chroma_passed,
            "Exact confirmed ranges keep decoded chroma shift within the bound.",
            "Observed decoded chroma shift exceeds the configured bound; manual "
            "review is required.",
        ),
    )


def _sharpen_measurement_matches_selected_qualification(
    measured: Mapping[str, JsonValue], selected_metrics: object
) -> bool:
    integer_fields = (
        "expected_frames",
        "compared_frames",
        "range_count",
        "passing_range_count",
    )
    float_fields = (
        "range_coverage_ratio",
        "minimum_aggregate_gain_ratio",
        "minimum_recovered_baseline_ratio",
        "minimum_improved_frame_fraction",
        "maximum_noise_increase",
        "maximum_edge_overshoot_ratio",
        "maximum_edge_overshoot_amplitude",
        "maximum_ringing_ratio",
    )
    for name in integer_fields:
        observed = _finite_metric(measured, name)
        expected = getattr(selected_metrics, name, None)
        if (
            observed is None
            or not observed.is_integer()
            or isinstance(expected, bool)
            or not isinstance(expected, int)
            or int(observed) != expected
        ):
            return False
    for name in float_fields:
        observed = _finite_metric(measured, name)
        expected = getattr(selected_metrics, name, None)
        if (
            observed is None
            or isinstance(expected, bool)
            or not isinstance(expected, (int, float))
            or not math.isfinite(float(expected))
            or not math.isclose(observed, float(expected), rel_tol=0.0, abs_tol=1e-9)
        ):
            return False
    return True


def _codec_aligned_sharpness_verification_check(
    action: RescueAction,
    mappings: tuple[SourceMapping, ...],
    locked_ranges: tuple[tuple[float, float], ...],
    measured: Mapping[str, JsonValue],
    *,
    plan_digest: str,
    expected_control_sha256: str,
    expected_candidate_sha256: str,
) -> RescueVerificationCheck:
    try:
        output_ranges = _exact_action_output_ranges(action.source_ranges, mappings)
        mapping_valid = not _ranges_intersect(action.source_ranges, locked_ranges)
    except ValueError:
        output_ranges = ()
        mapping_valid = False
    minimum_gain = _number(
        action.parameters.get("minimum_perceptible_sharpness_gain_ratio")
    )
    minimum_recovery = _number(
        action.parameters.get("minimum_recovered_baseline_ratio")
    )
    minimum_frame_fraction = _number(
        action.parameters.get("minimum_improved_frame_fraction")
    )
    scene_baseline = _number(action.parameters.get("scene_baseline_sharpness"))
    maximum_noise = _number(action.parameters.get("maximum_noise_increase"))
    maximum_overshoot = _number(action.parameters.get("maximum_edge_overshoot_ratio"))
    maximum_overshoot_amplitude = _number(
        action.parameters.get("maximum_edge_overshoot_amplitude")
    )
    maximum_ringing = _number(action.parameters.get("maximum_ringing_ratio"))
    required_parameter_names = (
        "minimum_perceptible_sharpness_gain_ratio",
        "minimum_recovered_baseline_ratio",
        "minimum_improved_frame_fraction",
        "scene_baseline_sharpness",
        "maximum_noise_increase",
        "edge_gradient_threshold",
        "edge_neighborhood_radius",
        "edge_overshoot_minimum_amplitude",
        "maximum_edge_overshoot_ratio",
        "maximum_edge_overshoot_amplitude",
        "ringing_minimum_amplitude",
        "maximum_ringing_ratio",
    )
    explicit_parameters = all(
        name in action.parameters for name in required_parameter_names
    )
    try:
        sharpen_config = SharpenConfig.model_validate(
            {
                name: value
                for name, value in action.parameters.items()
                if name in SharpenConfig.model_fields
            }
        )
    except ValueError:
        sharpen_config = None
    coverage = _finite_metric(measured, "range_coverage_ratio")
    expected = _finite_metric(measured, "expected_frames")
    compared = _finite_metric(measured, "compared_frames")
    range_count = _finite_metric(measured, "range_count")
    passing_ranges = _finite_metric(measured, "passing_range_count")
    observed_gain = _finite_metric(measured, "minimum_aggregate_gain_ratio")
    observed_recovery = _finite_metric(measured, "minimum_recovered_baseline_ratio")
    observed_fraction = _finite_metric(measured, "minimum_improved_frame_fraction")
    observed_noise = _finite_metric(measured, "maximum_noise_increase")
    observed_overshoot = _finite_metric(measured, "maximum_edge_overshoot_ratio")
    observed_overshoot_amplitude = _finite_metric(
        measured, "maximum_edge_overshoot_amplitude"
    )
    observed_ringing = _finite_metric(measured, "maximum_ringing_ratio")
    control_sha256 = measured.get("control_sha256")
    candidate_sha256 = measured.get("candidate_sha256")
    control_topology_sha256 = measured.get("control_topology_sha256")
    candidate_topology_sha256 = measured.get("candidate_topology_sha256")
    runtime_control_recipe_valid = measured.get("runtime_control_recipe_valid")
    selected_qualification_binding_valid = measured.get(
        "selected_qualification_binding_valid"
    )
    thresholds = (
        minimum_gain,
        minimum_recovery,
        minimum_frame_fraction,
        scene_baseline,
        maximum_noise,
        maximum_overshoot,
        maximum_overshoot_amplitude,
        maximum_ringing,
    )
    valid = (
        mapping_valid
        and bool(output_ranges)
        and explicit_parameters
        and sharpen_config is not None
        and all(value is not None for value in thresholds)
        and coverage is not None
        and math.isclose(coverage, 1.0, rel_tol=0.0, abs_tol=1e-9)
        and expected is not None
        and expected >= 1.0
        and compared is not None
        and math.isclose(compared, expected, rel_tol=0.0, abs_tol=1e-9)
        and range_count is not None
        and range_count.is_integer()
        and int(range_count) == len(output_ranges)
        and passing_ranges is not None
        and passing_ranges.is_integer()
        and passing_ranges == range_count
        and observed_gain is not None
        and observed_recovery is not None
        and observed_fraction is not None
        and observed_noise is not None
        and observed_overshoot is not None
        and observed_overshoot_amplitude is not None
        and observed_ringing is not None
        and control_sha256 == expected_control_sha256
        and candidate_sha256 == expected_candidate_sha256
        and runtime_control_recipe_valid is True
        and selected_qualification_binding_valid is True
        and isinstance(control_topology_sha256, str)
        and _is_sha256_digest(control_topology_sha256)
        and control_topology_sha256 == candidate_topology_sha256
    )
    passed = False
    if valid:
        assert minimum_gain is not None and minimum_recovery is not None
        assert minimum_frame_fraction is not None
        assert maximum_noise is not None and maximum_overshoot is not None
        assert maximum_overshoot_amplitude is not None
        assert maximum_ringing is not None
        assert observed_gain is not None and observed_recovery is not None
        assert observed_fraction is not None
        assert observed_noise is not None and observed_overshoot is not None
        assert observed_overshoot_amplitude is not None
        assert observed_ringing is not None
        passed = (
            observed_gain >= minimum_gain
            and observed_recovery >= minimum_recovery
            and observed_fraction >= minimum_frame_fraction
            and observed_noise <= maximum_noise
            and observed_overshoot <= maximum_overshoot
            and observed_overshoot_amplitude <= maximum_overshoot_amplitude
            and observed_ringing <= maximum_ringing
        )
    payload: dict[str, JsonValue] = {
        "applicable": True,
        "valid": bool(valid),
        "reference": "runtime_same_generation_visibility_control",
        "plan_digest": plan_digest,
        "control_artifact_sha256": expected_control_sha256,
        "candidate_artifact_sha256": expected_candidate_sha256,
        "source_ranges": [list(value) for value in action.source_ranges],
        "output_ranges": [list(value) for value in output_ranges],
        "thresholds": {
            "minimum_aggregate_gain_ratio": minimum_gain,
            "minimum_recovered_baseline_ratio": minimum_recovery,
            "minimum_improved_frame_fraction": minimum_frame_fraction,
            "decoded_scene_baseline_sharpness": scene_baseline,
            "maximum_noise_increase": maximum_noise,
            "maximum_edge_overshoot_ratio": maximum_overshoot,
            "maximum_edge_overshoot_amplitude": maximum_overshoot_amplitude,
            "maximum_ringing_ratio": maximum_ringing,
        },
        **{key: value for key, value in measured.items()},
    }
    return RescueVerificationCheck(
        check_id="perceptible_sharpness_improvement",
        artifact="improved",
        status=(
            RescueVerificationStatus.PASSED
            if passed
            else RescueVerificationStatus.NEEDS_REVIEW
        ),
        message=(
            "Exact action ranges show robust sharpness lift over the same-generation "
            "visibility control."
            if passed
            else "Full-range same-generation sharpness evidence is incomplete or "
            "exceeds one or more confirmed bounds; manual review is required."
        ),
        measured=payload,
        required=False,
    )


def _transition_action_evidence_is_exact(
    action: RescueAction,
    config: StabilizationConfig | None,
    expected_transition_frames: float | None,
) -> bool:
    """Validate the confirmed transition inventory without executor assertions."""
    if config is None or expected_transition_frames is None:
        return False
    try:
        if (
            action.parameters.get("estimator_algorithm_version")
            != "transition_anchor_v1"
        ):
            return False
        transition = action.parameters.get("transition_range")
        following = action.parameters.get("following_anchor_range")
        raw_transforms = action.parameters.get("motion_transforms")
        declared_count = action.parameters.get("transition_correction_count")
        if (
            not isinstance(transition, (list, tuple))
            or len(transition) != 2
            or not isinstance(following, (list, tuple))
            or len(following) != 2
            or not isinstance(raw_transforms, (list, tuple))
            or isinstance(declared_count, bool)
            or not isinstance(declared_count, int)
            or declared_count != len(raw_transforms)
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in (*transition, *following)
            )
        ):
            return False
        numeric_transition = cast(Sequence[int | float], transition)
        numeric_following = cast(Sequence[int | float], following)
        transition_start, transition_end = (
            float(value) for value in numeric_transition
        )
        following_start, following_end = (float(value) for value in numeric_following)
        if (
            not all(
                math.isfinite(value)
                for value in (
                    transition_start,
                    transition_end,
                    following_start,
                    following_end,
                )
            )
            or transition_start >= transition_end
            or following_start >= following_end
            or not math.isclose(
                transition_end,
                following_start,
                rel_tol=0.0,
                abs_tol=config.exact_timestamp_tolerance_seconds,
            )
            or not _intervals_match_exactly(
                ((transition_start, following_end),), action.source_ranges
            )
        ):
            return False
        transforms = tuple(
            MotionTransform.model_validate_json(json.dumps(item, ensure_ascii=False))
            for item in raw_transforms
        )
        transition_transforms = tuple(
            item
            for item in transforms
            if transition_start <= item.timestamp_seconds < transition_end
        )
        return (
            len(transforms) == len(raw_transforms)
            and all(item.semantics == "frame_correction" for item in transforms)
            and all(
                current.timestamp_seconds > previous.timestamp_seconds
                for previous, current in zip(transforms, transforms[1:], strict=False)
            )
            and math.isclose(
                float(len(transition_transforms)),
                expected_transition_frames,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        )
    except (TypeError, ValueError):
        return False


def _transition_boundary_path_residual(
    action: RescueAction,
    measured: Mapping[str, float],
) -> float | None:
    source_x = _finite_metric(measured, "transition_boundary_source_translation_x")
    source_y = _finite_metric(measured, "transition_boundary_source_translation_y")
    transition = action.parameters.get("transition_range")
    raw_transforms = action.parameters.get("motion_transforms")
    if (
        source_x is None
        or source_y is None
        or not isinstance(transition, (list, tuple))
        or len(transition) != 2
        or isinstance(transition[1], bool)
        or not isinstance(transition[1], (int, float))
        or not isinstance(raw_transforms, (list, tuple))
    ):
        return None
    try:
        transition_end = float(transition[1])
        transforms = tuple(
            MotionTransform.model_validate_json(json.dumps(item, ensure_ascii=False))
            for item in raw_transforms
        )
    except (TypeError, ValueError):
        return None
    before = tuple(
        item for item in transforms if item.timestamp_seconds < transition_end
    )
    after = tuple(
        item for item in transforms if item.timestamp_seconds >= transition_end
    )
    if not before or not after:
        return None
    return math.hypot(
        (before[-1].translation_x - after[0].translation_x) - source_x,
        (before[-1].translation_y - after[0].translation_y) - source_y,
    )


def _transition_anchor_verification_checks(
    artifact: Literal["faithful", "improved"],
    action: RescueAction,
    measured: Mapping[str, float],
    config: StabilizationConfig | None,
) -> tuple[RescueVerificationCheck, ...]:
    consensus_coverage = _finite_metric(measured, "transition_consensus_coverage_ratio")
    consensus_p90 = _finite_metric(measured, "transition_consensus_p90_pixels")
    seam = _finite_metric(measured, "transition_seam_residual_pixels")
    expected = _finite_metric(measured, "transition_expected_frames")
    reliable = _finite_metric(measured, "transition_reliable_frames")
    exact_evidence = _transition_action_evidence_is_exact(action, config, expected)
    boundary_path_residual = _transition_boundary_path_residual(action, measured)
    thresholds = (
        {
            "minimum_transition_consensus_coverage_ratio": 1.0,
            "maximum_transition_consensus_p90_pixels": max(
                config.maximum_transition_regional_p90_pixels,
                config.maximum_transition_lk_residual_pixels,
                config.maximum_transition_dense_residual_pixels,
                config.maximum_transition_vector_disagreement_pixels,
            ),
            "maximum_transition_seam_residual_pixels": (
                config.maximum_transition_seam_discontinuity_pixels
            ),
            "minimum_transition_coverage_ratio": 1.0,
        }
        if config is not None
        else {}
    )
    consensus_valid = (
        exact_evidence
        and consensus_coverage is not None
        and consensus_p90 is not None
        and boundary_path_residual is not None
    )
    seam_valid = (
        exact_evidence and seam is not None and boundary_path_residual is not None
    )
    coverage_valid = (
        exact_evidence
        and expected is not None
        and expected >= 1.0
        and reliable is not None
    )
    consensus_passed = False
    if consensus_valid:
        assert (
            consensus_coverage is not None
            and consensus_p90 is not None
            and boundary_path_residual is not None
            and config is not None
        )
        consensus_passed = (
            math.isclose(
                consensus_coverage,
                thresholds["minimum_transition_consensus_coverage_ratio"],
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and consensus_p90 <= thresholds["maximum_transition_consensus_p90_pixels"]
            and boundary_path_residual
            <= config.maximum_transition_vector_disagreement_pixels
        )
    seam_passed = False
    if seam_valid:
        assert seam is not None and boundary_path_residual is not None
        seam_passed = (
            seam <= thresholds["maximum_transition_seam_residual_pixels"]
            and boundary_path_residual
            <= thresholds["maximum_transition_seam_residual_pixels"]
        )
    coverage_passed = bool(
        coverage_valid
        and reliable is not None
        and expected is not None
        and math.isclose(reliable, expected, rel_tol=0.0, abs_tol=1e-9)
    )
    return (
        _required_measurement_check(
            "transition_stabilization_consensus",
            artifact,
            consensus_passed,
            "Independent regional consensus covers every transition source frame.",
            _measurement_payload(measured, thresholds, valid=consensus_valid),
        ),
        _required_measurement_check(
            "transition_stabilization_seam",
            artifact,
            seam_passed,
            "Independent boundary measurement confirms anchor seam continuity.",
            _measurement_payload(measured, thresholds, valid=seam_valid),
        ),
        _required_measurement_check(
            "transition_stabilization_coverage",
            artifact,
            coverage_passed,
            "Actual source PTS have one exact transition correction each.",
            _measurement_payload(measured, thresholds, valid=coverage_valid),
        ),
    )


def _action_parameters(
    plan: RescuePlan, action_kinds: tuple[RescueActionKind, ...]
) -> dict[str, object]:
    return {
        key: value
        for action in plan.actions
        if action.kind in action_kinds
        for key, value in action.parameters.items()
    }


def _stream_hash(path: Path) -> str | None:
    try:
        pinned = pinned_subprocess_options((str(path),))
        info = os.stat(path) if pinned else os.lstat(path)
        if not stat.S_ISREG(info.st_mode):
            return None
        digest = sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except (OSError, PinnedDescriptorError):
        return None


def _artifact_integrity(candidate: MediaVerificationSnapshot) -> bool:
    try:
        info = os.lstat(candidate.path)
        return (
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and _safe_relative(candidate.relative_path)
            and _stream_hash(candidate.path) == candidate.sha256
        )
    except OSError:
        return False


def _safe_relative(value: str) -> bool:
    posix, windows = PurePosixPath(value), PureWindowsPath(value)
    return bool(
        value
        and value == posix.as_posix()
        and "\\" not in value
        and not posix.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and ".." not in posix.parts
        and "." not in posix.parts
    )


def _mappings_are_complete(
    mappings: Sequence[SourceMapping], candidate_duration: float
) -> bool:
    ordered = tuple(
        sorted(mappings, key=lambda item: (item.output_start, item.source_start))
    )
    if not ordered or not math.isclose(ordered[0].output_start, 0.0, abs_tol=1e-9):
        return False
    previous_source_end = -1.0
    previous_output_end = 0.0
    total_source_duration = 0.0
    for item in ordered:
        values = (
            item.source_start,
            item.source_end,
            item.output_start,
            item.output_end,
        )
        if any(not math.isfinite(value) or value < 0 for value in values):
            return False
        if item.source_end <= item.source_start or item.output_end <= item.output_start:
            return False
        if item.source_start < previous_source_end:
            return False
        if not math.isclose(item.output_start, previous_output_end, abs_tol=1e-9):
            return False
        if not math.isclose(
            item.source_end - item.source_start,
            item.output_end - item.output_start,
            abs_tol=DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS,
        ):
            return False
        if not _mapping_output_name_allowed(item.output_relative_path):
            return False
        total_source_duration += item.source_end - item.source_start
        previous_source_end, previous_output_end = item.source_end, item.output_end
    return math.isclose(
        previous_output_end,
        candidate_duration,
        abs_tol=DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS,
    ) and math.isclose(
        total_source_duration,
        previous_output_end,
        abs_tol=DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS,
    )


def _mappings_cover_full_source(
    mappings: Sequence[SourceMapping], source_duration: float
) -> bool:
    cursor = 0.0
    for item in sorted(
        mappings, key=lambda value: (value.source_start, value.source_end)
    ):
        if not math.isclose(item.source_start, cursor, abs_tol=1e-9):
            return False
        cursor = item.source_end
    return math.isclose(cursor, source_duration, abs_tol=1e-9)


def _visual_comparison_values(
    applicable: bool,
    measured: dict[str, JsonValue],
    *,
    reference: str,
    reason: str | None,
) -> dict[str, JsonValue]:
    values: dict[str, JsonValue] = {
        "applicable": applicable,
        "reference": reference,
        **measured,
    }
    if not applicable:
        values["reason"] = reason or "visual_reference_unavailable"
    return values


def _parameter(parameters: Mapping[str, object], name: str, default: float) -> float:
    value = _number(parameters.get(name))
    return value if value is not None else default


def _mapping_output_name_allowed(value: str) -> bool:
    if value == "faithful-rescue.mp4":
        return True
    return bool(
        _safe_relative(value)
        and value.startswith("faithful-segment-")
        and value.endswith(".mp4")
        and len(value) == len("faithful-segment-0001.mp4")
        and value[len("faithful-segment-") : -4].isdigit()
    )


def _sharpness_ok(
    source: MediaVerificationSnapshot,
    candidate: MediaVerificationSnapshot,
    parameters: Mapping[str, object],
) -> bool:
    if source.sharpness <= 0:
        return candidate.sharpness >= 0
    maximum_loss = _parameter(
        parameters, "maximum_sharpness_loss_ratio", _DEFAULT_MAX_SHARPNESS_LOSS_RATIO
    )
    return (source.sharpness - candidate.sharpness) / source.sharpness <= maximum_loss


def _fixed_offset_ok(
    candidate: MediaVerificationSnapshot, parameters: Mapping[str, object]
) -> bool:
    offset, shift = (
        _number(parameters.get("offset_seconds")),
        _number(parameters.get("audio_shift_seconds")),
    )
    if offset is None and shift is None:
        return True
    return bool(
        offset is not None
        and shift is not None
        and candidate.av_offset_seconds is not None
        and candidate.av_offset_method == _PACKET_TIMESTAMP_METHOD
        and bool(candidate.av_offset_tool_version)
        and math.isclose(shift, -offset, abs_tol=1e-9)
        and math.isclose(
            candidate.av_offset_seconds,
            0.0,
            rel_tol=0.0,
            abs_tol=_DEFAULT_AV_OFFSET_TOLERANCE_SECONDS,
        )
    )


def _offset_values(
    candidate: MediaVerificationSnapshot, parameters: Mapping[str, object]
) -> dict[str, JsonValue]:
    applicable = "offset_seconds" in parameters or "audio_shift_seconds" in parameters
    return {
        "applicable": applicable,
        "measurement_method": candidate.av_offset_method,
        "tool_version": candidate.av_offset_tool_version,
        "planned_offset_seconds": _number(parameters.get("offset_seconds")),
        "planned_shift_seconds": _number(parameters.get("audio_shift_seconds")),
        "observed_residual_seconds": candidate.av_offset_seconds,
        "tolerance_seconds": _DEFAULT_AV_OFFSET_TOLERANCE_SECONDS,
    }


def _loudness_ok(
    candidate: MediaVerificationSnapshot, parameters: Mapping[str, object]
) -> bool:
    target = _number(parameters.get("target_integrated_lufs"))
    if target is None:
        target = _number(parameters.get("target_lufs"))
    if target is None:
        return True
    tolerance = _parameter(
        parameters, "loudness_tolerance_lu", _DEFAULT_LOUDNESS_TOLERANCE_LU
    )
    return (
        candidate.integrated_lufs is not None
        and abs(candidate.integrated_lufs - target) <= tolerance
    )


def _audio_sample_rate_ok(
    source: MediaVerificationSnapshot,
    candidate: MediaVerificationSnapshot,
    parameters: Mapping[str, object],
) -> bool:
    planned = _number(parameters.get("output_sample_rate_hz"))
    if planned is None:
        return True
    return bool(
        source.audio_sample_rate_hz is not None
        and candidate.audio_sample_rate_hz is not None
        and candidate.audio_sample_rate_hz == source.audio_sample_rate_hz
        and math.isclose(float(candidate.audio_sample_rate_hz), planned, abs_tol=0.0)
    )


def _audio_sample_rate_values(
    source: MediaVerificationSnapshot,
    candidate: MediaVerificationSnapshot,
    parameters: Mapping[str, object],
) -> dict[str, JsonValue]:
    planned = _number(parameters.get("output_sample_rate_hz"))
    return {
        "applicable": planned is not None,
        "source_hz": source.audio_sample_rate_hz,
        "planned_hz": planned,
        "output_hz": candidate.audio_sample_rate_hz,
    }


def _loudness_values(
    candidate: MediaVerificationSnapshot, parameters: Mapping[str, object]
) -> dict[str, JsonValue]:
    target = _number(parameters.get("target_integrated_lufs"))
    if target is None:
        target = _number(parameters.get("target_lufs"))
    return {
        "applicable": target is not None,
        "target_lufs": target,
        "observed_lufs": candidate.integrated_lufs,
        "tolerance_lu": _parameter(
            parameters, "loudness_tolerance_lu", _DEFAULT_LOUDNESS_TOLERANCE_LU
        ),
    }


def _peak_ok(
    candidate: MediaVerificationSnapshot, parameters: Mapping[str, object]
) -> bool:
    limit = _number(parameters.get("true_peak_limit_dbtp"))
    if limit is None:
        return True
    return candidate.true_peak_dbtp is not None and candidate.true_peak_dbtp <= limit


def _peak_values(
    candidate: MediaVerificationSnapshot, parameters: Mapping[str, object]
) -> dict[str, JsonValue]:
    limit = _number(parameters.get("true_peak_limit_dbtp"))
    return {
        "applicable": limit is not None,
        "limit_dbtp": limit if limit is not None else _DEFAULT_TRUE_PEAK_LIMIT_DBTP,
        "observed_dbtp": candidate.true_peak_dbtp,
    }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _stream_type(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    stream_type = value.get("codec_type")
    return stream_type if isinstance(stream_type, str) else None


def _stream_indexes(streams: Sequence[object]) -> set[int]:
    indexes: set[int] = set()
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        index = stream.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            indexes.add(index)
    return indexes


def _stream_sample_rates(streams: Sequence[object]) -> dict[int, float]:
    rates: dict[int, float] = {}
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        index = stream.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            continue
        rate = _finite_positive_number(stream.get("sample_rate"))
        if rate is not None:
            rates[index] = rate
    return rates


def _first_packet_timestamp(
    packets: Sequence[object],
    stream_indexes: set[int],
    sample_rates: Mapping[int, float],
) -> float | None:
    for packet in packets:
        if not isinstance(packet, dict):
            continue
        stream_index = packet.get("stream_index")
        if (
            not isinstance(stream_index, int)
            or isinstance(stream_index, bool)
            or stream_index < 0
        ):
            continue
        if stream_index not in stream_indexes:
            continue
        raw = packet.get("pts_time") if "pts_time" in packet else packet.get("dts_time")
        timestamp = _finite_number(raw)
        if timestamp is None:
            continue
        has_skip_samples, skip_samples = _packet_skip_samples(packet)
        if has_skip_samples:
            sample_rate = sample_rates.get(stream_index)
            if skip_samples is None or sample_rate is None:
                return None
            timestamp += skip_samples / sample_rate
            rounding_tolerance = 0.5 / sample_rate
            if -rounding_tolerance <= timestamp < 0:
                timestamp = 0.0
        if timestamp >= 0:
            return timestamp
    return None


def _packet_skip_samples(packet: Mapping[str, object]) -> tuple[bool, int | None]:
    if "side_data_list" not in packet:
        return False, None
    entries = packet.get("side_data_list")
    if not isinstance(entries, list):
        return True, None
    matching = [
        entry
        for entry in entries
        if isinstance(entry, dict) and entry.get("side_data_type") == "Skip Samples"
    ]
    if not matching:
        return False, None
    if len(matching) != 1:
        return True, None
    value = matching[0].get("skip_samples")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return True, None
    return True, value


def _finite_non_negative_number(raw: object) -> float | None:
    result = _finite_number(raw)
    return result if result is not None and result >= 0 else None


def _finite_positive_number(raw: object) -> float | None:
    result = _finite_number(raw)
    return result if result is not None and result > 0 else None


def _finite_number(raw: object) -> float | None:
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return None
    try:
        result = float(raw)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _measure_visual_stream(
    path: Path, cancellation_callback: Callable[[], bool]
) -> tuple[int, int, int, float, float, float]:
    return _measure_visual_stream_with_count(path, cancellation_callback)[0]


def _measure_visual_stream_with_count(
    path: Path,
    cancellation_callback: Callable[[], bool],
) -> tuple[tuple[int, int, int, float, float, float], int]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        return (0, 0, 0, 0.0, 0.0, 0.0), 0
    frames = 0
    clipped = 0
    pixels = 0
    noise_total = 0.0
    sharpness_total = 0.0
    black_events = 0
    freeze_events = 0
    flicker_events = 0
    in_black = False
    in_freeze = False
    previous_gray: np.ndarray | None = None
    previous_luma: float | None = None
    try:
        while True:
            if cancellation_callback():
                from videoscope.rescue.errors import RescueCancelledError

                raise RescueCancelledError("media measurement was cancelled")
            ok, frame = capture.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            luma = float(np.mean(gray))
            is_black = luma <= 8.0
            if is_black and not in_black:
                black_events += 1
            in_black = is_black
            if previous_gray is not None:
                difference = float(
                    np.mean(cv2.absdiff(gray, previous_gray), dtype=np.float64)
                )
                is_freeze = difference <= 0.5
                if is_freeze and not in_freeze:
                    freeze_events += 1
                in_freeze = is_freeze
            if previous_luma is not None and abs(luma - previous_luma) >= 24.0:
                flicker_events += 1
            clipped += int(np.count_nonzero((gray <= 1) | (gray >= 254)))
            pixels += int(gray.size)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            noise_total += float(np.mean(cv2.absdiff(gray, blurred))) / 255.0
            sharpness_total += float(cv2.Laplacian(gray, cv2.CV_64F).var()) / (
                255.0 * 255.0
            )
            frames += 1
            previous_gray = gray
            previous_luma = luma
    finally:
        capture.release()
    return (
        (
            black_events,
            freeze_events,
            flicker_events,
            clipped / pixels if pixels else 0.0,
            noise_total / frames if frames else 0.0,
            sharpness_total / frames if frames else 0.0,
        ),
        frames,
    )


def _measure_visual_ranges(
    path: Path,
    ranges: tuple[tuple[float, float], ...],
    cancellation_callback: Callable[[], bool],
) -> dict[str, float]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise ValueError("media ranges could not be opened")
    luma_values: list[float] = []
    clipped = 0
    pixels = 0
    noise_total = 0.0
    sharpness_total = 0.0
    frames = 0
    black_events = 0
    freeze_events = 0
    flicker_events = 0
    in_black = False
    in_freeze = False
    previous_gray: np.ndarray | None = None
    previous_luma: float | None = None
    previous_in_range = False
    try:
        while True:
            if cancellation_callback():
                raise RescueCancelledError("media range measurement was cancelled")
            ok, frame = capture.read()
            if not ok:
                break
            timestamp = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
            in_range = any(start <= timestamp < end for start, end in ranges)
            if not in_range:
                previous_gray = None
                previous_luma = None
                in_black = False
                in_freeze = False
                previous_in_range = False
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            luma = float(np.mean(gray))
            is_black = luma <= 8.0
            if is_black and not in_black:
                black_events += 1
            in_black = is_black
            if previous_in_range and previous_gray is not None:
                difference = float(
                    np.mean(cv2.absdiff(gray, previous_gray), dtype=np.float64)
                )
                is_freeze = difference <= 0.5
                if is_freeze and not in_freeze:
                    freeze_events += 1
                in_freeze = is_freeze
            if (
                previous_in_range
                and previous_luma is not None
                and abs(luma - previous_luma) >= 24.0
            ):
                flicker_events += 1
            normalized = gray.astype(np.float64) / 255.0
            luma_values.extend(np.percentile(normalized, (10, 50)).tolist())
            clipped += int(np.count_nonzero((gray <= 1) | (gray >= 254)))
            pixels += int(gray.size)
            blurred = cv2.GaussianBlur(gray, (3, 3), 0)
            noise_total += float(np.mean(cv2.absdiff(gray, blurred))) / 255.0
            sharpness_total += float(cv2.Laplacian(gray, cv2.CV_64F).var()) / (
                255.0 * 255.0
            )
            frames += 1
            previous_gray = gray
            previous_luma = luma
            previous_in_range = True
    finally:
        capture.release()
    if not luma_values or pixels == 0:
        raise ValueError("confirmed media ranges contain no decodable frames")
    p10_values = luma_values[0::2]
    p50_values = luma_values[1::2]
    return {
        "luma_p10": float(np.median(p10_values)),
        "luma_p50": float(np.median(p50_values)),
        "clipping_ratio": clipped / pixels,
        "noise_residual": noise_total / frames,
        "sharpness": sharpness_total / frames,
        "black_events": float(black_events),
        "freeze_events": float(freeze_events),
        "flicker_events": float(flicker_events),
    }


def _measure_stabilization(
    reference: Path,
    candidate: Path,
    ranges: tuple[tuple[float, float], ...],
    cancellation_callback: Callable[[], bool],
) -> dict[str, float]:
    """Independently measure motion reduction and the rendered crop ratio."""
    from videoscope.rescue.stabilization import (
        StabilizationConfig,
        estimate_motion_transforms,
    )

    if not ranges:
        raise ValueError("stabilization ranges are unavailable")
    step = 1.0 / _STABILIZATION_VERIFICATION_SAMPLE_RATE
    timestamp_values: list[float] = []
    for start, end in ranges:
        values: list[float] = (
            np.arange(start, end, step, dtype=np.float64).astype(float).tolist()
        )
        timestamp_values.extend(values)
    timestamps = tuple(timestamp_values)
    if len(timestamps) < 3:
        raise ValueError("stabilization ranges are too short to measure")

    def frames(path: Path) -> tuple[tuple[float, np.ndarray], ...]:
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise ValueError("stabilization candidate could not be opened")
        decoded: list[tuple[float, np.ndarray]] = []
        try:
            for timestamp in timestamps:
                if cancellation_callback():
                    raise RescueCancelledError(
                        "stabilization measurement was cancelled"
                    )
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                ok, frame = capture.read()
                if not ok:
                    continue
                decoded.append((timestamp, frame))
        finally:
            capture.release()
        if len(decoded) < 3:
            raise ValueError("stabilization ranges contain too few decoded frames")
        return tuple(decoded)

    source_frames = frames(reference)
    output_frames = frames(candidate)
    if tuple(item[0] for item in source_frames) != tuple(
        item[0] for item in output_frames
    ):
        raise ValueError("stabilization samples are not timestamp aligned")
    height, width = source_frames[0][1].shape[:2]
    config = StabilizationConfig(frame_width=width, frame_height=height)

    def motion(
        samples: tuple[tuple[float, np.ndarray], ...],
    ) -> tuple[float, float, int]:
        transforms = estimate_motion_transforms(samples, config)
        amplitudes = tuple(
            float(np.hypot(item.translation_x, item.translation_y))
            for item in transforms
            if item.inlier_ratio >= _STABILIZATION_MINIMUM_INLIER_RATIO
            and item.residual_pixels <= _STABILIZATION_MAXIMUM_RESIDUAL_PIXELS
        )
        if len(amplitudes) < 3:
            raise ValueError("too few reliable motion measurements")
        return (
            float(np.median(amplitudes)),
            float(np.percentile(amplitudes, 90)),
            len(amplitudes),
        )

    source_median, source_p90, source_count = motion(source_frames)
    output_median, output_p90, output_count = motion(output_frames)
    crop_values: list[float] = []
    for (_timestamp, source_frame), (_, output_frame) in zip(
        source_frames, output_frames, strict=True
    ):
        source_gray = cv2.cvtColor(source_frame, cv2.COLOR_BGR2GRAY)
        output_gray = cv2.cvtColor(output_frame, cv2.COLOR_BGR2GRAY)
        points = cv2.goodFeaturesToTrack(
            source_gray, maxCorners=300, qualityLevel=0.01, minDistance=5
        )
        if points is None or len(points) < 3:
            continue
        tracked, status, _errors = cv2.calcOpticalFlowPyrLK(
            source_gray, output_gray, points, None
        )
        if tracked is None or status is None:
            continue
        selected = status.reshape(-1).astype(bool)
        if int(selected.sum()) < 3:
            continue
        matrix, inliers = cv2.estimateAffinePartial2D(
            points[selected], tracked[selected], method=cv2.RANSAC
        )
        if matrix is None or inliers is None:
            continue
        if float(inliers.reshape(-1).mean()) < _STABILIZATION_MINIMUM_INLIER_RATIO:
            continue
        scale = float(math.hypot(float(matrix[0, 0]), float(matrix[1, 0])))
        if scale >= 1.0:
            crop_values.append(1.0 - 1.0 / scale)
    if len(crop_values) < 3:
        raise ValueError("stabilization crop could not be independently measured")
    return {
        "crop_ratio": float(np.median(crop_values)),
        "source_motion_median_pixels": source_median,
        "output_motion_median_pixels": output_median,
        "source_motion_p90_pixels": source_p90,
        "output_motion_p90_pixels": output_p90,
        "source_reliable_transforms": float(source_count),
        "output_reliable_transforms": float(output_count),
    }


def _compare_visual_ranges(
    reference: Path,
    candidate: Path,
    ranges: tuple[tuple[float, float], ...],
    cancellation_callback: Callable[[], bool],
) -> dict[str, float]:
    first = cv2.VideoCapture(str(reference))
    second = cv2.VideoCapture(str(candidate))
    if not first.isOpened() or not second.isOpened():
        first.release()
        second.release()
        raise ValueError("media comparison ranges could not be opened")
    frame_differences: list[float] = []
    frame_index = 0
    frame_rate = float(first.get(cv2.CAP_PROP_FPS))
    if not math.isfinite(frame_rate) or frame_rate <= 0:
        first.release()
        second.release()
        raise ValueError("media comparison frame rate is invalid")
    try:
        while True:
            if cancellation_callback():
                raise RescueCancelledError("media range comparison was cancelled")
            first_ok, first_frame = first.read()
            second_ok, second_frame = second.read()
            if first_ok != second_ok:
                raise ValueError("media comparison frame counts differ")
            if not first_ok:
                break
            timestamp = frame_index / frame_rate
            frame_index += 1
            if not any(start <= timestamp < end for start, end in ranges):
                continue
            if first_frame.shape != second_frame.shape:
                raise ValueError("media comparison frame shapes differ")
            difference = cv2.absdiff(first_frame, second_frame)
            frame_differences.append(
                float(np.mean(difference, dtype=np.float64)) / 255.0
            )
    finally:
        first.release()
        second.release()
    if not frame_differences:
        raise ValueError("media comparison contains no selected frames")
    return {
        "mean_absolute_pixel_difference": float(np.mean(frame_differences)),
        "p95_frame_difference": float(np.percentile(frame_differences, 95)),
        "compared_frames": float(len(frame_differences)),
    }


def _map_source_ranges_to_output(
    source_ranges: tuple[tuple[float, float], ...],
    mappings: tuple[SourceMapping, ...],
) -> tuple[tuple[float, float], ...]:
    output: list[tuple[float, float]] = []
    for source_start, source_end in source_ranges:
        for mapping in mappings:
            overlap_start = max(source_start, mapping.source_start)
            overlap_end = min(source_end, mapping.source_end)
            if overlap_end <= overlap_start:
                continue
            source_duration = mapping.source_end - mapping.source_start
            output_duration = mapping.output_end - mapping.output_start
            if source_duration <= 0 or output_duration <= 0:
                continue
            scale = output_duration / source_duration
            output.append(
                (
                    mapping.output_start
                    + (overlap_start - mapping.source_start) * scale,
                    mapping.output_start + (overlap_end - mapping.source_start) * scale,
                )
            )
    return tuple(sorted(output))


def _ranges_intersect(
    first: Sequence[tuple[float, float]],
    second: Sequence[tuple[float, float]],
) -> bool:
    return any(
        first_start < second_end and second_start < first_end
        for first_start, first_end in first
        for second_start, second_end in second
    )


def _exact_action_output_ranges(
    source_ranges: tuple[tuple[float, float], ...],
    mappings: tuple[SourceMapping, ...],
) -> tuple[tuple[float, float], ...]:
    """Map every confirmed action range exactly once or reject the obligation."""
    if not source_ranges or not mappings:
        raise ValueError("action mapping obligation is unavailable")
    if any(
        current[0] < previous[1]
        for previous, current in zip(source_ranges, source_ranges[1:], strict=False)
    ):
        raise ValueError("action source ranges are not ordered and disjoint")
    output: list[tuple[float, float]] = []
    for source_start, source_end in source_ranges:
        matches = tuple(
            mapping
            for mapping in mappings
            if mapping.source_start <= source_start and source_end <= mapping.source_end
        )
        if len(matches) != 1:
            raise ValueError("action source range is not retained exactly once")
        mapping = matches[0]
        source_duration = mapping.source_end - mapping.source_start
        output_duration = mapping.output_end - mapping.output_start
        if not math.isclose(
            source_duration,
            output_duration,
            rel_tol=0.0,
            abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
        ):
            raise ValueError("action mapping changes confirmed range duration")
        output_start = mapping.output_start + source_start - mapping.source_start
        output_end = mapping.output_start + source_end - mapping.source_start
        if not math.isclose(
            output_end - output_start,
            source_end - source_start,
            rel_tol=0.0,
            abs_tol=_PERCEPTUAL_TIME_TOLERANCE_SECONDS,
        ):
            raise ValueError("action mapped range duration is incomplete")
        output.append((output_start, output_end))
    if any(
        current[0] < previous[1]
        for previous, current in zip(output, output[1:], strict=False)
    ):
        raise ValueError("action output ranges are not ordered and disjoint")
    return tuple(output)


def _range_complement(
    ranges: tuple[tuple[float, float], ...], duration_seconds: float
) -> tuple[tuple[float, float], ...]:
    if duration_seconds <= 0:
        return ()
    merged: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        bounded_start = max(0.0, min(duration_seconds, start))
        bounded_end = max(bounded_start, min(duration_seconds, end))
        if bounded_end <= bounded_start:
            continue
        if merged and bounded_start <= merged[-1][1] + 1e-9:
            merged[-1] = (merged[-1][0], max(merged[-1][1], bounded_end))
        else:
            merged.append((bounded_start, bounded_end))
    output: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor:
            output.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_seconds:
        output.append((cursor, duration_seconds))
    return tuple(output)


__all__ = [
    "MediaMeasurementProvider",
    "MediaVerificationSnapshot",
    "NativeMediaMeasurementProvider",
    "ReferenceRenderOptions",
    "RescueVerifier",
]
