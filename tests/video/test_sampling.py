"""Tests for deterministic FFmpeg frame sampling."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from scripts import generate_test_videos as fixture_factory
from videoscope.video import (
    ExternalToolNotFoundError,
    FrameSamplingError,
    build_sampling_filter,
    probe_video,
    sample_frames,
)


def _write_mock_frames(output_pattern: Path, count: int) -> None:
    for sample_index in range(count):
        frame_path = Path(str(output_pattern).replace("%06d", f"{sample_index:06d}"))
        Image.new("RGB", (160, 90), color=(sample_index, 20, 40)).save(frame_path)


def test_sampling_filter_has_rate_and_max_edge() -> None:
    video_filter = build_sampling_filter(sample_rate=2.0, max_edge=640)

    assert "fps=fps=2:start_time=0:round=near" in video_filter
    assert "min(iw,640)" in video_filter
    assert "min(ih,640)" in video_filter


def test_sampling_count_timestamps_and_unicode_paths_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "输入 中文 空格.mp4"
    input_path.write_bytes(b"video")
    workspace_parent = tmp_path / "临时 工作区"

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        assert args[args.index("-i") + 1] == str(input_path)
        _write_mock_frames(Path(args[-1]), count=12)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)

    result = sample_frames(
        input_path,
        sample_rate=2.0,
        max_edge=640,
        workspace_parent=workspace_parent,
        ffmpeg="fake-ffmpeg",
    )

    assert result.work_directory.parent == workspace_parent
    assert result.work_directory.is_dir()
    assert len(result.samples) == 12
    assert [sample.sample_index for sample in result.samples] == list(range(12))
    assert [sample.timestamp_seconds for sample in result.samples] == [
        index / 2.0 for index in range(12)
    ]
    assert result.samples[0].relative_path == "frames/frame_000000.jpg"
    assert {(sample.width, sample.height) for sample in result.samples} == {(160, 90)}


def test_png_sampling_uses_png_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args[-1].endswith(".png")
        assert "-compression_level" in args
        _write_mock_frames(Path(args[-1]), count=1)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)

    result = sample_frames(input_path, image_format="png")

    assert result.samples[0].relative_path.endswith(".png")


def test_ffmpeg_failure_keeps_workspace_and_sanitizes_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "隐私目录" / "broken.mp4"
    input_path.parent.mkdir()
    input_path.write_bytes(b"broken")

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        output_pattern = Path(args[-1])
        return subprocess.CompletedProcess(
            args,
            1,
            stdout="",
            stderr=(f"Cannot decode {input_path}; output was {output_pattern.parent}"),
        )

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)

    with pytest.raises(FrameSamplingError) as error:
        sample_frames(input_path, workspace_parent=tmp_path / "工作")

    assert error.value.work_directory.is_dir()
    assert error.value.stderr_summary is not None
    assert "<input>" in error.value.stderr_summary
    assert str(input_path) not in error.value.stderr_summary
    assert str(error.value.work_directory) not in error.value.stderr_summary


def test_missing_ffmpeg_keeps_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(args[0])

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)

    with pytest.raises(ExternalToolNotFoundError) as error:
        sample_frames(input_path, workspace_parent=tmp_path / "work")

    assert error.value.code == "external_tool_not_found"
    assert error.value.work_directory is not None
    assert error.value.work_directory.is_dir()


def test_real_clean_motion_probe_and_sampling_when_available(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip(
            "FFmpeg and ffprobe are required for the video I/O integration test"
        )
    assert ffmpeg is not None
    assert ffprobe is not None

    generated_directory = Path(__file__).parents[1] / "fixtures" / "generated"
    clean_motion = generated_directory / "clean_motion.mp4"
    if not clean_motion.is_file():
        fixture_factory.generate_fixtures(
            output_directory=generated_directory,
            manifest_path=Path(__file__).parents[1] / "fixtures" / "manifest.json",
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            force=True,
        )

    metadata = probe_video(clean_motion, ffprobe=ffprobe)
    sampling = sample_frames(
        clean_motion,
        sample_rate=2.0,
        max_edge=640,
        workspace_parent=tmp_path,
        ffmpeg=ffmpeg,
    )

    assert metadata.codec == "mpeg4"
    assert metadata.duration_seconds == pytest.approx(6.0, abs=0.11)
    assert metadata.width == 320
    assert metadata.height == 180
    assert metadata.average_frame_rate == pytest.approx(10.0)
    assert len(sampling.samples) == 12
