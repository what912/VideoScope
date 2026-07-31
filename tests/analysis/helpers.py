"""Deterministic local doubles for analysis pipeline tests."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from videoscope.domain import VideoMetadata
from videoscope.scenes import SceneDetectionResult, VideoScene
from videoscope.video import FrameSample, FrameSamplingResult


class FixedSceneDetector:
    """Return one complete scene without invoking a media backend."""

    def detect(
        self,
        video_path: Path,
        *,
        duration_seconds: float,
    ) -> SceneDetectionResult:
        del video_path
        return SceneDetectionResult(
            source="test.fixed",
            scenes=(
                VideoScene(
                    scene_index=0,
                    start_seconds=0.0,
                    end_seconds=duration_seconds,
                    duration_seconds=duration_seconds,
                    representative_timestamp=duration_seconds / 2.0,
                ),
            ),
        )


class FakeMedia:
    """Create deterministic extracted frames and record workspace use."""

    def __init__(self) -> None:
        self.workspace_parents: list[Path] = []

    @staticmethod
    def probe(path: Path, *, ffprobe: str) -> VideoMetadata:
        del ffprobe
        return VideoMetadata(
            filename=path.name,
            container_format="mp4",
            codec="test",
            width=32,
            height=18,
            duration_seconds=3.0,
            average_frame_rate=10.0,
            estimated_frame_count=30,
            has_audio=False,
            file_size_bytes=path.stat().st_size,
        )

    def sample(
        self,
        path: Path,
        *,
        sample_rate: float,
        max_edge: int,
        image_format: str,
        workspace_parent: Path,
        ffmpeg: str,
    ) -> FrameSamplingResult:
        del path, max_edge, image_format, ffmpeg
        self.workspace_parents.append(workspace_parent)
        work_directory = workspace_parent / "sample-work"
        frame_directory = work_directory / "frames"
        frame_directory.mkdir(parents=True)
        samples: list[FrameSample] = []
        for index in range(7):
            frame_path = frame_directory / f"frame_{index:06d}.jpg"
            Image.new(
                "RGB",
                (32, 18),
                color=(40 + index * 20,) * 3,
            ).save(frame_path)
            samples.append(
                FrameSample(
                    timestamp_seconds=index / sample_rate,
                    sample_index=index,
                    relative_path=frame_path.relative_to(work_directory).as_posix(),
                    width=32,
                    height=18,
                )
            )
        return FrameSamplingResult(
            work_directory=work_directory,
            samples=tuple(samples),
        )


class TickClock:
    """Return reproducible elapsed values for report determinism tests."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        current = self.value
        self.value += 0.01
        return current
