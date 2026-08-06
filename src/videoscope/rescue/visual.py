"""Bounded, deterministic CPU measurements for optional visual adjustments.

The measurements in this module describe sampled pixels only.  They do not
claim that filtering recreates missing source information, and recommendations
always require a same-range preview before they can be accepted.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueMediaError,
)
from videoscope.rescue.models import RescueActionKind, RescueVerificationStatus
from videoscope.rescue.stabilization import (
    require_cfr_source_timestamps,
    validate_source_frame_timestamps,
)
from videoscope.rescue.timeline import SourceMapping

if TYPE_CHECKING:
    from videoscope.rescue.executor import ExternalCommandRunner
    from videoscope.scenes.models import VideoScene

_MAXIMUM_SAFE_FLICKER_GAIN = 1.25


class _VisualModel(BaseModel):
    """Strict, immutable models so filter inputs cannot be silently coerced."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LumaAdjustmentConfig(_VisualModel):
    """Conservative luma criteria and the bounded ``eq`` filter settings."""

    dark_percentile_threshold: float = Field(
        default=0.18, gt=0, lt=1, allow_inf_nan=False
    )
    shadow_percentile: float = Field(default=10.0, gt=0, lt=50, allow_inf_nan=False)
    low_clip_level: float = Field(default=0.01, ge=0, lt=1, allow_inf_nan=False)
    high_clip_level: float = Field(default=0.99, gt=0, le=1, allow_inf_nan=False)
    maximum_clip_ratio: float = Field(default=0.02, ge=0, le=0.1, allow_inf_nan=False)
    maximum_clip_increase: float = Field(
        default=0.0, ge=0, le=0.02, allow_inf_nan=False
    )
    brightness: float = Field(default=0.04, ge=0, le=0.08, allow_inf_nan=False)
    contrast: float = Field(default=1.02, ge=1, le=1.08, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_clip_levels(self) -> LumaAdjustmentConfig:
        if self.low_clip_level >= self.high_clip_level:
            raise ValueError("low_clip_level must be below high_clip_level")
        return self


class VideoDenoiseConfig(_VisualModel):
    """Noise threshold and small, fixed-range ``hqdn3d`` settings."""

    residual_threshold: float = Field(default=0.04, gt=0, le=0.2, allow_inf_nan=False)
    maximum_residual_increase: float = Field(
        default=0.0, ge=0, le=0.05, allow_inf_nan=False
    )
    luma_spatial: float = Field(default=1.5, ge=0, le=4, allow_inf_nan=False)
    chroma_spatial: float = Field(default=1.0, ge=0, le=4, allow_inf_nan=False)
    luma_temporal: float = Field(default=2.0, ge=0, le=4, allow_inf_nan=False)
    chroma_temporal: float = Field(default=1.5, ge=0, le=4, allow_inf_nan=False)


class SharpenConfig(_VisualModel):
    """Scene-relative sharpness criteria and capped ``unsharp`` settings."""

    relative_sharpness_threshold: float = Field(
        default=0.45, gt=0, lt=1, allow_inf_nan=False
    )
    absolute_sharpness_floor: float = Field(
        default=0.003, gt=0, le=1, allow_inf_nan=False
    )
    maximum_sharpness_loss_ratio: float = Field(
        default=0.1, ge=0, lt=1, allow_inf_nan=False
    )
    radius: int = Field(default=2, ge=1, le=3)
    amount: float = Field(default=0.4, ge=0, le=1, allow_inf_nan=False)


class FlickerConfig(_VisualModel):
    """Conservative, finite limits for sampled global-luma correction."""

    low_frequency_window_samples: int = Field(default=5, ge=3, le=61)
    scene_guard_seconds: float = Field(default=0.25, ge=0, le=2, allow_inf_nan=False)
    minimum_repetitions: int = Field(default=3, ge=2, le=20)
    residual_threshold: float = Field(default=0.04, gt=0, le=0.25, allow_inf_nan=False)
    maximum_gain: float = Field(default=1.08, gt=1, le=1.25, allow_inf_nan=False)
    fade_min_duration_seconds: float = Field(
        default=0.75, gt=0, le=10, allow_inf_nan=False
    )
    fade_min_luma_change: float = Field(default=0.12, gt=0, le=0.8, allow_inf_nan=False)
    fade_monotonic_tolerance: float = Field(
        default=0.03, ge=0, le=0.2, allow_inf_nan=False
    )
    fade_guard_seconds: float = Field(default=0.25, ge=0, le=2, allow_inf_nan=False)


class FlickerCorrectionPlan(_VisualModel):
    """Exact bounded multiplicative sampled curve; fades and cuts stay neutral."""

    intervals: tuple[tuple[float, float], ...] = ()
    gains: tuple[tuple[float, float], ...] = ()
    interval_gains: tuple[tuple[tuple[float, float], ...], ...] = ()
    excluded_fade_ranges: tuple[tuple[float, float], ...] = ()

    @model_validator(mode="after")
    def validate_curve(self) -> FlickerCorrectionPlan:
        previous = -1.0
        for timestamp, gain in self.gains:
            if not math.isfinite(timestamp) or timestamp < 0:
                raise ValueError("flicker timestamps must be finite and non-negative")
            if (
                not math.isfinite(gain)
                or gain < 1 / _MAXIMUM_SAFE_FLICKER_GAIN
                or gain > _MAXIMUM_SAFE_FLICKER_GAIN
            ):
                raise ValueError(
                    "flicker gains must remain within the global safety cap"
                )
            if timestamp <= previous:
                raise ValueError("flicker timestamps must be strictly increasing")
            previous = timestamp
        for start, end in (*self.intervals, *self.excluded_fade_ranges):
            if not all(math.isfinite(value) and value >= 0 for value in (start, end)):
                raise ValueError("flicker intervals must be finite and non-negative")
            if end < start:
                raise ValueError("flicker interval end must not precede start")
        if self.interval_gains and len(self.interval_gains) != len(self.intervals):
            raise ValueError("flicker interval curves must match accepted intervals")
        if self.interval_gains:
            for (start, end), curve in zip(
                self.intervals, self.interval_gains, strict=True
            ):
                if not curve:
                    raise ValueError("flicker interval curve must not be empty")
                previous = -1.0
                for timestamp, gain in curve:
                    if (
                        not math.isfinite(timestamp)
                        or timestamp < start
                        or timestamp > end
                        or timestamp <= previous
                    ):
                        raise ValueError(
                            "flicker interval curve timestamps must be ordered "
                            "and bounded"
                        )
                    if (
                        not math.isfinite(gain)
                        or gain < 1 / _MAXIMUM_SAFE_FLICKER_GAIN
                        or gain > _MAXIMUM_SAFE_FLICKER_GAIN
                    ):
                        raise ValueError(
                            "flicker interval gains must remain within the global "
                            "safety cap"
                        )
                    previous = timestamp
        return self


def remap_flicker_correction(
    correction: FlickerCorrectionPlan,
    authorized_ranges: Sequence[tuple[float, float]],
    mappings: Sequence[SourceMapping],
) -> FlickerCorrectionPlan | None:
    """Clip and affinely remap one reviewed curve onto a faithful timeline."""
    if not correction.gains:
        return None
    mapped_intervals, mapped_points, mapped_curves = _remap_flicker_ranges(
        correction.intervals,
        correction,
        authorized_ranges,
        mappings,
        include_gains=True,
    )
    if not mapped_intervals:
        return None
    mapped_fades, _unused, _unused_curves = _remap_flicker_ranges(
        correction.excluded_fade_ranges,
        correction,
        authorized_ranges,
        mappings,
        include_gains=False,
    )
    gains_by_timestamp: dict[float, float] = {}
    for timestamp, gain in mapped_points:
        if math.isfinite(timestamp) and math.isfinite(gain):
            gains_by_timestamp[timestamp] = gain
    if not gains_by_timestamp:
        return None
    curves_by_interval: dict[tuple[float, float], tuple[tuple[float, float], ...]] = {}
    for interval, curve in zip(mapped_intervals, mapped_curves, strict=True):
        if all(
            math.isfinite(timestamp) and math.isfinite(gain)
            for timestamp, gain in curve
        ):
            curves_by_interval[interval] = curve
    ordered_intervals = tuple(sorted(curves_by_interval))
    return FlickerCorrectionPlan(
        intervals=ordered_intervals,
        gains=tuple(sorted(gains_by_timestamp.items())),
        interval_gains=tuple(curves_by_interval[item] for item in ordered_intervals),
        excluded_fade_ranges=tuple(sorted(set(mapped_fades))),
    )


def _remap_flicker_ranges(
    ranges: Sequence[tuple[float, float]],
    correction: FlickerCorrectionPlan,
    authorized_ranges: Sequence[tuple[float, float]],
    mappings: Sequence[SourceMapping],
    *,
    include_gains: bool,
) -> tuple[
    list[tuple[float, float]],
    list[tuple[float, float]],
    list[tuple[tuple[float, float], ...]],
]:
    mapped_ranges: list[tuple[float, float]] = []
    mapped_points: list[tuple[float, float]] = []
    mapped_curves: list[tuple[tuple[float, float], ...]] = []
    for range_index, (range_start, range_end) in enumerate(ranges):
        for authorized_start, authorized_end in authorized_ranges:
            for mapping in mappings:
                source_duration = mapping.source_end - mapping.source_start
                output_duration = mapping.output_end - mapping.output_start
                if (
                    not all(
                        math.isfinite(value)
                        for value in (
                            mapping.source_start,
                            mapping.source_end,
                            mapping.output_start,
                            mapping.output_end,
                        )
                    )
                    or source_duration <= 0
                    or output_duration <= 0
                ):
                    continue
                overlap_start = max(range_start, authorized_start, mapping.source_start)
                overlap_end = min(range_end, authorized_end, mapping.source_end)
                if overlap_end <= overlap_start:
                    continue
                scale = output_duration / source_duration

                def map_timestamp(timestamp: float) -> float:
                    return (
                        mapping.output_start
                        + (timestamp - mapping.source_start) * scale
                    )

                mapped_start = map_timestamp(overlap_start)
                mapped_end = map_timestamp(overlap_end)
                if not all(
                    math.isfinite(value) for value in (mapped_start, mapped_end)
                ):
                    continue
                mapped_ranges.append((mapped_start, mapped_end))
                if not include_gains:
                    continue
                source_points = (
                    overlap_start,
                    *(
                        timestamp
                        for timestamp, _gain in correction.gains
                        if overlap_start < timestamp < overlap_end
                    ),
                    overlap_end,
                )
                mapped_curve = tuple(
                    (
                        map_timestamp(timestamp),
                        _flicker_gain_for_interval(timestamp, correction, range_index),
                    )
                    for timestamp in source_points
                )
                mapped_points.extend(mapped_curve)
                mapped_curves.append(mapped_curve)
    return mapped_ranges, mapped_points, mapped_curves


class VisualAssessmentConfig(_VisualModel):
    """All tunable visual thresholds, strengths, and evidence bounds."""

    luma: LumaAdjustmentConfig = Field(default_factory=LumaAdjustmentConfig)
    denoise: VideoDenoiseConfig = Field(default_factory=VideoDenoiseConfig)
    sharpen: SharpenConfig = Field(default_factory=SharpenConfig)
    max_evidence_samples: int = Field(default=3, ge=1, le=5)


class VisualSample(_VisualModel):
    """One already-sampled luma plane; the full video is never retained here."""

    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    luma: tuple[tuple[float, ...], ...]

    @model_validator(mode="after")
    def validate_luma(self) -> VisualSample:
        if not self.luma or not self.luma[0]:
            raise ValueError("luma must contain at least one pixel")
        width = len(self.luma[0])
        for row in self.luma:
            if len(row) != width:
                raise ValueError("luma rows must have a common width")
            for value in row:
                if not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError("luma values must be finite values in [0, 1]")
        return self


class VisualMetrics(_VisualModel):
    """Independent scalar measurements; these are not a quality score."""

    luma_p10: float = Field(ge=0, le=1, allow_inf_nan=False)
    luma_p50: float = Field(ge=0, le=1, allow_inf_nan=False)
    luma_p90: float = Field(ge=0, le=1, allow_inf_nan=False)
    low_clip_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    high_clip_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    noise_residual: float = Field(ge=0, allow_inf_nan=False)
    sharpness: float = Field(ge=0, allow_inf_nan=False)


class VisualEvidence(_VisualModel):
    """A bounded timestamped measurement supporting one recommended action."""

    action: RescueActionKind
    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    metric: str = Field(min_length=1)
    observed: float = Field(allow_inf_nan=False)
    threshold: float = Field(allow_inf_nan=False)


class VisualAssessment(_VisualModel):
    """Immutable assessment made from already-sampled luma frames."""

    metrics: VisualMetrics
    recommended_actions: tuple[RescueActionKind, ...] = ()
    evidence: tuple[VisualEvidence, ...] = ()
    limitations: tuple[str, ...] = ()
    preview_required: bool = False
    public_explanation: str = Field(min_length=1)


class VisualComparison(_VisualModel):
    """Objective before/after side-effect result without a subjective score."""

    before: VisualMetrics
    after: VisualMetrics
    status: RescueVerificationStatus
    reasons: tuple[str, ...] = ()


def assess_visual_samples(
    samples: Sequence[VisualSample],
    scenes: Sequence[VideoScene],
    config: VisualAssessmentConfig,
) -> VisualAssessment:
    """Assess bounded luma samples and return only evidence-supported actions."""
    ordered = tuple(sorted(samples, key=lambda sample: sample.timestamp_seconds))
    if not ordered:
        raise ValueError("at least one visual sample is required")
    metrics_by_sample = tuple(_sample_metrics(sample, config) for sample in ordered)
    metrics = _aggregate_metrics(metrics_by_sample)
    actions: list[RescueActionKind] = []
    evidence: list[VisualEvidence] = []
    limitations: list[str] = []

    dark = (
        metrics.luma_p10 < config.luma.dark_percentile_threshold
        and metrics.high_clip_ratio <= config.luma.maximum_clip_ratio
    )
    if dark:
        actions.append(RescueActionKind.ADJUST_LUMA)
        evidence.extend(
            _evidence_for_lowest(
                ordered,
                metrics_by_sample,
                RescueActionKind.ADJUST_LUMA,
                "luma_p10",
                config.luma.dark_percentile_threshold,
                config.max_evidence_samples,
            )
        )
        limitations.append(
            "Whole-scene darkness may be intentional; preview is required before "
            "using the luma adjustment."
        )

    noisy = metrics.noise_residual > config.denoise.residual_threshold
    if noisy:
        actions.append(RescueActionKind.DENOISE_VIDEO)
        evidence.extend(
            _evidence_for_highest(
                ordered,
                metrics_by_sample,
                RescueActionKind.DENOISE_VIDEO,
                "noise_residual",
                config.denoise.residual_threshold,
                config.max_evidence_samples,
            )
        )

    soft_indices = _scene_relative_soft_indices(
        ordered, metrics_by_sample, scenes, config.sharpen
    )
    if soft_indices:
        actions.append(RescueActionKind.SHARPEN)
        evidence.extend(
            VisualEvidence(
                action=RescueActionKind.SHARPEN,
                timestamp_seconds=ordered[index].timestamp_seconds,
                metric="scene_relative_sharpness",
                observed=metrics_by_sample[index].sharpness,
                threshold=config.sharpen.absolute_sharpness_floor,
            )
            for index in soft_indices[: config.max_evidence_samples]
        )
    elif metrics.sharpness < config.sharpen.absolute_sharpness_floor:
        limitations.append(
            "Scene-wide softness may reflect shallow depth of field or intentional "
            "focus; no sharpening is recommended."
        )

    recommended = tuple(actions)
    return VisualAssessment(
        metrics=metrics,
        recommended_actions=recommended,
        evidence=tuple(evidence),
        limitations=tuple(limitations),
        preview_required=bool(recommended),
        public_explanation=_public_explanation(recommended),
    )


def compare_visual_metrics(
    before: VisualMetrics,
    after: VisualMetrics,
    config: VisualAssessmentConfig,
) -> VisualComparison:
    """Flag any objective luma, noise, or sharpness regression for review."""
    reasons: list[str] = []
    if after.low_clip_ratio - before.low_clip_ratio > config.luma.maximum_clip_increase:
        reasons.append("low_clip_ratio_increased")
    if (
        after.high_clip_ratio - before.high_clip_ratio
        > config.luma.maximum_clip_increase
    ):
        reasons.append("high_clip_ratio_increased")
    if (
        after.noise_residual - before.noise_residual
        > config.denoise.maximum_residual_increase
    ):
        reasons.append("noise_residual_increased")
    if (
        before.sharpness > 0
        and (before.sharpness - after.sharpness) / before.sharpness
        > config.sharpen.maximum_sharpness_loss_ratio
    ):
        reasons.append("sharpness_decreased")
    return VisualComparison(
        before=before,
        after=after,
        status=(
            RescueVerificationStatus.NEEDS_REVIEW
            if reasons
            else RescueVerificationStatus.PASSED
        ),
        reasons=tuple(reasons),
    )


def plan_flicker_correction(
    brightness: Sequence[tuple[float, float]],
    scenes: Sequence[VideoScene],
    config: FlickerConfig,
) -> FlickerCorrectionPlan:
    """Build a correction only for repeated, scene-internal high-frequency luma.

    A centred median is the low-frequency trend.  Residuals near scene
    boundaries are explicitly ignored so a cut cannot become a visual action.
    """
    ordered = tuple(sorted((float(time), float(value)) for time, value in brightness))
    if len(ordered) < config.minimum_repetitions * 2:
        return FlickerCorrectionPlan()
    if any(
        not math.isfinite(time)
        or not math.isfinite(value)
        or time < 0
        or not 0 <= value <= 1
        for time, value in ordered
    ):
        raise ValueError("brightness measurements must be finite values in [0, 1]")
    if any(
        ordered[index][0] <= ordered[index - 1][0] for index in range(1, len(ordered))
    ):
        raise ValueError("brightness measurements must have strictly increasing times")
    values: NDArray[np.float64] = np.asarray(
        [value for _time, value in ordered], dtype=np.float64
    )
    window = (
        config.low_frequency_window_samples
        if config.low_frequency_window_samples % 2
        else config.low_frequency_window_samples + 1
    )
    padded: NDArray[np.float64] = np.pad(
        values, (window // 2, window // 2), mode="edge"
    )
    trend: NDArray[np.float64] = np.asarray(
        [np.mean(padded[index : index + window]) for index in range(len(values))]
    )
    residuals = values - trend
    boundaries = tuple(
        scene.start_seconds for scene in scenes if scene.start_seconds > 0
    )
    fade_ranges = _detected_fade_ranges(ordered, trend, scenes, config)
    candidates = [
        index
        for index, (timestamp, _value) in enumerate(ordered)
        if abs(residuals[index]) >= config.residual_threshold
        and not any(
            abs(timestamp - boundary) <= config.scene_guard_seconds
            for boundary in boundaries
        )
        and not any(
            start - config.fade_guard_seconds
            <= timestamp
            <= end + config.fade_guard_seconds
            for start, end in fade_ranges
        )
    ]
    if len(candidates) < config.minimum_repetitions * 2 or not _alternates(
        residuals, candidates
    ):
        return FlickerCorrectionPlan(excluded_fade_ranges=fade_ranges)
    sample_step = float(np.median(np.diff([time for time, _value in ordered])))
    intervals = _flicker_intervals(candidates, ordered, sample_step)
    if not intervals:
        return FlickerCorrectionPlan(excluded_fade_ranges=fade_ranges)
    target = np.maximum(trend, 1e-6)
    gain_min, gain_max = 1 / config.maximum_gain, config.maximum_gain
    candidate_set = frozenset(candidates)
    gains = tuple(
        (
            timestamp,
            (
                float(np.clip(target[index] / value, gain_min, gain_max))
                if index in candidate_set
                else 1.0
            ),
        )
        for index, (timestamp, value) in enumerate(ordered)
    )
    return FlickerCorrectionPlan(
        intervals=intervals,
        gains=gains,
        excluded_fade_ranges=fade_ranges,
    )


def luma_filter_fragment(config: LumaAdjustmentConfig) -> str:
    """Return a deterministic FFmpeg luma fragment with explicit parameters."""
    brightness = _number(config.brightness)
    contrast = _number(config.contrast)
    return f"eq=brightness={brightness}:contrast={contrast}"


def denoise_filter_fragment(config: VideoDenoiseConfig) -> str:
    """Return a deterministic, bounded FFmpeg ``hqdn3d`` fragment."""
    parameters = ":".join(
        (
            _number(config.luma_spatial),
            _number(config.chroma_spatial),
            _number(config.luma_temporal),
            _number(config.chroma_temporal),
        )
    )
    return f"hqdn3d={parameters}"


def sharpen_filter_fragment(config: SharpenConfig) -> str:
    """Return a deterministic, capped FFmpeg ``unsharp`` fragment."""
    size = config.radius * 2 + 1
    return f"unsharp={size}:{size}:{_number(config.amount)}:{size}:{size}:0"


def visual_action_parameters(kind: RescueActionKind) -> dict[str, float | int]:
    """Expose only bounded numeric defaults for planner action records."""
    if kind is RescueActionKind.ADJUST_LUMA:
        luma_config = LumaAdjustmentConfig()
        return {
            "brightness": luma_config.brightness,
            "contrast": luma_config.contrast,
            "maximum_clip_ratio": luma_config.maximum_clip_ratio,
        }
    if kind is RescueActionKind.DENOISE_VIDEO:
        denoise_config = VideoDenoiseConfig()
        return {
            "luma_spatial": denoise_config.luma_spatial,
            "chroma_spatial": denoise_config.chroma_spatial,
            "luma_temporal": denoise_config.luma_temporal,
            "chroma_temporal": denoise_config.chroma_temporal,
        }
    if kind is RescueActionKind.SHARPEN:
        sharpen_config = SharpenConfig()
        return {"radius": sharpen_config.radius, "amount": sharpen_config.amount}
    return {}


def filter_fragment_from_action(
    kind: RescueActionKind, parameters: Mapping[str, object]
) -> str | None:
    """Revalidate numeric plan fields before they become an FFmpeg filter string."""
    try:
        if kind is RescueActionKind.ADJUST_LUMA:
            return luma_filter_fragment(
                LumaAdjustmentConfig.model_validate(
                    {
                        "brightness": parameters["brightness"],
                        "contrast": parameters["contrast"],
                    }
                )
            )
        if kind is RescueActionKind.DENOISE_VIDEO:
            return denoise_filter_fragment(
                VideoDenoiseConfig.model_validate(
                    {
                        key: parameters[key]
                        for key in (
                            "luma_spatial",
                            "chroma_spatial",
                            "luma_temporal",
                            "chroma_temporal",
                        )
                    }
                )
            )
        if kind is RescueActionKind.SHARPEN:
            return sharpen_filter_fragment(
                SharpenConfig.model_validate(
                    {"radius": parameters["radius"], "amount": parameters["amount"]}
                )
            )
        if kind is RescueActionKind.DEFLICKER:
            return flicker_filter_fragment(
                flicker_correction_from_parameters(parameters)
            )
    except (KeyError, TypeError, ValueError):
        return None
    return None


def flicker_filter_fragment(correction: FlickerCorrectionPlan) -> str | None:
    """Build a deterministic piecewise FFmpeg preview filter from a bound curve."""
    fragments: list[str] = []
    for index, (start, end) in enumerate(correction.intervals):
        curve = (
            correction.interval_gains[index]
            if correction.interval_gains
            else correction.gains
        )
        points = sorted(
            {
                start,
                end,
                *(timestamp for timestamp, _gain in curve if start < timestamp < end),
            }
        )
        for left, right in zip(points, points[1:]):
            gain = _flicker_gain_at((left + right) / 2, correction)
            if math.isclose(gain, 1.0, abs_tol=1e-12):
                continue
            fragments.append(
                "lutyuv=y='clip(val*"
                + _number(gain)
                + ",0,255)':enable='gte(t,"
                + _number(left)
                + ")*lt(t,"
                + _number(right)
                + ")'"
            )
    return ",".join(fragments) or None


def render_deflickered_video(
    source: Path,
    output: Path,
    correction: FlickerCorrectionPlan,
    *,
    runner: ExternalCommandRunner,
    cancellation_callback: Callable[[], bool],
    ffmpeg: str = "ffmpeg",
    timeout_seconds: float = 3600.0,
    frame_timestamps: Sequence[float] | None = None,
) -> None:
    """Apply the accepted luma curve frame-by-frame and atomically preserve audio."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    source, output = Path(source), Path(output)
    if not source.is_file():
        raise RescueArtifactError("deflicker source must be an existing file")
    if _visual_paths_alias(source, output):
        raise RescueArtifactError("deflicker output must not alias the source")
    if output.exists() or output.is_symlink():
        raise RescueArtifactError("deflicker output must not already exist")
    if not correction.intervals or not correction.gains:
        raise RescueMediaError("deflicker correction contains no accepted interval")
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - package dependency boundary
        raise RescueMediaError("OpenCV is required for CPU deflicker") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="videoscope-deflicker-", dir=output.parent
    ) as temp_name:
        intermediate = Path(temp_name) / "video-only.mp4"
        muxed = Path(temp_name) / "deflickered-with-audio.mp4"
        capture = cv2.VideoCapture(str(source))
        if not capture.isOpened():
            raise RescueMediaError("source could not be opened for deflicker")
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
                raise RescueMediaError("deflickered video writer could not be opened")
            try:
                frame_index = 0
                observed_timestamps: list[float] = []
                while True:
                    if cancellation_callback():
                        raise RescueCancelledError("deflicker cancelled")
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
                    gain = _flicker_gain_at(timestamp, correction)
                    if not math.isclose(gain, 1.0, abs_tol=1e-12):
                        yuv = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
                        yuv[..., 0] = np.clip(
                            yuv[..., 0].astype(np.float64) * gain, 0, 255
                        ).astype(np.uint8)
                        frame = cv2.cvtColor(yuv, cv2.COLOR_YCrCb2BGR)
                    writer.write(frame)
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
                "deflicker audio mux failed: " + result.stderr_summary
            )
        if cancellation_callback():
            raise RescueCancelledError("deflicker cancelled before publication")
        try:
            if not muxed.is_file() or muxed.stat().st_size <= 0:
                raise RescueMediaError("deflicker audio mux produced no media")
            if output.exists() or output.is_symlink():
                raise RescueArtifactError(
                    "deflicker output appeared before publication"
                )
            muxed.replace(output)
        except (RescueArtifactError, RescueMediaError):
            raise
        except OSError as exc:
            raise RescueArtifactError(
                "deflickered output could not be published atomically"
            ) from exc


def _sample_metrics(
    sample: VisualSample, config: VisualAssessmentConfig
) -> VisualMetrics:
    frame: NDArray[np.float64] = np.asarray(sample.luma, dtype=np.float64)
    flat = frame.ravel()
    return VisualMetrics(
        luma_p10=float(np.percentile(flat, config.luma.shadow_percentile)),
        luma_p50=float(np.percentile(flat, 50)),
        luma_p90=float(np.percentile(flat, 90)),
        low_clip_ratio=float(np.mean(flat <= config.luma.low_clip_level)),
        high_clip_ratio=float(np.mean(flat >= config.luma.high_clip_level)),
        noise_residual=_local_structure_noise(frame),
        sharpness=_laplacian_sharpness(frame),
    )


def _aggregate_metrics(metrics: Sequence[VisualMetrics]) -> VisualMetrics:
    return VisualMetrics(
        **{
            field: float(np.median([getattr(item, field) for item in metrics]))
            for field in VisualMetrics.model_fields
        }
    )


def _local_structure_noise(frame: NDArray[np.float64]) -> float:
    if min(frame.shape) < 3:
        return 0.0
    centre = frame[1:-1, 1:-1]
    neighbours = (
        frame[:-2, 1:-1] + frame[2:, 1:-1] + frame[1:-1, :-2] + frame[1:-1, 2:]
    ) / 4
    local_structure = np.maximum.reduce(
        (
            np.abs(centre - frame[:-2, 1:-1]),
            np.abs(centre - frame[2:, 1:-1]),
            np.abs(centre - frame[1:-1, :-2]),
            np.abs(centre - frame[1:-1, 2:]),
        )
    )
    residual = np.abs(centre - neighbours)
    return float(np.mean(residual / (1.0 + local_structure)))


def _laplacian_sharpness(frame: NDArray[np.float64]) -> float:
    if min(frame.shape) < 3:
        return 0.0
    laplacian = (
        -4 * frame[1:-1, 1:-1]
        + frame[:-2, 1:-1]
        + frame[2:, 1:-1]
        + frame[1:-1, :-2]
        + frame[1:-1, 2:]
    )
    return float(np.var(laplacian))


def _scene_relative_soft_indices(
    samples: Sequence[VisualSample],
    metrics: Sequence[VisualMetrics],
    scenes: Sequence[VideoScene],
    config: SharpenConfig,
) -> tuple[int, ...]:
    candidates: list[int] = []
    for scene in scenes:
        indexes = [
            index
            for index, sample in enumerate(samples)
            if scene.start_seconds <= sample.timestamp_seconds <= scene.end_seconds
        ]
        if len(indexes) < 2:
            continue
        baseline = float(np.median([metrics[index].sharpness for index in indexes]))
        if baseline <= config.absolute_sharpness_floor:
            continue
        candidates.extend(
            index
            for index in indexes
            if metrics[index].sharpness < config.absolute_sharpness_floor
            and metrics[index].sharpness / baseline
            < config.relative_sharpness_threshold
        )
    return tuple(
        sorted(
            candidates,
            key=lambda index: (
                metrics[index].sharpness,
                samples[index].timestamp_seconds,
            ),
        )
    )


def _evidence_for_lowest(
    samples: Sequence[VisualSample],
    metrics: Sequence[VisualMetrics],
    action: RescueActionKind,
    metric: str,
    threshold: float,
    limit: int,
) -> tuple[VisualEvidence, ...]:
    indexes = sorted(
        range(len(samples)),
        key=lambda index: (
            getattr(metrics[index], metric),
            samples[index].timestamp_seconds,
        ),
    )[:limit]
    return tuple(
        VisualEvidence(
            action=action,
            timestamp_seconds=samples[index].timestamp_seconds,
            metric=metric,
            observed=getattr(metrics[index], metric),
            threshold=threshold,
        )
        for index in indexes
    )


def _evidence_for_highest(
    samples: Sequence[VisualSample],
    metrics: Sequence[VisualMetrics],
    action: RescueActionKind,
    metric: str,
    threshold: float,
    limit: int,
) -> tuple[VisualEvidence, ...]:
    indexes = sorted(
        range(len(samples)),
        key=lambda index: (
            -getattr(metrics[index], metric),
            samples[index].timestamp_seconds,
        ),
    )[:limit]
    return tuple(
        VisualEvidence(
            action=action,
            timestamp_seconds=samples[index].timestamp_seconds,
            metric=metric,
            observed=getattr(metrics[index], metric),
            threshold=threshold,
        )
        for index in indexes
    )


def _public_explanation(actions: tuple[RescueActionKind, ...]) -> str:
    if not actions:
        return (
            "Sampled luma, noise, and scene-relative detail measurements did not "
            "support a bounded visual filter."
        )
    labels = ", ".join(action.value for action in actions)
    return f"Sampled measurements support preview-only bounded filters: {labels}."


def _number(value: float) -> str:
    return format(value, ".6f").rstrip("0").rstrip(".") or "0"


def flicker_correction_from_parameters(
    parameters: Mapping[str, object],
) -> FlickerCorrectionPlan:
    ranges = parameters["affected_ranges"]
    curve = parameters["gain_curve"]
    if not isinstance(ranges, Sequence) or isinstance(ranges, (str, bytes)):
        raise TypeError("affected ranges must be a sequence")
    if not isinstance(curve, Sequence) or isinstance(curve, (str, bytes)):
        raise TypeError("gain curve must be a sequence")
    excluded = parameters.get("excluded_fade_ranges", ())
    if not isinstance(excluded, Sequence) or isinstance(excluded, (str, bytes)):
        raise TypeError("excluded fade ranges must be a sequence")

    def pairs(values: Sequence[object]) -> tuple[tuple[float, float], ...]:
        result: list[tuple[float, float]] = []
        for item in values:
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
                raise TypeError("curve entries must be pairs")
            if len(item) != 2:
                raise ValueError("curve entries must have exactly two values")
            result.append((float(item[0]), float(item[1])))
        return tuple(result)

    return FlickerCorrectionPlan(
        intervals=pairs(ranges),
        gains=pairs(curve),
        excluded_fade_ranges=pairs(excluded),
    )


def _flicker_gain_at(
    timestamp_seconds: float, correction: FlickerCorrectionPlan
) -> float:
    if not math.isfinite(timestamp_seconds) or timestamp_seconds < 0:
        raise ValueError("frame timestamp must be finite and non-negative")
    for interval_index in range(len(correction.intervals) - 1, -1, -1):
        start, end = correction.intervals[interval_index]
        if start <= timestamp_seconds < end:
            return _flicker_gain_for_interval(
                timestamp_seconds, correction, interval_index
            )
    return 1.0


def _flicker_gain_for_interval(
    timestamp_seconds: float,
    correction: FlickerCorrectionPlan,
    interval_index: int,
) -> float:
    curve = (
        correction.interval_gains[interval_index]
        if correction.interval_gains
        else correction.gains
    )
    return _interpolate_flicker_gain(timestamp_seconds, curve)


def _interpolate_flicker_gain(
    timestamp_seconds: float, gains: Sequence[tuple[float, float]]
) -> float:
    times: NDArray[np.float64] = np.asarray(
        [time for time, _gain in gains], dtype=np.float64
    )
    values: NDArray[np.float64] = np.asarray(
        [gain for _time, gain in gains], dtype=np.float64
    )
    return float(np.interp(timestamp_seconds, times, values))


def flicker_gains_for_timestamps(
    correction: FlickerCorrectionPlan, timestamps: Sequence[float]
) -> tuple[float, ...]:
    """Select bound luma gains using exact, possibly irregular source PTS."""
    exact_timestamps = validate_source_frame_timestamps(timestamps)
    return tuple(
        _flicker_gain_at(timestamp, correction) for timestamp in exact_timestamps
    )


def _detected_fade_ranges(
    measurements: Sequence[tuple[float, float]],
    trend: NDArray[np.float64],
    scenes: Sequence[VideoScene],
    config: FlickerConfig,
) -> tuple[tuple[float, float], ...]:
    if not measurements:
        return ()
    scene_ranges = (
        tuple((scene.start_seconds, scene.end_seconds) for scene in scenes)
        if scenes
        else ((measurements[0][0], measurements[-1][0]),)
    )
    detected: list[tuple[float, float]] = []
    for scene_start, scene_end in scene_ranges:
        indexes = [
            index
            for index, (timestamp, _value) in enumerate(measurements)
            if scene_start <= timestamp <= scene_end
        ]
        if len(indexes) < 2:
            continue
        start_index, end_index = indexes[0], indexes[-1]
        duration = measurements[end_index][0] - measurements[start_index][0]
        change = float(trend[end_index] - trend[start_index])
        if (
            duration < config.fade_min_duration_seconds
            or abs(change) < config.fade_min_luma_change
        ):
            continue
        direction = 1.0 if change > 0 else -1.0
        opposing = sum(
            max(0.0, -direction * float(trend[right] - trend[left]))
            for left, right in zip(indexes, indexes[1:])
        )
        if opposing <= config.fade_monotonic_tolerance:
            detected.append((measurements[start_index][0], measurements[end_index][0]))
    return tuple(detected)


def _visual_paths_alias(left: Path, right: Path) -> bool:
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
            "deflicker path identity could not be checked"
        ) from exc


def _alternates(residuals: NDArray[np.float64], indexes: Sequence[int]) -> bool:
    signs = [1 if residuals[index] > 0 else -1 for index in indexes]
    changes = sum(left != right for left, right in zip(signs, signs[1:]))
    return changes >= len(signs) - 2


def _flicker_intervals(
    indexes: Sequence[int],
    measurements: Sequence[tuple[float, float]],
    sample_step: float,
) -> tuple[tuple[float, float], ...]:
    groups: list[list[int]] = []
    for index in indexes:
        if groups and index == groups[-1][-1] + 1:
            groups[-1].append(index)
        else:
            groups.append([index])
    return tuple(
        (measurements[group[0]][0], measurements[group[-1]][0] + sample_step)
        for group in groups
        if len(group) >= 2
    )


__all__ = [
    "FlickerConfig",
    "FlickerCorrectionPlan",
    "LumaAdjustmentConfig",
    "SharpenConfig",
    "VideoDenoiseConfig",
    "VisualAssessment",
    "VisualAssessmentConfig",
    "VisualComparison",
    "VisualEvidence",
    "VisualMetrics",
    "VisualSample",
    "assess_visual_samples",
    "compare_visual_metrics",
    "denoise_filter_fragment",
    "filter_fragment_from_action",
    "flicker_correction_from_parameters",
    "flicker_filter_fragment",
    "flicker_gains_for_timestamps",
    "luma_filter_fragment",
    "plan_flicker_correction",
    "remap_flicker_correction",
    "render_deflickered_video",
    "sharpen_filter_fragment",
    "visual_action_parameters",
]
