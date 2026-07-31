"""Public scene detector contract independent of PySceneDetect."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from videoscope.scenes.models import SceneDetectionResult


class SceneDetector(Protocol):
    """Detect scene context without exposing vendor-specific types."""

    def detect(
        self,
        video_path: Path,
        *,
        duration_seconds: float,
    ) -> SceneDetectionResult:
        """Return continuous scenes covering the supplied video duration."""
        ...
