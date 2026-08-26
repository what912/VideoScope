"""Staged, shell-free faithful Video Rescue execution."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING, Final, Literal, Protocol

import numpy as np
from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.processes import pinned_subprocess_options
from videoscope.rescue.action_roles import faithful_restoration_action_ids
from videoscope.rescue.audio import (
    AudioDenoiseConfig,
    AudioNoiseInterval,
    LoudnessConfig,
    LoudnessMeasurement,
    audio_filter_fragment_from_actions,
    parse_loudnorm_measurement,
)
from videoscope.rescue.capabilities import require_executable_action_scopes
from videoscope.rescue.commands import (
    build_audio_improvement_command,
    build_audio_noise_measurement_command,
    build_decode_verification_command,
    build_faithful_concat_command,
    build_faithful_remux_command,
    build_faithful_segment_command,
    build_improved_viewing_command,
    build_keyframe_probe_command,
    build_loudnorm_measurement_command,
    build_media_probe_command,
    build_sharpen_qualification_command,
)
from videoscope.rescue.deblur import (
    BlurKernelEstimate,
    DeblurConfig,
    render_deblurred_video,
)
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueInputError,
    RescueMediaError,
)
from videoscope.rescue.models import (
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    canonical_video_encode_contract,
    validate_plan_video_encode_contracts,
    validate_rescue_plan_identity_contract,
)
from videoscope.rescue.qualification import (
    RuntimeVerificationControlHandle,
    SharpenQualificationEvidenceV1,
    SharpenVerificationControlHandle,
    SharpenVerificationControlRecipeV1,
    TonalVerificationControlHandle,
    TonalVerificationControlRecipeV1,
    VerificationControlHandle,
    VerificationControlRecipeV1,
    _map_exact_qualification_ranges,
)
from videoscope.rescue.timeline import (
    SourceMapping,
    mappings_for_ranges,
    retained_source_ranges,
    timestamp_in_half_open_range,
)
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    detect_local_tonal_interference,
    qualify_tonal_render_profiles,
    render_tonal_identity_audio,
    render_tonal_interference_reduced_audio,
    validate_tonal_profile_contracts,
)
from videoscope.rescue.tonal_qualification import (
    TonalAudioTimelineV1,
    TonalAudioTopologyV2,
    TonalEncodedQualificationEvidenceV3,
    audio_timeline_from_ffprobe_stdout,
    audio_topology_from_ffprobe_stdout,
    tonal_audio_timeline_probe_arguments,
    tonal_audio_topology_probe_arguments,
    validate_tonal_runtime_candidate,
    validate_tonal_runtime_parent,
)
from videoscope.video.errors import sanitize_diagnostic

if TYPE_CHECKING:
    from videoscope.rescue.stabilization import MotionTransform, StabilizationConfig
    from videoscope.rescue.visual import FlickerCorrectionPlan

DEFAULT_RESCUE_TIMEOUT_SECONDS: Final = 3600.0
DEFAULT_DURATION_TOLERANCE_SECONDS: Final = 0.25
MAX_COMMAND_STDOUT_BYTES: Final = 64 * 1024
MAX_COMMAND_STDERR_BYTES: Final = 8 * 1024
PROCESS_POLL_SECONDS: Final = 0.02
PROCESS_STOP_GRACE_SECONDS: Final = 0.5
_MEDIA_TIMING_PROBE_ATTEMPTS: Final = 2
_MEDIA_TIMING_TOLERANCE_SECONDS: Final = 0.002
_STAGING_RELATIVE_PATH: Final = Path("staging")
_FINAL_NAME: Final = "faithful-rescue.mp4"
_PARTIAL_NAME: Final = "faithful-rescue.partial.mp4"
_IMPROVED_NAME: Final = "improved-viewing.mp4"
_IMPROVED_PARTIAL_NAME: Final = "improved-viewing.partial.mp4"
_MANIFEST_NAME: Final = "segments.ffconcat"


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Bounded output from one local argument-array command."""

    returncode: int
    stderr_summary: str
    stdout_summary: str = ""


class ExternalCommandRunner(Protocol):
    """Injectable process boundary for bounded local media commands."""

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult: ...


class _BinaryTemporaryFile(Protocol):
    def flush(self) -> None: ...

    def tell(self) -> int: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...

    def read(self, size: int = -1) -> bytes: ...


@dataclass(frozen=True, slots=True)
class RescuedSegment:
    """One independently verified retained segment in the private staging tree."""

    source_start: float
    source_end: float
    output_start: float
    output_end: float
    output_relative_path: str

    @property
    def source_mapping(self) -> SourceMapping:
        return SourceMapping(
            source_start=self.source_start,
            source_end=self.source_end,
            output_start=self.output_start,
            output_end=self.output_end,
            output_relative_path=self.output_relative_path,
        )


@dataclass(frozen=True, slots=True)
class RescueExecutionResult:
    """Verified faithful staging output and deterministic source traceability."""

    output_path: Path
    output_relative_path: str
    segments: tuple[RescuedSegment, ...]
    source_mappings: tuple[SourceMapping, ...]
    failed_source_ranges: tuple[tuple[float, float], ...] = ()
    render_mode: Literal[
        "stream_copy", "single_reencode", "segment_concat_reencode"
    ] = "stream_copy"
    applied_action_ids: frozenset[str] = frozenset()
    verification_controls: tuple[RuntimeVerificationControlHandle, ...] = ()

    @property
    def is_partial(self) -> bool:
        return bool(self.failed_source_ranges)


@dataclass(frozen=True, slots=True)
class RescueImprovedExecutionResult:
    output_path: Path
    verification_controls: tuple[SharpenVerificationControlHandle, ...] = ()


@dataclass(frozen=True, slots=True)
class _VerifiedSegment:
    index: int
    source_start: float
    source_end: float
    path: Path
    measured_duration: float


def run_external_command(
    arguments: tuple[str, ...],
    *,
    timeout_seconds: float,
    sensitive_paths: tuple[Path, ...],
    cancellation_callback: Callable[[], bool],
) -> CommandResult:
    """Run one bounded child, terminating it promptly on timeout or cancellation."""
    if not arguments:
        raise ValueError("external command arguments cannot be empty")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    diagnostic_paths = sensitive_paths
    executable = Path(arguments[0])
    if executable.is_absolute():
        diagnostic_paths = (*diagnostic_paths, executable)
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        try:
            process = subprocess.Popen(
                list(arguments),
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                **pinned_subprocess_options(arguments),
            )
        except FileNotFoundError as exc:
            diagnostic = sanitize_diagnostic(str(exc), sensitive_paths=diagnostic_paths)
            raise RescueMediaError(
                f"required media executable was not found: {diagnostic}"
            ) from exc
        except OSError as exc:
            diagnostic = sanitize_diagnostic(str(exc), sensitive_paths=diagnostic_paths)
            raise RescueMediaError(
                f"media command could not start: {diagnostic}"
            ) from exc

        deadline = monotonic() + timeout_seconds
        while process.poll() is None:
            if cancellation_callback():
                _stop_process(process)
                raise RescueCancelledError("media child terminated after cancellation")
            if monotonic() >= deadline:
                _stop_process(process)
                diagnostic = _read_bounded_text(
                    stderr_file, MAX_COMMAND_STDERR_BYTES, tail=True
                )
                raise RescueMediaError(
                    "media command timed out: "
                    + sanitize_diagnostic(diagnostic, sensitive_paths=diagnostic_paths)
                )
            sleep(PROCESS_POLL_SECONDS)
        returncode = process.wait()
        stdout_summary = _read_bounded_text(
            stdout_file, MAX_COMMAND_STDOUT_BYTES, tail=False
        )
        raw_stderr = _read_bounded_text(
            stderr_file, MAX_COMMAND_STDERR_BYTES, tail=True
        )
        stderr_summary = sanitize_diagnostic(
            raw_stderr,
            sensitive_paths=diagnostic_paths,
        )
        # FFmpeg has emitted error-level decoder diagnostics while still returning
        # zero on some platform/version combinations, even with ``-xerror``.  A
        # strict, null-output decode is a verification command: any stderr that
        # survives ``-loglevel error`` means the candidate was not fully decoded.
        if (
            returncode == 0
            and "-xerror" in arguments
            and "-loglevel" in arguments
            and "error" in arguments
            and "-f" in arguments
            and "null" in arguments
            and raw_stderr.strip()
        ):
            returncode = 1
        return CommandResult(returncode, stderr_summary, stdout_summary)


class NativeRescueExecutor:
    """Create a faithful staged output from an already confirmed Rescue plan."""

    def __init__(
        self,
        *,
        runner: ExternalCommandRunner = run_external_command,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        timeout_seconds: float = DEFAULT_RESCUE_TIMEOUT_SECONDS,
        duration_tolerance_seconds: float = DEFAULT_DURATION_TOLERANCE_SECONDS,
        deblur_renderer: Callable[..., None] = render_deblurred_video,
        tonal_renderer: Callable[..., None] = render_tonal_interference_reduced_audio,
        tonal_identity_renderer: Callable[..., None] = render_tonal_identity_audio,
        stabilization_renderer: Callable[..., None] | None = None,
        verification_control_inspector: Callable[
            [Path, Path, Path, Callable[[], bool]],
            tuple[str, str, int, str, str, int, str, str, int],
        ]
        | None = None,
        sharpen_control_inspector: Callable[
            [Path, Path, Path, Callable[[], bool]], tuple[str, str, int]
        ]
        | None = None,
    ) -> None:
        if not ffmpeg or not ffprobe:
            raise ValueError("FFmpeg executable names must not be empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if (
            not math.isfinite(duration_tolerance_seconds)
            or duration_tolerance_seconds < 0
        ):
            raise ValueError(
                "duration_tolerance_seconds must be finite and non-negative"
            )
        self._runner = runner
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._timeout_seconds = timeout_seconds
        self._duration_tolerance_seconds = duration_tolerance_seconds
        self._deblur_renderer = deblur_renderer
        self._tonal_renderer = tonal_renderer
        self._tonal_identity_renderer = tonal_identity_renderer
        if stabilization_renderer is None:
            from videoscope.rescue.stabilization import render_stabilized_video

            self._stabilization_renderer = render_stabilized_video
        else:
            self._stabilization_renderer = stabilization_renderer
        self._verification_control_inspector = (
            verification_control_inspector or self._inspect_stabilization_control
        )
        self._sharpen_control_inspector = (
            sharpen_control_inspector or self._inspect_sharpen_runtime_controls
        )

    def measure_loudness(
        self,
        source: Path,
        work_root: Path,
        config: LoudnessConfig,
        cancellation_callback: Callable[[], bool],
    ) -> LoudnessMeasurement:
        """Run the loudnorm first pass and parse its bounded diagnostic JSON."""
        source = Path(source)
        work_root = Path(work_root)
        if not source.is_file():
            raise RescueInputError("source video was not found")
        result = self._run(
            build_loudnorm_measurement_command(source, config, ffmpeg=self._ffmpeg),
            source,
            work_root,
            cancellation_callback,
        )
        if result.returncode != 0:
            raise RescueMediaError(
                "loudness measurement command failed: " + result.stderr_summary
            )
        try:
            return parse_loudnorm_measurement(
                result.stderr_summary or result.stdout_summary
            )
        except ValueError as exc:
            raise RescueMediaError(
                "loudness measurement returned invalid JSON"
            ) from exc

    def measure_audio_noise(
        self,
        source: Path,
        work_root: Path,
        config: AudioDenoiseConfig,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[AudioNoiseInterval, ...]:
        """Locate sustained stationary noise-like audio with bounded local PCM."""
        source = Path(source)
        work_root = Path(work_root)
        if not source.is_file():
            raise RescueInputError("source video was not found")
        analysis_root = work_root / "audio-assessment"
        analysis_root.mkdir(parents=True, exist_ok=True)
        pcm_path = analysis_root / "noise-analysis.wav"
        try:
            result = self._run(
                build_audio_noise_measurement_command(
                    source, pcm_path, config, ffmpeg=self._ffmpeg
                ),
                source,
                work_root,
                cancellation_callback,
                extra_sensitive_paths=(pcm_path,),
            )
            if result.returncode != 0:
                raise RescueMediaError(
                    "audio noise measurement command failed: " + result.stderr_summary
                )
            return _measure_audio_noise_windows(pcm_path, config)
        finally:
            _discard(pcm_path)
            try:
                analysis_root.rmdir()
            except OSError:
                pass

    def measure_tonal_interference(
        self,
        source: Path,
        work_root: Path,
        metadata: VideoMetadata,
        config: TonalInterferenceConfig,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[InterferenceTone, ...]:
        """Decode bounded local PCM and measure persistent narrowband interference."""
        source = Path(source)
        work_root = Path(work_root)
        if not source.is_file():
            raise RescueInputError("source video was not found")
        staging = self._prepare_staging_root(work_root)
        pcm_path = staging / "tonal-measurement.f32le"
        self._validate_reserved_paths(source, work_root, (pcm_path,))
        try:
            probe = self._run(
                (
                    self._ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type,sample_rate,channels",
                    "-of",
                    "json",
                    str(source),
                ),
                source,
                work_root,
                cancellation_callback,
            )
            if probe.returncode != 0:
                raise RescueMediaError("tonal source probe failed")
            try:
                payload = json.loads(probe.stdout_summary)
                streams = payload["streams"]
                audio_stream = next(
                    stream for stream in streams if stream.get("codec_type") == "audio"
                )
                sample_rate_hz = int(audio_stream["sample_rate"])
                channel_count = int(audio_stream["channels"])
            except (
                KeyError,
                StopIteration,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ) as exc:
                raise RescueMediaError("tonal source probe was malformed") from exc
            if not (
                config.minimum_sample_rate_hz
                <= sample_rate_hz
                <= config.maximum_sample_rate_hz
                and 1 <= channel_count <= config.maximum_channels
            ):
                raise RescueMediaError(
                    "tonal source audio is outside configured bounds"
                )
            sample_count = math.ceil(metadata.duration_seconds * sample_rate_hz)
            window_size = max(2, round(config.window_seconds * sample_rate_hz))
            hop_size = max(1, round(config.hop_seconds * sample_rate_hz))
            measurement_windows = (
                0
                if sample_count < window_size
                else 1 + (sample_count - window_size) // hop_size
            )
            if measurement_windows > config.maximum_measurement_windows:
                raise RescueMediaError(
                    "tonal measurement inventory exceeds configured limit"
                )
            decoded = self._run(
                (
                    self._ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-n",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    str(channel_count),
                    "-ar",
                    str(sample_rate_hz),
                    "-c:a",
                    "pcm_f32le",
                    "-f",
                    "f32le",
                    str(pcm_path),
                ),
                source,
                work_root,
                cancellation_callback,
                extra_sensitive_paths=(pcm_path,),
            )
            if decoded.returncode != 0 or not pcm_path.is_file():
                raise RescueMediaError("tonal source audio decode failed")
            samples = np.fromfile(pcm_path, dtype="<f4")
            if samples.size == 0 or samples.size % channel_count:
                raise RescueMediaError("tonal decoded PCM was malformed")
            pcm = samples.reshape((-1, channel_count))
            detected: tuple[InterferenceTone, ...] = detect_local_tonal_interference(
                pcm, sample_rate_hz, config
            )
            qualified = qualify_tonal_render_profiles(
                pcm,
                sample_rate_hz,
                detected,
                config,
            )
            return tuple(
                next(
                    (
                        candidate
                        for candidate in qualified
                        if candidate.model_copy(update={"render_qualification": None})
                        == tone
                    ),
                    tone,
                )
                for tone in detected
            )
        finally:
            _discard(pcm_path)

    def execute_faithful(
        self,
        plan: RescuePlan,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
        *,
        _allow_unqualified_sharpen_draft: bool = False,
        _allow_unqualified_tonal_draft: bool = False,
    ) -> RescueExecutionResult:
        """Execute only structural faithful actions into ``staging/``."""
        _require_plan_video_encode_contracts(
            plan,
            allow_unqualified_sharpen_draft=_allow_unqualified_sharpen_draft,
            allow_unqualified_tonal_draft=_allow_unqualified_tonal_draft,
        )
        source = Path(source)
        work_root = Path(work_root)
        self._validate_source(plan, source)
        source_ranges = retained_source_ranges(plan)
        require_executable_action_scopes(
            plan, mappings_for_ranges(source_ranges, _FINAL_NAME)
        )
        _require_plan_identity_contract(plan)
        staging = self._prepare_staging_root(work_root)
        final_output = staging / _FINAL_NAME
        partial_output = staging / _PARTIAL_NAME
        segment_root = staging / "segments"
        segment_outputs = tuple(
            segment_root / f"segment-{index:03d}.mp4"
            for index in range(len(source_ranges))
        )
        segment_partials = tuple(
            segment_root / f"segment-{index:03d}.partial.mp4"
            for index in range(len(source_ranges))
        )
        manifest = staging / _MANIFEST_NAME
        reserved = (
            final_output,
            partial_output,
            manifest,
            *segment_outputs,
            *segment_partials,
        )
        self._validate_reserved_paths(source, work_root, reserved)
        self._check_cancelled(cancellation_callback)

        if _is_safe_remux_only(plan):
            stream_copy = self._source_is_mp4_copy_safe(
                source, work_root, cancellation_callback
            )
            return self._execute_single_output(
                plan=plan,
                source=source,
                work_root=work_root,
                final_output=final_output,
                partial_output=partial_output,
                source_range=source_ranges[0],
                stream_copy=stream_copy,
                cancellation_callback=cancellation_callback,
            )
        if not _has_segment_salvage(plan):
            return self._execute_single_output(
                plan=plan,
                source=source,
                work_root=work_root,
                final_output=final_output,
                partial_output=partial_output,
                source_range=source_ranges[0],
                stream_copy=False,
                cancellation_callback=cancellation_callback,
            )

        try:
            segment_root.mkdir(parents=True, exist_ok=False)
        except OSError as exc:
            raise RescueArtifactError(
                "segment staging directory could not be created"
            ) from exc
        verified: list[_VerifiedSegment] = []
        failed_ranges: list[tuple[float, float]] = []
        last_segment_error: RescueMediaError | None = None
        try:
            for index, (requested_start, requested_end) in enumerate(source_ranges):
                self._check_cancelled(cancellation_callback)
                partial = segment_partials[index]
                output = segment_outputs[index]
                try:
                    keyframe_start = self._validated_keyframe_start(
                        source,
                        requested_start,
                        requested_end,
                        work_root,
                        cancellation_callback,
                    )
                    if keyframe_start is None or keyframe_start >= requested_end:
                        raise RescueMediaError(
                            "retained range has no usable validated keyframe"
                        )
                    result = self._run(
                        build_faithful_segment_command(
                            source,
                            partial,
                            start_seconds=keyframe_start,
                            end_seconds=requested_end,
                            encode_config=plan.effective_config,
                            ffmpeg=self._ffmpeg,
                        ),
                        source,
                        work_root,
                        cancellation_callback,
                    )
                    if result.returncode != 0:
                        raise RescueMediaError(result.stderr_summary)
                    self._require_nonempty(partial, stage="retained segment")
                    measured_duration = self._verify_media(
                        partial, source, work_root, cancellation_callback
                    )
                    self._require_duration_match(
                        measured_duration,
                        requested_end - keyframe_start,
                        stage="retained segment",
                    )
                    self._require_source_unchanged(plan, source)
                    _replace_new(partial, output, stage="retained segment")
                    if keyframe_start > requested_start:
                        failed_ranges.append((requested_start, keyframe_start))
                    verified.append(
                        _VerifiedSegment(
                            index=index,
                            source_start=keyframe_start,
                            source_end=requested_end,
                            path=output,
                            measured_duration=measured_duration,
                        )
                    )
                except RescueCancelledError:
                    raise
                except RescueArtifactError:
                    raise
                except RescueMediaError as exc:
                    last_segment_error = exc
                    failed_ranges.append((requested_start, requested_end))
                finally:
                    _discard(partial)

            if not verified:
                if last_segment_error is not None:
                    raise last_segment_error
                raise RescueMediaError("no independently verified segment remained")
            self._write_concat_manifest(manifest, tuple(item.path for item in verified))
            self._check_cancelled(cancellation_callback)
            fixed_offset = next(
                (
                    action
                    for action in plan.actions
                    if action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
                ),
                None,
            )
            concat_audio_filter = (
                audio_filter_fragment_from_actions(
                    (fixed_offset.kind,), fixed_offset.parameters
                )
                if fixed_offset is not None
                else None
            )
            if fixed_offset is not None and concat_audio_filter is None:
                raise RescueMediaError(
                    "confirmed fixed A/V correction is not executable"
                )
            concat_result = self._run(
                build_faithful_concat_command(
                    manifest,
                    partial_output,
                    audio_filter=concat_audio_filter,
                    encode_config=plan.effective_config,
                    ffmpeg=self._ffmpeg,
                ),
                source,
                work_root,
                cancellation_callback,
            )
            if concat_result.returncode != 0:
                raise RescueMediaError(
                    "verified segments could not be concatenated: "
                    + concat_result.stderr_summary
                )
            self._require_nonempty(partial_output, stage="faithful output")
            final_duration = self._verify_media(
                partial_output, source, work_root, cancellation_callback
            )
            self._require_duration_match(
                final_duration,
                sum(item.measured_duration for item in verified),
                stage="concatenated faithful output",
            )
            self._require_source_unchanged(plan, source)
            self._check_cancelled(cancellation_callback)
            _replace_new(partial_output, final_output, stage="faithful output")
            segments, mappings = _mapped_segments(
                verified,
                work_root,
                final_output.relative_to(work_root).as_posix(),
                final_duration,
            )
            return RescueExecutionResult(
                output_path=final_output,
                output_relative_path=final_output.relative_to(work_root).as_posix(),
                segments=segments,
                source_mappings=mappings,
                failed_source_ranges=_normalized_failed_ranges(failed_ranges),
                render_mode="segment_concat_reencode",
            )
        finally:
            _discard(partial_output)

    def execute_audio_improved(
        self,
        plan: RescuePlan,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> RescueExecutionResult:
        """Render confirmed audio actions atomically while stream-copying video."""
        _require_plan_video_encode_contracts(plan)
        source = Path(source)
        work_root = Path(work_root)
        self._validate_source(plan, source)
        selected = tuple(
            action
            for action in plan.actions
            if action.kind
            in {
                RescueActionKind.NORMALIZE_AUDIO,
                RescueActionKind.DENOISE_AUDIO,
                RescueActionKind.CORRECT_FIXED_AV_OFFSET,
            }
        )
        if not selected:
            raise RescueMediaError("confirmed plan contains no measured audio action")
        parameters: dict[str, object] = {}
        for action in selected:
            parameters.update(action.parameters)
        _require_plan_identity_contract(plan)
        staging = self._prepare_staging_root(work_root)
        final_output = staging / _IMPROVED_NAME
        partial_output = staging / _IMPROVED_PARTIAL_NAME
        self._validate_reserved_paths(source, work_root, (final_output, partial_output))
        try:
            result = self._run(
                build_audio_improvement_command(
                    source,
                    partial_output,
                    tuple(action.kind for action in selected),
                    parameters,
                    ffmpeg=self._ffmpeg,
                ),
                source,
                work_root,
                cancellation_callback,
            )
            if result.returncode != 0:
                raise RescueMediaError(
                    "audio improvement command failed: " + result.stderr_summary
                )
            self._require_nonempty(partial_output, stage="audio improvement output")
            measured_duration = self._verify_media(
                partial_output, source, work_root, cancellation_callback
            )
            self._require_duration_match(
                measured_duration,
                _source_duration(plan),
                stage="audio improvement output",
            )
            self._require_source_unchanged(plan, source)
            self._check_cancelled(cancellation_callback)
            _replace_new(partial_output, final_output, stage="audio improvement output")
            relative_path = final_output.relative_to(work_root).as_posix()
            segment = RescuedSegment(
                source_start=0.0,
                source_end=_source_duration(plan),
                output_start=0.0,
                output_end=measured_duration,
                output_relative_path=relative_path,
            )
            return RescueExecutionResult(
                output_path=final_output,
                output_relative_path=relative_path,
                segments=(segment,),
                source_mappings=(segment.source_mapping,),
            )
        finally:
            _discard(partial_output)

    def execute_faithful_restoration(
        self,
        plan: RescuePlan,
        execution: RescueExecutionResult,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
        *,
        _allow_unqualified_sharpen_draft: bool = False,
    ) -> RescueExecutionResult:
        """Apply confirmed bounded cleanup and stabilization to the faithful copy."""
        _require_plan_video_encode_contracts(
            plan,
            allow_unqualified_sharpen_draft=_allow_unqualified_sharpen_draft,
        )
        selected_ids = faithful_restoration_action_ids(plan)
        if not selected_ids:
            return execution
        _require_plan_identity_contract(plan)
        staging = self._prepare_staging_root(Path(work_root))
        partial = staging / "faithful-restored.partial.mp4"
        deblurred_paths: list[Path] = []
        tonal_reduced = staging / "faithful-tonal.partial.mp4"
        tonal_control = staging / "faithful-tonal-identity-control.private.mp4"
        stabilized = staging / "faithful-anchor-stabilized.partial.mp4"
        stabilization_control = (
            staging / "faithful-stabilization-identity-control.private.mp4"
        )
        stabilization_parent = staging / "faithful-stabilization-parent.private.mp4"
        original = Path(execution.output_path)
        self._validate_reserved_paths(
            original,
            Path(work_root),
            (
                partial,
                tonal_reduced,
                tonal_control,
                stabilized,
                stabilization_control,
                stabilization_parent,
            ),
        )
        _expected_duration, audio_rate = self._probe_media(
            original, original, Path(work_root), cancellation_callback
        )
        deblur = next(
            (
                action
                for action in plan.actions
                if action.kind is RescueActionKind.DEBLUR and action.id in selected_ids
            ),
            None,
        )
        tonal = next(
            (
                action
                for action in plan.actions
                if action.kind is RescueActionKind.DENOISE_AUDIO
                and "interference_profiles" in action.parameters
                and action.id in selected_ids
            ),
            None,
        )
        stabilization = next(
            (
                action
                for action in plan.actions
                if action.kind is RescueActionKind.STABILIZE
                and action.id in selected_ids
            ),
            None,
        )
        native_ids = frozenset(
            action.id for action in (deblur, tonal, stabilization) if action is not None
        )
        filter_ids = selected_ids - native_ids
        applied_ids: set[str] = set()
        verification_controls: list[RuntimeVerificationControlHandle] = []
        retain_verification_controls = False
        try:
            candidate = original
            if filter_ids:
                command = build_improved_viewing_command(
                    plan,
                    original,
                    partial,
                    source_mappings=execution.source_mappings,
                    excluded_action_ids=frozenset(
                        action.id
                        for action in plan.actions
                        if action.id not in filter_ids
                    ),
                    audio_sample_rate_hz=audio_rate,
                    ffmpeg=self._ffmpeg,
                    _allow_unqualified_sharpen_draft=(_allow_unqualified_sharpen_draft),
                )
                result = self._run(
                    command,
                    original,
                    Path(work_root),
                    cancellation_callback,
                    extra_sensitive_paths=(partial,),
                )
                if result.returncode != 0:
                    raise RescueMediaError(
                        "faithful restoration command failed: " + result.stderr_summary
                    )
                self._require_nonempty(partial, stage="faithful restoration")
                candidate = partial
                applied_ids.update(filter_ids)
            if deblur is not None:
                deblur_operations = _deblur_operations(
                    deblur.parameters,
                    deblur.source_ranges,
                    execution.source_mappings,
                    expected_version=plan.effective_config.deblur_algorithm_version,
                )
                for index, (ranges, estimate, deblur_config) in enumerate(
                    deblur_operations
                ):
                    deblurred = staging / (
                        "faithful-deblurred.partial.mp4"
                        if index == 0
                        else f"faithful-deblurred-{index:02d}.partial.mp4"
                    )
                    self._validate_reserved_paths(
                        original, Path(work_root), (deblurred,)
                    )
                    deblurred_paths.append(deblurred)
                    self.execute_deblurred(
                        source=candidate,
                        output=deblurred,
                        ranges=ranges,
                        estimate=estimate,
                        config=deblur_config,
                        encode_config=plan.effective_config,
                        cancellation_callback=cancellation_callback,
                    )
                    self._require_nonempty(deblurred, stage="faithful deblur")
                    candidate = deblurred
                    self._check_cancelled(cancellation_callback)
                applied_ids.add(deblur.id)
            if tonal is not None:
                try:
                    tonal_evidence = (
                        TonalEncodedQualificationEvidenceV3.model_validate_json(
                            json.dumps(
                                tonal.parameters["encoded_candidate_qualification"],
                                ensure_ascii=False,
                            )
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise RescueMediaError(
                        "confirmed tonal encoded qualification is invalid"
                    ) from exc
                try:
                    validate_tonal_runtime_parent(
                        tonal_evidence,
                        execution.source_mappings,
                        parent_sha256=_sha256_file(candidate),
                        parent_audio_topology=self._probe_tonal_audio_topology(
                            candidate,
                            original,
                            Path(work_root),
                            cancellation_callback,
                        ),
                    )
                except ValueError as exc:
                    raise RescueMediaError(
                        "tonal encoded qualification parent differs from execution"
                    ) from exc
                tones, tonal_config = _tonal_operation(
                    tonal.parameters,
                    tonal.source_ranges,
                    execution.source_mappings,
                    expected_version=plan.effective_config.tonal_algorithm_version,
                )
                self.execute_tonal_identity(
                    source=candidate,
                    output=tonal_control,
                    config=tonal_config,
                    cancellation_callback=cancellation_callback,
                )
                self._require_nonempty(
                    tonal_control, stage="faithful tonal identity control"
                )
                control_topology = self._probe_tonal_audio_topology(
                    tonal_control,
                    original,
                    Path(work_root),
                    cancellation_callback,
                )
                control_timeline = self._probe_tonal_audio_timeline(
                    tonal_control,
                    original,
                    Path(work_root),
                    cancellation_callback,
                )
                if (
                    _sha256_file(tonal_control)
                    != tonal_evidence.boundary_control_sha256
                    or control_topology
                    != tonal_evidence.boundary_control_audio_topology
                    or control_timeline
                    != tonal_evidence.boundary_control_audio_timeline
                ):
                    raise RescueMediaError(
                        "tonal identity control differs from qualification"
                    )
                self.execute_tonal_reduced(
                    source=candidate,
                    output=tonal_reduced,
                    tones=tones,
                    config=tonal_config,
                    cancellation_callback=cancellation_callback,
                )
                self._require_nonempty(
                    tonal_reduced, stage="faithful tonal interference reduction"
                )
                candidate_topology = self._probe_tonal_audio_topology(
                    tonal_reduced,
                    original,
                    Path(work_root),
                    cancellation_callback,
                )
                candidate_timeline = self._probe_tonal_audio_timeline(
                    tonal_reduced,
                    original,
                    Path(work_root),
                    cancellation_callback,
                )
                try:
                    validate_tonal_runtime_candidate(
                        tonal_evidence,
                        candidate_sha256=_sha256_file(tonal_reduced),
                        candidate_audio_topology=candidate_topology,
                    )
                    if candidate_timeline != tonal_evidence.combined_audio_timeline:
                        raise ValueError("tonal encoded candidate timeline differs")
                except ValueError as exc:
                    raise RescueMediaError(
                        "tonal encoded candidate differs from qualification"
                    ) from exc
                verification_controls.append(
                    TonalVerificationControlHandle(
                        path=tonal_control,
                        recipe=TonalVerificationControlRecipeV1(
                            plan_digest=plan.plan_digest,
                            action_id=tonal.id,
                            parent_sha256=tonal_evidence.parent_sha256,
                            control_sha256=tonal_evidence.boundary_control_sha256,
                            qualified_candidate_sha256=(
                                tonal_evidence.combined_candidate_sha256 or ""
                            ),
                            source_ranges=tonal.source_ranges,
                            output_ranges=tonal_evidence.output_ranges,
                            encode_contract=(
                                tonal_evidence.audio_encode_contract.model_dump(
                                    mode="json"
                                )
                            ),
                            control_audio_topology=control_topology.model_dump(
                                mode="json"
                            ),
                            candidate_audio_topology=(
                                tonal_evidence.combined_audio_topology.model_dump(
                                    mode="json"
                                )
                                if tonal_evidence.combined_audio_topology is not None
                                else {}
                            ),
                            control_audio_timeline=(
                                control_timeline.model_dump(mode="json")
                            ),
                            candidate_audio_timeline=(
                                candidate_timeline.model_dump(mode="json")
                            ),
                        ),
                    )
                )
                candidate = tonal_reduced
                applied_ids.add(tonal.id)
                self._check_cancelled(cancellation_callback)
            if stabilization is not None:
                transforms, stabilization_config = _stabilization_operation(
                    stabilization.parameters,
                    stabilization.source_ranges,
                    execution.source_mappings,
                    expected_version=(
                        plan.effective_config.anchor_stabilization_algorithm_version
                    ),
                )
                identity_transforms = tuple(
                    transform.model_copy(
                        update={
                            "rotation_degrees": 0.0,
                            "scale": 1.0,
                            "translation_x": 0.0,
                            "translation_y": 0.0,
                        }
                    )
                    for transform in transforms
                )
                shutil.copyfile(candidate, stabilization_parent)
                self._require_nonempty(
                    stabilization_parent, stage="stabilization retained parent"
                )
                self.execute_stabilized(
                    source=stabilization_parent,
                    output=stabilization_control,
                    transforms=identity_transforms,
                    config=stabilization_config,
                    encode_config=plan.effective_config,
                    cancellation_callback=cancellation_callback,
                )
                self._require_nonempty(
                    stabilization_control,
                    stage="stabilization identity verification control",
                )
                self.execute_stabilized(
                    source=stabilization_parent,
                    output=stabilized,
                    transforms=transforms,
                    config=stabilization_config,
                    encode_config=plan.effective_config,
                    cancellation_callback=cancellation_callback,
                )
                self._require_nonempty(stabilized, stage="faithful stabilization")
                verification_controls.append(
                    self._build_stabilization_control_handle(
                        plan=plan,
                        action_id=stabilization.id,
                        source_ranges=stabilization.source_ranges,
                        parent=stabilization_parent,
                        control=stabilization_control,
                        candidate=stabilized,
                        cancellation_callback=cancellation_callback,
                    )
                )
                candidate = stabilized
                applied_ids.add(stabilization.id)
            observed_duration = self._verify_media(
                candidate, original, Path(work_root), cancellation_callback
            )
            self._require_duration_match(
                observed_duration,
                _expected_duration,
                stage="faithful restoration",
            )
            try:
                os.replace(candidate, original)
            except OSError as exc:
                raise RescueArtifactError(
                    "faithful restoration could not replace the staged candidate "
                    "atomically"
                ) from exc
            retain_verification_controls = bool(verification_controls)
            return replace(
                execution,
                render_mode="single_reencode",
                applied_action_ids=(
                    execution.applied_action_ids | frozenset(applied_ids)
                ),
                verification_controls=(
                    execution.verification_controls + tuple(verification_controls)
                ),
            )
        finally:
            _discard(partial)
            for deblurred in deblurred_paths:
                _discard(deblurred)
            _discard(tonal_reduced)
            _discard(stabilized)
            if not retain_verification_controls:
                _discard(tonal_control)
                _discard(stabilization_control)
                _discard(stabilization_parent)

    def _build_stabilization_control_handle(
        self,
        *,
        plan: RescuePlan,
        action_id: str,
        source_ranges: tuple[tuple[float, float], ...],
        parent: Path,
        control: Path,
        candidate: Path,
        cancellation_callback: Callable[[], bool],
    ) -> VerificationControlHandle:
        """Bind one exact-PTS identity sibling without exposing its path."""
        (
            pts_digest,
            topology_digest,
            frame_count,
            parent_pts_digest,
            parent_topology_digest,
            parent_frame_count,
            candidate_pts_digest,
            candidate_topology_digest,
            candidate_frame_count,
        ) = self._verification_control_inspector(
            parent, control, candidate, cancellation_callback
        )
        recipe = VerificationControlRecipeV1(
            plan_digest=plan.plan_digest,
            action_id=action_id,
            parent_sha256=_sha256_file(parent),
            control_sha256=_sha256_file(control),
            candidate_sha256=_sha256_file(candidate),
            encode_contract=canonical_video_encode_contract(plan.effective_config),
            normalized_pts_digest=pts_digest,
            stream_topology_digest=topology_digest,
            parent_normalized_pts_digest=parent_pts_digest,
            parent_stream_topology_digest=parent_topology_digest,
            candidate_normalized_pts_digest=candidate_pts_digest,
            candidate_stream_topology_digest=candidate_topology_digest,
            source_ranges=source_ranges,
            frame_count=frame_count,
            parent_frame_count=parent_frame_count,
            candidate_frame_count=candidate_frame_count,
        )
        return VerificationControlHandle(
            path=control, parent_path=parent, recipe=recipe
        )

    def _inspect_stabilization_control(
        self,
        parent: Path,
        control: Path,
        candidate: Path,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[str, str, int, str, str, int, str, str, int]:
        """Require exact PTS and topology across the same-generation triple."""
        from videoscope.rescue.verification import (
            _probe_sharpen_video_topology,
            _probe_video_timestamp_inventory,
        )

        parent_inventory = _probe_video_timestamp_inventory(
            parent,
            self._ffprobe,
            self._runner,
            self._timeout_seconds,
            cancellation_callback,
        )
        control_inventory = _probe_video_timestamp_inventory(
            control,
            self._ffprobe,
            self._runner,
            self._timeout_seconds,
            cancellation_callback,
        )
        candidate_inventory = _probe_video_timestamp_inventory(
            candidate,
            self._ffprobe,
            self._runner,
            self._timeout_seconds,
            cancellation_callback,
        )
        if not (
            parent_inventory.timestamps
            == control_inventory.timestamps
            == candidate_inventory.timestamps
        ):
            raise RescueMediaError(
                "stabilization control/candidate PTS inventory differs from its parent"
            )
        _topology, topology_digest = _probe_sharpen_video_topology(
            control,
            self._ffprobe,
            self._runner,
            self._timeout_seconds,
            cancellation_callback,
        )
        _parent_topology, parent_topology_digest = _probe_sharpen_video_topology(
            parent,
            self._ffprobe,
            self._runner,
            self._timeout_seconds,
            cancellation_callback,
        )
        _candidate_topology, candidate_topology_digest = _probe_sharpen_video_topology(
            candidate,
            self._ffprobe,
            self._runner,
            self._timeout_seconds,
            cancellation_callback,
        )
        if topology_digest != candidate_topology_digest:
            raise RescueMediaError("stabilization control/candidate topology differs")
        pts_payload = json.dumps(
            control_inventory.timestamps,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return (
            sha256(pts_payload).hexdigest(),
            topology_digest,
            len(control_inventory.timestamps),
            sha256(pts_payload).hexdigest(),
            parent_topology_digest,
            len(parent_inventory.timestamps),
            sha256(pts_payload).hexdigest(),
            candidate_topology_digest,
            len(candidate_inventory.timestamps),
        )

    def execute_improved(
        self,
        plan: RescuePlan,
        faithful: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
        source_mappings: tuple[SourceMapping, ...] | None = None,
        inherited_action_ids: frozenset[str] = frozenset(),
    ) -> Path:
        """Compatibility boundary that never leaks private runtime controls."""
        result = self.execute_improved_with_controls(
            plan,
            faithful,
            work_root,
            cancellation_callback,
            source_mappings=source_mappings,
            inherited_action_ids=inherited_action_ids,
            generate_verification_controls=False,
        )
        cleanup_failed = False
        for handle in result.verification_controls:
            for path in handle.cleanup_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    cleanup_failed = True
        if cleanup_failed or any(
            path.exists()
            for handle in result.verification_controls
            for path in handle.cleanup_paths
        ):
            raise RescueArtifactError("private SHARPEN control cleanup failed")
        return result.output_path

    def execute_improved_with_controls(
        self,
        plan: RescuePlan,
        faithful: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
        source_mappings: tuple[SourceMapping, ...] | None = None,
        inherited_action_ids: frozenset[str] = frozenset(),
        generate_verification_controls: bool = True,
        *,
        _allow_unqualified_sharpen_draft: bool = False,
    ) -> RescueImprovedExecutionResult:
        """Render the exact bounded filters recorded in a confirmed plan."""
        _require_plan_video_encode_contracts(
            plan,
            allow_unqualified_sharpen_draft=_allow_unqualified_sharpen_draft,
        )
        faithful = Path(faithful)
        work_root = Path(work_root)
        if not faithful.is_file():
            raise RescueInputError("verified faithful candidate was not found")
        require_executable_action_scopes(plan, source_mappings)
        _require_plan_identity_contract(plan)
        selected_sharpen_qualification = None
        if not _allow_unqualified_sharpen_draft:
            sharpen_actions = tuple(
                action
                for action in plan.actions
                if action.kind is RescueActionKind.SHARPEN
            )
            if sharpen_actions:
                qualified_sharpen = sharpen_actions[0]
                evidence = SharpenQualificationEvidenceV1.model_validate(
                    qualified_sharpen.parameters.get("qualification")
                )
                selected_sharpen_qualification = evidence.selected
                if selected_sharpen_qualification is None:
                    raise RescueMediaError(
                        "confirmed SHARPEN qualification has no selected profile"
                    )
                expected_output_ranges = _map_exact_qualification_ranges(
                    qualified_sharpen.source_ranges,
                    source_mappings
                    or mappings_for_ranges(retained_source_ranges(plan), _FINAL_NAME),
                )
                if evidence.output_ranges != expected_output_ranges:
                    raise RescueMediaError(
                        "confirmed SHARPEN qualification output ranges differ"
                    )
        faithful_hash = _sha256_file(faithful)
        staging = self._prepare_staging_root(work_root)
        final_output = staging / _IMPROVED_NAME
        partial_output = staging / _IMPROVED_PARTIAL_NAME
        stabilized_output = staging / "improved-stabilized.partial.mp4"
        deflickered_output = staging / "improved-deflickered.partial.mp4"
        sharpen_baseline = staging / "improved-sharpen-baseline.private.mp4"
        sharpen_visibility = staging / "improved-sharpen-visibility.private.mp4"
        self._validate_reserved_paths(
            faithful,
            work_root,
            (
                final_output,
                partial_output,
                stabilized_output,
                deflickered_output,
                sharpen_baseline,
                sharpen_visibility,
            ),
        )
        self._check_cancelled(cancellation_callback)
        retain_sharpen_controls = False
        try:
            expected_duration, audio_sample_rate_hz = self._probe_media(
                faithful, faithful, work_root, cancellation_callback
            )
            render_source = faithful
            stabilization = next(
                (
                    action
                    for action in plan.actions
                    if action.kind is RescueActionKind.STABILIZE
                    and action.id not in inherited_action_ids
                ),
                None,
            )
            if stabilization is not None:
                transforms, config = _stabilization_operation(
                    stabilization.parameters,
                    stabilization.source_ranges,
                    source_mappings
                    or mappings_for_ranges(
                        retained_source_ranges(plan), "faithful-rescue.mp4"
                    ),
                    expected_version=(
                        plan.effective_config.anchor_stabilization_algorithm_version
                    ),
                )
                self.execute_stabilized(
                    source=faithful,
                    output=stabilized_output,
                    transforms=transforms,
                    config=config,
                    encode_config=plan.effective_config,
                    cancellation_callback=cancellation_callback,
                )
                self._require_nonempty(
                    stabilized_output, stage="stabilized improved candidate"
                )
                render_source = stabilized_output
                self._check_cancelled(cancellation_callback)

            faithful_restoration_ids = faithful_restoration_action_ids(plan)
            excluded_filter_action_ids = faithful_restoration_ids
            deflicker = next(
                (
                    action
                    for action in plan.actions
                    if action.kind is RescueActionKind.DEFLICKER
                ),
                None,
            )
            if deflicker is not None:
                from videoscope.rescue.visual import (
                    flicker_correction_from_parameters,
                    remap_flicker_correction,
                )

                try:
                    correction = flicker_correction_from_parameters(
                        deflicker.parameters
                    )
                    resolved_mappings = (
                        source_mappings
                        if source_mappings is not None
                        else mappings_for_ranges(
                            retained_source_ranges(plan), _FINAL_NAME
                        )
                    )
                    mapped_correction = remap_flicker_correction(
                        correction,
                        deflicker.source_ranges,
                        resolved_mappings,
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise RescueMediaError(
                        "confirmed deflicker parameters are invalid"
                    ) from exc
                if mapped_correction is None:
                    raise RescueMediaError(
                        "confirmed deflicker has no executable retained interval"
                    )
                self.execute_deflickered(
                    source=render_source,
                    output=deflickered_output,
                    correction=mapped_correction,
                    encode_config=plan.effective_config,
                    cancellation_callback=cancellation_callback,
                )
                self._require_nonempty(
                    deflickered_output, stage="deflickered improved candidate"
                )
                render_source = deflickered_output
                excluded_filter_action_ids = excluded_filter_action_ids | frozenset(
                    (deflicker.id,)
                )
                self._check_cancelled(cancellation_callback)

            sharpen = next(
                (
                    action
                    for action in plan.actions
                    if action.kind is RescueActionKind.SHARPEN
                    and action.id not in excluded_filter_action_ids
                ),
                None,
            )
            if not generate_verification_controls:
                sharpen = None
            if sharpen is not None:
                control_outputs: tuple[
                    tuple[Literal["baseline", "visibility"], Path], ...
                ] = (
                    ("baseline", sharpen_baseline),
                    ("visibility", sharpen_visibility),
                )
                for mode, output in control_outputs:
                    control_command = build_improved_viewing_command(
                        plan,
                        render_source,
                        output,
                        source_mappings=source_mappings,
                        excluded_action_ids=excluded_filter_action_ids,
                        audio_sample_rate_hz=audio_sample_rate_hz,
                        sharpen_mode=mode,
                        force_video_encode=True,
                        ffmpeg=self._ffmpeg,
                        _allow_unqualified_sharpen_draft=(
                            _allow_unqualified_sharpen_draft
                        ),
                    )
                    control_result = self._run(
                        control_command,
                        faithful,
                        work_root,
                        cancellation_callback,
                        extra_sensitive_paths=(render_source, output),
                    )
                    if control_result.returncode != 0:
                        raise RescueMediaError(
                            "private SHARPEN verification control failed: "
                            + control_result.stderr_summary
                        )
                    self._require_nonempty(
                        output, stage="private SHARPEN verification control"
                    )
                    self._check_cancelled(cancellation_callback)

            try:
                command = build_improved_viewing_command(
                    plan,
                    render_source,
                    partial_output,
                    source_mappings=source_mappings,
                    excluded_action_ids=excluded_filter_action_ids,
                    audio_sample_rate_hz=audio_sample_rate_hz,
                    ffmpeg=self._ffmpeg,
                    _allow_unqualified_sharpen_draft=(_allow_unqualified_sharpen_draft),
                )
            except ValueError as exc:
                remaining_improvements = any(
                    action.kind
                    in {
                        RescueActionKind.ADJUST_LUMA,
                        RescueActionKind.DENOISE_VIDEO,
                        RescueActionKind.SHARPEN,
                        RescueActionKind.NORMALIZE_AUDIO,
                        RescueActionKind.DENOISE_AUDIO,
                    }
                    for action in plan.actions
                    if action.id not in excluded_filter_action_ids
                )
                if remaining_improvements:
                    raise RescueMediaError(
                        "confirmed improvement has no executable bound operation"
                    ) from exc
                if stabilization is not None or deflicker is not None:
                    candidate = render_source
                else:
                    raise RescueMediaError(
                        "confirmed improvement has no executable bound operation"
                    ) from exc
            else:
                result = self._run(
                    command,
                    faithful,
                    work_root,
                    cancellation_callback,
                    extra_sensitive_paths=(render_source, partial_output),
                )
                if result.returncode != 0:
                    raise RescueMediaError(
                        "improved viewing command failed: " + result.stderr_summary
                    )
                self._require_nonempty(partial_output, stage="improved viewing output")
                candidate = partial_output
            observed_duration = self._verify_media(
                candidate, faithful, work_root, cancellation_callback
            )
            self._require_duration_match(
                observed_duration,
                expected_duration,
                stage="improved viewing output",
            )
            if _sha256_file(faithful) != faithful_hash:
                raise RescueArtifactError(
                    "faithful candidate changed during improved rendering"
                )
            self._check_cancelled(cancellation_callback)
            _replace_new(candidate, final_output, stage="improved viewing output")
            controls: tuple[SharpenVerificationControlHandle, ...] = ()
            if sharpen is not None:
                output_ranges = _map_exact_qualification_ranges(
                    sharpen.source_ranges,
                    source_mappings
                    or mappings_for_ranges(retained_source_ranges(plan), _FINAL_NAME),
                )
                pts_digest, topology_digest, frame_count = (
                    self._sharpen_control_inspector(
                        sharpen_baseline,
                        sharpen_visibility,
                        final_output,
                        cancellation_callback,
                    )
                )
                recipe = SharpenVerificationControlRecipeV1(
                    plan_digest=plan.plan_digest,
                    action_id=sharpen.id,
                    baseline_sha256=_sha256_file(sharpen_baseline),
                    visibility_control_sha256=_sha256_file(sharpen_visibility),
                    candidate_sha256=_sha256_file(final_output),
                    encode_contract=canonical_video_encode_contract(
                        plan.effective_config
                    ),
                    normalized_pts_digest=pts_digest,
                    stream_topology_digest=topology_digest,
                    source_ranges=sharpen.source_ranges,
                    output_ranges=output_ranges,
                    inventory_frame_count=frame_count,
                )
                if selected_sharpen_qualification is not None and (
                    recipe.baseline_sha256
                    != selected_sharpen_qualification.baseline_sha256
                    or recipe.visibility_control_sha256
                    != selected_sharpen_qualification.visibility_control_sha256
                    or recipe.candidate_sha256
                    != selected_sharpen_qualification.candidate_sha256
                    or recipe.normalized_pts_digest
                    != selected_sharpen_qualification.normalized_pts_digest
                    or recipe.stream_topology_digest
                    != selected_sharpen_qualification.stream_topology_digest
                    or recipe.inventory_frame_count
                    != selected_sharpen_qualification.inventory_frame_count
                ):
                    raise RescueMediaError(
                        "runtime SHARPEN controls differ from selected qualification"
                    )
                controls = (
                    SharpenVerificationControlHandle(
                        baseline_path=sharpen_baseline,
                        visibility_path=sharpen_visibility,
                        recipe=recipe,
                    ),
                )
                retain_sharpen_controls = True
            return RescueImprovedExecutionResult(
                output_path=final_output, verification_controls=controls
            )
        finally:
            _discard(partial_output)
            _discard(stabilized_output)
            _discard(deflickered_output)
            if not retain_sharpen_controls:
                _discard(sharpen_baseline)
                _discard(sharpen_visibility)

    def render_sharpen_qualification_candidate(
        self,
        *,
        plan: RescuePlan,
        faithful_parent: Path,
        output: Path,
        source_ranges: tuple[tuple[float, float], ...],
        parameters: Mapping[str, object],
        mode: Literal["baseline", "visibility", "candidate"],
        source_mappings: tuple[SourceMapping, ...],
        cancellation_callback: Callable[[], bool],
    ) -> None:
        """Render one no-clobber private qualification generation."""
        faithful_parent = Path(faithful_parent)
        output = Path(output)
        if output.exists():
            raise RescueArtifactError("SHARPEN qualification output already exists")
        self._validate_reserved_paths(faithful_parent, output.parent.parent, (output,))
        command = build_sharpen_qualification_command(
            plan,
            faithful_parent,
            output,
            source_ranges=source_ranges,
            parameters=parameters,
            mode=mode,
            source_mappings=source_mappings,
            _allow_unqualified_sharpen_draft=True,
            ffmpeg=self._ffmpeg,
        )
        result = self._run(
            command,
            faithful_parent,
            output.parent.parent,
            cancellation_callback,
            extra_sensitive_paths=(output,),
        )
        if result.returncode != 0:
            raise RescueMediaError(
                "SHARPEN qualification render failed: " + result.stderr_summary
            )
        self._require_nonempty(output, stage="SHARPEN qualification")
        expected_duration, _audio_rate = self._probe_media(
            faithful_parent,
            faithful_parent,
            output.parent.parent,
            cancellation_callback,
        )
        observed_duration = self._verify_media(
            output,
            faithful_parent,
            output.parent.parent,
            cancellation_callback,
        )
        self._require_duration_match(
            observed_duration,
            expected_duration,
            stage="SHARPEN qualification",
        )
        self._check_cancelled(cancellation_callback)

    def _inspect_sharpen_runtime_controls(
        self,
        baseline: Path,
        visibility: Path,
        candidate: Path,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[str, str, int]:
        """Require exact PTS/cardinality/topology across final SHARPEN generations."""
        from videoscope.rescue.verification import (
            _probe_sharpen_video_topology,
            _probe_video_timestamp_inventory,
        )

        paths = (baseline, visibility, candidate)
        inventories = tuple(
            _probe_video_timestamp_inventory(
                path,
                self._ffprobe,
                self._runner,
                self._timeout_seconds,
                cancellation_callback,
            )
            for path in paths
        )
        if any(
            inventory.timestamps != inventories[0].timestamps
            for inventory in inventories[1:]
        ):
            raise RescueMediaError("SHARPEN runtime control PTS inventory differs")
        topology_digests = tuple(
            _probe_sharpen_video_topology(
                path,
                self._ffprobe,
                self._runner,
                self._timeout_seconds,
                cancellation_callback,
            )[1]
            for path in paths
        )
        if len(set(topology_digests)) != 1:
            raise RescueMediaError("SHARPEN runtime control topology differs")
        payload = json.dumps(
            inventories[0].timestamps,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return (
            sha256(payload).hexdigest(),
            topology_digests[0],
            len(inventories[0].timestamps),
        )

    def execute_stabilized(
        self,
        *,
        source: Path,
        output: Path,
        transforms: Sequence[MotionTransform],
        config: StabilizationConfig,
        encode_config: RescueEffectiveConfig | None = None,
        cancellation_callback: Callable[[], bool],
        frame_timestamps: Sequence[float] | None = None,
    ) -> None:
        """Render an accepted stabilization through the shared bounded runner."""
        self._stabilization_renderer(
            source=source,
            output=output,
            transforms=transforms,
            config=config,
            encode_config=encode_config or RescueEffectiveConfig(),
            runner=self._runner,
            cancellation_callback=cancellation_callback,
            ffmpeg=self._ffmpeg,
            timeout_seconds=self._timeout_seconds,
            frame_timestamps=frame_timestamps,
        )

    def execute_deblurred(
        self,
        *,
        source: Path,
        output: Path,
        ranges: Sequence[tuple[float, float]],
        estimate: BlurKernelEstimate,
        config: DeblurConfig,
        encode_config: RescueEffectiveConfig | None = None,
        cancellation_callback: Callable[[], bool],
    ) -> None:
        """Render measured deconvolution through the shared bounded runner."""
        self._deblur_renderer(
            source,
            output,
            ranges,
            estimate,
            config,
            encode_config=encode_config or RescueEffectiveConfig(),
            ffmpeg_path=Path(self._ffmpeg),
            ffprobe_path=Path(self._ffprobe),
            runner=self._runner,
            cancellation_callback=cancellation_callback,
        )

    def execute_tonal_reduced(
        self,
        *,
        source: Path,
        output: Path,
        tones: Sequence[InterferenceTone],
        config: TonalInterferenceConfig,
        cancellation_callback: Callable[[], bool],
    ) -> None:
        """Render measured narrowband reduction through the shared bounded runner."""
        self._tonal_renderer(
            source,
            output,
            tones,
            config,
            ffmpeg_path=Path(self._ffmpeg),
            ffprobe_path=Path(self._ffprobe),
            runner=self._runner,
            cancellation_callback=cancellation_callback,
        )

    def execute_tonal_identity(
        self,
        *,
        source: Path,
        output: Path,
        config: TonalInterferenceConfig,
        cancellation_callback: Callable[[], bool],
    ) -> None:
        """Render a same-parent, same-codec identity sibling for tonal boundaries."""
        self._tonal_identity_renderer(
            source,
            output,
            config,
            ffmpeg_path=Path(self._ffmpeg),
            ffprobe_path=Path(self._ffprobe),
            runner=self._runner,
            cancellation_callback=cancellation_callback,
        )

    def execute_deflickered(
        self,
        *,
        source: Path,
        output: Path,
        correction: FlickerCorrectionPlan,
        encode_config: RescueEffectiveConfig | None = None,
        cancellation_callback: Callable[[], bool],
        frame_timestamps: Sequence[float] | None = None,
    ) -> None:
        """Render an accepted flicker curve through the shared bounded runner."""
        from videoscope.rescue.visual import render_deflickered_video

        render_deflickered_video(
            source=source,
            output=output,
            correction=correction,
            encode_config=encode_config or RescueEffectiveConfig(),
            runner=self._runner,
            cancellation_callback=cancellation_callback,
            ffmpeg=self._ffmpeg,
            timeout_seconds=self._timeout_seconds,
            frame_timestamps=frame_timestamps,
        )

    def _execute_single_output(
        self,
        *,
        plan: RescuePlan,
        source: Path,
        work_root: Path,
        final_output: Path,
        partial_output: Path,
        source_range: tuple[float, float],
        stream_copy: bool,
        cancellation_callback: Callable[[], bool],
    ) -> RescueExecutionResult:
        fixed_offset = next(
            (
                action
                for action in plan.actions
                if action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
            ),
            None,
        )
        audio_filter = (
            audio_filter_fragment_from_actions(
                (fixed_offset.kind,), fixed_offset.parameters
            )
            if fixed_offset is not None
            else None
        )
        if fixed_offset is not None and audio_filter is None:
            raise RescueMediaError("confirmed fixed A/V correction is not executable")
        try:
            result = self._run(
                build_faithful_remux_command(
                    source,
                    partial_output,
                    stream_copy=stream_copy,
                    source_range=source_range,
                    audio_filter=audio_filter,
                    encode_config=plan.effective_config,
                    ffmpeg=self._ffmpeg,
                ),
                source,
                work_root,
                cancellation_callback,
            )
            if result.returncode != 0:
                raise RescueMediaError(
                    "faithful output command failed: " + result.stderr_summary
                )
            self._require_nonempty(partial_output, stage="faithful output")
            measured_duration = self._verify_media(
                partial_output, source, work_root, cancellation_callback
            )
            start, end = source_range
            self._require_duration_match(
                measured_duration,
                end - start,
                stage="faithful output",
            )
            self._require_source_unchanged(plan, source)
            self._check_cancelled(cancellation_callback)
            _replace_new(partial_output, final_output, stage="faithful output")
            relative_path = final_output.relative_to(work_root).as_posix()
            segment = RescuedSegment(
                source_start=start,
                source_end=end,
                output_start=0.0,
                output_end=measured_duration,
                output_relative_path=relative_path,
            )
            return RescueExecutionResult(
                output_path=final_output,
                output_relative_path=relative_path,
                segments=(segment,),
                source_mappings=(segment.source_mapping,),
                render_mode="stream_copy" if stream_copy else "single_reencode",
            )
        finally:
            _discard(partial_output)

    def _validated_keyframe_start(
        self,
        source: Path,
        start_seconds: float,
        end_seconds: float,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> float | None:
        result = self._run(
            build_keyframe_probe_command(
                source,
                start_seconds,
                end_seconds,
                ffprobe=self._ffprobe,
            ),
            source,
            work_root,
            cancellation_callback,
        )
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout_summary)
            frames = payload.get("frames", [])
        except (AttributeError, json.JSONDecodeError, TypeError):
            return None
        candidates: list[float] = []
        if isinstance(frames, list):
            for frame in frames:
                if not isinstance(frame, dict):
                    continue
                raw_timestamp = frame.get("best_effort_timestamp_time")
                if isinstance(raw_timestamp, bool) or not isinstance(
                    raw_timestamp, (int, float, str)
                ):
                    continue
                try:
                    timestamp = float(raw_timestamp)
                except ValueError:
                    continue
                if (
                    math.isfinite(timestamp)
                    and start_seconds <= timestamp < end_seconds
                ):
                    candidates.append(timestamp)
        return min(candidates) if candidates else None

    def _probe_media(
        self,
        candidate: Path,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[float, int | None]:
        for attempt in range(1, _MEDIA_TIMING_PROBE_ATTEMPTS + 1):
            result = self._run(
                build_media_probe_command(candidate, ffprobe=self._ffprobe),
                source,
                work_root,
                cancellation_callback,
                extra_sensitive_paths=(candidate,),
            )
            if result.returncode != 0:
                raise RescueMediaError(result.stderr_summary)
            try:
                payload = json.loads(result.stdout_summary)
            except (json.JSONDecodeError, TypeError):
                payload = None
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("streams"), list)
                or not isinstance(payload.get("format"), dict)
                or "duration" not in payload["format"]
            ):
                if attempt < _MEDIA_TIMING_PROBE_ATTEMPTS:
                    continue
                raise RescueMediaError(
                    "media timing probe returned invalid JSON"
                ) from None
            streams = payload["streams"]
            format_data = payload["format"]
            break
        else:
            raise AssertionError("media timing probe retry loop exhausted unexpectedly")
        try:
            container_duration = float(format_data["duration"])
        except (TypeError, ValueError):
            raise RescueMediaError(
                "media timing probe returned invalid timing"
            ) from None
        videos = (
            tuple(
                stream
                for stream in streams
                if isinstance(stream, dict) and stream.get("codec_type") == "video"
            )
            if isinstance(streams, list)
            else ()
        )
        if (
            not math.isfinite(container_duration)
            or container_duration <= 0
            or len(videos) != 1
        ):
            raise RescueMediaError("media timing probe returned invalid timing")
        video_duration = _validated_video_timeline_duration(videos[0])
        audio_rates = [
            int(stream["sample_rate"])
            for stream in streams
            if isinstance(stream, dict)
            and stream.get("codec_type") == "audio"
            and isinstance(stream.get("sample_rate"), (str, int))
            and str(stream["sample_rate"]).isdigit()
            and 8000 <= int(stream["sample_rate"]) <= 384000
        ]
        return video_duration, audio_rates[0] if audio_rates else None

    def _probe_tonal_audio_topology(
        self,
        candidate: Path,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> TonalAudioTopologyV2:
        result = self._run(
            tonal_audio_topology_probe_arguments(candidate, ffprobe=self._ffprobe),
            source,
            work_root,
            cancellation_callback,
            extra_sensitive_paths=(candidate,),
        )
        if result.returncode != 0:
            raise RescueMediaError("tonal audio topology probe failed")
        try:
            return audio_topology_from_ffprobe_stdout(result.stdout_summary)
        except ValueError as exc:
            raise RescueMediaError("tonal audio topology probe is incomplete") from exc

    def _probe_tonal_audio_timeline(
        self,
        candidate: Path,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> TonalAudioTimelineV1:
        result = self._run(
            tonal_audio_timeline_probe_arguments(candidate, ffprobe=self._ffprobe),
            source,
            work_root,
            cancellation_callback,
            extra_sensitive_paths=(candidate,),
        )
        if result.returncode != 0:
            raise RescueMediaError("tonal audio timeline probe failed")
        try:
            return audio_timeline_from_ffprobe_stdout(result.stdout_summary)
        except ValueError as exc:
            raise RescueMediaError("tonal audio timeline probe is incomplete") from exc

    def _decode_media(
        self,
        candidate: Path,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> None:
        result = self._run(
            build_decode_verification_command(candidate, ffmpeg=self._ffmpeg),
            source,
            work_root,
            cancellation_callback,
            extra_sensitive_paths=(candidate,),
        )
        if result.returncode != 0:
            raise RescueMediaError(result.stderr_summary)

    def _verify_media(
        self,
        candidate: Path,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> float:
        duration, _audio_sample_rate_hz = self._probe_media(
            candidate, source, work_root, cancellation_callback
        )
        self._decode_media(candidate, source, work_root, cancellation_callback)
        return duration

    def _require_duration_match(
        self,
        observed_duration: float,
        expected_duration: float,
        *,
        stage: str,
    ) -> None:
        if abs(observed_duration - expected_duration) > (
            self._duration_tolerance_seconds
        ):
            raise RescueMediaError(
                f"{stage} duration differs from verified source timing"
            )

    def _source_is_mp4_copy_safe(
        self,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> bool:
        result = self._run(
            build_media_probe_command(source, ffprobe=self._ffprobe),
            source,
            work_root,
            cancellation_callback,
        )
        if result.returncode != 0:
            return False
        try:
            payload = json.loads(result.stdout_summary)
            streams = payload.get("streams", [])
        except (AttributeError, json.JSONDecodeError, TypeError):
            return False
        if not isinstance(streams, list):
            return False
        observed: dict[str, list[str]] = {"video": [], "audio": []}
        for stream in streams:
            if not isinstance(stream, dict):
                continue
            stream_type = stream.get("codec_type")
            codec_name = stream.get("codec_name")
            if stream_type in observed and isinstance(codec_name, str):
                observed[stream_type].append(codec_name.lower())
        if not observed["video"]:
            return False
        safe_video = {"h264", "hevc", "mpeg4"}
        safe_audio = {"aac", "mp3", "ac3", "eac3", "alac"}
        return all(codec in safe_video for codec in observed["video"]) and all(
            codec in safe_audio for codec in observed["audio"]
        )

    def _run(
        self,
        arguments: Sequence[str],
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
        *,
        extra_sensitive_paths: tuple[Path, ...] = (),
    ) -> CommandResult:
        self._check_cancelled(cancellation_callback)
        return self._runner(
            tuple(arguments),
            timeout_seconds=self._timeout_seconds,
            sensitive_paths=(source, work_root, *extra_sensitive_paths),
            cancellation_callback=cancellation_callback,
        )

    @staticmethod
    def _validate_source(plan: RescuePlan, source: Path) -> None:
        try:
            if not source.is_file():
                raise RescueInputError("source video was not found")
            observed_hash = _sha256_file(source)
        except RescueInputError:
            raise
        except OSError as exc:
            raise RescueInputError("source video could not be read") from exc
        if observed_hash != plan.input_hash:
            raise RescueInputError("source hash does not match the confirmed plan")

    @staticmethod
    def _prepare_staging_root(work_root: Path) -> Path:
        try:
            if work_root.exists() and not work_root.is_dir():
                raise RescueArtifactError("work root is not a directory")
            work_root.mkdir(parents=True, exist_ok=True)
            resolved_work = work_root.resolve(strict=True)
            staging = work_root / _STAGING_RELATIVE_PATH
            if staging.is_symlink():
                raise RescueArtifactError("staging path cannot be a symlink")
            if staging.exists() and not staging.is_dir():
                raise RescueArtifactError("staging path is not a directory")
            staging.mkdir(exist_ok=True)
            if staging.resolve(strict=True).parent != resolved_work:
                raise RescueArtifactError("staging path escapes the work root")
            return staging
        except RescueArtifactError:
            raise
        except OSError as exc:
            raise RescueArtifactError(
                "validated staging root could not be created"
            ) from exc

    @staticmethod
    def _validate_reserved_paths(
        source: Path,
        work_root: Path,
        outputs: tuple[Path, ...],
    ) -> None:
        try:
            resolved_work = work_root.resolve(strict=True)
            resolved_source = os.path.normcase(str(source.resolve(strict=True)))
            source_identity = _file_identity(source)
            for output in outputs:
                resolved_output_path = output.resolve(strict=False)
                if resolved_work not in resolved_output_path.parents:
                    raise RescueArtifactError("staging output escapes the work root")
                if os.path.normcase(str(resolved_output_path)) == resolved_source:
                    raise RescueArtifactError("staging output collides with source")
                output_identity = _existing_file_identity(output)
                if output_identity is not None and output_identity == source_identity:
                    raise RescueArtifactError("staging output aliases source")
                if output.exists() or output.is_symlink():
                    raise RescueArtifactError("staging output already exists")
        except RescueArtifactError:
            raise
        except OSError as exc:
            raise RescueArtifactError("staging paths could not be validated") from exc

    @staticmethod
    def _check_cancelled(cancellation_callback: Callable[[], bool]) -> None:
        if cancellation_callback():
            raise RescueCancelledError("faithful Rescue execution was cancelled")

    @staticmethod
    def _require_nonempty(path: Path, *, stage: str) -> None:
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                raise RescueMediaError(f"{stage} was not a non-empty file")
        except RescueMediaError:
            raise
        except OSError as exc:
            raise RescueArtifactError(f"{stage} could not be inspected") from exc

    @staticmethod
    def _require_source_unchanged(plan: RescuePlan, source: Path) -> None:
        try:
            unchanged = _sha256_file(source) == plan.input_hash
        except OSError as exc:
            raise RescueArtifactError(
                "source immutability could not be checked"
            ) from exc
        if not unchanged:
            raise RescueArtifactError("source changed during faithful execution")

    @staticmethod
    def _write_concat_manifest(manifest: Path, segments: tuple[Path, ...]) -> None:
        lines = ["ffconcat version 1.0"]
        for segment in segments:
            escaped = segment.resolve(strict=True).as_posix().replace("'", "'\\''")
            lines.append(f"file '{escaped}'")
        try:
            manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise RescueArtifactError("segment manifest could not be written") from exc


def _require_plan_video_encode_contracts(
    plan: RescuePlan,
    *,
    allow_unqualified_sharpen_draft: bool = False,
    allow_unqualified_tonal_draft: bool = False,
) -> None:
    try:
        validate_plan_video_encode_contracts(
            plan,
            allow_unqualified_sharpen_draft=allow_unqualified_sharpen_draft,
            allow_unqualified_tonal_draft=allow_unqualified_tonal_draft,
        )
    except ValueError as exc:
        raise RescueMediaError(
            "confirmed action video encode contract is invalid"
        ) from exc


def _require_plan_identity_contract(plan: RescuePlan) -> None:
    try:
        validate_rescue_plan_identity_contract(plan)
    except ValueError as exc:
        raise RescueMediaError("confirmed plan digest is invalid") from exc


def _source_duration(plan: RescuePlan) -> float:
    candidates = [
        float(end)
        for action in plan.actions
        if action.kind is RescueActionKind.REMUX
        for _start, end in action.source_ranges
    ]
    if not candidates or not math.isfinite(max(candidates)) or max(candidates) <= 0:
        raise RescueInputError("confirmed plan has no positive source duration")
    return max(candidates)


def _has_segment_salvage(plan: RescuePlan) -> bool:
    return any(
        action.kind is RescueActionKind.SALVAGE_SEGMENTS for action in plan.actions
    )


def _validated_video_timeline_duration(stream: Mapping[str, object]) -> float:
    """Return normalized CFR video duration, independent of audio/container tails."""
    try:
        start = _finite_media_float(stream["start_time"])
        duration = _finite_media_float(stream["duration"])
        average_rate = _media_fraction(stream["avg_frame_rate"])
        nominal_rate = _media_fraction(stream["r_frame_rate"])
        frame_count = _positive_media_int(stream["nb_frames"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RescueMediaError("media timing probe returned invalid timing") from exc
    if (
        start < 0
        or duration <= 0
        or not math.isclose(
            average_rate,
            nominal_rate,
            rel_tol=1e-6,
            abs_tol=1e-9,
        )
        or not math.isclose(
            frame_count / average_rate,
            duration,
            rel_tol=1e-6,
            abs_tol=_MEDIA_TIMING_TOLERANCE_SECONDS,
        )
    ):
        raise RescueMediaError("media timing probe returned invalid timing")
    return duration


def _finite_media_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError("media timing field is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("media timing field is not finite")
    return result


def _positive_media_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("media frame count is invalid")
    result = int(value)
    if result <= 0:
        raise ValueError("media frame count is invalid")
    return result


def _media_fraction(value: object) -> float:
    if not isinstance(value, str) or value.count("/") != 1:
        raise TypeError("media frame rate is invalid")
    numerator, denominator = value.split("/", 1)
    try:
        result = float(numerator) / float(denominator)
    except ZeroDivisionError as exc:
        raise ValueError("media frame rate is invalid") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("media frame rate is invalid")
    return result


def _is_safe_remux_only(plan: RescuePlan) -> bool:
    stream_copy_kinds = {
        RescueActionKind.REMUX,
        RescueActionKind.SELECT_TRACKS,
        RescueActionKind.VERIFY,
    }
    return all(action.kind in stream_copy_kinds for action in plan.actions)


def _mapped_segments(
    verified: list[_VerifiedSegment],
    work_root: Path,
    final_output_relative_path: str,
    final_duration: float,
) -> tuple[tuple[RescuedSegment, ...], tuple[SourceMapping, ...]]:
    measured_total = sum(item.measured_duration for item in verified)
    scale = final_duration / measured_total
    output_cursor = 0.0
    segments: list[RescuedSegment] = []
    mappings: list[SourceMapping] = []
    for position, item in enumerate(verified):
        output_end = (
            final_duration
            if position == len(verified) - 1
            else output_cursor + item.measured_duration * scale
        )
        segments.append(
            RescuedSegment(
                source_start=item.source_start,
                source_end=item.source_end,
                output_start=0.0,
                output_end=item.measured_duration,
                output_relative_path=item.path.relative_to(work_root).as_posix(),
            )
        )
        mappings.append(
            SourceMapping(
                source_start=item.source_start,
                source_end=item.source_end,
                output_start=output_cursor,
                output_end=output_end,
                output_relative_path=final_output_relative_path,
            )
        )
        output_cursor = output_end
    return tuple(segments), tuple(mappings)


def _normalized_failed_ranges(
    ranges: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    normalized: list[tuple[float, float]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if normalized and start <= normalized[-1][1]:
            normalized[-1] = (normalized[-1][0], max(normalized[-1][1], end))
        else:
            normalized.append((start, end))
    return tuple(normalized)


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _measure_audio_noise_windows(
    path: Path, config: AudioDenoiseConfig
) -> tuple[AudioNoiseInterval, ...]:
    """Derive relative sustained tonal-noise intervals from mono PCM windows."""
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RescueMediaError("audio noise analysis PCM format was unexpected")
        sample_rate = handle.getframerate()
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    if sample_rate <= 0 or samples.size == 0:
        raise RescueMediaError("audio noise analysis contained no samples")
    normalized = samples.astype(np.float64) / 32768.0
    window_size = max(1, round(sample_rate * config.analysis_window_seconds))
    rows: list[tuple[float, float, float, float, tuple[float, ...]]] = []
    taper = np.hanning(window_size)
    frequencies = np.fft.rfftfreq(window_size, d=1.0 / sample_rate)
    for index, window_start in enumerate(range(0, normalized.size, window_size)):
        window = normalized[window_start : window_start + window_size]
        if window.size < window_size:
            window = np.pad(window, (0, window_size - window.size))
        rms = float(np.sqrt(np.mean(window * window)))
        rms_dbfs = 20.0 * math.log10(max(rms, 1e-12))
        spectrum = np.abs(np.fft.rfft(window * taper))
        energy = float(np.sum(spectrum))
        centroid = float(np.sum(frequencies * spectrum) / energy) if energy > 0 else 0.0
        peak_ratio = float(np.max(spectrum) / energy) if energy > 0 else 0.0
        spectral_copy = spectrum.copy()
        spectral_copy[frequencies < 20.0] = 0.0
        dominant: list[float] = []
        for frequency_index in np.argsort(spectral_copy)[::-1]:
            frequency = float(frequencies[int(frequency_index)])
            if spectral_copy[int(frequency_index)] <= 0:
                break
            if all(abs(frequency - existing) >= 10.0 for existing in dominant):
                dominant.append(frequency)
            if len(dominant) == 2:
                break
        rows.append(
            (
                index * config.analysis_window_seconds,
                rms_dbfs,
                centroid,
                peak_ratio,
                tuple(dominant),
            )
        )
    if len(rows) < 3:
        return ()
    baseline = float(np.median([row[1] for row in rows]))
    # A sustained relative level jump with a compact spectrum is an observable,
    # inexpensive proxy for hum/tonal interference.  It is deliberately not a
    # claim about semantic relevance or a speaker's identity.
    selected = [
        row
        for row in rows
        if row[1] >= baseline + config.relative_level_increase_db
        and row[3] >= 0.18
        and row[2] <= config.maximum_stationary_centroid_hz
    ]
    if not selected:
        return ()
    grouped: list[list[tuple[float, float, float, float, tuple[float, ...]]]] = []
    for row in selected:
        if (
            grouped
            and row[0] - grouped[-1][-1][0]
            <= config.analysis_window_seconds + config.merge_gap_seconds + 1e-9
        ):
            grouped[-1].append(row)
        else:
            grouped.append([row])
    intervals: list[AudioNoiseInterval] = []
    for group in grouped:
        interval_start = group[0][0]
        end = min(
            normalized.size / sample_rate,
            group[-1][0] + config.analysis_window_seconds,
        )
        interval_start = max(0.0, interval_start - config.boundary_guard_seconds)
        end = min(normalized.size / sample_rate, end + config.boundary_guard_seconds)
        if end - interval_start < config.minimum_interval_seconds:
            continue
        level_delta = float(np.median([row[1] for row in group])) - baseline
        confidence = min(
            1.0,
            config.minimum_confidence
            + max(0.0, level_delta - config.relative_level_increase_db) / 20.0,
        )
        intervals.append(
            AudioNoiseInterval(
                start_seconds=interval_start,
                end_seconds=end,
                rms_dbfs=float(np.median([row[1] for row in group])),
                spectral_centroid_hz=float(np.median([row[2] for row in group])),
                tone_frequencies_hz=group[len(group) // 2][4],
                relative_level_delta_db=level_delta,
                confidence=confidence,
            )
        )
    return tuple(intervals)


def _replace_new(source: Path, destination: Path, *, stage: str) -> None:
    try:
        if destination.exists() or destination.is_symlink():
            raise RescueArtifactError(f"{stage} destination already exists")
        source.replace(destination)
    except RescueArtifactError:
        raise
    except OSError as exc:
        raise RescueArtifactError(f"{stage} could not be staged atomically") from exc


def _copy_new(source: Path, destination: Path, *, stage: str) -> None:
    """Copy a verified candidate without re-encoding or following a destination link."""
    try:
        if destination.exists() or destination.is_symlink():
            raise RescueArtifactError(f"{stage} destination already exists")
        with source.open("rb") as source_file, destination.open("xb") as output_file:
            shutil.copyfileobj(source_file, output_file, length=1024 * 1024)
    except RescueArtifactError:
        raise
    except OSError as exc:
        _discard(destination)
        raise RescueArtifactError(f"{stage} could not be copied safely") from exc


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _deblur_operations(
    parameters: Mapping[str, JsonValue],
    source_ranges: Sequence[tuple[float, float]],
    mappings: Sequence[SourceMapping],
    *,
    expected_version: str,
) -> tuple[
    tuple[tuple[tuple[float, float], ...], BlurKernelEstimate, DeblurConfig], ...
]:
    try:
        if parameters["algorithm_version"] != expected_version:
            raise ValueError("deblur algorithm version does not match the plan")
        raw_operations = parameters.get("operations")
        if raw_operations is None:
            operation_values: tuple[
                tuple[
                    tuple[tuple[float, float], ...], BlurKernelEstimate, DeblurConfig
                ],
                ...,
            ] = (
                (
                    tuple(source_ranges),
                    BlurKernelEstimate.model_validate_json(
                        json.dumps(parameters["estimate"], ensure_ascii=False)
                    ),
                    DeblurConfig.model_validate_json(
                        json.dumps(parameters["config"], ensure_ascii=False)
                    ),
                ),
            )
        else:
            if not isinstance(raw_operations, (list, tuple)) or not raw_operations:
                raise ValueError("deblur operations are invalid")
            parsed_operations: list[
                tuple[tuple[tuple[float, float], ...], BlurKernelEstimate, DeblurConfig]
            ] = []
            for raw_operation in raw_operations:
                if not isinstance(raw_operation, Mapping) or set(raw_operation) != {
                    "source_ranges",
                    "estimate",
                    "config",
                }:
                    raise ValueError("deblur operation is invalid")
                parsed_operations.append(
                    (
                        _strict_serialized_ranges(raw_operation["source_ranges"]),
                        BlurKernelEstimate.model_validate_json(
                            json.dumps(raw_operation["estimate"], ensure_ascii=False)
                        ),
                        DeblurConfig.model_validate_json(
                            json.dumps(raw_operation["config"], ensure_ascii=False)
                        ),
                    )
                )
            operation_values = tuple(parsed_operations)
            operation_ranges = tuple(
                item
                for ranges, _estimate, _config in operation_values
                for item in ranges
            )
            if operation_ranges != tuple(source_ranges):
                raise ValueError("deblur operation ranges do not match the action")
    except (KeyError, TypeError, ValueError) as exc:
        raise RescueMediaError("confirmed deblur parameters are invalid") from exc
    mapped_operations = tuple(
        (mapped_ranges, estimate, config)
        for ranges, estimate, config in operation_values
        if (mapped_ranges := _map_source_ranges_to_output(ranges, mappings))
    )
    if not mapped_operations:
        raise RescueMediaError("confirmed deblur has no retained interval")
    return mapped_operations


def _tonal_operation(
    parameters: Mapping[str, JsonValue],
    source_ranges: Sequence[tuple[float, float]],
    mappings: Sequence[SourceMapping],
    *,
    expected_version: str,
) -> tuple[tuple[InterferenceTone, ...], TonalInterferenceConfig]:
    try:
        if parameters["algorithm_version"] != expected_version:
            raise ValueError("tonal algorithm version does not match the plan")
        config = TonalInterferenceConfig.model_validate_json(
            json.dumps(parameters["config"], ensure_ascii=False)
        )
        raw = parameters["interference_profiles"]
        if not isinstance(raw, (list, tuple)):
            raise ValueError("tonal profiles are invalid")
        source_tones = tuple(
            InterferenceTone.model_validate_json(json.dumps(value, ensure_ascii=False))
            for value in raw
        )
        profile_ranges = validate_tonal_profile_contracts(source_tones, config)
        if profile_ranges != tuple(source_ranges):
            raise ValueError("tonal profile ranges do not match the action")
        if any(
            not any(
                start <= tone.start_seconds and tone.end_seconds <= end
                for start, end in source_ranges
            )
            for tone in source_tones
        ):
            raise ValueError("tonal profile exceeds the confirmed action ranges")
    except (KeyError, TypeError, ValueError) as exc:
        raise RescueMediaError("confirmed tonal parameters are invalid") from exc
    tones: list[InterferenceTone] = []
    for tone in source_tones:
        for start, end in _map_source_ranges_to_output(
            ((tone.start_seconds, tone.end_seconds),), mappings
        ):
            tones.append(
                tone.model_copy(update={"start_seconds": start, "end_seconds": end})
            )
    if not tones:
        raise RescueMediaError("confirmed tonal reduction has no retained interval")
    return tuple(tones), config


def _map_source_ranges_to_output(
    ranges: Sequence[tuple[float, float]],
    mappings: Sequence[SourceMapping],
) -> tuple[tuple[float, float], ...]:
    mapped: list[tuple[float, float]] = []
    for start, end in ranges:
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("action ranges are invalid")
        for mapping in mappings:
            overlap_start = max(start, mapping.source_start)
            overlap_end = min(end, mapping.source_end)
            if overlap_end <= overlap_start:
                continue
            mapped_start = _map_source_timestamp(overlap_start, mappings)
            mapped_end = (
                mapping.output_end
                if overlap_end == mapping.source_end
                else _map_source_timestamp(overlap_end, mappings)
            )
            mapped.append((mapped_start, mapped_end))
    return tuple(mapped)


def _strict_serialized_ranges(
    value: JsonValue,
) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("serialized ranges are invalid")
    ranges: list[tuple[float, float]] = []
    previous_end = -1.0
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("serialized ranges are invalid")
        start, end = item
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
        ):
            raise ValueError("serialized ranges are invalid")
        start_value = float(start)
        end_value = float(end)
        if (
            not math.isfinite(start_value)
            or not math.isfinite(end_value)
            or start_value < 0
            or end_value <= start_value
            or start_value < previous_end
        ):
            raise ValueError("serialized ranges are invalid")
        ranges.append((start_value, end_value))
        previous_end = end_value
    return tuple(ranges)


def _stabilization_operation(
    parameters: Mapping[str, JsonValue],
    source_ranges: Sequence[tuple[float, float]],
    mappings: Sequence[SourceMapping],
    *,
    expected_version: str,
) -> tuple[tuple[MotionTransform, ...], StabilizationConfig]:
    """Validate and map one confirmed source-timeline stabilization operation."""
    from videoscope.rescue.stabilization import MotionTransform, StabilizationConfig

    try:
        method = parameters.get("method")
        direct_method = method in {"anchor_v1", "transition_anchor_v1"}
        if method is not None and not direct_method:
            raise ValueError("stabilization algorithm version does not match the plan")
        if direct_method and (parameters.get("algorithm_version") != expected_version):
            raise ValueError("stabilization algorithm version does not match the plan")
        raw_transforms = parameters["motion_transforms"]
        if not isinstance(raw_transforms, (list, tuple)):
            raise ValueError("motion transform list is invalid")
        source_transforms = tuple(
            MotionTransform.model_validate(value) for value in raw_transforms
        )
        raw_config = parameters.get("config")
        if raw_config is not None:
            config = StabilizationConfig.model_validate_json(
                json.dumps(raw_config, ensure_ascii=False)
            )
            if direct_method and config.accepted_ranges != tuple(source_ranges):
                raise ValueError(
                    "stabilization accepted ranges do not match the action"
                )
        elif direct_method:
            raise ValueError("anchor stabilization config is missing")
        else:
            config = StabilizationConfig(
                frame_width=_strict_int_parameter(parameters, "frame_width"),
                frame_height=_strict_int_parameter(parameters, "frame_height"),
                maximum_timeline_gap_seconds=_strict_float_parameter(
                    parameters, "maximum_timeline_gap_seconds"
                ),
                smoothing_window_samples=_strict_int_parameter(
                    parameters, "smoothing_window_samples"
                ),
                max_crop_ratio=_optional_float_parameter(
                    parameters, "max_crop_ratio", 0.12
                ),
                minimum_motion_amplitude_pixels=_optional_float_parameter(
                    parameters, "minimum_motion_amplitude_pixels", 1.0
                ),
            )
        if direct_method and any(
            not any(
                timestamp_in_half_open_range(
                    transform.timestamp_seconds,
                    start,
                    end,
                )
                for start, end in source_ranges
            )
            for transform in source_transforms
        ):
            raise ValueError("stabilization correction exceeds the action ranges")
        mapped = tuple(
            transform.model_copy(
                update={
                    "timestamp_seconds": _map_source_timestamp(
                        transform.timestamp_seconds,
                        mappings,
                    )
                }
            )
            for transform in source_transforms
            if _timestamp_in_mappings(
                transform.timestamp_seconds,
                mappings,
            )
        )
        if raw_config is not None:
            mapped_ranges = _map_source_ranges_to_output(source_ranges, mappings)
            config = config.model_copy(update={"accepted_ranges": mapped_ranges})
    except (KeyError, TypeError, ValueError) as exc:
        raise RescueMediaError(
            "confirmed stabilization parameters are invalid"
        ) from exc
    if not mapped:
        raise RescueMediaError("confirmed stabilization has no reviewed transforms")
    return mapped, config


def _strict_int_parameter(parameters: Mapping[str, JsonValue], key: str) -> int:
    value = parameters[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("stabilization integer parameter is invalid")
    return value


def _strict_float_parameter(parameters: Mapping[str, JsonValue], key: str) -> float:
    value = parameters[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("stabilization numeric parameter is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("stabilization numeric parameter is invalid")
    return result


def _optional_float_parameter(
    parameters: Mapping[str, JsonValue], key: str, default: float
) -> float:
    return _strict_float_parameter(parameters, key) if key in parameters else default


def _timestamp_in_mappings(
    timestamp: float,
    mappings: Sequence[SourceMapping],
) -> bool:
    return any(
        timestamp_in_half_open_range(
            timestamp,
            mapping.source_start,
            mapping.source_end,
        )
        for mapping in mappings
    )


def _map_source_timestamp(
    timestamp: float,
    mappings: Sequence[SourceMapping],
) -> float:
    for mapping in mappings:
        if timestamp_in_half_open_range(
            timestamp,
            mapping.source_start,
            mapping.source_end,
        ):
            source_duration = mapping.source_end - mapping.source_start
            output_duration = mapping.output_end - mapping.output_start
            if source_duration <= 0 or output_duration <= 0:
                break
            return float(
                mapping.output_start
                + ((timestamp - mapping.source_start) / source_duration)
                * output_duration
            )
    raise RescueMediaError("stabilization timestamp is outside faithful mappings")


def _file_identity(path: Path) -> tuple[int, int]:
    stat_result = path.stat()
    return (stat_result.st_dev, stat_result.st_ino)


def _existing_file_identity(path: Path) -> tuple[int, int] | None:
    try:
        return _file_identity(path)
    except FileNotFoundError:
        return None


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _read_bounded_text(
    handle: _BinaryTemporaryFile,
    limit: int,
    *,
    tail: bool,
) -> str:
    handle.flush()
    size = handle.tell()
    handle.seek(max(0, size - limit) if tail else 0)
    data = handle.read(limit)
    return data.decode("utf-8", errors="replace")


__all__ = [
    "CommandResult",
    "DEFAULT_DURATION_TOLERANCE_SECONDS",
    "DEFAULT_RESCUE_TIMEOUT_SECONDS",
    "ExternalCommandRunner",
    "NativeRescueExecutor",
    "RescueExecutionResult",
    "RescuedSegment",
    "SourceMapping",
    "run_external_command",
]
