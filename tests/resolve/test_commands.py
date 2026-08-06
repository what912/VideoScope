"""Tests for shell-free Publish Ready FFmpeg argument builders."""

from pathlib import Path

import pytest

from videoscope.domain import VideoMetadata
from videoscope.resolve.commands import (
    build_cover_arguments,
    build_preview_arguments,
    build_publish_arguments,
)
from videoscope.resolve.errors import PublishInputError
from videoscope.resolve.models import PublishPlan, PublishProfileId
from videoscope.resolve.planner import build_publish_plan


def _metadata(
    *,
    duration_seconds: float = 20.0,
    average_frame_rate: float = 30.0,
    has_audio: bool = True,
    compatible: bool = False,
) -> VideoMetadata:
    return VideoMetadata(
        filename="source.mp4",
        container_format="mov,mp4,m4a,3gp,3g2,mj2" if compatible else "matroska",
        codec="h264" if compatible else "vp9",
        width=640,
        height=360,
        duration_seconds=duration_seconds,
        average_frame_rate=average_frame_rate,
        estimated_frame_count=int(duration_seconds * average_frame_rate),
        has_audio=has_audio,
        file_size_bytes=1024,
        raw_probe={
            "pixel_format": "yuv420p" if compatible else "yuv444p",
            "audio_codec": "aac" if compatible and has_audio else "opus",
        },
    )


def _plan(
    profile_id: PublishProfileId = PublishProfileId.SOCIAL_VERTICAL,
    *,
    duration_seconds: float = 20.0,
    average_frame_rate: float = 30.0,
    has_audio: bool = True,
    compatible: bool = False,
) -> PublishPlan:
    return build_publish_plan(
        _metadata(
            duration_seconds=duration_seconds,
            average_frame_rate=average_frame_rate,
            has_audio=has_audio,
            compatible=compatible,
        ),
        "a" * 64,
        profile_id,
    )


def test_publish_arguments_are_exact_shell_free_transcode_array() -> None:
    source = Path("C:/素材 目录/输入；$(不会执行).mp4")
    partial_output = Path("C:/任务 输出/publish-ready.partial.mp4")

    arguments = build_publish_arguments(
        _plan(), source, partial_output, ffmpeg="ffmpeg"
    )

    assert arguments == (
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-vf",
        "scale=w=1080:h=1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-map_metadata",
        "-1",
        "-map_metadata:s",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        str(partial_output),
    )
    assert isinstance(arguments, tuple)
    assert all(isinstance(argument, str) for argument in arguments)
    assert str(source) in arguments
    assert str(partial_output) == arguments[-1]


def test_publish_arguments_remux_compatible_streams_without_filter() -> None:
    plan = _plan(PublishProfileId.COMPATIBLE_MP4, compatible=True)

    arguments = build_publish_arguments(plan, Path("source.mp4"), Path("out.mp4"))

    assert arguments == (
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        "source.mp4",
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
        "out.mp4",
    )
    assert "-vf" not in arguments


def test_publish_arguments_strip_global_stream_and_chapter_metadata() -> None:
    """Source tags and chapters must not survive either publish path."""
    arguments = build_publish_arguments(
        _plan(PublishProfileId.COMPATIBLE_MP4, compatible=True),
        Path("tagged source.mp4"),
        Path("published.mp4"),
    )

    assert arguments[
        arguments.index("-map_metadata") : arguments.index("-movflags")
    ] == (
        "-map_metadata",
        "-1",
        "-map_metadata:s",
        "-1",
        "-map_chapters",
        "-1",
    )


def test_publish_arguments_disable_audio_when_source_has_none() -> None:
    arguments = build_publish_arguments(
        _plan(has_audio=False), Path("source.mp4"), Path("out.mp4")
    )

    assert "0:a:0?" not in arguments
    assert "-c:a" not in arguments
    assert "-an" in arguments


def test_publish_arguments_cap_only_frame_rates_above_sixty() -> None:
    high_fps = build_publish_arguments(
        _plan(average_frame_rate=60.001), Path("source.mp4"), Path("high.mp4")
    )
    sixty_fps = build_publish_arguments(
        _plan(average_frame_rate=60.0), Path("source.mp4"), Path("sixty.mp4")
    )

    assert high_fps[high_fps.index("-vf") + 1].endswith(",fps=60")
    assert not sixty_fps[sixty_fps.index("-vf") + 1].endswith(",fps=60")


def test_preview_arguments_bound_short_and_long_sources_around_midpoint() -> None:
    long_arguments = build_preview_arguments(
        _plan(duration_seconds=20.0),
        Path("输入 视频.mp4"),
        Path("预览 输出.mp4"),
    )
    short_arguments = build_preview_arguments(
        _plan(duration_seconds=4.5), Path("short.mp4"), Path("preview.mp4")
    )

    assert long_arguments[:10] == (
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        "7",
        "-i",
        "输入 视频.mp4",
        "-t",
        "6",
    )
    assert long_arguments[-1] == "预览 输出.mp4"
    assert short_arguments[short_arguments.index("-ss") + 1] == "0"
    assert short_arguments[short_arguments.index("-t") + 1] == "4.5"
    assert "-c:v" in short_arguments
    assert "libx264" in short_arguments


def test_preview_arguments_use_the_effective_configured_duration() -> None:
    """A non-default preview duration must reach the FFmpeg command."""
    arguments = build_preview_arguments(
        _plan(duration_seconds=20.0),
        Path("source.mp4"),
        Path("preview.mp4"),
        preview_seconds=2.5,
    )

    assert arguments[arguments.index("-ss") + 1] == "8.75"
    assert arguments[arguments.index("-t") + 1] == "2.5"


def test_cover_arguments_extract_exactly_one_midpoint_jpeg() -> None:
    arguments = build_cover_arguments(
        Path("成品 视频.mp4"),
        Path("封面 输出.jpg"),
        duration_seconds=9.0,
        ffmpeg="ffmpeg-local",
    )

    assert arguments == (
        "ffmpeg-local",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-ss",
        "4.5",
        "-i",
        "成品 视频.mp4",
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-map_metadata",
        "-1",
        "封面 输出.jpg",
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "duration_seconds", [float("nan"), float("inf")]
)
def test_cover_arguments_reject_non_finite_duration(duration_seconds: float) -> None:
    """Non-finite seek values must never be emitted as FFmpeg arguments."""
    with pytest.raises(PublishInputError, match="finite"):
        build_cover_arguments(
            Path("source.mp4"),
            Path("cover.jpg"),
            duration_seconds=duration_seconds,
        )
