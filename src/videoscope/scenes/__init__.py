"""Vendor-neutral scene segmentation and PySceneDetect adapter."""

from videoscope.scenes.interface import SceneDetector
from videoscope.scenes.models import (
    SceneDetectionConfig,
    SceneDetectionResult,
    VideoScene,
)
from videoscope.scenes.normalization import (
    fixed_window_scenes,
    scenes_from_cuts,
)
from videoscope.scenes.pyscenedetect import PySceneDetectAdapter

__all__ = [
    "PySceneDetectAdapter",
    "SceneDetectionConfig",
    "SceneDetectionResult",
    "SceneDetector",
    "VideoScene",
    "fixed_window_scenes",
    "scenes_from_cuts",
]
