"""CPU-only detector for sustained similar or repeated sampled frames."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from videoscope.detectors.image_features import (
    average_hash,
    hash_distance,
    load_luma_image,
    mean_absolute_difference,
)
from videoscope.detectors.intervals import (
    IntervalCandidate,
    merge_intervals,
    select_representative_indices,
)
from videoscope.detectors.models import (
    AnalysisContext,
    DetectorRequirements,
    EstimatedCost,
)
from videoscope.detectors.scene_context import (
    scene_end_seconds,
    scene_index_for_timestamp,
)
from videoscope.domain import (
    Evidence,
    Finding,
    Severity,
    TimeRange,
    make_finding_id,
)

EVIDENCE_FRAME_COUNT = 3


class PossibleFreezeConfig(BaseModel):
    """Validated thresholds for possible frozen or repeated frames."""

    model_config = ConfigDict(extra="forbid")

    max_pixel_difference: float = Field(default=0.01, gt=0, le=1)
    max_hash_distance: int = Field(default=2, ge=0, le=64)
    min_duration_seconds: float = Field(default=1.5, gt=0)
    merge_gap_seconds: float = Field(default=0.25, ge=0)
    severity: Severity = Severity.MEDIUM


@dataclass(frozen=True, slots=True)
class FramePairMetrics:
    """Two observable low-cost differences for one adjacent frame pair."""

    left_position: int
    right_position: int
    scene_group: int
    pixel_difference: float
    hash_distance: int


def is_similar_pair(
    metrics: FramePairMetrics,
    config: PossibleFreezeConfig,
) -> bool:
    """Classify a frame pair using both configured difference metrics."""
    return (
        metrics.pixel_difference <= config.max_pixel_difference
        and metrics.hash_distance <= config.max_hash_distance
    )


def build_pair_metrics(context: AnalysisContext) -> list[FramePairMetrics]:
    """Compute pair differences without comparing across a scene boundary."""
    luma_images = [
        load_luma_image(context.workspace, sample) for sample in context.frame_samples
    ]
    hashes = [average_hash(luma) for luma in luma_images]
    groups = [
        scene_index_for_timestamp(
            context.scenes,
            timestamp_seconds=sample.timestamp_seconds,
            fallback_index=position,
        )
        for position, sample in enumerate(context.frame_samples)
    ]
    metrics: list[FramePairMetrics] = []
    for left_position in range(len(context.frame_samples) - 1):
        right_position = left_position + 1
        if groups[left_position] != groups[right_position]:
            continue
        metrics.append(
            FramePairMetrics(
                left_position=left_position,
                right_position=right_position,
                scene_group=groups[left_position],
                pixel_difference=mean_absolute_difference(
                    luma_images[left_position],
                    luma_images[right_position],
                ),
                hash_distance=hash_distance(
                    hashes[left_position],
                    hashes[right_position],
                ),
            )
        )
    return metrics


def find_freeze_candidates(
    pair_metrics: list[FramePairMetrics],
    timestamps: tuple[float, ...],
    duration_seconds: float,
    scene_ends: dict[int, float],
    config: PossibleFreezeConfig,
) -> list[IntervalCandidate]:
    """Build and merge similar-pair runs, preserving scene isolation."""
    similar = [metrics for metrics in pair_metrics if is_similar_pair(metrics, config)]
    if not similar:
        return []

    raw: list[IntervalCandidate] = []
    run = [similar[0]]
    for metrics in similar[1:]:
        previous = run[-1]
        if (
            metrics.scene_group == previous.scene_group
            and metrics.left_position == previous.right_position
        ):
            run.append(metrics)
            continue
        raw.append(_pair_run_candidate(run, timestamps))
        run = [metrics]
    raw.append(_pair_run_candidate(run, timestamps))

    merged = merge_intervals(
        raw,
        merge_gap_seconds=config.merge_gap_seconds,
        min_duration_seconds=0,
    )
    expanded: list[IntervalCandidate] = []
    for candidate in merged:
        last_position = candidate.evidence_indices[-1]
        next_timestamp = (
            timestamps[last_position + 1]
            if last_position + 1 < len(timestamps)
            else duration_seconds
        )
        end_seconds = min(
            next_timestamp,
            duration_seconds,
            scene_ends.get(candidate.group_index, duration_seconds),
        )
        expanded_candidate = IntervalCandidate(
            start_seconds=candidate.start_seconds,
            end_seconds=max(candidate.end_seconds, end_seconds),
            evidence_indices=candidate.evidence_indices,
            group_index=candidate.group_index,
        )
        if (
            expanded_candidate.duration_seconds >= config.min_duration_seconds
            and len(expanded_candidate.evidence_indices) >= EVIDENCE_FRAME_COUNT
        ):
            expanded.append(expanded_candidate)
    return expanded


def _pair_run_candidate(
    run: list[FramePairMetrics],
    timestamps: tuple[float, ...],
) -> IntervalCandidate:
    positions = tuple(range(run[0].left_position, run[-1].right_position + 1))
    return IntervalCandidate(
        start_seconds=timestamps[positions[0]],
        end_seconds=timestamps[positions[-1]],
        evidence_indices=positions,
        group_index=run[0].scene_group,
    )


class PossibleFreezeDetector:
    """Detect sustained similarity while treating scene cuts as hard resets."""

    id = "possible_freeze"
    display_name = "Possible frozen or repeated frames"
    version = "1.0.0"
    description = "Finds sustained adjacent sampled frames with low differences."
    requirements = DetectorRequirements(estimated_cost=EstimatedCost.LOW)
    default_enabled = True
    config_model = PossibleFreezeConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        """Return neutral repeated-frame observations within scene boundaries."""
        effective = PossibleFreezeConfig.model_validate(config.model_dump())
        pair_metrics = build_pair_metrics(context)
        timestamps = tuple(sample.timestamp_seconds for sample in context.frame_samples)
        candidates = find_freeze_candidates(
            pair_metrics,
            timestamps,
            context.metadata.duration_seconds,
            {
                scene.scene_index: scene_end_seconds(
                    context.scenes,
                    scene_index=scene.scene_index,
                    video_duration_seconds=context.metadata.duration_seconds,
                )
                for scene in context.scenes
            },
            effective,
        )
        pair_by_right_position = {
            metrics.right_position: metrics for metrics in pair_metrics
        }
        findings: list[Finding] = []
        for candidate in candidates:
            relevant_pairs = [
                metrics
                for metrics in pair_metrics
                if metrics.scene_group == candidate.group_index
                and metrics.left_position >= candidate.evidence_indices[0]
                and metrics.right_position <= candidate.evidence_indices[-1]
                and is_similar_pair(metrics, effective)
            ]
            mean_pixel_difference = sum(
                item.pixel_difference for item in relevant_pairs
            ) / len(relevant_pairs)
            mean_hash_distance = sum(
                item.hash_distance for item in relevant_pairs
            ) / len(relevant_pairs)
            pixel_similarity = max(
                0.0,
                1.0 - mean_pixel_difference / effective.max_pixel_difference,
            )
            hash_denominator = max(1, effective.max_hash_distance)
            hash_similarity = max(
                0.0,
                1.0 - mean_hash_distance / hash_denominator,
            )
            time_range = TimeRange(
                start_seconds=candidate.start_seconds,
                end_seconds=candidate.end_seconds,
            )
            selected_positions = select_representative_indices(
                candidate.evidence_indices,
                count=EVIDENCE_FRAME_COUNT,
            )
            evidence: list[Evidence] = []
            labels = ("First", "Middle", "Final")
            for label, position in zip(labels, selected_positions, strict=True):
                pair = pair_by_right_position.get(position)
                evidence.append(
                    Evidence(
                        evidence_type="sampled_frame",
                        timestamp_seconds=context.frame_samples[
                            position
                        ].timestamp_seconds,
                        relative_path=context.frame_samples[position].relative_path,
                        description=(
                            f"{label} sampled frame in the sustained similar-frame "
                            "interval."
                        ),
                        metadata={
                            "pixel_difference_from_previous": (
                                pair.pixel_difference if pair is not None else None
                            ),
                            "hash_distance_from_previous": (
                                pair.hash_distance if pair is not None else None
                            ),
                        },
                    )
                )
            findings.append(
                Finding(
                    id=make_finding_id(
                        input_hash=context.input_hash,
                        detector_id=self.id,
                        time_range=time_range,
                    ),
                    detector_id=self.id,
                    detector_version=self.version,
                    title="Possible frozen or repeated frames",
                    description=(
                        "Adjacent sampled frames remained within both configured "
                        "pixel and low-resolution structural difference thresholds "
                        "inside one scene. This is a heuristic observation, not "
                        "proof that playback froze."
                    ),
                    severity=effective.severity,
                    score=min(
                        1.0,
                        (pixel_similarity + hash_similarity) / 2.0,
                    ),
                    confidence=min(
                        1.0,
                        candidate.duration_seconds / effective.min_duration_seconds,
                    ),
                    time_range=time_range,
                    evidence=evidence,
                    tags=["possible_freeze", "repeated_frames"],
                    parameters=effective.model_dump(mode="json"),
                    limitations=[
                        "Intentional static shots, still images, or very subtle "
                        "motion may produce the same observable similarity.",
                        "Sampling can miss motion between extracted frames and "
                        "does not prove a decoder or playback fault.",
                    ],
                )
            )
        return findings
