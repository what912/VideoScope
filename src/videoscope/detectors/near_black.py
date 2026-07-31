"""CPU-only detector for sustained near-black sampled frames."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from videoscope.detectors.image_features import (
    LumaMetrics,
    compute_luma_metrics,
    load_luma_image,
)
from videoscope.detectors.intervals import (
    IntervalCandidate,
    expand_to_sample_boundary,
    merge_intervals,
    select_representative_indices,
)
from videoscope.detectors.models import (
    AnalysisContext,
    DetectorRequirements,
    EstimatedCost,
)
from videoscope.domain import (
    Evidence,
    Finding,
    Severity,
    TimeRange,
    make_finding_id,
)

EVIDENCE_FRAME_COUNT = 3


class NearBlackConfig(BaseModel):
    """Validated thresholds for observable near-black intervals."""

    model_config = ConfigDict(extra="forbid")

    mean_luma_threshold: float = Field(default=0.08, gt=0, le=1)
    dark_pixel_threshold: float = Field(default=0.10, ge=0, le=1)
    dark_pixel_ratio: float = Field(default=0.95, ge=0, le=1)
    min_duration_seconds: float = Field(default=1.0, gt=0)
    merge_gap_seconds: float = Field(default=0.25, ge=0)
    severity: Severity = Severity.MEDIUM


@dataclass(frozen=True, slots=True)
class MeasuredFrame:
    """One sampled frame and its observable luma metrics."""

    sample_position: int
    metrics: LumaMetrics


def is_near_black(metrics: LumaMetrics, config: NearBlackConfig) -> bool:
    """Classify one sample from configured observable luma thresholds."""
    return (
        metrics.mean_luma <= config.mean_luma_threshold
        and metrics.dark_pixel_ratio >= config.dark_pixel_ratio
    )


def find_near_black_candidates(
    measured_frames: list[MeasuredFrame],
    timestamps: tuple[float, ...],
    duration_seconds: float,
    config: NearBlackConfig,
) -> list[IntervalCandidate]:
    """Build sustained intervals from consecutive anomalous samples."""
    abnormal_positions = [
        frame.sample_position
        for frame in measured_frames
        if is_near_black(frame.metrics, config)
    ]
    if not abnormal_positions:
        return []

    raw: list[IntervalCandidate] = []
    run_start = abnormal_positions[0]
    previous = abnormal_positions[0]
    for position in abnormal_positions[1:]:
        if position == previous + 1:
            previous = position
            continue
        raw.append(
            IntervalCandidate(
                start_seconds=timestamps[run_start],
                end_seconds=timestamps[previous],
                evidence_indices=tuple(range(run_start, previous + 1)),
            )
        )
        run_start = position
        previous = position
    raw.append(
        IntervalCandidate(
            start_seconds=timestamps[run_start],
            end_seconds=timestamps[previous],
            evidence_indices=tuple(range(run_start, previous + 1)),
        )
    )
    merged = merge_intervals(
        raw,
        merge_gap_seconds=config.merge_gap_seconds,
        min_duration_seconds=config.min_duration_seconds,
    )
    return [
        expand_to_sample_boundary(
            candidate,
            timestamps=timestamps,
            duration_seconds=duration_seconds,
        )
        for candidate in merged
    ]


class NearBlackDetector:
    """Detect sustained low-luma intervals without inferring intent."""

    id = "near_black"
    display_name = "Near-black interval"
    version = "1.0.0"
    description = "Finds sustained sampled intervals with very low observed luma."
    requirements = DetectorRequirements(estimated_cost=EstimatedCost.LOW)
    default_enabled = True
    config_model = NearBlackConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        """Return neutral observations for sustained near-black samples."""
        effective = NearBlackConfig.model_validate(config.model_dump())
        measured = [
            MeasuredFrame(
                sample_position=position,
                metrics=compute_luma_metrics(
                    load_luma_image(context.workspace, sample),
                    dark_pixel_threshold=effective.dark_pixel_threshold,
                ),
            )
            for position, sample in enumerate(context.frame_samples)
        ]
        timestamps = tuple(sample.timestamp_seconds for sample in context.frame_samples)
        candidates = find_near_black_candidates(
            measured,
            timestamps,
            context.metadata.duration_seconds,
            effective,
        )
        metrics_by_position = {
            frame.sample_position: frame.metrics for frame in measured
        }
        findings: list[Finding] = []
        for candidate in candidates:
            interval_metrics = [
                metrics_by_position[position] for position in candidate.evidence_indices
            ]
            mean_luma = sum(item.mean_luma for item in interval_metrics) / len(
                interval_metrics
            )
            mean_dark_ratio = sum(
                item.dark_pixel_ratio for item in interval_metrics
            ) / len(interval_metrics)
            darkness_score = max(
                0.0,
                1.0 - mean_luma / effective.mean_luma_threshold,
            )
            ratio_score = max(
                0.0,
                min(1.0, mean_dark_ratio),
            )
            time_range = TimeRange(
                start_seconds=candidate.start_seconds,
                end_seconds=candidate.end_seconds,
            )
            evidence = [
                Evidence(
                    evidence_type="sampled_frame",
                    timestamp_seconds=context.frame_samples[position].timestamp_seconds,
                    relative_path=context.frame_samples[position].relative_path,
                    description=(
                        "Sampled frame with low observed luma in this interval."
                    ),
                    metadata={
                        "mean_luma": metrics_by_position[position].mean_luma,
                        "median_luma": metrics_by_position[position].median_luma,
                        "dark_pixel_ratio": metrics_by_position[
                            position
                        ].dark_pixel_ratio,
                    },
                )
                for position in select_representative_indices(
                    candidate.evidence_indices,
                    count=EVIDENCE_FRAME_COUNT,
                )
            ]
            findings.append(
                Finding(
                    id=make_finding_id(
                        input_hash=context.input_hash,
                        detector_id=self.id,
                        time_range=time_range,
                    ),
                    detector_id=self.id,
                    detector_version=self.version,
                    title="Near-black interval detected",
                    description=(
                        "Sampled frames remained below the configured mean-luma "
                        "threshold while the configured proportion of pixels was "
                        "dark. This describes the observed image values and does "
                        "not determine whether the interval is a fault."
                    ),
                    severity=effective.severity,
                    score=min(1.0, (darkness_score + ratio_score) / 2.0),
                    confidence=min(
                        1.0,
                        candidate.duration_seconds / effective.min_duration_seconds,
                    ),
                    time_range=time_range,
                    evidence=evidence,
                    tags=["near_black", "brightness"],
                    parameters=effective.model_dump(mode="json"),
                    limitations=[
                        "The interval may be an intentional black field or fade.",
                        "Night scenes and deliberately dark imagery can also "
                        "satisfy these luma thresholds.",
                    ],
                )
            )
        return findings
