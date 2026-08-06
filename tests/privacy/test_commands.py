"""Tests for shell-free Safe Sharing FFmpeg argument builders."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoscope.privacy.commands import (
    build_privacy_audio_arguments,
    build_privacy_audio_mute_filter,
    build_privacy_frame_timestamp_arguments,
    build_privacy_preview_arguments,
    build_privacy_rawvideo_decode_arguments,
    build_privacy_rawvideo_encode_arguments,
    build_privacy_remux_arguments,
)
from videoscope.privacy.errors import PrivacyPlanError
from videoscope.privacy.manual import ManualAudioIntervalInput, build_manual_audio_risk
from videoscope.privacy.models import (
    PrivacyEffectiveConfig,
    PrivacyPlan,
    PrivacyRiskMap,
)
from videoscope.privacy.planner import build_privacy_plan
from videoscope.privacy.profiles import get_share_audience_profile


def _audio_plan(*intervals: tuple[float, float]) -> PrivacyPlan:
    risks = tuple(
        build_manual_audio_risk(
            "a" * 64,
            ManualAudioIntervalInput(start_seconds=start, end_seconds=end),
        )
        for start, end in intervals
    )
    return build_privacy_plan(
        risk_map=PrivacyRiskMap(
            input_hash="a" * 64,
            profile="public",
            duration_seconds=10.0,
            risks=risks,
        ),
        reviews=(),
        profile=get_share_audience_profile("public"),
        config=PrivacyEffectiveConfig(preview_seconds=5.0),
    )


def test_preview_command_uses_argument_array_and_exact_duration(tmp_path: Path) -> None:
    source = tmp_path / "中文 source；$(literal).mp4"
    output = tmp_path / "preview output.mp4"

    arguments = build_privacy_preview_arguments(
        plan=_audio_plan((3.0, 4.0)),
        source=source,
        output=output,
        ffmpeg="ffmpeg",
    )

    assert isinstance(arguments, list)
    assert arguments[0] == "ffmpeg"
    assert arguments[arguments.index("-t") + 1] == "5"
    assert str(source) in arguments
    assert arguments[-1] == str(output)
    assert "shell=True" not in arguments


def test_preview_command_strips_metadata_chapters_and_maps_streams() -> None:
    arguments = build_privacy_preview_arguments(
        _audio_plan(),
        Path("source.mp4"),
        Path("preview.mp4"),
        "ffmpeg",
    )

    assert arguments[arguments.index("-map") : arguments.index("-c:v")] == [
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]
    assert ["-map_metadata", "-1"] == arguments[
        arguments.index("-map_metadata") : arguments.index("-map_metadata") + 2
    ]
    assert "-map_metadata:s" in arguments
    assert arguments[arguments.index("-map_chapters") + 1] == "-1"


def test_preview_command_can_mux_redacted_video_with_bounded_source_audio(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source audio.mp4"
    redacted_video = tmp_path / "redacted video.mp4"
    output = tmp_path / "private preview.mp4"

    arguments = build_privacy_preview_arguments(
        _audio_plan((1.0, 2.0)),
        source,
        output,
        "ffmpeg",
        video_source=redacted_video,
    )

    assert arguments.count("-i") == 2
    assert arguments[arguments.index("-i") + 1] == str(redacted_video)
    assert arguments[arguments.index("-i", arguments.index("-i") + 1) + 1] == str(
        source
    )
    assert arguments[arguments.index("-map") : arguments.index("-c:v")] == [
        "-map",
        "0:v:0",
        "-map",
        "1:a:0?",
    ]
    assert arguments[arguments.index("-t") + 1] == "5"


def test_audio_mute_filter_is_time_sorted_and_deterministic() -> None:
    plan = _audio_plan((7.0, 8.0), (1.25, 2.5), (2.5, 3.0))

    first = build_privacy_audio_mute_filter(plan)
    second = build_privacy_audio_mute_filter(plan)

    assert first == second
    assert first == (
        "volume=enable='between(t,1.25,3)':volume=0,"
        "volume=enable='between(t,7,8)':volume=0"
    )


def test_audio_arguments_copy_video_and_apply_only_reviewed_mutes() -> None:
    arguments = build_privacy_audio_arguments(
        _audio_plan((4.0, 5.0), (1.0, 2.0)),
        Path("source.mp4"),
        Path("muted.mp4"),
    )

    assert arguments[arguments.index("-c:v") + 1] == "copy"
    assert arguments[arguments.index("-c:a") + 1] == "aac"
    assert arguments[arguments.index("-af") + 1] == (
        "volume=enable='between(t,1,2)':volume=0,"
        "volume=enable='between(t,4,5)':volume=0"
    )
    assert arguments[arguments.index("-map_metadata") + 1] == "-1"


def test_audio_arguments_can_pair_rendered_video_with_read_only_source_audio() -> None:
    arguments = build_privacy_audio_arguments(
        _audio_plan((1.0, 2.0)),
        Path("visual-redacted.mp4"),
        Path("audio-muted.mp4"),
        audio_source=Path("原始 source.mp4"),
    )

    assert arguments[arguments.index("-i") + 1] == "visual-redacted.mp4"
    second_input = arguments.index("-i", arguments.index("-i") + 1)
    assert arguments[second_input + 1] == "原始 source.mp4"
    assert arguments[arguments.index("-map") + 1] == "0:v:0"
    second_map = arguments.index("-map", arguments.index("-map") + 1)
    assert arguments[second_map + 1] == "1:a:0?"


def test_remux_arguments_are_complete_and_do_not_reencode() -> None:
    arguments = build_privacy_remux_arguments(
        _audio_plan(),
        Path("输入 source.mp4"),
        Path("输出 share-safe.mp4"),
        ffmpeg="ffmpeg-local",
    )

    assert arguments == [
        "ffmpeg-local",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        "输入 source.mp4",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c",
        "copy",
        "-map_metadata",
        "-1",
        "-map_metadata:s",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        "输出 share-safe.mp4",
    ]


def test_command_builders_reject_overwriting_the_source(tmp_path: Path) -> None:
    source = tmp_path / "源 视频.mp4"

    with pytest.raises(PrivacyPlanError) as error:
        build_privacy_preview_arguments(_audio_plan(), source, source)

    assert "source read-only" in (error.value.internal_message or "")


def test_rawvideo_commands_are_shell_free_arrays_for_unicode_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "输入 视频.mp4"
    output = tmp_path / "输出 visual.mp4"

    decoder = build_privacy_rawvideo_decode_arguments(source, ffmpeg="ffmpeg-local")
    encoder = build_privacy_rawvideo_encode_arguments(
        output,
        width=320,
        height=180,
        frame_rate=12.0,
        ffmpeg="ffmpeg-local",
    )

    assert isinstance(decoder, list)
    assert decoder[decoder.index("-i") + 1] == str(source)
    assert decoder[-1] == "pipe:1"
    assert isinstance(encoder, list)
    assert encoder[encoder.index("-s:v") + 1] == "320x180"
    assert encoder[encoder.index("-r") + 1] == "12"
    assert encoder[-1] == str(output)
    assert "-map_metadata" in encoder


def test_rawvideo_decoder_disables_rotation_and_frame_rate_synthesis() -> None:
    arguments = build_privacy_rawvideo_decode_arguments(Path("rotated.mp4"))

    assert arguments.index("-noautorotate") < arguments.index("-i")
    assert arguments[arguments.index("-fps_mode") + 1] == "passthrough"


def test_frame_timestamp_command_streams_only_best_effort_pts() -> None:
    arguments = build_privacy_frame_timestamp_arguments(
        Path("中文 rotated source.mp4"),
        ffprobe="ffprobe-local",
    )

    assert arguments[0] == "ffprobe-local"
    assert arguments[arguments.index("-select_streams") + 1] == "v:0"
    assert arguments[arguments.index("-show_entries") + 1] == (
        "frame=best_effort_timestamp_time"
    )
    assert arguments[-1] == "中文 rotated source.mp4"
