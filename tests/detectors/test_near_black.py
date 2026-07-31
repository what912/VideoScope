"""Unit tests for the near-black CPU detector."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from videoscope.detectors.image_features import compute_luma_metrics
from videoscope.detectors.near_black import (
    NearBlackConfig,
    NearBlackDetector,
)

from .helpers import make_image_context

np: Any = importlib.import_module("numpy")


def test_luma_metrics_include_mean_median_and_dark_ratio() -> None:
    metrics = compute_luma_metrics(
        np.asarray([[0.0, 0.1], [0.2, 0.9]], dtype=np.float64),
        dark_pixel_threshold=0.2,
    )

    assert metrics.mean_luma == pytest.approx(0.3)
    assert metrics.median_luma == pytest.approx(0.15)
    assert metrics.dark_pixel_ratio == pytest.approx(0.75)


def test_near_black_detects_expected_sustained_interval(tmp_path: Path) -> None:
    context = make_image_context(
        tmp_path,
        [80, 90, 100, 110, 0, 0, 0, 120, 130, 140, 150, 160],
    )

    findings = NearBlackDetector().analyze(context, NearBlackConfig())
    repeated_findings = NearBlackDetector().analyze(context, NearBlackConfig())

    assert len(findings) == 1
    assert repeated_findings == findings
    finding = findings[0]
    assert finding.title == "Near-black interval detected"
    assert finding.time_range.start_seconds == pytest.approx(2.0)
    assert finding.time_range.end_seconds == pytest.approx(3.5)
    assert len(finding.evidence) == 3
    assert all(
        not Path(item.relative_path or "").is_absolute() for item in finding.evidence
    )
    assert any("intentional black" in item.lower() for item in finding.limitations)
    assert any("night" in item.lower() for item in finding.limitations)
    assert finding.parameters["mean_luma_threshold"] == 0.08


def test_near_black_empty_short_and_clean_inputs_have_no_finding(
    tmp_path: Path,
) -> None:
    detector = NearBlackDetector()

    assert (
        detector.analyze(make_image_context(tmp_path / "empty", []), NearBlackConfig())
        == []
    )
    short = make_image_context(tmp_path / "short", [0, 0])
    assert detector.analyze(short, NearBlackConfig()) == []
    clean = make_image_context(tmp_path / "clean", [80, 90, 100, 110, 120, 130])
    assert detector.analyze(clean, NearBlackConfig()) == []


def test_near_black_config_rejects_out_of_range_thresholds() -> None:
    with pytest.raises(ValidationError):
        NearBlackConfig(mean_luma_threshold=1.1)
    with pytest.raises(ValidationError):
        NearBlackConfig(min_duration_seconds=0)
