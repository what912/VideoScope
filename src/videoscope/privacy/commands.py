"""Shell-free FFmpeg argument builders for Safe Sharing."""

from __future__ import annotations

import math
from pathlib import Path

from videoscope.privacy.errors import PrivacyPlanError
from videoscope.privacy.models import PrivacyActionKind, PrivacyPlan


def build_privacy_preview_arguments(
    plan: PrivacyPlan,
    source: Path,
    output: Path,
    ffmpeg: str = "ffmpeg",
    *,
    video_source: Path | None = None,
) -> list[str]:
    """Return a bounded, metadata-free preview command as an argument array."""
    _require_distinct_paths(source, output)
    if video_source is not None:
        _require_distinct_paths(video_source, output)
    duration = _preview_duration(plan)
    arguments = _prefix(ffmpeg, video_source or source)
    audio_input = 0
    if video_source is not None:
        arguments.extend(("-i", str(source)))
        audio_input = 1
    arguments.extend(("-t", _format_seconds(duration)))
    arguments.extend(_explicit_mapping(audio_input=audio_input))
    arguments.extend(("-c:v", "libx264", "-preset", "veryfast", "-crf", "28"))
    mute_filter = build_privacy_audio_mute_filter(plan)
    if mute_filter:
        arguments.extend(("-af", mute_filter))
    arguments.extend(("-c:a", "aac", "-b:a", "128k"))
    arguments.extend(_publication_options())
    arguments.append(str(output))
    return arguments


def build_privacy_audio_arguments(
    plan: PrivacyPlan,
    source: Path,
    output: Path,
    ffmpeg: str = "ffmpeg",
    *,
    audio_source: Path | None = None,
) -> list[str]:
    """Return a video-copy/audio-transcode command for reviewed mute intervals."""
    _require_distinct_paths(source, output)
    if audio_source is not None:
        _require_distinct_paths(audio_source, output)
    mute_filter = build_privacy_audio_mute_filter(plan)
    if not mute_filter:
        raise PrivacyPlanError("audio processing requires a reviewed mute interval")
    arguments = _prefix(ffmpeg, source)
    if audio_source is not None:
        arguments.extend(("-i", str(audio_source)))
    arguments.extend(
        _explicit_mapping(audio_input=1 if audio_source is not None else 0)
    )
    arguments.extend(
        ("-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-af", mute_filter)
    )
    arguments.extend(_publication_options())
    arguments.append(str(output))
    return arguments


def build_privacy_remux_arguments(
    plan: PrivacyPlan,
    source: Path,
    output: Path,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Return a stream-copy command that removes global/stream/chapter metadata."""
    del plan
    _require_distinct_paths(source, output)
    arguments = _prefix(ffmpeg, source)
    arguments.extend(_explicit_mapping())
    arguments.extend(("-c", "copy"))
    arguments.extend(_publication_options())
    arguments.append(str(output))
    return arguments


def build_privacy_rawvideo_decode_arguments(
    source: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Return a video-only BGR raw-frame decoder command."""
    if not ffmpeg:
        raise PrivacyPlanError("FFmpeg executable name cannot be empty")
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-noautorotate",
        "-i",
        str(source),
    ]
    arguments.extend(
        (
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-dn",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-fps_mode",
            "passthrough",
            "pipe:1",
        )
    )
    return arguments


def build_privacy_frame_timestamp_arguments(
    source: Path,
    *,
    ffprobe: str = "ffprobe",
) -> list[str]:
    """Return a streaming per-frame best-effort PTS probe command."""
    if not ffprobe:
        raise PrivacyPlanError("ffprobe executable name cannot be empty")
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time",
        "-of",
        "csv=p=0",
        str(source),
    ]


def build_privacy_rawvideo_encode_arguments(
    output: Path,
    *,
    width: int,
    height: int,
    frame_rate: float,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Return a metadata-free H.264 encoder command for BGR raw frames."""
    if not ffmpeg:
        raise PrivacyPlanError("FFmpeg executable name cannot be empty")
    if width <= 0 or height <= 0:
        raise PrivacyPlanError("rawvideo dimensions must be positive")
    if not math.isfinite(frame_rate) or frame_rate <= 0:
        raise PrivacyPlanError("rawvideo frame rate must be positive and finite")
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "-s:v",
        f"{width}x{height}",
        "-r",
        _format_seconds(frame_rate),
        "-i",
        "pipe:0",
        "-map",
        "0:v:0",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    ]
    arguments.extend(_publication_options())
    arguments.append(str(output))
    return arguments


def build_privacy_audio_mute_filter(plan: PrivacyPlan) -> str:
    """Return the deterministic FFmpeg volume chain from reviewed plan actions."""
    intervals = sorted(
        (
            (action.start_seconds, action.end_seconds, action.id)
            for action in plan.actions
            if action.kind is PrivacyActionKind.AUDIO_MUTE
        ),
        key=lambda item: (item[0], item[1], item[2]),
    )
    return ",".join(
        "volume=enable='between(t,"
        f"{_format_seconds(start)},{_format_seconds(end)}"
        ")':volume=0"
        for start, end, _ in intervals
    )


def _prefix(ffmpeg: str, source: Path) -> list[str]:
    if not ffmpeg:
        raise PrivacyPlanError("FFmpeg executable name cannot be empty")
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
    ]


def _require_distinct_paths(source: Path, output: Path) -> None:
    """Enforce the confirmation-bound promise that the source stays read-only."""
    if source.resolve() == output.resolve():
        raise PrivacyPlanError("source read-only contract forbids in-place output")


def _explicit_mapping(*, audio_input: int = 0) -> list[str]:
    return ["-map", "0:v:0", "-map", f"{audio_input}:a:0?"]


def _publication_options() -> list[str]:
    return [
        "-map_metadata",
        "-1",
        "-map_metadata:s",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
    ]


def _preview_duration(plan: PrivacyPlan) -> float:
    if plan.duration_seconds is None:
        raise PrivacyPlanError("preview requires the source duration")
    return float(min(plan.effective_config.preview_seconds, plan.duration_seconds))


def _format_seconds(value: float) -> str:
    return format(value, ".15g")


__all__ = [
    "build_privacy_audio_arguments",
    "build_privacy_audio_mute_filter",
    "build_privacy_frame_timestamp_arguments",
    "build_privacy_preview_arguments",
    "build_privacy_rawvideo_decode_arguments",
    "build_privacy_rawvideo_encode_arguments",
    "build_privacy_remux_arguments",
]
