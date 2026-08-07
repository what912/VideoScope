"""Shell-free FFmpeg argument builders for Publish Ready."""

from __future__ import annotations

import math
from pathlib import Path

from videoscope.resolve.errors import PublishInputError
from videoscope.resolve.models import PublishActionKind, PublishPlan

_PREVIEW_DURATION_SECONDS = 6.0
_MAXIMUM_OUTPUT_FPS = 60.0


def _prefix(ffmpeg: str) -> list[str]:
    if not ffmpeg:
        raise PublishInputError("FFmpeg executable name cannot be empty")
    return [ffmpeg, "-hide_banner", "-nostdin", "-y"]


def _has_action(plan: PublishPlan, kind: PublishActionKind) -> bool:
    return any(action.kind is kind for action in plan.actions)


def _video_filters(plan: PublishPlan) -> tuple[str, ...]:
    filters: list[str] = []
    scale_actions = [
        action for action in plan.actions if action.kind is PublishActionKind.SCALE_PAD
    ]
    if len(scale_actions) > 1:
        raise PublishInputError("PublishPlan contains duplicate scale-and-pad actions")
    if scale_actions:
        parameters = scale_actions[0].parameters
        width = parameters.get("width")
        height = parameters.get("height")
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or width <= 0
            or not isinstance(height, int)
            or isinstance(height, bool)
            or height <= 0
        ):
            raise PublishInputError(
                "Scale-and-pad dimensions must be validated positive integers"
            )
        filters.extend(
            (
                f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease",
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
                "setsar=1",
            )
        )
    if plan.source_metadata.average_frame_rate > _MAXIMUM_OUTPUT_FPS:
        filters.append("fps=60")
    return tuple(filters)


def _stream_mapping(plan: PublishPlan) -> list[str]:
    arguments = ["-map", "0:v:0"]
    if plan.source_metadata.has_audio:
        arguments.extend(("-map", "0:a:0?"))
    return arguments


def _transcode_options(plan: PublishPlan) -> list[str]:
    arguments = [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
    ]
    filters = _video_filters(plan)
    if filters:
        arguments.extend(("-vf", ",".join(filters)))
    if plan.source_metadata.has_audio:
        arguments.extend(("-c:a", "aac", "-b:a", "192k"))
    else:
        arguments.append("-an")
    return arguments


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


def _format_seconds(value: float) -> str:
    return format(value, ".15g")


def build_publish_arguments(
    plan: PublishPlan,
    source_path: Path,
    output_path: Path,
    *,
    ffmpeg: str = "ffmpeg",
) -> tuple[str, ...]:
    remux = _has_action(plan, PublishActionKind.REMUX)
    transcode = _has_action(plan, PublishActionKind.TRANSCODE)
    if remux == transcode:
        raise PublishInputError(
            "PublishPlan must contain exactly one remux or transcode action"
        )

    arguments = _prefix(ffmpeg)
    arguments.extend(("-i", str(source_path)))
    arguments.extend(_stream_mapping(plan))
    if remux:
        arguments.extend(("-c", "copy"))
        if not plan.source_metadata.has_audio:
            arguments.append("-an")
    else:
        arguments.extend(_transcode_options(plan))
    arguments.extend(_publication_options())
    arguments.append(str(output_path))
    return tuple(arguments)


def build_preview_arguments(
    plan: PublishPlan,
    source_path: Path,
    output_path: Path,
    *,
    ffmpeg: str = "ffmpeg",
    preview_seconds: float = _PREVIEW_DURATION_SECONDS,
) -> tuple[str, ...]:
    if not math.isfinite(preview_seconds) or preview_seconds <= 0:
        raise PublishInputError("Preview duration must be finite and greater than zero")
    duration = min(preview_seconds, plan.source_metadata.duration_seconds)
    start = max(0.0, (plan.source_metadata.duration_seconds - duration) / 2.0)
    arguments = _prefix(ffmpeg)
    arguments.extend(
        (
            "-ss",
            _format_seconds(start),
            "-i",
            str(source_path),
            "-t",
            _format_seconds(duration),
        )
    )
    arguments.extend(_stream_mapping(plan))
    arguments.extend(_transcode_options(plan))
    arguments.extend(_publication_options())
    arguments.append(str(output_path))
    return tuple(arguments)


def build_cover_arguments(
    source_path: Path,
    output_path: Path,
    *,
    duration_seconds: float,
    ffmpeg: str = "ffmpeg",
) -> tuple[str, ...]:
    if not math.isfinite(duration_seconds) or duration_seconds < 0:
        raise PublishInputError("Cover duration must be finite and non-negative")
    arguments = _prefix(ffmpeg)
    arguments.extend(
        (
            "-ss",
            _format_seconds(duration_seconds / 2.0),
            "-i",
            str(source_path),
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            "-map_metadata",
            "-1",
            str(output_path),
        )
    )
    return tuple(arguments)


__all__ = [
    "build_cover_arguments",
    "build_preview_arguments",
    "build_publish_arguments",
]
