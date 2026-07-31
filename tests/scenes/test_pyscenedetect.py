"""Tests for the PySceneDetect adapter and failure fallback."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts import generate_test_videos as fixture_factory
from videoscope.scenes import (
    PySceneDetectAdapter,
    SceneDetectionConfig,
    SceneDetector,
)
from videoscope.video import probe_video


def test_adapter_implements_vendor_neutral_interface(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "中文 场景.mp4"
    input_path.write_bytes(b"video")
    monkeypatch.setattr(
        "videoscope.scenes.pyscenedetect._detect_cut_seconds",
        lambda path, *, config: [2.0, 4.0],
    )
    detector: SceneDetector = PySceneDetectAdapter()

    result = detector.detect(input_path, duration_seconds=6.0)

    assert result.source == "pyscenedetect.adaptive"
    assert result.warnings == ()
    assert [(scene.start_seconds, scene.end_seconds) for scene in result.scenes] == [
        (0.0, 2.0),
        (2.0, 4.0),
        (4.0, 6.0),
    ]


def test_adapter_returns_single_scene_when_no_cut_is_detected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "clean_motion.mp4"
    input_path.write_bytes(b"video")
    monkeypatch.setattr(
        "videoscope.scenes.pyscenedetect._detect_cut_seconds",
        lambda path, *, config: [],
    )

    result = PySceneDetectAdapter().detect(input_path, duration_seconds=6.0)

    assert len(result.scenes) == 1
    assert result.scenes[0].end_seconds == 6.0


def test_adapter_uses_fixed_windows_only_when_pyscenedetect_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "video.mp4"
    input_path.write_bytes(b"video")

    def fail_detection(path: Path, *, config: SceneDetectionConfig) -> list[float]:
        raise RuntimeError(f"private path must not leak: {path}")

    monkeypatch.setattr(
        "videoscope.scenes.pyscenedetect._detect_cut_seconds",
        fail_detection,
    )
    adapter = PySceneDetectAdapter(SceneDetectionConfig(fallback_window_seconds=2.0))

    result = adapter.detect(input_path, duration_seconds=5.0)

    assert result.source == "fixed-window-fallback"
    assert [(scene.start_seconds, scene.end_seconds) for scene in result.scenes] == [
        (0.0, 2.0),
        (2.0, 4.0),
        (4.0, 5.0),
    ]
    assert len(result.warnings) == 1
    assert "RuntimeError" in result.warnings[0]
    assert str(input_path) not in result.warnings[0]


def _generated_fixture(name: str) -> tuple[Path, str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required for real scene fixture tests")
    assert ffmpeg is not None
    assert ffprobe is not None

    fixtures = Path(__file__).parents[1] / "fixtures"
    generated = fixtures / "generated"
    video_path = generated / name
    if not video_path.is_file():
        fixture_factory.generate_fixtures(
            output_directory=generated,
            manifest_path=fixtures / "manifest.json",
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            force=True,
        )
    return video_path, ffmpeg, ffprobe


def test_scene_cut_fixture_detects_multiple_scenes() -> None:
    video_path, _, ffprobe = _generated_fixture("scene_cut.mp4")
    metadata = probe_video(video_path, ffprobe=ffprobe)

    result = PySceneDetectAdapter().detect(
        video_path,
        duration_seconds=metadata.duration_seconds,
    )

    assert result.source == "pyscenedetect.adaptive"
    assert len(result.scenes) >= 3
    assert result.scenes[0].start_seconds == 0.0
    assert result.scenes[-1].end_seconds == pytest.approx(metadata.duration_seconds)


def test_clean_motion_fixture_does_not_produce_many_false_scenes() -> None:
    video_path, _, ffprobe = _generated_fixture("clean_motion.mp4")
    metadata = probe_video(video_path, ffprobe=ffprobe)

    result = PySceneDetectAdapter().detect(
        video_path,
        duration_seconds=metadata.duration_seconds,
    )

    assert result.source == "pyscenedetect.adaptive"
    assert len(result.scenes) <= 2
