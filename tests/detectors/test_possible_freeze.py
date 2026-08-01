"""Unit tests for the possible-freeze CPU detector."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from videoscope.detectors.possible_freeze import (
    PossibleFreezeConfig,
    PossibleFreezeDetector,
)
from videoscope.domain import Severity
from videoscope.scenes import VideoScene

from .helpers import make_image_context


def test_possible_freeze_detects_expected_interval_and_three_frames(
    tmp_path: Path,
) -> None:
    context = make_image_context(
        tmp_path,
        [20, 50, 80, 110, 140, 140, 140, 140, 180, 210, 230, 250],
    )

    findings = PossibleFreezeDetector().analyze(
        context,
        PossibleFreezeConfig(),
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding.title == "Possible frozen or repeated frames"
    assert finding.time_range.start_seconds == pytest.approx(2.0)
    assert finding.time_range.end_seconds == pytest.approx(4.0)
    assert [item.timestamp_seconds for item in finding.evidence] == [2.0, 3.0, 3.5]
    assert all(
        not Path(item.relative_path or "").is_absolute() for item in finding.evidence
    )
    assert any("static shots" in item.lower() for item in finding.limitations)
    assert finding.parameters["max_hash_distance"] == 2


def test_scene_boundaries_reset_similarity_runs(tmp_path: Path) -> None:
    scenes = tuple(
        VideoScene(
            scene_index=index,
            start_seconds=float(index * 2),
            end_seconds=float((index + 1) * 2),
            duration_seconds=2.0,
            representative_timestamp=float(index * 2 + 1),
        )
        for index in range(3)
    )
    context = make_image_context(
        tmp_path,
        [40] * 4 + [120] * 4 + [220] * 4,
        scenes=scenes,
    )

    findings = PossibleFreezeDetector().analyze(
        context,
        PossibleFreezeConfig(),
    )

    assert len(findings) == 3
    assert all(
        finding.time_range.end_seconds - finding.time_range.start_seconds <= 2.0
        for finding in findings
    )
    assert all(
        finding.time_range.end_seconds in {2.0, 4.0, 6.0} for finding in findings
    )


def test_possible_freeze_empty_short_and_motion_inputs(
    tmp_path: Path,
) -> None:
    detector = PossibleFreezeDetector()
    config = PossibleFreezeConfig(min_duration_seconds=0.1)

    assert detector.analyze(make_image_context(tmp_path / "empty", []), config) == []
    short = make_image_context(tmp_path / "short", [100, 100])
    assert detector.analyze(short, config) == []
    motion = make_image_context(
        tmp_path / "motion",
        [20, 50, 80, 110, 140, 170, 200, 230],
    )
    findings = detector.analyze(motion, PossibleFreezeConfig())
    assert not any(finding.severity is Severity.HIGH for finding in findings)
    assert findings == []


def test_possible_freeze_config_rejects_bad_thresholds() -> None:
    with pytest.raises(ValidationError):
        PossibleFreezeConfig(max_pixel_difference=0)
    with pytest.raises(ValidationError):
        PossibleFreezeConfig(max_hash_distance=65)
