"""Bounded CPU motion assessment and streaming stabilization rendering.

This module only compensates measured camera-like affine motion.  It never
claims to restore detail or infer pixels which were not present in the source.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from queue import Queue
from typing import TYPE_CHECKING, Literal

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueMediaError,
)

if TYPE_CHECKING:
    from videoscope.rescue.executor import ExternalCommandRunner


class _StabilizationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class StabilizationConfig(_StabilizationModel):
    """Finite bounds for CPU-only affine partial motion stabilization."""

    frame_width: int = Field(default=640, gt=0, le=4096)
    frame_height: int = Field(default=360, gt=0, le=4096)
    max_features: int = Field(default=200, ge=8, le=1000)
    minimum_inlier_ratio: float = Field(default=0.55, gt=0, le=1, allow_inf_nan=False)
    maximum_residual_pixels: float = Field(
        default=3.0, gt=0, le=32, allow_inf_nan=False
    )
    max_crop_ratio: float = Field(default=0.12, gt=0, lt=0.5, allow_inf_nan=False)
    smoothing_window_samples: int = Field(default=5, ge=1, le=31)
    queue_capacity: int = Field(default=4, ge=1, le=32)
    maximum_timeline_gap_seconds: float = Field(
        default=1.0, gt=0, le=10, allow_inf_nan=False
    )


class MotionTransform(_StabilizationModel):
    """Measured partial-affine transform between adjacent downscaled frames."""

    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    rotation_degrees: float = Field(ge=-45, le=45, allow_inf_nan=False)
    scale: float = Field(gt=0.5, le=1.5, allow_inf_nan=False)
    translation_x: float = Field(ge=-4096, le=4096, allow_inf_nan=False)
    translation_y: float = Field(ge=-4096, le=4096, allow_inf_nan=False)
    inlier_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    residual_pixels: float = Field(ge=0, allow_inf_nan=False)
    scene_boundary: bool = False
    semantics: Literal["adjacent_motion", "frame_correction"] = "adjacent_motion"


class StabilizationAssessment(_StabilizationModel):
    """A conservative recommendation with exact measured limits."""

    recommended: bool
    reason: str = Field(min_length=1)
    crop_ratio: float = Field(ge=0, lt=1, allow_inf_nan=False)
    transforms: tuple[MotionTransform, ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_neutral_fallback(self) -> StabilizationAssessment:
        if not self.recommended and self.parameters:
            raise ValueError("a rejected stabilization assessment must be neutral")
        return self


FeatureEstimator = Callable[
    [np.ndarray, np.ndarray], tuple[float, float, float, float, float, float] | None
]


def estimate_motion_transforms(
    frames: Iterable[tuple[float, np.ndarray]],
    config: StabilizationConfig,
    *,
    scene_boundaries: Sequence[float] = (),
    estimator: FeatureEstimator | None = None,
) -> tuple[MotionTransform, ...]:
    """Estimate adjacent partial-affine transforms with an injectable pure seam.

    ``estimator`` receives two downscaled grayscale arrays and returns
    ``(rotation, scale, tx, ty, inlier_ratio, residual)``.  Production defaults
    to OpenCV feature tracking and RANSAC, while tests can remain fully local.
    """
    ordered = tuple(sorted(frames, key=lambda item: item[0]))
    if any(not math.isfinite(timestamp) or timestamp < 0 for timestamp, _ in ordered):
        raise ValueError("frame timestamps must be finite and non-negative")
    if any(
        ordered[index][0] <= ordered[index - 1][0] for index in range(1, len(ordered))
    ):
        raise ValueError("frame timestamps must be strictly increasing")
    if len(ordered) < 2:
        return ()
    boundary_set = tuple(sorted(scene_boundaries))
    estimate = estimator or _opencv_affine_estimator(config)
    transforms: list[MotionTransform] = []
    for (previous_time, previous), (timestamp, current) in zip(ordered, ordered[1:]):
        boundary = any(previous_time < item <= timestamp for item in boundary_set)
        measured = (
            None if boundary else estimate(_grayscale(previous), _grayscale(current))
        )
        if measured is None:
            transforms.append(
                MotionTransform(
                    timestamp_seconds=timestamp,
                    rotation_degrees=0.0,
                    scale=1.0,
                    translation_x=0.0,
                    translation_y=0.0,
                    inlier_ratio=0.0,
                    residual_pixels=4096.0,
                    scene_boundary=boundary,
                )
            )
            continue
        rotation, scale, tx, ty, inliers, residual = measured
        transforms.append(
            MotionTransform(
                timestamp_seconds=timestamp,
                rotation_degrees=rotation,
                scale=scale,
                translation_x=tx,
                translation_y=ty,
                inlier_ratio=inliers,
                residual_pixels=residual,
                scene_boundary=boundary,
            )
        )
    return tuple(transforms)


def smooth_motion_transforms(
    transforms: Sequence[MotionTransform], *, window_size: int
) -> tuple[MotionTransform, ...]:
    """Return timestamped frame corrections from scene-local cumulative paths."""
    if window_size < 1 or window_size % 2 == 0:
        raise ValueError("window_size must be an odd positive integer")
    ordered = tuple(transforms)
    if not ordered:
        return ()
    if any(item.semantics != "adjacent_motion" for item in ordered):
        raise ValueError("smoothing requires adjacent measured motion")
    result: list[MotionTransform] = []
    start = 0
    for index, transform in enumerate(ordered):
        if transform.scene_boundary:
            result.extend(_smooth_segment(ordered[start:index], window_size))
            result.append(
                _neutral_correction(
                    transform.timestamp_seconds, scene_boundary=True
                ).model_copy(
                    update={
                        "inlier_ratio": transform.inlier_ratio,
                        "residual_pixels": transform.residual_pixels,
                    }
                )
            )
            start = index + 1
    result.extend(_smooth_segment(ordered[start:], window_size))
    return tuple(result)


def motion_correction_at_timestamp(
    corrections: Sequence[MotionTransform],
    timestamp_seconds: float,
    *,
    maximum_gap_seconds: float,
) -> MotionTransform:
    """Interpolate a reviewed correction timeline without crossing cuts or gaps."""
    if not math.isfinite(timestamp_seconds) or timestamp_seconds < 0:
        raise ValueError("frame timestamp must be finite and non-negative")
    if not math.isfinite(maximum_gap_seconds) or maximum_gap_seconds <= 0:
        raise ValueError("maximum gap must be finite and positive")
    ordered = tuple(corrections)
    if not ordered:
        raise RescueMediaError("stabilization correction timeline is empty")
    if any(item.semantics != "frame_correction" for item in ordered):
        raise RescueMediaError(
            "stabilization timeline does not contain frame corrections"
        )
    if any(
        ordered[index].timestamp_seconds <= ordered[index - 1].timestamp_seconds
        for index in range(1, len(ordered))
    ):
        raise RescueMediaError("stabilization correction timeline is not ordered")
    first = ordered[0]
    if timestamp_seconds < first.timestamp_seconds:
        if first.timestamp_seconds - timestamp_seconds > maximum_gap_seconds:
            raise RescueMediaError("stabilization timeline does not cover the frame")
        return _neutral_correction(timestamp_seconds)
    last = ordered[-1]
    if timestamp_seconds > last.timestamp_seconds:
        if timestamp_seconds - last.timestamp_seconds > maximum_gap_seconds:
            raise RescueMediaError("stabilization timeline does not cover the frame")
        return last.model_copy(update={"timestamp_seconds": timestamp_seconds})
    for left, right in zip(ordered, ordered[1:]):
        if timestamp_seconds == left.timestamp_seconds:
            return left
        if left.timestamp_seconds < timestamp_seconds <= right.timestamp_seconds:
            gap = right.timestamp_seconds - left.timestamp_seconds
            if gap > maximum_gap_seconds:
                raise RescueMediaError(
                    "stabilization correction timeline contains a gap"
                )
            if left.scene_boundary or right.scene_boundary:
                return _neutral_correction(timestamp_seconds, scene_boundary=True)
            fraction = (timestamp_seconds - left.timestamp_seconds) / gap
            return MotionTransform(
                timestamp_seconds=timestamp_seconds,
                rotation_degrees=_lerp(
                    left.rotation_degrees, right.rotation_degrees, fraction
                ),
                scale=_lerp(left.scale, right.scale, fraction),
                translation_x=_lerp(left.translation_x, right.translation_x, fraction),
                translation_y=_lerp(left.translation_y, right.translation_y, fraction),
                inlier_ratio=min(left.inlier_ratio, right.inlier_ratio),
                residual_pixels=max(left.residual_pixels, right.residual_pixels),
                semantics="frame_correction",
            )
    return last


def validate_source_frame_timestamps(
    timestamps: Sequence[float], *, expected_count: int | None = None
) -> tuple[float, ...]:
    """Validate exact decoded source PTS without assuming constant frame rate."""
    values = tuple(timestamps)
    if expected_count is not None and len(values) != expected_count:
        raise RescueMediaError(
            "source frame timestamp count does not match decoded frames"
        )
    if not values:
        raise RescueMediaError("source frame timestamps are unavailable")
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
        for value in values
    ):
        raise RescueMediaError(
            "source frame timestamps must be finite and non-negative"
        )
    normalized = tuple(float(value) for value in values)
    if any(
        normalized[index] <= normalized[index - 1]
        for index in range(1, len(normalized))
    ):
        raise RescueMediaError("source frame timestamps must be strictly increasing")
    return normalized


def motion_corrections_for_timestamps(
    corrections: Sequence[MotionTransform],
    timestamps: Sequence[float],
    *,
    maximum_gap_seconds: float,
) -> tuple[MotionTransform, ...]:
    """Select reviewed corrections against exact, possibly irregular source PTS."""
    exact_timestamps = validate_source_frame_timestamps(timestamps)
    return tuple(
        motion_correction_at_timestamp(
            corrections,
            timestamp,
            maximum_gap_seconds=maximum_gap_seconds,
        )
        for timestamp in exact_timestamps
    )


def require_cfr_source_timestamps(
    timestamps: Sequence[float], *, nominal_fps: float, expected_count: int
) -> tuple[float, ...]:
    exact = validate_source_frame_timestamps(timestamps, expected_count=expected_count)
    if not math.isfinite(nominal_fps) or nominal_fps <= 0:
        raise RescueMediaError("source nominal frame rate is invalid")
    expected_step = 1.0 / nominal_fps
    tolerance = max(0.001, expected_step * 0.02)
    if exact[0] > tolerance:
        raise RescueMediaError("non-zero source video start time cannot be preserved")
    if any(
        not math.isclose(
            right - left,
            expected_step,
            rel_tol=0.0,
            abs_tol=tolerance,
        )
        for left, right in zip(exact, exact[1:])
    ):
        raise RescueMediaError(
            "variable source frame timing is not supported by the bounded renderer"
        )
    return exact


def assess_stabilization(
    transforms: Sequence[MotionTransform], config: StabilizationConfig
) -> StabilizationAssessment:
    """Reject unreliable/crop-heavy motion and expose only bounded parameters."""
    measured = tuple(transforms)
    if not measured:
        return StabilizationAssessment(
            recommended=False, reason="no_motion_measurements", crop_ratio=0.0
        )
    if any(item.semantics != "adjacent_motion" for item in measured):
        raise ValueError("stabilization assessment requires adjacent measured motion")
    within_scenes = tuple(item for item in measured if not item.scene_boundary)
    if not within_scenes:
        return StabilizationAssessment(
            recommended=False,
            reason="scene_boundary",
            crop_ratio=0.0,
            transforms=measured,
        )
    if any(item.inlier_ratio < config.minimum_inlier_ratio for item in within_scenes):
        return StabilizationAssessment(
            recommended=False,
            reason="low_inlier_ratio",
            crop_ratio=0.0,
            transforms=measured,
        )
    if any(
        item.residual_pixels > config.maximum_residual_pixels for item in within_scenes
    ):
        return StabilizationAssessment(
            recommended=False,
            reason="high_residual",
            crop_ratio=0.0,
            transforms=measured,
        )
    crop_ratio = _required_crop_ratio(within_scenes, config)
    if crop_ratio > config.max_crop_ratio:
        return StabilizationAssessment(
            recommended=False,
            reason="crop_budget_exceeded",
            crop_ratio=crop_ratio,
            transforms=measured,
        )
    smoothed = smooth_motion_transforms(
        measured, window_size=config.smoothing_window_samples | 1
    )
    return StabilizationAssessment(
        recommended=True,
        reason="measured_affine_motion",
        crop_ratio=crop_ratio,
        transforms=smoothed,
        parameters={
            "crop_ratio": crop_ratio,
            "max_crop_ratio": config.max_crop_ratio,
            "frame_height": config.frame_height,
            "frame_width": config.frame_width,
            "maximum_timeline_gap_seconds": config.maximum_timeline_gap_seconds,
            "smoothing_window_samples": config.smoothing_window_samples | 1,
        },
    )


def render_stabilized_video(
    source: Path,
    output: Path,
    transforms: Sequence[MotionTransform],
    config: StabilizationConfig,
    *,
    runner: ExternalCommandRunner,
    cancellation_callback: Callable[[], bool],
    ffmpeg: str = "ffmpeg",
    timeout_seconds: float = 3600.0,
    frame_timestamps: Sequence[float] | None = None,
) -> None:
    """Render one frame at a time, then mux unchanged source audio via ``runner``."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    source, output = Path(source), Path(output)
    if not source.is_file():
        raise RescueArtifactError("stabilization source must be an existing file")
    if _paths_alias(source, output):
        raise RescueArtifactError("stabilization output must not alias the source")
    if output.exists() or output.is_symlink():
        raise RescueArtifactError("stabilization output must not already exist")
    corrections = tuple(transforms)
    if not corrections or any(
        item.semantics != "frame_correction" for item in corrections
    ):
        raise RescueMediaError("stabilization requires reviewed frame corrections")
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - package dependency boundary
        raise RescueMediaError("OpenCV is required for CPU stabilization") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="videoscope-stabilize-", dir=output.parent
    ) as temp_name:
        intermediate = Path(temp_name) / "video-only.mp4"
        muxed = Path(temp_name) / "stabilized-with-audio.mp4"
        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            raise RescueMediaError("source could not be opened for stabilization")
        try:
            fps = float(capture.get(cv2.CAP_PROP_FPS))
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
            if not math.isfinite(fps) or fps <= 0 or width <= 0 or height <= 0:
                raise RescueMediaError("source has invalid video dimensions or rate")
            provided_timestamps = (
                require_cfr_source_timestamps(
                    frame_timestamps,
                    nominal_fps=fps,
                    expected_count=len(frame_timestamps),
                )
                if frame_timestamps is not None
                else None
            )
            fourcc = int(getattr(cv2, "VideoWriter_fourcc")(*"mp4v"))
            writer = cv2.VideoWriter(str(intermediate), fourcc, fps, (width, height))
            if not writer.isOpened():
                raise RescueMediaError("stabilized video writer could not be opened")
            try:
                frame_index = 0
                observed_timestamps: list[float] = []
                frame_queue: Queue[np.ndarray] = Queue(maxsize=config.queue_capacity)
                while True:
                    if cancellation_callback():
                        raise RescueCancelledError("stabilization cancelled")
                    ok, frame = capture.read()
                    if not ok:
                        break
                    if provided_timestamps is not None:
                        if frame_index >= len(provided_timestamps):
                            raise RescueMediaError(
                                "source frame timestamp count does not match "
                                "decoded frames"
                            )
                        timestamp = provided_timestamps[frame_index]
                    else:
                        timestamp = float(capture.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
                    observed_timestamps.append(timestamp)
                    frame_queue.put(frame)
                    queued_frame = frame_queue.get()
                    correction = motion_correction_at_timestamp(
                        corrections,
                        timestamp,
                        maximum_gap_seconds=config.maximum_timeline_gap_seconds,
                    )
                    if not _is_neutral_correction(correction):
                        queued_frame = cv2.warpAffine(
                            queued_frame,
                            _affine_matrix(correction, width, height),
                            (width, height),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE,
                        )
                    writer.write(queued_frame)
                    frame_index += 1
                if provided_timestamps is not None:
                    validate_source_frame_timestamps(
                        provided_timestamps,
                        expected_count=frame_index,
                    )
                require_cfr_source_timestamps(
                    observed_timestamps,
                    nominal_fps=fps,
                    expected_count=frame_index,
                )
            finally:
                writer.release()
        finally:
            capture.release()
        result = runner(
            (
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(intermediate),
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "1:a?",
                "-c:v",
                "libx264",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                "-fps_mode",
                "passthrough",
                str(muxed),
            ),
            timeout_seconds=timeout_seconds,
            sensitive_paths=(source, output, intermediate, muxed),
            cancellation_callback=cancellation_callback,
        )
        if result.returncode != 0:
            raise RescueMediaError(
                "stabilized audio mux failed: " + result.stderr_summary
            )
        if cancellation_callback():
            raise RescueCancelledError("stabilization cancelled before publication")
        try:
            if not muxed.is_file() or muxed.stat().st_size <= 0:
                raise RescueMediaError("stabilized audio mux produced no media")
            if output.exists() or output.is_symlink():
                raise RescueArtifactError(
                    "stabilization output appeared before publication"
                )
            muxed.replace(output)
        except (RescueArtifactError, RescueMediaError):
            raise
        except OSError as exc:
            raise RescueArtifactError(
                "stabilized output could not be published atomically"
            ) from exc


def _smooth_segment(
    segment: Sequence[MotionTransform], window_size: int
) -> list[MotionTransform]:
    if not segment:
        return []
    radius = window_size // 2
    cumulative: list[NDArray[np.float64]] = []
    camera_path: NDArray[np.float64] = np.eye(3, dtype=np.float64)
    for item in segment:
        camera_path = _motion_matrix(item) @ camera_path
        cumulative.append(camera_path.copy())
    components = tuple(_decompose_motion(matrix) for matrix in cumulative)
    result: list[MotionTransform] = []
    for index, item in enumerate(segment):
        if index < radius or index + radius >= len(segment):
            correction: NDArray[np.float64] = np.eye(3, dtype=np.float64)
        else:
            nearby = components[index - radius : index + radius + 1]
            smoothed = _matrix_from_components(
                rotation_degrees=float(np.median([value[0] for value in nearby])),
                scale=float(np.median([value[1] for value in nearby])),
                translation_x=float(np.median([value[2] for value in nearby])),
                translation_y=float(np.median([value[3] for value in nearby])),
            )
            correction = smoothed @ np.linalg.inv(cumulative[index])
        rotation, scale, translation_x, translation_y = _decompose_motion(correction)
        result.append(
            MotionTransform(
                timestamp_seconds=item.timestamp_seconds,
                rotation_degrees=rotation,
                scale=scale,
                translation_x=translation_x,
                translation_y=translation_y,
                inlier_ratio=item.inlier_ratio,
                residual_pixels=item.residual_pixels,
                semantics="frame_correction",
            )
        )
    return result


def _motion_matrix(transform: MotionTransform) -> NDArray[np.float64]:
    return _matrix_from_components(
        rotation_degrees=transform.rotation_degrees,
        scale=transform.scale,
        translation_x=transform.translation_x,
        translation_y=transform.translation_y,
    )


def _matrix_from_components(
    *,
    rotation_degrees: float,
    scale: float,
    translation_x: float,
    translation_y: float,
) -> NDArray[np.float64]:
    radians = math.radians(rotation_degrees)
    cosine = math.cos(radians) * scale
    sine = math.sin(radians) * scale
    return np.array(
        [
            [cosine, -sine, translation_x],
            [sine, cosine, translation_y],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def _decompose_motion(
    matrix: NDArray[np.float64],
) -> tuple[float, float, float, float]:
    scale = float(math.hypot(float(matrix[0, 0]), float(matrix[1, 0])))
    rotation = math.degrees(math.atan2(float(matrix[1, 0]), float(matrix[0, 0])))
    return rotation, scale, float(matrix[0, 2]), float(matrix[1, 2])


def _required_crop_ratio(
    transforms: Sequence[MotionTransform], config: StabilizationConfig
) -> float:
    x = max(abs(item.translation_x) for item in transforms) / config.frame_width
    y = max(abs(item.translation_y) for item in transforms) / config.frame_height
    rotation = max(abs(item.rotation_degrees) for item in transforms) / 180.0
    scale = max(abs(item.scale - 1.0) for item in transforms)
    return min(0.999999, max(x, y) + rotation + scale)


def _grayscale(frame: np.ndarray) -> NDArray[np.uint8]:
    array = np.asarray(frame)
    if array.ndim == 2:
        return np.asarray(array, dtype=np.uint8)
    if array.ndim == 3 and array.shape[2] >= 3:
        return np.asarray(np.mean(array[..., :3], axis=2), dtype=np.uint8)
    raise ValueError("motion frames must be grayscale or color images")


def _opencv_affine_estimator(config: StabilizationConfig) -> FeatureEstimator:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - package dependency boundary
        raise RescueMediaError("OpenCV is required for CPU stabilization") from exc

    def estimate(
        previous: np.ndarray, current: np.ndarray
    ) -> tuple[float, float, float, float, float, float] | None:
        points = cv2.goodFeaturesToTrack(
            previous, maxCorners=config.max_features, qualityLevel=0.01, minDistance=5
        )
        if points is None or len(points) < 3:
            return None
        tracked, status, _errors = cv2.calcOpticalFlowPyrLK(
            previous, current, points, None
        )
        if tracked is None or status is None:
            return None
        mask = status.reshape(-1).astype(bool)
        if int(mask.sum()) < 3:
            return None
        matrix, inliers = cv2.estimateAffinePartial2D(
            points[mask], tracked[mask], method=cv2.RANSAC
        )
        if matrix is None or inliers is None:
            return None
        selected = inliers.reshape(-1).astype(bool)
        ratio = float(selected.mean())
        source = points[mask].reshape(-1, 2)[selected]
        target = tracked[mask].reshape(-1, 2)[selected]
        projected = source @ matrix[:, :2].T + matrix[:, 2]
        residual = (
            float(np.mean(np.linalg.norm(projected - target, axis=1)))
            if len(source)
            else math.inf
        )
        scale = float(math.hypot(float(matrix[0, 0]), float(matrix[1, 0])))
        rotation = math.degrees(math.atan2(float(matrix[1, 0]), float(matrix[0, 0])))
        return (
            rotation,
            scale,
            float(matrix[0, 2]),
            float(matrix[1, 2]),
            ratio,
            residual,
        )

    return estimate


def _affine_matrix(
    transform: MotionTransform, width: int, height: int
) -> NDArray[np.float32]:
    del width, height
    return np.asarray(_motion_matrix(transform)[:2, :], dtype=np.float32)


def _neutral_correction(
    timestamp_seconds: float, *, scene_boundary: bool = False
) -> MotionTransform:
    return MotionTransform(
        timestamp_seconds=timestamp_seconds,
        rotation_degrees=0.0,
        scale=1.0,
        translation_x=0.0,
        translation_y=0.0,
        inlier_ratio=1.0,
        residual_pixels=0.0,
        scene_boundary=scene_boundary,
        semantics="frame_correction",
    )


def _is_neutral_correction(transform: MotionTransform) -> bool:
    return (
        math.isclose(transform.rotation_degrees, 0.0, abs_tol=1e-12)
        and math.isclose(transform.scale, 1.0, abs_tol=1e-12)
        and math.isclose(transform.translation_x, 0.0, abs_tol=1e-12)
        and math.isclose(transform.translation_y, 0.0, abs_tol=1e-12)
    )


def _lerp(left: float, right: float, fraction: float) -> float:
    return left + (right - left) * fraction


def _paths_alias(left: Path, right: Path) -> bool:
    if os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(
        str(right.resolve(strict=False))
    ):
        return True
    try:
        return os.path.samefile(left, right)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RescueArtifactError(
            "stabilization path identity could not be checked"
        ) from exc


__all__ = [
    "MotionTransform",
    "StabilizationAssessment",
    "StabilizationConfig",
    "assess_stabilization",
    "estimate_motion_transforms",
    "motion_correction_at_timestamp",
    "motion_corrections_for_timestamps",
    "render_stabilized_video",
    "require_cfr_source_timestamps",
    "smooth_motion_transforms",
    "validate_source_frame_timestamps",
]
