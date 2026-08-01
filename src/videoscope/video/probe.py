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
from videoscope.video.errors import (
    ExternalToolNotFoundError,
    NoVideoStreamError,
    VideoDecodeError,
    VideoNotFoundError,
    VideoProbeError,
    sanitize_diagnostic,
)

DEFAULT_PROBE_TIMEOUT_SECONDS = 30.0


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
        raise VideoProbeError(
            f"ffprobe returned invalid JSON for: {input_path.name}"
        ) from exc
    if not isinstance(raw_payload, Mapping):
        raise VideoProbeError(
            f"ffprobe returned an invalid JSON root for: {input_path.name}"
        )
    return metadata_from_ffprobe(
        cast(Mapping[str, Any], raw_payload),
        input_path=input_path,
    )
