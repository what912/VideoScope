"""Conservative CPU-only blur measurement and single-frame restoration.

The estimator accepts pixels only and deliberately returns no estimate when
detail, temporal consistency, or side-effect evidence is insufficient.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Callable, Sequence
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

import cv2
import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoscope.rescue.encoding import canonical_video_encode_arguments
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueInputError,
    RescueMediaError,
)
from videoscope.rescue.models import RescueEffectiveConfig
from videoscope.rescue.timeline import normalize_actual_video_timestamps

if TYPE_CHECKING:
    from videoscope.rescue.executor import CommandResult, ExternalCommandRunner

KernelKind = Literal["box", "gaussian"]
_RENDER_TIMEOUT_SECONDS: Final = 3600.0
_TIMING_TOLERANCE_SECONDS: Final = 0.002
_MAX_CFR_TIMING_OUTPUT_BYTES: Final = 60 * 1024
_MAX_CFR_FRAME_INVENTORY: Final = 4096


class _DeblurModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DeblurConfig(_DeblurModel):
    """Complete bounded candidate space and conservative acceptance gates."""

    candidate_kernel_kinds: tuple[KernelKind, ...] = ("box", "gaussian")
    candidate_radii: tuple[int, ...] = (1, 2, 3, 4, 5)
    candidate_regularizations: tuple[float, ...] = (0.001, 0.003, 0.01, 0.03)
    candidate_ringing_suppression_strengths: tuple[float, ...] = (0.0, 1.0)
    ringing_suppression_feather_pixels: int = Field(default=1, ge=0, le=8)
    maximum_observations: int = Field(default=16, ge=1, le=16)
    minimum_frame_dimension: int = Field(default=32, ge=8, le=256)
    minimum_luma_standard_deviation: float = Field(
        default=8.0, gt=0, le=64, allow_inf_nan=False
    )
    edge_gradient_threshold: float = Field(
        default=10.0, gt=0, le=128, allow_inf_nan=False
    )
    minimum_edge_pixels: int = Field(default=180, ge=8, le=100_000)
    maximum_supported_edge_width: float = Field(
        default=24.0, gt=1, le=32, allow_inf_nan=False
    )
    maximum_edge_width_ratio: float = Field(
        default=0.72, gt=0, lt=1, allow_inf_nan=False
    )
    minimum_edge_continuity_ratio: float = Field(
        default=0.72, gt=0, le=1, allow_inf_nan=False
    )
    maximum_reblur_error_ratio: float = Field(
        default=0.055, gt=0, le=1, allow_inf_nan=False
    )
    maximum_ringing_ratio: float = Field(default=0.08, ge=0, le=1, allow_inf_nan=False)
    ringing_tolerance: float = Field(default=8.0, ge=0, le=64, allow_inf_nan=False)
    maximum_noise_gain_ratio: float = Field(
        default=3.5, gt=1, le=20, allow_inf_nan=False
    )
    noise_measurement_margin_pixels: int = Field(default=3, ge=0, le=32)
    maximum_temporal_change_ratio: float = Field(
        default=0.16, gt=0, le=1, allow_inf_nan=False
    )
    maximum_alignment_shift_pixels: float = Field(
        default=3.0, ge=0, le=32, allow_inf_nan=False
    )
    minimum_alignment_response: float = Field(
        default=0.20, ge=0, le=1, allow_inf_nan=False
    )
    reflect_padding_pixels: int = Field(default=24, ge=4, le=128)
    edge_taper_pixels: int = Field(default=12, ge=0, le=64)
    score_reblur_weight: float = Field(default=1.0, gt=0, le=10, allow_inf_nan=False)
    score_width_weight: float = Field(default=0.35, ge=0, le=10, allow_inf_nan=False)
    score_side_effect_weight: float = Field(
        default=0.15, ge=0, le=10, allow_inf_nan=False
    )
    boundary_transition_seconds: float = Field(
        default=0.15, gt=0, le=2.0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def _validate_candidates(self) -> DeblurConfig:
        if not self.candidate_kernel_kinds:
            raise ValueError("candidate kernel kinds must not be empty")
        if len(set(self.candidate_kernel_kinds)) != len(self.candidate_kernel_kinds):
            raise ValueError("candidate kernel kinds must be unique")
        if not self.candidate_radii:
            raise ValueError("candidate radii must not be empty")
        if any(radius < 1 or radius > 5 for radius in self.candidate_radii):
            raise ValueError("candidate radii must be integers from 1 through 5")
        if len(set(self.candidate_radii)) != len(self.candidate_radii):
            raise ValueError("candidate radii must be unique")
        if not self.candidate_regularizations:
            raise ValueError("candidate regularizations must not be empty")
        if any(
            not np.isfinite(value) or value <= 0 or value > 1
            for value in self.candidate_regularizations
        ):
            raise ValueError("candidate regularizations must be in (0, 1]")
        if len(set(self.candidate_regularizations)) != len(
            self.candidate_regularizations
        ):
            raise ValueError("candidate regularizations must be unique")
        if not self.candidate_ringing_suppression_strengths:
            raise ValueError(
                "candidate ringing suppression strengths must not be empty"
            )
        if any(
            not np.isfinite(value) or value < 0 or value > 1
            for value in self.candidate_ringing_suppression_strengths
        ):
            raise ValueError(
                "candidate ringing suppression strengths must be in [0, 1]"
            )
        if len(set(self.candidate_ringing_suppression_strengths)) != len(
            self.candidate_ringing_suppression_strengths
        ):
            raise ValueError("candidate ringing suppression strengths must be unique")
        if self.candidate_ringing_suppression_strengths[0] != 0.0:
            raise ValueError(
                "candidate ringing suppression strengths must start with 0"
            )
        if self.edge_taper_pixels > self.reflect_padding_pixels:
            raise ValueError("edge taper cannot exceed reflect padding")
        return self


class BlurKernelEstimate(_DeblurModel):
    """Path-free measurements for one accepted restoration candidate."""

    kernel_kind: KernelKind
    radius: int = Field(ge=1, le=5)
    regularization: float = Field(gt=0, le=1, allow_inf_nan=False)
    ringing_suppression_strength: float = Field(
        default=0.0, ge=0, le=1, allow_inf_nan=False
    )
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    edge_width_before: float = Field(gt=0, allow_inf_nan=False)
    predicted_edge_width_after: float = Field(gt=0, allow_inf_nan=False)
    edge_continuity_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    reblur_error_ratio: float = Field(ge=0, allow_inf_nan=False)
    ringing_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    noise_gain_ratio: float = Field(ge=0, allow_inf_nan=False)
    temporal_change_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)


def estimate_blur_kernel(
    frames: Sequence[NDArray[np.generic]], config: DeblurConfig
) -> BlurKernelEstimate | None:
    """Choose the first lowest-scoring candidate that passes every hard gate."""
    observation_count = len(frames)
    if observation_count == 0:
        raise ValueError("at least one frame is required")
    if observation_count > config.maximum_observations:
        return None
    observations = tuple(_validated_luma(frame) for frame in frames)
    if any(not np.all(np.isfinite(frame)) for frame in observations):
        return None
    shape = observations[0].shape
    if any(frame.shape != shape for frame in observations):
        raise ValueError("all frames must have the same shape")
    if min(shape) < config.minimum_frame_dimension:
        return None
    selected = _select_temporally_consistent_observations(observations, config)
    if selected is None:
        return None
    reference = selected[0]
    if float(np.std(reference)) < config.minimum_luma_standard_deviation:
        return None
    edge_mask = _edge_mask(reference, config)
    if int(np.count_nonzero(edge_mask)) < config.minimum_edge_pixels:
        return None
    width_before = measure_edge_spread_width(reference, config)
    if (
        not np.isfinite(width_before)
        or width_before > config.maximum_supported_edge_width
    ):
        return None
    temporal_change = _temporal_change(selected, config)
    if (
        temporal_change is None
        or temporal_change > config.maximum_temporal_change_ratio
    ):
        return None

    accepted: list[tuple[float, int, BlurKernelEstimate]] = []
    order = 0
    for kind in config.candidate_kernel_kinds:
        for radius in config.candidate_radii:
            kernel = _kernel(kind, radius)
            for regularization in config.candidate_regularizations:
                for (
                    suppression_strength
                ) in config.candidate_ringing_suppression_strengths:
                    restored, raw = _restore_luma(
                        reference,
                        kernel,
                        regularization,
                        config,
                        ringing_suppression_strength=suppression_strength,
                    )
                    restored_observations = [restored]
                    for observation in selected[1:]:
                        restored_observation, _observation_raw = _restore_luma(
                            observation,
                            kernel,
                            regularization,
                            config,
                            ringing_suppression_strength=suppression_strength,
                        )
                        restored_observations.append(restored_observation)
                    try:
                        observable = _decoded_observable_candidate_metrics(
                            selected, tuple(restored_observations), config
                        )
                    except ValueError:
                        order += 1
                        continue
                    candidate_temporal_change = observable["temporal_change_ratio"]
                    width_after = measure_edge_spread_width(restored, config)
                    continuity = _edge_continuity(edge_mask, restored, kernel, config)
                    reblur_error = _reblur_error(reference, restored, kernel, edge_mask)
                    ringing = _ringing_ratio(reference, raw, edge_mask, config)
                    noise_gain = _noise_gain(
                        reference, restored, edge_mask, kernel, config
                    )
                    failed_gates = frozenset(
                        name
                        for name, failed in (
                            (
                                "width",
                                width_after
                                >= config.maximum_edge_width_ratio * width_before,
                            ),
                            (
                                "observable_width",
                                observable["edge_width_ratio"]
                                > config.maximum_edge_width_ratio,
                            ),
                            (
                                "continuity",
                                continuity < config.minimum_edge_continuity_ratio,
                            ),
                            (
                                "observable_continuity",
                                observable["edge_continuity_ratio"]
                                < config.minimum_edge_continuity_ratio,
                            ),
                            (
                                "reblur",
                                reblur_error > config.maximum_reblur_error_ratio,
                            ),
                            ("ringing", ringing > config.maximum_ringing_ratio),
                            (
                                "observable_ringing",
                                observable["ringing_ratio"]
                                > config.maximum_ringing_ratio,
                            ),
                            ("noise", noise_gain > config.maximum_noise_gain_ratio),
                            (
                                "observable_noise",
                                observable["noise_gain_ratio"]
                                > config.maximum_noise_gain_ratio,
                            ),
                            (
                                "temporal",
                                candidate_temporal_change
                                > config.maximum_temporal_change_ratio,
                            ),
                        )
                        if failed
                    )
                    if failed_gates:
                        order += 1
                        continue
                    score = (
                        config.score_reblur_weight * reblur_error
                        + config.score_width_weight * (width_after / width_before)
                        + config.score_side_effect_weight
                        * (
                            observable["ringing_ratio"]
                            + observable["noise_gain_ratio"] / 10.0
                        )
                    )
                    confidence = float(np.clip(1.0 - score, 0.0, 1.0))
                    accepted.append(
                        (
                            score,
                            order,
                            BlurKernelEstimate(
                                kernel_kind=kind,
                                radius=radius,
                                regularization=regularization,
                                ringing_suppression_strength=suppression_strength,
                                confidence=confidence,
                                edge_width_before=width_before,
                                predicted_edge_width_after=width_after,
                                edge_continuity_ratio=min(
                                    continuity,
                                    observable["edge_continuity_ratio"],
                                ),
                                reblur_error_ratio=reblur_error,
                                ringing_ratio=max(ringing, observable["ringing_ratio"]),
                                noise_gain_ratio=max(
                                    noise_gain, observable["noise_gain_ratio"]
                                ),
                                temporal_change_ratio=candidate_temporal_change,
                            ),
                        )
                    )
                    order += 1
    if not accepted:
        return None
    accepted.sort(key=lambda item: (item[0], item[1]))
    return accepted[0][2]


def _decoded_observable_candidate_metrics(
    source_frames: Sequence[NDArray[np.generic]],
    candidate_frames: Sequence[NDArray[np.generic]],
    config: DeblurConfig,
) -> dict[str, float]:
    """Aggregate verification-equivalent decoded-pixel gates independently."""
    sources = tuple(source_frames)
    candidates = tuple(candidate_frames)
    if (
        not sources
        or len(sources) != len(candidates)
        or len(sources) > config.maximum_observations
    ):
        raise ValueError("decoded-observable frame inventories are invalid")
    widths: list[float] = []
    continuities: list[float] = []
    ringing: list[float] = []
    noise: list[float] = []
    temporal: list[float] = []
    previous_residual: NDArray[np.float32] | None = None
    for source_frame, candidate_frame in zip(sources, candidates, strict=True):
        metrics = _decoded_observable_pair_metrics(source_frame, candidate_frame)
        widths.append(metrics["edge_width_ratio"])
        continuities.append(metrics["edge_continuity_ratio"])
        ringing.append(metrics["ringing_ratio"])
        noise.append(metrics["noise_gain_ratio"])
        source = _observable_gray(source_frame)
        candidate = _observable_gray(candidate_frame)
        residual = (candidate - source) / 255.0
        if previous_residual is not None:
            temporal.append(
                float(np.mean(np.abs(residual - previous_residual), dtype=np.float64))
            )
        previous_residual = residual
    return {
        "edge_width_ratio": float(np.median(widths)),
        "edge_continuity_ratio": float(np.percentile(continuities, 10)),
        "ringing_ratio": float(np.percentile(ringing, 95)),
        "noise_gain_ratio": float(np.percentile(noise, 95)),
        "temporal_change_ratio": (
            float(np.percentile(temporal, 95)) if temporal else 0.0
        ),
    }


def _decoded_observable_pair_metrics(
    source_frame: NDArray[np.generic], candidate_frame: NDArray[np.generic]
) -> dict[str, float]:
    """Measure one decoded pair without importing verifier implementation."""
    source = _observable_gray(source_frame)
    candidate = _observable_gray(candidate_frame)
    if source.shape != candidate.shape:
        raise ValueError("decoded-observable frame shapes differ")
    width_ratios: list[float] = []
    continuity_ratios: list[float] = []
    base_edges: NDArray[np.bool_] | None = None
    base_mask: NDArray[np.bool_] | None = None
    for sigma in (0.0, 1.0, 2.0):
        source_scale = (
            source if sigma == 0.0 else cv2.GaussianBlur(source, (0, 0), sigma)
        )
        candidate_scale = (
            candidate if sigma == 0.0 else cv2.GaussianBlur(candidate, (0, 0), sigma)
        )
        source_gradient = cv2.magnitude(
            cv2.Sobel(source_scale, cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(source_scale, cv2.CV_32F, 0, 1, ksize=3),
        )
        candidate_gradient = cv2.magnitude(
            cv2.Sobel(candidate_scale, cv2.CV_32F, 1, 0, ksize=3),
            cv2.Sobel(candidate_scale, cv2.CV_32F, 0, 1, ksize=3),
        )
        positive = source_gradient[source_gradient > 0]
        if positive.size < 16:
            raise ValueError("decoded-observable frame has too few measurable edges")
        edge_threshold = max(8.0, float(np.percentile(positive, 65)))
        source_edges = source_gradient >= edge_threshold
        if int(np.count_nonzero(source_edges)) < 16:
            raise ValueError("decoded-observable frame has too few measurable edges")
        mask = cv2.dilate(
            source_edges.astype(np.uint8), np.ones((3, 3), np.uint8)
        ).astype(bool)
        source_strength = float(np.percentile(source_gradient[source_edges], 75))
        candidate_strength = float(np.percentile(candidate_gradient[mask], 75))
        width_ratios.append(source_strength / max(candidate_strength, 1e-9))
        candidate_edges = candidate_gradient >= edge_threshold
        represented = source_edges & cv2.dilate(
            candidate_edges.astype(np.uint8), np.ones((3, 3), np.uint8)
        ).astype(bool)
        continuity_ratios.append(
            float(np.count_nonzero(represented))
            / max(1, int(np.count_nonzero(source_edges)))
        )
        if sigma == 0.0:
            base_edges = source_edges
            base_mask = mask
    assert base_edges is not None and base_mask is not None
    local_min = cv2.erode(source, np.ones((11, 11), np.uint8))
    local_max = cv2.dilate(source, np.ones((11, 11), np.uint8))
    ringing_pixels = base_mask & (
        (candidate < local_min - 8.0) | (candidate > local_max + 8.0)
    )
    ringing_ratio = float(np.count_nonzero(ringing_pixels)) / max(
        1, int(np.count_nonzero(base_mask))
    )
    non_edge = ~cv2.dilate(
        base_edges.astype(np.uint8), np.ones((7, 7), np.uint8)
    ).astype(bool)
    source_high = source - cv2.GaussianBlur(source, (3, 3), 0)
    candidate_high = candidate - cv2.GaussianBlur(candidate, (3, 3), 0)
    if int(np.count_nonzero(non_edge)) < 16:
        noise_gain_ratio = 1.0
    else:
        source_noise = float(np.mean(np.abs(source_high[non_edge]), dtype=np.float64))
        candidate_noise = float(
            np.mean(np.abs(candidate_high[non_edge]), dtype=np.float64)
        )
        noise_gain_ratio = candidate_noise / max(source_noise, 1.0 / 255.0)
    return {
        "edge_width_ratio": float(np.median(width_ratios)),
        "edge_continuity_ratio": min(continuity_ratios),
        "ringing_ratio": ringing_ratio,
        "noise_gain_ratio": noise_gain_ratio,
    }


def _observable_gray(frame: NDArray[np.generic]) -> NDArray[np.float32]:
    gray = _validated_luma(frame)
    if not np.all(np.isfinite(gray)) or min(gray.shape) < 8:
        raise ValueError("decoded-observable frame is invalid")
    return gray.astype(np.float32)


def restore_deblurred_frame(
    frame: NDArray[np.generic],
    estimate: BlurKernelEstimate,
    config: DeblurConfig,
) -> NDArray[np.uint8]:
    """Apply the measured Wiener candidate to luma while preserving chroma."""
    _validated_luma(frame)
    if frame.dtype != np.uint8:
        raise ValueError("restoration frame dtype must be uint8")
    kernel = _kernel(estimate.kernel_kind, estimate.radius)
    if frame.ndim == 2:
        restored, _raw = _restore_luma(
            frame.astype(np.float64),
            kernel,
            estimate.regularization,
            config,
            ringing_suppression_strength=estimate.ringing_suppression_strength,
        )
        return restored
    ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
    restored_luma, _raw = _restore_luma(
        ycrcb[:, :, 0].astype(np.float64),
        kernel,
        estimate.regularization,
        config,
        ringing_suppression_strength=estimate.ringing_suppression_strength,
    )
    output = ycrcb.copy()
    output[:, :, 0] = restored_luma
    return cv2.cvtColor(output, cv2.COLOR_YCrCb2BGR)


def render_deblurred_video(
    source: Path,
    output: Path,
    ranges: Sequence[tuple[float, float]],
    estimate: BlurKernelEstimate,
    config: DeblurConfig,
    *,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    runner: ExternalCommandRunner,
    cancellation_callback: Callable[[], bool] | None = None,
    encode_config: RescueEffectiveConfig | None = None,
) -> None:
    """Stream a confirmed CFR range through deconvolution and publish atomically."""
    source = Path(source)
    output = Path(output)
    ffmpeg_path = Path(ffmpeg_path)
    ffprobe_path = Path(ffprobe_path)
    cancelled = cancellation_callback or (lambda: False)
    callback_failure: list[Exception] = []

    def safe_cancelled() -> bool:
        if callback_failure:
            return True
        try:
            return bool(cancelled())
        except Exception as exc:
            callback_failure.append(exc)
            return True

    def raise_callback_failure() -> None:
        if callback_failure:
            raise callback_failure[0]

    def safe_runner(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del cancellation_callback
        try:
            result = runner(
                arguments,
                timeout_seconds=timeout_seconds,
                sensitive_paths=sensitive_paths,
                cancellation_callback=safe_cancelled,
            )
        except RescueCancelledError:
            raise_callback_failure()
            raise
        except RescueMediaError:
            raise_callback_failure()
            raise
        except Exception as exc:
            raise_callback_failure()
            raise RescueMediaError("deblur media command failed") from exc
        raise_callback_failure()
        return result

    _validate_render_paths(source, output, ffmpeg_path, ffprobe_path)
    selected_ranges = _validate_ranges(ranges)
    _validate_estimate_compatibility(estimate, config)
    if safe_cancelled():
        raise_callback_failure()
        raise RescueCancelledError("deblur rendering cancelled before probe")

    source_probe = _probe_render_media(
        source,
        ffprobe_path=ffprobe_path,
        runner=safe_runner,
        cancellation_callback=safe_cancelled,
    )
    duration, fps, expected_frames, width, height = _source_video_properties(
        source_probe
    )
    source_timestamps = _probe_and_validate_cfr_timing(
        source,
        ffprobe_path=ffprobe_path,
        runner=safe_runner,
        cancellation_callback=safe_cancelled,
        fps=fps,
        expected_frames=expected_frames,
        stream_origin_seconds=_video_stream_start_seconds(source_probe),
    )
    _validate_ranges_against_media(selected_ranges, duration, source_timestamps)

    output.parent.mkdir(parents=True, exist_ok=True)
    candidate_handle = tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.deblur-",
        suffix=".mp4",
        dir=output.parent,
        delete=False,
    )
    candidate = Path(candidate_handle.name)
    candidate_handle.close()
    candidate.unlink(missing_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="videoscope-deblur-", dir=output.parent
        ) as temp_name:
            intermediate = Path(temp_name) / "video-only-lossless.avi"
            _write_deblurred_intermediate(
                source,
                intermediate,
                selected_ranges,
                estimate,
                config,
                fps=fps,
                source_timestamps=source_timestamps,
                width=width,
                height=height,
                cancellation_callback=safe_cancelled,
            )
            mux_result = safe_runner(
                (
                    str(ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-n",
                    "-i",
                    str(intermediate),
                    "-i",
                    str(source),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a?",
                    *canonical_video_encode_arguments(
                        encode_config or RescueEffectiveConfig(),
                        frame_rate=_fraction_text(source_probe),
                    ),
                    "-c:a",
                    "copy",
                    "-map_metadata",
                    "-1",
                    "-movflags",
                    "+faststart",
                    str(candidate),
                ),
                timeout_seconds=_RENDER_TIMEOUT_SECONDS,
                sensitive_paths=(source, output, intermediate, candidate),
                cancellation_callback=safe_cancelled,
            )
            if mux_result.returncode != 0:
                raise RescueMediaError("deblur video encode or audio mux failed")
            if safe_cancelled():
                raise_callback_failure()
                raise RescueCancelledError("deblur rendering cancelled before verify")
            candidate_probe = _probe_render_media(
                candidate,
                ffprobe_path=ffprobe_path,
                runner=safe_runner,
                cancellation_callback=safe_cancelled,
            )
            _validate_candidate_probe(
                source_probe,
                candidate_probe,
                duration=duration,
                fps=fps,
                frame_count=expected_frames,
                width=width,
                height=height,
            )
            _candidate_timestamps = _probe_and_validate_cfr_timing(
                candidate,
                ffprobe_path=ffprobe_path,
                runner=safe_runner,
                cancellation_callback=safe_cancelled,
                fps=fps,
                expected_frames=expected_frames,
                stream_origin_seconds=_video_stream_start_seconds(candidate_probe),
            )
            decode_result = safe_runner(
                (
                    str(ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-xerror",
                    "-i",
                    str(candidate),
                    "-map",
                    "0:v:0",
                    "-f",
                    "null",
                    "-",
                ),
                timeout_seconds=_RENDER_TIMEOUT_SECONDS,
                sensitive_paths=(source, output, candidate),
                cancellation_callback=safe_cancelled,
            )
            if decode_result.returncode != 0:
                raise RescueMediaError("deblur candidate did not fully decode")
            if safe_cancelled():
                raise_callback_failure()
                raise RescueCancelledError(
                    "deblur rendering cancelled before publication"
                )
            if output.exists() or output.is_symlink():
                raise RescueArtifactError(
                    "deblur destination appeared before publication"
                )
            try:
                os.link(candidate, output)
            except OSError as exc:
                raise RescueArtifactError(
                    "deblur output could not be published without overwrite"
                ) from exc
            candidate.unlink()
    except RescueCancelledError:
        raise_callback_failure()
        raise
    finally:
        candidate.unlink(missing_ok=True)


def _validate_render_paths(
    source: Path, output: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    try:
        if not source.is_file():
            raise RescueInputError("deblur source must be an existing regular file")
        if output.exists() or output.is_symlink():
            raise RescueArtifactError("deblur output must not already exist")
        if _paths_alias(source, output):
            raise RescueArtifactError("deblur output must not alias the source")
        for executable in (ffmpeg_path, ffprobe_path):
            if str(executable) in ("", ".", "..") or (
                executable.exists() and not executable.is_file()
            ):
                raise RescueInputError("media executable path is not safe")
    except (RescueArtifactError, RescueInputError):
        raise
    except OSError as exc:
        raise RescueArtifactError("deblur paths could not be resolved safely") from exc


def _paths_alias(left: Path, right: Path) -> bool:
    if os.path.normcase(str(left.resolve(strict=False))) == os.path.normcase(
        str(right.resolve(strict=False))
    ):
        return True
    try:
        return right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _validate_ranges(
    ranges: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    values = tuple(ranges)
    if not values:
        raise RescueInputError("deblur ranges must not be empty")
    previous_end = -math.inf
    for item in values:
        if len(item) != 2:
            raise RescueInputError("each deblur range must contain two seconds values")
        start, end = item
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(start) < 0
            or float(end) <= float(start)
            or float(start) < previous_end
        ):
            raise RescueInputError(
                "deblur ranges must be finite, sorted, non-overlapping intervals"
            )
        previous_end = float(end)
    return tuple((float(start), float(end)) for start, end in values)


def _validate_estimate_compatibility(
    estimate: BlurKernelEstimate, config: DeblurConfig
) -> None:
    if (
        estimate.kernel_kind not in config.candidate_kernel_kinds
        or estimate.radius not in config.candidate_radii
        or estimate.regularization not in config.candidate_regularizations
        or estimate.ringing_suppression_strength
        not in config.candidate_ringing_suppression_strengths
    ):
        raise RescueInputError("blur estimate is incompatible with deblur config")


def _probe_render_media(
    path: Path,
    *,
    ffprobe_path: Path,
    runner: ExternalCommandRunner,
    cancellation_callback: Callable[[], bool],
) -> dict[str, object]:
    result = runner(
        (
            str(ffprobe_path),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ),
        timeout_seconds=_RENDER_TIMEOUT_SECONDS,
        sensitive_paths=(path,),
        cancellation_callback=cancellation_callback,
    )
    if result.returncode != 0:
        raise RescueMediaError("deblur media probe failed")
    try:
        payload = json.loads(result.stdout_summary)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RescueMediaError("deblur media probe returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RescueMediaError("deblur media probe returned invalid data")
    return cast(dict[str, object], payload)


def _parse_fraction(value: object) -> float:
    if not isinstance(value, str) or "/" not in value:
        raise RescueMediaError("source frame rate is unsupported")
    numerator, denominator = value.split("/", 1)
    try:
        result = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError) as exc:
        raise RescueMediaError("source frame rate is unsupported") from exc
    if not math.isfinite(result) or result <= 0:
        raise RescueMediaError("source frame rate is unsupported")
    return result


def _probe_and_validate_cfr_timing(
    path: Path,
    *,
    ffprobe_path: Path,
    runner: ExternalCommandRunner,
    cancellation_callback: Callable[[], bool],
    fps: float,
    expected_frames: int,
    stream_origin_seconds: float,
) -> tuple[float, ...]:
    result = runner(
        (
            str(ffprobe_path),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_frames",
            "-show_streams",
            "-show_entries",
            (
                "frame=best_effort_timestamp_time:stream=nb_read_frames:"
                "frame_side_data=:stream_tags=:stream_disposition=:"
                "stream_side_data="
            ),
            "-of",
            "compact=p=1:nk=1",
            str(path),
        ),
        timeout_seconds=_RENDER_TIMEOUT_SECONDS,
        sensitive_paths=(path,),
        cancellation_callback=cancellation_callback,
    )
    if result.returncode != 0:
        raise RescueMediaError("deblur frame timing probe failed")
    timestamps = _parse_cfr_timing_inventory(
        result.stdout_summary, expected_frames=expected_frames
    )
    try:
        normalized_timestamps = normalize_actual_video_timestamps(
            timestamps,
            stream_origin_seconds,
            origin_tolerance_seconds=_TIMING_TOLERANCE_SECONDS,
        )
    except ValueError as exc:
        raise RescueMediaError(
            "deblur frame timing origin does not match stream"
        ) from exc
    cadence = 1.0 / fps
    if not math.isclose(
        normalized_timestamps[0],
        0.0,
        rel_tol=0.0,
        abs_tol=_TIMING_TOLERANCE_SECONDS,
    ):
        raise RescueMediaError("deblur requires uniform finite frame timing")
    for previous_timestamp, timestamp in zip(
        normalized_timestamps, normalized_timestamps[1:]
    ):
        if not math.isclose(
            timestamp - previous_timestamp,
            cadence,
            rel_tol=1e-6,
            abs_tol=_TIMING_TOLERANCE_SECONDS,
        ):
            raise RescueMediaError("deblur requires uniform finite frame timing")
    if not math.isclose(
        normalized_timestamps[-1] + cadence,
        expected_frames * cadence,
        rel_tol=1e-6,
        abs_tol=_TIMING_TOLERANCE_SECONDS,
    ):
        raise RescueMediaError("deblur frame timing inventory is incomplete")
    return normalized_timestamps


def _parse_cfr_timing_inventory(
    stdout: str, *, expected_frames: int
) -> tuple[float, ...]:
    """Parse one bounded actual-PTS inventory with a counted terminal footer."""
    try:
        encoded_size = len(stdout.encode("utf-8"))
        if (
            not stdout
            or encoded_size > _MAX_CFR_TIMING_OUTPUT_BYTES
            or not stdout.endswith(("\n", "\r"))
            or expected_frames <= 0
            or expected_frames > _MAX_CFR_FRAME_INVENTORY
        ):
            raise ValueError
        lines = stdout.splitlines()
        if len(lines) < 2:
            raise ValueError
        footer = lines[-1].split("|")
        if len(footer) != 2 or footer[0] != "stream":
            raise ValueError
        reported_count = int(footer[1])
        frame_lines = lines[:-1]
        if (
            reported_count < 0
            or reported_count > _MAX_CFR_FRAME_INVENTORY
            or reported_count != expected_frames
            or len(frame_lines) != expected_frames
        ):
            raise ValueError

        timestamps: list[float] = []
        for line in frame_lines:
            fields = line.split("|")
            if (
                len(fields) not in {2, 3}
                or fields[0] != "frame"
                or (len(fields) == 3 and fields[2] != "")
            ):
                raise ValueError
            timestamp = float(fields[1])
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError
            if timestamps and timestamp <= timestamps[-1]:
                raise ValueError
            timestamps.append(timestamp)
    except (UnicodeEncodeError, TypeError, ValueError) as exc:
        raise RescueMediaError(
            "deblur frame timing probe returned invalid data"
        ) from exc
    return tuple(timestamps)


def _video_stream(payload: dict[str, object]) -> dict[str, object]:
    raw_streams = payload.get("streams")
    if not isinstance(raw_streams, list):
        raise RescueMediaError("media probe contains no stream inventory")
    videos = tuple(
        stream
        for stream in raw_streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    )
    if len(videos) != 1:
        raise RescueMediaError("deblur requires exactly one video stream")
    return cast(dict[str, object], videos[0])


def _video_stream_start_seconds(payload: dict[str, object]) -> float:
    raw_start = _video_stream(payload).get("start_time")
    if isinstance(raw_start, bool) or not isinstance(raw_start, (int, float, str)):
        raise RescueMediaError("video stream start time is unavailable")
    try:
        start = float(raw_start)
    except ValueError as exc:
        raise RescueMediaError("video stream start time is invalid") from exc
    if not math.isfinite(start) or start < 0:
        raise RescueMediaError("video stream start time is invalid")
    return start


def _source_video_properties(
    payload: dict[str, object],
) -> tuple[float, float, int, int, int]:
    stream = _video_stream(payload)
    raw_format = payload.get("format")
    if not isinstance(raw_format, dict):
        raise RescueMediaError("source media duration is unavailable")
    try:
        duration = float(raw_format["duration"])
        width = _strict_int(stream["width"])
        height = _strict_int(stream["height"])
        frame_count = _strict_int(stream["nb_frames"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RescueMediaError("source video metadata is incomplete") from exc
    average_fps = _parse_fraction(stream.get("avg_frame_rate"))
    nominal_fps = _parse_fraction(stream.get("r_frame_rate"))
    if (
        not math.isfinite(duration)
        or duration <= 0
        or width <= 0
        or height <= 0
        or frame_count <= 0
        or not math.isclose(average_fps, nominal_fps, rel_tol=1e-6, abs_tol=1e-9)
    ):
        raise RescueMediaError("deblur requires positive CFR source metadata")
    if abs(frame_count / average_fps - duration) > max(
        _TIMING_TOLERANCE_SECONDS, 1.0 / average_fps
    ):
        raise RescueMediaError("source frame count and duration are inconsistent")
    return duration, average_fps, frame_count, width, height


def _strict_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError("integer media field is invalid")
    return int(value)


def _fraction_text(payload: dict[str, object]) -> str:
    value = _video_stream(payload).get("avg_frame_rate")
    if not isinstance(value, str):
        raise RescueMediaError("source frame rate is unavailable")
    return value


def _validate_ranges_against_media(
    ranges: tuple[tuple[float, float], ...],
    duration: float,
    source_timestamps: tuple[float, ...],
) -> None:
    for start, end in ranges:
        if end > duration + _TIMING_TOLERANCE_SECONDS:
            raise RescueInputError("deblur range exceeds source duration")
        if not any(start <= timestamp < end for timestamp in source_timestamps):
            raise RescueInputError("deblur range contains no CFR source frame")


def _write_deblurred_intermediate(
    source: Path,
    intermediate: Path,
    ranges: tuple[tuple[float, float], ...],
    estimate: BlurKernelEstimate,
    config: DeblurConfig,
    *,
    fps: float,
    source_timestamps: tuple[float, ...],
    width: int,
    height: int,
    cancellation_callback: Callable[[], bool],
) -> None:
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RescueMediaError("source could not be opened for deblur")
    writer: cv2.VideoWriter | None = None
    try:
        observed_fps = float(capture.get(cv2.CAP_PROP_FPS))
        observed_width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        observed_height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if (
            not math.isfinite(observed_fps)
            or not math.isclose(observed_fps, fps, rel_tol=1e-5, abs_tol=1e-6)
            or observed_width != width
            or observed_height != height
        ):
            raise RescueMediaError("decoded source properties do not match probe")
        fourcc = int(getattr(cv2, "VideoWriter_fourcc")(*"FFV1"))
        writer = cv2.VideoWriter(str(intermediate), fourcc, fps, (width, height))
        if not writer.isOpened():
            raise RescueMediaError("deblur lossless writer could not be opened")
        expected_frames = len(source_timestamps)
        expected_selected_frames = sum(
            _range_at_timestamp(timestamp, ranges) is not None
            for timestamp in source_timestamps
        )
        processed_selected_frames = 0
        frame_index = 0
        while True:
            if cancellation_callback():
                raise RescueCancelledError("deblur rendering cancelled during decode")
            try:
                ok, frame = capture.read()
            except Exception as exc:
                raise RescueMediaError("source frame decode failed") from exc
            if not ok:
                break
            if frame_index >= expected_frames:
                raise RescueMediaError("source decode exceeded the probed frame count")
            if frame.shape != (height, width, 3) or frame.dtype != np.uint8:
                raise RescueMediaError("decoded source frame is malformed")
            timestamp = source_timestamps[frame_index]
            selected = _range_at_timestamp(timestamp, ranges)
            if selected is not None:
                processed_selected_frames += 1
                restored = restore_deblurred_frame(frame, estimate, config)
                weight = _boundary_weight(
                    timestamp,
                    selected[0],
                    selected[1],
                    config.boundary_transition_seconds,
                )
                if weight >= 1.0:
                    frame = restored
                elif weight > 0.0:
                    frame = np.clip(
                        np.rint(
                            frame.astype(np.float64) * (1.0 - weight)
                            + restored.astype(np.float64) * weight
                        ),
                        0,
                        255,
                    ).astype(np.uint8)
            try:
                writer.write(frame)
            except Exception as exc:
                raise RescueMediaError("deblur lossless writer failed") from exc
            frame_index += 1
        if frame_index != expected_frames:
            raise RescueMediaError("source decode ended before the probed frame count")
        if processed_selected_frames != expected_selected_frames:
            raise RescueMediaError(
                "source range processing did not match actual timing"
            )
    finally:
        if writer is not None:
            writer.release()
        capture.release()


def _range_at_timestamp(
    timestamp: float, ranges: tuple[tuple[float, float], ...]
) -> tuple[float, float] | None:
    for start, end in ranges:
        if timestamp < start:
            return None
        if start <= timestamp < end:
            return start, end
    return None


def _boundary_weight(timestamp: float, start: float, end: float, fade: float) -> float:
    transition = min(fade, (end - start) / 2.0)
    progress_in = min(1.0, max(0.0, (timestamp - start) / transition))
    progress_out = min(1.0, max(0.0, (end - timestamp) / transition))
    progress = min(progress_in, progress_out)
    return progress * progress * (3.0 - 2.0 * progress)


def _validate_candidate_probe(
    source: dict[str, object],
    candidate: dict[str, object],
    *,
    duration: float,
    fps: float,
    frame_count: int,
    width: int,
    height: int,
) -> None:
    candidate_video = _video_stream(candidate)
    (
        candidate_duration,
        candidate_fps,
        candidate_frames,
        candidate_width,
        candidate_height,
    ) = _source_video_properties(candidate)
    if (
        candidate_frames != frame_count
        or candidate_width != width
        or candidate_height != height
        or not math.isclose(candidate_fps, fps, rel_tol=1e-6, abs_tol=1e-9)
        or abs(candidate_duration - duration)
        > max(_TIMING_TOLERANCE_SECONDS, 1.0 / fps)
    ):
        raise RescueMediaError("deblur candidate does not preserve video timing")
    if (
        candidate_video.get("codec_name") != "h264"
        or candidate_video.get("pix_fmt") != "yuv420p"
    ):
        raise RescueMediaError("deblur candidate does not use required H.264 yuv420p")
    if _audio_stream_descriptors(candidate) != _audio_stream_descriptors(source):
        raise RescueMediaError("deblur candidate does not preserve audio streams")


def _audio_stream_descriptors(
    payload: dict[str, object],
) -> tuple[tuple[str, int, int, str], ...]:
    raw_streams = payload.get("streams")
    if not isinstance(raw_streams, list):
        raise RescueMediaError("media probe contains no stream inventory")
    descriptors: list[tuple[str, int, int, str]] = []
    for stream in raw_streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
            continue
        codec_name = stream.get("codec_name")
        channel_layout = stream.get("channel_layout")
        try:
            sample_rate = _strict_int(stream["sample_rate"])
            channels = _strict_int(stream["channels"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RescueMediaError("audio stream descriptor is incomplete") from exc
        if (
            not isinstance(codec_name, str)
            or not codec_name
            or sample_rate <= 0
            or channels <= 0
            or not isinstance(channel_layout, str)
            or not channel_layout
        ):
            raise RescueMediaError("audio stream descriptor is incomplete")
        descriptors.append((codec_name, sample_rate, channels, channel_layout))
    return tuple(descriptors)


def measure_edge_spread_width(
    frame: NDArray[np.generic], config: DeblurConfig
) -> float:
    """Return a deterministic inverse-gradient edge-spread proxy in pixels."""
    luma = _validated_luma(frame)
    if not np.all(np.isfinite(luma)) or min(luma.shape) < 3:
        return float("inf")
    gx = cv2.Sobel(luma, cv2.CV_64F, 1, 0, ksize=3) / 8.0
    gy = cv2.Sobel(luma, cv2.CV_64F, 0, 1, ksize=3) / 8.0
    gradient = np.hypot(gx, gy)
    values = gradient[gradient >= config.edge_gradient_threshold]
    if values.size < config.minimum_edge_pixels:
        return float("inf")
    # A unit 0-to-255 step spread over N pixels has mean derivative 255/N.
    return float(255.0 / max(float(np.percentile(values, 75)), 1e-9))


def _validated_luma(frame: NDArray[np.generic]) -> NDArray[np.float64]:
    if not isinstance(frame, np.ndarray):
        raise ValueError("frame must be a numpy pixel array")
    if frame.ndim not in (2, 3):
        raise ValueError("frame shape must be grayscale or BGR")
    if frame.ndim == 3 and frame.shape[2] != 3:
        raise ValueError("frame shape must have exactly three BGR channels")
    if frame.size == 0 or frame.shape[0] == 0 or frame.shape[1] == 0:
        raise ValueError("frame dimensions must be non-zero")
    if frame.dtype not in (
        np.dtype(np.uint8),
        np.dtype(np.float32),
        np.dtype(np.float64),
    ):
        raise ValueError("frame dtype must be uint8, float32, or float64")
    if frame.ndim == 2:
        return np.asarray(frame, dtype=np.float64)
    bgr = np.asarray(frame, dtype=np.float64)
    return bgr[:, :, 0] * 0.114 + bgr[:, :, 1] * 0.587 + bgr[:, :, 2] * 0.299


def _kernel(kind: KernelKind, radius: int) -> NDArray[np.float64]:
    size = radius * 2 + 1
    if kind == "box":
        return np.full((size, size), 1.0 / (size * size), dtype=np.float64)
    one_dimensional = cv2.getGaussianKernel(size, 0, cv2.CV_64F)
    return one_dimensional @ one_dimensional.T


def _edge_mask(luma: NDArray[np.float64], config: DeblurConfig) -> NDArray[np.bool_]:
    gx = cv2.Sobel(luma, cv2.CV_64F, 1, 0, ksize=3) / 8.0
    gy = cv2.Sobel(luma, cv2.CV_64F, 0, 1, ksize=3) / 8.0
    return np.hypot(gx, gy) >= config.edge_gradient_threshold


def _restore_luma(
    luma: NDArray[np.float64],
    kernel: NDArray[np.float64],
    regularization: float,
    config: DeblurConfig,
    *,
    ringing_suppression_strength: float = 0.0,
) -> tuple[NDArray[np.uint8], NDArray[np.float64]]:
    padding = max(config.reflect_padding_pixels, kernel.shape[0] * 2)
    padded = np.pad(luma, padding, mode="reflect")
    tapered = _edge_taper(padded, kernel, config.edge_taper_pixels)
    transfer = np.zeros_like(tapered)
    kh, kw = kernel.shape
    transfer[:kh, :kw] = kernel
    transfer = np.roll(transfer, (-(kh // 2), -(kw // 2)), axis=(0, 1))
    spectrum = np.fft.rfft2(tapered)
    response = np.fft.rfft2(transfer)
    restored = np.fft.irfft2(
        spectrum * np.conj(response) / (np.abs(response) ** 2 + regularization),
        s=tapered.shape,
    )
    cropped = restored[padding:-padding, padding:-padding]
    if ringing_suppression_strength > 0:
        cropped = _suppress_ringing(
            luma,
            cropped,
            config,
            ringing_suppression_strength,
        )
    bounded = np.clip(np.rint(cropped), 0, 255).astype(np.uint8)
    return bounded, cropped


def _edge_taper(
    image: NDArray[np.float64], kernel: NDArray[np.float64], width: int
) -> NDArray[np.float64]:
    if width == 0:
        return image.copy()
    blurred = cv2.filter2D(image, -1, kernel, borderType=cv2.BORDER_REFLECT_101)
    y = np.minimum(np.arange(image.shape[0]), np.arange(image.shape[0])[::-1])
    x = np.minimum(np.arange(image.shape[1]), np.arange(image.shape[1])[::-1])
    alpha = np.minimum(y[:, None], x[None, :]).astype(np.float64) / width
    alpha = np.clip(alpha, 0.0, 1.0)
    return alpha * image + (1.0 - alpha) * blurred


def _suppress_ringing(
    observed: NDArray[np.float64],
    restored: NDArray[np.float64],
    config: DeblurConfig,
    strength: float,
) -> NDArray[np.float64]:
    """Feather only physically overshooting pixels back toward observed luma."""
    overshoot = (restored < -config.ringing_tolerance) | (
        restored > 255.0 + config.ringing_tolerance
    )
    if not np.any(overshoot):
        return restored
    weight = overshoot.astype(np.float64)
    feather = config.ringing_suppression_feather_pixels
    if feather > 0:
        size = feather * 2 + 1
        feathered = cv2.GaussianBlur(weight, (size, size), 0)
        weight = np.maximum(weight, feathered)
    weight = np.clip(weight * strength, 0.0, 1.0)
    return restored * (1.0 - weight) + observed * weight


def _edge_continuity(
    original_mask: NDArray[np.bool_],
    restored: NDArray[np.uint8],
    kernel: NDArray[np.float64],
    config: DeblurConfig,
) -> float:
    restored_mask = _edge_mask(restored.astype(np.float64), config)
    radius = kernel.shape[0] // 2
    support_radius = radius + config.noise_measurement_margin_pixels
    support_size = support_radius * 2 + 1
    dilated = cv2.dilate(
        restored_mask.astype(np.uint8), np.ones((support_size, support_size), np.uint8)
    )
    return float(np.mean(dilated[original_mask] > 0))


def _reblur_error(
    observed: NDArray[np.float64],
    restored: NDArray[np.uint8],
    kernel: NDArray[np.float64],
    edge_mask: NDArray[np.bool_],
) -> float:
    reblurred = cv2.filter2D(
        restored.astype(np.float64), -1, kernel, borderType=cv2.BORDER_REFLECT_101
    )
    region = cv2.dilate(edge_mask.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    return float(np.sqrt(np.mean((reblurred[region] - observed[region]) ** 2)) / 255.0)


def _ringing_ratio(
    observed: NDArray[np.float64],
    raw: NDArray[np.float64],
    edge_mask: NDArray[np.bool_],
    config: DeblurConfig,
) -> float:
    region = cv2.dilate(edge_mask.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    # Count only physically out-of-range oscillation. Local contrast expansion
    # is the intended effect and is separately bounded by reblur/noise gates.
    ringing = (raw < -config.ringing_tolerance) | (
        raw > 255.0 + config.ringing_tolerance
    )
    return float(np.mean(ringing[region]))


def _noise_gain(
    observed: NDArray[np.float64],
    restored: NDArray[np.uint8],
    edge_mask: NDArray[np.bool_],
    kernel: NDArray[np.float64],
    config: DeblurConfig,
) -> float:
    smooth_observed = cv2.GaussianBlur(observed, (3, 3), 0)
    restored_float = restored.astype(np.float64)
    smooth_restored = cv2.GaussianBlur(restored_float, (3, 3), 0)
    # The restored support of a measured edge extends through the candidate
    # kernel and a configurable measurement margin. Excluding that support
    # prevents legitimate recovered detail from being counted as flat-region
    # noise, while retaining the original high-pass comparison elsewhere.
    radius = kernel.shape[0] // 2
    exclusion_radius = radius + config.noise_measurement_margin_pixels
    exclusion_size = exclusion_radius * 2 + 1
    non_edges = (
        cv2.dilate(
            edge_mask.astype(np.uint8),
            np.ones((exclusion_size, exclusion_size), np.uint8),
        )
        == 0
    )
    if np.count_nonzero(non_edges) < 16:
        non_edges = np.ones_like(edge_mask)
    before = float(np.mean(np.abs(observed[non_edges] - smooth_observed[non_edges])))
    after = float(
        np.mean(np.abs(restored_float[non_edges] - smooth_restored[non_edges]))
    )
    return after / max(before, 2.0)


def _select_temporally_consistent_observations(
    frames: tuple[NDArray[np.float64], ...], config: DeblurConfig
) -> tuple[NDArray[np.float64], ...] | None:
    """Select one unambiguous strict-majority pairwise-compatible cluster."""
    if len(frames) == 1:
        return frames
    compatible: list[set[int]] = [{index} for index in range(len(frames))]
    changes: dict[tuple[int, int], float] = {}
    for left_index, left in enumerate(frames):
        for right_index in range(left_index + 1, len(frames)):
            right = frames[right_index]
            shift, response = cv2.phaseCorrelate(
                left.astype(np.float32), right.astype(np.float32)
            )
            if (
                response < config.minimum_alignment_response
                or float(np.hypot(*shift)) > config.maximum_alignment_shift_pixels
            ):
                continue
            transform = np.float32([[1.0, 0.0, -shift[0]], [0.0, 1.0, -shift[1]]])
            aligned = cv2.warpAffine(
                right,
                transform,
                (right.shape[1], right.shape[0]),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REFLECT_101,
            )
            change = float(np.mean(np.abs(left - aligned))) / 255.0
            if change > config.maximum_temporal_change_ratio:
                continue
            compatible[left_index].add(right_index)
            compatible[right_index].add(left_index)
            changes[(left_index, right_index)] = change
            changes[(right_index, left_index)] = change
    minimum_support = len(frames) // 2 + 1
    largest_cliques: list[tuple[int, ...]] = []
    for size in range(len(frames), minimum_support - 1, -1):
        largest_cliques = [
            members
            for members in combinations(range(len(frames)), size)
            if all(
                right in compatible[left]
                for offset, left in enumerate(members)
                for right in members[offset + 1 :]
            )
        ]
        if largest_cliques:
            break
    if len(largest_cliques) != 1:
        return None
    selected_clique = largest_cliques[0]
    medoid = min(
        selected_clique,
        key=lambda index: (
            sum(changes.get((index, neighbor), 0.0) for neighbor in selected_clique),
            index,
        ),
    )
    selected_indices = [medoid] + sorted(set(selected_clique) - {medoid})
    return tuple(frames[index] for index in selected_indices)


def _temporal_change(
    frames: tuple[NDArray[np.float64], ...], config: DeblurConfig
) -> float | None:
    if len(frames) == 1:
        return 0.0
    reference = frames[0].astype(np.float32)
    changes: list[float] = []
    for frame in frames[1:]:
        shift, response = cv2.phaseCorrelate(reference, frame.astype(np.float32))
        if (
            response < config.minimum_alignment_response
            or float(np.hypot(*shift)) > config.maximum_alignment_shift_pixels
        ):
            return None
        transform = np.float32([[1.0, 0.0, -shift[0]], [0.0, 1.0, -shift[1]]])
        aligned = cv2.warpAffine(
            frame,
            transform,
            (frame.shape[1], frame.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT_101,
        )
        changes.append(float(np.mean(np.abs(reference - aligned))) / 255.0)
    return max(changes)


def _candidate_temporal_change(
    frames: tuple[NDArray[np.float64], ...],
    kernel: NDArray[np.float64],
    regularization: float,
    config: DeblurConfig,
    *,
    ringing_suppression_strength: float = 0.0,
) -> float:
    if len(frames) == 1:
        return 0.0
    restored_reference, _raw = _restore_luma(
        frames[0],
        kernel,
        regularization,
        config,
        ringing_suppression_strength=ringing_suppression_strength,
    )
    changes: list[float] = []
    for frame in frames[1:]:
        shift, response = cv2.phaseCorrelate(
            frames[0].astype(np.float32), frame.astype(np.float32)
        )
        if response < config.minimum_alignment_response:
            return float("inf")
        transform = np.float32([[1.0, 0.0, -shift[0]], [0.0, 1.0, -shift[1]]])
        aligned = cv2.warpAffine(
            frame,
            transform,
            (frame.shape[1], frame.shape[0]),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REFLECT_101,
        )
        restored, _raw = _restore_luma(
            aligned,
            kernel,
            regularization,
            config,
            ringing_suppression_strength=ringing_suppression_strength,
        )
        changes.append(
            float(np.mean(np.abs(restored_reference.astype(np.float64) - restored)))
            / 255.0
        )
    return max(changes)


__all__ = [
    "BlurKernelEstimate",
    "DeblurConfig",
    "estimate_blur_kernel",
    "measure_edge_spread_width",
    "render_deblurred_video",
    "restore_deblurred_frame",
]
