"""Normalize privacy-safe video metadata from ffprobe JSON output."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

from pydantic import JsonValue, ValidationError

from videoscope.domain import VideoMetadata
from videoscope.privacy.metadata import (
    PrivateProbeSummary,
    private_probe_summary_from_ffprobe,
)
from videoscope.processes import pinned_subprocess_options
from videoscope.video.errors import (
    ExternalToolNotFoundError,
    NoVideoStreamError,
    VideoDecodeError,
    VideoNotFoundError,
    VideoProbeError,
    sanitize_diagnostic,
)

DEFAULT_PROBE_TIMEOUT_SECONDS = 30.0
FFPROBE_METADATA_ATTEMPTS = 2


def parse_frame_rate(value: object) -> float:
    """Parse ffprobe integer or rational frame-rate values."""
    if value is None:
        return 0.0
    try:
        rate = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return 0.0
    if rate <= 0:
        return 0.0
    return float(rate)


def _non_negative_float(value: object) -> float:
    if value in (None, "", "N/A"):
        return 0.0
    try:
        converted = float(str(value))
    except (TypeError, ValueError):
        return 0.0
    return converted if converted >= 0 else 0.0


def _non_negative_int(value: object) -> int:
    if value in (None, "", "N/A"):
        return 0
    try:
        converted = int(str(value))
    except (TypeError, ValueError):
        return 0
    return converted if converted >= 0 else 0


def _positive_bounded_int(value: object, *, maximum: int) -> int | None:
    """Parse a positive integer without accepting booleans or decimals."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not text.isdigit():
        return None
    converted = int(text)
    return converted if 0 < converted <= maximum else None


def _duration_seconds(
    video_stream: Mapping[str, Any],
    media_format: Mapping[str, Any],
) -> float:
    stream_duration = _non_negative_float(video_stream.get("duration"))
    if stream_duration > 0:
        return stream_duration
    return _non_negative_float(media_format.get("duration"))


def _mapping(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, Mapping) else {}


def _creation_time(
    video_stream: Mapping[str, Any],
    media_format: Mapping[str, Any],
) -> datetime | None:
    for owner in (video_stream, media_format):
        raw_value = _mapping(owner.get("tags")).get("creation_time")
        if not isinstance(raw_value, str) or not raw_value:
            continue
        try:
            return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _rotation_degrees(video_stream: Mapping[str, Any]) -> float:
    candidates: list[object] = []
    raw_side_data = video_stream.get("side_data_list")
    if isinstance(raw_side_data, list):
        candidates.extend(
            _mapping(side_data).get("rotation") for side_data in raw_side_data
        )
    candidates.append(_mapping(video_stream.get("tags")).get("rotate"))
    for candidate in candidates:
        if candidate in (None, "", "N/A"):
            continue
        try:
            rotation = float(str(candidate))
        except (TypeError, ValueError):
            continue
        normalized = rotation % 360.0
        if normalized > 180.0:
            normalized -= 360.0
        return 0.0 if abs(normalized) < 1e-6 else normalized
    return 0.0


def _select_video_stream(
    streams: list[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    video_streams = [
        stream for stream in streams if stream.get("codec_type") == "video"
    ]
    if not video_streams:
        return None
    return next(
        (
            stream
            for stream in video_streams
            if _mapping(stream.get("disposition")).get("default") == 1
        ),
        video_streams[0],
    )


def metadata_from_ffprobe(
    payload: Mapping[str, Any],
    *,
    input_path: Path,
) -> VideoMetadata:
    """Map a decoded ffprobe JSON object to the stable domain model."""
    raw_streams = payload.get("streams")
    streams = (
        [_mapping(stream) for stream in raw_streams]
        if isinstance(raw_streams, list)
        else []
    )
    video_stream = _select_video_stream(streams)
    if video_stream is None:
        raise NoVideoStreamError(f"No video stream found in: {input_path.name}")

    media_format = _mapping(payload.get("format"))
    duration = _duration_seconds(video_stream, media_format)
    frame_rate = parse_frame_rate(video_stream.get("avg_frame_rate"))
    if frame_rate == 0:
        frame_rate = parse_frame_rate(video_stream.get("r_frame_rate"))
    frame_count = _non_negative_int(video_stream.get("nb_frames"))
    if frame_count == 0 and duration > 0 and frame_rate > 0:
        frame_count = round(duration * frame_rate)

    raw_probe: dict[str, JsonValue] = {
        "format_name": str(media_format.get("format_name") or "unknown"),
        "video_stream_index": _non_negative_int(video_stream.get("index")),
    }
    optional_summary = {
        "format_long_name": media_format.get("format_long_name"),
        "pixel_format": video_stream.get("pix_fmt"),
        "color_range": video_stream.get("color_range"),
        "color_space": video_stream.get("color_space"),
    }
    raw_probe.update(
        {
            key: str(value)
            for key, value in optional_summary.items()
            if value not in (None, "")
        }
    )
    audio_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"),
        None,
    )
    if audio_stream is not None and audio_stream.get("codec_name"):
        raw_probe["audio_codec"] = str(audio_stream["codec_name"])
    if audio_stream is not None:
        audio_sample_rate = _positive_bounded_int(
            audio_stream.get("sample_rate"), maximum=384000
        )
        if audio_sample_rate is not None and audio_sample_rate >= 8000:
            raw_probe["audio_sample_rate_hz"] = audio_sample_rate
    rotation_degrees = _rotation_degrees(video_stream)
    if rotation_degrees != 0:
        raw_probe["rotation_degrees"] = rotation_degrees

    try:
        return VideoMetadata(
            filename=input_path.name,
            container_format=str(media_format.get("format_name") or "unknown"),
            codec=str(
                video_stream.get("codec_name")
                or video_stream.get("codec_long_name")
                or "unknown"
            ),
            width=int(video_stream.get("width", 0)),
            height=int(video_stream.get("height", 0)),
            duration_seconds=duration,
            average_frame_rate=frame_rate,
            estimated_frame_count=frame_count,
            has_audio=any(stream.get("codec_type") == "audio" for stream in streams),
            file_size_bytes=input_path.stat().st_size,
            creation_time=_creation_time(video_stream, media_format),
            raw_probe=raw_probe,
        )
    except (OSError, TypeError, ValueError, ValidationError) as exc:
        raise VideoProbeError(
            f"ffprobe metadata was incomplete for: {input_path.name}"
        ) from exc


def probe_video(
    path: Path,
    *,
    ffprobe: str = "ffprobe",
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> VideoMetadata:
    """Probe one local video through a shell-free ffprobe invocation."""
    input_path, payload = _probe_payload(
        path,
        ffprobe=ffprobe,
        timeout_seconds=timeout_seconds,
    )
    return metadata_from_ffprobe(payload, input_path=input_path)


def probe_video_with_private_summary(
    path: Path,
    *,
    ffprobe: str = "ffprobe",
    timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> tuple[VideoMetadata, PrivateProbeSummary]:
    """Probe once and keep sensitive tags only in a bounded private summary."""
    input_path, payload = _probe_payload(
        path,
        ffprobe=ffprobe,
        timeout_seconds=timeout_seconds,
    )
    public_metadata = metadata_from_ffprobe(payload, input_path=input_path)
    private_summary = private_probe_summary_from_ffprobe(
        payload,
        filename=input_path.name,
        duration_seconds=public_metadata.duration_seconds,
    )
    return public_metadata, private_summary


def _probe_payload(
    path: Path,
    *,
    ffprobe: str,
    timeout_seconds: float,
) -> tuple[Path, Mapping[str, Any]]:
    """Return decoded ffprobe JSON without logging or persisting its raw form."""
    input_path = Path(path)
    if not input_path.is_file():
        raise VideoNotFoundError(f"Input file not found: {input_path.name}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    arguments = [
        ffprobe,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(input_path),
    ]
    for attempt in range(1, FFPROBE_METADATA_ATTEMPTS + 1):
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
                f"Required executable not found: {Path(ffprobe).name}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise VideoProbeError(
                f"ffprobe timed out while probing: {input_path.name}"
            ) from exc
        except OSError as exc:
            raise VideoProbeError(
                f"Could not start ffprobe for: {input_path.name}"
            ) from exc

        if completed.returncode != 0:
            diagnostic = sanitize_diagnostic(
                completed.stderr or completed.stdout,
                sensitive_paths=(input_path,),
            )
            raise VideoDecodeError(
                f"ffprobe could not decode: {input_path.name}",
                stderr_summary=diagnostic,
            )

        try:
            raw_payload: object = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            if attempt < FFPROBE_METADATA_ATTEMPTS:
                continue
            raise VideoProbeError(
                "ffprobe returned invalid JSON after "
                f"{attempt} attempts for: {input_path.name} "
                f"(line {exc.lineno}, column {exc.colno})"
            ) from exc
        if not isinstance(raw_payload, Mapping):
            if attempt < FFPROBE_METADATA_ATTEMPTS:
                continue
            raise VideoProbeError(
                "ffprobe returned an invalid JSON root after "
                f"{attempt} attempts for: {input_path.name}"
            )
        payload = cast(Mapping[str, Any], raw_payload)
        structure_error = _ffprobe_payload_structure_error(payload)
        if structure_error is not None:
            if attempt < FFPROBE_METADATA_ATTEMPTS:
                continue
            raise VideoProbeError(
                "ffprobe returned an unusable JSON structure after "
                f"{attempt} attempts for: {input_path.name} "
                f"({structure_error})"
            )
        return input_path, payload

    raise AssertionError("ffprobe metadata retry loop exhausted unexpectedly")


def _ffprobe_payload_structure_error(payload: Mapping[str, Any]) -> str | None:
    """Return a stable reason for truncated ffprobe container structures."""
    streams = payload.get("streams")
    if not isinstance(streams, list):
        return "streams_not_array"
    if any(not isinstance(stream, Mapping) for stream in streams):
        return "stream_not_object"
    if not isinstance(payload.get("format"), Mapping):
        return "format_not_object"
    return None
