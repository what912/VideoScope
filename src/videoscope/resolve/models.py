"""Strict, versioned domain contracts for Publish Ready."""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from videoscope.domain import VideoMetadata

RESOLVE_SCHEMA_VERSION = "0.3"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TASK_ID_PATTERN = r"^[0-9a-f]{32}$"
PUBLISH_PREVIEW_ARTIFACT = "preview/publish-preview.mp4"
EXPECTED_PUBLISH_ARTIFACTS = (
    "plan.json",
    PUBLISH_PREVIEW_ARTIFACT,
    "publish-ready.mp4",
    "cover.jpg",
    "changes.json",
    "technical-report.json",
    "analysis-before/report.json",
    "analysis-after/report.json",
)


class ResolveModel(BaseModel):
    """Base model that rejects unknown public Resolve JSON fields."""

    model_config = ConfigDict(extra="forbid")


class PublishProfileId(StrEnum):
    """The only Publish Ready profile identifiers supported by this MVP."""

    COMPATIBLE_MP4 = "compatible_mp4"
    SOCIAL_VERTICAL = "social_vertical_9_16"
    SOCIAL_HORIZONTAL = "social_horizontal_16_9"


class PublishBackend(StrEnum):
    """The supported local processing backend."""

    NATIVE_LOCAL = "native_local"


class PublishActionKind(StrEnum):
    """Safe processing actions that can be placed in a PublishPlan."""

    REMUX = "remux"
    TRANSCODE = "transcode"
    SCALE_PAD = "scale_pad"
    STRIP_METADATA = "strip_metadata"
    FASTSTART = "faststart"
    EXTRACT_COVER = "extract_cover"


class VerificationStatus(StrEnum):
    """The result of a versioned verification check or report."""

    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


_ACTION_ORDER: dict[PublishActionKind, int] = {
    PublishActionKind.REMUX: 0,
    PublishActionKind.TRANSCODE: 1,
    PublishActionKind.SCALE_PAD: 2,
    PublishActionKind.STRIP_METADATA: 3,
    PublishActionKind.FASTSTART: 4,
    PublishActionKind.EXTRACT_COVER: 5,
}


class PublishAction(ResolveModel):
    """One safe, inspectable operation requested by a PublishPlan."""

    action_id: str = Field(min_length=1)
    kind: PublishActionKind
    description: str = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    affects: tuple[str, ...] = Field(min_length=1)
    changes_content_semantics: bool
    confirmation_required: bool

    @model_validator(mode="after")
    def reject_content_semantic_change(self) -> Self:
        """Keep the MVP limited to transformations preserving source semantics."""
        if self.changes_content_semantics:
            raise ValueError("Publish Ready actions cannot change content semantics")
        return self


class PublishArtifact(ResolveModel):
    """A publicly reportable output-root-relative artifact."""

    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        """Reject paths that are absolute, non-POSIX, or escape the output root."""
        path = PurePosixPath(self.relative_path)
        if (
            path.is_absolute()
            or PureWindowsPath(self.relative_path).drive
            or ".." in path.parts
            or "\\" in self.relative_path
            or self.relative_path != path.as_posix()
        ):
            raise ValueError("artifact path must be a normalized relative POSIX path")
        return self


class PublishEffectiveConfig(ResolveModel):
    """Path-free task configuration bound into plan confirmation."""

    preview_seconds: float = Field(gt=0, le=10, allow_inf_nan=False)
    keep_workspace: bool
    run_diagnostics: bool


class PublishPlan(ResolveModel):
    """The complete, confirmable plan before any final output is written."""

    schema_version: str = Field(default=RESOLVE_SCHEMA_VERSION, pattern=r"^\d+\.\d+$")
    task_id: str = Field(pattern=_TASK_ID_PATTERN)
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    source_metadata: VideoMetadata
    source_read_only: Literal[True]
    profile_id: PublishProfileId
    profile_version: str = Field(min_length=1)
    backend: PublishBackend
    actions: tuple[PublishAction, ...] = Field(min_length=1)
    preview_artifact: str
    confirmation_required: Literal[True]
    expected_artifacts: tuple[str, ...] = Field(min_length=1)
    effective_config: PublishEffectiveConfig
    output_filename: str = Field(min_length=1)
    plan_digest: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("output_filename")
    @classmethod
    def validate_output_filename(cls, value: str) -> str:
        """Keep the final public output artifact inside the output root."""
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or PureWindowsPath(value).drive
            or ".." in path.parts
            or "\\" in value
            or value != path.as_posix()
            or len(path.parts) != 1
        ):
            raise ValueError("artifact path must be a normalized relative POSIX path")
        return value

    @model_validator(mode="after")
    def validate_actions_and_digest(self) -> Self:
        """Require one stable action order and a digest of the effective plan."""
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("duplicate action_id in PublishPlan")

        action_order = [_ACTION_ORDER[action.kind] for action in self.actions]
        if action_order != sorted(action_order):
            raise ValueError("PublishPlan actions must use the stable action order")

        if self.preview_artifact != PUBLISH_PREVIEW_ARTIFACT:
            raise ValueError("PublishPlan preview artifact does not match the contract")
        if self.expected_artifacts != EXPECTED_PUBLISH_ARTIFACTS:
            raise ValueError("PublishPlan expected artifacts do not match the contract")
        if self.output_filename not in self.expected_artifacts:
            raise ValueError("PublishPlan output is not listed as an expected artifact")

        expected_digest = make_publish_plan_digest(
            schema_version=self.schema_version,
            task_id=self.task_id,
            input_hash=self.input_hash,
            source_read_only=self.source_read_only,
            profile_id=self.profile_id,
            profile_version=self.profile_version,
            backend=self.backend,
            actions=self.actions,
            preview_artifact=self.preview_artifact,
            confirmation_required=self.confirmation_required,
            expected_artifacts=self.expected_artifacts,
            effective_config=self.effective_config,
            output_filename=self.output_filename,
        )
        if self.plan_digest != expected_digest:
            raise ValueError("plan_digest does not match the effective PublishPlan")
        return self


class VerificationCheck(ResolveModel):
    """One observable technical or detector-regression verification result."""

    check_id: str = Field(min_length=1)
    status: VerificationStatus
    message: str = Field(min_length=1)
    measured: dict[str, JsonValue] = Field(default_factory=dict)


class VerificationReport(ResolveModel):
    """Versioned, profile-specific verification without a global quality score."""

    schema_version: str = Field(default=RESOLVE_SCHEMA_VERSION, pattern=r"^\d+\.\d+$")
    profile_id: PublishProfileId
    profile_version: str = Field(min_length=1)
    status: VerificationStatus
    checks: tuple[VerificationCheck, ...] = Field(min_length=1)
    manual_review_reasons: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def reject_duplicate_check_ids(self) -> Self:
        """Keep each report check independently addressable."""
        check_ids = [check.check_id for check in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("duplicate check_id in VerificationReport")
        expected_status = _verification_status_for(self.checks)
        if self.status is not expected_status:
            raise ValueError(
                "VerificationReport status must be "
                f"{expected_status.value} for its checks"
            )
        return self


class PublishChangeLog(ResolveModel):
    """The actions actually executed and the artifacts they produced."""

    schema_version: str = Field(default=RESOLVE_SCHEMA_VERSION, pattern=r"^\d+\.\d+$")
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    actions: tuple[PublishAction, ...] = Field(min_length=1)
    artifacts: tuple[PublishArtifact, ...] = Field(default_factory=tuple)


class PublishTechnicalReport(ResolveModel):
    """The public technical report that carries verification and artifacts."""

    schema_version: str = Field(default=RESOLVE_SCHEMA_VERSION, pattern=r"^\d+\.\d+$")
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    verification: VerificationReport
    artifacts: tuple[PublishArtifact, ...] = Field(default_factory=tuple)


def make_publish_plan_digest(
    *,
    schema_version: str = RESOLVE_SCHEMA_VERSION,
    task_id: str,
    input_hash: str,
    source_read_only: bool,
    profile_id: PublishProfileId,
    profile_version: str,
    backend: PublishBackend,
    actions: tuple[PublishAction, ...],
    preview_artifact: str,
    confirmation_required: bool,
    expected_artifacts: tuple[str, ...],
    effective_config: PublishEffectiveConfig,
    output_filename: str,
) -> str:
    """Return the stable SHA-256 confirmation digest for a PublishPlan."""
    if not _is_sha256(input_hash):
        raise ValueError("input_hash must be a lowercase SHA-256 hex digest")
    payload = {
        "actions": [action.model_dump(mode="json") for action in actions],
        "backend": backend.value,
        "confirmation_required": confirmation_required,
        "effective_config": effective_config.model_dump(mode="json"),
        "expected_artifacts": list(expected_artifacts),
        "input_hash": input_hash,
        "output_filename": output_filename,
        "preview_artifact": preview_artifact,
        "profile_id": profile_id.value,
        "profile_version": profile_version,
        "schema_version": schema_version,
        "source_read_only": source_read_only,
        "task_id": task_id,
    }
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(content.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    """Return whether a value is exactly a lowercase SHA-256 hex digest."""
    if len(value) != 64 or value != value.lower():
        return False
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def _verification_status_for(
    checks: tuple[VerificationCheck, ...],
) -> VerificationStatus:
    """Derive the only valid aggregate status from check-result precedence."""
    statuses = {check.status for check in checks}
    if VerificationStatus.FAILED in statuses:
        return VerificationStatus.FAILED
    if VerificationStatus.NEEDS_REVIEW in statuses:
        return VerificationStatus.NEEDS_REVIEW
    return VerificationStatus.PASSED
