"""Numeric, fade, and scene-boundary tests for global flicker detection."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from videoscope.detectors.global_flicker import (
    GlobalFlickerConfig,
    GlobalFlickerDetector,
    extract_luminance_series,
)
from videoscope.scenes import VideoScene

from .helpers import make_image_context


def _three_scenes() -> tuple[VideoScene, ...]:
    return tuple(
        VideoScene(
            scene_index=index,
            start_seconds=float(index * 2),
            end_seconds=float((index + 1) * 2),
            duration_seconds=2.0,
            representative_timestamp=float(index * 2 + 1),
        )
        for index in range(3)
    )


def test_global_flicker_reports_residuals_peaks_and_numeric_interval(
    tmp_path: Path,
) -> None:
    context = make_image_context(
        tmp_path,
        [128] * 4 + [20, 235, 20, 235] + [128] * 4,
    )
    config = GlobalFlickerConfig()

    series = extract_luminance_series(context, config)
    findings = GlobalFlickerDetector().analyze(context, config)

    assert series[4].residual < -config.residual_threshold
    assert series[5].residual > config.residual_threshold
    assert len(findings) == 1
    finding = findings[0]
    assert finding.title == "Potential global luminance flicker"
    assert finding.time_range.start_seconds == pytest.approx(2.0)
    assert finding.time_range.end_seconds == pytest.approx(4.0)
    assert len(finding.evidence) == 3
    metadata = finding.evidence[0].metadata
    peak_timestamps = cast(
        list[JsonValue],
        metadata["peak_timestamps_seconds"],
    )
    summary = cast(
        dict[str, JsonValue],
        metadata["luminance_series_summary"],
    )
    assert len(peak_timestamps) == 3
    assert summary["sample_count"] == 4
    assert "Smooth trends" in finding.description


def test_smooth_fade_does_not_form_alternating_cycles(tmp_path: Path) -> None:
    context = make_image_context(
        tmp_path,
        [20, 40, 60, 80, 100, 120, 140, 160, 180, 200, 220, 240],
    )

    findings = GlobalFlickerDetector().analyze(
        context,
        GlobalFlickerConfig(residual_threshold=0.02),
    )

    assert findings == []


def test_scene_cut_guard_excludes_normal_brightness_jumps(
    tmp_path: Path,
) -> None:
    context = make_image_context(
        tmp_path,
        [30] * 4 + [130] * 4 + [230] * 4,
        scenes=_three_scenes(),
    )
    config = GlobalFlickerConfig(
        residual_threshold=0.01,
        scene_boundary_guard_seconds=0.5,
    )

    series = extract_luminance_series(context, config)
    findings = GlobalFlickerDetector().analyze(context, config)

    assert series[4].guarded
    assert series[8].guarded
    assert findings == []


def test_empty_short_and_insufficient_cycles_have_no_finding(
    tmp_path: Path,
) -> None:
    detector = GlobalFlickerDetector()
    config = GlobalFlickerConfig(
        residual_threshold=0.05,
        min_duration_seconds=0.1,
    )

    assert detector.analyze(make_image_context(tmp_path / "empty", []), config) == []
    short = make_image_context(tmp_path / "short", [20, 235, 20])
    assert detector.analyze(short, config) == []


def test_global_flicker_config_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValidationError):
        GlobalFlickerConfig(residual_threshold=0)
    with pytest.raises(ValidationError):
        GlobalFlickerConfig(minimum_cycles=0)
