"""Strict, versioned data contracts for local Video Rescue."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

RESCUE_SCHEMA_VERSION: Final = "0.2"
_SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
_DAMAGE_ID_PATTERN: Final = r"^damage_[0-9a-f]{64}$"
RESCUE_REQUIRED_VERIFICATION_CHECK_IDS: Final = (
    "decodable",
    "duration",
    "streams",
    "source_read_only",
)
RESCUE_ACTION_VERIFICATION_CHECK_IDS: Final = (
    "deblur_edge_recovery",
    "deblur_ringing",
    "deblur_temporal_consistency",
    "tonal_interference_reduction",
    "tonal_boundary_transient",
    "anchor_stabilization_residual",
    "transition_stabilization_consensus",
    "transition_stabilization_seam",
    "transition_stabilization_coverage",
)
_APPLICABLE_REVIEW_GATES: Final = frozenset(
    {
        "audio_loudness",
        "audio_peak",
        "audio_sample_rate",
        "black_regression",
        "fixed_av_offset",
        "flicker_regression",
        "freeze_regression",
        "luma_chroma_side_effects",
        "luma_clipping",
        "noise_side_effects",
        "perceptible_audio_noise_reduction",
        "perceptible_luma_improvement",
        "perceptible_sharpness_improvement",
        "perceptible_stabilization_improvement",
        "sharpness_side_effects",
        "stabilization_crop",
    }
)
RESCUE_REQUIRED_PUBLIC_DOCUMENTS: Final = (
    "rescue-plan.json",
    "damaged-segments.json",
    "changes.json",
    "verification-report.json",
    "technical-report.json",
    "report.html",
)
_WINDOWS_ABSOLUTE_PATH_PATTERN: Final = re.compile(r"[A-Za-z]:[\\/]")
_UNIX_ABSOLUTE_PATH_PATTERN: Final = re.compile(r"(?<![A-Za-z0-9_.-])/\S+")
_UNC_PATH_PATTERN: Final = re.compile(r"\\\\[^\\/\s]+[\\/]")


class _FrozenDict(dict[str, Any]):
    """A JSON object which exposes reads but rejects in-place mutation."""

    def _reject(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen JSON object does not support mutation")

    __setitem__ = _reject
    __delitem__ = _reject
    clear = _reject
    pop = _reject
    popitem = _reject  # type: ignore[assignment]
    setdefault = _reject
    update = _reject
    __ior__ = _reject  # type: ignore[assignment]


def rescue_public_artifacts(*, include_improved: bool = False) -> tuple[str, ...]:
    """Return the canonical, digest-bound Rescue publication declaration."""
    media = (
        ("faithful-rescue.mp4", "improved-viewing.mp4")
        if include_improved
        else ("faithful-rescue.mp4",)
    )
    return (*RESCUE_REQUIRED_PUBLIC_DOCUMENTS, *media)


class _FrozenList(list[Any]):
    """A JSON list which preserves serialization shape but rejects mutation."""

    def _reject(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen JSON list does not support mutation")

    __setitem__ = _reject
    __delitem__ = _reject
    __iadd__ = _reject  # type: ignore[assignment]
    __imul__ = _reject  # type: ignore[assignment]
    append = _reject
    clear = _reject
    extend = _reject
    insert = _reject
    pop = _reject
    remove = _reject
    reverse = _reject
    sort = _reject


class RescueModel(BaseModel):
    """Base model for immutable, versioned public Rescue documents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def freeze_nested_containers(self) -> Self:
        """Prevent post-validation JSON mutation from invalidating a digest."""
        for field_name in type(self).model_fields:
            object.__setattr__(
                self, field_name, _deep_freeze(getattr(self, field_name))
            )
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return a fully revalidated copy, including supplied updates."""
        del deep
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


class RescueStrategy(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"


class RescueSymptom(StrEnum):
    UNPLAYABLE = "unplayable"
    TIMELINE_DISCONTINUITY = "timeline_discontinuity"
    MISSING_AUDIO = "missing_audio"
    AUDIO_VIDEO_OFFSET = "audio_video_offset"
    DARK = "dark"
    VIDEO_NOISE = "video_noise"
    SOFT_DETAIL = "soft_detail"
    FLICKER = "flicker"
    SHAKE = "shake"
    LOW_LOUDNESS = "low_loudness"
    AUDIO_NOISE = "audio_noise"
    AUDIO_CLIPPING = "audio_clipping"


class DamageKind(StrEnum):
    DECODABLE = "decodable"
    UNDECODABLE = "undecodable"
    TIMESTAMP_DISCONTINUITY = "timestamp_discontinuity"
    MISSING_STREAM = "missing_stream"
    FIXED_AV_OFFSET = "fixed_av_offset"
    DARK = "dark"
    VIDEO_NOISE = "video_noise"
    SOFT_DETAIL = "soft_detail"
    FLICKER = "flicker"
    SHAKE = "shake"
    LOW_LOUDNESS = "low_loudness"
    AUDIO_NOISE = "audio_noise"
    AUDIO_CLIPPING = "audio_clipping"
    UNCERTAIN = "uncertain"
    MISSING_INFORMATION = "missing_information"


class RescueActionKind(StrEnum):
    REMUX = "remux"
    REBUILD_TIMESTAMPS = "rebuild_timestamps"
    SELECT_TRACKS = "select_tracks"
    NORMALIZE_ROTATION = "normalize_rotation"
    SALVAGE_SEGMENTS = "salvage_segments"
    TRIM_DAMAGED_EDGES = "trim_damaged_edges"
    CORRECT_FIXED_AV_OFFSET = "correct_fixed_av_offset"
    ADJUST_LUMA = "adjust_luma"
    DENOISE_VIDEO = "denoise_video"
    SHARPEN = "sharpen"
    DEFLICKER = "deflicker"
    STABILIZE = "stabilize"
    NORMALIZE_AUDIO = "normalize_audio"
    DENOISE_AUDIO = "denoise_audio"
    VERIFY = "verify"
    DEBLUR = "deblur"


class RescueOutcome(StrEnum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class RescueVerificationStatus(StrEnum):
    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class RescueActionExecutionStatus(StrEnum):
    ATTEMPTED = "attempted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


_ACTION_ORDER: Final = {
    kind: position for position, kind in enumerate(RescueActionKind)
}
_BALANCED_ONLY_ACTIONS: Final = frozenset(
    {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.SHARPEN,
        RescueActionKind.DEFLICKER,
        RescueActionKind.STABILIZE,
        RescueActionKind.NORMALIZE_AUDIO,
        RescueActionKind.DENOISE_AUDIO,
    }
)
FAITHFUL_RESTORATION_ACTION_KINDS: Final = frozenset(
    {
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.DEBLUR,
        RescueActionKind.STABILIZE,
        RescueActionKind.DENOISE_AUDIO,
    }
)
REMAINING_IMPROVEMENT_ACTION_KINDS: Final = frozenset(
    {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.SHARPEN,
        RescueActionKind.DEFLICKER,
        RescueActionKind.NORMALIZE_AUDIO,
    }
)


class DamageInterval(RescueModel):
    """One observable media interval, not a claim about its root cause."""

    id: str = Field(pattern=_DAMAGE_ID_PATTERN)
    stream_id: str = Field(min_length=1)
    kind: DamageKind
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    description: str = Field(default="Observable media interval.", min_length=1)
    measurements: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must not be before start_seconds")
        return self


class MediaDamageMap(RescueModel):
    """Deterministic local scan results with no source path disclosure."""

    schema_version: Literal["0.2"] = RESCUE_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    scanner_version: str = Field(default="1", min_length=1)
    scan_coverage: tuple[tuple[float, float], ...] = ()
    intervals: tuple[DamageInterval, ...] = ()

    @model_validator(mode="after")
    def validate_intervals(self) -> Self:
        identifiers: set[str] = set()
        for start_seconds, end_seconds in self.scan_coverage:
            _validate_time_range(start_seconds, end_seconds, field_name="scan coverage")
            if end_seconds > self.duration_seconds:
                raise ValueError("scan coverage exceeds source duration")
        for interval in self.intervals:
            if interval.id in identifiers:
                raise ValueError("duplicate damage interval ID")
            identifiers.add(interval.id)
            expected_id = make_damage_id(
                self.input_hash,
                interval.stream_id,
                interval.kind,
                interval.start_seconds,
                interval.end_seconds,
            )
            if interval.id != expected_id:
                raise ValueError("damage interval ID does not match observable inputs")
            if interval.end_seconds > self.duration_seconds:
                raise ValueError("damage interval exceeds source duration")
        object.__setattr__(self, "scan_coverage", tuple(sorted(self.scan_coverage)))
        object.__setattr__(
            self, "intervals", tuple(sorted(self.intervals, key=_damage_sort_key))
        )
        return self


class CanonicalVideoEncodeContract(RescueModel):
    """Stable action wire for every output-affecting video encode option."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_version: Literal["1"] = "1"
    encoder: Literal["libx264"] = "libx264"
    preset: Literal["slow", "medium"] = "medium"
    crf: int = Field(default=16, ge=1, le=30)
    pixel_format: Literal["yuv420p"] = "yuv420p"
    profile: Literal["high"] = "high"
    level: Literal["3.1"] = "3.1"
    fps_mode: Literal["cfr"] = "cfr"
    track_timescale: int = Field(default=120000, ge=120000, le=120000, strict=True)
    gop_size: int = Field(default=48, ge=48, le=48, strict=True)
    minimum_keyframe_interval: int = Field(default=24, ge=24, le=24, strict=True)
    b_frames: int = Field(default=0, ge=0, le=0, strict=True)
    reference_frames: int = Field(default=3, ge=3, le=3, strict=True)
    scene_change_threshold: int = Field(default=0, ge=0, le=0, strict=True)


class SharpenQualificationProfile(RescueModel):
    """One finite, source-independent SHARPEN candidate profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    cas_strength_scale: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    unsharp_amount_scale: float = Field(gt=0.0, le=1.0, allow_inf_nan=False)
    pass_count: int = Field(ge=1, le=3, strict=True)
    radius: int = Field(default=2, ge=1, le=3, strict=True)


class StabilizationQualificationProfile(RescueModel):
    """One finite optional estimator profile for transition stabilization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    estimator_algorithm_version: Literal["transition_anchor_v1"] = (
        "transition_anchor_v1"
    )


def _default_sharpen_qualification_profiles() -> tuple[
    SharpenQualificationProfile, ...
]:
    return (
        SharpenQualificationProfile(
            profile_id="full",
            cas_strength_scale=1.0,
            unsharp_amount_scale=1.0,
            pass_count=3,
        ),
        SharpenQualificationProfile(
            profile_id="moderate",
            cas_strength_scale=0.75,
            unsharp_amount_scale=0.75,
            pass_count=2,
        ),
        SharpenQualificationProfile(
            profile_id="gentle",
            cas_strength_scale=0.5,
            unsharp_amount_scale=0.5,
            pass_count=1,
        ),
    )


def _default_stabilization_qualification_profiles() -> tuple[
    StabilizationQualificationProfile, ...
]:
    return (StabilizationQualificationProfile(profile_id="transition_anchor_v1"),)


class RescueEffectiveConfig(RescueModel):
    """Path-free configuration bound into a confirmation digest."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    planner_version: str = Field(default="1", min_length=1)
    deblur_algorithm_version: Literal["1"] = "1"
    tonal_algorithm_version: Literal["1"] = "1"
    anchor_stabilization_algorithm_version: Literal["1"] = "1"
    source_read_only: Literal[True] = True
    max_preview_ranges: int = Field(default=3, ge=1, le=3)
    max_preview_total_seconds: float = Field(
        default=10.0, gt=0, le=10, allow_inf_nan=False
    )
    trim_guard_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    balanced_strength_limit: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    improved_video_crf: int = Field(default=16, ge=1, le=30)
    improved_video_preset: Literal["slow", "medium"] = "medium"
    improved_pixel_format: Literal["yuv420p"] = "yuv420p"
    video_encode_topology_version: Literal["1"] = "1"
    video_encoder: Literal["libx264"] = "libx264"
    video_profile: Literal["high"] = "high"
    video_level: Literal["3.1"] = "3.1"
    video_fps_mode: Literal["cfr"] = "cfr"
    video_track_timescale: int = Field(
        default=120000, ge=120000, le=120000, strict=True
    )
    video_gop_size: int = Field(default=48, ge=48, le=48, strict=True)
    video_min_keyframe_interval: int = Field(default=24, ge=24, le=24, strict=True)
    video_b_frames: int = Field(default=0, ge=0, le=0, strict=True)
    video_reference_frames: int = Field(default=3, ge=3, le=3, strict=True)
    video_scene_change_threshold: int = Field(default=0, ge=0, le=0, strict=True)
    improved_audio_bitrate_kbps: int = Field(default=192, ge=96, le=320)
    sharpen_qualification_profiles: tuple[SharpenQualificationProfile, ...] = Field(
        default_factory=_default_sharpen_qualification_profiles
    )
    stabilization_qualification_profiles: tuple[
        StabilizationQualificationProfile, ...
    ] = Field(default_factory=_default_stabilization_qualification_profiles)
    locked_ranges: tuple[tuple[float, float], ...] = ()
    verification_policy: tuple[str, ...] = RESCUE_REQUIRED_VERIFICATION_CHECK_IDS

    @field_validator("locked_ranges", mode="before")
    @classmethod
    def accept_json_range_arrays(cls, value: object) -> object:
        """Normalize only JSON array containers; scalar values remain strict."""
        if isinstance(value, list):
            return tuple(
                tuple(item) if isinstance(item, list) else item for item in value
            )
        return value

    @field_validator("verification_policy", mode="before")
    @classmethod
    def accept_json_policy_array(cls, value: object) -> object:
        """Normalize the JSON array representation of the immutable policy."""
        return tuple(value) if isinstance(value, list) else value

    @field_validator("sharpen_qualification_profiles", mode="before")
    @classmethod
    def accept_json_profile_array(cls, value: object) -> object:
        """Normalize the JSON array while retaining strict nested validation."""
        return tuple(value) if isinstance(value, list) else value

    @field_validator("stabilization_qualification_profiles", mode="before")
    @classmethod
    def accept_json_stabilization_profile_array(cls, value: object) -> object:
        """Normalize the JSON array while retaining strict nested validation."""
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.verification_policy != RESCUE_REQUIRED_VERIFICATION_CHECK_IDS:
            raise ValueError(
                "verification policy must match the canonical v0.1 verification policy"
            )
        normalized_ranges: list[tuple[float, float]] = []
        for start_seconds, end_seconds in self.locked_ranges:
            _validate_time_range(
                start_seconds, end_seconds, field_name="locked source range"
            )
            normalized_ranges.append((start_seconds, end_seconds))
        object.__setattr__(self, "locked_ranges", tuple(sorted(set(normalized_ranges))))
        profile_ids = tuple(
            profile.profile_id for profile in self.sharpen_qualification_profiles
        )
        if not profile_ids:
            raise ValueError("at least one SHARPEN qualification profile is required")
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("duplicate SHARPEN qualification profile ID")
        stabilization_profile_ids = tuple(
            profile.profile_id for profile in self.stabilization_qualification_profiles
        )
        if not stabilization_profile_ids:
            raise ValueError("at least one STABILIZE qualification profile is required")
        if len(stabilization_profile_ids) != len(set(stabilization_profile_ids)):
            raise ValueError("duplicate STABILIZE qualification profile ID")
        return self


class RescueAction(RescueModel):
    """One ordered, inspectable local media operation in a Rescue plan."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    kind: RescueActionKind
    description: str = Field(min_length=1)
    source_ranges: tuple[tuple[float, float], ...] = ()
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    changes_content: bool
    requires_confirmation: bool
    depends_on: tuple[str, ...] = ()
    fallback: RescueActionKind | None = None
    strategy: RescueStrategy = RescueStrategy.CONSERVATIVE

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        for start_seconds, end_seconds in self.source_ranges:
            _validate_time_range(start_seconds, end_seconds, field_name="source range")
        if self.changes_content and not self.requires_confirmation:
            raise ValueError("content-changing rescue action requires confirmation")
        if self.id in self.depends_on:
            raise ValueError("rescue action cannot depend on itself")
        if len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("duplicate rescue action dependency")
        return self


class RescueActionExecution(RescueModel):
    """Terminal truth about one planned action, separate from the proposal."""

    action_id: str = Field(min_length=1)
    kind: RescueActionKind
    status: RescueActionExecutionStatus
    artifact_role: Literal["faithful", "improved", "document"]
    reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_reason(self) -> Self:
        if (
            self.status
            in {
                RescueActionExecutionStatus.FAILED,
                RescueActionExecutionStatus.SKIPPED,
            }
            and self.reason is None
        ):
            raise ValueError("failed or skipped action execution requires a reason")
        return self


class RescueArtifact(RescueModel):
    """A public output-root-relative Rescue artifact reference."""

    artifact_role: Literal["faithful", "improved", "document"]
    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        _validate_relative_posix_path(self.relative_path, field_name="artifact path")
        expected_media_path = {
            "faithful": "faithful-rescue.mp4",
            "improved": "improved-viewing.mp4",
        }.get(self.artifact_role)
        if (
            expected_media_path is not None
            and self.relative_path != expected_media_path
        ):
            raise ValueError("rescue media artifact role does not match its path")
        return self


class RescuePlan(RescueModel):
    """The exact strategy and artifacts requiring a bounded confirmation."""

    schema_version: Literal["0.2"] = RESCUE_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    strategy: RescueStrategy
    requested_symptoms: tuple[RescueSymptom, ...] = ()
    assessment_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    assessment_limitations: tuple[str, ...] = ()
    assessment_warnings: tuple[str, ...] = ()
    effective_config: RescueEffectiveConfig
    actions: tuple[RescueAction, ...] = ()
    preview_ranges: tuple[tuple[float, float], ...] = ()
    private_artifacts: tuple[str, ...] = ()
    public_artifacts: tuple[str, ...] = ()
    damage_intervals: tuple[DamageInterval, ...] = ()
    plan_digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        action_ids = tuple(action.id for action in self.actions)
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("duplicate rescue action ID")
        action_kinds = tuple(action.kind for action in self.actions)
        if len(action_kinds) != len(set(action_kinds)):
            raise ValueError("duplicate rescue action kind")
        action_order = tuple(_ACTION_ORDER[action.kind] for action in self.actions)
        if action_order != tuple(sorted(action_order)):
            raise ValueError("rescue actions must use the stable action order")
        for action in self.actions:
            if action.strategy is not self.strategy:
                raise ValueError("rescue action strategy does not match its plan")
            if (
                self.strategy is RescueStrategy.CONSERVATIVE
                and action.kind in _BALANCED_ONLY_ACTIONS
            ):
                raise ValueError(
                    "Conservative plans cannot contain enhancement actions"
                )
            if any(dependency not in action_ids for dependency in action.depends_on):
                raise ValueError("rescue action dependency is not in the plan")
            _validate_action_video_encode_contract(action, self.effective_config)
        if RescueActionKind.ADJUST_LUMA in action_kinds:
            from videoscope.rescue.visual import validate_plan_luma_action_contracts

            validate_plan_luma_action_contracts(self)
        if RescueActionKind.SHARPEN in action_kinds:
            from videoscope.rescue.qualification import (
                validate_plan_sharpen_qualification_contracts,
            )

            validate_plan_sharpen_qualification_contracts(self)
        if RescueActionKind.DENOISE_AUDIO in action_kinds:
            from videoscope.rescue.tonal import validate_plan_tonal_action_contracts

            validate_plan_tonal_action_contracts(self)
        if RescueActionKind.STABILIZE in action_kinds:
            from videoscope.rescue.stabilization import (
                validate_plan_stabilization_qualification_contracts,
            )

            validate_plan_stabilization_qualification_contracts(self)
        for start_seconds, end_seconds in self.preview_ranges:
            _validate_time_range(start_seconds, end_seconds, field_name="preview range")
        _validate_path_collection(self.private_artifacts, field_name="private artifact")
        _validate_path_collection(self.public_artifacts, field_name="public artifact")
        damage_ids = tuple(interval.id for interval in self.damage_intervals)
        if len(damage_ids) != len(set(damage_ids)):
            raise ValueError("duplicate plan damage interval ID")
        for interval in self.damage_intervals:
            expected_damage_id = make_damage_id(
                self.input_hash,
                interval.stream_id,
                interval.kind,
                interval.start_seconds,
                interval.end_seconds,
            )
            if interval.id != expected_damage_id:
                raise ValueError("plan damage interval ID does not match plan input")
        object.__setattr__(
            self,
            "damage_intervals",
            tuple(sorted(self.damage_intervals, key=_damage_sort_key)),
        )
        expected = make_rescue_plan_digest(
            self.model_dump(mode="json", exclude={"plan_digest"})
        )
        if self.plan_digest != expected:
            raise ValueError("plan_digest does not match the effective RescuePlan")
        return self

    def validate_confirmation(self, confirmation: RescueConfirmation) -> None:
        """Reject a stale or over-broad confirmation before media execution."""
        if confirmation.plan_digest != self.plan_digest:
            raise ValueError("confirmation plan_digest does not match RescuePlan")
        action_ids = {
            action.id for action in self.actions if action.requires_confirmation
        }
        if set(confirmation.accepted_action_ids) != action_ids:
            raise ValueError("confirmation must accept the immutable action set")
        trim_damage_ids = {
            value
            for action in self.actions
            if action.kind is RescueActionKind.TRIM_DAMAGED_EDGES
            for values in (action.parameters.get("damage_ids"),)
            if isinstance(values, list)
            for value in values
            if isinstance(value, str)
        }
        if set(confirmation.accepted_trim_damage_ids) != trim_damage_ids:
            raise ValueError("confirmation must accept the immutable trim set")
        supports_improved = self.strategy is RescueStrategy.BALANCED and any(
            action.kind in REMAINING_IMPROVEMENT_ACTION_KINDS for action in self.actions
        )
        if confirmation.publish_improved is not supports_improved:
            raise ValueError("publish_improved must match the immutable action set")


def required_verification_check_ids_for_plan(
    plan: RescuePlan,
) -> tuple[str, ...]:
    """Derive the one canonical verification policy from confirmed actions."""
    kinds: set[RescueActionKind] = set()
    transition_stabilization = False
    for action in plan.actions:
        if action.kind is RescueActionKind.DEBLUR:
            kinds.add(action.kind)
        elif action.kind is RescueActionKind.DENOISE_AUDIO and action.parameters.get(
            "interference_profiles"
        ):
            kinds.add(action.kind)
        elif action.kind is RescueActionKind.STABILIZE and action.parameters.get(
            "method"
        ) in {"anchor_v1", "transition_anchor_v1"}:
            kinds.add(action.kind)
            transition_stabilization = (
                transition_stabilization
                or action.parameters.get("method") == "transition_anchor_v1"
            )
    required = list(RESCUE_REQUIRED_VERIFICATION_CHECK_IDS)
    if RescueActionKind.DEBLUR in kinds:
        required.extend(RESCUE_ACTION_VERIFICATION_CHECK_IDS[:3])
    if RescueActionKind.DENOISE_AUDIO in kinds:
        required.extend(RESCUE_ACTION_VERIFICATION_CHECK_IDS[3:5])
    if RescueActionKind.STABILIZE in kinds:
        required.append(RESCUE_ACTION_VERIFICATION_CHECK_IDS[5])
    if transition_stabilization:
        required.extend(RESCUE_ACTION_VERIFICATION_CHECK_IDS[6:9])
    return tuple(required)


class RescueConfirmation(RescueModel):
    """The user's explicit choices, validated against a specific Rescue plan."""

    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    publish_faithful: Literal[True]
    publish_improved: bool
    accepted_action_ids: tuple[str, ...] = ()
    accepted_trim_damage_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        if len(self.accepted_action_ids) != len(set(self.accepted_action_ids)):
            raise ValueError("duplicate accepted action ID")
        if len(self.accepted_trim_damage_ids) != len(
            set(self.accepted_trim_damage_ids)
        ):
            raise ValueError("duplicate accepted trim damage ID")
        return self


class RescueChangeLog(RescueModel):
    """The actual local actions and artifacts, with source immutability recorded."""

    schema_version: Literal["0.2"] = RESCUE_SCHEMA_VERSION
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    source_modified: Literal[False] = False
    processor: dict[str, JsonValue] = Field(default_factory=dict)
    actions: tuple[RescueAction, ...] = ()
    action_executions: tuple[RescueActionExecution, ...] = ()
    artifacts: tuple[RescueArtifact, ...] = ()

    @property
    def action_execution_state_known(self) -> bool:
        """Whether this record explicitly states its execution ledger."""
        return "action_executions" in self.model_fields_set


class RescueVerificationCheck(RescueModel):
    """One independently observable verification result without a quality score."""

    check_id: str = Field(min_length=1)
    artifact: Literal["faithful", "improved"] = "faithful"
    status: RescueVerificationStatus
    message: str = Field(min_length=1)
    measured: dict[str, JsonValue] = Field(default_factory=dict)
    required: bool = True


class RescueVerificationReport(RescueModel):
    """Independent verification for faithful and improved artifacts."""

    schema_version: Literal["0.2"] = RESCUE_SCHEMA_VERSION
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    faithful_status: RescueVerificationStatus
    improved_status: RescueVerificationStatus | None = None
    checks: tuple[RescueVerificationCheck, ...] = ()
    artifacts: tuple[RescueArtifact, ...] = ()
    outcome: RescueOutcome
    required_check_ids: tuple[str, ...] = RESCUE_REQUIRED_VERIFICATION_CHECK_IDS

    @model_validator(mode="after")
    def validate_statuses(self) -> Self:
        if self.required_check_ids[: len(RESCUE_REQUIRED_VERIFICATION_CHECK_IDS)] != (
            RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
        ):
            raise ValueError(
                "required rescue verification checks must match the canonical v0.1 "
                "verification policy"
            )
        action_check_ids = self.required_check_ids[
            len(RESCUE_REQUIRED_VERIFICATION_CHECK_IDS) :
        ]
        if (
            len(action_check_ids) != len(set(action_check_ids))
            or any(
                check_id not in RESCUE_ACTION_VERIFICATION_CHECK_IDS
                for check_id in action_check_ids
            )
            or action_check_ids
            != tuple(
                check_id
                for check_id in RESCUE_ACTION_VERIFICATION_CHECK_IDS
                if check_id in action_check_ids
            )
        ):
            raise ValueError(
                "required rescue verification checks must match the canonical action "
                "verification policy"
            )
        faithful_checks = tuple(
            check for check in self.checks if check.artifact == "faithful"
        )
        improved_checks = tuple(
            check for check in self.checks if check.artifact == "improved"
        )
        if self.checks != faithful_checks + improved_checks:
            raise ValueError("rescue verification checks must be grouped by artifact")
        artifact_paths = tuple(item.relative_path for item in self.artifacts)
        artifact_roles = tuple(item.artifact_role for item in self.artifacts)
        if len(artifact_paths) != len(set(artifact_paths)) or any(
            name not in {"faithful-rescue.mp4", "improved-viewing.mp4"}
            for name in artifact_paths
        ):
            raise ValueError("verification artifact bindings must be unique outputs")
        if (
            artifact_roles
            != tuple(
                role for role in ("faithful", "improved") if role in artifact_roles
            )
            or "document" in artifact_roles
        ):
            raise ValueError("verification artifact roles must use stable media order")
        if artifact_paths != tuple(
            name
            for name in ("faithful-rescue.mp4", "improved-viewing.mp4")
            if name in artifact_paths
        ):
            raise ValueError("verification artifact bindings must use stable order")
        _validate_required_checks(
            faithful_checks,
            self.required_check_ids,
            artifact="faithful",
        )
        if improved_checks:
            _validate_required_checks(
                improved_checks,
                self.required_check_ids,
                artifact="improved",
            )
        faithful_status = _artifact_verification_status(faithful_checks)
        improved_status = (
            _artifact_verification_status(improved_checks) if improved_checks else None
        )
        outcome = _verification_outcome(faithful_status, improved_status)
        object.__setattr__(self, "faithful_status", faithful_status)
        object.__setattr__(self, "improved_status", improved_status)
        object.__setattr__(self, "outcome", outcome)
        return self


class RescueTechnicalReport(RescueModel):
    """The public technical record for Rescue results and remaining limits."""

    schema_version: Literal["0.2"] = RESCUE_SCHEMA_VERSION
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    outcome: RescueOutcome
    damage_map: MediaDamageMap
    verification: RescueVerificationReport
    requested_symptoms: tuple[RescueSymptom, ...] = ()
    assessment_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    assessment_limitations: tuple[str, ...] = ()
    assessment_warnings: tuple[str, ...] = ()
    artifacts: tuple[RescueArtifact, ...] = ()
    action_executions: tuple[RescueActionExecution, ...] = ()
    limitations: tuple[str, ...] = ()
    manual_review_reasons: tuple[str, ...] = ()

    @property
    def action_execution_state_known(self) -> bool:
        """Whether this record explicitly states its execution ledger."""
        return "action_executions" in self.model_fields_set

    @model_validator(mode="after")
    def validate_digest_and_outcome(self) -> Self:
        if self.verification.plan_digest != self.plan_digest:
            raise ValueError("verification plan digest does not match technical report")
        review_override = (
            self.outcome in {RescueOutcome.PARTIAL, RescueOutcome.NEEDS_REVIEW}
            and self.verification.outcome is RescueOutcome.COMPLETED
            and bool(self.manual_review_reasons)
        )
        if self.outcome is not self.verification.outcome and not review_override:
            raise ValueError("technical report outcome does not match verification")
        _validate_public_json_value(self.model_dump(mode="json"))
        return self


def make_damage_id(
    input_hash: str,
    stream_id: str,
    kind: DamageKind,
    start_seconds: float,
    end_seconds: float,
) -> str:
    """Return the deterministic identity of an observable damage interval."""
    if not _is_sha256(input_hash):
        raise ValueError("input_hash must be a lowercase SHA-256 hex digest")
    if not stream_id:
        raise ValueError("stream_id must not be empty")
    start = _normalize_seconds(start_seconds)
    end = _normalize_seconds(end_seconds)
    if end < start:
        raise ValueError("end_seconds must not be before start_seconds")
    return "damage_" + _canonical_digest(
        {
            "end_seconds": end,
            "input_hash": input_hash,
            "kind": kind.value,
            "start_seconds": start,
            "stream_id": stream_id,
        }
    )


def make_rescue_plan_digest(plan_without_digest: Mapping[str, JsonValue]) -> str:
    """Hash an effective path-free Rescue plan with a stable schema identity."""
    payload = dict(plan_without_digest)
    payload.pop("plan_digest", None)
    payload.setdefault("schema_version", RESCUE_SCHEMA_VERSION)
    payload.setdefault("requested_symptoms", [])
    payload.setdefault("assessment_parameters", {})
    payload.setdefault("assessment_limitations", [])
    payload.setdefault("assessment_warnings", [])
    damage_intervals = payload.get("damage_intervals")
    if isinstance(damage_intervals, (list, tuple)):
        payload["damage_intervals"] = sorted(
            damage_intervals,
            key=_canonical_digest,
        )
    return _canonical_digest(payload)


def canonical_video_encode_contract(
    config: RescueEffectiveConfig,
) -> CanonicalVideoEncodeContract:
    """Project the effective config into the one stable action-level wire."""
    return CanonicalVideoEncodeContract(
        contract_version=config.video_encode_topology_version,
        encoder=config.video_encoder,
        preset=config.improved_video_preset,
        crf=config.improved_video_crf,
        pixel_format=config.improved_pixel_format,
        profile=config.video_profile,
        level=config.video_level,
        fps_mode=config.video_fps_mode,
        track_timescale=config.video_track_timescale,
        gop_size=config.video_gop_size,
        minimum_keyframe_interval=config.video_min_keyframe_interval,
        b_frames=config.video_b_frames,
        reference_frames=config.video_reference_frames,
        scene_change_threshold=config.video_scene_change_threshold,
    )


def make_rescue_action_id(
    *,
    kind: RescueActionKind,
    parameters: Mapping[str, JsonValue],
    source_ranges: tuple[tuple[float, float], ...],
    strategy: RescueStrategy,
    version: str,
) -> str:
    """Return the stable identity of an action and its executable parameters."""
    return "rescue_action_" + _canonical_digest(
        {
            "kind": kind.value,
            "parameters": parameters,
            "source_ranges": source_ranges,
            "strategy": strategy.value,
            "version": version,
        }
    )


def validate_plan_video_encode_contracts(
    plan: RescuePlan,
    *,
    allow_unqualified_sharpen_draft: bool = False,
    allow_unqualified_tonal_draft: bool = False,
) -> None:
    """Recheck action wires at preview/command/executor trust boundaries."""
    for action in plan.actions:
        _validate_action_video_encode_contract(action, plan.effective_config)
    from videoscope.rescue.qualification import (
        validate_plan_sharpen_qualification_contracts,
    )
    from videoscope.rescue.stabilization import (
        validate_plan_stabilization_qualification_contracts,
    )
    from videoscope.rescue.tonal import validate_plan_tonal_action_contracts
    from videoscope.rescue.visual import validate_plan_luma_action_contracts

    validate_plan_luma_action_contracts(plan)
    validate_plan_tonal_action_contracts(
        plan, allow_unqualified_draft=allow_unqualified_tonal_draft
    )
    validate_plan_sharpen_qualification_contracts(
        plan, allow_unqualified_draft=allow_unqualified_sharpen_draft
    )
    validate_plan_stabilization_qualification_contracts(plan)


def validate_rescue_plan_identity_contract(plan: RescuePlan) -> None:
    """Recompute the immutable plan digest at every media-writing boundary."""
    expected_digest = make_rescue_plan_digest(
        plan.model_dump(mode="json", exclude={"plan_digest"})
    )
    if plan.plan_digest != expected_digest:
        raise ValueError("plan digest differs from the effective RescuePlan")


def _validate_action_video_encode_contract(
    action: RescueAction,
    config: RescueEffectiveConfig,
) -> None:
    raw_contract = action.parameters.get("video_encode_contract")
    if not action.changes_content:
        if raw_contract is not None:
            raise ValueError("non-content action must not bind a video encode contract")
        return
    if raw_contract is None:
        raise ValueError("content action video encode contract is missing")
    try:
        observed = CanonicalVideoEncodeContract.model_validate(raw_contract)
    except ValueError as exc:
        raise ValueError("content action video encode contract is invalid") from exc
    if observed != canonical_video_encode_contract(config):
        raise ValueError(
            "content action video encode contract does not match effective config"
        )
    expected_id = make_rescue_action_id(
        kind=action.kind,
        parameters=action.parameters,
        source_ranges=action.source_ranges,
        strategy=action.strategy,
        version=action.version,
    )
    if action.id != expected_id:
        raise ValueError("content action ID does not match its video encode contract")


def _validate_relative_posix_path(value: str, *, field_name: str) -> None:
    path = PurePosixPath(value)
    if (
        not path.parts
        or path == PurePosixPath(".")
        or path.is_absolute()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or "\\" in value
        or value != path.as_posix()
    ):
        raise ValueError(f"{field_name} must be a normalized relative POSIX path")


def _validate_path_collection(values: tuple[str, ...], *, field_name: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {field_name}")
    for value in values:
        _validate_relative_posix_path(value, field_name=field_name)


def _validate_time_range(start: float, end: float, *, field_name: str) -> None:
    _normalize_seconds(start)
    _normalize_seconds(end)
    if end < start:
        raise ValueError(f"{field_name} end_seconds must not be before start_seconds")


def _damage_sort_key(interval: DamageInterval) -> tuple[float, float, str, str, str]:
    return (
        interval.start_seconds,
        interval.end_seconds,
        interval.kind.value,
        interval.stream_id,
        interval.id,
    )


def _verification_outcome(
    faithful_status: RescueVerificationStatus,
    improved_status: RescueVerificationStatus | None,
) -> RescueOutcome:
    if faithful_status is RescueVerificationStatus.FAILED:
        return RescueOutcome.FAILED
    if faithful_status is RescueVerificationStatus.NEEDS_REVIEW:
        return RescueOutcome.NEEDS_REVIEW
    if improved_status is RescueVerificationStatus.FAILED:
        return RescueOutcome.PARTIAL
    if improved_status is RescueVerificationStatus.NEEDS_REVIEW:
        return RescueOutcome.NEEDS_REVIEW
    return RescueOutcome.COMPLETED


def _validate_required_checks(
    checks: tuple[RescueVerificationCheck, ...],
    required_check_ids: tuple[str, ...],
    *,
    artifact: Literal["faithful", "improved"],
) -> None:
    """Require every configured check once and in stable artifact order."""
    required_checks = tuple(check for check in checks if check.required)
    optional_checks = tuple(check for check in checks if not check.required)
    check_ids = tuple(check.check_id for check in required_checks)
    if check_ids != required_check_ids:
        raise ValueError(
            f"required rescue verification checks for {artifact} must match "
            "the configured canonical order"
        )
    optional_ids = tuple(check.check_id for check in optional_checks)
    if optional_ids != tuple(sorted(optional_ids)) or len(optional_ids) != len(
        set(optional_ids)
    ):
        raise ValueError("optional rescue verification checks must use stable order")


def _artifact_verification_status(
    checks: tuple[RescueVerificationCheck, ...],
) -> RescueVerificationStatus:
    """Derive status from mandatory and applicable safety checks.

    Supplementary checks remain optional in the stable serialized policy, but
    an observed check which explicitly applies to the delivered artifact is a
    real safety gate.  Placeholders for an unselected action carry
    ``applicable=false`` and therefore cannot turn an otherwise verified
    artifact into ``needs_review``.
    """
    statuses = {
        check.status
        for check in checks
        if (
            check.required
            or check.status is RescueVerificationStatus.FAILED
            or (
                check.status is RescueVerificationStatus.NEEDS_REVIEW
                and check.check_id in _APPLICABLE_REVIEW_GATES
                and check.measured.get("applicable") is True
            )
        )
    }
    if RescueVerificationStatus.FAILED in statuses:
        return RescueVerificationStatus.FAILED
    if RescueVerificationStatus.NEEDS_REVIEW in statuses:
        return RescueVerificationStatus.NEEDS_REVIEW
    return RescueVerificationStatus.PASSED


def _validate_public_json_value(value: object) -> None:
    """Reject an absolute local path at every public JSON boundary."""
    if isinstance(value, str):
        if _contains_absolute_path(value):
            raise ValueError("public Rescue JSON must not contain an absolute path")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            _validate_public_json_value(key)
            _validate_public_json_value(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_public_json_value(item)


def _contains_absolute_path(value: str) -> bool:
    """Detect Windows, UNC, and Unix absolute-path forms inside public text."""
    return bool(
        _WINDOWS_ABSOLUTE_PATH_PATTERN.search(value)
        or _UNC_PATH_PATTERN.search(value)
        or _UNIX_ABSOLUTE_PATH_PATTERN.search(value)
    )


def _canonical_digest(payload: object) -> str:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(content.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    if len(value) != 64 or value != value.lower():
        return False
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def _is_simple_identifier(value: str) -> bool:
    return bool(value) and value.replace("_", "").isalnum()


def _normalize_seconds(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("seconds must be finite and non-negative")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("seconds must be finite and non-negative")
    return 0.0 if normalized == 0 else normalized


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value
    if isinstance(value, dict):
        return _FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value
