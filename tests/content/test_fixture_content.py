"""Deterministic, local-only useful-content fixture gates."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from scripts import generate_test_videos as factory
from videoscope.content.errors import ContentTranscriptError
from videoscope.content.transcript import load_timed_transcript


def _tools() -> tuple[str, str]:
    ffmpeg = os.environ.get("VIDEOSCOPE_TEST_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("VIDEOSCOPE_TEST_FFPROBE") or shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg and ffprobe are required for native content fixtures")
    assert ffmpeg is not None and ffprobe is not None
    return ffmpeg, ffprobe


def _decoded_hash(ffmpeg: str, path: Path, stream: str) -> str:
    command = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        stream,
        "-f",
        "hash",
        "-hash",
        "sha256",
        "-",
    ]
    completed = subprocess.run(command, check=True, shell=False, capture_output=True)
    return completed.stdout.decode("ascii").strip()


def test_content_manifest_is_path_free_and_complete() -> None:
    content = factory.content_manifest_data()

    assert tuple(sorted(name for name in content if name.endswith(".mp4"))) == (
        "content_join_regression.mp4",
        "content_locked_context.mp4",
        "content_meeting_structure.mp4",
        "content_tutorial_chapters.mp4",
    )
    serialized = json.dumps(content, ensure_ascii=False)
    assert "Users" not in serialized
    assert "下载" not in serialized
    locked = cast(dict[str, object], content["content_locked_context.mp4"])
    assert locked["expected_removed_ranges"] == [
        [4.0, 5.0],
        [7.0, 8.0],
    ]


def test_content_media_decodes_deterministically_across_two_generations(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = _tools()
    first = tmp_path / "first run"
    second = tmp_path / "第二次"
    factory.generate_content_fixtures(
        output_directory=first,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        force=True,
    )
    factory.generate_content_fixtures(
        output_directory=second,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        force=True,
    )

    for spec in factory.content_fixture_specs():
        first_path = first / spec.filename
        second_path = second / spec.filename
        assert _decoded_hash(ffmpeg, first_path, "0:v:0") == _decoded_hash(
            ffmpeg, second_path, "0:v:0"
        )
        assert _decoded_hash(ffmpeg, first_path, "0:a:0") == _decoded_hash(
            ffmpeg, second_path, "0:a:0"
        )


def test_generated_transcripts_cover_valid_unicode_and_invalid_cases(
    tmp_path: Path,
) -> None:
    factory.write_content_transcripts(tmp_path)

    meeting = load_timed_transcript(
        tmp_path / "content_meeting_valid.srt", duration_seconds=12
    )
    tutorial = load_timed_transcript(
        tmp_path / "content_tutorial_zh.vtt", duration_seconds=12
    )
    assert len(meeting.cues) == 2
    assert [cue.text for cue in tutorial.cues] == ["准备材料", "执行操作", "检查结果"]
    for filename in (
        "content_overlap.srt",
        "content_out_of_range.vtt",
        "content_malformed.srt",
    ):
        with pytest.raises(ContentTranscriptError):
            load_timed_transcript(tmp_path / filename, duration_seconds=12)
