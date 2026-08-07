"""Staged, shell-free faithful Video Rescue execution."""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from typing import TYPE_CHECKING, Final, Literal, Protocol

from videoscope.processes import pinned_subprocess_options
from videoscope.rescue.audio import (
    LoudnessConfig,
    LoudnessMeasurement,
    audio_filter_fragment_from_actions,
    parse_loudnorm_measurement,
)
from videoscope.rescue.capabilities import require_executable_action_scopes
from videoscope.rescue.commands import (
    build_audio_improvement_command,
    build_decode_verification_command,
    build_faithful_concat_command,
    build_faithful_remux_command,
    build_faithful_segment_command,
    build_improved_viewing_command,
    build_keyframe_probe_command,
    build_loudnorm_measurement_command,
    build_media_probe_command,
)
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueInputError,
    RescueMediaError,
)
from videoscope.rescue.models import RescueActionKind, RescuePlan
from videoscope.rescue.timeline import (
    SourceMapping,
    mappings_for_ranges,
    retained_source_ranges,
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

    @property
    def is_partial(self) -> bool:
        return bool(self.failed_source_ranges)


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

    def execute_faithful(
        self,
        plan: RescuePlan,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> RescueExecutionResult:
        """Execute only structural faithful actions into ``staging/``."""
        source = Path(source)
        work_root = Path(work_root)
        self._validate_source(plan, source)
        source_ranges = retained_source_ranges(plan)
        require_executable_action_scopes(
            plan, mappings_for_ranges(source_ranges, _FINAL_NAME)
        )
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

    def execute_improved(
        self,
        plan: RescuePlan,
        faithful: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
        source_mappings: tuple[SourceMapping, ...] | None = None,
    ) -> Path:
        """Render the exact bounded filters recorded in a confirmed plan."""
        faithful = Path(faithful)
        work_root = Path(work_root)
        if not faithful.is_file():
            raise RescueInputError("verified faithful candidate was not found")
        require_executable_action_scopes(plan, source_mappings)
        faithful_hash = _sha256_file(faithful)
        staging = self._prepare_staging_root(work_root)
        final_output = staging / _IMPROVED_NAME
        partial_output = staging / _IMPROVED_PARTIAL_NAME
        stabilized_output = staging / "improved-stabilized.partial.mp4"
        deflickered_output = staging / "improved-deflickered.partial.mp4"
        self._validate_reserved_paths(
            faithful,
            work_root,
            (final_output, partial_output, stabilized_output, deflickered_output),
        )
        self._check_cancelled(cancellation_callback)
        try:
            expected_duration = self._probe_media(
                faithful, faithful, work_root, cancellation_callback
            )
            render_source = faithful
            stabilization = next(
                (
                    action
                    for action in plan.actions
                    if action.kind is RescueActionKind.STABILIZE
                ),
                None,
            )
            if stabilization is not None:
                try:
                    from videoscope.rescue.stabilization import (
                        MotionTransform,
                        StabilizationConfig,
                    )

                    raw_transforms = stabilization.parameters["motion_transforms"]
                    if not isinstance(raw_transforms, (list, tuple)):
                        raise ValueError("motion transform list is invalid")
                    transforms = tuple(
                        MotionTransform.model_validate(value)
                        for value in raw_transforms
                    )
                    frame_width = stabilization.parameters["frame_width"]
                    frame_height = stabilization.parameters["frame_height"]
                    maximum_gap = stabilization.parameters[
                        "maximum_timeline_gap_seconds"
                    ]
                    smoothing_window = stabilization.parameters[
                        "smoothing_window_samples"
                    ]
                    if isinstance(frame_width, bool) or not isinstance(
                        frame_width, (int, float)
                    ):
                        raise ValueError("stabilization numeric parameter is invalid")
                    if isinstance(frame_height, bool) or not isinstance(
                        frame_height, (int, float)
                    ):
                        raise ValueError("stabilization numeric parameter is invalid")
                    if isinstance(maximum_gap, bool) or not isinstance(
                        maximum_gap, (int, float)
                    ):
                        raise ValueError("stabilization numeric parameter is invalid")
                    if isinstance(smoothing_window, bool) or not isinstance(
                        smoothing_window, (int, float)
                    ):
                        raise ValueError("stabilization numeric parameter is invalid")
                    config = StabilizationConfig(
                        frame_width=int(frame_width),
                        frame_height=int(frame_height),
                        maximum_timeline_gap_seconds=float(maximum_gap),
                        smoothing_window_samples=int(smoothing_window),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise RescueMediaError(
                        "confirmed stabilization parameters are invalid"
                    ) from exc
                if not transforms:
                    raise RescueMediaError(
                        "confirmed stabilization has no reviewed transforms"
                    )
                self.execute_stabilized(
                    source=faithful,
                    output=stabilized_output,
                    transforms=transforms,
                    config=config,
                    cancellation_callback=cancellation_callback,
                )
                self._require_nonempty(
                    stabilized_output, stage="stabilized improved candidate"
                )
                render_source = stabilized_output
                self._check_cancelled(cancellation_callback)

            excluded_filter_action_ids: frozenset[str] = frozenset()
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
                    cancellation_callback=cancellation_callback,
                )
                self._require_nonempty(
                    deflickered_output, stage="deflickered improved candidate"
                )
                render_source = deflickered_output
                excluded_filter_action_ids = frozenset((deflicker.id,))
                self._check_cancelled(cancellation_callback)

            try:
                command = build_improved_viewing_command(
                    plan,
                    render_source,
                    partial_output,
                    source_mappings=source_mappings,
                    excluded_action_ids=excluded_filter_action_ids,
                    ffmpeg=self._ffmpeg,
                )
            except ValueError as exc:
                generic_improvements = any(
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
                if stabilization is None and deflicker is None or generic_improvements:
                    raise RescueMediaError(
                        "confirmed improvement has no executable bound operation"
                    ) from exc
                candidate = render_source
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
            return final_output
        finally:
            _discard(partial_output)
            _discard(stabilized_output)
            _discard(deflickered_output)

    def execute_stabilized(
        self,
        *,
        source: Path,
        output: Path,
        transforms: Sequence[MotionTransform],
        config: StabilizationConfig,
        cancellation_callback: Callable[[], bool],
        frame_timestamps: Sequence[float] | None = None,
    ) -> None:
        """Render an accepted stabilization through the shared bounded runner."""
        from videoscope.rescue.stabilization import render_stabilized_video

        render_stabilized_video(
            source=source,
            output=output,
            transforms=transforms,
            config=config,
            runner=self._runner,
            cancellation_callback=cancellation_callback,
            ffmpeg=self._ffmpeg,
            timeout_seconds=self._timeout_seconds,
            frame_timestamps=frame_timestamps,
        )

    def execute_deflickered(
        self,
        *,
        source: Path,
        output: Path,
        correction: FlickerCorrectionPlan,
        cancellation_callback: Callable[[], bool],
        frame_timestamps: Sequence[float] | None = None,
    ) -> None:
        """Render an accepted flicker curve through the shared bounded runner."""
        from videoscope.rescue.visual import render_deflickered_video

        render_deflickered_video(
            source=source,
            output=output,
            correction=correction,
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
    ) -> float:
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
            streams = payload.get("streams", [])
            format_data = payload.get("format", {})
            duration = float(format_data.get("duration"))
        except (AttributeError, json.JSONDecodeError, TypeError, ValueError):
            raise RescueMediaError("media timing probe returned invalid JSON") from None
        has_video = isinstance(streams, list) and any(
            isinstance(stream, dict) and stream.get("codec_type") == "video"
            for stream in streams
        )
        if not math.isfinite(duration) or duration <= 0 or not has_video:
            raise RescueMediaError("media timing probe returned invalid timing")
        return duration

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
        duration = self._probe_media(
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


def _replace_new(source: Path, destination: Path, *, stage: str) -> None:
    try:
        if destination.exists() or destination.is_symlink():
            raise RescueArtifactError(f"{stage} destination already exists")
        source.replace(destination)
    except RescueArtifactError:
        raise
    except OSError as exc:
        raise RescueArtifactError(f"{stage} could not be staged atomically") from exc


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


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
