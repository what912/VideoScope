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

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

RESCUE_SCHEMA_VERSION: Final = "0.2"
_SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
_DAMAGE_ID_PATTERN: Final = r"^damage_[0-9a-f]{64}$"
RESCUE_REQUIRED_VERIFICATION_CHECK_IDS: Final = (
    "decodable",
    "duration",
    "streams",
    "source_read_only",
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


class RescueEffectiveConfig(RescueModel):
    """Path-free configuration bound into a confirmation digest."""

    planner_version: str = Field(default="1", min_length=1)
    source_read_only: Literal[True] = True
    max_preview_ranges: int = Field(default=3, ge=1, le=3)
    max_preview_total_seconds: float = Field(
        default=10.0, gt=0, le=10, allow_inf_nan=False
    )
    trim_guard_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    balanced_strength_limit: float = Field(default=1.0, gt=0, le=1, allow_inf_nan=False)
    locked_ranges: tuple[tuple[float, float], ...] = ()
    verification_policy: tuple[str, ...] = RESCUE_REQUIRED_VERIFICATION_CHECK_IDS

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
            action.kind in _BALANCED_ONLY_ACTIONS for action in self.actions
        )
        if confirmation.publish_improved is not supports_improved:
            raise ValueError("publish_improved must match the immutable action set")


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
        if self.required_check_ids != RESCUE_REQUIRED_VERIFICATION_CHECK_IDS:
            raise ValueError(
                "required rescue verification checks must match the canonical v0.1 "
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
    """Derive an artifact status from all of its mandatory check statuses."""
    statuses = {check.status for check in checks}
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
