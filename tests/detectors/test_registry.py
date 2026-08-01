"""Tests for deterministic detector registration."""

from __future__ import annotations

import pytest

from videoscope.detectors import (
    DetectorRegistry,
    DuplicateDetectorError,
    UnknownDetectorError,
    create_builtin_detector_registry,
)

from .dummy import DummyDetector


def test_registry_registers_builtins_and_lists_by_id() -> None:
    detector_z = DummyDetector("test.z", default_enabled=False)
    detector_a = DummyDetector("test.a")

    registry = DetectorRegistry([detector_z, detector_a])

    assert registry.get("test.a") is detector_a
    assert [detector.id for detector in registry.list_available()] == [
        "test.a",
        "test.z",
    ]
    assert [detector.id for detector in registry.list_default_enabled()] == ["test.a"]


def test_registry_rejects_duplicate_ids() -> None:
    registry = DetectorRegistry([DummyDetector("test.duplicate")])

    with pytest.raises(DuplicateDetectorError, match="already registered"):
        registry.register(DummyDetector("test.duplicate"))


def test_registry_reports_unknown_id() -> None:
    registry = DetectorRegistry()

    with pytest.raises(UnknownDetectorError):
        registry.get("test.missing")


def test_production_builtin_registry_excludes_test_dummy() -> None:
    registry = create_builtin_detector_registry()

    assert [detector.id for detector in registry.list_available()] == [
        "global_flicker",
        "near_black",
        "possible_freeze",
        "scene_relative_blur",
    ]
    assert all(
        not detector.id.startswith("test.") for detector in registry.list_available()
    )
