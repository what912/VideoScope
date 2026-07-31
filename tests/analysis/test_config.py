"""Tests for strict JSON analysis configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from videoscope.analysis import (
    AnalysisConfig,
    AnalysisConfigError,
    load_analysis_config,
)


def test_json_only_rejects_bundled_video() -> None:
    with pytest.raises(ValidationError, match="bundle_video"):
        AnalysisConfig(json_only=True, bundle_video=True)


def test_analysis_config_normalizes_detector_order() -> None:
    config = AnalysisConfig(
        enabled_detectors=("possible_freeze", "near_black", "near_black"),
        detector_configurations={
            "possible_freeze": {"min_duration_seconds": 2.0},
        },
    )

    assert config.enabled_detectors == ("near_black", "possible_freeze")


def test_analysis_config_rejects_invalid_numeric_values() -> None:
    with pytest.raises(ValidationError):
        AnalysisConfig(sample_fps=0)
    with pytest.raises(ValidationError):
        AnalysisConfig(thumbnail_max_size=0)


def test_load_json_config_and_reject_invalid_files(tmp_path: Path) -> None:
    valid = tmp_path / "配置 file.json"
    valid.write_text(
        '{"sample_fps": 1.5, "locale": "zh-CN"}',
        encoding="utf-8",
    )

    config = load_analysis_config(valid)

    assert config.sample_fps == 1.5
    assert config.locale == "zh-CN"

    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"unknown": true}', encoding="utf-8")
    with pytest.raises(AnalysisConfigError, match="Invalid"):
        load_analysis_config(invalid)


def test_cli_overrides_do_not_mutate_original_config(tmp_path: Path) -> None:
    original = AnalysisConfig(enabled_detectors=("near_black", "possible_freeze"))

    updated = original.with_cli_overrides(
        output_directory=tmp_path / "自定义 输出",
        sample_fps=3.0,
        disabled_detectors=("possible_freeze",),
        keep_workspace=True,
    )

    assert original.sample_fps == 2.0
    assert updated.sample_fps == 3.0
    assert updated.enabled_detectors == ("near_black",)
    assert updated.keep_workspace is True
