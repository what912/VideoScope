"""Every Rescue media child preserves the one pinned source descriptor."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from videoscope.rescue.errors import RescueMediaError, RescueScanError
from videoscope.video.errors import FrameSamplingError

executor_module = importlib.import_module("videoscope.rescue.executor")
preview_module = importlib.import_module("videoscope.rescue.preview")
probe_module = importlib.import_module("videoscope.video.probe")
sampling_module = importlib.import_module("videoscope.video.sampling")
scanner_module = importlib.import_module("videoscope.rescue.scanner")

_PINNED_OPTIONS = {"close_fds": True, "pass_fds": (41,)}


def _install_options(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        module,
        "pinned_subprocess_options",
        lambda _arguments: dict(_PINNED_OPTIONS),
        raising=False,
    )


def _assert_options(kwargs: dict[str, Any]) -> None:
    assert kwargs["close_fds"] is True
    assert kwargs["pass_fds"] == (41,)


def test_probe_child_receives_pinned_source_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    _install_options(probe_module, monkeypatch)

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _assert_options(kwargs)
        payload = {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 16,
                    "height": 16,
                    "avg_frame_rate": "1/1",
                    "duration": "1",
                }
            ],
            "format": {"format_name": "mp4", "duration": "1"},
        }
        return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")

    monkeypatch.setattr(probe_module.subprocess, "run", fake_run)
    probe_module.probe_video(source, ffprobe="ffprobe")


def test_sampling_child_receives_pinned_source_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    _install_options(sampling_module, monkeypatch)

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _assert_options(kwargs)
        return subprocess.CompletedProcess(args, 1, "", "decode failed")

    monkeypatch.setattr(sampling_module.subprocess, "run", fake_run)
    with pytest.raises(FrameSamplingError):
        sampling_module.sample_frames(source, workspace_parent=tmp_path / "work")


def test_preview_child_receives_pinned_source_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_options(preview_module, monkeypatch)

    def fake_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        _assert_options(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(preview_module.subprocess, "run", fake_run)
    preview_module.SubprocessPreviewRunner().run(
        ["ffmpeg", "-i", "/dev/fd/41", "preview.mp4"]
    )


def test_scanner_child_receives_pinned_source_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_options(scanner_module, monkeypatch)

    def fake_popen(args: list[str], **kwargs: Any) -> None:
        _assert_options(kwargs)
        raise FileNotFoundError

    monkeypatch.setattr(scanner_module.subprocess, "Popen", fake_popen)
    with pytest.raises(RescueScanError):
        scanner_module._start_process(["ffprobe", "/dev/fd/41"], bytearray())


def test_executor_child_receives_pinned_source_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_options(executor_module, monkeypatch)

    def fake_popen(args: list[str], **kwargs: Any) -> None:
        _assert_options(kwargs)
        raise FileNotFoundError

    monkeypatch.setattr(executor_module.subprocess, "Popen", fake_popen)
    with pytest.raises(RescueMediaError):
        executor_module.run_external_command(
            ("ffmpeg", "-i", "/dev/fd/41", "output.mp4"),
            timeout_seconds=1,
            sensitive_paths=(),
            cancellation_callback=lambda: False,
        )
