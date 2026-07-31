"""Small deterministic image features shared by CPU detectors."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from PIL import Image

from videoscope.video import FrameSample

np: Any = importlib.import_module("numpy")

RGB_RED_WEIGHT = 0.299
RGB_GREEN_WEIGHT = 0.587
RGB_BLUE_WEIGHT = 0.114
PIXEL_VALUE_SCALE = 255.0
PERCEPTUAL_HASH_EDGE = 8

FloatImage: TypeAlias = Any
BoolHash: TypeAlias = Any


@dataclass(frozen=True, slots=True)
class LumaMetrics:
    """Observable brightness metrics for one sampled frame."""

    mean_luma: float
    median_luma: float
    dark_pixel_ratio: float


def resolve_sample_path(workspace: Path, sample: FrameSample) -> Path:
    """Resolve a sample while preventing relative paths escaping the workspace."""
    resolved_workspace = workspace.resolve()
    relative_path = Path(sample.relative_path)
    if relative_path.is_absolute():
        raise ValueError("Frame sample path must be relative to the workspace")
    resolved_path = (resolved_workspace / relative_path).resolve()
    if not resolved_path.is_relative_to(resolved_workspace):
        raise ValueError("Frame sample path escapes the analysis workspace")
    return resolved_path


def load_luma_image(workspace: Path, sample: FrameSample) -> FloatImage:
    """Load one sampled image as normalized BT.601 luma."""
    with Image.open(resolve_sample_path(workspace, sample)) as image:
        rgb = np.asarray(image.convert("RGB"), dtype=np.float64) / PIXEL_VALUE_SCALE
    return (
        rgb[..., 0] * RGB_RED_WEIGHT
        + rgb[..., 1] * RGB_GREEN_WEIGHT
        + rgb[..., 2] * RGB_BLUE_WEIGHT
    )


def compute_luma_metrics(
    luma: FloatImage,
    *,
    dark_pixel_threshold: float,
) -> LumaMetrics:
    """Calculate normalized mean, median, and dark-pixel ratio."""
    if luma.size == 0:
        raise ValueError("luma image must not be empty")
    return LumaMetrics(
        mean_luma=float(np.mean(luma)),
        median_luma=float(np.median(luma)),
        dark_pixel_ratio=float(np.mean(luma <= dark_pixel_threshold)),
    )


def mean_absolute_difference(left: FloatImage, right: FloatImage) -> float:
    """Return normalized grayscale mean absolute difference."""
    if left.shape != right.shape:
        raise ValueError("images must have matching shapes")
    if left.size == 0:
        raise ValueError("images must not be empty")
    return float(np.mean(np.abs(left - right)))


def laplacian_variance(luma: FloatImage) -> float:
    """Return variance of a four-neighbour Laplacian in 8-bit luma units."""
    if luma.ndim != 2 or min(luma.shape) < 3:
        return 0.0
    pixels = luma * PIXEL_VALUE_SCALE
    laplacian = (
        -4.0 * pixels[1:-1, 1:-1]
        + pixels[:-2, 1:-1]
        + pixels[2:, 1:-1]
        + pixels[1:-1, :-2]
        + pixels[1:-1, 2:]
    )
    return float(np.var(laplacian))


def robust_global_luminance(luma: FloatImage) -> float:
    """Return median normalized luma, robust to localized bright objects."""
    if luma.size == 0:
        raise ValueError("luma image must not be empty")
    return float(np.median(luma))


def average_hash(luma: FloatImage) -> BoolHash:
    """Return a compact average hash for low-cost structural comparison."""
    if luma.size == 0:
        raise ValueError("luma image must not be empty")
    pixels = np.clip(luma * PIXEL_VALUE_SCALE, 0, PIXEL_VALUE_SCALE).astype(np.uint8)
    image = Image.fromarray(pixels, mode="L").resize(
        (PERCEPTUAL_HASH_EDGE, PERCEPTUAL_HASH_EDGE),
        resample=Image.Resampling.BILINEAR,
    )
    reduced = np.asarray(image, dtype=np.float64)
    return reduced >= float(np.mean(reduced))


def hash_distance(left: BoolHash, right: BoolHash) -> int:
    """Return Hamming distance between equally sized perceptual hashes."""
    if left.shape != right.shape:
        raise ValueError("hashes must have matching shapes")
    return int(np.count_nonzero(left != right))
