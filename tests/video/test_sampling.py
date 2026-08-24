"""Tests for deterministic FFmpeg frame sampling."""

from __future__ import annotations

import json
import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from typing import Any

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
from videoscope.video import sampling as sampling_module


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


def test_frame_indices_without_max_samples_rejects_hard_limit_before_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    def unexpected_run(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"subprocess must not start: {args!r}, {kwargs!r}")

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", unexpected_run)

    with pytest.raises(ValueError, match="frame_indices"):
        sample_frames(input_path, frame_indices=tuple(range(1001)))


def test_out_of_range_frame_index_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        _write_mock_frames(Path(args[-1]), count=1)
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="",
            stderr="[Parsed_showinfo_0] n:0 pts:0 pts_time:0.0",
        )

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)

    with pytest.raises(FrameSamplingError, match="timestamps"):
        sample_frames(input_path, frame_indices=(0, 100))


def test_timeline_sampling_fails_closed_when_frame_and_timestamp_counts_differ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")
    image_payload = BytesIO()
    Image.new("RGB", (16, 16), color=(20, 40, 60)).save(image_payload, format="PNG")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        payload = {
            "streams": [{"start_time": "0", "duration": "1"}],
            "format": {"start_time": "0", "duration": "1"},
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO(image_payload.getvalue() * 2)
            self.stderr = BytesIO(
                b"[Parsed_showinfo_2] n:0 pts:0 pts_time:0 "
                b"duration:1 duration_time:0.1\n"
            )
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)
    monkeypatch.setattr(
        "videoscope.video.sampling.subprocess.Popen",
        lambda args, **kwargs: FakeProcess(),
    )

    with pytest.raises(FrameSamplingError, match="sampled-frame stream") as error:
        sample_frames(
            input_path,
            sample_rate=3.0,
            max_samples=2,
            timeline_duration_seconds=1.0,
            motion_sample_rate=10.0,
            maximum_motion_samples=10,
        )

    assert not list(error.value.work_directory.rglob("*.png"))


def test_uncapped_timeline_uses_fixed_rate_targets_in_one_streaming_decode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "two second video.mp4"
    input_path.write_bytes(b"video")
    payloads = BytesIO()
    for color in ((10, 20, 30), (40, 50, 60), (70, 80, 90), (100, 110, 120)):
        Image.new("RGB", (16, 16), color=color).save(payloads, format="PNG")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        payload = {
            "streams": [{"start_time": "0", "duration": "2"}],
            "format": {"start_time": "0", "duration": "2"},
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO(payloads.getvalue())
            self.stderr = BytesIO(
                b"[Parsed_showinfo_2] n:0 pts:0 pts_time:0 "
                b"duration:1 duration_time:0.5\n"
                b"[Parsed_showinfo_2] n:1 pts:1 pts_time:0.5 "
                b"duration:1 duration_time:0.5\n"
                b"[Parsed_showinfo_2] n:2 pts:2 pts_time:1 "
                b"duration:1 duration_time:0.5\n"
                b"[Parsed_showinfo_2] n:3 pts:3 pts_time:1.5 "
                b"duration:1 duration_time:0.5\n"
            )
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)
    monkeypatch.setattr(
        "videoscope.video.sampling.subprocess.Popen",
        lambda args, **kwargs: FakeProcess(),
    )

    result = sample_frames(
        input_path,
        sample_rate=2.0,
        image_format="png",
        max_samples=6,
        timeline_duration_seconds=2.0,
    )

    assert [sample.timestamp_seconds for sample in result.samples] == pytest.approx(
        [0.0, 0.5, 1.0, 1.5], abs=0.11
    )
    assert result.truncated is False
    assert result.decode_passes == 1


def test_timeline_sampling_uses_one_decode_for_independently_bounded_consumers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "dual consumer video.mp4"
    input_path.write_bytes(b"video")
    payloads = BytesIO()
    timing_lines: list[bytes] = []
    for frame_index in range(20):
        Image.new("RGB", (16, 16), color=(frame_index, 20, 40)).save(
            payloads, format="PNG"
        )
        timestamp = frame_index / 10.0
        timing_lines.append(
            f"[Parsed_showinfo_2] n:{frame_index} pts:{frame_index} "
            f"pts_time:{timestamp:g} duration:1 duration_time:0.1\n".encode()
        )

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        payload = {
            "streams": [{"start_time": "0", "duration": "2"}],
            "format": {"start_time": "0", "duration": "2"},
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO(payloads.getvalue())
            self.stderr = BytesIO(b"".join(timing_lines))
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    popen_count = 0

    def recording_popen(args: list[str], **kwargs: object) -> FakeProcess:
        nonlocal popen_count
        del args, kwargs
        popen_count += 1
        return FakeProcess()

    audits: list[sampling_module._TimelineStreamResult] = []
    original_stream = sampling_module._stream_timeline_candidates_unchecked

    def recording_stream(*args: Any, **kwargs: Any) -> Any:
        result = original_stream(*args, **kwargs)
        audits.append(result)
        return result

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)
    monkeypatch.setattr("videoscope.video.sampling.subprocess.Popen", recording_popen)
    monkeypatch.setattr(
        sampling_module, "_stream_timeline_candidates_unchecked", recording_stream
    )

    result = sample_frames(
        input_path,
        sample_rate=10.0,
        image_format="png",
        max_samples=6,
        timeline_duration_seconds=2.0,
        motion_sample_rate=10.0,
        maximum_motion_samples=20,
    )

    assert popen_count == 1
    assert len(result.samples) == 6
    assert len(result.motion_samples) == 20
    assert result.truncated is True
    assert result.motion_truncated is False
    assert audits[0].target_advances == 6
    assert audits[0].motion_target_advances == 20
    assert audits[0].distance_comparisons <= 2 * audits[0].target_advances
    assert audits[0].motion_distance_comparisons <= (
        2 * audits[0].motion_target_advances
    )
    assert audits[0].retained_payload_high_water <= 2


def test_dual_consumer_cancellation_stops_decode_and_removes_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "cancel video.mp4"
    input_path.write_bytes(b"video")
    payload = BytesIO()
    Image.new("RGB", (16, 16), color=(20, 40, 60)).save(payload, format="PNG")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        probe = {
            "streams": [{"start_time": "0", "duration": "1"}],
            "format": {"start_time": "0", "duration": "1"},
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(probe), "")

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO(payload.getvalue())
            self.stderr = BytesIO(
                b"[Parsed_showinfo_2] n:0 pts:0 pts_time:0 duration:1 duration_time:1\n"
            )
            self.returncode: int | None = None
            self.was_stopped = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.was_stopped = True
            self.returncode = 1

        def kill(self) -> None:
            self.was_stopped = True
            self.returncode = 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)
    monkeypatch.setattr(
        "videoscope.video.sampling.subprocess.Popen",
        lambda args, **kwargs: process,
    )

    def cancel() -> None:
        raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        sample_frames(
            input_path,
            sample_rate=2.0,
            max_samples=2,
            timeline_duration_seconds=1.0,
            motion_sample_rate=10.0,
            maximum_motion_samples=10,
            workspace_parent=tmp_path,
            cancellation_check=cancel,
        )

    assert process.was_stopped is True
    assert not list(tmp_path.rglob(".timeline-candidates"))
    assert not list(tmp_path.rglob("*.png"))


def test_capped_timeline_deduplicates_source_frames_and_publishes_contiguous_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "sparse video.mp4"
    input_path.write_bytes(b"video")
    payloads = BytesIO()
    for color in ((10, 20, 30), (40, 50, 60), (70, 80, 90)):
        Image.new("RGB", (16, 16), color=color).save(payloads, format="PNG")

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        payload = {
            "streams": [{"start_time": "0", "duration": "3"}],
            "format": {"start_time": "0", "duration": "3"},
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO(payloads.getvalue())
            self.stderr = BytesIO(
                b"[Parsed_showinfo_2] n:0 pts:0 pts_time:0 "
                b"duration:1 duration_time:1\n"
                b"[Parsed_showinfo_2] n:1 pts:1 pts_time:1 "
                b"duration:1 duration_time:1\n"
                b"[Parsed_showinfo_2] n:2 pts:2 pts_time:2 "
                b"duration:1 duration_time:1\n"
            )
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)
    monkeypatch.setattr(
        "videoscope.video.sampling.subprocess.Popen",
        lambda args, **kwargs: FakeProcess(),
    )

    result = sample_frames(
        input_path,
        sample_rate=10.0,
        image_format="png",
        max_samples=6,
        timeline_duration_seconds=3.0,
    )

    assert result.truncated is True
    assert [sample.timestamp_seconds for sample in result.samples] == [0.0, 1.0, 2.0]
    assert [sample.relative_path for sample in result.samples] == [
        "frames/frame_000000.png",
        "frames/frame_000001.png",
        "frames/frame_000002.png",
    ]
    assert [sample.sample_index for sample in result.samples] == [0, 1, 2]
    published_colors = []
    for sample in result.samples:
        with Image.open(result.work_directory / sample.relative_path) as image:
            published_colors.append(image.getpixel((0, 0)))
    assert published_colors == [(10, 20, 30), (40, 50, 60), (70, 80, 90)]
    assert not (result.work_directory / ".timeline-candidates").exists()


def test_timeline_sampling_fails_closed_when_probe_timing_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "unknown duration.mp4"
    input_path.write_bytes(b"video")
    payload = BytesIO()
    Image.new("RGB", (16, 16), color=(20, 40, 60)).save(payload, format="PNG")
    popen_commands: list[tuple[str, ...]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps({"streams": [{}], "format": {}}),
            "",
        )

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = BytesIO(payload.getvalue())
            self.stderr = BytesIO(
                b"[Parsed_showinfo_2] n:0 pts:0 pts_time:0 duration:1 duration_time:1\n"
            )
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = 1

        def kill(self) -> None:
            self.returncode = 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    def recording_popen(args: list[str], **kwargs: object) -> FakeProcess:
        del kwargs
        popen_commands.append(tuple(args))
        return FakeProcess()

    monkeypatch.setattr("videoscope.video.sampling.subprocess.run", fake_run)
    monkeypatch.setattr("videoscope.video.sampling.subprocess.Popen", recording_popen)

    with pytest.raises(FrameSamplingError, match="duration.*unavailable") as error:
        sample_frames(
            input_path,
            sample_rate=2.0,
            max_samples=1,
            timeline_duration_seconds=1.0,
        )

    assert len(popen_commands) == 1
    assert not list(error.value.work_directory.rglob("*.png"))


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
