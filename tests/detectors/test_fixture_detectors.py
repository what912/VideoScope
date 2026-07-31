"""Conditional integration tests against generated video fixture expectations."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest

from scripts import generate_test_videos as fixture_factory
from videoscope.detectors import AnalysisContext
from videoscope.detectors.global_flicker import (
    GlobalFlickerConfig,
    GlobalFlickerDetector,
)
from videoscope.detectors.near_black import NearBlackConfig, NearBlackDetector
from videoscope.detectors.possible_freeze import (
    PossibleFreezeConfig,
    PossibleFreezeDetector,
)
from videoscope.detectors.scene_relative_blur import (
    SceneRelativeBlurConfig,
    SceneRelativeBlurDetector,
)
from videoscope.domain import Finding, Severity
from videoscope.scenes import PySceneDetectAdapter
from videoscope.video import (
    compute_file_sha256,
    probe_video,
    sample_frames,
)

FIXTURES_DIRECTORY = Path(__file__).parents[1] / "fixtures"
GENERATED_DIRECTORY = FIXTURES_DIRECTORY / "generated"
MANIFEST_PATH = FIXTURES_DIRECTORY / "manifest.json"


def _local_video_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip(
            "FFmpeg and ffprobe are required for generated detector fixture tests"
        )
    assert ffmpeg is not None
    assert ffprobe is not None
    return ffmpeg, ffprobe


def _ensure_fixture(name: str) -> tuple[Path, str, str]:
    ffmpeg, ffprobe = _local_video_tools()
    video_path = GENERATED_DIRECTORY / name
    if not video_path.is_file():
        fixture_factory.generate_fixtures(
            output_directory=GENERATED_DIRECTORY,
            manifest_path=MANIFEST_PATH,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            force=True,
        )
    return video_path, ffmpeg, ffprobe


def _fixture_context(name: str, tmp_path: Path) -> AnalysisContext:
    video_path, ffmpeg, ffprobe = _ensure_fixture(name)
    metadata = probe_video(video_path, ffprobe=ffprobe)
    sampling = sample_frames(
        video_path,
        sample_rate=2.0,
        max_edge=640,
        image_format="png",
        workspace_parent=tmp_path,
        ffmpeg=ffmpeg,
    )
    scenes = (
        PySceneDetectAdapter()
        .detect(
            video_path,
            duration_seconds=metadata.duration_seconds,
        )
        .scenes
    )
    return AnalysisContext(
        input_path=video_path,
        input_hash=compute_file_sha256(video_path),
        metadata=metadata,
        frame_samples=sampling.samples,
        scenes=scenes,
        workspace=sampling.work_directory,
    )


def _manifest_expectation(name: str) -> tuple[float, float, float]:
    document = cast(dict[str, Any], json.loads(MANIFEST_PATH.read_text("utf-8")))
    video = cast(dict[str, Any], document["videos"][name])
    expected = cast(list[dict[str, float]], video["expected_time_ranges"])
    assert len(expected) == 1
    return (
        expected[0]["start_seconds"],
        expected[0]["end_seconds"],
        cast(float, video["tolerance_seconds"]),
    )


def _assert_matches_manifest(name: str, findings: list[Finding]) -> None:
    expected_start, expected_end, tolerance = _manifest_expectation(name)
    assert len(findings) == 1
    assert findings[0].time_range.start_seconds == pytest.approx(
        expected_start,
        abs=tolerance,
    )
    assert findings[0].time_range.end_seconds == pytest.approx(
        expected_end,
        abs=tolerance,
    )


def test_black_fixture_matches_manifest(tmp_path: Path) -> None:
    context = _fixture_context("black_segment.mp4", tmp_path)

    findings = NearBlackDetector().analyze(context, NearBlackConfig())

    _assert_matches_manifest("black_segment.mp4", findings)


def test_freeze_fixture_matches_manifest(tmp_path: Path) -> None:
    context = _fixture_context("freeze_segment.mp4", tmp_path)

    findings = PossibleFreezeDetector().analyze(
        context,
        PossibleFreezeConfig(),
    )

    _assert_matches_manifest("freeze_segment.mp4", findings)


def test_blur_fixture_matches_manifest(tmp_path: Path) -> None:
    context = _fixture_context("blur_segment.mp4", tmp_path)

    findings = SceneRelativeBlurDetector().analyze(
        context,
        SceneRelativeBlurConfig(),
    )

    _assert_matches_manifest("blur_segment.mp4", findings)


def test_flicker_fixture_matches_manifest(tmp_path: Path) -> None:
    context = _fixture_context("flicker_segment.mp4", tmp_path)

    findings = GlobalFlickerDetector().analyze(
        context,
        GlobalFlickerConfig(),
    )

    _assert_matches_manifest("flicker_segment.mp4", findings)


def test_clean_motion_has_no_high_findings(tmp_path: Path) -> None:
    context = _fixture_context("clean_motion.mp4", tmp_path)

    findings = [
        *NearBlackDetector().analyze(context, NearBlackConfig()),
        *PossibleFreezeDetector().analyze(
            context,
            PossibleFreezeConfig(),
        ),
        *SceneRelativeBlurDetector().analyze(
            context,
            SceneRelativeBlurConfig(),
        ),
        *GlobalFlickerDetector().analyze(
            context,
            GlobalFlickerConfig(),
        ),
    ]

    assert not any(finding.severity is Severity.HIGH for finding in findings)


def test_scene_cuts_do_not_form_one_long_freeze(tmp_path: Path) -> None:
    context = _fixture_context("scene_cut.mp4", tmp_path)

    findings = PossibleFreezeDetector().analyze(
        context,
        PossibleFreezeConfig(),
    )

    scene_boundaries = {scene.start_seconds for scene in context.scenes} | {
        scene.end_seconds for scene in context.scenes
    }
    assert all(
        finding.time_range.start_seconds in scene_boundaries
        and finding.time_range.end_seconds in scene_boundaries
        for finding in findings
    )
    assert all(
        finding.time_range.end_seconds - finding.time_range.start_seconds
        <= max(scene.duration_seconds for scene in context.scenes)
        for finding in findings
    )

    flicker_findings = GlobalFlickerDetector().analyze(
        context,
        GlobalFlickerConfig(),
    )
    assert all(
        finding.time_range.end_seconds - finding.time_range.start_seconds
        <= max(scene.duration_seconds for scene in context.scenes)
        for finding in flicker_findings
    )
