"""Shell-free native command builders for useful-content media."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoscope.content.commands import (
    build_chapter_mux_command,
    build_content_clip_command,
    build_content_concat_command,
    build_content_duration_probe_command,
    build_content_segment_command,
)
from videoscope.content.models import ContentTimeRange


def time_range(start: float, end: float) -> ContentTimeRange:
    return ContentTimeRange(start_seconds=start, end_seconds=end)


def test_segment_command_supports_unicode_paths_and_safe_codecs(tmp_path: Path) -> None:
    source = tmp_path / "输入 video.mp4"
    output = tmp_path / "输出 segment.mp4"
    command = build_content_segment_command(
        source,
        output,
        time_range(1.25, 4.5),
        has_audio=True,
        ffmpeg="ffmpeg.exe",
    )

    assert command[0] == "ffmpeg.exe"
    assert str(source) in command and command[-1] == str(output)
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-c:a") + 1] == "aac"
    assert "-ss" in command and "-t" in command


def test_silent_segment_and_hard_join_do_not_invent_audio(tmp_path: Path) -> None:
    segment = build_content_segment_command(
        tmp_path / "in.mp4",
        tmp_path / "part.mp4",
        time_range(0, 2),
        has_audio=False,
    )
    joined = build_content_concat_command(
        (tmp_path / "a.mp4", tmp_path / "b.mp4"),
        tmp_path / "joined.mp4",
        has_audio=False,
    )

    assert "-an" in segment and "-c:a" not in segment
    assert "concat=n=2:v=1:a=0" in joined[joined.index("-filter_complex") + 1]
    assert "xfade" not in " ".join(joined)


def test_bounded_audio_fade_is_explicit_and_invalid_values_fail(tmp_path: Path) -> None:
    command = build_content_segment_command(
        tmp_path / "in.mp4",
        tmp_path / "out.mp4",
        time_range(0, 3),
        has_audio=True,
        audio_fade_seconds=0.25,
    )
    assert "afade=t=in" in command[command.index("-af") + 1]

    with pytest.raises(ValueError, match="bounded"):
        build_content_segment_command(
            tmp_path / "in.mp4",
            tmp_path / "out.mp4",
            time_range(0, 3),
            has_audio=True,
            audio_fade_seconds=0.75,
        )


def test_selected_clip_chapter_mux_and_probe_are_argument_arrays(
    tmp_path: Path,
) -> None:
    clip = build_content_clip_command(
        tmp_path / "source.mp4",
        tmp_path / "clip.mp4",
        time_range(2, 4),
        has_audio=True,
    )
    chapters = build_chapter_mux_command(
        tmp_path / "joined.mp4",
        tmp_path / "chapters.ffmeta",
        tmp_path / "final.mp4",
    )
    probe = build_content_duration_probe_command(
        tmp_path / "final.mp4", ffprobe="ffprobe.exe"
    )

    assert clip[-1].endswith("clip.mp4")
    assert chapters[chapters.index("-map_chapters") + 1] == "1"
    assert probe[0] == "ffprobe.exe"
    assert not any(
        value in command
        for command in (clip, chapters, probe)
        for value in ("|", "&&", ";")
    )
