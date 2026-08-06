"""Safe argument-vector builders for private Rescue media processing."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from videoscope.rescue.audio import (
    LoudnessConfig,
    audio_filter_fragment_from_actions,
    loudnorm_measurement_filter,
)
from videoscope.rescue.errors import RescueArtifactError
from videoscope.rescue.models import RescueActionKind, RescuePlan, RescueStrategy
from videoscope.rescue.timeline import (
    SourceMapping,
    mappings_for_ranges,
    preview_source_mappings,
    retained_source_ranges,
)
from videoscope.rescue.visual import (
    filter_fragment_from_action,
    flicker_correction_from_parameters,
    flicker_filter_fragment,
    remap_flicker_correction,
)

_FFMPEG = "ffmpeg"
_FFPROBE = "ffprobe"


def build_preview_commands(
    plan: RescuePlan, source: Path, work_root: Path
) -> tuple[list[str], ...]:
    """Return bounded ffmpeg argument vectors without shell interpolation."""
    _validate_preview_paths(plan, source, work_root)
    commands: list[list[str]] = []
    for index, (start_seconds, end_seconds) in enumerate(plan.preview_ranges):
        duration_seconds = end_seconds - start_seconds
        faithful_name = f"faithful-{index:02d}.mp4"
        faithful_path = work_root / faithful_name
        faithful_mappings = preview_source_mappings(
            plan, (start_seconds, end_seconds), faithful_name
        )
        if not faithful_mappings:
            continue
        faithful_duration = sum(
            mapping.output_end - mapping.output_start for mapping in faithful_mappings
        )
        commands.append(
            _preview_command(
                source,
                work_root / f"source-{index:02d}.mp4",
                start_seconds,
                duration_seconds,
            )
        )
        faithful_video, faithful_audio = _faithful_preview_filters(
            plan, start_seconds, end_seconds, faithful_mappings
        )
        commands.append(
            _preview_command(
                source,
                faithful_path,
                start_seconds,
                duration_seconds,
                video_filter=faithful_video,
                audio_filter=faithful_audio,
            )
        )
        if _supports_improved_preview(plan, faithful_mappings):
            video_filter, audio_filter = _improvement_filters(
                plan, source_mappings=faithful_mappings
            )
            commands.append(
                _preview_command(
                    faithful_path,
                    work_root / f"improved-{index:02d}.mp4",
                    0.0,
                    faithful_duration,
                    video_filter=video_filter,
                    audio_filter=audio_filter,
                )
            )
    return tuple(commands)


def build_faithful_remux_command(
    source: Path,
    output: Path,
    *,
    stream_copy: bool,
    source_range: tuple[float, float] | None = None,
    audio_filter: str | None = None,
    preserve_packet_origin: bool = False,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Build one full-source faithful output command."""
    command = _faithful_command_prefix(ffmpeg, source)
    if source_range is not None:
        start_seconds, end_seconds = source_range
        if start_seconds > 0:
            command.extend(("-ss", _seconds(start_seconds)))
        command.extend(("-t", _seconds(end_seconds - start_seconds)))
    if audio_filter is not None:
        if stream_copy:
            raise ValueError("a faithful audio correction cannot use stream copy")
        command.extend(("-filter:a:0", audio_filter))
    if stream_copy:
        command.extend(("-c", "copy"))
    else:
        command.extend(
            _faithful_reencode_arguments(
                preserve_negative_timestamps=(
                    audio_filter is not None or preserve_packet_origin
                )
            )
        )
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def build_loudnorm_measurement_command(
    source: Path,
    config: LoudnessConfig,
    *,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Build the first, measurement-only loudnorm pass as an argument vector."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-af",
        loudnorm_measurement_filter(config),
        "-f",
        "null",
        "-",
    ]


def build_audio_improvement_command(
    source: Path,
    output: Path,
    actions: tuple[RescueActionKind, ...],
    parameters: Mapping[str, object],
    *,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Build one atomic-candidate render preserving all unaffected streams."""
    audio_filter = audio_filter_fragment_from_actions(actions, parameters)
    if audio_filter is None:
        raise ValueError("confirmed audio actions have no valid bounded filter")
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-filter:a:0",
        audio_filter,
        "-c",
        "copy",
        "-c:v",
        "copy",
        "-c:a:0",
        "aac",
        "-avoid_negative_ts",
        "disabled",
        "-movflags",
        "+faststart",
        str(output),
    ]


def build_improved_viewing_command(
    plan: RescuePlan,
    source: Path,
    output: Path,
    *,
    source_mappings: Sequence[SourceMapping] | None = None,
    excluded_action_ids: frozenset[str] = frozenset(),
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Build one plan-bound CPU render from the already faithful candidate."""
    resolved_mappings = _resolved_improved_source_mappings(plan, source_mappings)
    video_filter, audio_filter = _improvement_filters(
        plan,
        source_mappings=resolved_mappings,
        excluded_action_ids=excluded_action_ids,
    )
    if not video_filter and not audio_filter:
        raise ValueError("confirmed plan contains no executable improvement filter")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
    ]
    if video_filter:
        command.extend(("-filter:v:0", video_filter, "-c:v:0", "libx264"))
    else:
        command.extend(("-c:v", "copy"))
    if audio_filter:
        command.extend(("-filter:a:0", audio_filter, "-c:a:0", "aac"))
    else:
        command.extend(("-c:a", "copy"))
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def build_keyframe_probe_command(
    source: Path,
    start_seconds: float,
    end_seconds: float,
    *,
    ffprobe: str = _FFPROBE,
) -> list[str]:
    """Build a bounded keyframe query for one retained source interval."""
    return [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-skip_frame",
        "nokey",
        "-read_intervals",
        f"{_seconds(start_seconds)}%{_seconds(end_seconds)}",
        "-show_entries",
        "frame=best_effort_timestamp_time",
        "-of",
        "json",
        str(source),
    ]


def build_packet_timestamp_probe_command(
    source: Path,
    *,
    ffprobe: str = _FFPROBE,
    maximum_packets: int = 128,
) -> list[str]:
    """Build a packet-count-bounded A/V timestamp query."""
    if isinstance(maximum_packets, bool) or maximum_packets <= 0:
        raise ValueError("maximum_packets must be a positive integer")
    return [
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-show_packets",
        "-read_intervals",
        f"%+#{maximum_packets}",
        "-show_entries",
        (
            "stream=index,codec_type,sample_rate:"
            "packet=stream_index,pts_time,dts_time,side_data_list"
        ),
        "-of",
        "json",
        str(source),
    ]


def build_ffprobe_version_command(*, ffprobe: str = _FFPROBE) -> list[str]:
    """Build the bounded runner's local FFprobe version query."""
    return [ffprobe, "-version"]


def build_faithful_segment_command(
    source: Path,
    output: Path,
    *,
    start_seconds: float,
    end_seconds: float,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Build one independently decodable, recovery-safe retained segment."""
    duration_seconds = end_seconds - start_seconds
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        _seconds(start_seconds),
        "-i",
        str(source),
        "-t",
        _seconds(duration_seconds),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
    ]
    command.extend(_faithful_reencode_arguments())
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def build_faithful_concat_command(
    manifest: Path,
    output: Path,
    *,
    audio_filter: str | None = None,
    preserve_packet_origin: bool = False,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Build a re-encoding concat command for independently verified segments."""
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(manifest),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
    ]
    if audio_filter is not None:
        command.extend(("-filter:a:0", audio_filter))
    command.extend(
        _faithful_reencode_arguments(
            preserve_negative_timestamps=(
                audio_filter is not None or preserve_packet_origin
            )
        )
    )
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def build_media_probe_command(
    source: Path,
    *,
    ffprobe: str = _FFPROBE,
) -> list[str]:
    """Build a small structural probe used before trusting a staged artifact."""
    return [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name",
        "-of",
        "json",
        str(source),
    ]


def build_decode_verification_command(
    source: Path,
    *,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Build a streaming full-video decode check with no media output."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-xerror",
        "-err_detect",
        "explode",
        "-max_error_rate",
        "0",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-f",
        "null",
        "-",
    ]


def _faithful_command_prefix(ffmpeg: str, source: Path) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
    ]


def _faithful_reencode_arguments(
    *, preserve_negative_timestamps: bool = False
) -> tuple[str, ...]:
    video_arguments: tuple[str, ...] = (
        "-fflags",
        "+genpts",
        "-avoid_negative_ts",
        "disabled" if preserve_negative_timestamps else "make_zero",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    )
    audio_arguments = (
        "-c:a",
        "aac",
        "-metadata:s:v:0",
        "rotate=0",
    )
    if preserve_negative_timestamps:
        return (*video_arguments, "-bf", "0", *audio_arguments)
    return (*video_arguments, *audio_arguments)


def _preview_command(
    source: Path,
    output: Path,
    start_seconds: float,
    duration_seconds: float,
    *,
    video_filter: str | None = None,
    audio_filter: str | None = None,
) -> list[str]:
    command = [
        _FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-ss",
        _seconds(start_seconds),
        "-t",
        _seconds(duration_seconds),
        "-map",
        "0",
    ]
    if video_filter is not None:
        command.extend(("-vf", video_filter))
    if audio_filter is not None:
        command.extend(("-af", audio_filter))
    command.extend(("-c:v", "libx264", "-c:a", "aac"))
    command.append(str(output))
    return command


def _validate_preview_paths(plan: RescuePlan, source: Path, work_root: Path) -> None:
    """Reject every reserved output which resolves to the read-only source."""
    try:
        resolved_source = _normalized_resolved_path(source)
        outputs = tuple(
            work_root / f"{variant}-{index:02d}.mp4"
            for index, _time_range in enumerate(plan.preview_ranges)
            for variant in ("source", "faithful", "improved")
        )
        resolved_outputs = tuple(
            _normalized_resolved_path(output) for output in outputs
        )
    except OSError as exc:
        raise RescueArtifactError(
            "preview output paths could not be resolved safely"
        ) from exc
    if resolved_source in resolved_outputs:
        raise RescueArtifactError("a private preview output collides with the source")
    if len(resolved_outputs) != len(set(resolved_outputs)):
        raise RescueArtifactError("private preview outputs must use distinct paths")
    source_identity = _existing_file_identity(source)
    if source_identity is not None and any(
        _existing_file_identity(output) == source_identity for output in outputs
    ):
        raise RescueArtifactError("a private preview output aliases the source")


def _normalized_resolved_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def _existing_file_identity(path: Path) -> tuple[int, int] | None:
    """Return an existing file's device/inode identity without following absence."""
    try:
        result = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RescueArtifactError(
            "preview output identity could not be checked"
        ) from exc
    return (result.st_dev, result.st_ino)


def _supports_improved_preview(
    plan: RescuePlan, source_mappings: Sequence[SourceMapping]
) -> bool:
    enhancement_kinds = {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.SHARPEN,
        RescueActionKind.DEFLICKER,
        RescueActionKind.STABILIZE,
        RescueActionKind.NORMALIZE_AUDIO,
        RescueActionKind.DENOISE_AUDIO,
    }
    if plan.strategy is not RescueStrategy.BALANCED:
        return False
    video_filter, audio_filter = _improvement_filters(
        plan, source_mappings=source_mappings
    )
    return bool(video_filter or audio_filter) and any(
        action.kind in enhancement_kinds for action in plan.actions
    )


def _improvement_filters(
    plan: RescuePlan,
    *,
    source_mappings: Sequence[SourceMapping] | None = None,
    source_window: tuple[float, float] | None = None,
    excluded_action_ids: frozenset[str] = frozenset(),
) -> tuple[str | None, str | None]:
    selected_video, selected_audio = _improvement_filter_parts(
        plan,
        source_mappings=source_mappings,
        source_window=source_window,
        excluded_action_ids=excluded_action_ids,
    )
    return (
        ",".join(fragment for _action_id, fragment in selected_video) or None,
        ",".join(fragment for _action_id, fragment in selected_audio) or None,
    )


def previewed_improvement_action_ids(
    plan: RescuePlan,
    source_mappings: Sequence[SourceMapping],
) -> frozenset[str]:
    """Return actions with filters executable on retained preview content."""
    video, audio = _improvement_filter_parts(
        plan,
        source_mappings=source_mappings,
        source_window=None,
    )
    return frozenset(action_id for action_id, _fragment in (*video, *audio))


def _improvement_filter_parts(
    plan: RescuePlan,
    *,
    source_mappings: Sequence[SourceMapping] | None,
    source_window: tuple[float, float] | None,
    excluded_action_ids: frozenset[str] = frozenset(),
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    selected_video: list[tuple[str, str]] = []
    for action in plan.actions:
        if action.id in excluded_action_ids:
            continue
        if action.kind is RescueActionKind.DEFLICKER:
            try:
                correction = flicker_correction_from_parameters(action.parameters)
            except (KeyError, TypeError, ValueError):
                continue
            mappings = _flicker_source_mappings(
                source_mappings=source_mappings,
                source_window=source_window,
            )
            mapped = remap_flicker_correction(
                correction, action.source_ranges, mappings
            )
            if mapped is not None and (fragment := flicker_filter_fragment(mapped)):
                selected_video.append((action.id, fragment))
            continue
        fragment = filter_fragment_from_action(action.kind, action.parameters)
        if fragment is not None:
            output_ranges = _output_ranges(
                action.source_ranges,
                source_mappings=source_mappings,
                source_window=source_window,
            )
            if output_ranges:
                selected_video.append(
                    (
                        action.id,
                        fragment
                        + ":enable='"
                        + _enable_expression(output_ranges)
                        + "'",
                    )
                )
    selected_audio = [
        (action.id, fragment)
        for action in plan.actions
        if action.id not in excluded_action_ids
        if action.kind
        in {
            RescueActionKind.NORMALIZE_AUDIO,
            RescueActionKind.DENOISE_AUDIO,
        }
        for fragment in [
            audio_filter_fragment_from_actions((action.kind,), action.parameters)
        ]
        if fragment is not None
        and _covers_complete_output(
            action.source_ranges,
            source_mappings=source_mappings,
            source_window=source_window,
        )
    ]
    return tuple(selected_video), tuple(selected_audio)


def _flicker_source_mappings(
    *,
    source_mappings: Sequence[SourceMapping] | None,
    source_window: tuple[float, float] | None,
) -> Sequence[SourceMapping]:
    if source_mappings is not None:
        return source_mappings
    if source_window is not None:
        start, end = source_window
        return (SourceMapping(start, end, 0.0, end - start, "preview.mp4"),)
    return ()


def _resolved_improved_source_mappings(
    plan: RescuePlan, source_mappings: Sequence[SourceMapping] | None
) -> Sequence[SourceMapping]:
    if source_mappings is not None:
        return source_mappings
    retained = retained_source_ranges(plan)
    duration = max(
        (
            end
            for action in plan.actions
            if action.kind is RescueActionKind.REMUX
            for _start, end in action.source_ranges
        ),
        default=0.0,
    )
    if retained != ((0.0, duration),):
        raise ValueError("confirmed faithful source mapping is required")
    return mappings_for_ranges(retained, "faithful-rescue.mp4")


def _faithful_preview_filters(
    plan: RescuePlan,
    start_seconds: float,
    end_seconds: float,
    source_mappings: Sequence[SourceMapping],
) -> tuple[str | None, str | None]:
    """Represent confirmed structural operations in the faithful preview."""
    video: list[str] = []
    audio: list[str] = []
    clipped = _removed_window_ranges(source_mappings, (start_seconds, end_seconds))
    if clipped:
        keep = "not(" + _enable_expression(clipped) + ")"
        video.extend((f"select='{keep}'", "setpts=N/FRAME_RATE/TB"))
        audio.extend((f"aselect='{keep}'", "asetpts=N/SR/TB"))
    rotation = next(
        (
            action
            for action in plan.actions
            if action.kind is RescueActionKind.NORMALIZE_ROTATION
        ),
        None,
    )
    if rotation is not None:
        raw_degrees = rotation.parameters.get("rotation_degrees", 0.0)
        degrees = (
            float(raw_degrees)
            if isinstance(raw_degrees, (int, float, str))
            and not isinstance(raw_degrees, bool)
            else 0.0
        ) % 360
        if degrees == 90:
            video.append("transpose=clock")
        elif degrees == 180:
            video.extend(("hflip", "vflip"))
        elif degrees == 270:
            video.append("transpose=cclock")
    fixed_offset = next(
        (
            action
            for action in plan.actions
            if action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
        ),
        None,
    )
    if fixed_offset is not None:
        fragment = audio_filter_fragment_from_actions(
            (fixed_offset.kind,), fixed_offset.parameters
        )
        if fragment:
            audio.append(fragment)
    return ",".join(video) or None, ",".join(audio) or None


def _output_ranges(
    source_ranges: Sequence[tuple[float, float]],
    *,
    source_mappings: Sequence[SourceMapping] | None,
    source_window: tuple[float, float] | None,
) -> tuple[tuple[float, float], ...]:
    if source_mappings is not None:
        output: list[tuple[float, float]] = []
        for source_start, source_end in source_ranges:
            for mapping in source_mappings:
                map_source_start = mapping.source_start
                map_source_end = mapping.source_end
                overlap_start = max(source_start, map_source_start)
                overlap_end = min(source_end, map_source_end)
                if overlap_end <= overlap_start:
                    continue
                source_duration = map_source_end - map_source_start
                output_duration = mapping.output_end - mapping.output_start
                scale = output_duration / source_duration
                output.append(
                    (
                        mapping.output_start
                        + (overlap_start - map_source_start) * scale,
                        mapping.output_start + (overlap_end - map_source_start) * scale,
                    )
                )
        return tuple(output)
    if source_window is not None:
        return _window_relative_ranges(source_ranges, source_window)
    return tuple(source_ranges)


def _window_relative_ranges(
    source_ranges: Sequence[tuple[float, float]],
    window: tuple[float, float],
) -> tuple[tuple[float, float], ...]:
    start, end = window
    return tuple(
        (max(source_start, start) - start, min(source_end, end) - start)
        for source_start, source_end in source_ranges
        if min(source_end, end) > max(source_start, start)
    )


def _removed_window_ranges(
    source_mappings: Sequence[SourceMapping],
    window: tuple[float, float],
) -> tuple[tuple[float, float], ...]:
    start, end = window
    removed: list[tuple[float, float]] = []
    cursor = start
    for mapping in source_mappings:
        if cursor < mapping.source_start:
            removed.append((cursor - start, mapping.source_start - start))
        cursor = max(cursor, mapping.source_end)
    if cursor < end:
        removed.append((cursor - start, end - start))
    return tuple(removed)


def _enable_expression(ranges: Sequence[tuple[float, float]]) -> str:
    predicates = tuple(
        f"gte(t,{_seconds(start)})*lt(t,{_seconds(end)})"
        for start, end in ranges
        if end > start
    )
    return "+".join(predicates) or "0"


def _covers_complete_output(
    source_ranges: Sequence[tuple[float, float]],
    *,
    source_mappings: Sequence[SourceMapping] | None,
    source_window: tuple[float, float] | None,
) -> bool:
    ranges = _output_ranges(
        source_ranges,
        source_mappings=source_mappings,
        source_window=source_window,
    )
    if not ranges:
        return False
    if source_mappings is not None:
        output_end = max(item.output_end for item in source_mappings)
    elif source_window is not None:
        output_end = source_window[1] - source_window[0]
    else:
        return len(ranges) == 1 and ranges[0][0] == 0.0
    merged = sorted(ranges)
    cursor = 0.0
    for start, end in merged:
        if start > cursor + 1e-9:
            return False
        cursor = max(cursor, end)
    return cursor >= output_end - 1e-9


def _seconds(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".") or "0"


__all__ = [
    "build_ffprobe_version_command",
    "build_faithful_concat_command",
    "build_decode_verification_command",
    "build_audio_improvement_command",
    "build_improved_viewing_command",
    "build_faithful_remux_command",
    "build_faithful_segment_command",
    "build_keyframe_probe_command",
    "build_loudnorm_measurement_command",
    "build_media_probe_command",
    "build_packet_timestamp_probe_command",
    "build_preview_commands",
]
