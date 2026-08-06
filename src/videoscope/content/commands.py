"""Pure FFmpeg/FFprobe argument builders for useful-content execution."""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path

from videoscope.content.models import ContentTimeRange


def build_content_segment_command(
    source: Path,
    output: Path,
    source_range: ContentTimeRange,
    *,
    has_audio: bool,
    audio_fade_seconds: float = 0.0,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build one exact-range, independently decodable segment encode."""
    _validate_fade(audio_fade_seconds, source_range.duration_seconds)
    command = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        _seconds(source_range.start_seconds),
        "-i",
        str(source),
        "-t",
        _seconds(source_range.duration_seconds),
        "-map",
        "0:v:0",
    ]
    if has_audio:
        command.extend(("-map", "0:a:0"))
    command.extend(
        (
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        )
    )
    if has_audio:
        command.extend(("-c:a", "aac"))
        if audio_fade_seconds > 0:
            fade_out_start = source_range.duration_seconds - audio_fade_seconds
            command.extend(
                (
                    "-af",
                    (
                        f"afade=t=in:st=0:d={_seconds(audio_fade_seconds)},"
                        f"afade=t=out:st={_seconds(fade_out_start)}:"
                        f"d={_seconds(audio_fade_seconds)}"
                    ),
                )
            )
    else:
        command.append("-an")
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def build_content_concat_command(
    segments: Sequence[Path],
    output: Path,
    *,
    has_audio: bool,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build a deterministic hard join for equally encoded segments."""
    if not segments:
        raise ValueError("content join requires at least one segment")
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    for segment in segments:
        command.extend(("-i", str(segment)))
    if has_audio:
        inputs = "".join(
            f"[{index}:v:0][{index}:a:0]" for index in range(len(segments))
        )
        graph = f"{inputs}concat=n={len(segments)}:v=1:a=1[v][a]"
    else:
        inputs = "".join(f"[{index}:v:0]" for index in range(len(segments)))
        graph = f"{inputs}concat=n={len(segments)}:v=1:a=0[v]"
    command.extend(("-filter_complex", graph, "-map", "[v]"))
    if has_audio:
        command.extend(("-map", "[a]", "-c:a", "aac"))
    else:
        command.append("-an")
    command.extend(
        (
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        )
    )
    return command


def build_content_clip_command(
    source: Path,
    output: Path,
    source_range: ContentTimeRange,
    *,
    has_audio: bool,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Build an optional per-selection clip export."""
    return build_content_segment_command(
        source,
        output,
        source_range,
        has_audio=has_audio,
        ffmpeg=ffmpeg,
    )


def build_chapter_mux_command(
    source: Path,
    metadata: Path,
    output: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> list[str]:
    """Attach reviewed chapter metadata without re-encoding media."""
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-i",
        str(metadata),
        "-map",
        "0",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]


def build_content_duration_probe_command(
    source: Path,
    *,
    ffprobe: str = "ffprobe",
) -> list[str]:
    """Build a bounded single-value output duration query."""
    return [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source),
    ]


def _validate_fade(fade_seconds: float, duration_seconds: float) -> None:
    if not math.isfinite(fade_seconds) or fade_seconds < 0:
        raise ValueError("audio fade must be finite and non-negative")
    if fade_seconds > 0.5:
        raise ValueError("audio fade exceeds the bounded maximum")
    if fade_seconds * 2 > duration_seconds:
        raise ValueError("audio fade cannot consume the segment")


def _seconds(value: float) -> str:
    return f"{value:.6f}"


__all__ = [
    "build_chapter_mux_command",
    "build_content_clip_command",
    "build_content_concat_command",
    "build_content_duration_probe_command",
    "build_content_segment_command",
]
