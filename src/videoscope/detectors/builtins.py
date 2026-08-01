"""Explicit production registry for built-in detectors."""

from __future__ import annotations

from videoscope.detectors.global_flicker import GlobalFlickerDetector
from videoscope.detectors.interface import Detector
from videoscope.detectors.near_black import NearBlackDetector
from videoscope.detectors.possible_freeze import PossibleFreezeDetector
from videoscope.detectors.prompt_alignment import PromptAlignmentDetector
from videoscope.detectors.registry import DetectorRegistry
from videoscope.detectors.scene_relative_blur import (
    SceneRelativeBlurDetector,
)
from videoscope.detectors.text_stability import TextStabilityDetector
from videoscope.detectors.visual_semantic_drift import (
    VisualSemanticDriftDetector,
)

# Test doubles must never be imported into this module.
BUILTIN_DETECTORS: tuple[Detector, ...] = (
    GlobalFlickerDetector(),
    NearBlackDetector(),
    PossibleFreezeDetector(),
    SceneRelativeBlurDetector(),
)


def create_builtin_detector_registry() -> DetectorRegistry:
    """Create a fresh registry containing production built-in detectors."""
    return DetectorRegistry(BUILTIN_DETECTORS)


def create_ai_detector_registry() -> DetectorRegistry:
    """Create a registry containing CPU and explicitly enabled AI detectors."""
    return create_optional_detector_registry(enable_ai=True, enable_ocr=False)


def create_ocr_detector_registry() -> DetectorRegistry:
    """Create a registry containing CPU and explicitly enabled OCR detectors."""
    return create_optional_detector_registry(enable_ai=False, enable_ocr=True)


def create_optional_detector_registry(
    *,
    enable_ai: bool,
    enable_ocr: bool,
) -> DetectorRegistry:
    """Create a production registry for explicitly enabled optional groups."""
    return DetectorRegistry(
        (
            *BUILTIN_DETECTORS,
            *(
                (PromptAlignmentDetector(), VisualSemanticDriftDetector())
                if enable_ai
                else ()
            ),
            *((TextStabilityDetector(),) if enable_ocr else ()),
        )
    )
