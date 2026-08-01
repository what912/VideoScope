"""Deterministic, bounded frame sampling through local FFmpeg."""

from __future__ import annotations

import math
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from videoscope.video.errors import (
    ExternalToolNotFoundError,
    FrameSamplingError,
    VideoNotFoundError,
    sanitize_diagnostic,
)

DEFAULT_SAMPLE_RATE = 2.0
DEFAULT_MAX_EDGE = 640
DEFAULT_SAMPLING_TIMEOUT_SECONDS = 300.0
ImageFormat = Literal["jpeg", "png"]


class FrameSample(BaseModel):
    """One extracted frame and its deterministic sample time."""

    model_config = ConfigDict(extra="forbid")

    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    sample_index: int = Field(ge=0)
    relative_path: str = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class FrameSamplingResult(BaseModel):
    """Extracted frames plus the caller-owned temporary work directory."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    work_directory: Path
    samples: tuple[FrameSample, ...]


def build_sampling_filter(*, sample_rate: float, max_edge: int) -> str:
    """Build a deterministic constant-rate and bounded-size filter graph."""
    rate_text = format(sample_rate, ".15g")
    scale = (
        f"scale=w='if(gte(iw,ih),min(iw,{max_edge}),-2)':"
        f"h='if(gte(iw,ih),-2,min(ih,{max_edge}))'"
    )
    return f"setpts=PTS-STARTPTS,fps=fps={rate_text}:start_time=0:round=near,{scale}"


def _output_suffix(image_format: ImageFormat) -> str:
    return "jpg" if image_format == "jpeg" else "png"


def sample_frames(
    path: Path,
    *,
    sample_rate: float = DEFAULT_SAMPLE_RATE,
    max_edge: int = DEFAULT_MAX_EDGE,
    image_format: ImageFormat = "jpeg",
    workspace_parent: Path | None = None,
    ffmpeg: str = "ffmpeg",
    timeout_seconds: float = DEFAULT_SAMPLING_TIMEOUT_SECONDS,
) -> FrameSamplingResult:
    """Extract fixed-rate frames into a caller-owned temporary directory."""
    input_path = Path(path)
    if not input_path.is_file():
        raise VideoNotFoundError(f"Input file not found: {input_path.name}")
    if not math.isfinite(sample_rate) or sample_rate <= 0:
        raise ValueError("sample_rate must be a finite value greater than zero")
    if max_edge <= 0:
        raise ValueError("max_edge must be greater than zero")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if image_format not in ("jpeg", "png"):
        raise ValueError("image_format must be 'jpeg' or 'png'")

    parent = Path(workspace_parent) if workspace_parent is not None else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    work_directory = Path(tempfile.mkdtemp(prefix="videoscope-frames-", dir=parent))
    frames_directory = work_directory / "frames"
    frames_directory.mkdir()
    suffix = _output_suffix(image_format)
    output_pattern = frames_directory / f"frame_%06d.{suffix}"
    arguments = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-vf",
        build_sampling_filter(sample_rate=sample_rate, max_edge=max_edge),
        "-an",
        "-threads",
        "1",
        "-start_number",
        "0",
    ]
    if image_format == "jpeg":
        arguments.extend(["-q:v", "2"])
    else:
        arguments.extend(["-compression_level", "6"])
    arguments.append(str(output_pattern))

    try:
        completed = subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise ExternalToolNotFoundError(
            f"Required executable not found: {Path(ffmpeg).name}",
            work_directory=work_directory,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FrameSamplingError(
            f"FFmpeg timed out while sampling: {input_path.name}",
            work_directory=work_directory,
        ) from exc
    except OSError as exc:
        raise FrameSamplingError(
            f"Could not start FFmpeg for: {input_path.name}",
            work_directory=work_directory,
        ) from exc

    if completed.returncode != 0:
        diagnostic = sanitize_diagnostic(
            completed.stderr or completed.stdout,
            sensitive_paths=(input_path, work_directory),
        )
        raise FrameSamplingError(
            f"FFmpeg could not sample frames from: {input_path.name}",
            work_directory=work_directory,
            stderr_summary=diagnostic,
        )

    samples: list[FrameSample] = []
    try:
        for sample_index, frame_path in enumerate(
            sorted(frames_directory.glob(f"frame_*.{suffix}"))
        ):
            with Image.open(frame_path) as image:
                width, height = image.size
            samples.append(
                FrameSample(
                    timestamp_seconds=sample_index / sample_rate,
                    sample_index=sample_index,
                    relative_path=frame_path.relative_to(work_directory).as_posix(),
                    width=width,
                    height=height,
                )
            )
    except (OSError, UnidentifiedImageError) as exc:
        raise FrameSamplingError(
            f"An extracted frame was unreadable for: {input_path.name}",
            work_directory=work_directory,
        ) from exc

    if not samples:
        raise FrameSamplingError(
            f"FFmpeg produced no frames for: {input_path.name}",
            work_directory=work_directory,
            stderr_summary="no output frames",
        )
    return FrameSamplingResult(
        work_directory=work_directory,
        samples=tuple(samples),
    )
