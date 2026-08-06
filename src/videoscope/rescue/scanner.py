"""Bounded, streaming observations for the opt-in Video Rescue workflow."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from queue import Empty, Full, Queue
from time import monotonic
from typing import Any, Final, Protocol

from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.processes import pinned_subprocess_options
from videoscope.rescue.errors import RescueScanError
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    make_damage_id,
)
from videoscope.video.errors import REDACTED_PATH

DEFAULT_CHUNK_SECONDS: Final = 1.0
DEFAULT_MERGE_TOLERANCE_SECONDS: Final = 0.05
DEFAULT_COMMAND_TIMEOUT_SECONDS: Final = 30.0
MAX_STDERR_BYTES: Final = 2048
STDERR_CAPTURE_BYTES: Final = MAX_STDERR_BYTES * 4
PROGRESS_CAPTURE_BYTES: Final = 4096
PROCESS_STOP_GRACE_SECONDS: Final = 0.5


@dataclass(frozen=True, slots=True)
class RescueScanConfig:
    """Explicit, bounded controls for packet and decode observations."""

    chunk_seconds: float = DEFAULT_CHUNK_SECONDS
    merge_tolerance_seconds: float = DEFAULT_MERGE_TOLERANCE_SECONDS
    timestamp_regression_tolerance_seconds: float = 0.0
    command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT_SECONDS
    ffprobe_executable: str = "ffprobe"
    ffmpeg_executable: str = "ffmpeg"

    def __post_init__(self) -> None:
        if self.chunk_seconds <= 0:
            raise ValueError("chunk_seconds must be greater than zero")
        if self.merge_tolerance_seconds < 0:
            raise ValueError("merge_tolerance_seconds must not be negative")
        if self.timestamp_regression_tolerance_seconds < 0:
            raise ValueError(
                "timestamp_regression_tolerance_seconds must not be negative"
            )
        if self.command_timeout_seconds <= 0:
            raise ValueError("command_timeout_seconds must be greater than zero")
        if not self.ffprobe_executable or not self.ffmpeg_executable:
            raise ValueError("FFmpeg executable names must not be empty")


@dataclass(frozen=True, slots=True)
class PacketObservation:
    """One lightweight video packet timestamp observation."""

    stream_id: str
    pts_seconds: float | None
    dts_seconds: float | None
    duration_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not self.stream_id:
            raise ValueError("stream_id must not be empty")
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class DecodeObservation:
    """The decode result for one bounded source-time window."""

    stream_id: str
    start_seconds: float
    end_seconds: float
    decodable: bool
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not self.stream_id:
            raise ValueError("stream_id must not be empty")
        if self.start_seconds < 0 or self.end_seconds < self.start_seconds:
            raise ValueError("decode observation has an invalid time range")
        if self.decodable and self.error_summary is not None:
            raise ValueError("successful decode observations cannot carry an error")


class MediaRunner(Protocol):
    """Small boundary that keeps scanner tests independent of local FFmpeg."""

    def iter_packets(self, source: Path) -> Iterator[PacketObservation]: ...

    def iter_decodes(
        self,
        source: Path,
        *,
        duration_seconds: float,
        chunk_seconds: float,
    ) -> Iterator[DecodeObservation]: ...


class FFmpegMediaRunner:
    """Shell-free FFmpeg adapter that streams metadata and bounded decode windows."""

    def __init__(self, config: RescueScanConfig) -> None:
        self._config = config

    def iter_packets(self, source: Path) -> Iterator[PacketObservation]:
        arguments = [
            self._config.ffprobe_executable,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=stream_index,pts_time,dts_time,duration_time",
            "-of",
            "compact=p=0:nk=0",
            str(source),
        ]
        for line in _iter_command_stdout_lines(
            arguments,
            source=source,
            timeout_seconds=self._config.command_timeout_seconds,
        ):
            values = _parse_compact_line(line)
            stream_index = values.get("stream_index")
            if stream_index is None:
                continue
            yield PacketObservation(
                stream_id=f"video:{stream_index}",
                pts_seconds=_optional_timestamp_seconds(values.get("pts_time")),
                dts_seconds=_optional_timestamp_seconds(values.get("dts_time")),
                duration_seconds=_non_negative_seconds(values.get("duration_time")),
            )

    def iter_decodes(
        self,
        source: Path,
        *,
        duration_seconds: float,
        chunk_seconds: float,
    ) -> Iterator[DecodeObservation]:
        start_seconds = 0.0
        while start_seconds < duration_seconds:
            end_seconds = min(duration_seconds, start_seconds + chunk_seconds)
            arguments = [
                self._config.ffmpeg_executable,
                "-v",
                "error",
                "-xerror",
                "-ss",
                _format_seconds(start_seconds),
                "-t",
                _format_seconds(end_seconds - start_seconds),
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-progress",
                "pipe:1",
                "-nostats",
                "-f",
                "null",
                "-",
            ]
            error_summary = _decode_window(
                arguments,
                source=source,
                timeout_seconds=self._config.command_timeout_seconds,
                requested_duration_seconds=end_seconds - start_seconds,
            )
            yield DecodeObservation(
                stream_id="video:0",
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                decodable=error_summary is None,
                error_summary=error_summary,
            )
            start_seconds = end_seconds


class RescueScanner:
    """Convert bounded packet/decode observations into a deterministic damage map."""

    scanner_version: Final[str] = "1"

    def __init__(self, runner: MediaRunner | None = None) -> None:
        self._runner = runner

    def scan(
        self,
        source: Path,
        input_hash: str,
        metadata: VideoMetadata,
        config: RescueScanConfig,
    ) -> MediaDamageMap:
        """Scan read-only media without retaining decoded frames or packet output."""
        input_path = Path(source)
        runner = self._runner or FFmpegMediaRunner(config)
        try:
            decode_candidates, coverage = _observe_decodes(
                runner.iter_decodes(
                    input_path,
                    duration_seconds=metadata.duration_seconds,
                    chunk_seconds=config.chunk_seconds,
                ),
                duration_seconds=metadata.duration_seconds,
                merge_tolerance_seconds=config.merge_tolerance_seconds,
            )
            timestamp_candidates, packet_timestamp_summaries = (
                _timestamp_discontinuities(
                    runner.iter_packets(input_path),
                    duration_seconds=metadata.duration_seconds,
                    tolerance_seconds=config.timestamp_regression_tolerance_seconds,
                    merge_tolerance_seconds=config.merge_tolerance_seconds,
                )
            )
        except RescueScanError:
            raise
        except Exception as exc:
            raise RescueScanError(type(exc).__name__) from exc

        candidates = _attach_packet_timestamp_summaries(
            [*decode_candidates, *timestamp_candidates],
            packet_timestamp_summaries,
        )
        if not metadata.has_audio:
            candidates.append(
                _Candidate(
                    stream_id="audio",
                    kind=DamageKind.MISSING_STREAM,
                    start_seconds=0.0,
                    end_seconds=metadata.duration_seconds,
                    description=(
                        "No audio stream was observed in the container metadata."
                    ),
                    measurements={"stream_present": False},
                )
            )

        merged = _merge_candidates(candidates, config.merge_tolerance_seconds)
        intervals = tuple(
            DamageInterval(
                id=make_damage_id(
                    input_hash,
                    candidate.stream_id,
                    candidate.kind,
                    candidate.start_seconds,
                    candidate.end_seconds,
                ),
                stream_id=candidate.stream_id,
                kind=candidate.kind,
                start_seconds=candidate.start_seconds,
                end_seconds=candidate.end_seconds,
                description=candidate.description,
                measurements=candidate.measurements,
            )
            for candidate in merged
        )
        return MediaDamageMap(
            input_hash=input_hash,
            duration_seconds=metadata.duration_seconds,
            scanner_version=self.scanner_version,
            scan_coverage=coverage,
            intervals=intervals,
        )


@dataclass(frozen=True, slots=True)
class _Candidate:
    stream_id: str
    kind: DamageKind
    start_seconds: float
    end_seconds: float
    description: str
    measurements: dict[str, JsonValue]


def _observe_decodes(
    observations: Iterator[DecodeObservation],
    *,
    duration_seconds: float,
    merge_tolerance_seconds: float,
) -> tuple[list[_Candidate], tuple[tuple[float, float], ...]]:
    candidates: list[_Candidate] = []
    coverage: list[tuple[float, float]] = []
    seen_observation = False
    for observation in observations:
        seen_observation = True
        start_seconds = min(observation.start_seconds, duration_seconds)
        end_seconds = min(observation.end_seconds, duration_seconds)
        if end_seconds <= start_seconds:
            continue
        _append_range(coverage, start_seconds, end_seconds, merge_tolerance_seconds)
        kind = DamageKind.DECODABLE if observation.decodable else DamageKind.UNDECODABLE
        measurements: dict[str, JsonValue] = {"decode_observed": True}
        if observation.error_summary:
            measurements["error_summary"] = observation.error_summary
        _append_candidate(
            candidates,
            _Candidate(
                stream_id=observation.stream_id,
                kind=kind,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                description=(
                    "A bounded decode completed for this source interval."
                    if observation.decodable
                    else "A bounded decode reported an unreadable source interval."
                ),
                measurements=measurements,
            ),
            merge_tolerance_seconds,
        )
    if not seen_observation and duration_seconds > 0:
        candidates.append(
            _Candidate(
                stream_id="video:0",
                kind=DamageKind.MISSING_INFORMATION,
                start_seconds=0.0,
                end_seconds=duration_seconds,
                description="No bounded video decode observation was available.",
                measurements={"decode_observations": 0},
            )
        )
    return candidates, tuple(coverage)


def _timestamp_discontinuities(
    observations: Iterator[PacketObservation],
    *,
    duration_seconds: float,
    tolerance_seconds: float,
    merge_tolerance_seconds: float,
) -> tuple[list[_Candidate], dict[str, dict[str, JsonValue]]]:
    candidates: list[_Candidate] = []
    previous_by_stream: dict[str, float] = {}
    previous_end_by_stream: dict[str, float] = {}
    first_by_stream: dict[str, float] = {}
    last_by_stream: dict[str, float] = {}
    for observation in observations:
        timestamp = (
            observation.dts_seconds
            if observation.dts_seconds is not None
            else observation.pts_seconds
        )
        if timestamp is None:
            continue
        first_by_stream.setdefault(observation.stream_id, timestamp)
        last_by_stream[observation.stream_id] = timestamp
        previous = previous_by_stream.get(observation.stream_id)
        previous_end = previous_end_by_stream.get(observation.stream_id)
        previous_by_stream[observation.stream_id] = timestamp
        previous_end_by_stream[observation.stream_id] = (
            timestamp + observation.duration_seconds
        )
        if previous_end is not None and timestamp > previous_end + tolerance_seconds:
            _append_candidate(
                candidates,
                _Candidate(
                    stream_id=observation.stream_id,
                    kind=DamageKind.TIMESTAMP_DISCONTINUITY,
                    start_seconds=max(0.0, previous_end),
                    end_seconds=min(duration_seconds, timestamp),
                    description="Packet timestamps contain a forward gap.",
                    measurements={"packet_gap_seconds": timestamp - previous_end},
                ),
                merge_tolerance_seconds,
            )
        if previous is None or timestamp + tolerance_seconds >= previous:
            continue
        start_seconds = max(0.0, observation.pts_seconds or timestamp)
        end_seconds = min(
            duration_seconds,
            max(start_seconds, previous, start_seconds + observation.duration_seconds),
        )
        _append_candidate(
            candidates,
            _Candidate(
                stream_id=observation.stream_id,
                kind=DamageKind.TIMESTAMP_DISCONTINUITY,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                description=(
                    "Packet DTS values were not monotonic in this observed interval."
                ),
                measurements={"dts_monotonic": False, "previous_dts_seconds": previous},
            ),
            merge_tolerance_seconds,
        )
    summaries: dict[str, dict[str, JsonValue]] = {
        stream_id: {
            "first_valid_timestamp_seconds": first_by_stream[stream_id],
            "last_valid_timestamp_seconds": last_by_stream[stream_id],
        }
        for stream_id in sorted(first_by_stream)
    }
    return candidates, summaries


def _attach_packet_timestamp_summaries(
    candidates: list[_Candidate],
    summaries: dict[str, dict[str, JsonValue]],
) -> list[_Candidate]:
    return [
        _Candidate(
            stream_id=candidate.stream_id,
            kind=candidate.kind,
            start_seconds=candidate.start_seconds,
            end_seconds=candidate.end_seconds,
            description=candidate.description,
            measurements={
                **candidate.measurements,
                **summaries.get(candidate.stream_id, {}),
            },
        )
        for candidate in candidates
    ]


def _merge_candidates(
    candidates: list[_Candidate], tolerance_seconds: float
) -> list[_Candidate]:
    merged: list[_Candidate] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (
            item.stream_id,
            item.kind.value,
            item.start_seconds,
            item.end_seconds,
        ),
    ):
        if (
            merged
            and merged[-1].stream_id == candidate.stream_id
            and merged[-1].kind is candidate.kind
            and candidate.start_seconds - merged[-1].end_seconds <= tolerance_seconds
        ):
            previous = merged[-1]
            merged[-1] = _Candidate(
                stream_id=previous.stream_id,
                kind=previous.kind,
                start_seconds=previous.start_seconds,
                end_seconds=max(previous.end_seconds, candidate.end_seconds),
                description=previous.description,
                measurements=previous.measurements,
            )
        else:
            merged.append(candidate)
    return merged


def _append_candidate(
    candidates: list[_Candidate], candidate: _Candidate, tolerance_seconds: float
) -> None:
    if (
        candidates
        and candidates[-1].stream_id == candidate.stream_id
        and candidates[-1].kind is candidate.kind
        and candidate.start_seconds - candidates[-1].end_seconds <= tolerance_seconds
    ):
        previous = candidates[-1]
        candidates[-1] = _Candidate(
            stream_id=previous.stream_id,
            kind=previous.kind,
            start_seconds=previous.start_seconds,
            end_seconds=max(previous.end_seconds, candidate.end_seconds),
            description=previous.description,
            measurements=previous.measurements,
        )
        return
    candidates.append(candidate)


def _append_range(
    ranges: list[tuple[float, float]],
    start_seconds: float,
    end_seconds: float,
    tolerance_seconds: float,
) -> None:
    if ranges and start_seconds - ranges[-1][1] <= tolerance_seconds:
        ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end_seconds))
        return
    ranges.append((start_seconds, end_seconds))


def _iter_command_stdout_lines(
    arguments: list[str], *, source: Path, timeout_seconds: float
) -> Iterator[str]:
    """Yield FFprobe lines while continuously draining a bounded stderr tail."""
    stderr_data = bytearray()
    with _start_process(arguments, stderr_data) as process:
        stdout = process.stdout
        assert stdout is not None
        lines: Queue[str | None] = Queue(maxsize=16)
        stop_reader = threading.Event()

        def put_line(line: str | None) -> None:
            while not stop_reader.is_set():
                try:
                    lines.put(line, timeout=0.05)
                    return
                except Full:
                    continue

        def read_stdout() -> None:
            try:
                for raw_line in stdout:
                    if stop_reader.is_set():
                        return
                    put_line(raw_line.decode("utf-8", "replace"))
            finally:
                put_line(None)

        worker = threading.Thread(target=read_stdout, daemon=True)
        worker.start()
        deadline = monotonic() + timeout_seconds
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    _stop_process(process)
                    raise RescueScanError("ffprobe timed out")
                try:
                    line = lines.get(timeout=remaining)
                except Empty:
                    _stop_process(process)
                    raise RescueScanError("ffprobe timed out") from None
                if line is None:
                    break
                yield line.rstrip("\n")
            remaining = max(0.0, deadline - monotonic())
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            raise RescueScanError("ffprobe timed out") from exc
        finally:
            stop_reader.set()
            if process.poll() is None:
                _stop_process(process)
            stdout.close()
            worker.join(timeout=PROCESS_STOP_GRACE_SECONDS)
    if process.returncode != 0:
        raise RescueScanError(
            _stderr_summary(stderr_data, source=source, executable="ffprobe")
        )


def _decode_window(
    arguments: list[str],
    *,
    source: Path,
    timeout_seconds: float,
    requested_duration_seconds: float,
) -> str | None:
    stderr_data = bytearray()
    progress_data = bytearray()
    with _start_process(arguments, stderr_data) as process:
        stdout = process.stdout
        assert stdout is not None

        def drain_progress() -> None:
            while True:
                chunk = stdout.read(512)
                if not chunk:
                    return
                _append_bounded_tail(
                    progress_data,
                    chunk,
                    PROGRESS_CAPTURE_BYTES,
                )

        progress_worker = threading.Thread(target=drain_progress, daemon=True)
        progress_worker.start()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _stop_process(process)
            raise RescueScanError("ffmpeg decode window timed out") from exc
        finally:
            progress_worker.join(timeout=PROCESS_STOP_GRACE_SECONDS)
            stdout.close()
    if process.returncode == 0 or _decode_progress_covers_window(
        bytes(progress_data), requested_duration_seconds
    ):
        return None
    return _stderr_summary(stderr_data, source=source, executable="ffmpeg")


def _decode_progress_covers_window(
    progress_data: bytes, requested_duration_seconds: float
) -> bool:
    """Accept boundary read-ahead errors only after the requested window completed."""
    if not isfinite(requested_duration_seconds) or requested_duration_seconds <= 0:
        return False
    latest_microseconds: int | None = None
    completed = False
    for raw_line in progress_data.decode("ascii", "ignore").splitlines():
        key, separator, value = raw_line.partition("=")
        if not separator:
            continue
        if key == "progress" and value == "end":
            completed = True
        elif key == "out_time_us":
            try:
                candidate = int(value)
            except ValueError:
                continue
            if candidate >= 0:
                latest_microseconds = max(latest_microseconds or 0, candidate)
    if not completed or latest_microseconds is None:
        return False
    requested_microseconds = round(requested_duration_seconds * 1_000_000)
    return latest_microseconds >= max(0, requested_microseconds - 1)


def _stderr_summary(stderr_data: bytearray, *, source: Path, executable: str) -> str:
    text = bytes(stderr_data).decode("utf-8", "replace")
    summary = _sanitize_stderr_tail(text, sensitive_paths=(source, Path.home()))
    prefix = f"{executable}: "
    prefix_bytes = len(prefix.encode("utf-8"))
    return prefix + _cap_utf8_tail(summary, MAX_STDERR_BYTES - prefix_bytes)


class _ProcessContext:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        worker: threading.Thread,
    ) -> None:
        self.process = process
        self.worker = worker

    def __enter__(self) -> Any:
        return self.process

    def __exit__(self, *_args: object) -> None:
        if self.process.poll() is None:
            _stop_process(self.process)
        self.worker.join(timeout=PROCESS_STOP_GRACE_SECONDS)
        if self.process.stderr is not None:
            self.process.stderr.close()


def _start_process(
    arguments: list[str], stderr_data: bytearray, *, stdout: int = subprocess.PIPE
) -> _ProcessContext:
    try:
        process: subprocess.Popen[bytes] = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            **pinned_subprocess_options(arguments),
        )
    except FileNotFoundError as exc:
        raise RescueScanError(
            f"{Path(arguments[0]).name} executable was not found"
        ) from exc
    except OSError as exc:
        raise RescueScanError(f"{Path(arguments[0]).name} could not start") from exc
    stderr = process.stderr
    assert stderr is not None

    def drain() -> None:
        while True:
            chunk = stderr.read(512)
            if not chunk:
                return
            _append_bounded_tail(stderr_data, chunk, STDERR_CAPTURE_BYTES)

    worker = threading.Thread(target=drain, daemon=True)
    worker.start()
    return _ProcessContext(process, worker)


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=PROCESS_STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _append_bounded_tail(target: bytearray, chunk: bytes, limit: int) -> None:
    if len(chunk) >= limit:
        target[:] = chunk[-limit:]
        return
    overflow = len(target) + len(chunk) - limit
    if overflow > 0:
        del target[:overflow]
    target.extend(chunk)


def _sanitize_stderr_tail(text: str, *, sensitive_paths: tuple[Path, ...]) -> str:
    diagnostic = text.strip()
    replacements: set[str] = set()
    for path in sensitive_paths:
        candidates = (str(path), path.as_posix())
        replacements.update(candidate for candidate in candidates if candidate)
        try:
            resolved = path.resolve(strict=False)
        except OSError:
            continue
        replacements.update((str(resolved), resolved.as_posix()))
    for candidate in sorted(replacements, key=len, reverse=True):
        diagnostic = diagnostic.replace(candidate, REDACTED_PATH)
    return diagnostic or "no diagnostic output"


def _cap_utf8_tail(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    return encoded[-limit:].decode("utf-8", "ignore")


def _parse_compact_line(line: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for field in line.split("|"):
        key, separator, value = field.partition("=")
        if separator:
            values[key] = value
    return values


def _optional_timestamp_seconds(value: str | None) -> float | None:
    if value is None or value in ("", "N/A"):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if isfinite(result) else None


def _non_negative_seconds(value: str | None) -> float:
    result = _optional_timestamp_seconds(value)
    return result if result is not None and result >= 0 else 0.0


def _format_seconds(value: float) -> str:
    return format(value, ".9g")
