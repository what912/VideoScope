"""Deterministic, bounded frame sampling through local FFmpeg."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Full, Queue
from time import monotonic
from typing import BinaryIO, Final, Literal, cast

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from videoscope.processes import pinned_subprocess_options
from videoscope.video.errors import (
    ExternalToolNotFoundError,
    FrameSamplingError,
    VideoNotFoundError,
    sanitize_diagnostic,
)

DEFAULT_SAMPLE_RATE = 2.0
DEFAULT_MAX_EDGE = 640
DEFAULT_SAMPLING_TIMEOUT_SECONDS = 300.0
MAX_FRAME_INDEX_SELECTIONS: Final = 1000
_TIMELINE_LINE_QUEUE_SIZE: Final = 16
_TIMELINE_FRAME_QUEUE_SIZE: Final = 2
_TIMELINE_DIAGNOSTIC_BYTES: Final = 2048
_PROCESS_STOP_GRACE_SECONDS: Final = 2.0
_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_SHOWINFO_FRAME = re.compile(
    r"\bn:\s*(?P<index>\d+).*?\bpts_time:(?P<timestamp>[^\s]+)"
    r".*?\bduration_time:(?P<duration>[^\s]+)"
)
ImageFormat = Literal["jpeg", "png"]


class FrameSample(BaseModel):
    """One extracted frame and its deterministic sample time."""

    model_config = ConfigDict(extra="forbid")

    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    sample_index: int = Field(ge=0)
    relative_path: str = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class FrameSamplingResult(BaseModel):
    """Extracted frames plus the caller-owned temporary work directory."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    work_directory: Path
    samples: tuple[FrameSample, ...]
    timeline_duration_seconds: float | None = Field(
        default=None, gt=0, allow_inf_nan=False
    )
    decode_passes: int = Field(default=1, ge=1, le=1)
    truncated: bool = False
    motion_samples: tuple[FrameSample, ...] = ()
    motion_truncated: bool = False


@dataclass(frozen=True)
class _TimelineProbe:
    duration_seconds: float
    raw_duration_seconds: float
    deferred_duration_error: str | None = None


@dataclass(frozen=True)
class _FrameTiming:
    frame_index: int
    timestamp_seconds: float
    duration_seconds: float


@dataclass(frozen=True)
class _PngFrame:
    timing: _FrameTiming
    payload: bytes


@dataclass(frozen=True)
class _StoredPngFrame:
    timing: _FrameTiming
    path: Path


@dataclass(frozen=True)
class _TimelineStreamResult:
    selected: tuple[_StoredPngFrame, ...]
    motion_selected: tuple[_StoredPngFrame, ...]
    actual_end_seconds: float
    truncated: bool
    motion_truncated: bool
    decoded_frames: int
    retained_payload_high_water: int
    target_advances: int
    motion_target_advances: int
    distance_comparisons: int
    motion_distance_comparisons: int
    finalization_visits: int


def build_sampling_filter(*, sample_rate: float, max_edge: int) -> str:
    """Build a deterministic constant-rate and bounded-size filter graph."""
    rate_text = format(sample_rate, ".15g")
    scale = (
        f"scale=w='if(gte(iw,ih),min(iw,{max_edge}),-2)':"
        f"h='if(gte(iw,ih),-2,min(ih,{max_edge}))'"
    )
    return f"setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0:round=near,{scale}"


def build_index_sampling_filter(
    *, frame_indices: tuple[int, ...], max_edge: int
) -> str:
    """Build a bounded single-pass filter for selected source-frame indices."""
    scale = (
        f"scale=w='if(gte(iw,ih),min(iw,{max_edge}),-2)':"
        f"h='if(gte(iw,ih),-2,min(ih,{max_edge}))'"
    )
    selectors = "+".join(f"eq(n\\,{index})" for index in frame_indices)
    return f"select='{selectors}',showinfo,{scale}"


def _output_suffix(image_format: ImageFormat) -> str:
    return "jpg" if image_format == "jpeg" else "png"


def _append_bounded_tail(target: bytearray, chunk: bytes, limit: int) -> None:
    if len(chunk) >= limit:
        target[:] = chunk[-limit:]
        return
    overflow = len(target) + len(chunk) - limit
    if overflow > 0:
        del target[:overflow]
    target.extend(chunk)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=_PROCESS_STOP_GRACE_SECONDS)


def _positive_finite(value: object) -> float | None:
    try:
        converted = float(str(value))
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) and converted > 0 else None


def _finite(value: object) -> float | None:
    try:
        converted = float(str(value))
    except (TypeError, ValueError):
        return None
    return converted if math.isfinite(converted) else None


def _timeline_probe(
    input_path: Path,
    *,
    requested_duration_seconds: float,
    ffprobe: str,
    timeout_seconds: float,
    work_directory: Path,
) -> _TimelineProbe:
    """Read bounded stream/container timing metadata without decoding frames."""
    arguments = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=start_time,duration:format=start_time,duration",
        "-of",
        "json",
        str(input_path),
    ]
    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
            **pinned_subprocess_options(arguments),
        )
    except FileNotFoundError as exc:
        raise ExternalToolNotFoundError(
            f"Required executable not found: {Path(arguments[0]).name}",
            work_directory=work_directory,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FrameSamplingError(
            f"FFprobe timed out while reading stream timing: {input_path.name}",
            work_directory=work_directory,
        ) from exc
    except OSError as exc:
        raise FrameSamplingError(
            f"Could not start FFprobe timing probe for: {input_path.name}",
            work_directory=work_directory,
        ) from exc
    if completed.returncode != 0:
        raise FrameSamplingError(
            f"FFprobe could not read stream timing from: {input_path.name}",
            work_directory=work_directory,
            stderr_summary=sanitize_diagnostic(
                completed.stderr or completed.stdout,
                sensitive_paths=(input_path, work_directory),
            ),
        )
    try:
        payload = json.loads(completed.stdout)
        streams = payload.get("streams")
        stream = streams[0] if isinstance(streams, list) and streams else {}
        media_format = payload.get("format")
        if not isinstance(stream, dict) or not isinstance(media_format, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError, AttributeError) as exc:
        raise FrameSamplingError(
            f"FFprobe returned invalid stream timing for: {input_path.name}",
            work_directory=work_directory,
        ) from exc

    stream_duration = _positive_finite(stream.get("duration"))
    format_duration = _positive_finite(media_format.get("duration"))
    format_start = _finite(media_format.get("start_time")) or 0.0
    if stream_duration is not None:
        duration = stream_duration
    elif format_duration is not None:
        duration = format_duration - max(0.0, format_start)
    else:
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        return _TimelineProbe(
            duration_seconds=requested_duration_seconds,
            raw_duration_seconds=requested_duration_seconds,
            deferred_duration_error="unavailable",
        )
    raw_duration = format_duration or duration
    duration_matches = math.isclose(
        requested_duration_seconds, duration, rel_tol=1e-6, abs_tol=1e-3
    ) or math.isclose(
        requested_duration_seconds, raw_duration, rel_tol=1e-6, abs_tol=1e-3
    )
    caller_is_stale_low = requested_duration_seconds < min(duration, raw_duration)
    if not duration_matches and not caller_is_stale_low:
        return _TimelineProbe(
            duration_seconds=duration,
            raw_duration_seconds=raw_duration,
            deferred_duration_error="stale",
        )
    return _TimelineProbe(
        duration_seconds=duration,
        raw_duration_seconds=raw_duration,
    )


def _read_exact(stream: BinaryIO, count: int, *, allow_eof: bool = False) -> bytes:
    result = bytearray()
    while len(result) < count:
        chunk = stream.read(count - len(result))
        if not chunk:
            if allow_eof and not result:
                return b""
            raise ValueError("truncated PNG frame stream")
        result.extend(chunk)
    return bytes(result)


def _iter_png_payloads(stream: BinaryIO) -> Iterator[bytes]:
    """Yield self-delimiting PNG frames while retaining at most one in memory."""
    while True:
        signature = _read_exact(stream, len(_PNG_SIGNATURE), allow_eof=True)
        if not signature:
            return
        if signature != _PNG_SIGNATURE:
            raise ValueError("invalid PNG frame signature")
        frame = bytearray(signature)
        while True:
            length_bytes = _read_exact(stream, 4)
            chunk_length = int.from_bytes(length_bytes, "big")
            chunk_type = _read_exact(stream, 4)
            chunk_payload_and_crc = _read_exact(stream, chunk_length + 4)
            frame.extend(length_bytes)
            frame.extend(chunk_type)
            frame.extend(chunk_payload_and_crc)
            if chunk_type == b"IEND":
                yield bytes(frame)
                break


def _put_bounded(
    queue: Queue[bytes | _FrameTiming | None],
    item: bytes | _FrameTiming | None,
    stop_reader: threading.Event,
) -> None:
    while not stop_reader.is_set():
        try:
            queue.put(item, timeout=0.05)
            return
        except Full:
            continue


def _timeline_stream_filter(*, max_edge: int) -> str:
    scale = (
        f"scale=w='if(gte(iw,ih),min(iw,{max_edge}),-2)':"
        f"h='if(gte(iw,ih),-2,min(ih,{max_edge}))'"
    )
    return f"setpts=PTS-STARTPTS,{scale},showinfo"


def _stream_timeline_candidates_unchecked(
    input_path: Path,
    *,
    duration_seconds: float,
    sample_rate: float,
    maximum_count: int,
    motion_sample_rate: float | None,
    maximum_motion_count: int | None,
    max_edge: int,
    ffmpeg: str,
    timeout_seconds: float,
    work_directory: Path,
    candidate_directory: Path,
    enforce_duration_match: bool,
    cancellation_check: Callable[[], None] | None,
) -> _TimelineStreamResult:
    """Decode once and persist target candidates without retaining their payloads."""

    def bounded_targets(rate: float, maximum: int) -> tuple[tuple[float, ...], bool]:
        requested_count = max(1, math.ceil(duration_seconds * rate - 1e-9))
        truncated_result = requested_count > maximum
        target_count = min(maximum, requested_count)
        if truncated_result:
            targets_result = (
                (0.0,)
                if target_count == 1
                else tuple(
                    position * duration_seconds / (target_count - 1)
                    for position in range(target_count)
                )
            )
        else:
            targets_result = tuple(position / rate for position in range(target_count))
        return targets_result, truncated_result

    targets, truncated = bounded_targets(sample_rate, maximum_count)
    if motion_sample_rate is None or maximum_motion_count is None:
        motion_targets: tuple[float, ...] = ()
        motion_truncated = False
    else:
        motion_targets, motion_truncated = bounded_targets(
            motion_sample_rate, maximum_motion_count
        )
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-vf",
        _timeline_stream_filter(max_edge=max_edge),
        "-an",
        "-threads",
        "1",
        "-fps_mode",
        "passthrough",
        "-compression_level",
        "1",
        "-c:v",
        "png",
        "-f",
        "image2pipe",
        "pipe:1",
    ]
    try:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            **pinned_subprocess_options(arguments),
        )
    except FileNotFoundError as exc:
        raise ExternalToolNotFoundError(
            f"Required executable not found: {Path(ffmpeg).name}",
            work_directory=work_directory,
        ) from exc
    except OSError as exc:
        raise FrameSamplingError(
            f"Could not start FFmpeg for: {input_path.name}",
            work_directory=work_directory,
        ) from exc

    stdout = process.stdout
    stderr = process.stderr
    assert stdout is not None
    assert stderr is not None
    frame_queue: Queue[bytes | _FrameTiming | None] = Queue(
        maxsize=_TIMELINE_FRAME_QUEUE_SIZE
    )
    timing_queue: Queue[bytes | _FrameTiming | None] = Queue(
        maxsize=_TIMELINE_LINE_QUEUE_SIZE
    )
    stop_reader = threading.Event()
    diagnostic = bytearray()
    reader_errors: list[Exception] = []
    emitted_frames = [0]
    emitted_timings = [0]

    def read_frames() -> None:
        try:
            for payload in _iter_png_payloads(cast(BinaryIO, stdout)):
                emitted_frames[0] += 1
                _put_bounded(frame_queue, payload, stop_reader)
        except Exception as exc:
            reader_errors.append(exc)
        finally:
            _put_bounded(frame_queue, None, stop_reader)

    def read_timings() -> None:
        try:
            for raw_line in stderr:
                _append_bounded_tail(diagnostic, raw_line, _TIMELINE_DIAGNOSTIC_BYTES)
                match = _SHOWINFO_FRAME.search(raw_line.decode("utf-8", "replace"))
                if match is None:
                    continue
                try:
                    timing = _FrameTiming(
                        frame_index=int(match.group("index")),
                        timestamp_seconds=float(match.group("timestamp")),
                        duration_seconds=float(match.group("duration")),
                    )
                except ValueError as exc:
                    reader_errors.append(exc)
                    return
                emitted_timings[0] += 1
                _put_bounded(timing_queue, timing, stop_reader)
        finally:
            _put_bounded(timing_queue, None, stop_reader)

    workers = (
        threading.Thread(target=read_frames, daemon=True),
        threading.Thread(target=read_timings, daemon=True),
    )
    for worker in workers:
        worker.start()

    selected_by_index: dict[int, _StoredPngFrame] = {}
    target_frame_indices: list[int] = []
    motion_target_frame_indices: list[int] = []
    previous_frame: _PngFrame | None = None
    target_index = 0
    target_advances = 0
    motion_target_index = 0
    motion_target_advances = 0
    distance_comparisons = 0
    motion_distance_comparisons = 0
    retained_payload_high_water = 0
    previous_timestamp: float | None = None
    previous_to_last_timestamp: float | None = None
    paired_count = 0

    def persist(frame: _PngFrame) -> None:
        if frame.timing.frame_index in selected_by_index:
            return
        candidate_path = (
            candidate_directory / f"frame_{frame.timing.frame_index:012d}.png"
        )
        candidate_path.write_bytes(frame.payload)
        selected_by_index[frame.timing.frame_index] = _StoredPngFrame(
            timing=frame.timing,
            path=candidate_path,
        )

    def select_nearest(
        target: float,
        previous: _PngFrame | None,
        current: _PngFrame,
        *,
        motion: bool = False,
    ) -> _PngFrame:
        nonlocal distance_comparisons, motion_distance_comparisons
        if motion:
            motion_distance_comparisons += 1
        else:
            distance_comparisons += 1
        current_key = (
            abs(current.timing.timestamp_seconds - target),
            current.timing.frame_index,
        )
        if previous is None:
            return current
        if motion:
            motion_distance_comparisons += 1
        else:
            distance_comparisons += 1
        previous_key = (
            abs(previous.timing.timestamp_seconds - target),
            previous.timing.frame_index,
        )
        return previous if previous_key <= current_key else current

    deadline = monotonic() + timeout_seconds
    try:
        while True:
            if cancellation_check is not None:
                cancellation_check()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout_seconds)
            try:
                payload = frame_queue.get(timeout=min(0.05, remaining))
            except Empty:
                continue
            if payload is None:
                break
            if not isinstance(payload, bytes):
                raise ValueError("invalid PNG frame queue item")
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(arguments, timeout_seconds)
                try:
                    timing = timing_queue.get(timeout=min(0.05, remaining))
                except Empty:
                    continue
                break
            if timing is None or not isinstance(timing, _FrameTiming):
                raise ValueError("frame timing ended before PNG output")
            if (
                timing.frame_index != paired_count
                or not math.isfinite(timing.timestamp_seconds)
                or timing.timestamp_seconds < 0
                or not math.isfinite(timing.duration_seconds)
                or timing.duration_seconds < 0
                or (
                    previous_timestamp is not None
                    and timing.timestamp_seconds < previous_timestamp
                )
            ):
                raise ValueError("frame timestamps were invalid or out of order")
            previous_to_last_timestamp = previous_timestamp
            previous_timestamp = timing.timestamp_seconds
            frame = _PngFrame(timing=timing, payload=payload)
            retained_payload_high_water = max(
                retained_payload_high_water,
                1 if previous_frame is None else 2,
            )
            while (
                target_index < len(targets)
                and targets[target_index] <= timing.timestamp_seconds
            ):
                selected_frame = select_nearest(
                    targets[target_index],
                    previous_frame,
                    frame,
                )
                persist(selected_frame)
                target_frame_indices.append(selected_frame.timing.frame_index)
                target_index += 1
                target_advances += 1
            while (
                motion_target_index < len(motion_targets)
                and motion_targets[motion_target_index] <= timing.timestamp_seconds
            ):
                selected_frame = select_nearest(
                    motion_targets[motion_target_index],
                    previous_frame,
                    frame,
                    motion=True,
                )
                persist(selected_frame)
                motion_target_frame_indices.append(selected_frame.timing.frame_index)
                motion_target_index += 1
                motion_target_advances += 1
            previous_frame = frame
            paired_count += 1
        process.wait(timeout=max(0.0, deadline - monotonic()))
    except BaseException as exc:
        _stop_process(process)
        if not isinstance(exc, (ValueError, OSError, subprocess.TimeoutExpired)):
            raise
        message = (
            "FFmpeg timed out while sampling"
            if isinstance(exc, subprocess.TimeoutExpired)
            else "FFmpeg returned an invalid sampled-frame stream"
        )
        raise FrameSamplingError(
            f"{message}: {input_path.name}",
            work_directory=work_directory,
            stderr_summary="sample frame/timestamp streams were invalid",
        ) from exc
    finally:
        stop_reader.set()
        _stop_process(process)
        stdout.close()
        stderr.close()
        for worker in workers:
            worker.join(timeout=_PROCESS_STOP_GRACE_SECONDS)

    if process.returncode != 0 or reader_errors:
        raise FrameSamplingError(
            f"FFmpeg could not sample frames from: {input_path.name}",
            work_directory=work_directory,
            stderr_summary=sanitize_diagnostic(
                bytes(diagnostic).decode("utf-8", "replace"),
                sensitive_paths=(input_path, work_directory),
            ),
        )
    if (
        previous_frame is None
        or emitted_frames[0] != paired_count
        or emitted_timings[0] != paired_count
    ):
        raise FrameSamplingError(
            f"FFmpeg did not provide aligned sampled frames: {input_path.name}",
            work_directory=work_directory,
            stderr_summary="sample frame/timestamp cardinality did not match",
        )

    while target_index < len(targets):
        persist(select_nearest(targets[target_index], None, previous_frame))
        target_frame_indices.append(previous_frame.timing.frame_index)
        target_index += 1
        target_advances += 1

    while motion_target_index < len(motion_targets):
        persist(
            select_nearest(
                motion_targets[motion_target_index],
                None,
                previous_frame,
                motion=True,
            )
        )
        motion_target_frame_indices.append(previous_frame.timing.frame_index)
        motion_target_index += 1
        motion_target_advances += 1

    if truncated and len(targets) > 1:
        persist(previous_frame)
        target_frame_indices[-1] = previous_frame.timing.frame_index
    if motion_truncated and len(motion_targets) > 1:
        persist(previous_frame)
        motion_target_frame_indices[-1] = previous_frame.timing.frame_index

    tail_duration = previous_frame.timing.duration_seconds
    if tail_duration <= 0 and previous_to_last_timestamp is not None:
        tail_duration = (
            previous_frame.timing.timestamp_seconds - previous_to_last_timestamp
        )
    if not math.isfinite(tail_duration) or tail_duration <= 0:
        raise FrameSamplingError(
            f"FFmpeg could not audit the video timeline end: {input_path.name}",
            work_directory=work_directory,
            stderr_summary="last frame duration was unavailable",
        )
    actual_end = previous_frame.timing.timestamp_seconds + tail_duration
    if enforce_duration_match and not math.isclose(
        actual_end,
        duration_seconds,
        rel_tol=1e-6,
        abs_tol=max(1e-3, tail_duration),
    ):
        raise FrameSamplingError(
            f"Video timeline duration was stale for: {input_path.name}",
            work_directory=work_directory,
            stderr_summary="decoded timeline end did not match stream/container timing",
        )

    def finalize_selection(
        frame_indices: list[int],
    ) -> tuple[tuple[_StoredPngFrame, ...], int]:
        selected_frames: list[_StoredPngFrame] = []
        last_selected_index: int | None = None
        visits = 0
        for selected_index in frame_indices:
            visits += 1
            if last_selected_index is not None and selected_index < last_selected_index:
                raise FrameSamplingError(
                    f"FFmpeg produced invalid target mappings for: {input_path.name}",
                    work_directory=work_directory,
                    stderr_summary="sample target mappings were out of order",
                )
            if selected_index != last_selected_index:
                selected_frames.append(selected_by_index[selected_index])
            last_selected_index = selected_index
        return tuple(selected_frames), visits

    selected_frames, finalization_visits = finalize_selection(target_frame_indices)
    motion_selected_frames, _motion_finalization_visits = finalize_selection(
        motion_target_frame_indices
    )
    return _TimelineStreamResult(
        selected=selected_frames,
        motion_selected=motion_selected_frames,
        actual_end_seconds=actual_end,
        truncated=truncated,
        motion_truncated=motion_truncated,
        decoded_frames=paired_count,
        retained_payload_high_water=retained_payload_high_water,
        target_advances=target_advances,
        motion_target_advances=motion_target_advances,
        distance_comparisons=distance_comparisons,
        motion_distance_comparisons=motion_distance_comparisons,
        finalization_visits=finalization_visits,
    )


def _stream_timeline_candidates(
    input_path: Path,
    *,
    duration_seconds: float,
    sample_rate: float,
    maximum_count: int,
    motion_sample_rate: float | None,
    maximum_motion_count: int | None,
    max_edge: int,
    ffmpeg: str,
    timeout_seconds: float,
    work_directory: Path,
    enforce_duration_match: bool,
    cancellation_check: Callable[[], None] | None,
) -> _TimelineStreamResult:
    """Stage capped samples and remove every staged payload on audit failure."""
    candidate_directory = work_directory / ".timeline-candidates"
    candidate_directory.mkdir()
    try:
        return _stream_timeline_candidates_unchecked(
            input_path,
            duration_seconds=duration_seconds,
            sample_rate=sample_rate,
            maximum_count=maximum_count,
            motion_sample_rate=motion_sample_rate,
            maximum_motion_count=maximum_motion_count,
            max_edge=max_edge,
            ffmpeg=ffmpeg,
            timeout_seconds=timeout_seconds,
            work_directory=work_directory,
            candidate_directory=candidate_directory,
            enforce_duration_match=enforce_duration_match,
            cancellation_check=cancellation_check,
        )
    except Exception:
        shutil.rmtree(candidate_directory, ignore_errors=True)
        raise


def sample_frames(
    path: Path,
    *,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
    max_edge: int = DEFAULT_MAX_EDGE,
    image_format: ImageFormat = "jpeg",
    workspace_parent: Path | None = None,
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    timeout_seconds: float = DEFAULT_SAMPLING_TIMEOUT_SECONDS,
    max_samples: int | None = None,
    frame_indices: tuple[int, ...] | None = None,
    timeline_duration_seconds: float | None = None,
    motion_sample_rate: float | None = None,
    maximum_motion_samples: int | None = None,
    cancellation_check: Callable[[], None] | None = None,
) -> FrameSamplingResult:
    """Extract fixed-rate frames into a caller-owned temporary directory."""
    input_path = Path(path)
    if not input_path.is_file():
        raise VideoNotFoundError(f"Input file not found: {input_path.name}")
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("sample_rate must be a finite value greater than zero")
    if max_edge <= 0:
        raise ValueError("max_edge must be greater than zero")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if max_samples is not None and (
        isinstance(max_samples, bool)
        or not isinstance(max_samples, int)
        or max_samples <= 0
    ):
        raise ValueError("max_samples must be a positive integer when provided")
    if (motion_sample_rate is None) != (maximum_motion_samples is None):
        raise ValueError(
            "motion_sample_rate and maximum_motion_samples must be provided together"
        )
    if motion_sample_rate is not None and (
        not math.isfinite(motion_sample_rate) or motion_sample_rate <= 0
    ):
        raise ValueError("motion_sample_rate must be finite and greater than zero")
    if maximum_motion_samples is not None and (
        isinstance(maximum_motion_samples, bool)
        or not isinstance(maximum_motion_samples, int)
        or maximum_motion_samples <= 0
    ):
        raise ValueError("maximum_motion_samples must be a positive integer")
    if frame_indices is not None:
        if not frame_indices:
            raise ValueError("frame_indices must not be empty when provided")
        if len(frame_indices) > MAX_FRAME_INDEX_SELECTIONS:
            raise ValueError(
                "frame_indices must not exceed the hard selection limit "
                f"of {MAX_FRAME_INDEX_SELECTIONS}"
            )
        if any(
            isinstance(index, bool) or not isinstance(index, int) or index < 0
            for index in frame_indices
        ):
            raise ValueError("frame_indices must contain non-negative integers")
        if tuple(sorted(set(frame_indices))) != frame_indices:
            raise ValueError("frame_indices must be sorted and unique")
        if max_samples is not None and len(frame_indices) > max_samples:
            raise ValueError("frame_indices must not exceed max_samples")
    if timeline_duration_seconds is not None:
        if frame_indices is not None:
            raise ValueError(
                "frame_indices and timeline_duration_seconds are mutually exclusive"
            )
        if max_samples is None:
            raise ValueError("timeline sampling requires max_samples")
    elif motion_sample_rate is not None:
        raise ValueError("motion sampling requires timeline_duration_seconds")
    if image_format not in ("jpeg", "png"):
        raise ValueError("image_format must be 'jpeg' or 'png'")

    parent = Path(workspace_parent) if workspace_parent is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    work_directory = Path(tempfile.mkdtemp(prefix="videoscope-frames-", dir=parent))
    frames_directory = work_directory / "frames"
    frames_directory.mkdir()
    probed_timeline_duration: float | None = None
    if timeline_duration_seconds is not None:
        assert max_samples is not None
        if (
            not math.isfinite(timeline_duration_seconds)
            or timeline_duration_seconds <= 0
        ):
            raise FrameSamplingError(
                f"Video timeline duration was unavailable for: {input_path.name}",
                work_directory=work_directory,
                stderr_summary="requested duration was missing or zero",
            )
        probe = _timeline_probe(
            input_path,
            requested_duration_seconds=timeline_duration_seconds,
            ffprobe=ffprobe,
            timeout_seconds=timeout_seconds,
            work_directory=work_directory,
        )
        probed_timeline_duration = probe.duration_seconds
        capped_count = min(max_samples, MAX_FRAME_INDEX_SELECTIONS)
        stream_result = _stream_timeline_candidates(
            input_path,
            duration_seconds=probe.duration_seconds,
            sample_rate=sample_rate,
            maximum_count=capped_count,
            motion_sample_rate=motion_sample_rate,
            maximum_motion_count=maximum_motion_samples,
            max_edge=max_edge,
            ffmpeg=ffmpeg,
            timeout_seconds=timeout_seconds,
            work_directory=work_directory,
            enforce_duration_match=probe.deferred_duration_error is None,
            cancellation_check=cancellation_check,
        )
        if probe.deferred_duration_error is not None:
            shutil.rmtree(work_directory / ".timeline-candidates", ignore_errors=True)
            raise FrameSamplingError(
                f"Video timeline duration was {probe.deferred_duration_error} for: "
                f"{input_path.name}",
                work_directory=work_directory,
                stderr_summary=(
                    "stream/container duration was missing or zero"
                    if probe.deferred_duration_error == "unavailable"
                    else "requested duration did not match stream/container timing"
                ),
            )
        suffix = _output_suffix(image_format)
        timeline_samples: list[FrameSample] = []
        motion_samples: list[FrameSample] = []
        try:
            for sample_index, frame in enumerate(stream_result.selected):
                frame_path = frames_directory / f"frame_{sample_index:06d}.{suffix}"
                with Image.open(frame.path) as image:
                    width, height = image.size
                    if image_format == "jpeg":
                        image.convert("RGB").save(frame_path, quality=95)
                if image_format == "png":
                    shutil.copyfile(frame.path, frame_path)
                timeline_samples.append(
                    FrameSample(
                        timestamp_seconds=frame.timing.timestamp_seconds,
                        sample_index=sample_index,
                        relative_path=frame_path.relative_to(work_directory).as_posix(),
                        width=width,
                        height=height,
                    )
                )
            motion_directory = work_directory / "motion_frames"
            if stream_result.motion_selected:
                motion_directory.mkdir()
            for sample_index, frame in enumerate(stream_result.motion_selected):
                frame_path = motion_directory / f"frame_{sample_index:06d}.{suffix}"
                with Image.open(frame.path) as image:
                    width, height = image.size
                    if image_format == "jpeg":
                        image.convert("RGB").save(frame_path, quality=95)
                    else:
                        image.save(frame_path, format="PNG")
                motion_samples.append(
                    FrameSample(
                        timestamp_seconds=frame.timing.timestamp_seconds,
                        sample_index=sample_index,
                        relative_path=frame_path.relative_to(work_directory).as_posix(),
                        width=width,
                        height=height,
                    )
                )
        except (OSError, UnidentifiedImageError) as exc:
            shutil.rmtree(frames_directory, ignore_errors=True)
            frames_directory.mkdir()
            raise FrameSamplingError(
                f"An extracted frame was unreadable for: {input_path.name}",
                work_directory=work_directory,
            ) from exc
        finally:
            shutil.rmtree(work_directory / ".timeline-candidates", ignore_errors=True)
        if (
            len(timeline_samples) != len(stream_result.selected)
            or len(timeline_samples) > max_samples
            or len(motion_samples) != len(stream_result.motion_selected)
            or (
                maximum_motion_samples is not None
                and len(motion_samples) > maximum_motion_samples
            )
        ):
            shutil.rmtree(frames_directory, ignore_errors=True)
            frames_directory.mkdir()
            raise FrameSamplingError(
                f"FFmpeg did not provide aligned sampled frames: {input_path.name}",
                work_directory=work_directory,
                stderr_summary="sample output cardinality did not match",
            )
        return FrameSamplingResult(
            work_directory=work_directory,
            samples=tuple(timeline_samples),
            timeline_duration_seconds=stream_result.actual_end_seconds,
            decode_passes=1,
            truncated=stream_result.truncated,
            motion_samples=tuple(motion_samples),
            motion_truncated=stream_result.motion_truncated,
        )
    suffix = _output_suffix(image_format)
    output_pattern = frames_directory / f"frame_%06d.{suffix}"
    filter_graph = (
        build_index_sampling_filter(
            frame_indices=frame_indices,
            max_edge=max_edge,
        )
        if frame_indices is not None
        else build_sampling_filter(sample_rate=sample_rate, max_edge=max_edge)
    )
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info" if frame_indices is not None else "error",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-vf",
        filter_graph,
        "-an",
        "-threads",
        "1",
        "-start_number",
        "0",
    ]
    if frame_indices is not None:
        arguments.extend(["-vsync", "0"])
    if image_format == "jpeg":
        arguments.extend(["-q:v", "2"])
    else:
        arguments.extend(["-compression_level", "6"])
    if max_samples is not None:
        arguments.extend(["-frames:v", str(max_samples)])
    arguments.append(str(output_pattern))

    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
            **pinned_subprocess_options(arguments),
        )
    except FileNotFoundError as exc:
        raise ExternalToolNotFoundError(
            f"Required executable not found: {Path(ffmpeg).name}",
            work_directory=work_directory,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FrameSamplingError(
            f"FFmpeg timed out while sampling: {input_path.name}",
            work_directory=work_directory,
        ) from exc
    except OSError as exc:
        raise FrameSamplingError(
            f"Could not start FFmpeg for: {input_path.name}",
            work_directory=work_directory,
        ) from exc

    if completed.returncode != 0:
        diagnostic = sanitize_diagnostic(
            completed.stderr or completed.stdout,
            sensitive_paths=(input_path, work_directory),
        )
        raise FrameSamplingError(
            f"FFmpeg could not sample frames from: {input_path.name}",
            work_directory=work_directory,
            stderr_summary=diagnostic,
        )

    sample_timestamps: tuple[float, ...] | None = None
    if frame_indices is not None:
        sample_timestamps = tuple(
            float(value)
            for value in re.findall(r"\bpts_time:([^\s]+)", completed.stderr or "")
        )
    samples: list[FrameSample] = []
    try:
        for sample_index, frame_path in enumerate(
            sorted(frames_directory.glob(f"frame_*.{suffix}"))
        ):
            with Image.open(frame_path) as image:
                width, height = image.size
            timestamp_seconds = (
                sample_timestamps[sample_index]
                if sample_timestamps is not None
                and sample_index < len(sample_timestamps)
                else sample_index / sample_rate
            )
            samples.append(
                FrameSample(
                    timestamp_seconds=timestamp_seconds,
                    sample_index=sample_index,
                    relative_path=frame_path.relative_to(work_directory).as_posix(),
                    width=width,
                    height=height,
                )
            )
    except (OSError, UnidentifiedImageError) as exc:
        raise FrameSamplingError(
            f"An extracted frame was unreadable for: {input_path.name}",
            work_directory=work_directory,
        ) from exc

    if sample_timestamps is not None and (
        frame_indices is None
        or len(frame_indices) != len(samples)
        or len(sample_timestamps) != len(samples)
        or any(not math.isfinite(value) or value < 0 for value in sample_timestamps)
        or any(
            later < earlier
            for earlier, later in zip(sample_timestamps, sample_timestamps[1:])
        )
    ):
        raise FrameSamplingError(
            f"FFmpeg did not provide timestamps for sampled frames: {input_path.name}",
            work_directory=work_directory,
            stderr_summary="sample timestamp count did not match output frames",
        )

    if not samples:
        raise FrameSamplingError(
            f"FFmpeg produced no frames for: {input_path.name}",
            work_directory=work_directory,
            stderr_summary="no output frames",
        )
    return FrameSamplingResult(
        work_directory=work_directory,
        samples=tuple(samples),
        timeline_duration_seconds=probed_timeline_duration,
    )
