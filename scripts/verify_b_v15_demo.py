"""Independently verify the private Video Rescue V15 demo artifacts."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Protocol

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, model_validator

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DemoContract(_StrictModel):
    """Explicit engineering-only contract for the approved 42-second demo."""

    schema_version: Literal["1"] = "1"
    expected_duration_seconds: float = Field(default=42.0, gt=0, allow_inf_nan=False)
    expected_width: int = Field(default=1280, gt=0)
    expected_height: int = Field(default=720, gt=0)
    expected_frame_rate: float = Field(default=24.0, gt=0, allow_inf_nan=False)
    expected_sample_rate: int = Field(default=48_000, gt=0)
    expected_pts_origin_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    duration_tolerance_seconds: float = Field(default=0.08, ge=0, allow_inf_nan=False)
    timestamp_tolerance_seconds: float = Field(default=0.002, gt=0, allow_inf_nan=False)
    clarity_timestamp_seconds: float = Field(default=6.0, ge=0, allow_inf_nan=False)
    audio_start_seconds: float = Field(default=31.8, ge=0, allow_inf_nan=False)
    audio_end_seconds: float = Field(default=32.2, gt=0, allow_inf_nan=False)
    audio_window_seconds: float = Field(default=0.05, gt=0, allow_inf_nan=False)
    persistent_frequency_hz: float = Field(default=220.0, gt=0, allow_inf_nan=False)
    interference_frequency_hz: float = Field(default=880.0, gt=0, allow_inf_nan=False)
    minimum_tonal_attenuation_db: float = Field(
        default=24.0, ge=24.0, allow_inf_nan=False
    )
    motion_start_seconds: float = Field(default=32.0, ge=0, allow_inf_nan=False)
    motion_end_seconds: float = Field(default=36.0, gt=0, allow_inf_nan=False)
    maximum_motion_median_pixels: float = Field(
        default=0.5, ge=0, le=0.5, allow_inf_nan=False
    )
    maximum_motion_p90_pixels: float = Field(
        default=1.0, ge=0, le=1.0, allow_inf_nan=False
    )
    maximum_motion_crop_ratio: float = Field(
        default=0.08, ge=0, le=0.12, allow_inf_nan=False
    )
    outside_analysis_width: int = Field(default=160, ge=64, le=640)
    motion_analysis_width: int = Field(default=320, ge=160, le=640)
    maximum_outside_visual_mae: float = Field(
        default=0.02, ge=0, le=0.05, allow_inf_nan=False
    )
    maximum_outside_audio_mae: float = Field(
        default=0.02, ge=0, le=0.05, allow_inf_nan=False
    )
    maximum_edge_spread_source_ratio: float = Field(
        default=0.85, gt=0, le=1, allow_inf_nan=False
    )
    maximum_edge_spread_v14_ratio: float = Field(
        default=0.95, gt=0, le=1, allow_inf_nan=False
    )
    minimum_edge_continuity_source_delta: float = Field(
        default=0.02, ge=0, le=1, allow_inf_nan=False
    )
    minimum_edge_continuity_v14_delta: float = Field(
        default=0.01, ge=0, le=1, allow_inf_nan=False
    )
    minimum_structural_similarity_delta: float = Field(
        default=0.02, ge=0, le=1, allow_inf_nan=False
    )
    maximum_structure_error_source_ratio: float = Field(
        default=0.75, gt=0, le=1, allow_inf_nan=False
    )
    maximum_structure_error_v14_ratio: float = Field(
        default=0.9, gt=0, le=1, allow_inf_nan=False
    )
    maximum_ringing_noise_ratio: float = Field(default=0.35, ge=0, allow_inf_nan=False)
    maximum_temporal_residual: float = Field(
        default=0.05, ge=0, le=1, allow_inf_nan=False
    )
    clarity_roi_rows: int = Field(default=3, ge=1, le=8)
    clarity_roi_columns: int = Field(default=4, ge=1, le=8)
    minimum_clarity_roi_edge_pixels: int = Field(default=32, ge=32)
    target_evidence_margin_db: float = Field(default=12.0, ge=0, allow_inf_nan=False)
    maximum_persistent_tone_difference_db: float = Field(
        default=1.0, ge=0, allow_inf_nan=False
    )
    maximum_non_target_difference_db: float = Field(
        default=1.5, ge=0, allow_inf_nan=False
    )
    maximum_boundary_sample_jump_excess: float = Field(
        default=0.05, ge=0, le=2, allow_inf_nan=False
    )
    maximum_boundary_rms_increase_db: float = Field(
        default=1.5, ge=0, allow_inf_nan=False
    )
    maximum_boundary_crest_excess: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    minimum_registration_response: float = Field(
        default=0.05, ge=0, le=1, allow_inf_nan=False
    )
    target_intervals: tuple[tuple[float, float], ...] = (
        (0.0, 10.0),
        (25.0, 36.0),
    )

    @model_validator(mode="after")
    def validate_ranges(self) -> DemoContract:
        if self.maximum_edge_spread_source_ratio > self.maximum_edge_spread_v14_ratio:
            raise ValueError(
                "source edge-spread ratio must be at least as strict as V14"
            )
        if (
            self.minimum_edge_continuity_source_delta
            < self.minimum_edge_continuity_v14_delta
        ):
            raise ValueError(
                "source continuity delta must be at least as strict as V14"
            )
        if (
            self.maximum_structure_error_source_ratio
            > self.maximum_structure_error_v14_ratio
        ):
            raise ValueError("source structure ratio must be at least as strict as V14")
        if self.maximum_motion_median_pixels > self.maximum_motion_p90_pixels:
            raise ValueError("motion median bound cannot exceed the P90 bound")
        if self.audio_end_seconds <= self.audio_start_seconds:
            raise ValueError("audio verification range must be forward")
        if self.motion_end_seconds <= self.motion_start_seconds:
            raise ValueError("motion verification range must be forward")
        if self.clarity_timestamp_seconds >= self.expected_duration_seconds:
            raise ValueError("clarity timestamp must be inside the media")
        previous_end = 0.0
        for start, end in self.target_intervals:
            if not all(math.isfinite(value) for value in (start, end)):
                raise ValueError("target intervals must be finite")
            if start < previous_end or end <= start:
                raise ValueError("target intervals must be ordered and disjoint")
            if end > self.expected_duration_seconds:
                raise ValueError("target interval exceeds the media duration")
            previous_end = end
        return self


class MediaInfo(_StrictModel):
    label: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    video_duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    audio_duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_rate: float = Field(gt=0, allow_inf_nan=False)
    frame_count: int = Field(gt=0)
    sample_rate: int = Field(gt=0)
    frame_timestamps: tuple[float, ...]

    @model_validator(mode="after")
    def validate_timestamps(self) -> MediaInfo:
        if len(self.frame_timestamps) != self.frame_count:
            raise ValueError("actual frame count does not match timestamps")
        previous = -math.inf
        for timestamp in self.frame_timestamps:
            if not math.isfinite(timestamp) or timestamp < 0 or timestamp <= previous:
                raise ValueError(
                    "actual frame timestamps must be finite and increasing"
                )
            previous = timestamp
        return self


@dataclass(frozen=True, slots=True)
class VerificationBundle:
    metrics: dict[str, object]
    audio: dict[str, object]
    motion: dict[str, object]
    contact_sheet_png: bytes


@dataclass(frozen=True, slots=True)
class DemoVerificationResult:
    status: Literal["passed", "needs_review"]
    output: Path


@dataclass(frozen=True, slots=True)
class TransitionFrameTransform:
    timestamp_seconds: float
    rotation_degrees: float
    scale: float
    translation_x: float
    translation_y: float


@dataclass(frozen=True, slots=True)
class TransitionPlanBinding:
    """Path-free expected transition corrections from one validated plan."""

    plan_sha256: str
    plan_digest: str
    action_id: str
    frame_width: int
    frame_height: int
    safe_crop_ratio: float
    transforms: tuple[TransitionFrameTransform, ...]


class DemoVerificationError(RuntimeError):
    """The private demo verification cannot be completed safely."""


class DemoVerificationCancelled(DemoVerificationError):
    """The private demo verification was cancelled cooperatively."""


def _raise_if_cancelled(callback: Callable[[], bool]) -> None:
    if callback():
        raise DemoVerificationCancelled("verification was cancelled")


class CommandRunner(Protocol):
    def __call__(
        self,
        arguments: list[str],
        *,
        timeout_seconds: float = 120.0,
        maximum_output_bytes: int = 64 * 1024 * 1024,
    ) -> bytes: ...


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise DemoVerificationError(f"{label} is invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise DemoVerificationError(f"{label} is invalid") from error
    if not math.isfinite(number):
        raise DemoVerificationError(f"{label} is invalid")
    return number


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise DemoVerificationError(f"{label} is invalid")
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise DemoVerificationError(f"{label} is invalid") from error
    if number <= 0:
        raise DemoVerificationError(f"{label} is invalid")
    return number


def _frame_rate(value: object) -> float:
    if not isinstance(value, str) or "/" not in value:
        return _finite_float(value, "frame rate")
    numerator, denominator = value.split("/", maxsplit=1)
    top = _finite_float(numerator, "frame rate")
    bottom = _finite_float(denominator, "frame rate")
    if bottom == 0:
        raise DemoVerificationError("frame rate is invalid")
    return top / bottom


def _parse_media_probe(
    *,
    label: str,
    sha256: str,
    probe_bytes: bytes,
    frame_bytes: bytes,
) -> MediaInfo:
    try:
        probe = json.loads(probe_bytes)
        frame_document = json.loads(frame_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DemoVerificationError(f"{label} probe output is invalid") from error
    if not isinstance(probe, dict) or not isinstance(frame_document, dict):
        raise DemoVerificationError(f"{label} probe output is invalid")
    streams = probe.get("streams")
    raw_frames = frame_document.get("frames")
    raw_format = probe.get("format")
    if (
        not isinstance(streams, list)
        or not isinstance(raw_frames, list)
        or not isinstance(raw_format, dict)
    ):
        raise DemoVerificationError(f"{label} probe output is incomplete")
    video_streams = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    audio_streams = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    if len(video_streams) != 1 or len(audio_streams) != 1:
        raise DemoVerificationError(
            f"{label} requires exactly one video and audio stream"
        )
    video = video_streams[0]
    audio = audio_streams[0]
    timestamps: list[float] = []
    for raw_frame in raw_frames:
        if not isinstance(raw_frame, dict):
            raise DemoVerificationError(f"{label} frame inventory is invalid")
        timestamps.append(
            _finite_float(
                raw_frame.get("best_effort_timestamp_time"),
                f"{label} frame timestamp",
            )
        )
    try:
        return MediaInfo(
            label=label,
            sha256=sha256,
            duration_seconds=_finite_float(raw_format.get("duration"), "duration"),
            video_duration_seconds=_finite_float(video.get("duration"), "duration"),
            audio_duration_seconds=_finite_float(audio.get("duration"), "duration"),
            width=_positive_int(video.get("width"), "width"),
            height=_positive_int(video.get("height"), "height"),
            frame_rate=_frame_rate(video.get("avg_frame_rate")),
            frame_count=len(timestamps),
            sample_rate=_positive_int(audio.get("sample_rate"), "sample rate"),
            frame_timestamps=tuple(timestamps),
        )
    except ValueError as error:
        raise DemoVerificationError(f"{label} media contract is invalid") from error


def _validate_media_set(media: Sequence[MediaInfo], contract: DemoContract) -> None:
    if len(media) != 4:
        raise DemoVerificationError("four media inputs are required")
    reference = media[0]
    expected_frames = round(
        contract.expected_duration_seconds * contract.expected_frame_rate
    )
    for item in media:
        if not _SHA256.fullmatch(item.sha256):
            raise DemoVerificationError("media hash is invalid")
        if abs(item.duration_seconds - contract.expected_duration_seconds) > (
            contract.duration_tolerance_seconds
        ):
            raise DemoVerificationError(
                "media duration does not match the demo contract"
            )
        if (
            item.width != contract.expected_width
            or item.height != contract.expected_height
            or not math.isclose(
                item.frame_rate,
                contract.expected_frame_rate,
                rel_tol=0.0,
                abs_tol=1e-6,
            )
            or item.frame_count != expected_frames
        ):
            raise DemoVerificationError("video stream does not match the demo contract")
        if item.sample_rate != contract.expected_sample_rate:
            raise DemoVerificationError(
                "audio sample rate does not match the demo contract"
            )
        if (
            max(
                abs(item.video_duration_seconds - item.audio_duration_seconds),
                abs(item.duration_seconds - item.video_duration_seconds),
            )
            > contract.duration_tolerance_seconds
        ):
            raise DemoVerificationError(
                "audio/video duration drift exceeds the contract"
            )
        expected_timestamps = tuple(
            contract.expected_pts_origin_seconds + index / contract.expected_frame_rate
            for index in range(expected_frames)
        )
        if (
            any(
                abs(actual - expected) > contract.timestamp_tolerance_seconds
                for actual, expected in zip(
                    item.frame_timestamps, expected_timestamps, strict=True
                )
            )
            or abs(
                item.frame_timestamps[-1]
                + 1.0 / contract.expected_frame_rate
                - (
                    contract.expected_pts_origin_seconds
                    + contract.expected_duration_seconds
                )
            )
            > contract.timestamp_tolerance_seconds
        ):
            raise DemoVerificationError(
                "actual frame timestamps do not match the declared CFR timeline"
            )
        if len(item.frame_timestamps) != len(reference.frame_timestamps) or any(
            abs(actual - expected) > contract.timestamp_tolerance_seconds
            for actual, expected in zip(
                item.frame_timestamps, reference.frame_timestamps, strict=True
            )
        ):
            raise DemoVerificationError("actual frame timestamps do not match")


def _gray(frame: NDArray[np.uint8]) -> NDArray[np.float32]:
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise DemoVerificationError("decoded frame has an invalid shape")
    return cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0


def _gradient(gray: NDArray[np.float32]) -> NDArray[np.float32]:
    x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return np.sqrt(x * x + y * y)


def _ssim(first: NDArray[np.float32], second: NDArray[np.float32]) -> float:
    c1 = 0.01**2
    c2 = 0.03**2
    mu_first = cv2.GaussianBlur(first, (11, 11), 1.5)
    mu_second = cv2.GaussianBlur(second, (11, 11), 1.5)
    sigma_first = cv2.GaussianBlur(first * first, (11, 11), 1.5) - mu_first**2
    sigma_second = cv2.GaussianBlur(second * second, (11, 11), 1.5) - mu_second**2
    covariance = cv2.GaussianBlur(first * second, (11, 11), 1.5) - mu_first * mu_second
    numerator = (2 * mu_first * mu_second + c1) * (2 * covariance + c2)
    denominator = (mu_first**2 + mu_second**2 + c1) * (sigma_first + sigma_second + c2)
    value = float(np.mean(numerator / np.maximum(denominator, 1e-12)))
    if not math.isfinite(value):
        raise DemoVerificationError("structural similarity is non-finite")
    return value


def _clarity_metrics(
    frame: NDArray[np.uint8], clean_frame: NDArray[np.uint8]
) -> dict[str, float]:
    candidate = _gray(frame)
    clean = _gray(clean_frame)
    if candidate.shape != clean.shape:
        raise DemoVerificationError("clarity frames do not share one resolution")
    clean_gradient = _gradient(clean)
    candidate_gradient = _gradient(candidate)
    positive = clean_gradient[clean_gradient > 1e-6]
    if positive.size < 32:
        raise DemoVerificationError("clean reference lacks measurable edges")
    threshold = float(np.percentile(positive, 75))
    edge_mask = clean_gradient >= threshold
    clean_edge = float(np.mean(clean_gradient[edge_mask]))
    candidate_edge = float(np.mean(candidate_gradient[edge_mask]))
    edge_spread = clean_edge / max(candidate_edge, 1e-9)
    continuity = float(
        np.mean(candidate_gradient[edge_mask] >= clean_gradient[edge_mask] * 0.62)
    )
    scale_errors = []
    for sigma in (0.0, 1.0, 2.0):
        first = candidate if sigma == 0 else cv2.GaussianBlur(candidate, (0, 0), sigma)
        second = clean if sigma == 0 else cv2.GaussianBlur(clean, (0, 0), sigma)
        scale_errors.append(float(np.mean(np.abs(first - second))))
    laplacian_delta = cv2.Laplacian(candidate - clean, cv2.CV_32F)
    clean_laplacian = cv2.Laplacian(clean, cv2.CV_32F)
    ringing_noise = float(np.mean(np.abs(laplacian_delta))) / max(
        float(np.mean(np.abs(clean_laplacian))), 1e-6
    )
    result = {
        "multi_scale_structure_error": float(np.mean(scale_errors)),
        "edge_spread": edge_spread,
        "edge_continuity": continuity,
        "structural_similarity": _ssim(candidate, clean),
        "ringing_noise_ratio": ringing_noise,
        "global_laplacian_variance": float(cv2.Laplacian(candidate, cv2.CV_32F).var()),
    }
    if not all(math.isfinite(value) for value in result.values()):
        raise DemoVerificationError("clarity measurement is non-finite")
    return result


def _measure_clarity_region(
    frames: dict[str, NDArray[np.uint8]],
    temporal_frames: dict[str, tuple[NDArray[np.uint8], NDArray[np.uint8]]],
    contract: DemoContract,
) -> dict[str, object]:
    required = ("source", "v14", "candidate", "clean")
    metrics = {
        label: _clarity_metrics(frames[label], frames["clean"]) for label in required
    }
    candidate = metrics["candidate"]
    source = metrics["source"]
    v14 = metrics["v14"]
    candidate_delta = _gray(temporal_frames["candidate"][1]) - _gray(
        temporal_frames["candidate"][0]
    )
    clean_delta = _gray(temporal_frames["clean"][1]) - _gray(
        temporal_frames["clean"][0]
    )
    temporal_residual = float(np.mean(np.abs(candidate_delta - clean_delta)))
    checks: dict[str, dict[str, object]] = {
        "multi_scale_edge_spread": {
            "status": (
                "passed"
                if candidate["edge_spread"]
                <= source["edge_spread"] * contract.maximum_edge_spread_source_ratio
                and candidate["edge_spread"]
                <= v14["edge_spread"] * contract.maximum_edge_spread_v14_ratio
                else "failed"
            ),
            "candidate": candidate["edge_spread"],
            "source": source["edge_spread"],
            "v14": v14["edge_spread"],
        },
        "edge_continuity": {
            "status": (
                "passed"
                if candidate["edge_continuity"]
                >= source["edge_continuity"]
                + contract.minimum_edge_continuity_source_delta
                and candidate["edge_continuity"]
                >= v14["edge_continuity"] + contract.minimum_edge_continuity_v14_delta
                else "failed"
            ),
            "candidate": candidate["edge_continuity"],
            "source": source["edge_continuity"],
            "v14": v14["edge_continuity"],
        },
        "structural_similarity": {
            "status": (
                "passed"
                if candidate["structural_similarity"]
                >= max(source["structural_similarity"], v14["structural_similarity"])
                + contract.minimum_structural_similarity_delta
                and candidate["multi_scale_structure_error"]
                <= min(
                    source["multi_scale_structure_error"]
                    * contract.maximum_structure_error_source_ratio,
                    v14["multi_scale_structure_error"]
                    * contract.maximum_structure_error_v14_ratio,
                )
                else "failed"
            ),
            "candidate": candidate["structural_similarity"],
            "source": source["structural_similarity"],
            "v14": v14["structural_similarity"],
        },
        "ringing_noise": {
            "status": (
                "passed"
                if candidate["ringing_noise_ratio"]
                <= contract.maximum_ringing_noise_ratio
                else "failed"
            ),
            "candidate_ratio": candidate["ringing_noise_ratio"],
        },
        "temporal_residual": {
            "status": (
                "passed"
                if temporal_residual <= contract.maximum_temporal_residual
                else "failed"
            ),
            "candidate": temporal_residual,
        },
    }
    return {
        "status": (
            "passed"
            if all(item["status"] == "passed" for item in checks.values())
            else "failed"
        ),
        "checks": checks,
        "variants": metrics,
    }


def _select_clarity_regions(
    clean: NDArray[np.uint8], contract: DemoContract
) -> tuple[tuple[int, int, int, int, float], ...]:
    """Select one deterministic high-frequency tile per non-empty image row."""

    height, width = clean.shape[:2]
    if height < contract.clarity_roi_rows or width < contract.clarity_roi_columns:
        raise DemoVerificationError("clarity frame is too small for the ROI grid")
    gradient = _gradient(_gray(clean))
    selected: list[tuple[int, int, int, int, float]] = []
    for row in range(contract.clarity_roi_rows):
        y0 = row * height // contract.clarity_roi_rows
        y1 = (row + 1) * height // contract.clarity_roi_rows
        candidates: list[tuple[float, int, int, int, int, int]] = []
        for column in range(contract.clarity_roi_columns):
            x0 = column * width // contract.clarity_roi_columns
            x1 = (column + 1) * width // contract.clarity_roi_columns
            tile = gradient[y0:y1, x0:x1]
            edge_pixels = int(np.count_nonzero(tile > 1e-6))
            if edge_pixels >= contract.minimum_clarity_roi_edge_pixels:
                candidates.append((float(np.sum(tile)), edge_pixels, x0, y0, x1, y1))
        if candidates:
            score, _, x0, y0, x1, y1 = max(
                candidates, key=lambda item: (item[0], item[1], -item[2])
            )
            selected.append((x0, y0, x1, y1, score))
    if not selected:
        raise DemoVerificationError("clean reference lacks a measurable local ROI")
    return tuple(selected)


def _measure_clarity_frames(
    frames: dict[str, NDArray[np.uint8]],
    temporal_frames: dict[str, tuple[NDArray[np.uint8], NDArray[np.uint8]]],
    contract: DemoContract | None = None,
) -> dict[str, object]:
    effective = contract or DemoContract()
    required = ("source", "v14", "candidate", "clean")
    if tuple(sorted(frames)) != tuple(sorted(required)) or tuple(
        sorted(temporal_frames)
    ) != tuple(sorted(required)):
        raise DemoVerificationError("clarity measurement requires four bound variants")
    global_measurement = _measure_clarity_region(frames, temporal_frames, effective)
    regions: list[dict[str, object]] = []
    for index, (x0, y0, x1, y1, edge_score) in enumerate(
        _select_clarity_regions(frames["clean"], effective)
    ):
        region_frames = {label: frame[y0:y1, x0:x1] for label, frame in frames.items()}
        region_temporal = {
            label: (pair[0][y0:y1, x0:x1], pair[1][y0:y1, x0:x1])
            for label, pair in temporal_frames.items()
        }
        region = _measure_clarity_region(region_frames, region_temporal, effective)
        region.update(
            {
                "index": index,
                "box": [x0, y0, x1, y1],
                "clean_edge_score": edge_score,
            }
        )
        regions.append(region)
    local_status = (
        "passed"
        if all(region["status"] == "passed" for region in regions)
        else "failed"
    )
    return {
        "status": (
            "passed"
            if global_measurement["status"] == "passed" and local_status == "passed"
            else "failed"
        ),
        "checks": global_measurement["checks"],
        "variants": global_measurement["variants"],
        "local_regions_status": local_status,
        "regions": regions,
    }


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-10))


def _tone_dbfs(
    samples: NDArray[np.float32], sample_rate: int, frequency: float
) -> float:
    window: NDArray[np.float64] = np.hanning(samples.size).astype(np.float64)
    timeline = np.arange(samples.size, dtype=np.float64) / sample_rate
    phase = np.exp(-2j * np.pi * frequency * timeline)
    amplitude = (
        2.0
        * abs(np.sum(samples.astype(np.float64) * window * phase))
        / max(float(np.sum(window)), 1e-12)
    )
    return _dbfs(float(amplitude))


def _non_target_dbfs(
    samples: NDArray[np.float32],
    sample_rate: int,
    excluded_frequencies: tuple[float, ...],
) -> float:
    window: NDArray[np.float64] = np.hanning(samples.size).astype(np.float64)
    spectrum = np.fft.rfft(samples.astype(np.float64) * window)
    frequencies = np.fft.rfftfreq(samples.size, 1.0 / sample_rate)
    keep = (frequencies >= 80.0) & (frequencies <= min(8000.0, sample_rate / 2))
    resolution = sample_rate / samples.size
    for frequency in excluded_frequencies:
        keep &= np.abs(frequencies - frequency) > max(20.0, resolution * 2.0)
    energy = (
        float(np.sqrt(np.mean(np.abs(spectrum[keep]) ** 2))) if np.any(keep) else 0.0
    )
    normalized = energy * 2.0 / max(float(np.sum(window)), 1e-12)
    return _dbfs(normalized)


def _window_audio_metrics(
    samples: NDArray[np.float32],
    sample_rate: int,
    contract: DemoContract,
) -> dict[str, float]:
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    peak = float(np.max(np.abs(samples)))
    return {
        "persistent_dbfs": _tone_dbfs(
            samples, sample_rate, contract.persistent_frequency_hz
        ),
        "target_dbfs": _tone_dbfs(
            samples, sample_rate, contract.interference_frequency_hz
        ),
        "non_target_dbfs": _non_target_dbfs(
            samples,
            sample_rate,
            (
                contract.persistent_frequency_hz,
                contract.interference_frequency_hz,
            ),
        ),
        "rms_dbfs": _dbfs(rms),
        "crest_ratio": peak / max(rms, 1e-10),
    }


def _measure_audio_boundary(
    variants: dict[str, NDArray[np.float32]],
    sample_rate: int,
    contract: DemoContract,
    *,
    boundary_sample: int,
    side: Literal["left", "right"],
) -> dict[str, object]:
    """Measure the complete 50 ms windows on both sides of one edit boundary."""

    window_samples = round(contract.audio_window_seconds * sample_rate)
    if side == "left":
        first_start = boundary_sample - window_samples
        first_label = "before"
        second_start = boundary_sample
        second_label = "inside"
    else:
        first_start = boundary_sample - window_samples
        first_label = "inside"
        second_start = boundary_sample
        second_label = "after"
    second_stop = second_start + window_samples
    sample_count = variants["candidate"].size
    if first_start < 0 or second_stop > sample_count:
        raise DemoVerificationError(
            f"audio {side} boundary lacks complete adjacent 50 ms windows"
        )

    per_variant: dict[str, dict[str, dict[str, float]]] = {}
    for label, samples in variants.items():
        per_variant[label] = {
            first_label: _window_audio_metrics(
                samples[first_start:boundary_sample], sample_rate, contract
            ),
            second_label: _window_audio_metrics(
                samples[second_start:second_stop], sample_rate, contract
            ),
        }

    candidate_jump = float(
        abs(
            variants["candidate"][boundary_sample]
            - variants["candidate"][boundary_sample - 1]
        )
    )
    reference_jump = max(
        float(
            abs(
                variants["source"][boundary_sample]
                - variants["source"][boundary_sample - 1]
            )
        ),
        float(
            abs(
                variants["clean"][boundary_sample]
                - variants["clean"][boundary_sample - 1]
            )
        ),
    )
    sample_jump_excess = max(0.0, candidate_jump - reference_jump)

    candidate_first = per_variant["candidate"][first_label]
    candidate_second = per_variant["candidate"][second_label]
    clean_first = per_variant["clean"][first_label]
    clean_second = per_variant["clean"][second_label]
    candidate_rms_change = abs(
        candidate_second["rms_dbfs"] - candidate_first["rms_dbfs"]
    )
    clean_rms_change = abs(clean_second["rms_dbfs"] - clean_first["rms_dbfs"])
    rms_increase = max(0.0, candidate_rms_change - clean_rms_change)

    candidate_crest = max(
        candidate_first["crest_ratio"], candidate_second["crest_ratio"]
    )
    reference_crest = max(
        per_variant[label][window]["crest_ratio"]
        for label in ("source", "clean")
        for window in (first_label, second_label)
    )
    crest_excess = max(0.0, candidate_crest - reference_crest)
    passed = (
        sample_jump_excess <= contract.maximum_boundary_sample_jump_excess
        and rms_increase <= contract.maximum_boundary_rms_increase_db
        and crest_excess <= contract.maximum_boundary_crest_excess
    )
    return {
        "status": "passed" if passed else "failed",
        "boundary_seconds": boundary_sample / sample_rate,
        "sample_jump_excess": sample_jump_excess,
        "rms_increase_db": rms_increase,
        "energy_increase_db": rms_increase,
        "crest_excess": crest_excess,
        "windows": per_variant,
    }


def _measure_audio_samples(
    variants: dict[str, NDArray[np.float32]],
    sample_rate: int,
    contract: DemoContract,
) -> dict[str, object]:
    required = ("source", "v14", "candidate", "clean")
    if tuple(sorted(variants)) != tuple(sorted(required)):
        raise DemoVerificationError("audio measurement requires four bound variants")
    if sample_rate != contract.expected_sample_rate:
        raise DemoVerificationError("audio sample rate does not match the contract")
    lengths = {samples.size for samples in variants.values()}
    if len(lengths) != 1:
        raise DemoVerificationError("audio variants have inconsistent sample counts")
    if any(samples.ndim != 1 for samples in variants.values()):
        raise DemoVerificationError("decoded audio must be mono")
    if any(not np.all(np.isfinite(samples)) for samples in variants.values()):
        raise DemoVerificationError("decoded audio samples must be finite")
    window_samples = round(contract.audio_window_seconds * sample_rate)
    start_sample = round(contract.audio_start_seconds * sample_rate)
    end_sample = round(contract.audio_end_seconds * sample_rate)
    if window_samples <= 0 or end_sample > next(iter(lengths)):
        raise DemoVerificationError("audio verification range is unavailable")
    if (end_sample - start_sample) % window_samples != 0:
        raise DemoVerificationError("audio range does not contain complete windows")
    windows: list[dict[str, object]] = []
    target_attenuations: list[float] = []
    persistent_differences: list[float] = []
    non_target_differences: list[float] = []
    candidate_jumps: list[float] = []
    reference_jumps: list[float] = []
    for index, offset in enumerate(range(start_sample, end_sample, window_samples)):
        stop = offset + window_samples
        per_variant = {
            label: _window_audio_metrics(
                variants[label][offset:stop], sample_rate, contract
            )
            for label in required
        }
        source = per_variant["source"]
        candidate = per_variant["candidate"]
        clean = per_variant["clean"]
        target_evidence = (
            source["target_dbfs"] - clean["target_dbfs"]
            >= contract.target_evidence_margin_db
        )
        attenuation = source["target_dbfs"] - candidate["target_dbfs"]
        if target_evidence:
            target_attenuations.append(attenuation)
        persistent_differences.append(
            abs(candidate["persistent_dbfs"] - source["persistent_dbfs"])
        )
        non_target_differences.append(
            abs(candidate["non_target_dbfs"] - clean["non_target_dbfs"])
        )
        if offset > 0:
            candidate_jumps.append(
                float(
                    abs(
                        variants["candidate"][offset]
                        - variants["candidate"][offset - 1]
                    )
                )
            )
            reference_jumps.append(
                max(
                    float(
                        abs(variants["source"][offset] - variants["source"][offset - 1])
                    ),
                    float(
                        abs(variants["clean"][offset] - variants["clean"][offset - 1])
                    ),
                )
            )
        windows.append(
            {
                "index": index,
                "start_seconds": offset / sample_rate,
                "end_seconds": stop / sample_rate,
                "target_evidence": target_evidence,
                "target_attenuation_db": attenuation,
                "variants": per_variant,
            }
        )
    if not target_attenuations:
        raise DemoVerificationError("target windows lack reliable tonal evidence")
    minimum_attenuation = min(target_attenuations)
    maximum_persistent_difference = max(persistent_differences)
    maximum_non_target_difference = max(non_target_differences)
    maximum_excess_jump = max(
        (
            candidate - reference
            for candidate, reference in zip(
                candidate_jumps, reference_jumps, strict=True
            )
        ),
        default=0.0,
    )
    maximum_candidate_crest = max(
        float(window["variants"]["candidate"]["crest_ratio"])  # type: ignore[index]
        for window in windows
    )
    maximum_source_crest = max(
        float(window["variants"]["source"]["crest_ratio"])  # type: ignore[index]
        for window in windows
    )
    boundaries = {
        "left": _measure_audio_boundary(
            variants,
            sample_rate,
            contract,
            boundary_sample=start_sample,
            side="left",
        ),
        "right": _measure_audio_boundary(
            variants,
            sample_rate,
            contract,
            boundary_sample=end_sample,
            side="right",
        ),
    }
    maximum_boundary_jump = max(
        maximum_excess_jump,
        *(
            _finite_float(item["sample_jump_excess"], "boundary sample jump")
            for item in boundaries.values()
        ),
    )
    maximum_boundary_rms_increase = max(
        _finite_float(item["rms_increase_db"], "boundary RMS increase")
        for item in boundaries.values()
    )
    maximum_boundary_crest_excess = max(
        _finite_float(item["crest_excess"], "boundary crest excess")
        for item in boundaries.values()
    )
    checks: dict[str, dict[str, object]] = {
        "minimum_target_attenuation": {
            "status": (
                "passed"
                if minimum_attenuation >= contract.minimum_tonal_attenuation_db
                else "failed"
            ),
            "value_db": minimum_attenuation,
            "required_db": contract.minimum_tonal_attenuation_db,
            "evidence_window_count": len(target_attenuations),
        },
        "persistent_tone_preservation": {
            "status": (
                "passed"
                if maximum_persistent_difference
                <= contract.maximum_persistent_tone_difference_db
                else "failed"
            ),
            "maximum_difference_db": maximum_persistent_difference,
        },
        "non_target_preservation": {
            "status": (
                "passed"
                if maximum_non_target_difference
                <= contract.maximum_non_target_difference_db
                else "failed"
            ),
            "maximum_difference_db": maximum_non_target_difference,
        },
        "boundary_transient": {
            "status": (
                "passed"
                if maximum_boundary_jump <= contract.maximum_boundary_sample_jump_excess
                and maximum_boundary_rms_increase
                <= contract.maximum_boundary_rms_increase_db
                and maximum_boundary_crest_excess
                <= contract.maximum_boundary_crest_excess
                and all(item["status"] == "passed" for item in boundaries.values())
                else "failed"
            ),
            "maximum_excess_sample_jump": maximum_boundary_jump,
            "maximum_rms_increase_db": maximum_boundary_rms_increase,
            "maximum_crest_excess": maximum_boundary_crest_excess,
            "maximum_candidate_crest": maximum_candidate_crest,
            "maximum_source_crest": maximum_source_crest,
            "boundaries": boundaries,
        },
    }
    for window_record in windows:
        raw_variants = window_record.get("variants")
        if not isinstance(raw_variants, Mapping):
            raise DemoVerificationError("audio measurements are invalid")
        for raw_metrics in raw_variants.values():
            if not isinstance(raw_metrics, Mapping) or not all(
                isinstance(value, (int, float)) and math.isfinite(float(value))
                for value in raw_metrics.values()
            ):
                raise DemoVerificationError("audio measurements must be finite")
    return {
        "status": (
            "passed"
            if all(item["status"] == "passed" for item in checks.values())
            else "failed"
        ),
        "window_seconds": contract.audio_window_seconds,
        "windows": windows,
        "checks": checks,
    }


def _phase_translation(
    source: NDArray[np.uint8], target: NDArray[np.uint8]
) -> tuple[float, float, float]:
    source_gray = _gray(source)
    target_gray = _gray(target)
    if source_gray.shape != target_gray.shape:
        raise DemoVerificationError("motion frames do not share one resolution")
    height, width = source_gray.shape
    window = cv2.createHanningWindow((width, height), cv2.CV_32F)
    shift, response = cv2.phaseCorrelate(source_gray, target_gray, window)
    values = (float(shift[0]), float(shift[1]), float(response))
    if not all(math.isfinite(value) for value in values):
        raise DemoVerificationError("motion registration is non-finite")
    return values


def _candidate_crop_ratio(
    candidate: NDArray[np.uint8], clean: NDArray[np.uint8]
) -> float:
    candidate_gray = _gray(candidate)
    clean_gray = _gray(clean)
    warp = np.asarray(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0)), dtype=np.float32)
    criteria = (
        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
        40,
        1e-5,
    )
    try:
        _, measured = cv2.findTransformECC(
            clean_gray,
            candidate_gray,
            warp,
            cv2.MOTION_AFFINE,
            criteria,
            None,
            3,
        )
    except cv2.error:
        return math.nan
    scale_x = math.hypot(float(measured[0, 0]), float(measured[1, 0]))
    scale_y = math.hypot(float(measured[0, 1]), float(measured[1, 1]))
    scale = (scale_x + scale_y) / 2.0
    if not math.isfinite(scale) or scale <= 0:
        return math.nan
    return max(0.0, 1.0 - 1.0 / scale) if scale >= 1.0 else 0.0


def _transition_binding_from_validated_plan(
    plan: object,
    *,
    plan_bytes: bytes,
    source_sha256: str,
    timestamps: tuple[float, ...],
    contract: DemoContract,
) -> TransitionPlanBinding:
    """Bind one strict STABILIZE action to every measured source PTS."""
    try:
        input_hash = getattr(plan, "input_hash")
        plan_digest = getattr(plan, "plan_digest")
        actions = tuple(getattr(plan, "actions"))
        if input_hash != source_sha256:
            raise ValueError
        if not isinstance(plan_digest, str) or _SHA256.fullmatch(plan_digest) is None:
            raise ValueError
        matching = tuple(
            action
            for action in actions
            if getattr(getattr(action, "kind", None), "value", None) == "stabilize"
        )
        if len(matching) != 1:
            raise ValueError
        action = matching[0]
        action_id = getattr(action, "id")
        if (
            not isinstance(action_id, str)
            or re.fullmatch(r"rescue_action_[0-9a-f]{64}", action_id) is None
        ):
            raise ValueError
        source_ranges = tuple(
            (float(start), float(end))
            for start, end in tuple(getattr(action, "source_ranges"))
        )
        if source_ranges != (
            (contract.motion_start_seconds, contract.motion_end_seconds),
        ):
            raise ValueError
        parameters = getattr(action, "parameters")
        if not isinstance(parameters, Mapping):
            raise ValueError
        if (
            parameters.get("algorithm_version") != "1"
            or parameters.get("method") != "transition_anchor_v1"
        ):
            raise ValueError
        median_goal = parameters.get("residual_goal_median_pixels")
        p90_goal = parameters.get("residual_goal_p90_pixels")
        if (
            isinstance(median_goal, bool)
            or not isinstance(median_goal, (int, float))
            or not math.isclose(
                float(median_goal),
                contract.maximum_motion_median_pixels,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or isinstance(p90_goal, bool)
            or not isinstance(p90_goal, (int, float))
            or not math.isclose(
                float(p90_goal),
                contract.maximum_motion_p90_pixels,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError
        frame_width = parameters.get("frame_width")
        frame_height = parameters.get("frame_height")
        safe_crop_ratio = parameters.get("crop_ratio")
        if (
            isinstance(frame_width, bool)
            or not isinstance(frame_width, int)
            or frame_width <= 0
            or isinstance(frame_height, bool)
            or not isinstance(frame_height, int)
            or frame_height <= 0
            or isinstance(safe_crop_ratio, bool)
            or not isinstance(safe_crop_ratio, (int, float))
            or not math.isfinite(float(safe_crop_ratio))
            or not 0.0 <= float(safe_crop_ratio) <= 0.12
        ):
            raise ValueError
        correction_count = parameters.get("transition_correction_count")
        transforms = tuple(parameters.get("motion_transforms", ()))
        if (
            isinstance(correction_count, bool)
            or not isinstance(correction_count, int)
            or correction_count != len(timestamps)
            or len(transforms) != len(timestamps)
        ):
            raise ValueError
        expected: list[TransitionFrameTransform] = []
        for timestamp, transform in zip(timestamps, transforms, strict=True):
            if not isinstance(transform, Mapping):
                raise ValueError
            measured_timestamp = transform.get("timestamp_seconds")
            rotation_degrees = transform.get("rotation_degrees")
            scale = transform.get("scale")
            translation_x = transform.get("translation_x")
            translation_y = transform.get("translation_y")
            values = (
                measured_timestamp,
                rotation_degrees,
                scale,
                translation_x,
                translation_y,
            )
            if any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in values
            ):
                raise ValueError
            numeric_values = tuple(
                value
                for value in values
                if isinstance(value, (int, float)) and not isinstance(value, bool)
            )
            if len(numeric_values) != len(values):
                raise ValueError
            finite_values = tuple(float(value) for value in numeric_values)
            if not all(math.isfinite(value) for value in finite_values):
                raise ValueError
            if (
                abs(finite_values[0] - timestamp) > contract.timestamp_tolerance_seconds
                or transform.get("semantics") != "frame_correction"
            ):
                raise ValueError
            if finite_values[2] <= 0.0:
                raise ValueError
            expected.append(
                TransitionFrameTransform(
                    timestamp_seconds=finite_values[0],
                    rotation_degrees=finite_values[1],
                    scale=finite_values[2],
                    translation_x=finite_values[3],
                    translation_y=finite_values[4],
                )
            )
    except (AttributeError, TypeError, ValueError) as error:
        raise DemoVerificationError("transition plan binding is invalid") from error
    return TransitionPlanBinding(
        plan_sha256=hashlib.sha256(plan_bytes).hexdigest(),
        plan_digest=plan_digest,
        action_id=action_id,
        frame_width=frame_width,
        frame_height=frame_height,
        safe_crop_ratio=float(safe_crop_ratio),
        transforms=tuple(expected),
    )


def _load_transition_plan_binding(
    path: Path,
    *,
    source_sha256: str,
    timestamps: tuple[float, ...],
    contract: DemoContract,
) -> TransitionPlanBinding:
    if not path.is_file() or path.is_symlink():
        raise DemoVerificationError("transition plan is unavailable")
    try:
        plan_bytes = path.read_bytes()
        if not plan_bytes or len(plan_bytes) > 16 * 1024 * 1024:
            raise ValueError
        from videoscope.rescue.models import RescuePlan

        plan = RescuePlan.model_validate_json(plan_bytes)
    except (OSError, ValueError) as error:
        raise DemoVerificationError("transition plan is invalid") from error
    return _transition_binding_from_validated_plan(
        plan,
        plan_bytes=plan_bytes,
        source_sha256=source_sha256,
        timestamps=timestamps,
        contract=contract,
    )


def _render_transition_plan_frames(
    source_frames: Sequence[NDArray[np.uint8]],
    binding: TransitionPlanBinding,
) -> tuple[NDArray[np.uint8], ...]:
    """Independently render the complete plan affine at analysis resolution."""
    if len(source_frames) != len(binding.transforms) or not source_frames:
        raise DemoVerificationError("transition plan frame inventory is incomplete")
    rendered: list[NDArray[np.uint8]] = []
    for source, transform in zip(source_frames, binding.transforms, strict=True):
        if source.ndim != 3 or source.shape[2] != 3:
            raise DemoVerificationError("transition plan frame shape is invalid")
        height, width = source.shape[:2]
        radians = math.radians(transform.rotation_degrees)
        cosine = math.cos(radians) * transform.scale
        sine = math.sin(radians) * transform.scale
        matrix = np.array(
            [
                [
                    cosine,
                    -sine,
                    transform.translation_x * width / binding.frame_width,
                ],
                [
                    sine,
                    cosine,
                    transform.translation_y * height / binding.frame_height,
                ],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        if binding.safe_crop_ratio > 0.0:
            zoom = 1.0 / (1.0 - binding.safe_crop_ratio)
            center_x = (width - 1) / 2.0
            center_y = (height - 1) / 2.0
            matrix = (
                np.array(
                    [
                        [zoom, 0.0, center_x * (1.0 - zoom)],
                        [0.0, zoom, center_y * (1.0 - zoom)],
                        [0.0, 0.0, 1.0],
                    ],
                    dtype=np.float64,
                )
                @ matrix
            )
        rendered.append(
            cv2.warpAffine(
                source,
                np.asarray(matrix[:2, :], dtype=np.float32),
                (width, height),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
        )
    return tuple(rendered)


def _measure_motion_frames(
    *,
    timestamps: Sequence[float],
    source_frames: Sequence[NDArray[np.uint8]],
    candidate_frames: Sequence[NDArray[np.uint8]],
    clean_frames: Sequence[NDArray[np.uint8]],
    contract: DemoContract,
    pixel_scale: float = 1.0,
    expected_plan_frames: Sequence[NDArray[np.uint8]] | None = None,
) -> dict[str, object]:
    """Measure motion against either clean or complete planned affine frames.

    The default remains the public direct-clean-anchor contract.  A caller that
    holds a separately confirmed transition plan may provide one independently
    rendered expected frame per actual PTS; this does not relax any median/P90/
    crop gate and is rejected on inventory mismatch.
    """
    expected = len(timestamps)
    if expected < 2 or not (
        len(source_frames) == len(candidate_frames) == len(clean_frames) == expected
    ):
        raise DemoVerificationError("motion frame coverage is incomplete")
    if not math.isfinite(pixel_scale) or pixel_scale <= 0:
        raise DemoVerificationError("motion pixel scale is invalid")
    planned_frames = (
        tuple(expected_plan_frames) if expected_plan_frames is not None else None
    )
    if planned_frames is not None and (
        len(planned_frames) != expected
        or any(
            frame.shape != source_frames[index].shape
            for index, frame in enumerate(planned_frames)
        )
    ):
        raise DemoVerificationError("motion expected frame inventory is incomplete")
    if any(
        not math.isfinite(timestamp)
        or timestamp < contract.motion_start_seconds
        or timestamp >= contract.motion_end_seconds
        for timestamp in timestamps
    ):
        raise DemoVerificationError("motion timestamps are outside the contract")
    if any(
        current <= previous for previous, current in zip(timestamps, timestamps[1:])
    ):
        raise DemoVerificationError("motion timestamps must be strictly increasing")
    frame_metrics: list[dict[str, float]] = []
    translation_deltas: list[tuple[float, float]] = []
    crop_ratios: list[float] = []
    reliable = 0
    for timestamp, source, candidate, clean in zip(
        timestamps, source_frames, candidate_frames, clean_frames, strict=True
    ):
        observed_x, observed_y, observed_response = _phase_translation(
            source, candidate
        )
        if planned_frames is None:
            expected_x, expected_y, expected_response = _phase_translation(
                source, clean
            )
        else:
            expected_x, expected_y, expected_response = _phase_translation(
                source, planned_frames[len(frame_metrics)]
            )
        crop_ratio = _candidate_crop_ratio(candidate, clean)
        response = min(expected_response, observed_response)
        is_reliable = (
            response >= contract.minimum_registration_response
            and math.isfinite(crop_ratio)
        )
        if is_reliable:
            reliable += 1
            translation_deltas.append(
                (observed_x - expected_x, observed_y - expected_y)
            )
            crop_ratios.append(crop_ratio)
        frame_metrics.append(
            {
                "timestamp_seconds": float(timestamp),
                "expected_translation_x": expected_x * pixel_scale,
                "expected_translation_y": expected_y * pixel_scale,
                "observed_translation_x": observed_x * pixel_scale,
                "observed_translation_y": observed_y * pixel_scale,
                "registration_response": response,
                "residual_pixels": math.nan,
                "crop_ratio": crop_ratio,
                "reliable": float(is_reliable),
            }
        )
    if reliable != expected or len(translation_deltas) != expected:
        raise DemoVerificationError("motion frame coverage lacks reliable registration")
    anchor_offset_x = float(np.median([item[0] for item in translation_deltas]))
    anchor_offset_y = float(np.median([item[1] for item in translation_deltas]))
    residuals: list[float] = []
    for frame, (delta_x, delta_y) in zip(
        frame_metrics, translation_deltas, strict=True
    ):
        residual = (
            math.hypot(
                delta_x - anchor_offset_x,
                delta_y - anchor_offset_y,
            )
            * pixel_scale
        )
        frame["residual_pixels"] = residual
        residuals.append(residual)
    median = float(np.median(residuals))
    p90 = float(np.percentile(residuals, 90))
    maximum = max(residuals)
    crop = float(np.percentile(crop_ratios, 95))
    status = (
        "passed"
        if median <= contract.maximum_motion_median_pixels
        and p90 <= contract.maximum_motion_p90_pixels
        and crop <= contract.maximum_motion_crop_ratio
        else "failed"
    )
    return {
        "status": status,
        "expected_frame_count": expected,
        "compared_frame_count": expected,
        "reliable_frame_count": reliable,
        "residual_median_pixels": median,
        "residual_p90_pixels": p90,
        "residual_maximum_pixels": maximum,
        "crop_ratio_p95": crop,
        "anchor_offset_translation_x": anchor_offset_x * pixel_scale,
        "anchor_offset_translation_y": anchor_offset_y * pixel_scale,
        "frames": frame_metrics,
    }


def _scaled_height(width: int, source_width: int, source_height: int) -> int:
    height = max(2, round(width * source_height / source_width))
    return height if height % 2 == 0 else height + 1


def _decode_audio(
    path: Path,
    ffmpeg: Path,
    contract: DemoContract,
    runner: CommandRunner,
) -> NDArray[np.float32]:
    expected_samples = round(
        contract.expected_duration_seconds * contract.expected_sample_rate
    )
    payload = runner(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-ac",
            "1",
            "-ar",
            str(contract.expected_sample_rate),
            "-f",
            "f32le",
            "-",
        ],
        timeout_seconds=300.0,
        maximum_output_bytes=expected_samples * 4 + 1024 * 1024,
    )
    if len(payload) % 4:
        raise DemoVerificationError("decoded audio is truncated")
    samples = np.frombuffer(payload, dtype="<f4").copy()
    tolerance = round(
        contract.expected_sample_rate * contract.duration_tolerance_seconds
    )
    if abs(samples.size - expected_samples) > tolerance:
        raise DemoVerificationError(
            "decoded audio sample count does not match duration"
        )
    if samples.size < expected_samples:
        raise DemoVerificationError("decoded audio is shorter than the contract")
    samples = samples[:expected_samples]
    if not np.all(np.isfinite(samples)):
        raise DemoVerificationError("decoded audio samples must be finite")
    return samples


def _extract_frame(
    path: Path,
    timestamp: float,
    info: MediaInfo,
    ffmpeg: Path,
    runner: CommandRunner,
) -> NDArray[np.uint8]:
    expected_bytes = info.width * info.height * 3
    payload = runner(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-ss",
            f"{timestamp:.9f}",
            "-map",
            "0:v:0",
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "-",
        ],
        maximum_output_bytes=expected_bytes,
    )
    if len(payload) != expected_bytes:
        raise DemoVerificationError("exact evidence frame could not be decoded")
    return (
        np.frombuffer(payload, dtype=np.uint8)
        .reshape(info.height, info.width, 3)
        .copy()
    )


def _decode_scaled_video(
    path: Path,
    info: MediaInfo,
    ffmpeg: Path,
    runner: CommandRunner,
    *,
    width: int,
    channels: Literal[1, 3],
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> tuple[NDArray[np.uint8], ...]:
    height = _scaled_height(width, info.width, info.height)
    if (start_seconds is None) != (end_seconds is None):
        raise DemoVerificationError("video decode range is incomplete")
    selected_timestamps = info.frame_timestamps
    filters: list[str] = []
    if start_seconds is not None and end_seconds is not None:
        selected_timestamps = tuple(
            timestamp
            for timestamp in info.frame_timestamps
            if start_seconds <= timestamp < end_seconds
        )
        filters.append(f"trim=start={start_seconds:.9f}:end={end_seconds:.9f}")
    filters.append(f"scale={width}:{height}:flags=lanczos")
    pixel_format = "gray" if channels == 1 else "rgb24"
    frame_bytes = width * height * channels
    expected_bytes = frame_bytes * len(selected_timestamps)
    payload = runner(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            ",".join(filters),
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            pixel_format,
            "-f",
            "rawvideo",
            "-",
        ],
        timeout_seconds=300.0,
        maximum_output_bytes=expected_bytes,
    )
    if len(payload) != expected_bytes or not selected_timestamps:
        raise DemoVerificationError("scaled video frame coverage is incomplete")
    array = np.frombuffer(payload, dtype=np.uint8)
    if channels == 1:
        reshaped = array.reshape(len(selected_timestamps), height, width)
    else:
        reshaped = array.reshape(len(selected_timestamps), height, width, channels)
    return tuple(frame.copy() for frame in reshaped)


def _outside_mask(
    values: NDArray[np.float64], contract: DemoContract
) -> NDArray[np.bool_]:
    mask = np.ones(values.shape, dtype=np.bool_)
    for start, end in contract.target_intervals:
        mask &= (values < start) | (values >= end)
    return mask


def _measure_outside_fidelity(
    *,
    source_frames: Sequence[NDArray[np.uint8]],
    candidate_frames: Sequence[NDArray[np.uint8]],
    frame_timestamps: Sequence[float],
    source_audio: NDArray[np.float32],
    candidate_audio: NDArray[np.float32],
    sample_rate: int,
    contract: DemoContract,
) -> dict[str, object]:
    if len(source_frames) != len(candidate_frames) or len(source_frames) != len(
        frame_timestamps
    ):
        raise DemoVerificationError("outside video mapping is incomplete")
    frame_mask = _outside_mask(np.asarray(frame_timestamps, dtype=np.float64), contract)
    if not np.any(frame_mask):
        raise DemoVerificationError("outside video range is empty")
    visual_mae = [
        float(
            np.mean(
                np.abs(
                    source_frames[index].astype(np.float32)
                    - candidate_frames[index].astype(np.float32)
                )
            )
            / 255.0
        )
        for raw_index in np.flatnonzero(frame_mask)
        for index in (int(raw_index),)
    ]
    if source_audio.size != candidate_audio.size:
        raise DemoVerificationError("outside audio mapping is incomplete")
    audio_timestamps = np.arange(source_audio.size, dtype=np.float64) / sample_rate
    audio_mask = _outside_mask(audio_timestamps, contract)
    if not np.any(audio_mask):
        raise DemoVerificationError("outside audio range is empty")
    audio_mae = float(
        np.mean(
            np.abs(
                source_audio[audio_mask].astype(np.float64)
                - candidate_audio[audio_mask].astype(np.float64)
            )
        )
    )
    visual_mean = float(np.mean(visual_mae))
    visual_p95 = float(np.percentile(visual_mae, 95))
    status = (
        "passed"
        if visual_p95 <= contract.maximum_outside_visual_mae
        and audio_mae <= contract.maximum_outside_audio_mae
        else "failed"
    )
    return {
        "status": status,
        "video_compared_frame_count": len(visual_mae),
        "video_mae_mean": visual_mean,
        "video_mae_p95": visual_p95,
        "audio_compared_sample_count": int(np.count_nonzero(audio_mask)),
        "audio_mae": audio_mae,
        "target_intervals": [list(interval) for interval in contract.target_intervals],
    }


def _contact_sheet_png(
    frames: Mapping[str, NDArray[np.uint8]], contract: DemoContract
) -> bytes:
    labels = ("source", "v14", "candidate", "clean")
    if tuple(sorted(frames)) != tuple(sorted(labels)):
        raise DemoVerificationError("contact sheet requires four evidence frames")
    clean = frames["clean"]
    crop_x, crop_y, crop_x1, crop_y1, _ = _select_clarity_regions(clean, contract)[0]
    tile_width, tile_height, label_height = 320, 180, 20
    sheet = Image.new(
        "RGB", (tile_width * 4, (tile_height + label_height) * 2), "black"
    )
    draw = ImageDraw.Draw(sheet)
    for index, label in enumerate(labels):
        image = Image.fromarray(frames[label], mode="RGB")
        full = image.resize((tile_width, tile_height), Image.Resampling.LANCZOS)
        crop = image.crop((crop_x, crop_y, crop_x1, crop_y1)).resize(
            (tile_width, tile_height), Image.Resampling.LANCZOS
        )
        x = index * tile_width
        sheet.paste(full, (x, 0))
        sheet.paste(crop, (x, tile_height + label_height))
        draw.text((x + 6, tile_height + 3), label, fill="white")
        draw.text(
            (x + 6, tile_height * 2 + label_height + 3), f"{label} zoom", fill="white"
        )
    buffer = BytesIO()
    sheet.save(buffer, format="PNG", compress_level=9, optimize=False)
    return buffer.getvalue()


def _validate_public_json(value: object) -> None:
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DemoVerificationError("public JSON contains a non-finite number")
        return
    if isinstance(value, str):
        if Path(value).is_absolute() or PureWindowsPath(value).is_absolute():
            raise DemoVerificationError("public JSON contains an absolute path")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DemoVerificationError("public JSON keys must be strings")
            _validate_public_json(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _validate_public_json(item)
        return
    raise DemoVerificationError("public JSON contains an unsupported value")


def _canonical_json_bytes(value: dict[str, object]) -> bytes:
    _validate_public_json(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    except (TypeError, ValueError) as error:
        raise DemoVerificationError("public JSON is invalid") from error
    return f"{text}\n".encode()


def _contract_digest(contract: DemoContract) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(contract.model_dump(mode="json"))
    ).hexdigest()


def _validate_artifact_paths(metrics: Mapping[str, object]) -> None:
    raw = metrics.get("artifacts")
    if not isinstance(raw, Mapping):
        raise DemoVerificationError("metrics artifacts are missing")
    expected = {
        "contact_sheet": "frame-contact-sheet.png",
        "audio": "audio-short-windows.json",
        "motion": "motion-residual.json",
    }
    if raw != expected:
        raise DemoVerificationError(
            "metrics artifacts do not match the output contract"
        )
    for value in raw.values():
        if not isinstance(value, str) or PurePosixPath(value).as_posix() != value:
            raise DemoVerificationError("artifact paths must be relative POSIX paths")


def _raise_no_replace_error(target: Path, error_code: int) -> None:
    if (
        target.exists()
        or target.is_symlink()
        or error_code
        in (
            errno.EEXIST,
            errno.ENOTEMPTY,
        )
    ):
        raise FileExistsError(error_code, "destination already exists", str(target))
    raise OSError(error_code, os.strerror(error_code), str(target))


_WINDOWS_RENAME_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08, 0.16)


def _retry_windows_no_replace_rename(
    source: Path,
    target: Path,
    *,
    rename: Callable[[Path, Path], None] = os.rename,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for delay in (*_WINDOWS_RENAME_RETRY_DELAYS_SECONDS, None):
        try:
            rename(source, target)
        except OSError as error:
            if getattr(error, "winerror", None) == 5 and os.path.lexists(target):
                raise FileExistsError(
                    errno.EEXIST,
                    "destination already exists",
                    str(target),
                ) from error
            if (
                delay is None
                or getattr(error, "winerror", None) != 5
                or not os.path.lexists(source)
            ):
                raise
            sleep(delay)
        else:
            return


def _rename_directory_no_replace(source: Path, target: Path) -> None:
    """Atomically rename a directory while failing if the target exists."""

    if os.name == "nt":
        _retry_windows_no_replace_rename(source, target)
        return

    source_bytes = os.fsencode(source)
    target_bytes = os.fsencode(target)
    libc = ctypes.CDLL(None, use_errno=True)
    at_fdcwd = -100
    result: int
    if sys.platform.startswith("linux"):
        rename_at2 = getattr(libc, "renameat2", None)
        if rename_at2 is None:
            raise OSError(
                errno.ENOSYS,
                "atomic no-replace directory rename is unavailable",
                str(target),
            )
        rename_at2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_at2.restype = ctypes.c_int
        result = rename_at2(at_fdcwd, source_bytes, at_fdcwd, target_bytes, 1)
    elif sys.platform == "darwin":
        rename_atx = getattr(libc, "renameatx_np", None)
        if rename_atx is None:
            raise OSError(
                errno.ENOSYS,
                "atomic no-replace directory rename is unavailable",
                str(target),
            )
        rename_atx.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_atx.restype = ctypes.c_int
        result = rename_atx(at_fdcwd, source_bytes, at_fdcwd, target_bytes, 0x4)
    else:
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace directory rename is unavailable",
            str(target),
        )
    if result != 0:
        _raise_no_replace_error(target, ctypes.get_errno())


def _publish_bundle(output: Path, bundle: VerificationBundle) -> None:
    if output.exists() or output.is_symlink():
        raise DemoVerificationError("output already exists")
    _validate_artifact_paths(bundle.metrics)
    metrics_bytes = _canonical_json_bytes(bundle.metrics)
    audio_bytes = _canonical_json_bytes(bundle.audio)
    motion_bytes = _canonical_json_bytes(bundle.motion)
    if not bundle.contact_sheet_png:
        raise DemoVerificationError("contact sheet is empty")
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.is_dir() or parent.is_symlink():
        raise DemoVerificationError("output parent is unavailable")
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=parent))
    try:
        (staging / "metrics.json").write_bytes(metrics_bytes)
        (staging / "audio-short-windows.json").write_bytes(audio_bytes)
        (staging / "motion-residual.json").write_bytes(motion_bytes)
        (staging / "frame-contact-sheet.png").write_bytes(bundle.contact_sheet_png)
        try:
            _rename_directory_no_replace(staging, output)
        except OSError as error:
            if output.exists() or output.is_symlink():
                raise DemoVerificationError("output already exists") from error
            raise DemoVerificationError(
                "output could not be published atomically"
            ) from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _build_verification_bundle(
    *,
    source: Path,
    v14: Path,
    candidate: Path,
    clean_reference: Path,
    ffmpeg: Path,
    ffprobe: Path,
    contract: DemoContract,
    rescue_plan: Path | None,
    runner: CommandRunner,
    cancellation_callback: Callable[[], bool],
) -> VerificationBundle:
    _raise_if_cancelled(cancellation_callback)
    paths = {
        "source": source,
        "v14": v14,
        "candidate": candidate,
        "clean": clean_reference,
    }
    media = {
        label: _probe_media(
            label=label,
            path=path,
            ffprobe=ffprobe,
            ffmpeg=ffmpeg,
            runner=runner,
        )
        for label, path in paths.items()
    }
    _raise_if_cancelled(cancellation_callback)
    _validate_media_set(tuple(media.values()), contract)
    source_info = media["source"]
    clarity_index = min(
        range(source_info.frame_count),
        key=lambda index: abs(
            source_info.frame_timestamps[index] - contract.clarity_timestamp_seconds
        ),
    )
    if abs(
        source_info.frame_timestamps[clarity_index] - contract.clarity_timestamp_seconds
    ) > max(
        contract.timestamp_tolerance_seconds,
        0.5 / contract.expected_frame_rate + contract.timestamp_tolerance_seconds,
    ):
        raise DemoVerificationError("clarity evidence timestamp is unavailable")
    next_index = min(clarity_index + 1, source_info.frame_count - 1)
    clarity_timestamp = source_info.frame_timestamps[clarity_index]
    next_timestamp = source_info.frame_timestamps[next_index]
    clarity_frames = {
        label: _extract_frame(path, clarity_timestamp, media[label], ffmpeg, runner)
        for label, path in paths.items()
    }
    temporal_frames: dict[str, tuple[NDArray[np.uint8], NDArray[np.uint8]]] = {}
    for label, path in paths.items():
        temporal_frames[label] = (
            clarity_frames[label],
            _extract_frame(path, next_timestamp, media[label], ffmpeg, runner),
        )
    clarity = _measure_clarity_frames(clarity_frames, temporal_frames, contract)
    clarity["actual_timestamp_seconds"] = clarity_timestamp
    clarity["next_timestamp_seconds"] = next_timestamp

    audio_samples = {
        label: _decode_audio(path, ffmpeg, contract, runner)
        for label, path in paths.items()
    }
    _raise_if_cancelled(cancellation_callback)
    audio = _measure_audio_samples(
        audio_samples, contract.expected_sample_rate, contract
    )

    motion_timestamps = tuple(
        timestamp
        for timestamp in source_info.frame_timestamps
        if contract.motion_start_seconds <= timestamp < contract.motion_end_seconds
    )
    transition_binding = (
        _load_transition_plan_binding(
            rescue_plan,
            source_sha256=source_info.sha256,
            timestamps=motion_timestamps,
            contract=contract,
        )
        if rescue_plan is not None
        else None
    )
    motion_frames = {
        label: _decode_scaled_video(
            paths[label],
            media[label],
            ffmpeg,
            runner,
            width=contract.motion_analysis_width,
            channels=3,
            start_seconds=contract.motion_start_seconds,
            end_seconds=contract.motion_end_seconds,
        )
        for label in ("source", "candidate", "clean")
    }
    expected_plan_frames = (
        _render_transition_plan_frames(motion_frames["source"], transition_binding)
        if transition_binding is not None
        else None
    )
    _raise_if_cancelled(cancellation_callback)
    motion = _measure_motion_frames(
        timestamps=motion_timestamps,
        source_frames=motion_frames["source"],
        candidate_frames=motion_frames["candidate"],
        clean_frames=motion_frames["clean"],
        contract=contract,
        pixel_scale=source_info.width / contract.motion_analysis_width,
        expected_plan_frames=expected_plan_frames,
    )

    outside_frames = {
        label: _decode_scaled_video(
            paths[label],
            media[label],
            ffmpeg,
            runner,
            width=contract.outside_analysis_width,
            channels=1,
        )
        for label in ("source", "candidate")
    }
    _raise_if_cancelled(cancellation_callback)
    outside = _measure_outside_fidelity(
        source_frames=outside_frames["source"],
        candidate_frames=outside_frames["candidate"],
        frame_timestamps=source_info.frame_timestamps,
        source_audio=audio_samples["source"],
        candidate_audio=audio_samples["candidate"],
        sample_rate=contract.expected_sample_rate,
        contract=contract,
    )
    statuses = {
        "media_binding": "passed",
        "clarity": clarity["status"],
        "audio": audio["status"],
        "motion": motion["status"],
        "outside_fidelity": outside["status"],
    }
    overall_status = (
        "passed"
        if all(status == "passed" for status in statuses.values())
        else "needs_review"
    )
    media_document = {
        label: {
            "sha256": item.sha256,
            "duration_seconds": item.duration_seconds,
            "video_duration_seconds": item.video_duration_seconds,
            "audio_duration_seconds": item.audio_duration_seconds,
            "width": item.width,
            "height": item.height,
            "frame_rate": item.frame_rate,
            "frame_count": item.frame_count,
            "sample_rate": item.sample_rate,
            "frame_timestamps_seconds": list(item.frame_timestamps),
        }
        for label, item in media.items()
    }
    metrics: dict[str, object] = {
        "schema_version": "1",
        "tool_version": "1.0",
        "status": overall_status,
        "contract": contract.model_dump(mode="json"),
        "contract_sha256": _contract_digest(contract),
        "media": media_document,
        "checks": {
            "statuses": statuses,
            "clarity": clarity,
            "outside_fidelity": outside,
        },
        "artifacts": {
            "contact_sheet": "frame-contact-sheet.png",
            "audio": "audio-short-windows.json",
            "motion": "motion-residual.json",
        },
        "limitations": [
            "This engineering gate measures approved demo intervals only.",
            "A passed result does not replace direct user playback review.",
        ],
    }
    audio_document: dict[str, object] = {
        "schema_version": "1",
        "status": audio["status"],
        "sample_rate": contract.expected_sample_rate,
        "range_seconds": [
            contract.audio_start_seconds,
            contract.audio_end_seconds,
        ],
        "measurement": audio,
    }
    motion_document: dict[str, object] = {
        "schema_version": "1",
        "status": motion["status"],
        "range_seconds": [
            contract.motion_start_seconds,
            contract.motion_end_seconds,
        ],
        "measurement": motion,
    }
    if transition_binding is not None:
        motion_document["transition_plan_binding"] = {
            "plan_sha256": transition_binding.plan_sha256,
            "plan_digest": transition_binding.plan_digest,
            "action_id": transition_binding.action_id,
            "expected_frame_count": len(transition_binding.transforms),
        }
    return VerificationBundle(
        metrics=metrics,
        audio=audio_document,
        motion=motion_document,
        contact_sheet_png=_contact_sheet_png(clarity_frames, contract),
    )


def _run_external(
    arguments: Sequence[str],
    *,
    timeout_seconds: float = 120.0,
    maximum_output_bytes: int = 64 * 1024 * 1024,
) -> bytes:
    if not arguments:
        raise DemoVerificationError("external command is empty")
    try:
        completed = subprocess.run(
            list(arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except FileNotFoundError as error:
        raise DemoVerificationError(
            "required local media tool is unavailable"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise DemoVerificationError("local media command timed out") from error
    if completed.returncode != 0:
        raise DemoVerificationError("local media command failed")
    if (
        len(completed.stdout) > maximum_output_bytes
        or len(completed.stderr) > maximum_output_bytes
    ):
        raise DemoVerificationError("local media command output exceeded its limit")
    return completed.stdout


def _probe_media(
    *,
    label: str,
    path: Path,
    ffprobe: Path,
    ffmpeg: Path,
    runner: CommandRunner = _run_external,
) -> MediaInfo:
    before_hash = _sha256(path)
    probe_bytes = runner(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
    )
    frame_bytes = runner(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(path),
        ],
        maximum_output_bytes=16 * 1024 * 1024,
    )
    runner(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-xerror",
            "-err_detect",
            "explode",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            "-",
        ],
        timeout_seconds=300.0,
        maximum_output_bytes=1024 * 1024,
    )
    after_hash = _sha256(path)
    if after_hash != before_hash:
        raise DemoVerificationError(f"{label} changed during verification")
    return _parse_media_probe(
        label=label,
        sha256=before_hash,
        probe_bytes=probe_bytes,
        frame_bytes=frame_bytes,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_request(
    *,
    source: Path,
    v14: Path,
    candidate: Path,
    clean_reference: Path,
    output: Path,
) -> None:
    inputs = {
        "source": source,
        "v14": v14,
        "candidate": candidate,
        "clean_reference": clean_reference,
    }
    for label, path in inputs.items():
        if not path.is_file() or path.is_symlink():
            raise DemoVerificationError(f"{label} input is unavailable")
    items = tuple(inputs.items())
    for index, (first_label, first_path) in enumerate(items):
        for second_label, second_path in items[index + 1 :]:
            try:
                alias = first_path.samefile(second_path)
            except OSError as error:
                raise DemoVerificationError(
                    "input identity could not be verified"
                ) from error
            if alias:
                raise DemoVerificationError(
                    f"input alias is forbidden: {first_label}/{second_label}"
                )
    if _sha256(candidate) == _sha256(source):
        raise DemoVerificationError("candidate must differ from source bytes")
    if output.exists() or output.is_symlink():
        raise DemoVerificationError("output already exists")


def verify_demo(
    *,
    source: Path,
    v14: Path,
    candidate: Path,
    clean_reference: Path,
    output: Path,
    ffmpeg: Path,
    ffprobe: Path,
    rescue_plan: Path | None = None,
    contract: DemoContract | None = None,
    runner: CommandRunner = _run_external,
    cancellation_callback: Callable[[], bool] | None = None,
) -> DemoVerificationResult:
    """Verify four local media inputs without modifying them."""
    _validate_request(
        source=source,
        v14=v14,
        candidate=candidate,
        clean_reference=clean_reference,
        output=output,
    )
    input_paths = {
        "source": source,
        "v14": v14,
        "candidate": candidate,
        "clean": clean_reference,
    }
    if rescue_plan is not None:
        if not rescue_plan.is_file() or rescue_plan.is_symlink():
            raise DemoVerificationError("transition plan is unavailable")
        for media_path in input_paths.values():
            try:
                if os.path.samefile(rescue_plan, media_path):
                    raise DemoVerificationError(
                        "transition plan cannot alias a media input"
                    )
            except OSError as error:
                raise DemoVerificationError(
                    "transition plan identity could not be verified"
                ) from error
    bound_hashes = {label: _sha256(path) for label, path in input_paths.items()}
    bound_plan_hash = _sha256(rescue_plan) if rescue_plan is not None else None
    callback = cancellation_callback or (lambda: False)
    _raise_if_cancelled(callback)
    bundle = _build_verification_bundle(
        source=source,
        v14=v14,
        candidate=candidate,
        clean_reference=clean_reference,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        contract=contract or DemoContract(),
        rescue_plan=rescue_plan,
        runner=runner,
        cancellation_callback=callback,
    )
    _raise_if_cancelled(callback)
    for label, path in input_paths.items():
        if _sha256(path) != bound_hashes[label]:
            raise DemoVerificationError(f"{label} changed during verification")
    if (
        rescue_plan is not None
        and bound_plan_hash is not None
        and _sha256(rescue_plan) != bound_plan_hash
    ):
        raise DemoVerificationError("transition plan changed during verification")
    raw_status = bundle.metrics.get("status")
    if raw_status not in {"passed", "needs_review"}:
        raise DemoVerificationError("verification bundle has an invalid status")
    _publish_bundle(output, bundle)
    return DemoVerificationResult(status=raw_status, output=output)


def _load_test_contract(path: Path) -> DemoContract:
    if os.environ.get("VIDEOSCOPE_ALLOW_B_V15_TEST_CONTRACT") != "1":
        raise DemoVerificationError("test contract override is disabled")
    if not path.is_file() or path.is_symlink():
        raise DemoVerificationError("test contract is unavailable")
    try:
        return DemoContract.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise DemoVerificationError("test contract is invalid") from error


def main(argv: Sequence[str] | None = None) -> int:
    """Run the private V15 demo verifier."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--v14", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--clean-reference", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--ffprobe", required=True, type=Path)
    parser.add_argument("--rescue-plan", type=Path)
    parser.add_argument("--test-contract", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        contract = (
            _load_test_contract(arguments.test_contract)
            if arguments.test_contract is not None
            else DemoContract()
        )
        result = verify_demo(
            source=arguments.source,
            v14=arguments.v14,
            candidate=arguments.candidate,
            clean_reference=arguments.clean_reference,
            output=arguments.output,
            ffmpeg=arguments.ffmpeg,
            ffprobe=arguments.ffprobe,
            rescue_plan=arguments.rescue_plan,
            contract=contract,
        )
    except DemoVerificationCancelled:
        return 130
    except DemoVerificationError:
        return 2
    return 0 if result.status == "passed" else 5


if __name__ == "__main__":
    raise SystemExit(main())
