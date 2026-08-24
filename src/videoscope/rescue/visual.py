"""Bounded, deterministic CPU measurements for optional visual adjustments.

The measurements in this module describe sampled pixels only.  They do not
claim that filtering recreates missing source information, and recommendations
always require a same-range preview before they can be accepted.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import struct
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from videoscope.rescue.encoding import canonical_video_encode_arguments
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueMediaError,
)
from videoscope.rescue.models import (
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueVerificationStatus,
    make_rescue_action_id,
)
from videoscope.rescue.stabilization import (
    require_cfr_source_timestamps,
    validate_source_frame_timestamps,
)
from videoscope.rescue.timeline import SourceMapping

if TYPE_CHECKING:
    from videoscope.rescue.executor import ExternalCommandRunner
    from videoscope.scenes.models import VideoScene

_MAXIMUM_SAFE_FLICKER_GAIN = 1.25
_VIDEO_CODE_VALUE_MAXIMUM = 255


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
    contrast_noise_guard_threshold: float = Field(
        default=0.02, gt=0, le=0.2, allow_inf_nan=False
    )
    target_shadow_luma: float = Field(
        default=0.10, gt=0.04, le=0.20, allow_inf_nan=False
    )
    minimum_brightness: float = Field(default=0.02, ge=0, le=0.08, allow_inf_nan=False)
    maximum_brightness: float = Field(
        default=0.08, ge=0.02, le=0.16, allow_inf_nan=False
    )
    minimum_gamma: float = Field(default=1.0, ge=1, le=1.5, allow_inf_nan=False)
    maximum_gamma: float = Field(default=1.5, ge=1, le=2.2, allow_inf_nan=False)
    gamma_weight: float = Field(default=0.85, ge=0, le=1, allow_inf_nan=False)
    minimum_perceptible_luma_delta: float = Field(
        default=0.04, gt=0, le=0.2, allow_inf_nan=False
    )
    maximum_luma_improvement_delta: float = Field(
        default=0.08, gt=0, le=0.25, allow_inf_nan=False
    )
    maximum_noise_increase: float = Field(default=0.0, ge=0, le=0, allow_inf_nan=False)
    maximum_chroma_shift: float = Field(
        default=0.01, ge=0, le=0.05, allow_inf_nan=False
    )
    noise_guard_luma_lift_scale: float = Field(
        default=2.0, ge=1.0, le=4.0, allow_inf_nan=False
    )
    noise_guard_video_crf: int = Field(default=23, ge=1, le=30, strict=True)
    noise_guard_chroma_qp_offset: int = Field(default=-6, ge=-12, le=0, strict=True)

    @model_validator(mode="after")
    def validate_clip_levels(self) -> LumaAdjustmentConfig:
        if self.low_clip_level >= self.high_clip_level:
            raise ValueError("low_clip_level must be below high_clip_level")
        if self.minimum_brightness > self.maximum_brightness:
            raise ValueError("minimum_brightness must not exceed maximum_brightness")
        if self.minimum_gamma > self.maximum_gamma:
            raise ValueError("minimum_gamma must not exceed maximum_gamma")
        if self.minimum_perceptible_luma_delta > self.maximum_luma_improvement_delta:
            raise ValueError(
                "minimum_perceptible_luma_delta must not exceed "
                "maximum_luma_improvement_delta"
            )
        return self


class LumaActionWire(_VisualModel):
    """Strict executable ADJUST_LUMA contract bound into action identity."""

    derivation_version: Literal["5"] = "5"
    luma_config: LumaAdjustmentConfig
    brightness: float = Field(ge=0, le=0.16, allow_inf_nan=False)
    contrast: float = Field(ge=1, le=1.08, allow_inf_nan=False)
    gamma: float = Field(ge=1, le=2.2, allow_inf_nan=False)
    gamma_weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    filter_mode: Literal["eq", "noise_guarded_y_offset"]
    contrast_noise_guard_threshold: float = Field(gt=0, le=0.2, allow_inf_nan=False)
    observed_noise_residual: float = Field(ge=0, allow_inf_nan=False)
    contrast_derivation: Literal["configured", "noise_guarded"]
    maximum_clip_ratio: float = Field(ge=0, le=0.1, allow_inf_nan=False)
    maximum_clip_increase: float = Field(ge=0, le=0.02, allow_inf_nan=False)
    minimum_perceptible_luma_delta: float = Field(gt=0, le=0.2, allow_inf_nan=False)
    maximum_luma_improvement_delta: float = Field(gt=0, le=0.25, allow_inf_nan=False)
    maximum_residual_increase: float = Field(ge=0, le=0, allow_inf_nan=False)
    maximum_chroma_shift: float = Field(ge=0, le=0.05, allow_inf_nan=False)
    observed_luma_p10: float = Field(ge=0, le=1, allow_inf_nan=False)
    observed_luma_p50: float = Field(ge=0, le=1, allow_inf_nan=False)
    target_shadow_luma: float = Field(gt=0.04, le=0.2, allow_inf_nan=False)
    noise_guard_luma_lift_scale: float = Field(ge=1, le=4, allow_inf_nan=False)
    luma_lift_steps: int | None
    noise_guard_video_crf: int | None
    noise_guard_chroma_qp_offset: int | None = Field(
        default=None, ge=-12, le=0, strict=True
    )


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
    minimum_strength_ratio: float = Field(default=0.5, gt=0, le=1, allow_inf_nan=False)
    full_strength_residual: float = Field(
        default=0.08, gt=0, le=0.4, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_residual_strength_range(self) -> VideoDenoiseConfig:
        if self.full_strength_residual <= self.residual_threshold:
            raise ValueError("full_strength_residual must exceed residual_threshold")
        return self


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
    adaptive_strength: float = Field(default=0.32, ge=0, le=0.5, allow_inf_nan=False)
    amount: float = Field(default=1.0, ge=0, le=1.5, allow_inf_nan=False)
    minimum_amount: float = Field(default=0.8, ge=0, le=1.5, allow_inf_nan=False)
    maximum_amount: float = Field(default=1.5, ge=0.4, le=1.5, allow_inf_nan=False)
    maximum_detail_passes: int = Field(default=3, ge=1, le=3)
    detail_passes: int = Field(default=1, ge=1, le=3)
    minimum_perceptible_sharpness_gain_ratio: float = Field(
        default=0.01, gt=0, le=0.5, allow_inf_nan=False
    )
    maximum_noise_increase: float = Field(
        default=0.02, ge=0, le=0.1, allow_inf_nan=False
    )
    minimum_recovered_baseline_ratio: float = Field(
        default=0.8, gt=0, le=1, allow_inf_nan=False
    )
    minimum_improved_frame_fraction: float = Field(
        default=0.8, gt=0, le=1, allow_inf_nan=False
    )
    edge_gradient_threshold: float = Field(
        default=0.02, gt=0, le=1, allow_inf_nan=False
    )
    edge_neighborhood_radius: int = Field(default=8, ge=1, le=32)
    edge_overshoot_minimum_amplitude: float = Field(
        default=0.01, ge=0, le=1, allow_inf_nan=False
    )
    maximum_edge_overshoot_ratio: float = Field(
        default=0.05, ge=0, le=1, allow_inf_nan=False
    )
    maximum_edge_overshoot_amplitude: float = Field(
        default=0.05, ge=0, le=1, allow_inf_nan=False
    )
    ringing_minimum_amplitude: float = Field(
        default=0.02, gt=0, le=1, allow_inf_nan=False
    )
    maximum_ringing_ratio: float = Field(default=0.08, ge=0, le=1, allow_inf_nan=False)
    visibility_target_luma: float = Field(
        default=0.18, ge=0.05, le=0.5, allow_inf_nan=False
    )
    visibility_brightness: float = Field(
        default=0.0, ge=0, le=0.25, allow_inf_nan=False
    )
    maximum_visibility_brightness: float = Field(
        default=0.15, ge=0, le=0.25, allow_inf_nan=False
    )
    boundary_transition_seconds: float = Field(
        default=0.25, ge=0.05, le=1.0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_amount_bounds(self) -> SharpenConfig:
        if self.minimum_amount > self.maximum_amount:
            raise ValueError("minimum_amount must not exceed maximum_amount")
        if self.visibility_brightness > self.maximum_visibility_brightness:
            raise ValueError("visibility brightness exceeds its configured cap")
        if self.detail_passes > self.maximum_detail_passes:
            raise ValueError("detail_passes exceeds its configured cap")
        if (
            self.edge_overshoot_minimum_amplitude
            > self.maximum_edge_overshoot_amplitude
        ):
            raise ValueError(
                "edge overshoot detection amplitude exceeds the configured cap"
            )
        return self


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
                    return float(
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
    context_luma_p50: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    scene_baseline_sharpness: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )


class VisualActionInterval(_VisualModel):
    """Full contiguous interval measured for one bounded visual action."""

    action: RescueActionKind
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_interval(self) -> VisualActionInterval:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("visual action interval must have positive duration")
        return self


class VisualAssessment(_VisualModel):
    """Immutable assessment made from already-sampled luma frames."""

    metrics: VisualMetrics
    recommended_actions: tuple[RescueActionKind, ...] = ()
    evidence: tuple[VisualEvidence, ...] = ()
    action_intervals: tuple[VisualActionInterval, ...] = ()
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
    ordered_candidates = sorted(samples, key=_canonical_visual_sample_key)
    ordered = tuple(
        sample
        for index, sample in enumerate(ordered_candidates)
        if index == 0
        or _canonical_visual_sample_key(sample)
        != _canonical_visual_sample_key(ordered_candidates[index - 1])
    )
    if not ordered:
        raise ValueError("at least one visual sample is required")
    metrics_by_sample = tuple(_sample_metrics(sample, config) for sample in ordered)
    metrics = _aggregate_metrics(metrics_by_sample)
    actions: list[RescueActionKind] = []
    evidence: list[VisualEvidence] = []
    action_intervals: list[VisualActionInterval] = []
    limitations: list[str] = []

    dark_indexes = tuple(
        index
        for index, item in enumerate(metrics_by_sample)
        if item.luma_p10 < config.luma.dark_percentile_threshold
        and item.high_clip_ratio <= config.luma.maximum_clip_ratio
    )
    dark = bool(dark_indexes) and (
        metrics.luma_p10 < config.luma.dark_percentile_threshold
        and metrics.high_clip_ratio <= config.luma.maximum_clip_ratio
    )
    if dark:
        dark_intervals = _action_intervals_from_indexes(
            ordered,
            dark_indexes,
            RescueActionKind.ADJUST_LUMA,
        )
        luma_evidence, retained_dark_intervals = _range_aware_evidence_for_lowest(
            ordered,
            metrics_by_sample,
            dark_indexes,
            dark_intervals,
            RescueActionKind.ADJUST_LUMA,
            "luma_p10",
            config.luma.dark_percentile_threshold,
            config.max_evidence_samples,
        )
        actions.append(RescueActionKind.ADJUST_LUMA)
        evidence.extend(luma_evidence)
        limitations.append(
            "Whole-scene darkness may be intentional; preview is required before "
            "using the luma adjustment."
        )
        if len(retained_dark_intervals) < len(dark_intervals):
            limitations.append(
                "Some measured dark intervals were omitted because the bounded "
                "luma evidence budget could not cover every interval."
            )
        action_intervals.extend(retained_dark_intervals)

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
        action_intervals.extend(
            _action_intervals_from_indexes(
                ordered,
                tuple(
                    index
                    for index, item in enumerate(metrics_by_sample)
                    if item.noise_residual > config.denoise.residual_threshold
                ),
                RescueActionKind.DENOISE_VIDEO,
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
                context_luma_p50=metrics_by_sample[index].luma_p50,
                scene_baseline_sharpness=_scene_sharpness_baseline(
                    ordered[index].timestamp_seconds,
                    ordered,
                    metrics_by_sample,
                    scenes,
                ),
            )
            for index in soft_indices[: config.max_evidence_samples]
        )
        action_intervals.extend(
            _action_intervals_from_indexes(
                ordered, soft_indices, RescueActionKind.SHARPEN
            )
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
        action_intervals=tuple(
            sorted(
                action_intervals,
                key=lambda item: (
                    item.start_seconds,
                    item.end_seconds,
                    item.action.value,
                ),
            )
        ),
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
    gamma = _number(config.maximum_gamma)
    gamma_weight = _number(config.gamma_weight)
    return (
        f"eq=brightness={brightness}:contrast={contrast}:gamma={gamma}:"
        f"gamma_weight={gamma_weight}"
    )


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
    fragments: list[str] = []
    if config.visibility_brightness > 0:
        fragments.append(
            "eq=brightness="
            + _number(config.visibility_brightness)
            + ":contrast=1.08:gamma=1.2:gamma_weight=0.85"
        )
    fragments.append(f"cas=strength={_number(config.adaptive_strength)}")
    for pass_index in range(config.detail_passes):
        pass_amount = config.amount / (1.0 + 0.45 * pass_index)
        fragments.append(
            f"unsharp={size}:{size}:{_number(pass_amount)}:{size}:{size}:0"
        )
    return ",".join(fragments)


def _derive_luma_action_wire(
    metrics: VisualMetrics,
    config: LumaAdjustmentConfig,
    *,
    strength_limit: float = 1.0,
) -> LumaActionWire:
    if not math.isfinite(strength_limit) or not 0 < strength_limit <= 1:
        raise ValueError("luma strength limit is invalid")
    deficit = max(0.0, config.target_shadow_luma - metrics.luma_p10)
    base_brightness = min(
        config.maximum_brightness,
        max(config.minimum_brightness, deficit),
    )
    severity = min(1.0, deficit / config.target_shadow_luma)
    base_gamma = (
        config.minimum_gamma + (config.maximum_gamma - config.minimum_gamma) * severity
    )
    guarded = metrics.noise_residual >= config.contrast_noise_guard_threshold
    brightness = base_brightness * strength_limit
    contrast = 1.0 if guarded else 1.0 + (config.contrast - 1.0) * strength_limit
    gamma = 1.0 if guarded else 1.0 + (base_gamma - 1.0) * strength_limit
    lift_steps = (
        max(
            1,
            round(
                max(brightness, config.minimum_perceptible_luma_delta)
                * _VIDEO_CODE_VALUE_MAXIMUM
                * config.noise_guard_luma_lift_scale
            ),
        )
        if guarded
        else None
    )
    return LumaActionWire(
        luma_config=config,
        brightness=brightness,
        contrast=contrast,
        gamma=gamma,
        gamma_weight=0.0 if guarded else config.gamma_weight,
        filter_mode="noise_guarded_y_offset" if guarded else "eq",
        contrast_noise_guard_threshold=config.contrast_noise_guard_threshold,
        observed_noise_residual=metrics.noise_residual,
        contrast_derivation="noise_guarded" if guarded else "configured",
        maximum_clip_ratio=config.maximum_clip_ratio,
        maximum_clip_increase=config.maximum_clip_increase,
        minimum_perceptible_luma_delta=config.minimum_perceptible_luma_delta,
        maximum_luma_improvement_delta=config.maximum_luma_improvement_delta,
        maximum_residual_increase=config.maximum_noise_increase,
        maximum_chroma_shift=config.maximum_chroma_shift,
        observed_luma_p10=metrics.luma_p10,
        observed_luma_p50=metrics.luma_p50,
        target_shadow_luma=config.target_shadow_luma,
        noise_guard_luma_lift_scale=config.noise_guard_luma_lift_scale,
        luma_lift_steps=lift_steps,
        noise_guard_video_crf=config.noise_guard_video_crf if guarded else None,
        noise_guard_chroma_qp_offset=(
            config.noise_guard_chroma_qp_offset if guarded else None
        ),
    )


def luma_action_wire_from_parameters(
    parameters: Mapping[str, object],
) -> LumaActionWire:
    """Parse and semantically rederive one versioned ADJUST_LUMA action wire."""
    try:
        raw = {key: parameters[key] for key in LumaActionWire.model_fields}
        wire = LumaActionWire.model_validate(raw)
        strength = parameters.get("strength_limit", 1.0)
        if (
            isinstance(strength, bool)
            or not isinstance(strength, (int, float))
            or not math.isfinite(float(strength))
            or not 0 < float(strength) <= 1
        ):
            raise ValueError("luma strength limit is invalid")
        expected = _derive_luma_action_wire(
            VisualMetrics(
                luma_p10=wire.observed_luma_p10,
                luma_p50=wire.observed_luma_p50,
                luma_p90=wire.observed_luma_p50,
                low_clip_ratio=0.0,
                high_clip_ratio=0.0,
                noise_residual=wire.observed_noise_residual,
                sharpness=0.0,
            ),
            wire.luma_config,
            strength_limit=float(strength),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ADJUST_LUMA action wire is invalid") from exc
    if wire != expected:
        raise ValueError("ADJUST_LUMA action wire is internally inconsistent")
    return wire


def apply_luma_strength_limit(
    parameters: dict[str, JsonValue], strength_limit: float
) -> None:
    """Rebind every derived luma field after the Balanced strength cap."""
    try:
        wire = LumaActionWire.model_validate(
            {key: parameters[key] for key in LumaActionWire.model_fields}
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("ADJUST_LUMA action wire is invalid") from exc
    adjusted = _derive_luma_action_wire(
        VisualMetrics(
            luma_p10=wire.observed_luma_p10,
            luma_p50=wire.observed_luma_p50,
            luma_p90=wire.observed_luma_p50,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=wire.observed_noise_residual,
            sharpness=0.0,
        ),
        wire.luma_config,
        strength_limit=strength_limit,
    )
    parameters.update(adjusted.model_dump(mode="json"))


def validate_luma_action_evidence(
    evidence: Sequence[VisualEvidence],
    config: LumaAdjustmentConfig,
    source_ranges: Sequence[tuple[float, float]],
) -> tuple[float, float]:
    """Validate range-bound luma provenance and return its measured medians."""
    selected = tuple(evidence)
    ranges = tuple(source_ranges)
    if (
        not selected
        or not ranges
        or any(
            item.action is not RescueActionKind.ADJUST_LUMA
            or item.metric != "luma_p10"
            or item.threshold != config.dark_percentile_threshold
            or item.observed >= item.threshold
            or item.context_luma_p50 is None
            for item in selected
        )
    ):
        raise ValueError("ADJUST_LUMA assessment evidence is invalid")
    timestamps = tuple(item.timestamp_seconds for item in selected)
    if len(set(timestamps)) != len(timestamps):
        raise ValueError("ADJUST_LUMA assessment evidence is invalid")
    range_indexes: list[int] = []
    for item in selected:
        matching_ranges = tuple(
            index
            for index, (start, end) in enumerate(ranges)
            if start <= item.timestamp_seconds < end
        )
        if len(matching_ranges) != 1:
            raise ValueError("ADJUST_LUMA assessment evidence is invalid")
        range_indexes.append(matching_ranges[0])
    if set(range_indexes) != set(range(len(ranges))):
        raise ValueError("ADJUST_LUMA assessment evidence is invalid")
    contexts = tuple(cast(float, item.context_luma_p50) for item in selected)
    return (
        float(statistics.median(item.observed for item in selected)),
        float(statistics.median(contexts)),
    )


def validate_plan_luma_action_contracts(plan: RescuePlan) -> None:
    """Recheck luma assessment provenance at preview/final trust boundaries."""
    actions = tuple(
        action for action in plan.actions if action.kind is RescueActionKind.ADJUST_LUMA
    )
    if not actions:
        return
    if len(actions) != 1:
        raise ValueError("ADJUST_LUMA action inventory is ambiguous")
    action = actions[0]
    if "strength_limit" not in action.parameters:
        raise ValueError("ADJUST_LUMA action strength limit is missing")
    raw_visual = plan.assessment_parameters.get("visual_config")
    if raw_visual is None:
        assessment_config = VisualAssessmentConfig()
    else:
        if not isinstance(raw_visual, Mapping):
            raise ValueError("assessment visual config is invalid")
        raw_luma = raw_visual.get("luma")
        if not isinstance(raw_luma, Mapping) or set(raw_luma) != set(
            LumaAdjustmentConfig.model_fields
        ):
            raise ValueError("assessment LUMA config fields are incomplete")
        assessment_config = VisualAssessmentConfig.model_validate(raw_visual)
    wire = luma_action_wire_from_parameters(action.parameters)
    if wire.luma_config != assessment_config.luma:
        raise ValueError("ADJUST_LUMA wire does not match assessment config")
    metrics = VisualMetrics.model_validate(action.parameters.get("assessment_metrics"))
    raw_evidence = action.parameters.get("assessment_evidence")
    if not isinstance(raw_evidence, (list, tuple)):
        raise ValueError("ADJUST_LUMA assessment evidence is invalid")
    try:
        evidence = tuple(
            VisualEvidence.model_validate_json(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            for item in raw_evidence
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("ADJUST_LUMA assessment evidence is invalid") from exc
    expected_p10, expected_p50 = validate_luma_action_evidence(
        evidence,
        wire.luma_config,
        action.source_ranges,
    )
    if (
        wire.observed_luma_p10 != expected_p10
        or metrics.luma_p10 != expected_p10
        or wire.observed_luma_p50 != expected_p50
        or metrics.luma_p50 != expected_p50
        or wire.observed_noise_residual != metrics.noise_residual
    ):
        raise ValueError("ADJUST_LUMA assessment evidence does not match its wire")
    expected_id = make_rescue_action_id(
        kind=action.kind,
        parameters=action.parameters,
        source_ranges=action.source_ranges,
        strategy=action.strategy,
        version=action.version,
    )
    if action.id != expected_id:
        raise ValueError("ADJUST_LUMA action ID does not match its wire")


def derive_visual_action_parameters(
    kind: RescueActionKind,
    metrics: VisualMetrics,
    *,
    luma_config: LumaAdjustmentConfig | None = None,
    denoise_config: VideoDenoiseConfig | None = None,
    sharpen_config: SharpenConfig | None = None,
    scene_baseline_sharpness: float | None = None,
) -> dict[str, JsonValue]:
    """Derive deterministic bounded filters from measured visual evidence."""
    if kind is RescueActionKind.ADJUST_LUMA:
        luma = luma_config or LumaAdjustmentConfig()
        return _derive_luma_action_wire(metrics, luma).model_dump(mode="json")
    if kind is RescueActionKind.DENOISE_VIDEO:
        denoise = denoise_config or VideoDenoiseConfig()
        residual_span = denoise.full_strength_residual - denoise.residual_threshold
        residual_severity = min(
            1.0,
            max(
                0.0,
                (metrics.noise_residual - denoise.residual_threshold) / residual_span,
            ),
        )
        strength_ratio = (
            denoise.minimum_strength_ratio
            + (1.0 - denoise.minimum_strength_ratio) * residual_severity
        )
        return {
            "luma_spatial": denoise.luma_spatial * strength_ratio,
            "chroma_spatial": denoise.chroma_spatial * strength_ratio,
            "luma_temporal": denoise.luma_temporal * strength_ratio,
            "chroma_temporal": denoise.chroma_temporal * strength_ratio,
            "maximum_residual_increase": denoise.maximum_residual_increase,
            "minimum_strength_ratio": denoise.minimum_strength_ratio,
            "full_strength_residual": denoise.full_strength_residual,
            "residual_threshold": denoise.residual_threshold,
            "observed_noise_residual": metrics.noise_residual,
            "strength_ratio": strength_ratio,
            "derivation_version": "2",
        }
    if kind is RescueActionKind.SHARPEN:
        sharpen = sharpen_config or SharpenConfig()
        if metrics.sharpness <= 0:
            severity = 1.0
        else:
            absolute_severity = min(
                1.0,
                max(
                    0.0,
                    (sharpen.absolute_sharpness_floor - metrics.sharpness)
                    / sharpen.absolute_sharpness_floor,
                ),
            )
            relative_severity = 0.0
            if (
                scene_baseline_sharpness is not None
                and math.isfinite(scene_baseline_sharpness)
                and scene_baseline_sharpness > 0
            ):
                relative_severity = min(
                    1.0,
                    max(0.0, 1.0 - metrics.sharpness / scene_baseline_sharpness),
                )
            severity = max(absolute_severity, relative_severity)
        relative_severity = locals().get("relative_severity", 1.0)
        # Use a concave response so severely soft intervals get enough bounded
        # edge recovery to clear the perceptibility gate without oversharpening
        # mild anomalies.
        amount = sharpen.minimum_amount + (
            sharpen.maximum_amount - sharpen.minimum_amount
        ) * math.sqrt(severity)
        detail_passes = min(
            sharpen.maximum_detail_passes,
            1
            + int(relative_severity >= 0.35 or severity >= 0.70)
            + int(relative_severity >= 0.70 or severity >= 0.95),
        )
        visibility_brightness = min(
            sharpen.maximum_visibility_brightness,
            max(0.0, sharpen.visibility_target_luma - metrics.luma_p50),
        )
        return {
            "radius": sharpen.radius,
            "adaptive_strength": sharpen.adaptive_strength,
            "amount": amount,
            "detail_passes": detail_passes,
            "visibility_brightness": visibility_brightness,
            "maximum_visibility_brightness": sharpen.maximum_visibility_brightness,
            "boundary_transition_seconds": sharpen.boundary_transition_seconds,
            "maximum_sharpness_loss_ratio": (sharpen.maximum_sharpness_loss_ratio),
            "minimum_perceptible_sharpness_gain_ratio": (
                sharpen.minimum_perceptible_sharpness_gain_ratio
            ),
            "maximum_noise_increase": sharpen.maximum_noise_increase,
            "observed_sharpness": metrics.sharpness,
            "scene_baseline_sharpness": metrics.sharpness,
            "minimum_recovered_baseline_ratio": (
                sharpen.minimum_recovered_baseline_ratio
            ),
            "minimum_improved_frame_fraction": (
                sharpen.minimum_improved_frame_fraction
            ),
            "edge_gradient_threshold": sharpen.edge_gradient_threshold,
            "edge_neighborhood_radius": sharpen.edge_neighborhood_radius,
            "edge_overshoot_minimum_amplitude": (
                sharpen.edge_overshoot_minimum_amplitude
            ),
            "maximum_edge_overshoot_ratio": (sharpen.maximum_edge_overshoot_ratio),
            "maximum_edge_overshoot_amplitude": (
                sharpen.maximum_edge_overshoot_amplitude
            ),
            "ringing_minimum_amplitude": sharpen.ringing_minimum_amplitude,
            "maximum_ringing_ratio": sharpen.maximum_ringing_ratio,
            "derivation_version": "2",
        }
    return {}


def visual_action_parameters(kind: RescueActionKind) -> dict[str, JsonValue]:
    """Expose only bounded numeric defaults for planner action records."""
    if kind is RescueActionKind.ADJUST_LUMA:
        default_metrics = VisualMetrics(
            luma_p10=LumaAdjustmentConfig().dark_percentile_threshold,
            luma_p50=LumaAdjustmentConfig().dark_percentile_threshold,
            luma_p90=0.5,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.0,
            sharpness=0.0,
        )
        return derive_visual_action_parameters(kind, default_metrics)
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
            wire = luma_action_wire_from_parameters(parameters)
            if wire.filter_mode == "eq":
                render_config = wire.luma_config.model_copy(
                    update={
                        "brightness": wire.brightness,
                        "contrast": wire.contrast,
                        "minimum_gamma": wire.gamma,
                        "maximum_gamma": wire.gamma,
                        "gamma_weight": wire.gamma_weight,
                    }
                )
                return luma_filter_fragment(render_config)
            if wire.luma_lift_steps is None:
                return None
            return "lutyuv=y='val+" + str(wire.luma_lift_steps) + "'"
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
                    {
                        "radius": parameters["radius"],
                        "adaptive_strength": parameters["adaptive_strength"],
                        "amount": parameters["amount"],
                        "detail_passes": parameters.get("detail_passes", 1),
                        "visibility_brightness": parameters.get(
                            "visibility_brightness", 0.0
                        ),
                        "maximum_visibility_brightness": parameters.get(
                            "maximum_visibility_brightness", 0.15
                        ),
                        "boundary_transition_seconds": parameters.get(
                            "boundary_transition_seconds", 0.25
                        ),
                    }
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
    encode_config: RescueEffectiveConfig | None = None,
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
                *canonical_video_encode_arguments(
                    encode_config or RescueEffectiveConfig()
                ),
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
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


def _scene_sharpness_baseline(
    timestamp: float,
    samples: Sequence[VisualSample],
    metrics: Sequence[VisualMetrics],
    scenes: Sequence[VideoScene],
) -> float:
    for scene in scenes:
        if scene.start_seconds <= timestamp <= scene.end_seconds:
            values = [
                metrics[index].sharpness
                for index, sample in enumerate(samples)
                if scene.start_seconds <= sample.timestamp_seconds <= scene.end_seconds
            ]
            if values:
                return float(np.median(values))
    return 0.0


def _action_intervals_from_indexes(
    samples: Sequence[VisualSample],
    indexes: Sequence[int],
    action: RescueActionKind,
) -> tuple[VisualActionInterval, ...]:
    """Convert every qualifying sample, not only evidence thumbnails, to ranges."""
    ordered_indexes = sorted(
        set(indexes), key=lambda index: samples[index].timestamp_seconds
    )
    if not ordered_indexes:
        return ()
    timestamps = [samples[index].timestamp_seconds for index in ordered_indexes]
    all_timestamps = sorted(sample.timestamp_seconds for sample in samples)
    positive_steps = [
        right - left
        for left, right in zip(all_timestamps, all_timestamps[1:], strict=False)
        if right > left
    ]
    step = float(np.median(positive_steps)) if positive_steps else 0.5
    groups: list[list[float]] = []
    for timestamp in timestamps:
        if groups and timestamp - groups[-1][-1] <= step * 1.5 + 1e-9:
            groups[-1].append(timestamp)
        else:
            groups.append([timestamp])
    return tuple(
        VisualActionInterval(
            action=action,
            start_seconds=max(0.0, group[0] - step / 2),
            end_seconds=group[-1] + step / 2,
        )
        for group in groups
    )


def _range_aware_evidence_for_lowest(
    samples: Sequence[VisualSample],
    metrics: Sequence[VisualMetrics],
    eligible_indexes: Sequence[int],
    eligible_ranges: Sequence[VisualActionInterval],
    action: RescueActionKind,
    metric: str,
    threshold: float,
    limit: int,
) -> tuple[tuple[VisualEvidence, ...], tuple[VisualActionInterval, ...]]:
    ranked_indexes = sorted(
        set(eligible_indexes),
        key=lambda index: (
            getattr(metrics[index], metric),
            _canonical_visual_sample_key(samples[index]),
        ),
    )
    unique_indexes: list[int] = []
    seen_timestamps: set[float] = set()
    for index in ranked_indexes:
        timestamp = samples[index].timestamp_seconds
        if timestamp not in seen_timestamps:
            unique_indexes.append(index)
            seen_timestamps.add(timestamp)

    range_candidates = tuple(
        (
            item,
            tuple(
                index
                for index in unique_indexes
                if item.start_seconds
                <= samples[index].timestamp_seconds
                < item.end_seconds
            ),
        )
        for item in eligible_ranges
    )
    ranked_ranges = sorted(
        [(item, indexes) for item, indexes in range_candidates if indexes],
        key=lambda pair: (
            getattr(metrics[pair[1][0]], metric),
            _canonical_visual_sample_key(samples[pair[1][0]]),
            pair[0].start_seconds,
            pair[0].end_seconds,
        ),
    )[:limit]
    retained_ranges = tuple(
        sorted(
            (item for item, _indexes in ranked_ranges),
            key=lambda item: (item.start_seconds, item.end_seconds),
        )
    )
    reserved = {indexes[0] for _item, indexes in ranked_ranges}
    retained_indexes = tuple(
        index
        for index in unique_indexes
        if any(
            item.start_seconds <= samples[index].timestamp_seconds < item.end_seconds
            for item in retained_ranges
        )
    )
    indexes = tuple(
        sorted(
            reserved,
            key=lambda index: (
                getattr(metrics[index], metric),
                _canonical_visual_sample_key(samples[index]),
            ),
        )
    )
    indexes += tuple(index for index in retained_indexes if index not in reserved)[
        : max(0, limit - len(indexes))
    ]
    indexes = tuple(
        sorted(
            indexes,
            key=lambda index: (
                getattr(metrics[index], metric),
                _canonical_visual_sample_key(samples[index]),
            ),
        )
    )
    evidence = tuple(
        VisualEvidence(
            action=action,
            timestamp_seconds=samples[index].timestamp_seconds,
            metric=metric,
            observed=getattr(metrics[index], metric),
            threshold=threshold,
            context_luma_p50=metrics[index].luma_p50,
        )
        for index in indexes
    )
    return evidence, retained_ranges


def _canonical_visual_sample_key(
    sample: VisualSample,
) -> tuple[
    tuple[float, bytes],
    int,
    int,
    tuple[tuple[float, bytes], ...],
]:
    """Return a total order over every finite field in a visual sample."""
    return (
        _finite_float_order_key(sample.timestamp_seconds),
        len(sample.luma),
        len(sample.luma[0]),
        tuple(_finite_float_order_key(value) for row in sample.luma for value in row),
    )


def _finite_float_order_key(value: float) -> tuple[float, bytes]:
    """Preserve numeric ordering and break equal-value ties by binary64 bits."""
    if not math.isfinite(value):
        raise ValueError("canonical float ordering requires a finite value")
    return value, struct.pack(">d", value)


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
    "VisualActionInterval",
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
