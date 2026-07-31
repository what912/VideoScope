"""Numeric and interval tests for scene-relative sharpness detection."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter
from pydantic import ValidationError

from videoscope.detectors.scene_relative_blur import (
    SceneRelativeBlurConfig,
    SceneRelativeBlurDetector,
    extract_sharpness_series,
)

from .helpers import FrameInput, make_image_context


def _checkerboard() -> Image.Image:
    image = Image.new("L", (32, 18), color=0)
    draw = ImageDraw.Draw(image)
    for y in range(0, 18, 2):
        for x in range(0, 32, 2):
            if (x // 2 + y // 2) % 2 == 0:
                draw.rectangle((x, y, x + 1, y + 1), fill=255)
    return image.convert("RGB")


def test_scene_relative_blur_reports_metrics_and_manifest_style_interval(
    tmp_path: Path,
) -> None:
    clear = _checkerboard()
    blurred = clear.filter(ImageFilter.GaussianBlur(radius=3.0))
    frames: list[FrameInput] = [clear] * 4 + [blurred] * 4 + [clear] * 4
    context = make_image_context(tmp_path, frames)
    config = SceneRelativeBlurConfig(absolute_floor=0)

    series = extract_sharpness_series(context, config)
    findings = SceneRelativeBlurDetector().analyze(context, config)

    assert series[0].sharpness > series[4].sharpness
    assert series[4].sharpness < (
        series[4].scene_baseline * config.relative_ratio_threshold
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.title == "Relative sharpness drop"
    assert finding.time_range.start_seconds == pytest.approx(2.0)
    assert finding.time_range.end_seconds == pytest.approx(4.0)
    assert len(finding.evidence) == 3
    assert all("sharpness" in item.metadata for item in finding.evidence)
    assert all("scene_baseline" in item.metadata for item in finding.evidence)
    assert "out of focus" in finding.description


def test_absolute_floor_handles_uniformly_soft_scene(tmp_path: Path) -> None:
    context = make_image_context(tmp_path, [128] * 8)
    config = SceneRelativeBlurConfig(
        absolute_floor=1.0,
        min_duration_seconds=1.0,
    )

    series = extract_sharpness_series(context, config)
    findings = SceneRelativeBlurDetector().analyze(context, config)

    assert [sample.sharpness for sample in series] == [0.0] * 8
    assert all(sample.is_anomalous for sample in series)
    assert len(findings) == 1
    assert findings[0].time_range.start_seconds == pytest.approx(0.0)
    assert findings[0].time_range.end_seconds == pytest.approx(4.0)


def test_sharp_clean_sequence_and_short_sequence_have_no_finding(
    tmp_path: Path,
) -> None:
    clear = _checkerboard()
    detector = SceneRelativeBlurDetector()
    config = SceneRelativeBlurConfig(absolute_floor=0)

    clean = make_image_context(tmp_path / "clean", [clear] * 8)
    assert detector.analyze(clean, config) == []
    short = make_image_context(
        tmp_path / "short",
        [clear, clear.filter(ImageFilter.GaussianBlur(radius=3.0)), clear],
    )
    assert detector.analyze(short, config) == []


def test_scene_relative_blur_config_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValidationError):
        SceneRelativeBlurConfig(relative_ratio_threshold=1.0)
    with pytest.raises(ValidationError):
        SceneRelativeBlurConfig(absolute_floor=-1)
