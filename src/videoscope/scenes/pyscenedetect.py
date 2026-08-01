"""PySceneDetect adapter with deterministic fixed-window fallback."""

from __future__ import annotations

import math
from pathlib import Path

from scenedetect import AdaptiveDetector, SceneManager, open_video

from videoscope.scenes.models import (
    SceneDetectionConfig,
    SceneDetectionResult,
)
from videoscope.scenes.normalization import (
    fixed_window_scenes,
    scenes_from_cuts,
)
from videoscope.video import VideoNotFoundError

PYSCENEDETECT_SOURCE = "pyscenedetect.adaptive"
FALLBACK_SOURCE = "fixed-window-fallback"


def _detect_cut_seconds(
    video_path: Path,
    *,
    config: SceneDetectionConfig,
) -> list[float]:
    """Run PySceneDetect and return only vendor-neutral cut times."""
    video = open_video(video_path, backend="opencv")
    scene_manager = SceneManager()
    scene_manager.add_detector(
        AdaptiveDetector(
            adaptive_threshold=config.adaptive_threshold,
            min_scene_len=1,
            window_width=config.window_width,
            min_content_val=config.min_content_value,
        )
    )
    scene_manager.detect_scenes(video=video, show_progress=False)
    vendor_scenes = scene_manager.get_scene_list(start_in_scene=True)
    return [float(start_time.seconds) for start_time, _ in vendor_scenes[1:]]


class PySceneDetectAdapter:
    """Expose AdaptiveDetector results through VideoScope scene models."""

    def __init__(self, config: SceneDetectionConfig | None = None) -> None:
        self.config = config or SceneDetectionConfig()

    def detect(
        self,
        video_path: Path,
        *,
        duration_seconds: float,
    ) -> SceneDetectionResult:
        """Detect scenes, falling back only after a PySceneDetect failure."""
        input_path = Path(video_path)
        if not input_path.is_file():
            raise VideoNotFoundError(f"Input file not found: {input_path.name}")
        if not math.isfinite(duration_seconds) or duration_seconds < 0:
            raise ValueError("duration_seconds must be finite and non-negative")

        try:
            cuts = _detect_cut_seconds(input_path, config=self.config)
        except Exception as exc:
            warning = (
                "PySceneDetect failed "
                f"({type(exc).__name__}); used fixed-window fallback."
            )
            return SceneDetectionResult(
                source=FALLBACK_SOURCE,
                scenes=fixed_window_scenes(
                    duration_seconds=duration_seconds,
                    window_seconds=self.config.fallback_window_seconds,
                ),
                warnings=(warning,),
            )

        scenes = scenes_from_cuts(
            cuts,
            duration_seconds=duration_seconds,
            minimum_duration_seconds=self.config.minimum_scene_duration_seconds,
        )
        return SceneDetectionResult(
            source=PYSCENEDETECT_SOURCE,
            scenes=scenes,
        )
