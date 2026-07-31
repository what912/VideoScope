"""Local image-backed contexts for CPU detector tests."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TypeAlias

from PIL import Image

from videoscope.detectors import AnalysisContext
from videoscope.domain import VideoMetadata
from videoscope.scenes import VideoScene
from videoscope.video import FrameSample

FrameInput: TypeAlias = int | tuple[int, int, int] | Image.Image


def make_image_context(
    tmp_path: Path,
    colors: Sequence[FrameInput],
    *,
    duration_seconds: float | None = None,
    scenes: tuple[VideoScene, ...] = (),
) -> AnalysisContext:
    """Create a Unicode/space-path context containing deterministic PNG frames."""
    workspace = tmp_path / "分析 工作区"
    frame_directory = workspace / "证据 帧"
    frame_directory.mkdir(parents=True)
    samples: list[FrameSample] = []
    for position, color in enumerate(colors):
        relative_path = Path("证据 帧") / f"帧_{position:03d}.png"
        if isinstance(color, Image.Image):
            image = color.convert("RGB")
        else:
            image_color = (color, color, color) if isinstance(color, int) else color
            image = Image.new("RGB", (32, 18), color=image_color)
        image.save(workspace / relative_path)
        samples.append(
            FrameSample(
                timestamp_seconds=position / 2.0,
                sample_index=position,
                relative_path=relative_path.as_posix(),
                width=32,
                height=18,
            )
        )

    effective_duration = (
        duration_seconds
        if duration_seconds is not None
        else (len(colors) / 2.0 if colors else 0.0)
    )
    input_path = tmp_path / "输入 视频.mp4"
    input_path.write_bytes(b"test")
    return AnalysisContext(
        input_path=input_path,
        input_hash="a" * 64,
        metadata=VideoMetadata(
            filename=input_path.name,
            container_format="test",
            codec="test",
            width=32,
            height=18,
            duration_seconds=effective_duration,
            average_frame_rate=2.0,
            estimated_frame_count=len(colors),
            has_audio=False,
            file_size_bytes=4,
        ),
        frame_samples=tuple(samples),
        scenes=scenes,
        workspace=workspace,
    )
