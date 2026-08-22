"""Safe argument-vector builders for private Rescue media processing."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from videoscope.rescue.action_roles import (
    FAITHFUL_RESTORATION_ACTION_KINDS,
    REMAINING_IMPROVEMENT_ACTION_KINDS,
    faithful_restoration_action_ids,
)
from videoscope.rescue.audio import (
    AudioDenoiseConfig,
    AudioNoiseInterval,
    LoudnessConfig,
    audio_filter_fragment_from_actions,
    denoise_filter_fragment,
    loudnorm_measurement_filter,
)
from videoscope.rescue.encoding import canonical_video_encode_arguments
from videoscope.rescue.errors import RescueArtifactError
from videoscope.rescue.models import (
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    validate_plan_video_encode_contracts,
    validate_rescue_plan_identity_contract,
)
from videoscope.rescue.qualification import (
    validate_plan_sharpen_output_range_contracts,
)
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
    luma_action_wire_from_parameters,
    remap_flicker_correction,
)

_FFMPEG = "ffmpeg"
_FFPROBE = "ffprobe"


def build_preview_commands(
    plan: RescuePlan, source: Path, work_root: Path
) -> tuple[list[str], ...]:
    """Return bounded ffmpeg argument vectors without shell interpolation."""
    validate_plan_video_encode_contracts(plan)
    validate_rescue_plan_identity_contract(plan)
    validate_plan_sharpen_output_range_contracts(
        plan,
        mappings_for_ranges(retained_source_ranges(plan), "faithful-rescue.mp4"),
    )
    _validate_preview_paths(plan, source, work_root)
    retained_preview_mappings = tuple(
        mapping
        for index, preview_range in enumerate(plan.preview_ranges)
        for mapping in preview_source_mappings(
            plan, preview_range, f"faithful-{index:02d}.mp4"
        )
    )
    supports_improved_preview = _supports_improved_preview(
        plan, retained_preview_mappings
    )
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
                plan=plan,
            )
        )
        faithful_video, faithful_audio = _faithful_preview_filters(
            plan, start_seconds, end_seconds, faithful_mappings
        )
        restorative_video, restorative_audio = _improvement_filters(
            plan,
            source_mappings=faithful_mappings,
            excluded_action_ids=frozenset(
                action.id
                for action in plan.actions
                if action.kind not in FAITHFUL_RESTORATION_ACTION_KINDS
            ),
        )
        faithful_video = _join_filters(faithful_video, restorative_video)
        faithful_audio = _join_filters(faithful_audio, restorative_audio)
        commands.append(
            _preview_command(
                source,
                faithful_path,
                start_seconds,
                duration_seconds,
                video_filter=faithful_video,
                audio_filter=faithful_audio,
                audio_sample_rate_hz=_plan_audio_sample_rate(plan),
                plan=plan,
            )
        )
        if supports_improved_preview:
            excluded_faithful_action_ids = frozenset(
                action.id
                for action in plan.actions
                if action.kind in FAITHFUL_RESTORATION_ACTION_KINDS
            )
            video_filter, audio_filter = _improvement_filters(
                plan,
                source_mappings=faithful_mappings,
                excluded_action_ids=excluded_faithful_action_ids,
            )
            commands.append(
                _preview_command(
                    faithful_path,
                    work_root / f"improved-{index:02d}.mp4",
                    0.0,
                    faithful_duration,
                    video_filter=video_filter,
                    audio_filter=audio_filter,
                    audio_sample_rate_hz=_plan_audio_sample_rate(plan),
                    plan=plan,
                    video_crf_override=_luma_video_crf_override(
                        plan,
                        faithful_mappings,
                        excluded_faithful_action_ids,
                    ),
                    video_chroma_qp_offset=_luma_chroma_qp_offset(
                        plan,
                        faithful_mappings,
                        excluded_faithful_action_ids,
                    ),
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
    encode_config: RescueEffectiveConfig | None = None,
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
                encode_config or RescueEffectiveConfig(),
                preserve_negative_timestamps=(
                    audio_filter is not None or preserve_packet_origin
                ),
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


def build_audio_noise_measurement_command(
    source: Path,
    output: Path,
    config: AudioDenoiseConfig,
    *,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Decode bounded mono PCM for deterministic local noise analysis."""
    sample_rate = 8000
    samples_per_window = round(sample_rate * config.analysis_window_seconds)
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
        "0:a:0",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-af",
        f"asetnsamples=n={samples_per_window}:p=1",
        "-c:a",
        "pcm_s16le",
        str(output),
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
    audio_sample_rate_hz: int | None = None,
    sharpen_mode: Literal["baseline", "visibility", "candidate"] = "candidate",
    force_video_encode: bool = False,
    _allow_unqualified_sharpen_draft: bool = False,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Build one plan-bound CPU render from the already faithful candidate."""
    validate_plan_video_encode_contracts(
        plan,
        allow_unqualified_sharpen_draft=_allow_unqualified_sharpen_draft,
    )
    validate_rescue_plan_identity_contract(plan)
    resolved_mappings = _resolved_improved_source_mappings(plan, source_mappings)
    validate_plan_sharpen_output_range_contracts(
        plan,
        resolved_mappings,
        allow_unqualified_draft=_allow_unqualified_sharpen_draft,
    )
    video_filter, audio_filter = _improvement_filters(
        plan,
        source_mappings=resolved_mappings,
        excluded_action_ids=excluded_action_ids,
        sharpen_mode=sharpen_mode,
    )
    if not video_filter and not audio_filter and not force_video_encode:
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
    if video_filter or force_video_encode:
        command.extend(("-filter:v:0", video_filter or "null"))
        command.extend(
            canonical_video_encode_arguments(
                plan.effective_config,
                crf_override=_luma_video_crf_override(
                    plan, resolved_mappings, excluded_action_ids
                ),
            )
        )
        chroma_qp_offset = _luma_chroma_qp_offset(
            plan, resolved_mappings, excluded_action_ids
        )
        if chroma_qp_offset is not None:
            command.extend(("-chromaoffset", str(chroma_qp_offset)))
    else:
        command.extend(("-c:v", "copy"))
    if audio_filter:
        if (
            isinstance(audio_sample_rate_hz, bool)
            or not isinstance(audio_sample_rate_hz, int)
            or not 8000 <= audio_sample_rate_hz <= 384000
        ):
            raise ValueError("confirmed audio sample rate is invalid")
        command.extend(
            (
                "-filter:a:0",
                audio_filter,
                "-c:a:0",
                "aac",
                "-b:a:0",
                f"{plan.effective_config.improved_audio_bitrate_kbps}k",
                "-ar:a:0",
                str(audio_sample_rate_hz),
            )
        )
    else:
        command.extend(("-c:a", "copy"))
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def build_sharpen_qualification_command(
    plan: RescuePlan,
    source: Path,
    output: Path,
    *,
    source_ranges: tuple[tuple[float, float], ...],
    parameters: Mapping[str, object],
    mode: Literal["baseline", "visibility", "candidate"],
    source_mappings: Sequence[SourceMapping],
    _allow_unqualified_sharpen_draft: bool = False,
    ffmpeg: str = _FFMPEG,
) -> list[str]:
    """Render one full-range, same-generation private SHARPEN control."""
    validate_plan_video_encode_contracts(plan, allow_unqualified_sharpen_draft=True)
    validate_rescue_plan_identity_contract(plan)
    output_ranges = _output_ranges(
        source_ranges,
        source_mappings=source_mappings,
        source_window=None,
    )
    if not output_ranges or len(output_ranges) != len(source_ranges):
        raise ValueError("SHARPEN qualification ranges are not fully retained")
    if mode == "baseline":
        video_filter = "null"
    else:
        render_parameters = (
            _neutral_sharpen_parameters(parameters)
            if mode == "visibility"
            else parameters
        )
        fragment = filter_fragment_from_action(
            RescueActionKind.SHARPEN,
            render_parameters,
        )
        if fragment is None:
            raise ValueError("SHARPEN qualification parameters are invalid")
        video_filter = _ramped_visibility_fragment(
            fragment,
            render_parameters,
            output_ranges,
        )
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
        "-filter:v:0",
        video_filter,
        *canonical_video_encode_arguments(plan.effective_config),
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
    ]


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
    encode_config: RescueEffectiveConfig | None = None,
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
    command.extend(
        _faithful_reencode_arguments(encode_config or RescueEffectiveConfig())
    )
    command.extend(("-movflags", "+faststart", str(output)))
    return command


def build_faithful_concat_command(
    manifest: Path,
    output: Path,
    *,
    audio_filter: str | None = None,
    preserve_packet_origin: bool = False,
    encode_config: RescueEffectiveConfig | None = None,
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
            encode_config or RescueEffectiveConfig(),
            preserve_negative_timestamps=(
                audio_filter is not None or preserve_packet_origin
            ),
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
        (
            "format=duration:stream=codec_type,codec_name,sample_rate,start_time,"
            "duration,avg_frame_rate,r_frame_rate,nb_frames"
        ),
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
    config: RescueEffectiveConfig,
    *,
    preserve_negative_timestamps: bool = False,
) -> tuple[str, ...]:
    video_arguments = (
        "-fflags",
        "+genpts",
        "-avoid_negative_ts",
        "disabled" if preserve_negative_timestamps else "make_zero",
        *canonical_video_encode_arguments(config),
    )
    audio_arguments = (
        "-c:a",
        "aac",
        "-b:a",
        f"{config.improved_audio_bitrate_kbps}k",
        "-metadata:s:v:0",
        "rotate=0",
    )
    return (*video_arguments, *audio_arguments)


def _preview_command(
    source: Path,
    output: Path,
    start_seconds: float,
    duration_seconds: float,
    *,
    video_filter: str | None = None,
    audio_filter: str | None = None,
    audio_sample_rate_hz: int | None = None,
    plan: RescuePlan | None = None,
    video_crf_override: int | None = None,
    video_chroma_qp_offset: int | None = None,
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
    config = plan.effective_config if plan is not None else None
    encode_config = config or RescueEffectiveConfig()
    command.extend(
        canonical_video_encode_arguments(encode_config, crf_override=video_crf_override)
    )
    if video_chroma_qp_offset is not None:
        command.extend(("-chromaoffset", str(video_chroma_qp_offset)))
    command.extend(
        (
            "-c:a",
            "aac",
            "-b:a:0",
            f"{encode_config.improved_audio_bitrate_kbps}k",
        )
    )
    if (
        audio_filter is not None
        and plan is not None
        and _plan_requires_audio_rate(plan)
    ):
        if audio_sample_rate_hz is None:
            raise ValueError("confirmed audio sample rate is required for preview")
        command.extend(("-ar:a:0", str(audio_sample_rate_hz)))
    command.append(str(output))
    return command


def _luma_video_crf_override(
    plan: RescuePlan,
    source_mappings: Sequence[SourceMapping],
    excluded_action_ids: frozenset[str],
) -> int | None:
    selected = tuple(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.ADJUST_LUMA
        and action.id not in excluded_action_ids
        and _ranges_intersect(
            action.source_ranges, _mapping_source_ranges(source_mappings)
        )
    )
    if not selected:
        return None
    if len(selected) != 1:
        raise ValueError("ADJUST_LUMA action inventory is ambiguous")
    wire = luma_action_wire_from_parameters(selected[0].parameters)
    value = wire.noise_guard_video_crf
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("ADJUST_LUMA video CRF is invalid")
    return value


def _luma_chroma_qp_offset(
    plan: RescuePlan,
    source_mappings: Sequence[SourceMapping],
    excluded_action_ids: frozenset[str],
) -> int | None:
    selected = tuple(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.ADJUST_LUMA
        and action.id not in excluded_action_ids
        and _ranges_intersect(
            action.source_ranges, _mapping_source_ranges(source_mappings)
        )
    )
    if not selected:
        return None
    if len(selected) != 1:
        raise ValueError("ADJUST_LUMA action inventory is ambiguous")
    wire = luma_action_wire_from_parameters(selected[0].parameters)
    value = wire.noise_guard_chroma_qp_offset
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("ADJUST_LUMA chroma QP offset is invalid")
    return value


def _plan_audio_sample_rate(plan: RescuePlan) -> int | None:
    values = {
        value
        for action in plan.actions
        if action.kind
        in {RescueActionKind.NORMALIZE_AUDIO, RescueActionKind.DENOISE_AUDIO}
        for value in (action.parameters.get("output_sample_rate_hz"),)
        if isinstance(value, int) and not isinstance(value, bool)
    }
    return next(iter(values)) if len(values) == 1 else None


def _plan_requires_audio_rate(plan: RescuePlan) -> bool:
    return any(
        action.kind
        in {RescueActionKind.NORMALIZE_AUDIO, RescueActionKind.DENOISE_AUDIO}
        for action in plan.actions
    )


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
    if plan.strategy is not RescueStrategy.BALANCED:
        return False
    mapped_ranges = _mapping_source_ranges(source_mappings)
    relevant_actions = tuple(
        action
        for action in plan.actions
        if action.kind in REMAINING_IMPROVEMENT_ACTION_KINDS
        and _ranges_intersect(action.source_ranges, mapped_ranges)
    )
    retained_ranges = retained_source_ranges(plan)
    return (
        "improved-viewing.mp4" in plan.public_artifacts
        and bool(relevant_actions)
        and all(
            all(
                any(
                    action_start < retained_end and retained_start < action_end
                    for retained_start, retained_end in retained_ranges
                )
                for action_start, action_end in action.source_ranges
            )
            for action in relevant_actions
        )
    )


def _join_filters(first: str | None, second: str | None) -> str | None:
    return ",".join(fragment for fragment in (first, second) if fragment) or None


def _improvement_filters(
    plan: RescuePlan,
    *,
    source_mappings: Sequence[SourceMapping] | None = None,
    source_window: tuple[float, float] | None = None,
    excluded_action_ids: frozenset[str] = frozenset(),
    sharpen_mode: Literal["baseline", "visibility", "candidate"] = "candidate",
) -> tuple[str | None, str | None]:
    selected_video, selected_audio = _improvement_filter_parts(
        plan,
        source_mappings=source_mappings,
        source_window=source_window,
        excluded_action_ids=excluded_action_ids,
        sharpen_mode=sharpen_mode,
    )
    return (
        ",".join(fragment for _action_id, fragment in selected_video) or None,
        ",".join(fragment for _action_id, fragment in selected_audio) or None,
    )


def previewed_improvement_action_ids(
    plan: RescuePlan,
    source_mappings: Sequence[SourceMapping],
    *,
    rendered_native_action_ids: frozenset[str] = frozenset(),
    included_action_ids: frozenset[str] | None = None,
) -> frozenset[str]:
    """Return actions whose filter or native operation actually ran in preview."""
    validate_plan_video_encode_contracts(plan)
    validate_rescue_plan_identity_contract(plan)
    selected_action_ids = (
        frozenset(action.id for action in plan.actions)
        if included_action_ids is None
        else included_action_ids
    )
    video, audio = _improvement_filter_parts(
        plan,
        source_mappings=source_mappings,
        source_window=None,
        excluded_action_ids=frozenset(
            action.id for action in plan.actions if action.id not in selected_action_ids
        ),
    )
    ids = {action_id for action_id, _fragment in (*video, *audio)}
    ids.update(
        action.id
        for action in plan.actions
        if action.id in selected_action_ids
        and action.id in rendered_native_action_ids
        and _ranges_intersect(
            action.source_ranges, _mapping_source_ranges(source_mappings)
        )
    )
    return frozenset(ids)


def _mapping_source_ranges(
    mappings: Sequence[SourceMapping],
) -> tuple[tuple[float, float], ...]:
    return tuple((item.source_start, item.source_end) for item in mappings)


def _ranges_intersect(
    left: Sequence[tuple[float, float]], right: Sequence[tuple[float, float]]
) -> bool:
    return any(
        left_start < right_end and right_start < left_end
        for left_start, left_end in left
        for right_start, right_end in right
    )


def _improvement_filter_parts(
    plan: RescuePlan,
    *,
    source_mappings: Sequence[SourceMapping] | None,
    source_window: tuple[float, float] | None,
    excluded_action_ids: frozenset[str] = frozenset(),
    sharpen_mode: Literal["baseline", "visibility", "candidate"] = "candidate",
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
        render_parameters: Mapping[str, object] = action.parameters
        if action.kind is RescueActionKind.SHARPEN and sharpen_mode == "visibility":
            render_parameters = _neutral_sharpen_parameters(action.parameters)
        fragment = filter_fragment_from_action(action.kind, render_parameters)
        if fragment is not None:
            output_ranges = _output_ranges(
                action.source_ranges,
                source_mappings=source_mappings,
                source_window=source_window,
            )
            if output_ranges:
                expression = _enable_expression(output_ranges)
                if action.kind is RescueActionKind.SHARPEN:
                    if sharpen_mode == "baseline":
                        continue
                    fragment = _ramped_visibility_fragment(
                        fragment, render_parameters, output_ranges
                    )
                selected_video.append(
                    (
                        action.id,
                        fragment
                        if fragment.startswith("eq=brightness='")
                        else ",".join(
                            item + ":enable='" + expression + "'"
                            for item in fragment.split(",")
                        ),
                    )
                )
    selected_audio: list[tuple[str, str]] = []
    for action in plan.actions:
        if action.id in excluded_action_ids or action.kind not in {
            RescueActionKind.NORMALIZE_AUDIO,
            RescueActionKind.DENOISE_AUDIO,
        }:
            continue
        if (
            action.kind is RescueActionKind.DENOISE_AUDIO
            and "interference_profiles" in action.parameters
        ):
            continue
        if action.kind is RescueActionKind.DENOISE_AUDIO and (
            profile_fragments := _audio_noise_profile_fragments(
                action.parameters,
                source_mappings=source_mappings,
                source_window=source_window,
            )
        ):
            selected_audio.extend((action.id, item) for item in profile_fragments)
            continue
        fragment = audio_filter_fragment_from_actions((action.kind,), action.parameters)
        if fragment is None:
            continue
        output_ranges = _output_ranges(
            action.source_ranges,
            source_mappings=source_mappings,
            source_window=source_window,
        )
        if not output_ranges:
            continue
        if not _covers_complete_output(
            action.source_ranges,
            source_mappings=source_mappings,
            source_window=source_window,
        ):
            expression = _enable_expression(output_ranges)
            fragment = ",".join(
                item + ":enable='" + expression + "'" for item in fragment.split(",")
            )
        selected_audio.append((action.id, fragment))
    return tuple(selected_video), tuple(selected_audio)


def _ramped_visibility_fragment(
    fragment: str,
    parameters: Mapping[str, object],
    output_ranges: Sequence[tuple[float, float]],
) -> str:
    """Crossfade a measured visibility lift so range edges do not flash."""
    parts = fragment.split(",")
    if not parts or not parts[0].startswith("eq=brightness="):
        return fragment
    brightness = parameters.get("visibility_brightness")
    transition = parameters.get("boundary_transition_seconds")
    if (
        isinstance(brightness, bool)
        or not isinstance(brightness, (int, float))
        or isinstance(transition, bool)
        or not isinstance(transition, (int, float))
        or not 0.0 < float(brightness) <= 0.25
        or not 0.05 <= float(transition) <= 1.0
    ):
        return fragment
    weights = []
    for start, end in output_ranges:
        duration = end - start
        ramp = min(float(transition), duration / 2.0)
        if ramp <= 0:
            continue
        weights.append(
            "between(t,"
            + _seconds(start)
            + ","
            + _seconds(end)
            + ")*min(1,max(0,(t-"
            + _seconds(start)
            + ")/"
            + _seconds(ramp)
            + "))*min(1,max(0,("
            + _seconds(end)
            + "-t)/"
            + _seconds(ramp)
            + "))"
        )
    if not weights:
        return fragment
    # libavfilter treats commas as filter-chain separators before the eq
    # expression evaluator sees them.  Keep the expression in one filter by
    # escaping every function-argument comma explicitly.
    weight = "+".join(weights).replace(",", r"\,")
    ramped = (
        "eq=brightness='"
        + _seconds(float(brightness))
        + "*("
        + weight
        + ")':contrast='1+0.08*("
        + weight
        + ")':gamma='1+0.2*("
        + weight
        + ")':gamma_weight=0.85:eval=frame"
    )
    expression = _enable_expression(output_ranges)
    bounded_detail = tuple(item + ":enable='" + expression + "'" for item in parts[1:])
    return ",".join((ramped, *bounded_detail))


def _neutral_sharpen_parameters(
    parameters: Mapping[str, object],
) -> dict[str, object]:
    """Keep the selected operator topology while removing its sharpening effect."""
    neutral = dict(parameters)
    neutral["adaptive_strength"] = 0.0
    neutral["amount"] = 0.0
    return neutral


def _audio_noise_profile_fragments(
    parameters: Mapping[str, object],
    *,
    source_mappings: Sequence[SourceMapping] | None,
    source_window: tuple[float, float] | None,
) -> tuple[str, ...]:
    """Build one independently measured filter chain per confirmed noise interval."""
    raw_profiles = parameters.get("noise_profiles")
    if not isinstance(raw_profiles, (list, tuple)):
        return ()
    reduction = parameters.get("maximum_reduction_db")
    if isinstance(reduction, bool) or not isinstance(reduction, (int, float)):
        return ()
    fragments: list[str] = []
    try:
        normalized_profiles: list[dict[str, object]] = []
        for item in raw_profiles:
            if not isinstance(item, Mapping):
                return ()
            normalized = dict(item)
            tones = normalized.get("tone_frequencies_hz")
            if isinstance(tones, (list, tuple)):
                normalized["tone_frequencies_hz"] = tuple(tones)
            normalized_profiles.append(normalized)
        profiles = tuple(
            AudioNoiseInterval.model_validate(item) for item in normalized_profiles
        )
    except (TypeError, ValueError):
        return ()
    for profile in profiles:
        ranges = _output_ranges(
            ((profile.start_seconds, profile.end_seconds),),
            source_mappings=source_mappings,
            source_window=source_window,
        )
        if not ranges:
            continue
        expression = _enable_expression(ranges)
        fragment = denoise_filter_fragment(
            profile.rms_dbfs,
            float(reduction),
            profile.tone_frequencies_hz,
        )
        fragments.append(
            ",".join(
                item + ":enable='" + expression + "'" for item in fragment.split(",")
            )
        )
    return tuple(fragments)


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
    return tuple(mappings_for_ranges(retained, "faithful-rescue.mp4"))


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
    return bool(cursor >= output_end - 1e-9)


def _seconds(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".") or "0"


__all__ = [
    "build_ffprobe_version_command",
    "build_faithful_concat_command",
    "build_decode_verification_command",
    "build_audio_improvement_command",
    "build_audio_noise_measurement_command",
    "build_improved_viewing_command",
    "build_faithful_remux_command",
    "build_faithful_segment_command",
    "build_keyframe_probe_command",
    "build_loudnorm_measurement_command",
    "build_media_probe_command",
    "build_packet_timestamp_probe_command",
    "build_preview_commands",
    "faithful_restoration_action_ids",
]
