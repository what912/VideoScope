"""CPU-only detector for high-frequency global luminance residuals."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from videoscope.detectors.image_features import (
    load_luma_image,
    robust_global_luminance,
)
from videoscope.detectors.intervals import expand_to_sample_boundary
from videoscope.detectors.models import (
    AnalysisContext,
    DetectorRequirements,
    EstimatedCost,
)
from videoscope.detectors.scene_context import (
    is_inside_scene_boundary_guard,
    scene_index_for_timestamp,
)
from videoscope.detectors.time_series import (
    TimeSeriesPoint,
    anomalous_points_to_intervals,
    centered_moving_average,
)
from videoscope.domain import (
    Evidence,
    Finding,
    Severity,
    TimeRange,
    make_finding_id,
)

EVIDENCE_FRAME_COUNT = 3


class GlobalFlickerConfig(BaseModel):
    """Validated thresholds for potential global luminance flicker."""

    model_config = ConfigDict(extra="forbid")

    residual_threshold: float = Field(default=0.12, gt=0, le=1)
    minimum_cycles: int = Field(default=2, ge=1)
    min_duration_seconds: float = Field(default=1.5, gt=0)
    scene_boundary_guard_seconds: float = Field(default=0.25, ge=0)
    trend_window_seconds: float = Field(default=2.0, gt=0)
    merge_gap_seconds: float = Field(default=0.25, ge=0)
    severity: Severity = Severity.MEDIUM


@dataclass(frozen=True, slots=True)
class LuminanceSample:
    """Raw brightness, low-frequency trend, and residual for one sample."""

    sample_position: int
    timestamp_seconds: float
    scene_index: int
    luminance: float
    trend: float
    residual: float
    guarded: bool


def extract_luminance_series(
    context: AnalysisContext,
    config: GlobalFlickerConfig,
) -> list[LuminanceSample]:
    """Measure robust brightness and remove a within-scene moving trend."""
    timestamps = tuple(sample.timestamp_seconds for sample in context.frame_samples)
    luminance = tuple(
        robust_global_luminance(load_luma_image(context.workspace, sample))
        for sample in context.frame_samples
    )
    groups = tuple(
        scene_index_for_timestamp(
            context.scenes,
            timestamp_seconds=sample.timestamp_seconds,
            fallback_index=position,
        )
        for position, sample in enumerate(context.frame_samples)
    )
    trend = centered_moving_average(
        timestamps,
        luminance,
        groups,
        window_seconds=config.trend_window_seconds,
    )
    return [
        LuminanceSample(
            sample_position=position,
            timestamp_seconds=timestamp,
            scene_index=groups[position],
            luminance=luminance[position],
            trend=trend[position],
            residual=luminance[position] - trend[position],
            guarded=is_inside_scene_boundary_guard(
                context.scenes,
                timestamp_seconds=timestamp,
                guard_seconds=config.scene_boundary_guard_seconds,
            ),
        )
        for position, timestamp in enumerate(timestamps)
    ]


def alternating_peak_positions(
    series: list[LuminanceSample],
    config: GlobalFlickerConfig,
) -> set[int]:
    """Return samples belonging to sufficiently long alternating peaks."""
    peaks: set[int] = set()
    run: list[LuminanceSample] = []
    for sample in series:
        is_strong = (
            not sample.guarded and abs(sample.residual) >= config.residual_threshold
        )
        continues = (
            bool(run)
            and is_strong
            and sample.scene_index == run[-1].scene_index
            and sample.sample_position == run[-1].sample_position + 1
            and sample.residual * run[-1].residual < 0
        )
        if continues:
            run.append(sample)
            continue
        _add_qualified_run(peaks, run, config)
        run = [sample] if is_strong else []
    _add_qualified_run(peaks, run, config)
    return peaks


def _add_qualified_run(
    peaks: set[int],
    run: list[LuminanceSample],
    config: GlobalFlickerConfig,
) -> None:
    oscillations = max(0, len(run) - 2)
    if oscillations >= config.minimum_cycles:
        peaks.update(sample.sample_position for sample in run)


class GlobalFlickerDetector:
    """Detect high-frequency global luminance changes away from scene cuts."""

    id = "global_flicker"
    display_name = "Global luminance flicker"
    version = "1.0.0"
    description = "Finds alternating high-frequency global luminance residuals."
    requirements = DetectorRequirements(estimated_cost=EstimatedCost.LOW)
    default_enabled = True
    config_model = GlobalFlickerConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        """Return potential flicker intervals without treating fades as cycles."""
        effective = GlobalFlickerConfig.model_validate(config.model_dump())
        series = extract_luminance_series(context, effective)
        peak_positions = alternating_peak_positions(series, effective)
        candidates = anomalous_points_to_intervals(
            [
                TimeSeriesPoint(
                    timestamp_seconds=sample.timestamp_seconds,
                    sample_position=sample.sample_position,
                    is_anomalous=sample.sample_position in peak_positions,
                    group_index=sample.scene_index,
                )
                for sample in series
            ],
            merge_gap_seconds=effective.merge_gap_seconds,
            min_duration_seconds=effective.min_duration_seconds,
        )
        scene_ends = {scene.scene_index: scene.end_seconds for scene in context.scenes}
        timestamps = tuple(sample.timestamp_seconds for sample in context.frame_samples)
        candidates = [
            expand_to_sample_boundary(
                candidate,
                timestamps=timestamps,
                duration_seconds=context.metadata.duration_seconds,
                group_end_seconds=scene_ends.get(candidate.group_index),
            )
            for candidate in candidates
        ]
        series_by_position = {sample.sample_position: sample for sample in series}
        findings: list[Finding] = []
        for candidate in candidates:
            interval_samples = [
                series_by_position[position] for position in candidate.evidence_indices
            ]
            strongest_positions = sorted(
                candidate.evidence_indices,
                key=lambda position: (
                    -abs(series_by_position[position].residual),
                    series_by_position[position].timestamp_seconds,
                ),
            )[:EVIDENCE_FRAME_COUNT]
            strongest_positions.sort(
                key=lambda position: series_by_position[position].timestamp_seconds
            )
            peak_timestamps: list[JsonValue] = [
                series_by_position[position].timestamp_seconds
                for position in strongest_positions
            ]
            luminance_values = [sample.luminance for sample in interval_samples]
            peak_residual = max(abs(sample.residual) for sample in interval_samples)
            summary: dict[str, JsonValue] = {
                "sample_count": len(interval_samples),
                "minimum_luminance": min(luminance_values),
                "maximum_luminance": max(luminance_values),
                "mean_luminance": (sum(luminance_values) / len(luminance_values)),
                "peak_absolute_residual": peak_residual,
            }
            time_range = TimeRange(
                start_seconds=candidate.start_seconds,
                end_seconds=candidate.end_seconds,
            )
            evidence = [
                Evidence(
                    evidence_type="sampled_frame",
                    timestamp_seconds=series_by_position[position].timestamp_seconds,
                    relative_path=context.frame_samples[position].relative_path,
                    description=(
                        "Sampled frame at a strong alternating global luminance "
                        "residual peak."
                    ),
                    metadata={
                        "luminance": series_by_position[position].luminance,
                        "trend": series_by_position[position].trend,
                        "residual": series_by_position[position].residual,
                        "peak_timestamps_seconds": peak_timestamps,
                        "luminance_series_summary": summary,
                    },
                )
                for position in strongest_positions
            ]
            completed_cycles = max(0, len(candidate.evidence_indices) - 2)
            findings.append(
                Finding(
                    id=make_finding_id(
                        input_hash=context.input_hash,
                        detector_id=self.id,
                        time_range=time_range,
                    ),
                    detector_id=self.id,
                    detector_version=self.version,
                    title="Potential global luminance flicker",
                    description=(
                        "The detrended robust global luminance alternated above "
                        "the configured residual threshold for multiple cycles, "
                        "away from scene-boundary guard windows. Smooth trends "
                        "such as ordinary fades do not satisfy this cycle test."
                    ),
                    severity=effective.severity,
                    score=min(1.0, peak_residual),
                    confidence=min(
                        1.0,
                        completed_cycles / effective.minimum_cycles,
                    ),
                    time_range=time_range,
                    evidence=evidence,
                    tags=["global_luminance", "potential_flicker"],
                    parameters=effective.model_dump(mode="json"),
                    limitations=[
                        "Intentional rhythmic lighting or exposure changes can "
                        "produce the same global luminance pattern.",
                        "Fixed-rate sampling can alias faster brightness changes "
                        "or miss changes between sampled frames.",
                    ],
                )
            )
        return findings
