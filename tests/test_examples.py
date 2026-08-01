"""Release examples remain importable and compatible with public schemas."""

from __future__ import annotations

import runpy
from pathlib import Path

from videoscope.analysis import load_analysis_config
from videoscope.detectors import DetectorRegistry

REPOSITORY = Path(__file__).resolve().parents[1]
EXAMPLES = REPOSITORY / "examples"


def test_json_compatible_yaml_example_loads() -> None:
    config = load_analysis_config(EXAMPLES / "config.example.yaml")

    assert config.sample_fps == 2.0
    assert config.enabled_detectors == (
        "global_flicker",
        "near_black",
        "possible_freeze",
        "scene_relative_blur",
    )


def test_custom_detector_example_satisfies_registry_contract() -> None:
    namespace = runpy.run_path(str(EXAMPLES / "custom_detector.py"))
    detector_type = namespace["CustomObservationDetector"]

    registry = DetectorRegistry([detector_type()])

    assert registry.get("example.custom_observation").version == "1.0.0"


def test_batch_example_imports_without_running_analysis() -> None:
    namespace = runpy.run_path(str(EXAMPLES / "batch_analysis.py"))

    assert callable(namespace["main"])
