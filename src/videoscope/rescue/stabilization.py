"""Bounded CPU motion assessment and streaming stabilization rendering.

This module only compensates measured camera-like affine motion.  It never
claims to restore detail or infer pixels which were not present in the source.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from queue import Queue
from typing import TYPE_CHECKING, Any, Final, Literal, Self, cast

import numpy as np
from numpy.typing import NDArray
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from videoscope.rescue.encoding import canonical_video_encode_arguments
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueMediaError,
    RescueQualificationUnavailableError,
)
from videoscope.rescue.models import (
    CanonicalVideoEncodeContract,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    StabilizationQualificationProfile,
    canonical_video_encode_contract,
    make_rescue_action_id,
    make_rescue_plan_digest,
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
    exact_timestamp_tolerance_seconds: float = Field(
        default=0.001, gt=0, le=0.02, allow_inf_nan=False
    )
    minimum_motion_amplitude_pixels: float = Field(
        default=1.0, ge=0, le=64, allow_inf_nan=False
    )
    minimum_active_correction_count: int = Field(default=1, ge=1, le=120)
    range_padding_seconds: float = Field(default=1.0, ge=0, le=2.0, allow_inf_nan=False)
    accepted_ranges: tuple[tuple[float, float], ...] = ()
    maximum_bridged_low_confidence_samples: int = Field(default=0, ge=0, le=3)
    minimum_background_coverage: float = Field(
        default=0.05, gt=0, le=1, allow_inf_nan=False
    )
    minimum_anchor_inlier_ratio: float = Field(
        default=0.65, gt=0, le=1, allow_inf_nan=False
    )
    maximum_anchor_residual_pixels: float = Field(
        default=1.0, gt=0, le=16, allow_inf_nan=False
    )
    maximum_rotation_degrees: float = Field(
        default=3.0, gt=0, le=15, allow_inf_nan=False
    )
    maximum_scale_excursion: float = Field(
        default=0.05, gt=0, lt=0.25, allow_inf_nan=False
    )
    maximum_intentional_trend_pixels_per_frame: float = Field(
        default=1.0, ge=0, le=32, allow_inf_nan=False
    )
    maximum_consecutive_low_confidence_frames: int = Field(default=1, ge=0, le=3)
    maximum_tracking_roundtrip_error_pixels: float = Field(
        default=1.5, gt=0, le=8, allow_inf_nan=False
    )
    minimum_phase_correlation_response: float = Field(
        default=0.2, gt=0, le=1, allow_inf_nan=False
    )
    minimum_phase_fallback_inlier_ratio: float = Field(
        default=0.35, gt=0, le=1, allow_inf_nan=False
    )
    maximum_phase_affine_residual_pixels: float = Field(
        default=2.0, gt=0, le=16, allow_inf_nan=False
    )
    maximum_phase_affine_disagreement_pixels: float = Field(
        default=1.0, gt=0, le=8, allow_inf_nan=False
    )
    maximum_regional_translation_deviation_pixels: float = Field(
        default=1.5, gt=0, le=8, allow_inf_nan=False
    )
    strong_phase_correlation_response: float = Field(
        default=0.6, gt=0, le=1, allow_inf_nan=False
    )
    strong_regional_translation_deviation_pixels: float = Field(
        default=0.5, gt=0, le=4, allow_inf_nan=False
    )
    phase_region_rows: int = Field(default=2, ge=2, le=4)
    phase_region_columns: int = Field(default=3, ge=2, le=4)
    minimum_consistent_phase_regions: int = Field(default=4, ge=2, le=16)
    source_rate_cap_fps: float = Field(default=30.0, gt=0, le=30, allow_inf_nan=False)
    maximum_frame_inventory: int = Field(default=900, ge=2, le=5000)
    residual_goal_median_pixels: float = Field(
        default=0.5, gt=0, le=4, allow_inf_nan=False
    )
    residual_goal_p90_pixels: float = Field(
        default=1.0, gt=0, le=8, allow_inf_nan=False
    )
    transition_phase_region_rows: int = Field(default=3, ge=3, le=3)
    transition_phase_region_columns: int = Field(default=4, ge=4, le=4)
    minimum_transition_phase_response: float = Field(
        default=0.2, gt=0, le=1, allow_inf_nan=False
    )
    minimum_transition_nonempty_regions: int = Field(default=10, ge=10, le=12)
    minimum_transition_tile_luma_std: float = Field(
        default=8.0, gt=0, le=64, allow_inf_nan=False
    )
    minimum_transition_alignment_gain: float = Field(
        default=0.0, ge=0, lt=1, allow_inf_nan=False
    )
    maximum_transition_regional_p90_pixels: float = Field(
        default=1.5, gt=0, le=8, allow_inf_nan=False
    )
    minimum_transition_lk_track_count: int = Field(default=12, ge=8, le=1000)
    maximum_transition_lk_feature_inventory: int = Field(default=500, ge=12, le=1000)
    minimum_transition_lk_track_ratio: float = Field(
        default=0.1, gt=0, le=1, allow_inf_nan=False
    )
    minimum_transition_lk_spatial_coverage: float = Field(
        default=0.5, gt=0, le=1, allow_inf_nan=False
    )
    maximum_transition_lk_residual_pixels: float = Field(
        default=1.5, gt=0, le=8, allow_inf_nan=False
    )
    maximum_transition_dense_residual_pixels: float = Field(
        default=4.0, gt=0, le=8, allow_inf_nan=False
    )
    minimum_transition_dense_coherent_ratio: float = Field(
        default=0.6, gt=0, le=1, allow_inf_nan=False
    )
    maximum_transition_vector_disagreement_pixels: float = Field(
        default=1.5, gt=0, le=8, allow_inf_nan=False
    )
    maximum_transition_seam_discontinuity_pixels: float = Field(
        default=0.25, ge=0, le=4, allow_inf_nan=False
    )
    maximum_transition_candidate_frames: int = Field(default=120, ge=2, le=240)
    maximum_transition_duration_seconds: float = Field(
        default=1.0, gt=0, le=2, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def _validate_anchor_relationships(self) -> StabilizationConfig:
        if self.residual_goal_median_pixels > self.residual_goal_p90_pixels:
            raise ValueError("median residual goal must not exceed the P90 goal")
        if self.residual_goal_p90_pixels > self.maximum_anchor_residual_pixels:
            raise ValueError("residual goals must fit within the anchor residual gate")
        if self.minimum_consistent_phase_regions > (
            self.phase_region_rows * self.phase_region_columns
        ):
            raise ValueError("consistent phase regions exceed the configured grid")
        if (
            self.strong_phase_correlation_response
            < self.minimum_phase_correlation_response
        ):
            raise ValueError("strong phase response must not be below its minimum")
        if (
            self.strong_regional_translation_deviation_pixels
            > self.maximum_regional_translation_deviation_pixels
        ):
            raise ValueError("strong regional deviation must fit its maximum")
        if (
            self.minimum_transition_lk_track_count
            > self.maximum_transition_lk_feature_inventory
        ):
            raise ValueError("transition LK track count exceeds the feature inventory")
        previous_end = -1.0
        for start, end in self.accepted_ranges:
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0
                or end <= start
                or start < previous_end
            ):
                raise ValueError(
                    "accepted stabilization ranges must be ordered and finite"
                )
            previous_end = end
        return self


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


class TransitionConsensusStep(_StabilizationModel):
    """Fresh three-estimator source evidence for one actual-PTS frame pair."""

    previous_timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    current_timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    translation_x: float = Field(ge=-4096, le=4096, allow_inf_nan=False)
    translation_y: float = Field(ge=-4096, le=4096, allow_inf_nan=False)
    residual_pixels: float = Field(ge=0, allow_inf_nan=False)


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


STABILIZATION_QUALIFICATION_VERSION: Final = "1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class StabilizationQualificationThresholdsV1(_StabilizationModel):
    """The unchanged required STABILIZE gates used during qualification."""

    maximum_residual_median_pixels: float = Field(ge=0, allow_inf_nan=False)
    maximum_residual_p90_pixels: float = Field(ge=0, allow_inf_nan=False)
    maximum_crop_ratio: float = Field(ge=0, lt=1, allow_inf_nan=False)
    minimum_transition_consensus_coverage_ratio: float = Field(
        default=1.0, ge=1.0, le=1.0, allow_inf_nan=False
    )
    maximum_transition_consensus_p90_pixels: float = Field(ge=0, allow_inf_nan=False)
    maximum_transition_seam_residual_pixels: float = Field(ge=0, allow_inf_nan=False)
    maximum_transition_vector_disagreement_pixels: float = Field(
        ge=0, allow_inf_nan=False
    )
    minimum_transition_coverage_ratio: float = Field(
        default=1.0, ge=1.0, le=1.0, allow_inf_nan=False
    )


class StabilizationQualificationMetricsV1(_StabilizationModel):
    """Complete independent measurements for one encoded candidate."""

    range_coverage_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    expected_frames: float = Field(ge=1, allow_inf_nan=False)
    reliable_transforms: float = Field(ge=0, allow_inf_nan=False)
    residual_median_pixels: float = Field(ge=0, allow_inf_nan=False)
    residual_p90_pixels: float = Field(ge=0, allow_inf_nan=False)
    crop_ratio: float = Field(ge=0, lt=1, allow_inf_nan=False)
    transition_consensus_coverage_ratio: float = Field(ge=0, le=1, allow_inf_nan=False)
    transition_consensus_p90_pixels: float = Field(ge=0, allow_inf_nan=False)
    transition_seam_residual_pixels: float = Field(ge=0, allow_inf_nan=False)
    transition_expected_frames: float = Field(ge=1, allow_inf_nan=False)
    transition_reliable_frames: float = Field(ge=0, allow_inf_nan=False)
    transition_boundary_path_residual_pixels: float = Field(ge=0, allow_inf_nan=False)


def stabilization_actual_pts_digest(actual_pts: Sequence[float]) -> str:
    """Return a canonical digest for one normalized actual-PTS inventory."""
    payload: list[str] = []
    for value in actual_pts:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("stabilization actual PTS inventory is invalid")
        normalized = float(value)
        if not math.isfinite(normalized) or normalized < 0:
            raise ValueError("stabilization actual PTS inventory is invalid")
        payload.append(normalized.hex())
    return sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class StabilizationImmediateParentHandle:
    """Runtime-only handle for the exact generation immediately before STABILIZE."""

    root: Path
    path: Path
    draft_plan_digest: str
    stabilization_action_id: str
    preceding_action_ids: tuple[str, ...]
    sha256: str
    encode_contract: CanonicalVideoEncodeContract
    actual_pts: tuple[float, ...]
    normalized_pts_digest: str
    stream_topology_digest: str
    frame_count: int
    cleanup_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "actual_pts", tuple(self.actual_pts))
        object.__setattr__(
            self, "preceding_action_ids", tuple(self.preceding_action_ids)
        )
        object.__setattr__(self, "cleanup_paths", tuple(map(Path, self.cleanup_paths)))
        digests = (
            self.draft_plan_digest,
            self.sha256,
            self.normalized_pts_digest,
            self.stream_topology_digest,
        )
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in digests
        ):
            raise ValueError("stabilization immediate-parent digest is invalid")
        if (
            not self.stabilization_action_id
            or any(not action_id for action_id in self.preceding_action_ids)
            or len(self.preceding_action_ids) != len(set(self.preceding_action_ids))
        ):
            raise ValueError("stabilization immediate-parent action chain is invalid")
        if self.frame_count != len(
            self.actual_pts
        ) or self.normalized_pts_digest != stabilization_actual_pts_digest(
            self.actual_pts
        ):
            raise ValueError("stabilization immediate-parent timeline is invalid")
        try:
            validate_stabilization_immediate_parent_handle(self, self.root)
        except RescueArtifactError as exc:
            raise ValueError(
                "stabilization immediate-parent path is not an owned regular "
                "non-symlink file in its private root"
            ) from exc


def validate_stabilization_immediate_parent_handle(
    parent: StabilizationImmediateParentHandle,
    expected_root: Path,
) -> None:
    """Validate one parent handle without following paths outside its owned root."""
    try:
        root = Path(parent.root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError
        resolved_root = root.resolve(strict=True)
        if resolved_root != Path(expected_root).resolve(strict=True):
            raise ValueError
        cleanup_paths = tuple(map(Path, parent.cleanup_paths))
        if not cleanup_paths:
            raise ValueError
        resolved_cleanup: list[Path] = []
        for cleanup_path in cleanup_paths:
            if cleanup_path.is_symlink() or not cleanup_path.is_file():
                raise ValueError
            resolved = cleanup_path.resolve(strict=True)
            resolved.relative_to(resolved_root)
            if resolved == resolved_root or resolved in resolved_cleanup:
                raise ValueError
            resolved_cleanup.append(resolved)
        parent_path = Path(parent.path)
        if parent_path.is_symlink() or not parent_path.is_file():
            raise ValueError
        resolved_parent = parent_path.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
        if resolved_parent not in resolved_cleanup:
            raise ValueError
        if _sha256_regular_file(parent_path) != parent.sha256:
            raise ValueError
    except (AttributeError, OSError, TypeError, ValueError):
        raise RescueArtifactError(
            "stabilization immediate-parent path is outside its private root"
        ) from None


def stabilization_qualification_thresholds(
    config: StabilizationConfig,
) -> StabilizationQualificationThresholdsV1:
    """Project the existing final verifier bounds without reinterpretation."""
    return StabilizationQualificationThresholdsV1(
        maximum_residual_median_pixels=config.residual_goal_median_pixels,
        maximum_residual_p90_pixels=config.residual_goal_p90_pixels,
        maximum_crop_ratio=config.max_crop_ratio,
        maximum_transition_consensus_p90_pixels=max(
            config.maximum_transition_regional_p90_pixels,
            config.maximum_transition_lk_residual_pixels,
            config.maximum_transition_dense_residual_pixels,
            config.maximum_transition_vector_disagreement_pixels,
        ),
        maximum_transition_seam_residual_pixels=(
            config.maximum_transition_seam_discontinuity_pixels
        ),
        maximum_transition_vector_disagreement_pixels=(
            config.maximum_transition_vector_disagreement_pixels
        ),
    )


def _qualification_ranges(
    ranges: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    normalized: list[tuple[float, float]] = []
    for start, end in ranges:
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(start) < 0
            or float(end) <= float(start)
        ):
            raise ValueError("stabilization qualification range is invalid")
        normalized.append((float(start), float(end)))
    ordered = tuple(normalized)
    if not ordered or ordered != tuple(sorted(ordered)):
        raise ValueError("stabilization qualification range order is invalid")
    if any(current[0] < previous[1] for previous, current in zip(ordered, ordered[1:])):
        raise ValueError("stabilization qualification ranges overlap")
    return ordered


class StabilizationProfileMeasurementV1(_StabilizationModel):
    """Path-free same-generation evidence for one optional estimator profile."""

    profile: StabilizationQualificationProfile
    parent_sha256: str = Field(pattern=_SHA256_PATTERN)
    control_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    encode_contract: CanonicalVideoEncodeContract
    source_ranges: tuple[tuple[float, float], ...]
    actual_pts: tuple[float, ...]
    parent_normalized_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    control_normalized_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    candidate_normalized_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    parent_stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    control_stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    candidate_stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    parent_frame_count: int = Field(ge=1, strict=True)
    control_frame_count: int = Field(ge=1, strict=True)
    candidate_frame_count: int = Field(ge=1, strict=True)
    control_recipe: Literal["same_parent_identity_v1"] = "same_parent_identity_v1"
    action_parameters: dict[str, JsonValue]
    metrics: StabilizationQualificationMetricsV1
    thresholds: StabilizationQualificationThresholdsV1

    @field_validator("source_ranges", mode="before")
    @classmethod
    def accept_json_ranges(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(
                tuple(item) if isinstance(item, list) else item for item in value
            )
        return value

    @field_validator("actual_pts", mode="before")
    @classmethod
    def accept_json_pts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        from videoscope.rescue.qualification import validate_path_free_canonical_json

        ranges = _qualification_ranges(self.source_ranges)
        object.__setattr__(self, "source_ranges", ranges)
        validate_path_free_canonical_json(
            self.action_parameters,
            field_name="stabilization qualification action parameters",
        )
        if any(
            str(key).startswith("stabilization_qualification")
            for key in self.action_parameters
        ):
            raise ValueError("stabilization qualification parameters are recursive")
        pts = tuple(float(value) for value in self.actual_pts)
        if (
            not pts
            or any(not math.isfinite(value) or value < 0 for value in pts)
            or any(current <= previous for previous, current in zip(pts, pts[1:]))
            or any(
                not any(start <= value < end for start, end in ranges) for value in pts
            )
        ):
            raise ValueError("stabilization qualification PTS inventory is invalid")
        object.__setattr__(self, "actual_pts", pts)
        pts_digest = stabilization_actual_pts_digest(pts)
        if (
            self.parent_normalized_pts_digest != pts_digest
            or self.control_normalized_pts_digest != pts_digest
            or self.candidate_normalized_pts_digest != pts_digest
        ):
            raise ValueError("stabilization qualification PTS binding differs")
        if (
            self.parent_frame_count != len(pts)
            or self.control_frame_count != len(pts)
            or self.candidate_frame_count != len(pts)
        ):
            raise ValueError("stabilization qualification frame inventory differs")
        if len({self.parent_sha256, self.control_sha256, self.candidate_sha256}) != 3:
            raise ValueError("stabilization qualification generations must be distinct")
        if (
            len(
                {
                    self.parent_stream_topology_digest,
                    self.control_stream_topology_digest,
                    self.candidate_stream_topology_digest,
                }
            )
            != 1
        ):
            raise ValueError("stabilization qualification topology differs")
        self._validate_action_parameters(pts, ranges)
        return self

    def _validate_action_parameters(
        self,
        pts: tuple[float, ...],
        ranges: tuple[tuple[float, float], ...],
    ) -> None:
        try:
            if (
                self.action_parameters.get("method") != "transition_anchor_v1"
                or self.action_parameters.get("algorithm_version") != "1"
                or self.action_parameters.get("estimator_algorithm_version")
                != self.profile.estimator_algorithm_version
            ):
                raise ValueError
            config = StabilizationConfig.model_validate_json(
                json.dumps(self.action_parameters.get("config"), ensure_ascii=False)
            )
            if config.accepted_ranges != ranges:
                raise ValueError
            if self.thresholds != stabilization_qualification_thresholds(config):
                raise ValueError
            transition = self.action_parameters.get("transition_range")
            following = self.action_parameters.get("following_anchor_range")
            transforms_raw = self.action_parameters.get("motion_transforms")
            declared_count = self.action_parameters.get("transition_correction_count")
            if (
                not isinstance(transition, (list, tuple))
                or len(transition) != 2
                or not isinstance(following, (list, tuple))
                or len(following) != 2
                or not isinstance(transforms_raw, (list, tuple))
                or isinstance(declared_count, bool)
                or not isinstance(declared_count, int)
            ):
                raise ValueError
            transition_values = tuple(float(cast(Any, value)) for value in transition)
            following_values = tuple(float(cast(Any, value)) for value in following)
            if (
                transition_values[0] >= transition_values[1]
                or following_values[0] >= following_values[1]
                or not math.isclose(
                    transition_values[1],
                    following_values[0],
                    rel_tol=0.0,
                    abs_tol=config.exact_timestamp_tolerance_seconds,
                )
                or ranges != ((transition_values[0], following_values[1]),)
            ):
                raise ValueError
            transforms = tuple(
                MotionTransform.model_validate(item) for item in transforms_raw
            )
            if (
                declared_count != len(transforms)
                or len(transforms) != len(pts)
                or tuple(item.timestamp_seconds for item in transforms) != pts
                or any(item.semantics != "frame_correction" for item in transforms)
            ):
                raise ValueError
            transition_count = sum(
                transition_values[0] <= value < transition_values[1] for value in pts
            )
            if self.metrics.expected_frames != float(
                len(pts)
            ) or self.metrics.transition_expected_frames != float(transition_count):
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "stabilization qualification action/range contract differs"
            ) from exc

    @property
    def passed(self) -> bool:
        metrics = self.metrics
        thresholds = self.thresholds
        return bool(
            math.isclose(metrics.range_coverage_ratio, 1.0, rel_tol=0.0, abs_tol=1e-9)
            and metrics.reliable_transforms == metrics.expected_frames
            and metrics.residual_median_pixels
            <= thresholds.maximum_residual_median_pixels
            and metrics.residual_p90_pixels <= thresholds.maximum_residual_p90_pixels
            and metrics.crop_ratio <= thresholds.maximum_crop_ratio
            and math.isclose(
                metrics.transition_consensus_coverage_ratio,
                thresholds.minimum_transition_consensus_coverage_ratio,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            and metrics.transition_consensus_p90_pixels
            <= thresholds.maximum_transition_consensus_p90_pixels
            and metrics.transition_seam_residual_pixels
            <= thresholds.maximum_transition_seam_residual_pixels
            and metrics.transition_boundary_path_residual_pixels
            <= thresholds.maximum_transition_seam_residual_pixels
            and metrics.transition_boundary_path_residual_pixels
            <= thresholds.maximum_transition_vector_disagreement_pixels
            and metrics.transition_reliable_frames == metrics.transition_expected_frames
        )


class StabilizationQualificationEvidenceV1(_StabilizationModel):
    """Canonical optional qualification envelope embedded in a final action."""

    schema_version: Literal["1"] = "1"
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    draft_plan_digest: str = Field(pattern=_SHA256_PATTERN)
    draft_action_id: str = Field(min_length=1)
    draft_parameters: dict[str, JsonValue]
    source_ranges: tuple[tuple[float, float], ...]
    encode_contract: CanonicalVideoEncodeContract
    parent_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_encode_contract: CanonicalVideoEncodeContract
    parent_actual_pts: tuple[float, ...]
    parent_normalized_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    parent_frame_count: int = Field(ge=1, strict=True)
    parent_stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    preceding_action_ids: tuple[str, ...]
    authoritative_actual_pts: tuple[float, ...]
    authoritative_actual_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    authoritative_frame_count: int = Field(ge=1, strict=True)
    authoritative_parent_stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    configured_profiles: tuple[StabilizationQualificationProfile, ...]
    actual_profile_order: tuple[str, ...]
    profile_measurements: tuple[StabilizationProfileMeasurementV1, ...]
    selected_profile_id: str | None = None

    @field_validator(
        "source_ranges",
        "parent_actual_pts",
        "preceding_action_ids",
        "authoritative_actual_pts",
        "configured_profiles",
        "actual_profile_order",
        "profile_measurements",
        mode="before",
    )
    @classmethod
    def accept_json_arrays(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(
                tuple(item) if isinstance(item, list) else item for item in value
            )
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        from videoscope.rescue.qualification import validate_path_free_canonical_json

        ranges = _qualification_ranges(self.source_ranges)
        object.__setattr__(self, "source_ranges", ranges)
        validate_path_free_canonical_json(
            self.draft_parameters,
            field_name="stabilization qualification draft parameters",
        )
        authoritative_pts = _authoritative_stabilization_actual_pts(
            self.draft_parameters, ranges
        )
        authoritative_digest = stabilization_actual_pts_digest(authoritative_pts)
        if (
            self.parent_actual_pts != authoritative_pts
            or self.parent_normalized_pts_digest != authoritative_digest
            or self.parent_frame_count != len(authoritative_pts)
            or self.authoritative_actual_pts != authoritative_pts
            or self.authoritative_actual_pts_digest != authoritative_digest
            or self.authoritative_frame_count != len(authoritative_pts)
            or self.parent_encode_contract != self.encode_contract
            or self.parent_stream_topology_digest
            != self.authoritative_parent_stream_topology_digest
        ):
            raise ValueError(
                "stabilization qualification authoritative PTS binding differs"
            )
        configured_order = tuple(item.profile_id for item in self.configured_profiles)
        measurement_order = tuple(
            item.profile.profile_id for item in self.profile_measurements
        )
        if (
            not configured_order
            or len(configured_order) != len(set(configured_order))
            or self.actual_profile_order != configured_order
            or measurement_order != configured_order
        ):
            raise ValueError("stabilization qualification profile order differs")
        if any(
            item.parent_sha256 != self.parent_sha256
            or item.source_ranges != ranges
            or item.encode_contract != self.parent_encode_contract
            or item.actual_pts != authoritative_pts
            or item.parent_normalized_pts_digest != authoritative_digest
            or item.control_normalized_pts_digest != authoritative_digest
            or item.candidate_normalized_pts_digest != authoritative_digest
            or item.parent_frame_count != len(authoritative_pts)
            or item.control_frame_count != len(authoritative_pts)
            or item.candidate_frame_count != len(authoritative_pts)
            or item.parent_stream_topology_digest
            != self.authoritative_parent_stream_topology_digest
            or item.control_stream_topology_digest
            != self.authoritative_parent_stream_topology_digest
            or item.candidate_stream_topology_digest
            != self.authoritative_parent_stream_topology_digest
            for item in self.profile_measurements
        ):
            raise ValueError("stabilization qualification parent binding differs")
        selected = next(
            (
                item.profile.profile_id
                for item in self.profile_measurements
                if item.passed
            ),
            None,
        )
        if self.selected_profile_id != selected:
            raise ValueError("stabilization qualification selection is not first-pass")
        return self

    @property
    def selected(self) -> StabilizationProfileMeasurementV1 | None:
        return next(
            (
                item
                for item in self.profile_measurements
                if item.profile.profile_id == self.selected_profile_id
            ),
            None,
        )


def build_stabilization_qualification_evidence(
    draft_plan: RescuePlan,
    measurements: Sequence[StabilizationProfileMeasurementV1],
    *,
    parent: StabilizationImmediateParentHandle | None = None,
) -> StabilizationQualificationEvidenceV1:
    """Bind ordered profile measurements to one exact transition-STABILIZE draft."""
    actions = tuple(
        action
        for action in draft_plan.actions
        if action.kind is RescueActionKind.STABILIZE
        and action.parameters.get("method") == "transition_anchor_v1"
    )
    if len(actions) != 1:
        raise ValueError("stabilization qualification action inventory is ambiguous")
    action = actions[0]
    ordered = tuple(measurements)
    profiles = draft_plan.effective_config.stabilization_qualification_profiles
    if tuple(item.profile for item in ordered) != profiles:
        raise ValueError("stabilization qualification measurements are out of order")
    authoritative_pts = _authoritative_stabilization_actual_pts(
        action.parameters, action.source_ranges
    )
    selected = next((item for item in ordered if item.passed), None)
    preceding_action_ids = tuple(
        item.id for item in draft_plan.actions[: draft_plan.actions.index(action)]
    )
    parent_sha256 = ordered[0].parent_sha256
    parent_topology = ordered[0].parent_stream_topology_digest
    if parent is not None:
        _validate_stabilization_immediate_parent(draft_plan, action, parent)
        if (
            parent.sha256 != parent_sha256
            or parent.stream_topology_digest != parent_topology
        ):
            raise ValueError("stabilization qualification parent handle differs")
        preceding_action_ids = parent.preceding_action_ids
    return StabilizationQualificationEvidenceV1(
        input_hash=draft_plan.input_hash,
        draft_plan_digest=draft_plan.plan_digest,
        draft_action_id=action.id,
        draft_parameters=action.parameters,
        source_ranges=action.source_ranges,
        encode_contract=canonical_video_encode_contract(draft_plan.effective_config),
        parent_sha256=parent_sha256,
        parent_encode_contract=canonical_video_encode_contract(
            draft_plan.effective_config
        ),
        parent_actual_pts=authoritative_pts,
        parent_normalized_pts_digest=stabilization_actual_pts_digest(authoritative_pts),
        parent_frame_count=len(authoritative_pts),
        parent_stream_topology_digest=parent_topology,
        preceding_action_ids=preceding_action_ids,
        authoritative_actual_pts=authoritative_pts,
        authoritative_actual_pts_digest=stabilization_actual_pts_digest(
            authoritative_pts
        ),
        authoritative_frame_count=len(authoritative_pts),
        authoritative_parent_stream_topology_digest=(parent_topology),
        configured_profiles=profiles,
        actual_profile_order=tuple(item.profile_id for item in profiles),
        profile_measurements=ordered,
        selected_profile_id=(
            selected.profile.profile_id if selected is not None else None
        ),
    )


def _authoritative_stabilization_actual_pts(
    parameters: Mapping[str, JsonValue],
    source_ranges: Sequence[tuple[float, float]],
) -> tuple[float, ...]:
    """Derive the one authoritative actual-PTS inventory from the draft action."""
    try:
        if (
            parameters.get("method") != "transition_anchor_v1"
            or parameters.get("estimator_algorithm_version") != "transition_anchor_v1"
        ):
            raise ValueError
        raw_transforms = parameters.get("motion_transforms")
        declared_count = parameters.get("transition_correction_count")
        if (
            not isinstance(raw_transforms, (list, tuple))
            or isinstance(declared_count, bool)
            or not isinstance(declared_count, int)
        ):
            raise ValueError
        transforms = tuple(
            MotionTransform.model_validate(item) for item in raw_transforms
        )
        actual_pts = tuple(item.timestamp_seconds for item in transforms)
        if (
            declared_count != len(transforms)
            or any(item.semantics != "frame_correction" for item in transforms)
            or any(
                current <= previous
                for previous, current in zip(actual_pts, actual_pts[1:])
            )
            or any(
                not any(start <= value < end for start, end in source_ranges)
                for value in actual_pts
            )
        ):
            raise ValueError
        return actual_pts
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "stabilization qualification authoritative PTS inventory is invalid"
        ) from exc


def _validate_stabilization_immediate_parent(
    draft_plan: RescuePlan,
    action: Any,
    parent: StabilizationImmediateParentHandle,
) -> None:
    validate_stabilization_immediate_parent_handle(parent, parent.root)
    expected_preceding = tuple(
        item.id for item in draft_plan.actions[: draft_plan.actions.index(action)]
    )
    authoritative_pts = _authoritative_stabilization_actual_pts(
        action.parameters, action.source_ranges
    )
    expected_contract = canonical_video_encode_contract(draft_plan.effective_config)
    if (
        parent.draft_plan_digest != draft_plan.plan_digest
        or parent.stabilization_action_id != action.id
        or parent.preceding_action_ids != expected_preceding
        or parent.encode_contract != expected_contract
        or parent.actual_pts != authoritative_pts
        or parent.normalized_pts_digest
        != stabilization_actual_pts_digest(authoritative_pts)
        or parent.frame_count != len(authoritative_pts)
    ):
        raise RescueMediaError(
            "stabilization immediate parent differs from the draft action chain"
        )


def stabilization_qualification_action_parameters(
    evidence: StabilizationQualificationEvidenceV1,
) -> dict[str, JsonValue]:
    """Return the canonical action-only qualification binding."""
    selected = evidence.selected
    if selected is None:
        raise ValueError("failed stabilization qualification has no action parameters")
    return {
        "stabilization_qualification": evidence.model_dump(mode="json"),
        "stabilization_qualification_version": STABILIZATION_QUALIFICATION_VERSION,
        "stabilization_qualification_profile_id": selected.profile.profile_id,
    }


def validate_plan_stabilization_qualification_contracts(plan: RescuePlan) -> None:
    """Rederive an optional selected profile before every media/output boundary."""
    actions = tuple(
        action for action in plan.actions if action.kind is RescueActionKind.STABILIZE
    )
    qualified = tuple(
        action
        for action in actions
        if action.parameters.get("stabilization_qualification") is not None
    )
    if not qualified:
        return
    if (
        len(actions) != 1
        or len(qualified) != 1
        or qualified[0].parameters.get("method") != "transition_anchor_v1"
    ):
        raise ValueError("stabilization qualification action inventory is ambiguous")
    action = qualified[0]
    try:
        evidence = StabilizationQualificationEvidenceV1.model_validate(
            action.parameters["stabilization_qualification"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stabilization qualification wire is invalid") from exc
    selected = evidence.selected
    if selected is None:
        raise ValueError("stabilization qualification has no selected profile")
    if (
        evidence.input_hash != plan.input_hash
        or evidence.source_ranges != action.source_ranges
        or evidence.encode_contract
        != canonical_video_encode_contract(plan.effective_config)
        or evidence.configured_profiles
        != plan.effective_config.stabilization_qualification_profiles
        or evidence.actual_profile_order
        != tuple(
            item.profile_id
            for item in plan.effective_config.stabilization_qualification_profiles
        )
    ):
        raise ValueError("stabilization qualification binding differs from the plan")
    expected_preceding = tuple(
        item.id for item in plan.actions[: plan.actions.index(action)]
    )
    if evidence.preceding_action_ids != expected_preceding:
        raise ValueError("stabilization qualification parent chain differs")
    expected_parameters = dict(selected.action_parameters)
    expected_parameters.update(stabilization_qualification_action_parameters(evidence))
    if action.parameters != expected_parameters:
        raise ValueError("stabilization action parameters differ from qualification")
    expected_action_id = make_rescue_action_id(
        kind=action.kind,
        parameters=expected_parameters,
        source_ranges=action.source_ranges,
        strategy=action.strategy,
        version=action.version,
    )
    if action.id != expected_action_id:
        raise ValueError("stabilization action ID differs from qualification")
    draft_action_id = make_rescue_action_id(
        kind=action.kind,
        parameters=evidence.draft_parameters,
        source_ranges=action.source_ranges,
        strategy=action.strategy,
        version=action.version,
    )
    if evidence.draft_action_id != draft_action_id:
        raise ValueError("stabilization qualification draft action ID is invalid")
    draft_action = action.model_copy(
        update={"id": draft_action_id, "parameters": evidence.draft_parameters}
    )
    draft_payload = plan.model_dump(mode="python", exclude={"plan_digest"})
    draft_payload["actions"] = [
        (
            draft_action.model_dump(mode="python")
            if item.id == action.id
            else item.model_dump(mode="python")
        )
        for item in plan.actions
    ]
    if make_rescue_plan_digest(draft_payload) != evidence.draft_plan_digest:
        raise ValueError("stabilization qualification draft plan digest is stale")


class UnavailableStabilizationImmediateParentProvider:
    """Default provider: do not claim the raw input is the immediate parent."""

    def provide(
        self,
        draft_plan: RescuePlan,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> StabilizationImmediateParentHandle:
        del draft_plan, source, work_root
        if cancellation_callback():
            raise RescueCancelledError("stabilization qualification was cancelled")
        raise RescueQualificationUnavailableError(
            "stabilization immediate-parent generation is unavailable"
        )


class UnavailableStabilizationCandidateQualifier:
    """Default optional provider: preserve the existing STABILIZE action."""

    def qualify(
        self,
        draft_plan: RescuePlan,
        parent: StabilizationImmediateParentHandle,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> StabilizationQualificationEvidenceV1:
        del draft_plan, parent, work_root
        if cancellation_callback():
            raise RescueCancelledError("stabilization qualification was cancelled")
        raise RescueQualificationUnavailableError(
            "stabilization candidate qualification is unavailable"
        )


class CallbackStabilizationCandidateQualifier:
    """Strict local fake/provider seam for bounded private profile generations."""

    def __init__(
        self, *, renderer: Callable[..., Path], measurement_provider: Callable[..., Any]
    ) -> None:
        self._renderer = renderer
        self._measurement_provider = measurement_provider

    def qualify(
        self,
        draft_plan: RescuePlan,
        parent: StabilizationImmediateParentHandle,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> StabilizationQualificationEvidenceV1:
        if cancellation_callback():
            raise RescueCancelledError("stabilization qualification was cancelled")
        if not isinstance(parent, StabilizationImmediateParentHandle):
            raise RescueMediaError(
                "stabilization qualification requires an immediate parent"
            )
        source = parent.path
        work_root = Path(work_root)
        if work_root.exists():
            raise RescueArtifactError("stabilization qualification root already exists")
        try:
            work_root.mkdir(parents=True, exist_ok=False)
            resolved_root = work_root.resolve(strict=True)
        except OSError as exc:
            raise RescueArtifactError(
                "stabilization qualification private root is unavailable"
            ) from exc
        artifacts: list[Path] = []
        cleanup_error: RescueArtifactError | None = None
        try:
            actions = tuple(
                action
                for action in draft_plan.actions
                if action.kind is RescueActionKind.STABILIZE
                and action.parameters.get("method") == "transition_anchor_v1"
            )
            if len(actions) != 1:
                raise RescueMediaError(
                    "stabilization qualification action inventory is ambiguous"
                )
            action = actions[0]
            _validate_stabilization_immediate_parent(draft_plan, action, parent)
            try:
                source_sha256 = _sha256_regular_file(source)
            except OSError as exc:
                raise RescueMediaError(
                    "stabilization qualification parent is unavailable"
                ) from exc
            if source_sha256 != parent.sha256:
                raise RescueMediaError(
                    "stabilization qualification parent bytes differ from the plan"
                )
            measurements: list[StabilizationProfileMeasurementV1] = []
            for index, profile in enumerate(
                draft_plan.effective_config.stabilization_qualification_profiles
            ):
                if cancellation_callback():
                    raise RescueCancelledError(
                        "stabilization qualification was cancelled"
                    )
                control = work_root / f"control-{index:02d}.private"
                candidate = work_root / f"candidate-{index:02d}.private"
                artifacts.extend((control, candidate))
                returned_control = Path(
                    self._renderer(source, control, action, profile, True)
                )
                _require_private_generation(returned_control, control, resolved_root)
                if cancellation_callback():
                    raise RescueCancelledError(
                        "stabilization qualification was cancelled"
                    )
                returned_candidate = Path(
                    self._renderer(source, candidate, action, profile, False)
                )
                _require_private_generation(
                    returned_candidate, candidate, resolved_root
                )
                if cancellation_callback():
                    raise RescueCancelledError(
                        "stabilization qualification was cancelled"
                    )
                measured = self._measurement_provider(
                    source,
                    control,
                    candidate,
                    draft_plan,
                    action,
                    profile,
                    index,
                )
                if not isinstance(measured, StabilizationProfileMeasurementV1):
                    raise RescueMediaError(
                        "stabilization qualification measurement is invalid"
                    )
                if (
                    measured.profile != profile
                    or measured.parent_sha256 != source_sha256
                    or measured.control_sha256 != _sha256_regular_file(control)
                    or measured.candidate_sha256 != _sha256_regular_file(candidate)
                ):
                    raise RescueMediaError(
                        "stabilization qualification artifact identity differs"
                    )
                measurements.append(measured)
            return build_stabilization_qualification_evidence(
                draft_plan, tuple(measurements), parent=parent
            )
        finally:
            for artifact in tuple(dict.fromkeys(artifacts)):
                try:
                    if artifact.is_symlink():
                        raise OSError
                    artifact.resolve(strict=False).relative_to(resolved_root)
                    artifact.unlink(missing_ok=True)
                except (OSError, ValueError):
                    cleanup_error = RescueArtifactError(
                        "stabilization qualification private cleanup failed"
                    )
            try:
                work_root.rmdir()
            except OSError:
                cleanup_error = RescueArtifactError(
                    "stabilization qualification private cleanup failed"
                )
            if cleanup_error is not None:
                raise cleanup_error


def _require_private_generation(
    returned: Path, expected: Path, resolved_root: Path
) -> None:
    try:
        if returned != expected or returned.is_symlink():
            raise ValueError
        resolved = returned.resolve(strict=True)
        resolved.relative_to(resolved_root)
        if not returned.is_file():
            raise ValueError
    except (OSError, ValueError):
        raise RescueArtifactError(
            "stabilization qualification output escaped the private root"
        ) from None


def _sha256_regular_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise OSError("not a regular file")
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


FeatureEstimator = Callable[
    [np.ndarray, np.ndarray], tuple[float, float, float, float, float, float] | None
]
AnchorFeatureEstimator = FeatureEstimator


def select_stable_anchor(
    frames: Sequence[tuple[float, NDArray[np.uint8]]],
    config: StabilizationConfig,
    *,
    scene_boundaries: Sequence[float] = (),
    estimator: AnchorFeatureEstimator | None = None,
) -> int | None:
    """Choose one deterministic, measurable background anchor for one scene."""
    ordered = _validate_anchor_frames(frames, config)
    if len(ordered) < 2 or scene_boundaries:
        # A caller with multiple scenes must split them first; choosing one anchor
        # across a cut would make the cut itself look like camera motion.
        return None
    coverages = tuple(
        _background_feature_coverage(frame, config) for _, frame in ordered
    )
    candidates = tuple(
        index
        for index, coverage in enumerate(coverages)
        if coverage >= config.minimum_background_coverage
    )
    if not candidates:
        return None
    estimate = estimator or _opencv_affine_estimator(config)
    peer_indexes = _representative_indexes(len(ordered), maximum=7)
    scored: list[tuple[tuple[float, ...], int]] = []
    for candidate in candidates:
        measurements = []
        anchor = _grayscale(ordered[candidate][1])
        for peer in peer_indexes:
            if peer == candidate:
                continue
            measured = estimate(anchor, _grayscale(ordered[peer][1]))
            if measured is not None:
                measurements.append(measured)
        reliable = tuple(
            item
            for item in measurements
            if item[4] >= config.minimum_anchor_inlier_ratio
            and item[5] <= config.maximum_anchor_residual_pixels
            and abs(item[0]) <= config.maximum_rotation_degrees
            and abs(item[1] - 1.0) <= config.maximum_scale_excursion
        )
        required = max(1, len(peer_indexes) - 2)
        if len(reliable) < required:
            continue
        boundary_distance = float(min(candidate, len(ordered) - 1 - candidate))
        score = (
            float(len(reliable)),
            float(np.median([item[4] for item in reliable])),
            -float(np.median([item[5] for item in reliable])),
            coverages[candidate],
            boundary_distance,
            -float(candidate),
        )
        scored.append((score, candidate))
    return max(scored)[1] if scored else None


def estimate_anchor_corrections(
    frames: Sequence[tuple[float, NDArray[np.uint8]]],
    config: StabilizationConfig,
    *,
    scene_boundaries: Sequence[float] = (),
    estimator: AnchorFeatureEstimator | None = None,
) -> tuple[MotionTransform, ...]:
    """Measure every accepted source frame directly against a scene anchor."""
    ordered = _validate_anchor_frames(frames, config)
    if len(ordered) < 2:
        return ()
    boundaries = _validate_scene_boundaries(scene_boundaries, ordered)
    segments = _scene_segments(ordered, boundaries)
    estimate = estimator or _opencv_affine_estimator(config)
    corrections: list[MotionTransform] = []
    for start, end in segments:
        scene = ordered[start:end]
        anchor_local = select_stable_anchor(scene, config, estimator=estimate)
        if anchor_local is None:
            return ()
        anchor = _grayscale(scene[anchor_local][1])
        scene_corrections: list[MotionTransform] = []
        low_confidence_run = 0
        raw_translations: list[tuple[float, float]] = []
        for index, (timestamp, frame) in enumerate(scene):
            measured: tuple[float, float, float, float, float, float] | None
            if index == anchor_local:
                measured = (0.0, 1.0, 0.0, 0.0, 1.0, 0.0)
            else:
                measured = estimate(anchor, _grayscale(frame))
            phase = (
                None
                if measured is None
                else _phase_translation_measurement(anchor, _grayscale(frame), config)
            )
            accepted = (
                measured
                if measured is not None
                and _anchor_measurement_is_reliable(measured, config)
                else _phase_dominant_translation(measured, phase, config)
            )
            if accepted is None:
                low_confidence_run += 1
                if (
                    low_confidence_run
                    > config.maximum_consecutive_low_confidence_frames
                ):
                    return ()
                scene_corrections.append(
                    _neutral_correction(timestamp).model_copy(
                        update={"inlier_ratio": 0.0, "residual_pixels": 4096.0}
                    )
                )
                continue
            low_confidence_run = 0
            rotation, scale, tx, ty, inliers, residual = accepted
            raw_translations.append((tx, ty))
            inverse = np.linalg.inv(
                _matrix_from_components(
                    rotation_degrees=rotation,
                    scale=scale,
                    translation_x=tx,
                    translation_y=ty,
                )
            )
            corr_rotation, corr_scale, corr_x, corr_y = _decompose_motion(inverse)
            scene_corrections.append(
                MotionTransform(
                    timestamp_seconds=timestamp,
                    rotation_degrees=corr_rotation,
                    scale=corr_scale,
                    translation_x=corr_x,
                    translation_y=corr_y,
                    inlier_ratio=inliers,
                    residual_pixels=residual,
                    semantics="frame_correction",
                )
            )
        if _is_intentional_unidirectional_motion(raw_translations, config):
            return ()
        if start > 0 and scene_corrections:
            scene_corrections[0] = _neutral_correction(
                scene_corrections[0].timestamp_seconds, scene_boundary=True
            )
        corrections.extend(scene_corrections)
    return tuple(corrections)


def estimate_transition_anchor_corrections(
    frames: Sequence[tuple[float, NDArray[np.uint8]]],
    config: StabilizationConfig,
    *,
    transition_range: tuple[float, float],
    following_anchor_corrections: Sequence[MotionTransform],
) -> tuple[MotionTransform, ...]:
    """Bind a measured transition path backwards to an accepted anchor run.

    The transition is never measured against one appearance anchor.  Instead,
    every adjacent source-PTS pair must independently pass regional phase,
    forward/backward LK, and dense-flow translation gates.  Corrections are
    then accumulated backwards from the first already accepted anchor
    correction, so the boundary is exact rather than interpolated.
    """
    ordered = _validate_anchor_frames(frames, config)
    start, end = (float(transition_range[0]), float(transition_range[1]))
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("transition range must be finite and non-empty")
    transition_indexes = tuple(
        index
        for index, (timestamp, _frame) in enumerate(ordered)
        if start <= timestamp < end
    )
    if not transition_indexes:
        return ()
    if transition_indexes != tuple(
        range(transition_indexes[0], transition_indexes[-1] + 1)
    ):
        raise ValueError("transition frames must form one contiguous PTS range")
    if len(transition_indexes) + 1 > config.maximum_transition_candidate_frames:
        raise ValueError("transition candidate frame inventory exceeds its maximum")

    following = tuple(following_anchor_corrections)
    if len(following) < 2 or any(
        item.semantics != "frame_correction" for item in following
    ):
        return ()
    if any(
        item.inlier_ratio < config.minimum_anchor_inlier_ratio
        or item.residual_pixels > config.maximum_anchor_residual_pixels
        or abs(item.rotation_degrees) > config.maximum_rotation_degrees
        or abs(item.scale - 1.0) > config.maximum_scale_excursion
        for item in following
        if not item.scene_boundary
    ):
        return ()
    if any(
        following[index].timestamp_seconds <= following[index - 1].timestamp_seconds
        for index in range(1, len(following))
    ):
        raise ValueError("following anchor corrections must be strictly ordered")
    frame_by_timestamp = {
        timestamp: index for index, (timestamp, _frame) in enumerate(ordered)
    }
    if len(frame_by_timestamp) != len(ordered):
        raise ValueError("motion frame timestamps must be unique")
    try:
        following_indexes = tuple(
            frame_by_timestamp[item.timestamp_seconds] for item in following
        )
    except KeyError:
        return ()
    first_following = following_indexes[0]
    if first_following != transition_indexes[-1] + 1:
        return ()
    if following_indexes != tuple(
        range(first_following, first_following + len(following))
    ):
        return ()
    if first_following + len(following) != len(ordered):
        return ()
    following_phase = _phase_translation_measurement(
        _grayscale(ordered[following_indexes[0]][1]),
        _grayscale(ordered[following_indexes[1]][1]),
        config,
    )
    if following_phase is None:
        return ()
    following_seam = math.hypot(
        (following[0].translation_x - following[1].translation_x) - following_phase[0],
        (following[0].translation_y - following[1].translation_y) - following_phase[1],
    )
    if following_seam > config.maximum_transition_seam_discontinuity_pixels:
        return ()

    timestamps = np.asarray([item[0] for item in ordered], dtype=np.float64)
    steps = np.diff(timestamps)
    nominal_step = float(np.median(steps))
    if any(
        step > min(config.maximum_timeline_gap_seconds, nominal_step * 1.5)
        for step in steps[transition_indexes[0] : first_following]
    ):
        return ()

    candidate_frames = tuple(
        _grayscale(ordered[index][1])
        for index in range(transition_indexes[0], first_following + 1)
    )
    persistent_lk = _persistent_transition_lk_measurements(candidate_frames, config)
    if persistent_lk is None:
        return ()
    adjacent: list[tuple[float, float, float, float]] = []
    for index in range(transition_indexes[0], first_following):
        local_index = index - transition_indexes[0]
        measured = _transition_translation_measurement(
            _grayscale(ordered[index][1]),
            _grayscale(ordered[index + 1][1]),
            config,
            persistent_lk=persistent_lk[local_index],
        )
        if measured is None:
            return ()
        adjacent.append(measured)

    observed_path: list[tuple[float, float]] = [(0.0, 0.0)]
    for tx, ty, _confidence, _residual in adjacent:
        prior_x, prior_y = observed_path[-1]
        observed_path.append((prior_x + tx, prior_y + ty))
    if _is_intentional_unidirectional_motion(observed_path, config):
        return ()

    corrections: list[MotionTransform | None] = [None] * len(transition_indexes)
    current_x = following[0].translation_x
    current_y = following[0].translation_y
    for local_index in range(len(adjacent) - 1, -1, -1):
        tx, ty, confidence, residual = adjacent[local_index]
        current_x += tx
        current_y += ty
        timestamp = ordered[transition_indexes[local_index]][0]
        corrections[local_index] = MotionTransform(
            timestamp_seconds=timestamp,
            rotation_degrees=following[0].rotation_degrees,
            scale=following[0].scale,
            translation_x=current_x,
            translation_y=current_y,
            inlier_ratio=max(config.minimum_anchor_inlier_ratio, confidence),
            residual_pixels=min(config.maximum_anchor_residual_pixels, residual),
            semantics="frame_correction",
        )
    measured_transition = tuple(item for item in corrections if item is not None)
    union = measured_transition + following
    if len(union) != len(ordered) - transition_indexes[0]:
        return ()
    if _required_crop_ratio(union, config) > config.max_crop_ratio:
        return ()
    seam = math.hypot(
        (measured_transition[-1].translation_x - following[0].translation_x)
        - adjacent[-1][0],
        (measured_transition[-1].translation_y - following[0].translation_y)
        - adjacent[-1][1],
    )
    if seam > config.maximum_transition_seam_discontinuity_pixels:
        return ()
    return union


def _transition_consensus_not_cancelled() -> bool:
    return False


def _raise_if_transition_consensus_cancelled(
    cancellation_callback: Callable[[], bool],
) -> None:
    if cancellation_callback():
        raise RescueCancelledError(
            "transition source consensus measurement was cancelled"
        )


def measure_transition_source_consensus(
    frames: Sequence[tuple[float, NDArray[np.uint8]]],
    config: StabilizationConfig,
    *,
    cancellation_callback: Callable[[], bool] = _transition_consensus_not_cancelled,
) -> tuple[TransitionConsensusStep, ...]:
    """Re-measure strict regional/LK/dense consensus from actual source frames."""
    _raise_if_transition_consensus_cancelled(cancellation_callback)
    ordered = _validate_anchor_frames(frames, config)
    if len(ordered) < 2 or len(ordered) > config.maximum_transition_candidate_frames:
        return ()
    timestamps = np.asarray([item[0] for item in ordered], dtype=np.float64)
    steps = np.diff(timestamps)
    nominal_step = float(np.median(steps))
    if any(
        step > min(config.maximum_timeline_gap_seconds, nominal_step * 1.5)
        for step in steps
    ):
        return ()
    candidate_frames = tuple(_grayscale(frame) for _timestamp, frame in ordered)
    _raise_if_transition_consensus_cancelled(cancellation_callback)
    persistent_lk = _persistent_transition_lk_measurements(
        candidate_frames,
        config,
        cancellation_callback=cancellation_callback,
    )
    _raise_if_transition_consensus_cancelled(cancellation_callback)
    if persistent_lk is None or len(persistent_lk) != len(ordered) - 1:
        return ()
    evidence: list[TransitionConsensusStep] = []
    for index, ((previous_timestamp, previous), (timestamp, current)) in enumerate(
        zip(ordered, ordered[1:], strict=False)
    ):
        _raise_if_transition_consensus_cancelled(cancellation_callback)
        measured = _transition_translation_measurement(
            _grayscale(previous),
            _grayscale(current),
            config,
            persistent_lk=persistent_lk[index],
            cancellation_callback=cancellation_callback,
        )
        _raise_if_transition_consensus_cancelled(cancellation_callback)
        if measured is None:
            return ()
        tx, ty, _confidence, residual = measured
        evidence.append(
            TransitionConsensusStep(
                previous_timestamp_seconds=previous_timestamp,
                current_timestamp_seconds=timestamp,
                translation_x=tx,
                translation_y=ty,
                residual_pixels=residual,
            )
        )
    return tuple(evidence)


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


def _exact_motion_corrections_for_timestamps(
    corrections: Sequence[MotionTransform],
    timestamps: Sequence[float],
    *,
    tolerance_seconds: float,
) -> tuple[MotionTransform, ...]:
    """Bind one already-reviewed correction to each source PTS without inference."""
    ordered = tuple(corrections)
    exact = tuple(float(timestamp) for timestamp in timestamps)
    if len(ordered) != len(exact):
        raise RescueMediaError(
            "stabilization corrections must cover every source timestamp exactly"
        )
    if any(item.semantics != "frame_correction" for item in ordered):
        raise RescueMediaError(
            "stabilization timeline does not contain frame corrections"
        )
    for correction, timestamp in zip(ordered, exact, strict=True):
        if not math.isclose(
            correction.timestamp_seconds,
            timestamp,
            rel_tol=0.0,
            abs_tol=tolerance_seconds,
        ):
            raise RescueMediaError(
                "stabilization corrections must cover every source timestamp exactly"
            )
    return ordered


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
    reliable = tuple(
        item
        for item in within_scenes
        if item.inlier_ratio >= config.minimum_inlier_ratio
        and item.residual_pixels <= config.maximum_residual_pixels
    )
    if not reliable:
        return StabilizationAssessment(
            recommended=False,
            reason=(
                "low_inlier_ratio"
                if any(
                    item.inlier_ratio < config.minimum_inlier_ratio
                    for item in within_scenes
                )
                else "high_residual"
            ),
            crop_ratio=0.0,
            transforms=measured,
        )
    bridged_indexes = _bridged_low_confidence_indexes(measured, config)
    runs: list[list[MotionTransform]] = []
    current: list[MotionTransform] = []
    previous: MotionTransform | None = None
    for index, item in enumerate(measured):
        valid = index in bridged_indexes or (
            not item.scene_boundary
            and item.inlier_ratio >= config.minimum_inlier_ratio
            and item.residual_pixels <= config.maximum_residual_pixels
        )
        contiguous = (
            previous is not None
            and item.timestamp_seconds - previous.timestamp_seconds
            <= config.maximum_timeline_gap_seconds
        )
        if valid and (not current or contiguous):
            current.append(item)
        else:
            if current:
                runs.append(current)
            current = [item] if valid else []
        previous = item
    if current:
        runs.append(current)
    step = max(_median_step(measured), config.range_padding_seconds)
    accepted_runs: list[
        tuple[
            list[MotionTransform],
            tuple[MotionTransform, ...],
            int,
            tuple[float, float],
        ]
    ] = []
    run_assessments: list[JsonValue] = []
    over_budget_crop_ratios: list[float] = []
    inactive_run_count = 0
    for run in runs:
        run_corrections = _stabilize_run(
            run, window_size=config.smoothing_window_samples | 1
        )
        motion_amplitude = max(
            _motion_amplitude(run), _motion_amplitude(run_corrections)
        )
        active_count = sum(
            _correction_amplitude(item) >= config.minimum_motion_amplitude_pixels
            for item in run_corrections
        )
        crop_ratio = _required_crop_ratio(run, config)
        safe_start, safe_end, start_limited, end_limited = _bounded_motion_run_range(
            run, measured, config, padding_seconds=step
        )
        if motion_amplitude < config.minimum_motion_amplitude_pixels:
            reason = "insufficient_motion_amplitude"
        elif (
            active_count < config.minimum_active_correction_count
            or safe_end <= safe_start
        ):
            inactive_run_count += 1
            reason = "insufficient_active_corrections"
        elif crop_ratio > config.max_crop_ratio:
            over_budget_crop_ratios.append(crop_ratio)
            reason = "crop_budget_exceeded"
        else:
            reason = "accepted"
            accepted_runs.append(
                (run, run_corrections, active_count, (safe_start, safe_end))
            )
        run_assessments.append(
            cast(
                JsonValue,
                {
                    "accepted": reason == "accepted",
                    "active_correction_count": active_count,
                    "crop_ratio": crop_ratio,
                    "end_boundary_limited": end_limited,
                    "end_seconds": safe_end,
                    "motion_amplitude_pixels": motion_amplitude,
                    "reason": reason,
                    "start_boundary_limited": start_limited,
                    "start_seconds": safe_start,
                    "transform_count": len(run),
                },
            )
        )
    if not accepted_runs:
        if over_budget_crop_ratios:
            return StabilizationAssessment(
                recommended=False,
                reason="crop_budget_exceeded",
                crop_ratio=max(over_budget_crop_ratios),
                transforms=measured,
            )
        if inactive_run_count:
            return StabilizationAssessment(
                recommended=False,
                reason="insufficient_active_corrections",
                crop_ratio=0.0,
                transforms=measured,
            )
        return StabilizationAssessment(
            recommended=False,
            reason="insufficient_motion_amplitude",
            crop_ratio=0.0,
            transforms=measured,
        )
    crop_ratio = max(
        _required_crop_ratio(run, config) for run, _, _, _ in accepted_runs
    )
    corrections = {
        item.timestamp_seconds: item
        for _run, run_corrections, _active_count, (start, end) in accepted_runs
        for item in run_corrections
        if start <= item.timestamp_seconds < end
    }
    smoothed = tuple(
        corrections.get(
            item.timestamp_seconds,
            MotionTransform(
                timestamp_seconds=item.timestamp_seconds,
                rotation_degrees=0.0,
                scale=1.0,
                translation_x=0.0,
                translation_y=0.0,
                inlier_ratio=item.inlier_ratio,
                residual_pixels=item.residual_pixels,
                scene_boundary=item.scene_boundary,
                semantics="frame_correction",
            ),
        )
        for item in measured
    )
    affected_ranges = [
        [start, end]
        for _run, _run_corrections, _active_count, (start, end) in accepted_runs
    ]
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
            "affected_ranges": cast(JsonValue, affected_ranges),
            "minimum_motion_amplitude_pixels": config.minimum_motion_amplitude_pixels,
            "minimum_active_correction_count": config.minimum_active_correction_count,
            "bridged_low_confidence_samples": len(bridged_indexes),
            "run_assessments": run_assessments,
        },
    )


def _bridged_low_confidence_indexes(
    measured: Sequence[MotionTransform], config: StabilizationConfig
) -> frozenset[int]:
    """Return bounded low-texture holes surrounded by reliable same-scene motion."""
    maximum = config.maximum_bridged_low_confidence_samples
    if maximum == 0 or len(measured) < 3:
        return frozenset()

    def reliable(item: MotionTransform) -> bool:
        return (
            not item.scene_boundary
            and item.inlier_ratio >= config.minimum_inlier_ratio
            and item.residual_pixels <= config.maximum_residual_pixels
        )

    bridged: set[int] = set()
    index = 1
    while index < len(measured) - 1:
        if reliable(measured[index]):
            index += 1
            continue
        start = index
        while index < len(measured) - 1 and not reliable(measured[index]):
            index += 1
        end = index
        span = measured[start:end]
        if not (1 <= len(span) <= maximum):
            continue
        if any(
            item.scene_boundary or item.residual_pixels > config.maximum_residual_pixels
            for item in span
        ):
            continue
        neighborhood = measured[start - 1 : end + 1]
        if not reliable(neighborhood[0]) or not reliable(neighborhood[-1]):
            continue
        if any(
            right.timestamp_seconds - left.timestamp_seconds
            > config.maximum_timeline_gap_seconds
            for left, right in zip(neighborhood, neighborhood[1:])
        ):
            continue
        bridged.update(range(start, end))
    return frozenset(bridged)


def _median_step(transforms: Sequence[MotionTransform]) -> float:
    steps = [
        right.timestamp_seconds - left.timestamp_seconds
        for left, right in zip(transforms, transforms[1:])
        if right.timestamp_seconds > left.timestamp_seconds
    ]
    return float(np.median(steps)) if steps else 0.0


def _bounded_motion_run_range(
    run: Sequence[MotionTransform],
    measured: Sequence[MotionTransform],
    config: StabilizationConfig,
    *,
    padding_seconds: float,
) -> tuple[float, float, bool, bool]:
    """Keep configured padding unless an observed unsafe neighbor bounds the run."""
    first_index = next(
        index
        for index, item in enumerate(measured)
        if item.timestamp_seconds == run[0].timestamp_seconds
    )
    last_index = next(
        index
        for index in range(first_index, len(measured))
        if measured[index].timestamp_seconds == run[-1].timestamp_seconds
    )

    def unsafe(item: MotionTransform) -> bool:
        return (
            item.scene_boundary
            or item.inlier_ratio < config.minimum_inlier_ratio
            or item.residual_pixels > config.maximum_residual_pixels
        )

    start = max(0.0, run[0].timestamp_seconds - padding_seconds)
    end = run[-1].timestamp_seconds + padding_seconds
    start_limited = first_index > 0 and unsafe(measured[first_index - 1])
    end_limited = last_index + 1 < len(measured) and unsafe(measured[last_index + 1])
    if start_limited:
        # A preceding unsafe observation may bound the run, but it cannot be
        # claimed as corrected.  Start at the first reliable measurement.
        start = max(start, run[0].timestamp_seconds)
    if end_limited:
        # The next unsafe measurement only proves that the last reliable sample
        # preceded a transition.  Half-open output must stop at that reliable
        # timestamp rather than padding into unmeasured transition frames.
        end = min(end, run[-1].timestamp_seconds)
    return start, end, start_limited, end_limited


def _stabilize_run(
    transforms: Sequence[MotionTransform], *, window_size: int
) -> tuple[MotionTransform, ...]:
    """Build corrections against a robust low-frequency camera path.

    The historical local-median smoother intentionally preserves alternating
    motion.  For a run explicitly classified as shake, the target path must be
    a stable run-level median; otherwise a periodic +/- motion can be mistaken
    for the desired path and produce an all-neutral correction.
    """
    ordered = tuple(transforms)
    if not ordered:
        return ()
    cumulative: list[NDArray[np.float64]] = []
    path: NDArray[np.float64] = np.eye(3, dtype=np.float64)
    for item in ordered:
        path = _motion_matrix(item) @ path
        cumulative.append(path.copy())
    components = tuple(_decompose_motion(matrix) for matrix in cumulative)
    target = _matrix_from_components(
        rotation_degrees=float(np.median([value[0] for value in components])),
        scale=float(np.median([value[1] for value in components])),
        translation_x=float(np.median([value[2] for value in components])),
        translation_y=float(np.median([value[3] for value in components])),
    )
    return tuple(
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
        for item, cumulative_path in zip(ordered, cumulative, strict=True)
        for rotation, scale, translation_x, translation_y in (
            (_decompose_motion(target @ np.linalg.inv(cumulative_path))),
        )
    )


def _motion_amplitude(transforms: Sequence[MotionTransform]) -> float:
    if not transforms:
        return 0.0
    x = [item.translation_x for item in transforms]
    y = [item.translation_y for item in transforms]
    return max(
        max(x) - min(x),
        max(y) - min(y),
        max((abs(value) for value in x), default=0.0),
        max((abs(value) for value in y), default=0.0),
    )


def _correction_amplitude(transform: MotionTransform) -> float:
    return max(
        abs(transform.translation_x),
        abs(transform.translation_y),
        abs(transform.rotation_degrees) * 2.0,
        abs(transform.scale - 1.0) * 100.0,
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
    encode_config: RescueEffectiveConfig | None = None,
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
    active_corrections = tuple(
        item
        for item in corrections
        if _correction_amplitude(item) >= config.minimum_motion_amplitude_pixels
    )
    safe_crop_ratio = (
        _required_crop_ratio(active_corrections, config) if active_corrections else 0.0
    )
    active_step = _median_step(corrections)
    active_ranges = config.accepted_ranges or (
        (
            corrections[0].timestamp_seconds,
            (
                corrections[-1].timestamp_seconds + active_step
                if active_step > 0
                else math.nextafter(corrections[-1].timestamp_seconds, math.inf)
            ),
        ),
    )
    corrections_in_ranges = tuple(
        item
        for item in corrections
        if any(start <= item.timestamp_seconds < end for start, end in active_ranges)
    )
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - package dependency boundary
        raise RescueMediaError("OpenCV is required for CPU stabilization") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="videoscope-stabilize-", dir=output.parent
    ) as temp_name:
        # OpenCV's common ``mp4v`` writer is lossy.  A stabilisation pass using
        # it would soften every frame, including ranges where the correction is
        # neutral.  FFV1 in AVI is a local, lossless intermediate supported by
        # the project FFmpeg/OpenCV stack and keeps the restored detail intact.
        intermediate = Path(temp_name) / "video-only-lossless.avi"
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
            provided_corrections = (
                _exact_motion_corrections_for_timestamps(
                    corrections_in_ranges,
                    tuple(
                        timestamp
                        for timestamp in provided_timestamps
                        if any(start <= timestamp < end for start, end in active_ranges)
                    ),
                    tolerance_seconds=config.exact_timestamp_tolerance_seconds,
                )
                if provided_timestamps is not None
                else None
            )
            fourcc = int(getattr(cv2, "VideoWriter_fourcc")(*"FFV1"))
            writer = cv2.VideoWriter(str(intermediate), fourcc, fps, (width, height))
            if not writer.isOpened():
                raise RescueMediaError("stabilized video writer could not be opened")
            try:
                frame_index = 0
                observed_timestamps: list[float] = []
                observed_active_timestamps: list[float] = []
                active_frame_index = 0
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
                    inside_active_range = any(
                        start <= timestamp < end for start, end in active_ranges
                    )
                    if inside_active_range:
                        observed_active_timestamps.append(timestamp)
                        if provided_corrections is not None:
                            correction = provided_corrections[active_frame_index]
                        else:
                            if active_frame_index >= len(corrections_in_ranges):
                                raise RescueMediaError(
                                    "stabilization corrections must cover every "
                                    "source timestamp exactly"
                                )
                            correction = corrections_in_ranges[active_frame_index]
                            if not math.isclose(
                                correction.timestamp_seconds,
                                timestamp,
                                rel_tol=0.0,
                                abs_tol=config.exact_timestamp_tolerance_seconds,
                            ):
                                raise RescueMediaError(
                                    "stabilization corrections must cover every "
                                    "source timestamp exactly"
                                )
                        queued_frame = cv2.warpAffine(
                            queued_frame,
                            _affine_matrix(
                                correction,
                                width,
                                height,
                                safe_crop_ratio=safe_crop_ratio,
                                translation_scale_x=width / config.frame_width,
                                translation_scale_y=height / config.frame_height,
                            ),
                            (width, height),
                            flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_REPLICATE,
                        )
                        active_frame_index += 1
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
                _exact_motion_corrections_for_timestamps(
                    corrections_in_ranges,
                    observed_active_timestamps,
                    tolerance_seconds=config.exact_timestamp_tolerance_seconds,
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
                "stabilized audio mux failed: " + result.stderr_summary
            )
        if cancellation_callback():
            raise RescueCancelledError("stabilization cancelled before publication")
        try:
            if not muxed.is_file() or muxed.stat().st_size <= 0:
                raise RescueMediaError("stabilized audio mux produced no media")
            os.link(muxed, output)
        except (RescueArtifactError, RescueMediaError):
            raise
        except FileExistsError as exc:
            raise RescueArtifactError(
                "stabilization output appeared before publication"
            ) from exc
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


def _validate_anchor_frames(
    frames: Sequence[tuple[float, NDArray[np.uint8]]],
    config: StabilizationConfig,
) -> tuple[tuple[float, NDArray[np.uint8]], ...]:
    if len(frames) > config.maximum_frame_inventory:
        raise ValueError("motion frame inventory exceeds the configured maximum")
    ordered = tuple(frames)
    if any(
        not isinstance(timestamp, (int, float))
        or isinstance(timestamp, bool)
        or not math.isfinite(float(timestamp))
        or float(timestamp) < 0
        for timestamp, _frame in ordered
    ):
        raise ValueError("frame timestamps must be finite and non-negative")
    if any(
        ordered[index][0] <= ordered[index - 1][0] for index in range(1, len(ordered))
    ):
        raise ValueError("frame timestamps must be strictly increasing")
    if len(ordered) >= 2:
        steps = np.diff(np.asarray([item[0] for item in ordered], dtype=np.float64))
        observed_rate = 1.0 / float(np.median(steps))
        if observed_rate > config.source_rate_cap_fps + 1e-6:
            raise ValueError("motion frame rate exceeds the configured source-rate cap")
    shape: tuple[int, int] | None = None
    result: list[tuple[float, NDArray[np.uint8]]] = []
    for timestamp, frame in ordered:
        gray = _grayscale(frame)
        if gray.size == 0 or not np.isfinite(gray).all():
            raise ValueError("motion frames must contain finite pixels")
        if gray.shape != (config.frame_height, config.frame_width):
            raise ValueError(
                "motion frames must match configured frame dimensions before "
                "feature or dense-flow allocation"
            )
        if shape is None:
            shape = gray.shape
        elif gray.shape != shape:
            raise ValueError("motion frames must have identical dimensions")
        result.append((float(timestamp), np.asarray(frame, dtype=np.uint8)))
    return tuple(result)


def _validate_scene_boundaries(
    boundaries: Sequence[float],
    frames: Sequence[tuple[float, NDArray[np.uint8]]],
) -> tuple[float, ...]:
    values = tuple(float(item) for item in boundaries)
    if any(
        not math.isfinite(item) or item <= frames[0][0] or item > frames[-1][0]
        for item in values
    ):
        raise ValueError("scene boundaries must be finite and inside the inventory")
    if any(values[index] <= values[index - 1] for index in range(1, len(values))):
        raise ValueError("scene boundaries must be strictly increasing")
    return values


def _scene_segments(
    frames: Sequence[tuple[float, NDArray[np.uint8]]], boundaries: Sequence[float]
) -> tuple[tuple[int, int], ...]:
    starts = [0]
    for boundary in boundaries:
        starts.append(
            next(index for index, item in enumerate(frames) if item[0] >= boundary)
        )
    starts.append(len(frames))
    return tuple(
        (left, right) for left, right in zip(starts, starts[1:]) if right > left
    )


def assess_anchor_corrections(
    corrections: Sequence[MotionTransform],
    config: StabilizationConfig,
    *,
    affected_ranges: Sequence[tuple[float, float]],
) -> StabilizationAssessment:
    """Validate source-rate anchor corrections without re-smoothing them."""
    measured = tuple(corrections)
    if not measured:
        return StabilizationAssessment(
            recommended=False, reason="no_anchor_corrections", crop_ratio=0.0
        )
    if any(item.semantics != "frame_correction" for item in measured):
        raise ValueError("anchor assessment requires frame corrections")
    bridged_indexes = _bridged_anchor_correction_indexes(measured, config)
    if bridged_indexes:
        mutable = list(measured)
        for index in bridged_indexes:
            left, weak, right = mutable[index - 1], mutable[index], mutable[index + 1]
            fraction = (weak.timestamp_seconds - left.timestamp_seconds) / (
                right.timestamp_seconds - left.timestamp_seconds
            )
            mutable[index] = MotionTransform(
                timestamp_seconds=weak.timestamp_seconds,
                rotation_degrees=_lerp(
                    left.rotation_degrees, right.rotation_degrees, fraction
                ),
                scale=_lerp(left.scale, right.scale, fraction),
                translation_x=_lerp(left.translation_x, right.translation_x, fraction),
                translation_y=_lerp(left.translation_y, right.translation_y, fraction),
                inlier_ratio=config.minimum_anchor_inlier_ratio,
                residual_pixels=min(
                    config.maximum_anchor_residual_pixels,
                    max(
                        left.residual_pixels,
                        weak.residual_pixels,
                        right.residual_pixels,
                    ),
                ),
                semantics="frame_correction",
            )
        measured = tuple(mutable)
    if any(
        measured[index].timestamp_seconds <= measured[index - 1].timestamp_seconds
        for index in range(1, len(measured))
    ):
        raise ValueError("anchor corrections must be strictly ordered")
    if any(
        item.inlier_ratio < config.minimum_anchor_inlier_ratio
        or item.residual_pixels > config.maximum_anchor_residual_pixels
        or abs(item.rotation_degrees) > config.maximum_rotation_degrees
        or abs(item.scale - 1.0) > config.maximum_scale_excursion
        for item in measured
        if not item.scene_boundary
    ):
        return StabilizationAssessment(
            recommended=False,
            reason="unreliable_anchor_correction",
            crop_ratio=0.0,
            transforms=measured,
        )
    amplitude = _motion_amplitude(measured)
    if amplitude < config.minimum_motion_amplitude_pixels:
        return StabilizationAssessment(
            recommended=False,
            reason="insufficient_motion_amplitude",
            crop_ratio=0.0,
            transforms=measured,
        )
    crop_ratio = _required_crop_ratio(measured, config)
    if crop_ratio > config.max_crop_ratio:
        return StabilizationAssessment(
            recommended=False,
            reason="crop_budget_exceeded",
            crop_ratio=crop_ratio,
            transforms=measured,
        )
    normalized_ranges = tuple(
        (float(start), float(end))
        for start, end in affected_ranges
        if math.isfinite(start) and math.isfinite(end) and 0 <= start < end
    )
    if len(normalized_ranges) != len(tuple(affected_ranges)):
        raise ValueError("anchor affected ranges must be finite and non-empty")
    return StabilizationAssessment(
        recommended=True,
        reason="measured_scene_anchor_motion",
        crop_ratio=crop_ratio,
        transforms=measured,
        parameters={
            "algorithm_version": "anchor_v1",
            "affected_ranges": cast(
                JsonValue, [[start, end] for start, end in normalized_ranges]
            ),
            "crop_ratio": crop_ratio,
            "frame_height": config.frame_height,
            "frame_width": config.frame_width,
            "source_rate_cap_fps": config.source_rate_cap_fps,
            "maximum_frame_inventory": config.maximum_frame_inventory,
            "residual_goal_median_pixels": config.residual_goal_median_pixels,
            "residual_goal_p90_pixels": config.residual_goal_p90_pixels,
            "bridged_low_confidence_samples": len(bridged_indexes),
        },
    )


def _bridged_anchor_correction_indexes(
    corrections: Sequence[MotionTransform], config: StabilizationConfig
) -> frozenset[int]:
    if config.maximum_bridged_low_confidence_samples == 0 or len(corrections) < 3:
        return frozenset()

    def reliable(item: MotionTransform) -> bool:
        return (
            not item.scene_boundary
            and item.inlier_ratio >= config.minimum_anchor_inlier_ratio
            and item.residual_pixels <= config.maximum_anchor_residual_pixels
        )

    weak = tuple(index for index, item in enumerate(corrections) if not reliable(item))
    if len(weak) != 1:
        return frozenset()
    index = weak[0]
    if index == 0 or index == len(corrections) - 1:
        return frozenset()
    left, item, right = corrections[index - 1 : index + 2]
    if (
        item.scene_boundary
        or item.inlier_ratio >= config.minimum_anchor_inlier_ratio
        or abs(item.rotation_degrees) > config.maximum_rotation_degrees
        or abs(item.scale - 1.0) > config.maximum_scale_excursion
        or not reliable(left)
        or not reliable(right)
    ):
        return frozenset()
    steps = (
        item.timestamp_seconds - left.timestamp_seconds,
        right.timestamp_seconds - item.timestamp_seconds,
    )
    if any(step <= 0 or step > config.maximum_timeline_gap_seconds for step in steps):
        return frozenset()
    if not math.isclose(
        steps[0],
        steps[1],
        rel_tol=0.0,
        abs_tol=config.exact_timestamp_tolerance_seconds,
    ):
        return frozenset()
    return frozenset((index,))


def _representative_indexes(length: int, *, maximum: int) -> tuple[int, ...]:
    if length <= maximum:
        return tuple(range(length))
    return tuple(
        sorted(
            {
                int(round(index * (length - 1) / (maximum - 1)))
                for index in range(maximum)
            }
        )
    )


def _background_feature_coverage(
    frame: NDArray[np.uint8], config: StabilizationConfig
) -> float:
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise RescueMediaError("OpenCV is required for CPU stabilization") from exc
    gray = _grayscale(frame)
    points = cv2.goodFeaturesToTrack(
        gray, maxCorners=config.max_features, qualityLevel=0.01, minDistance=5
    )
    if points is None:
        return 0.0
    cells_x = min(8, max(1, gray.shape[1] // 16))
    cells_y = min(6, max(1, gray.shape[0] // 16))
    occupied = {
        (
            min(cells_x - 1, int(point[0][0] * cells_x / gray.shape[1])),
            min(cells_y - 1, int(point[0][1] * cells_y / gray.shape[0])),
        )
        for point in points
    }
    return len(occupied) / float(cells_x * cells_y)


def _anchor_measurement_is_reliable(
    measured: tuple[float, float, float, float, float, float],
    config: StabilizationConfig,
) -> bool:
    rotation, scale, tx, ty, inliers, residual = measured
    values = (rotation, scale, tx, ty, inliers, residual)
    return (
        all(math.isfinite(value) for value in values)
        and inliers >= config.minimum_anchor_inlier_ratio
        and residual <= config.maximum_anchor_residual_pixels
        and abs(rotation) <= config.maximum_rotation_degrees
        and abs(scale - 1.0) <= config.maximum_scale_excursion
    )


def _phase_translation_measurement(
    anchor: NDArray[np.uint8],
    frame: NDArray[np.uint8],
    config: StabilizationConfig,
) -> tuple[float, float, float, float] | None:
    """Return global translation, response and regional consensus deviation."""
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover
        raise RescueMediaError("OpenCV is required for CPU stabilization") from exc
    left = np.asarray(anchor, dtype=np.float32)
    right = np.asarray(frame, dtype=np.float32)
    window = cv2.createHanningWindow((left.shape[1], left.shape[0]), cv2.CV_32F)
    (tx, ty), response = cv2.phaseCorrelate(left, right, window)
    if not all(math.isfinite(value) for value in (tx, ty, response)):
        return None
    regional: list[tuple[float, float]] = []
    for row in range(config.phase_region_rows):
        y0 = round(row * left.shape[0] / config.phase_region_rows)
        y1 = round((row + 1) * left.shape[0] / config.phase_region_rows)
        for column in range(config.phase_region_columns):
            x0 = round(column * left.shape[1] / config.phase_region_columns)
            x1 = round((column + 1) * left.shape[1] / config.phase_region_columns)
            region_left = left[y0:y1, x0:x1]
            region_right = right[y0:y1, x0:x1]
            region_window = cv2.createHanningWindow(
                (region_left.shape[1], region_left.shape[0]), cv2.CV_32F
            )
            (region_x, region_y), region_response = cv2.phaseCorrelate(
                region_left, region_right, region_window
            )
            if (
                math.isfinite(region_x)
                and math.isfinite(region_y)
                and region_response >= config.minimum_phase_correlation_response
            ):
                regional.append((region_x, region_y))
    if len(regional) < config.minimum_consistent_phase_regions:
        return None
    deviations = tuple(math.hypot(x - tx, y - ty) for x, y in regional)
    return tx, ty, float(response), float(np.median(deviations))


def _persistent_transition_lk_measurements(
    frames: Sequence[NDArray[np.uint8]],
    config: StabilizationConfig,
    *,
    cancellation_callback: Callable[[], bool] = _transition_consensus_not_cancelled,
) -> tuple[tuple[float, float, float, int, float, float], ...] | None:
    """Return only LK vectors whose features survive the complete candidate."""
    _raise_if_transition_consensus_cancelled(cancellation_callback)
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - package dependency boundary
        raise RescueMediaError("OpenCV is required for CPU stabilization") from exc
    ordered = tuple(np.asarray(frame, dtype=np.uint8) for frame in frames)
    if len(ordered) < 2 or len(ordered) > config.maximum_transition_candidate_frames:
        return None
    points = cv2.goodFeaturesToTrack(
        ordered[0],
        maxCorners=config.maximum_transition_lk_feature_inventory,
        qualityLevel=0.01,
        minDistance=5,
    )
    _raise_if_transition_consensus_cancelled(cancellation_callback)
    if points is None or len(points) < config.minimum_transition_lk_track_count:
        return None
    initial = points.reshape(-1, 2)
    active_ids: NDArray[np.int32] = np.arange(len(initial), dtype=np.int32)
    current = initial.copy()
    history: list[dict[int, NDArray[np.float32]]] = [
        {
            int(index): np.asarray(point, dtype=np.float32)
            for index, point in enumerate(initial)
        }
    ]
    for previous, following in zip(ordered, ordered[1:], strict=False):
        _raise_if_transition_consensus_cancelled(cancellation_callback)
        tracked, status, _errors = cv2.calcOpticalFlowPyrLK(
            previous, following, current.reshape(-1, 1, 2), None
        )
        _raise_if_transition_consensus_cancelled(cancellation_callback)
        if tracked is None or status is None:
            return None
        returned, reverse_status, _reverse_errors = cv2.calcOpticalFlowPyrLK(
            following, previous, tracked, None
        )
        _raise_if_transition_consensus_cancelled(cancellation_callback)
        if returned is None or reverse_status is None:
            return None
        target = tracked.reshape(-1, 2)
        roundtrip = np.linalg.norm(returned.reshape(-1, 2) - current, axis=1)
        inside = (
            (target[:, 0] >= 0)
            & (target[:, 0] < previous.shape[1])
            & (target[:, 1] >= 0)
            & (target[:, 1] < previous.shape[0])
        )
        valid = (
            status.reshape(-1).astype(bool)
            & reverse_status.reshape(-1).astype(bool)
            & (roundtrip <= config.maximum_tracking_roundtrip_error_pixels)
            & inside
        )
        active_ids = active_ids[valid]
        current = target[valid]
        history.append(
            {
                int(index): np.asarray(point, dtype=np.float32)
                for index, point in zip(active_ids, current, strict=True)
            }
        )
        if len(active_ids) < config.minimum_transition_lk_track_count:
            return None
    survivor_ids = tuple(int(value) for value in active_ids)
    survivor_ratio = len(survivor_ids) / float(len(initial))
    if survivor_ratio < config.minimum_transition_lk_track_ratio:
        return None
    occupied = {
        (
            min(
                config.transition_phase_region_columns - 1,
                int(
                    initial[index][0]
                    * config.transition_phase_region_columns
                    / ordered[0].shape[1]
                ),
            ),
            min(
                config.transition_phase_region_rows - 1,
                int(
                    initial[index][1]
                    * config.transition_phase_region_rows
                    / ordered[0].shape[0]
                ),
            ),
        )
        for index in survivor_ids
    }
    coverage = len(occupied) / float(
        config.transition_phase_region_rows * config.transition_phase_region_columns
    )
    if coverage < config.minimum_transition_lk_spatial_coverage:
        return None
    measurements: list[tuple[float, float, float, int, float, float]] = []
    survivor_set = frozenset(survivor_ids)
    for previous_positions, following_positions in zip(
        history, history[1:], strict=False
    ):
        _raise_if_transition_consensus_cancelled(cancellation_callback)
        shared = tuple(
            sorted(
                survivor_set.intersection(previous_positions).intersection(
                    following_positions
                )
            )
        )
        if len(shared) != len(survivor_ids):
            return None
        vectors = np.asarray(
            [
                following_positions[index] - previous_positions[index]
                for index in shared
            ],
            dtype=np.float64,
        )
        lk_x, lk_y = (float(value) for value in np.median(vectors, axis=0))
        residual = float(
            np.percentile(
                np.linalg.norm(vectors - np.asarray((lk_x, lk_y)), axis=1), 90
            )
        )
        if residual > config.maximum_transition_lk_residual_pixels:
            return None
        measurements.append(
            (lk_x, lk_y, residual, len(shared), survivor_ratio, coverage)
        )
    return tuple(measurements)


def _transition_translation_measurement(
    previous: NDArray[np.uint8],
    current: NDArray[np.uint8],
    config: StabilizationConfig,
    *,
    persistent_lk: tuple[float, float, float, int, float, float],
    cancellation_callback: Callable[[], bool] = _transition_consensus_not_cancelled,
) -> tuple[float, float, float, float] | None:
    """Require three independent translation estimators to agree for one pair."""
    _raise_if_transition_consensus_cancelled(cancellation_callback)
    try:
        import cv2
    except ImportError as exc:  # pragma: no cover - package dependency boundary
        raise RescueMediaError("OpenCV is required for CPU stabilization") from exc

    left = np.asarray(previous, dtype=np.float32)
    right = np.asarray(current, dtype=np.float32)
    regional: list[tuple[float, float, float]] = []
    nonempty_regions = 0
    for row in range(config.transition_phase_region_rows):
        y0 = round(row * left.shape[0] / config.transition_phase_region_rows)
        y1 = round((row + 1) * left.shape[0] / config.transition_phase_region_rows)
        for column in range(config.transition_phase_region_columns):
            _raise_if_transition_consensus_cancelled(cancellation_callback)
            x0 = round(column * left.shape[1] / config.transition_phase_region_columns)
            x1 = round(
                (column + 1) * left.shape[1] / config.transition_phase_region_columns
            )
            region_left = left[y0:y1, x0:x1]
            region_right = right[y0:y1, x0:x1]
            if region_left.shape[0] < 8 or region_left.shape[1] < 8:
                return None
            if float(np.std(region_left)) < config.minimum_transition_tile_luma_std:
                continue
            nonempty_regions += 1
            window = cv2.createHanningWindow(
                (region_left.shape[1], region_left.shape[0]), cv2.CV_32F
            )
            (tx, ty), response = cv2.phaseCorrelate(
                region_left.copy(), region_right.copy(), window
            )
            _raise_if_transition_consensus_cancelled(cancellation_callback)
            raw_mad = float(np.mean(np.abs(region_left - region_right)))
            aligned = cv2.warpAffine(
                region_right,
                np.asarray(((1.0, 0.0, -tx), (0.0, 1.0, -ty)), dtype=np.float32),
                (region_left.shape[1], region_left.shape[0]),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            )
            _raise_if_transition_consensus_cancelled(cancellation_callback)
            margin_x = min(region_left.shape[1] // 4, int(math.ceil(abs(tx))) + 2)
            margin_y = min(region_left.shape[0] // 4, int(math.ceil(abs(ty))) + 2)
            if (
                region_left.shape[1] - 2 * margin_x < 8
                or region_left.shape[0] - 2 * margin_y < 8
            ):
                continue
            visible_left = region_left[
                margin_y : region_left.shape[0] - margin_y,
                margin_x : region_left.shape[1] - margin_x,
            ]
            visible_aligned = aligned[
                margin_y : aligned.shape[0] - margin_y,
                margin_x : aligned.shape[1] - margin_x,
            ]
            aligned_mad = float(np.mean(np.abs(visible_left - visible_aligned)))
            alignment_gain = (raw_mad - aligned_mad) / max(raw_mad, 1e-9)
            if (
                all(math.isfinite(value) for value in (tx, ty, response))
                and response >= config.minimum_transition_phase_response
                and alignment_gain > config.minimum_transition_alignment_gain
            ):
                regional.append((float(tx), float(ty), float(response)))
    if nonempty_regions < config.minimum_transition_nonempty_regions:
        return None
    required_regions = math.ceil(0.8 * nonempty_regions)
    if len(regional) < required_regions:
        return None
    phase_x = float(np.median([item[0] for item in regional]))
    phase_y = float(np.median([item[1] for item in regional]))
    region_residuals = np.asarray(
        [math.hypot(item[0] - phase_x, item[1] - phase_y) for item in regional],
        dtype=np.float64,
    )
    regional_p90 = float(np.percentile(region_residuals, 90))
    consistent = int(
        np.count_nonzero(
            region_residuals <= config.maximum_transition_regional_p90_pixels
        )
    )
    if (
        consistent < required_regions
        or regional_p90 > config.maximum_transition_regional_p90_pixels
    ):
        return None

    lk_x, lk_y, lk_residual, lk_count, lk_ratio, lk_coverage = persistent_lk
    if (
        lk_count < config.minimum_transition_lk_track_count
        or lk_ratio < config.minimum_transition_lk_track_ratio
        or lk_coverage < config.minimum_transition_lk_spatial_coverage
        or lk_residual > config.maximum_transition_lk_residual_pixels
    ):
        return None

    _raise_if_transition_consensus_cancelled(cancellation_callback)
    half_size = (max(8, previous.shape[1] // 2), max(8, previous.shape[0] // 2))
    dense_previous = cv2.resize(previous, half_size, interpolation=cv2.INTER_AREA)
    dense_current = cv2.resize(current, half_size, interpolation=cv2.INTER_AREA)
    dense = cv2.calcOpticalFlowFarneback(
        dense_previous,
        dense_current,
        None,
        pyr_scale=0.5,
        levels=3,
        winsize=21,
        iterations=3,
        poly_n=5,
        poly_sigma=1.2,
        flags=0,
    )
    _raise_if_transition_consensus_cancelled(cancellation_callback)
    if dense is None or dense.shape[:2] != dense_previous.shape:
        return None
    gradient_x = cv2.Sobel(dense_previous, cv2.CV_32F, 1, 0, ksize=3)
    gradient_y = cv2.Sobel(dense_previous, cv2.CV_32F, 0, 1, ksize=3)
    gradient = cv2.magnitude(gradient_x, gradient_y)
    texture_threshold = max(3.0, float(np.percentile(gradient, 70)))
    dense_mask = gradient >= texture_threshold
    dense_mask[:8, :] = False
    dense_mask[-8:, :] = False
    dense_mask[:, :8] = False
    dense_mask[:, -8:] = False
    dense_vectors = dense[dense_mask]
    if len(dense_vectors) < config.minimum_transition_lk_track_count:
        return None
    dense_center = np.median(dense_vectors, axis=0) * 2.0
    dense_x, dense_y = (float(value) for value in dense_center)
    dense_distances = (
        np.linalg.norm(dense_vectors - np.median(dense_vectors, axis=0), axis=1) * 2.0
    )
    dense_residual = float(np.percentile(dense_distances, 90))
    dense_coherent_ratio = float(
        np.mean(dense_distances <= config.maximum_transition_vector_disagreement_pixels)
    )
    vectors = ((phase_x, phase_y), (lk_x, lk_y), (dense_x, dense_y))
    disagreement = max(
        math.hypot(left_vector[0] - right_vector[0], left_vector[1] - right_vector[1])
        for index, left_vector in enumerate(vectors)
        for right_vector in vectors[index + 1 :]
    )
    if (
        dense_residual > config.maximum_transition_dense_residual_pixels
        or dense_coherent_ratio < config.minimum_transition_dense_coherent_ratio
        or disagreement > config.maximum_transition_vector_disagreement_pixels
    ):
        return None
    consensus_x = float(np.median([item[0] for item in vectors]))
    consensus_y = float(np.median([item[1] for item in vectors]))
    confidence = min(
        1.0,
        float(len(regional)) / float(required_regions),
        lk_ratio / config.minimum_transition_lk_track_ratio,
    )
    residual = max(regional_p90, lk_residual, dense_residual, disagreement)
    return consensus_x, consensus_y, confidence, residual


def _phase_dominant_translation(
    affine: tuple[float, float, float, float, float, float] | None,
    phase: tuple[float, float, float, float] | None,
    config: StabilizationConfig,
) -> tuple[float, float, float, float, float, float] | None:
    if affine is None or phase is None:
        return None
    rotation, scale, affine_x, affine_y, inliers, residual = affine
    phase_x, phase_y, response, regional_deviation = phase
    if (
        inliers < config.minimum_phase_fallback_inlier_ratio
        or residual > config.maximum_phase_affine_residual_pixels
        or response < config.minimum_phase_correlation_response
        or regional_deviation > config.maximum_regional_translation_deviation_pixels
    ):
        return None
    affine_agrees = (
        abs(rotation) <= config.maximum_rotation_degrees
        and abs(scale - 1.0) <= config.maximum_scale_excursion
        and math.hypot(affine_x - phase_x, affine_y - phase_y)
        <= config.maximum_phase_affine_disagreement_pixels
    )
    strong_regional_consensus = (
        response >= config.strong_phase_correlation_response
        and regional_deviation <= config.strong_regional_translation_deviation_pixels
    )
    if not affine_agrees and not strong_regional_consensus:
        return None
    return (0.0, 1.0, phase_x, phase_y, response, regional_deviation)


def _is_intentional_unidirectional_motion(
    translations: Sequence[tuple[float, float]], config: StabilizationConfig
) -> bool:
    if len(translations) < 4:
        return False
    values = np.asarray(translations, dtype=np.float64)
    indexes: NDArray[np.float64] = np.arange(len(values), dtype=np.float64)
    slopes = tuple(float(np.polyfit(indexes, values[:, axis], 1)[0]) for axis in (0, 1))
    return (
        max(abs(item) for item in slopes)
        > config.maximum_intentional_trend_pixels_per_frame
    )


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
        returned, reverse_status, _reverse_errors = cv2.calcOpticalFlowPyrLK(
            current, previous, tracked, None
        )
        if returned is None or reverse_status is None:
            return None
        roundtrip_error = np.linalg.norm(
            returned.reshape(-1, 2) - points.reshape(-1, 2), axis=1
        )
        source_points = points.reshape(-1, 2)
        margin_x = previous.shape[1] * config.max_crop_ratio
        margin_y = previous.shape[0] * config.max_crop_ratio
        background_safe = (
            (source_points[:, 0] >= margin_x)
            & (source_points[:, 0] < previous.shape[1] - margin_x)
            & (source_points[:, 1] >= margin_y)
            & (source_points[:, 1] < previous.shape[0] - margin_y)
        )
        mask = (
            status.reshape(-1).astype(bool)
            & reverse_status.reshape(-1).astype(bool)
            & (roundtrip_error <= config.maximum_tracking_roundtrip_error_pixels)
            & background_safe
        )
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
    transform: MotionTransform,
    width: int,
    height: int,
    *,
    safe_crop_ratio: float = 0.0,
    translation_scale_x: float = 1.0,
    translation_scale_y: float = 1.0,
) -> NDArray[np.float32]:
    matrix = _motion_matrix(
        transform.model_copy(
            update={
                "translation_x": transform.translation_x * translation_scale_x,
                "translation_y": transform.translation_y * translation_scale_y,
            }
        )
    )
    if safe_crop_ratio > 0:
        scale = 1.0 / max(1e-9, 1.0 - safe_crop_ratio)
        center_x = (width - 1) / 2.0
        center_y = (height - 1) / 2.0
        centered_zoom = np.array(
            [
                [scale, 0.0, center_x * (1.0 - scale)],
                [0.0, scale, center_y * (1.0 - scale)],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        matrix = centered_zoom @ matrix
    return np.asarray(matrix[:2, :], dtype=np.float32)


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
