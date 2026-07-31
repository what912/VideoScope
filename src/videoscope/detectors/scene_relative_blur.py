"""CPU-only detector for scene-relative sharpness drops."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from pydantic import BaseModel, ConfigDict, Field

from videoscope.detectors.image_features import (
    laplacian_variance,
    load_luma_image,
)
from videoscope.detectors.intervals import (
    expand_to_sample_boundary,
    select_representative_indices,
)
from videoscope.detectors.models import (
    AnalysisContext,
    DetectorRequirements,
    EstimatedCost,
)
from videoscope.detectors.scene_context import scene_index_for_timestamp
from videoscope.detectors.time_series import (
    TimeSeriesPoint,
    anomalous_points_to_intervals,
)
from videoscope.domain import (
    Evidence,
    Finding,
    Severity,
    TimeRange,
    make_finding_id,
)

EVIDENCE_FRAME_COUNT = 3


class SceneRelativeBlurConfig(BaseModel):
    """Validated thresholds for scene-relative sharpness observations."""

    model_config = ConfigDict(extra="forbid")

    relative_ratio_threshold: float = Field(default=0.45, gt=0, lt=1)
    absolute_floor: float = Field(default=20.0, ge=0)
    min_duration_seconds: float = Field(default=1.0, gt=0)
    merge_gap_seconds: float = Field(default=0.25, ge=0)
    severity: Severity = Severity.MEDIUM


@dataclass(frozen=True, slots=True)
class SharpnessSample:
    """Raw and scene-relative sharpness values for one sampled frame."""

    sample_position: int
    timestamp_seconds: float
    scene_index: int
    sharpness: float
    scene_baseline: float
    is_anomalous: bool


def sharpness_is_anomalous(
    *,
    sharpness: float,
    scene_baseline: float,
    config: SceneRelativeBlurConfig,
) -> bool:
    """Apply a scene-relative threshold with an absolute-floor fallback."""
    relative_drop = (
        scene_baseline > 0
        and sharpness <= scene_baseline * config.relative_ratio_threshold
    )
    below_absolute_floor = (
        config.absolute_floor > 0 and sharpness <= config.absolute_floor
    )
    return relative_drop or below_absolute_floor


def extract_sharpness_series(
    context: AnalysisContext,
    config: SceneRelativeBlurConfig,
) -> list[SharpnessSample]:
    """Measure raw sharpness and assign a within-scene median baseline."""
    raw: list[tuple[int, float, int, float]] = []
    for position, sample in enumerate(context.frame_samples):
        scene_index = scene_index_for_timestamp(
            context.scenes,
            timestamp_seconds=sample.timestamp_seconds,
            fallback_index=position,
        )
        raw.append(
            (
                position,
                sample.timestamp_seconds,
                scene_index,
                laplacian_variance(load_luma_image(context.workspace, sample)),
            )
        )
    baselines = {
        scene_index: float(
            median(
                sharpness
                for _, _, candidate_scene, sharpness in raw
                if candidate_scene == scene_index
            )
        )
        for scene_index in sorted({item[2] for item in raw})
    }
    return [
        SharpnessSample(
            sample_position=position,
            timestamp_seconds=timestamp,
            scene_index=scene_index,
            sharpness=sharpness,
            scene_baseline=baselines[scene_index],
            is_anomalous=sharpness_is_anomalous(
                sharpness=sharpness,
                scene_baseline=baselines[scene_index],
                config=config,
            ),
        )
        for position, timestamp, scene_index, sharpness in raw
    ]


class SceneRelativeBlurDetector:
    """Detect sustained sharpness drops relative to each scene."""

    id = "scene_relative_blur"
    display_name = "Scene-relative blur"
    version = "1.0.0"
    description = "Finds sustained within-scene relative sharpness drops."
    requirements = DetectorRequirements(estimated_cost=EstimatedCost.LOW)
    default_enabled = True
    config_model = SceneRelativeBlurConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        """Return relative sharpness observations without inferring focus."""
        effective = SceneRelativeBlurConfig.model_validate(config.model_dump())
        series = extract_sharpness_series(context, effective)
        candidates = anomalous_points_to_intervals(
            [
                TimeSeriesPoint(
                    timestamp_seconds=sample.timestamp_seconds,
                    sample_position=sample.sample_position,
                    is_anomalous=sample.is_anomalous,
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
            ratios = [
                (
                    sample.sharpness / sample.scene_baseline
                    if sample.scene_baseline > 0
                    else 0.0
                )
                for sample in interval_samples
            ]
            minimum_ratio = min(ratios)
            relative_strength = max(
                0.0,
                1.0 - minimum_ratio / effective.relative_ratio_threshold,
            )
            minimum_sharpness = min(sample.sharpness for sample in interval_samples)
            absolute_strength = (
                max(
                    0.0,
                    1.0 - minimum_sharpness / effective.absolute_floor,
                )
                if effective.absolute_floor > 0
                else 0.0
            )
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
                        "Sampled frame contributing to the relative sharpness "
                        "drop interval."
                    ),
                    metadata={
                        "sharpness": series_by_position[position].sharpness,
                        "scene_baseline": series_by_position[position].scene_baseline,
                        "sharpness_to_baseline_ratio": (
                            series_by_position[position].sharpness
                            / series_by_position[position].scene_baseline
                            if series_by_position[position].scene_baseline > 0
                            else 0.0
                        ),
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
                    title="Relative sharpness drop",
                    description=(
                        "The sampled frames sustained lower Laplacian-variance "
                        "sharpness than the median baseline of their scene, or "
                        "fell below the configured absolute floor. This does not "
                        "establish that the video was out of focus."
                    ),
                    severity=effective.severity,
                    score=min(
                        1.0,
                        max(relative_strength, absolute_strength),
                    ),
                    confidence=min(
                        1.0,
                        candidate.duration_seconds / effective.min_duration_seconds,
                    ),
                    time_range=time_range,
                    evidence=evidence,
                    tags=["relative_sharpness", "possible_blur"],
                    parameters=effective.model_dump(mode="json"),
                    limitations=[
                        "Intentional soft focus, motion blur, depth of field, "
                        "and low-detail imagery can reduce this metric.",
                        "The scene median is a relative baseline and is not a "
                        "calibrated measure of optical focus.",
                    ],
                )
            )
        return findings
