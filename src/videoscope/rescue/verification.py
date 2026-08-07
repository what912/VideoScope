"""Deterministic, independent verification gates for Rescue outputs."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol

import cv2
import numpy as np
from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.processes import PinnedDescriptorError, pinned_subprocess_options
from videoscope.rescue.audio import LoudnessConfig, parse_loudnorm_measurement
from videoscope.rescue.commands import (
    build_faithful_concat_command,
    build_faithful_remux_command,
    build_faithful_segment_command,
    build_ffprobe_version_command,
    build_loudnorm_measurement_command,
    build_packet_timestamp_probe_command,
)
from videoscope.rescue.errors import RescueCancelledError
from videoscope.rescue.executor import (
    ExternalCommandRunner,
    SourceMapping,
    run_external_command,
)
from videoscope.rescue.models import (
    RescueActionKind,
    RescueArtifact,
    RescueOutcome,
    RescuePlan,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
)
from videoscope.rescue.timeline import (
    DEFAULT_MAPPING_DURATION_TOLERANCE_SECONDS,
    mappings_match_retained_ranges,
    retained_source_ranges,
)
from videoscope.video.errors import sanitize_diagnostic
from videoscope.video.probe import probe_video

_SUPPLEMENTARY_IDS = (
    "artifact_integrity",
    "audio_loudness",
    "audio_peak",
    "black_regression",
    "fixed_av_offset",
    "flicker_regression",
    "freeze_regression",
    "luma_clipping",
    "noise_side_effects",
    "sharpness_side_effects",
    "source_mapping",
    "stabilization_crop",
)
_DEFAULT_MAX_CLIP_INCREASE = 0.0
_DEFAULT_MAX_NOISE_INCREASE = 0.0
_DEFAULT_MAX_SHARPNESS_LOSS_RATIO = 0.1
_DEFAULT_MAX_CROP_RATIO = 0.12
_DEFAULT_LOUDNESS_TOLERANCE_LU = 1.0
_DEFAULT_TRUE_PEAK_LIMIT_DBTP = -1.5
_DEFAULT_AV_OFFSET_TOLERANCE_SECONDS = 0.04
_DEFAULT_MEDIA_TIMEOUT_SECONDS = 120.0
_PACKET_TIMESTAMP_METHOD = "first_usable_packet_timestamp"
_FAITHFUL_PARAMETER_ACTIONS = (
    RescueActionKind.REMUX,
    RescueActionKind.REBUILD_TIMESTAMPS,
    RescueActionKind.SELECT_TRACKS,
    RescueActionKind.NORMALIZE_ROTATION,
    RescueActionKind.SALVAGE_SEGMENTS,
    RescueActionKind.TRIM_DAMAGED_EDGES,
    RescueActionKind.CORRECT_FIXED_AV_OFFSET,
)
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

    def _stream_inventory(
        self, path: Path, cancellation_callback: Callable[[], bool]
    ) -> tuple[int, int, float | None, str | None, str | None]:
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
        if (
            not video_indexes
            or not audio_indexes
            or video_indexes.intersection(audio_indexes)
        ):
            return len(video), len(audio), None, None, None
        video_start = _first_packet_timestamp(packets, video_indexes, {})
        audio_start = _first_packet_timestamp(
            packets, audio_indexes, audio_sample_rates
        )
        if video_start is None or audio_start is None:
            return len(video), len(audio), None, None, None
        tool_version = self._get_ffprobe_version(cancellation_callback)
        return (
            len(video),
            len(audio),
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
    ) -> RescueVerificationReport:
        """Verify faithful and improved artifacts independently."""
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
                    visual_reference,
                    visual_reference_name,
                    visual_reference_reason,
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
        fixed_offset_parameters = _action_parameters(
            plan, (RescueActionKind.CORRECT_FIXED_AV_OFFSET,)
        )
        parameters = (
            faithful_parameters if artifact == "faithful" else improvement_parameters
        )
        visual_comparison_applicable = visual_reference is not None
        comparison = visual_reference or source
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
                _loudness_ok(candidate, improvement_parameters),
                "Measured integrated loudness is within the confirmed bound.",
                _loudness_values(candidate, improvement_parameters),
            ),
            self._optional(
                "audio_peak",
                artifact,
                _peak_ok(candidate, improvement_parameters),
                "Measured true peak is within the confirmed bound.",
                _peak_values(candidate, improvement_parameters),
            ),
            self._optional(
                "black_regression",
                artifact,
                visual_comparison_applicable
                and candidate.black_events <= comparison.black_events,
                "No additional black-event observations were measured.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_events": comparison.black_events,
                        "output_events": candidate.black_events,
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
                and candidate.flicker_events <= comparison.flicker_events,
                "No additional flicker-event observations were measured.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_events": comparison.flicker_events,
                        "output_events": candidate.flicker_events,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
            ),
            self._optional(
                "freeze_regression",
                artifact,
                visual_comparison_applicable
                and candidate.freeze_events <= comparison.freeze_events,
                "No additional freeze-event observations were measured.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_events": comparison.freeze_events,
                        "output_events": candidate.freeze_events,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
            ),
            self._optional(
                "luma_clipping",
                artifact,
                visual_comparison_applicable
                and candidate.clipping_ratio - comparison.clipping_ratio
                <= _parameter(
                    parameters, "maximum_clip_increase", _DEFAULT_MAX_CLIP_INCREASE
                ),
                "No excessive luma clipping was introduced.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_ratio": comparison.clipping_ratio,
                        "output_ratio": candidate.clipping_ratio,
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
                <= _parameter(
                    parameters, "maximum_residual_increase", _DEFAULT_MAX_NOISE_INCREASE
                ),
                "Denoising did not introduce an excessive residual regression.",
                _visual_comparison_values(
                    visual_comparison_applicable,
                    {
                        "source_residual": comparison.noise_residual,
                        "output_residual": candidate.noise_residual,
                    },
                    reference=visual_reference_name,
                    reason=visual_reference_reason,
                ),
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


__all__ = [
    "MediaMeasurementProvider",
    "MediaVerificationSnapshot",
    "NativeMediaMeasurementProvider",
    "ReferenceRenderOptions",
    "RescueVerifier",
]
