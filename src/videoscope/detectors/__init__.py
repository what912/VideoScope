"""Detector plugin protocol, registry, and sequential runner."""

from videoscope.detectors.builtins import (
    BUILTIN_DETECTORS,
    create_ai_detector_registry,
    create_builtin_detector_registry,
    create_ocr_detector_registry,
    create_optional_detector_registry,
)
from videoscope.detectors.global_flicker import (
    GlobalFlickerConfig,
    GlobalFlickerDetector,
)
from videoscope.detectors.interface import Detector
from videoscope.detectors.models import (
    DETECTOR_DIAGNOSTICS_CACHE_KEY,
    AnalysisContext,
    DetectorRequirements,
    DetectorRunResult,
    EstimatedCost,
)
from videoscope.detectors.near_black import NearBlackConfig, NearBlackDetector
from videoscope.detectors.possible_freeze import (
    PossibleFreezeConfig,
    PossibleFreezeDetector,
)
from videoscope.detectors.prompt_alignment import (
    PromptAlignmentConfig,
    PromptAlignmentDetector,
    PromptAlignmentMode,
)
from videoscope.detectors.registry import (
    DetectorRegistrationError,
    DetectorRegistry,
    DuplicateDetectorError,
    UnknownDetectorError,
)
from videoscope.detectors.runner import (
    DetectorConfigInput,
    DetectorRunner,
)
from videoscope.detectors.scene_relative_blur import (
    SceneRelativeBlurConfig,
    SceneRelativeBlurDetector,
)
from videoscope.detectors.text_stability import (
    TextStabilityConfig,
    TextStabilityDetector,
)
from videoscope.detectors.visual_semantic_drift import (
    VisualSemanticDriftConfig,
    VisualSemanticDriftDetector,
)

__all__ = [
    "AnalysisContext",
    "BUILTIN_DETECTORS",
    "DETECTOR_DIAGNOSTICS_CACHE_KEY",
    "Detector",
    "DetectorConfigInput",
    "DetectorRegistrationError",
    "DetectorRegistry",
    "DetectorRequirements",
    "DetectorRunResult",
    "DetectorRunner",
    "DuplicateDetectorError",
    "EstimatedCost",
    "GlobalFlickerConfig",
    "GlobalFlickerDetector",
    "NearBlackConfig",
    "NearBlackDetector",
    "PossibleFreezeConfig",
    "PossibleFreezeDetector",
    "PromptAlignmentConfig",
    "PromptAlignmentDetector",
    "PromptAlignmentMode",
    "SceneRelativeBlurConfig",
    "SceneRelativeBlurDetector",
    "TextStabilityConfig",
    "TextStabilityDetector",
    "UnknownDetectorError",
    "VisualSemanticDriftConfig",
    "VisualSemanticDriftDetector",
    "create_ai_detector_registry",
    "create_builtin_detector_registry",
    "create_ocr_detector_registry",
    "create_optional_detector_registry",
]
