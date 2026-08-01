"""Tests for normalized ffprobe metadata."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from videoscope.video import (
    NoVideoStreamError,
    VideoDecodeError,
    VideoNotFoundError,
    metadata_from_ffprobe,
    parse_frame_rate,
    probe_video,
)


def ffprobe_payload() -> dict[str, object]:
    """Return a representative ffprobe response."""
    return {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 320,
                "height": 180,
                "avg_frame_rate": "30000/1001",
                "nb_frames": "180",
                "duration": "6.006",
                "pix_fmt": "yuv420p",
                "disposition": {"default": 1},
                "tags": {"creation_time": "2026-07-28T10:00:00Z"},
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
            },
        ],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "format_long_name": "QuickTime / MOV",
            "duration": "6.006",
            "filename": "must-not-be-copied.mp4",
            "tags": {"private": "must-not-be-copied"},
        },
    }


def test_parse_frame_rate() -> None:
    cases: tuple[tuple[object, float], ...] = (
        ("30000/1001", 30000 / 1001),
        ("24", 24.0),
        ("0/0", 0.0),
        ("N/A", 0.0),
        (None, 0.0),
    )
    for raw_value, expected in cases:
        assert parse_frame_rate(raw_value) == pytest.approx(expected)


def test_probe_normal_video_and_unicode_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "中文 空格 😀.mp4"
    input_path.write_bytes(b"synthetic-video")
    payload = ffprobe_payload()

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args[-1] == str(input_path)
        assert kwargs["shell"] is False
        assert kwargs["capture_output"] is True
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("videoscope.video.probe.subprocess.run", fake_run)

    metadata = probe_video(input_path, ffprobe="fake-ffprobe")

    assert metadata.filename == input_path.name
    assert metadata.codec == "h264"
    assert metadata.width == 320
    assert metadata.height == 180
    assert metadata.duration_seconds == pytest.approx(6.006)
    assert metadata.average_frame_rate == pytest.approx(30000 / 1001)
    assert metadata.estimated_frame_count == 180
    assert metadata.has_audio is True
    assert metadata.file_size_bytes == len(b"synthetic-video")
    assert metadata.creation_time is not None
    assert "filename" not in metadata.raw_probe
    assert "tags" not in metadata.raw_probe


def test_missing_duration_and_frame_count_are_tolerated(tmp_path: Path) -> None:
    input_path = tmp_path / "missing metadata.mp4"
    input_path.write_bytes(b"video")
    payload = ffprobe_payload()
    video_stream = payload["streams"][0]  # type: ignore[index]
    assert isinstance(video_stream, dict)
    video_stream.pop("duration")
    video_stream.pop("nb_frames")
    media_format = payload["format"]
    assert isinstance(media_format, dict)
    media_format.pop("duration")

    metadata = metadata_from_ffprobe(payload, input_path=input_path)

    assert metadata.duration_seconds == 0.0
    assert metadata.estimated_frame_count == 0


def test_frame_count_is_estimated_when_only_count_is_missing(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "estimated.mp4"
    input_path.write_bytes(b"video")
    payload = ffprobe_payload()
    video_stream = payload["streams"][0]  # type: ignore[index]
    assert isinstance(video_stream, dict)
    video_stream.pop("nb_frames")

    metadata = metadata_from_ffprobe(payload, input_path=input_path)

    assert metadata.estimated_frame_count == round(6.006 * (30000 / 1001))


def test_probe_uses_format_duration_and_r_frame_rate_fallbacks(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "fallbacks.mp4"
    input_path.write_bytes(b"video")
    payload = ffprobe_payload()
    video_stream = payload["streams"][0]  # type: ignore[index]
    assert isinstance(video_stream, dict)
    video_stream["duration"] = "N/A"
    video_stream["avg_frame_rate"] = "0/0"
    video_stream["r_frame_rate"] = "25/1"
    video_stream.pop("nb_frames")

    metadata = metadata_from_ffprobe(payload, input_path=input_path)

    assert metadata.duration_seconds == pytest.approx(6.006)
    assert metadata.average_frame_rate == 25.0
    assert metadata.estimated_frame_count == round(6.006 * 25)


def test_missing_file_has_structured_error(tmp_path: Path) -> None:
    with pytest.raises(VideoNotFoundError) as error:
        probe_video(tmp_path / "不存在.mp4")

    assert error.value.code == "video_not_found"
    assert str(tmp_path) not in str(error.value)


def test_non_video_file_has_sanitized_decode_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "隐私 目录" / "不是视频.txt"
    input_path.parent.mkdir()
    input_path.write_text("not video", encoding="utf-8")

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args,
            returncode=1,
            stdout="",
            stderr=f"{input_path}: Invalid data found when processing input",
        )

    monkeypatch.setattr("videoscope.video.probe.subprocess.run", fake_run)

    with pytest.raises(VideoDecodeError) as error:
        probe_video(input_path)

    assert error.value.code == "video_decode_error"
    assert error.value.stderr_summary is not None
    assert "<input>" in error.value.stderr_summary
    assert str(input_path) not in error.value.stderr_summary
    assert str(input_path.parent) not in str(error.value)


def test_audio_only_media_has_no_video_stream_error(tmp_path: Path) -> None:
    input_path = tmp_path / "audio-only.m4a"
    input_path.write_bytes(b"audio")

    with pytest.raises(NoVideoStreamError) as error:
        metadata_from_ffprobe(
            {
                "streams": [{"codec_type": "audio", "codec_name": "aac"}],
                "format": {"format_name": "mov,mp4"},
            },
            input_path=input_path,
        )

    assert error.value.code == "no_video_stream"
