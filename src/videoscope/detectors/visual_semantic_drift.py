"""Optional within-scene visual embedding drift diagnostics."""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from videoscope.ai import (
    MODEL_RUNTIME_CACHE_KEY,
    ImageEmbeddingInput,
    ModelRuntimeManager,
)
from videoscope.ai.providers import (
    DEFAULT_DINOV2_MODEL_ID,
    DEFAULT_OPENCLIP_MODEL_ID,
    DINOV2_PREPROCESSING_VERSION,
    DINOV2_PROVIDER_ID,
    OPENCLIP_PREPROCESSING_VERSION,
    OPENCLIP_PROVIDER_ID,
)
from videoscope.detectors.image_features import resolve_sample_path
from videoscope.detectors.intervals import IntervalCandidate, merge_intervals
from videoscope.detectors.models import (
    DETECTOR_DIAGNOSTICS_CACHE_KEY,
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
from videoscope.scenes import VideoScene
from videoscope.video import FrameSample

np: Any = importlib.import_module("numpy")
_TIME_TOLERANCE = 1e-9

LIMITATIONS = (
    "Rapid camera movement can produce high visual embedding distances.",
    "Large occlusion can produce high visual embedding distances.",
    "Reasonable deformation can produce high visual embedding distances.",
    "Large lighting changes can produce high visual embedding distances.",
    "This is heuristic visual consistency analysis, not identity recognition.",
)


class VisualSemanticDriftConfig(BaseModel):
    """Model selection and scene-relative drift thresholds."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = DINOV2_PROVIDER_ID
    model_id: str | None = None
    preprocessing_version: str | None = None
    long_gap_seconds: float = Field(default=1.5, gt=0, allow_inf_nan=False)
    scene_boundary_guard_seconds: float = Field(
        default=0.25,
        ge=0,
        allow_inf_nan=False,
    )
    minimum_scene_samples: int = Field(default=4, ge=3)
    minimum_baseline_pairs: int = Field(default=3, ge=1)
    baseline_mad_multiplier: float = Field(
        default=3.0,
        gt=0,
        allow_inf_nan=False,
    )
    minimum_distance_threshold: float = Field(
        default=0.15,
        ge=0,
        le=2,
        allow_inf_nan=False,
    )
    min_duration_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    merge_gap_seconds: float = Field(default=0.5, ge=0, allow_inf_nan=False)
    severity: Severity = Severity.MEDIUM

    @field_validator("provider_id")
    @classmethod
    def normalize_provider_id(cls, value: str) -> str:
        """Reject blank provider identifiers."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider_id must not be blank")
        return normalized

    @field_validator("model_id", "preprocessing_version")
    @classmethod
    def normalize_optional_identifiers(cls, value: str | None) -> str | None:
        """Normalize explicit model and preprocessing identifiers."""
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("model identifiers must not be blank")
        return normalized

    @model_validator(mode="after")
    def select_provider_defaults(self) -> Self:
        """Fill stable defaults for supported production providers."""
        defaults = {
            DINOV2_PROVIDER_ID: (
                DEFAULT_DINOV2_MODEL_ID,
                DINOV2_PREPROCESSING_VERSION,
            ),
            OPENCLIP_PROVIDER_ID: (
                DEFAULT_OPENCLIP_MODEL_ID,
                OPENCLIP_PREPROCESSING_VERSION,
            ),
        }
        selected = defaults.get(self.provider_id)
        if selected is None and (
            self.model_id is None or self.preprocessing_version is None
        ):
            raise ValueError(
                "custom providers require model_id and preprocessing_version"
            )
        if selected is not None:
            if self.model_id is None:
                self.model_id = selected[0]
            if self.preprocessing_version is None:
                self.preprocessing_version = selected[1]
        return self

    def resolved_model_id(self) -> str:
        """Return the validated non-optional model identifier."""
        if self.model_id is None:
            raise ValueError("model_id was not resolved")
        return self.model_id

    def resolved_preprocessing_version(self) -> str:
        """Return the validated non-optional preprocessing version."""
        if self.preprocessing_version is None:
            raise ValueError("preprocessing_version was not resolved")
        return self.preprocessing_version


@dataclass(frozen=True, slots=True)
class SceneSampleGroup:
    """Eligible sampled frame positions for one scene."""

    scene: VideoScene
    positions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class EmbeddingComparison:
    """One within-scene cosine-distance observation."""

    scene_index: int
    left_position: int
    right_position: int
    comparison_type: str
    timestamp_seconds: float
    distance: float

    def as_json(
        self,
        samples: tuple[FrameSample, ...],
        *,
        threshold: float,
    ) -> dict[str, Any]:
        """Return a path-free representation for report diagnostics."""
        return {
            "comparison_type": self.comparison_type,
            "left_timestamp_seconds": samples[self.left_position].timestamp_seconds,
            "right_timestamp_seconds": samples[self.right_position].timestamp_seconds,
            "timestamp_seconds": self.timestamp_seconds,
            "distance": self.distance,
            "is_abrupt": self.distance > threshold,
        }


@dataclass(frozen=True, slots=True)
class SceneDistanceSummary:
    """Robust scene baseline plus its complete distance series."""

    scene: VideoScene
    positions: tuple[int, ...]
    comparisons: tuple[EmbeddingComparison, ...]
    median_distance: float
    mad_distance: float
    robust_scale: float
    threshold: float

    def as_json(self, samples: tuple[FrameSample, ...]) -> dict[str, Any]:
        """Return reproducible scene diagnostics without local file paths."""
        peak = max(
            self.comparisons,
            key=lambda item: (item.distance, -item.timestamp_seconds),
            default=None,
        )
        return {
            "scene_index": self.scene.scene_index,
            "start_seconds": self.scene.start_seconds,
            "end_seconds": self.scene.end_seconds,
            "eligible_sample_count": len(self.positions),
            "comparison_count": len(self.comparisons),
            "baseline": {
                "median_distance": self.median_distance,
                "mad_distance": self.mad_distance,
                "robust_scale": self.robust_scale,
                "threshold": self.threshold,
            },
            "peak": (
                None
                if peak is None
                else {
                    "timestamp_seconds": peak.timestamp_seconds,
                    "distance": peak.distance,
                    "comparison_type": peak.comparison_type,
                }
            ),
            "distance_series": [
                item.as_json(samples, threshold=self.threshold)
                for item in self.comparisons
            ],
        }


def select_scene_sample_groups(
    samples: tuple[FrameSample, ...],
    scenes: tuple[VideoScene, ...],
    *,
    video_duration_seconds: float,
    boundary_guard_seconds: float,
    minimum_scene_samples: int,
) -> tuple[SceneSampleGroup, ...]:
    """Select guarded samples without allowing a frame into two scenes."""
    groups: list[SceneSampleGroup] = []
    for scene in scenes:
        is_final = math.isclose(
            scene.end_seconds,
            video_duration_seconds,
            rel_tol=0,
            abs_tol=_TIME_TOLERANCE,
        )
        positions = tuple(
            position
            for position, sample in enumerate(samples)
            if sample.timestamp_seconds
            >= scene.start_seconds + boundary_guard_seconds - _TIME_TOLERANCE
            and sample.timestamp_seconds
            <= scene.end_seconds - boundary_guard_seconds + _TIME_TOLERANCE
            and (
                sample.timestamp_seconds < scene.end_seconds - _TIME_TOLERANCE
                or is_final
            )
        )
        if len(positions) >= minimum_scene_samples:
            groups.append(SceneSampleGroup(scene=scene, positions=positions))
    return tuple(groups)


def cosine_distance(left: Any, right: Any) -> float:
    """Return stable cosine distance in the normalized range ``[0, 2]``."""
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    if (
        left_array.ndim != 1
        or right_array.ndim != 1
        or left_array.shape != right_array.shape
    ):
        raise ValueError("embedding vectors must have equal one-dimensional shapes")
    left_norm = float(np.linalg.norm(left_array))
    right_norm = float(np.linalg.norm(right_array))
    if left_norm <= 0 or right_norm <= 0:
        raise ValueError("embedding vectors must have non-zero norms")
    similarity = float(np.dot(left_array, right_array) / (left_norm * right_norm))
    return float(np.clip(1.0 - similarity, 0.0, 2.0))


def build_scene_comparisons(
    group: SceneSampleGroup,
    samples: tuple[FrameSample, ...],
    embeddings_by_position: dict[int, Any],
    *,
    long_gap_seconds: float,
) -> tuple[EmbeddingComparison, ...]:
    """Compare adjacent and deterministic longer-gap frames within one scene."""
    positions = group.positions
    pairs: list[tuple[int, int, str]] = [
        (positions[index], positions[index + 1], "adjacent")
        for index in range(len(positions) - 1)
    ]
    for left_offset, left_position in enumerate(positions):
        left_time = samples[left_position].timestamp_seconds
        right_position = next(
            (
                candidate
                for candidate in positions[left_offset + 2 :]
                if samples[candidate].timestamp_seconds - left_time
                >= long_gap_seconds - _TIME_TOLERANCE
            ),
            None,
        )
        if right_position is not None:
            pairs.append((left_position, right_position, "long_gap"))
    comparisons = [
        EmbeddingComparison(
            scene_index=group.scene.scene_index,
            left_position=left,
            right_position=right,
            comparison_type=comparison_type,
            timestamp_seconds=(
                samples[left].timestamp_seconds + samples[right].timestamp_seconds
            )
            / 2.0,
            distance=cosine_distance(
                embeddings_by_position[left],
                embeddings_by_position[right],
            ),
        )
        for left, right, comparison_type in pairs
    ]
    return tuple(
        sorted(
            comparisons,
            key=lambda item: (
                item.timestamp_seconds,
                item.comparison_type,
                item.left_position,
                item.right_position,
            ),
        )
    )


def summarize_scene_distances(
    group: SceneSampleGroup,
    comparisons: tuple[EmbeddingComparison, ...],
    *,
    minimum_distance_threshold: float,
    baseline_mad_multiplier: float,
) -> SceneDistanceSummary:
    """Compute a robust scene-relative threshold from its distance series."""
    distances = np.asarray(
        [comparison.distance for comparison in comparisons],
        dtype=np.float64,
    )
    median = float(np.median(distances))
    mad = float(np.median(np.abs(distances - median)))
    robust_scale = 1.4826 * mad
    threshold = min(
        2.0,
        max(
            minimum_distance_threshold,
            median + baseline_mad_multiplier * robust_scale,
        ),
    )
    return SceneDistanceSummary(
        scene=group.scene,
        positions=group.positions,
        comparisons=comparisons,
        median_distance=median,
        mad_distance=mad,
        robust_scale=robust_scale,
        threshold=threshold,
    )


def drift_interval_candidates(
    summaries: tuple[SceneDistanceSummary, ...],
    samples: tuple[FrameSample, ...],
    *,
    merge_gap_seconds: float,
    min_duration_seconds: float,
) -> tuple[IntervalCandidate, ...]:
    """Merge adjacent abrupt comparisons without crossing a scene boundary."""
    raw = [
        IntervalCandidate(
            start_seconds=samples[comparison.left_position].timestamp_seconds,
            end_seconds=samples[comparison.right_position].timestamp_seconds,
            evidence_indices=(
                comparison.left_position,
                comparison.right_position,
            ),
            group_index=summary.scene.scene_index,
        )
        for summary in summaries
        for comparison in summary.comparisons
        if comparison.distance > summary.threshold
    ]
    return tuple(
        merge_intervals(
            raw,
            merge_gap_seconds=merge_gap_seconds,
            min_duration_seconds=min_duration_seconds,
        )
    )


class VisualSemanticDriftDetector:
    """Find abrupt scene-relative changes in local visual embeddings."""

    id = "visual_semantic_drift"
    display_name = "Visual semantic drift"
    version = "1.0.0"
    description = (
        "Compares shared visual embeddings only within each scene and reports "
        "abrupt changes relative to that scene's distance baseline."
    )
    requirements = DetectorRequirements(
        requires_prompt=False,
        requires_gpu=False,
        requires_network=False,
        optional_packages=("open-clip-torch", "torch", "torchvision"),
        estimated_cost=EstimatedCost.HIGH,
    )
    default_enabled = True
    config_model = VisualSemanticDriftConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        """Encode eligible frames once and report within-scene abrupt drift."""
        effective = VisualSemanticDriftConfig.model_validate(config.model_dump())
        groups = select_scene_sample_groups(
            context.frame_samples,
            context.scenes,
            video_duration_seconds=context.metadata.duration_seconds,
            boundary_guard_seconds=effective.scene_boundary_guard_seconds,
            minimum_scene_samples=effective.minimum_scene_samples,
        )
        if not groups:
            self._record_diagnostics(context, effective=effective, summaries=())
            return []
        runtime = context.shared_cache.get(MODEL_RUNTIME_CACHE_KEY)
        if not isinstance(runtime, ModelRuntimeManager):
            raise RuntimeError(
                "visual_semantic_drift requires the shared AI model runtime"
            )
        positions = tuple(
            sorted({position for group in groups for position in group.positions})
        )
        inputs = tuple(
            ImageEmbeddingInput(
                path=resolve_sample_path(
                    context.workspace,
                    context.frame_samples[position],
                ),
                video_hash=context.input_hash,
                timestamp_seconds=context.frame_samples[position].timestamp_seconds,
                preprocessing_version=effective.resolved_preprocessing_version(),
            )
            for position in positions
        )
        batch = runtime.encode_images(
            effective.provider_id,
            effective.resolved_model_id(),
            inputs,
        )
        embeddings_by_position = {
            position: batch.embeddings[index]
            for index, position in enumerate(positions)
        }
        summaries: list[SceneDistanceSummary] = []
        for group in groups:
            comparisons = build_scene_comparisons(
                group,
                context.frame_samples,
                embeddings_by_position,
                long_gap_seconds=effective.long_gap_seconds,
            )
            if len(comparisons) < effective.minimum_baseline_pairs:
                continue
            summaries.append(
                summarize_scene_distances(
                    group,
                    comparisons,
                    minimum_distance_threshold=(effective.minimum_distance_threshold),
                    baseline_mad_multiplier=effective.baseline_mad_multiplier,
                )
            )
        finalized = tuple(summaries)
        self._record_diagnostics(
            context,
            effective=effective,
            summaries=finalized,
        )
        candidates = drift_interval_candidates(
            finalized,
            context.frame_samples,
            merge_gap_seconds=effective.merge_gap_seconds,
            min_duration_seconds=effective.min_duration_seconds,
        )
        return [
            self._finding_for_candidate(
                context,
                effective=effective,
                summaries=finalized,
                candidate=candidate,
            )
            for candidate in candidates
        ]

    @staticmethod
    def _record_diagnostics(
        context: AnalysisContext,
        *,
        effective: VisualSemanticDriftConfig,
        summaries: tuple[SceneDistanceSummary, ...],
    ) -> None:
        store = context.shared_cache.setdefault(
            DETECTOR_DIAGNOSTICS_CACHE_KEY,
            {},
        )
        if not isinstance(store, dict):
            raise TypeError("detector diagnostics cache has an invalid type")
        peaks = sorted(
            (comparison for summary in summaries for comparison in summary.comparisons),
            key=lambda item: (
                -item.distance,
                item.timestamp_seconds,
                item.scene_index,
            ),
        )[:5]
        store[VisualSemanticDriftDetector.id] = {
            "provider_id": effective.provider_id,
            "model_id": effective.resolved_model_id(),
            "preprocessing_version": (effective.resolved_preprocessing_version()),
            "method": "within_scene_robust_cosine_distance",
            "scenes": [summary.as_json(context.frame_samples) for summary in summaries],
            "distance_series_summary": {
                "comparison_count": sum(
                    len(summary.comparisons) for summary in summaries
                ),
                "peak_count": len(peaks),
                "peaks": [
                    {
                        "scene_index": peak.scene_index,
                        "timestamp_seconds": peak.timestamp_seconds,
                        "distance": peak.distance,
                        "comparison_type": peak.comparison_type,
                    }
                    for peak in peaks
                ],
            },
            "limitations": list(LIMITATIONS),
        }

    def _finding_for_candidate(
        self,
        context: AnalysisContext,
        *,
        effective: VisualSemanticDriftConfig,
        summaries: tuple[SceneDistanceSummary, ...],
        candidate: IntervalCandidate,
    ) -> Finding:
        summary = next(
            item
            for item in summaries
            if item.scene.scene_index == candidate.group_index
        )
        relevant = tuple(
            comparison
            for comparison in summary.comparisons
            if comparison.distance > summary.threshold
            and context.frame_samples[comparison.right_position].timestamp_seconds
            >= candidate.start_seconds - _TIME_TOLERANCE
            and context.frame_samples[comparison.left_position].timestamp_seconds
            <= candidate.end_seconds + _TIME_TOLERANCE
        )
        peak = max(
            relevant,
            key=lambda item: (
                item.distance,
                -item.timestamp_seconds,
                -item.left_position,
            ),
        )
        left_sample = context.frame_samples[peak.left_position]
        right_sample = context.frame_samples[peak.right_position]
        time_range = TimeRange(
            start_seconds=candidate.start_seconds,
            end_seconds=candidate.end_seconds,
        )
        threshold_room = max(1e-12, 2.0 - summary.threshold)
        score = min(
            1.0,
            max(0.0, (peak.distance - summary.threshold) / threshold_room),
        )
        confidence = min(
            1.0,
            len(summary.comparisons) / (effective.minimum_baseline_pairs * 2),
        )
        common_metadata: dict[str, Any] = {
            "scene_index": summary.scene.scene_index,
            "scene_baseline_median": summary.median_distance,
            "scene_baseline_mad": summary.mad_distance,
            "scene_threshold": summary.threshold,
            "peak_distance": peak.distance,
            "comparison_type": peak.comparison_type,
            "provider_id": effective.provider_id,
            "model_id": effective.resolved_model_id(),
        }
        return Finding(
            id=make_finding_id(
                input_hash=context.input_hash,
                detector_id=self.id,
                time_range=time_range,
            ),
            detector_id=self.id,
            detector_version=self.version,
            title="Abrupt visual semantic drift",
            description=(
                "Within one scene, visual embedding distance rose abruptly "
                "above that scene's robust baseline. This is an observable "
                "feature-space change, not a determination about identity."
            ),
            severity=effective.severity,
            score=score,
            confidence=confidence,
            time_range=time_range,
            evidence=[
                Evidence(
                    evidence_type="sampled_frame",
                    timestamp_seconds=left_sample.timestamp_seconds,
                    relative_path=left_sample.relative_path,
                    description="Sample immediately before the peak comparison.",
                    metadata={**common_metadata, "peak_frame_role": "before"},
                ),
                Evidence(
                    evidence_type="sampled_frame",
                    timestamp_seconds=right_sample.timestamp_seconds,
                    relative_path=right_sample.relative_path,
                    description="Sample immediately after the peak comparison.",
                    metadata={**common_metadata, "peak_frame_role": "after"},
                ),
            ],
            tags=["visual_semantic_drift", "embedding_consistency"],
            parameters=effective.model_dump(mode="json"),
            limitations=list(LIMITATIONS),
        )
