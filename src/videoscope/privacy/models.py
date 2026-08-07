"""Strict, versioned domain contracts for Safe Sharing."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final, Literal, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
    model_validator,
)

from videoscope.domain import Severity

PRIVACY_SCHEMA_VERSION: Final = "0.1"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS: Final = (
    "decodable",
    "duration",
    "streams",
    "profile",
    "metadata",
    "visual_coverage",
    "qr_redaction",
    "text_redaction",
    "audio_mute",
    "black_regression",
    "freeze_regression",
    "public_artifact_privacy",
)
_DEFAULT_EXPECTED_ARTIFACTS: Final = (
    "share-safe.mp4",
    "privacy-summary.json",
    "changes.json",
    "verification.json",
    "technical-report.json",
)
_DEFAULT_VERIFICATION_POLICY: Final = PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS


class _FrozenDict(dict[str, Any]):
    """A dict-compatible JSON object that rejects every in-place mutation."""

    def __setitem__(self, key: str, value: Any) -> None:
        raise TypeError("frozen JSON object does not support item assignment")

    def __delitem__(self, key: str) -> None:
        raise TypeError("frozen JSON object does not support item deletion")

    def clear(self) -> None:
        raise TypeError("frozen JSON object cannot be cleared")

    def pop(self, key: str, default: Any = None) -> Any:
        raise TypeError("frozen JSON object cannot remove items")

    def popitem(self) -> tuple[str, Any]:
        raise TypeError("frozen JSON object cannot remove items")

    def setdefault(self, key: str, default: Any = None) -> Any:
        raise TypeError("frozen JSON object cannot add defaults")

    def update(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("frozen JSON object cannot be updated")

    def __ior__(self, value: Any, /) -> Self:  # type: ignore[override,misc]
        raise TypeError("frozen JSON object cannot be updated")


class PrivacyModel(BaseModel):
    """Base class that rejects undeclared Safe Sharing JSON fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def freeze_nested_containers(self) -> Self:
        """Detach and recursively freeze every mutable public field value."""
        for field_name in type(self).model_fields:
            object.__setattr__(
                self,
                field_name,
                _deep_freeze(getattr(self, field_name)),
            )
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """Return a fully revalidated immutable copy, including caller updates."""
        del deep
        values = self.model_dump(mode="python")
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


class PrivacyRiskType(StrEnum):
    METADATA = "metadata"
    FACE_REGION = "face_region"
    QR_CODE = "qr_code"
    BARCODE = "barcode"
    SUSPICIOUS_TEXT = "suspicious_text"
    MANUAL_VISUAL = "manual_visual"
    MANUAL_AUDIO = "manual_audio"


class PrivacyDecision(StrEnum):
    UNREVIEWED = "unreviewed"
    ALLOW = "allow"
    REDACT = "redact"


class RedactionStyle(StrEnum):
    BLUR = "blur"
    PIXELATE = "pixelate"
    SOLID_FILL = "solid_fill"
    CROP = "crop"
    MUTE = "mute"
    REMOVE_METADATA = "remove_metadata"


class PrivacyActionKind(StrEnum):
    REMOVE_METADATA = "remove_metadata"
    CROP = "crop"
    VISUAL_REDACTION = "visual_redaction"
    AUDIO_MUTE = "audio_mute"
    REMUX = "remux"
    VERIFY = "verify"


class PrivacyJobOutcome(StrEnum):
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    PARTIAL = "partial"
    FAILED = "failed"


class VerificationStatus(StrEnum):
    """The result of one independent Safe Sharing verification check."""

    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class NormalizedBox(PrivacyModel):
    """A positive-area rectangle expressed as fractions of frame dimensions."""

    x_min: float = Field(ge=0, le=1, allow_inf_nan=False)
    y_min: float = Field(ge=0, le=1, allow_inf_nan=False)
    x_max: float = Field(ge=0, le=1, allow_inf_nan=False)
    y_max: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_positive_area(self) -> Self:
        """Reject inverted and zero-area rectangles."""
        if self.x_min >= self.x_max or self.y_min >= self.y_max:
            raise ValueError("normalized box must have positive area")
        return self


class PrivacyRisk(PrivacyModel):
    """One reviewable privacy observation without an identity claim."""

    id: str = Field(pattern=r"^privacy_risk_[0-9a-f]{64}$")
    scanner_id: str = Field(min_length=1)
    scanner_version: str = Field(min_length=1)
    risk_type: PrivacyRiskType
    title: str = Field(min_length=1)
    public_description: str = Field(min_length=1)
    severity: Severity
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    box: NormalizedBox | None = None
    track_id: str | None = None
    metadata_scope: str | None = None
    metadata_key: str | None = None
    recommended_style: RedactionStyle | None = None
    decision: PrivacyDecision = PrivacyDecision.UNREVIEWED
    style: RedactionStyle | None = None
    limitations: tuple[str, ...] = ()
    evidence: tuple[dict[str, JsonValue], ...] = ()
    private_evidence: tuple[dict[str, JsonValue], ...] = ()

    @field_serializer("evidence", "private_evidence")
    def serialize_evidence(
        self,
        value: tuple[dict[str, JsonValue], ...],
    ) -> list[dict[str, JsonValue]]:
        """Expose frozen evidence through ordinary JSON arrays and objects."""
        return cast(list[dict[str, JsonValue]], _deep_thaw(value))

    @model_validator(mode="after")
    def validate_interval_and_decision(self) -> Self:
        """Keep each decision actionable without retaining contradictory styles."""
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must not be before start_seconds")
        if self.decision is PrivacyDecision.REDACT and self.style is None:
            raise ValueError("REDACT decision requires a redaction style")
        if self.decision is PrivacyDecision.ALLOW and self.style is not None:
            raise ValueError("ALLOW decision forbids a redaction style")
        if self.style is not None and not _style_applies_to_risk(
            self.style, self.risk_type
        ):
            raise ValueError("redaction style is not applicable to this risk type")
        return self


class PrivacyRiskMap(PrivacyModel):
    """The private review document; public summaries omit private evidence."""

    schema_version: Literal["0.1"] = PRIVACY_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    profile: str = Field(min_length=1)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    risks: tuple[PrivacyRisk, ...] = ()
    is_private: bool = True

    @model_validator(mode="after")
    def validate_risks_and_sort(self) -> Self:
        """Validate identity, privacy boundary, crop constraints, and ordering."""
        identifiers: set[str] = set()
        for risk in self.risks:
            expected_id = make_privacy_risk_id(
                self.input_hash,
                risk.scanner_id,
                risk.risk_type,
                risk.start_seconds,
                risk.end_seconds,
                risk.box,
            )
            if risk.id != expected_id:
                raise ValueError("privacy risk ID does not match the observed risk")
            if risk.id in identifiers:
                raise ValueError("duplicate privacy risk ID")
            identifiers.add(risk.id)
            if risk.end_seconds > self.duration_seconds:
                raise ValueError("privacy risk interval exceeds media duration")
            if not self.is_private and risk.private_evidence:
                raise ValueError(
                    "public privacy risk map cannot contain private evidence"
                )
            if risk.style is RedactionStyle.CROP and (
                risk.box is None
                or risk.track_id is not None
                or risk.start_seconds != 0
                or risk.end_seconds != self.duration_seconds
            ):
                raise ValueError("CROP requires one static full-duration box")
        object.__setattr__(
            self,
            "risks",
            tuple(sorted(self.risks, key=privacy_risk_sort_key)),
        )
        return self

    def public_summary(self) -> Self:
        """Return a public-safe copy without any private review evidence."""
        return type(self)(
            schema_version=self.schema_version,
            input_hash=self.input_hash,
            profile=self.profile,
            duration_seconds=self.duration_seconds,
            risks=tuple(
                risk.model_copy(update={"private_evidence": ()}) for risk in self.risks
            ),
            is_private=False,
        )


class PrivacyReviewDecision(PrivacyModel):
    """An auditable human review whose timestamp is outside plan identity."""

    risk_id: str = Field(pattern=r"^privacy_risk_[0-9a-f]{64}$")
    decision: PrivacyDecision
    style: RedactionStyle | None = None
    edited_box: NormalizedBox | None = None
    reviewed_at: datetime

    @field_validator("reviewed_at")
    @classmethod
    def validate_reviewed_at(cls, value: datetime) -> datetime:
        """Require an aware timestamp while preserving its audit-only role."""
        if value.tzinfo is None:
            raise ValueError("reviewed_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_decision_style(self) -> Self:
        """Prevent contradictory human decisions at the review boundary."""
        if self.decision is PrivacyDecision.REDACT and self.style is None:
            raise ValueError("REDACT decision requires a redaction style")
        if self.decision is PrivacyDecision.ALLOW and self.style is not None:
            raise ValueError("ALLOW decision forbids a redaction style")
        return self


class PrivacyEffectiveConfig(PrivacyModel):
    """Path-free effective configuration covered by plan confirmation."""

    preview_seconds: float = Field(default=5.0, gt=0, allow_inf_nan=False)
    guard_pixels: int = Field(default=0, ge=0)
    blur_kernel_size: int = Field(default=21, ge=3, le=255)
    pixelate_block_size: int = Field(default=12, ge=2, le=512)
    solid_fill_color: tuple[int, int, int] = (0, 0, 0)
    interpolation_guard_ratio: float = Field(
        default=0.05,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    expand_track_gaps: bool = True
    profile_version: str = Field(default="1", pattern=r"^\d+$")
    qr_handling: Literal["review", "redact_by_default"] = "review"
    default_visual_style: RedactionStyle = RedactionStyle.BLUR
    preview_identity: str = "preview/privacy-preview.mp4"
    expected_artifacts: tuple[str, ...] = _DEFAULT_EXPECTED_ARTIFACTS
    source_read_only: Literal[True] = True
    verification_policy: tuple[str, ...] = _DEFAULT_VERIFICATION_POLICY

    @field_validator("blur_kernel_size")
    @classmethod
    def validate_blur_kernel_size(cls, value: int) -> int:
        """OpenCV Gaussian blur requires an odd positive kernel."""
        if value % 2 == 0:
            raise ValueError("blur kernel size must be odd")
        return value

    @field_validator("solid_fill_color")
    @classmethod
    def validate_solid_fill_color(
        cls,
        value: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        """Keep the digest-bound BGR fill color within byte range."""
        if any(channel < 0 or channel > 255 for channel in value):
            raise ValueError("solid fill color channels must be bytes")
        return value

    @model_validator(mode="after")
    def validate_confirmation_contract(self) -> Self:
        """Keep every digest-bound identity normalized and unambiguous."""
        if self.default_visual_style not in {
            RedactionStyle.BLUR,
            RedactionStyle.PIXELATE,
            RedactionStyle.SOLID_FILL,
        }:
            raise ValueError("default visual style must be a region redaction style")
        _validate_relative_posix_path(
            self.preview_identity, field_name="preview identity"
        )
        if len(self.expected_artifacts) != len(set(self.expected_artifacts)):
            raise ValueError("duplicate expected privacy artifact")
        for path in self.expected_artifacts:
            _validate_relative_posix_path(path, field_name="expected artifact")
        if not self.verification_policy:
            raise ValueError("verification policy must not be empty")
        if len(self.verification_policy) != len(set(self.verification_policy)):
            raise ValueError("duplicate verification policy check")
        if any(
            not check_id or not check_id.replace("_", "").isalnum()
            for check_id in self.verification_policy
        ):
            raise ValueError("verification policy check IDs must be simple identifiers")
        return self


class PrivacyAction(PrivacyModel):
    """One deterministic privacy action that can be included in a plan."""

    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    kind: PrivacyActionKind
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    box: NormalizedBox | None = None
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    changes_semantics: bool
    requires_confirmation: bool

    @field_serializer("parameters")
    def serialize_parameters(
        self,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Expose frozen action parameters through ordinary JSON objects."""
        return cast(dict[str, JsonValue], _deep_thaw(value))

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """Actions cannot target negative or inverted media intervals."""
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must not be before start_seconds")
        if self.changes_semantics and not self.requires_confirmation:
            raise ValueError("content-changing privacy action requires confirmation")
        return self


class PrivacyArtifact(PrivacyModel):
    """A public, share-package-relative artifact reference."""

    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=_SHA256_PATTERN)
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relative_path(self) -> Self:
        """Reject absolute, escaped, or platform-specific public paths."""
        _validate_relative_posix_path(self.relative_path, field_name="artifact path")
        return self


class PrivacyPlan(PrivacyModel):
    """The immutable, confirmation-bound plan for Safe Sharing execution."""

    schema_version: Literal["0.1"] = PRIVACY_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    profile: str = Field(min_length=1)
    duration_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    effective_config: PrivacyEffectiveConfig
    risks: tuple[PrivacyRisk, ...]
    actions: tuple[PrivacyAction, ...]
    artifacts: tuple[PrivacyArtifact, ...]
    digest: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        """Reject a plan whose confirmation digest misses any effective content."""
        for risk in self.risks:
            if risk.private_evidence:
                raise ValueError("privacy plan cannot contain private evidence")
            if (
                self.duration_seconds is not None
                and risk.end_seconds > self.duration_seconds
            ):
                raise ValueError("privacy risk interval exceeds source duration")
            if risk.style is RedactionStyle.CROP and (
                self.duration_seconds is None
                or risk.box is None
                or risk.track_id is not None
                or risk.start_seconds != 0
                or risk.end_seconds != self.duration_seconds
            ):
                raise ValueError("CROP risk requires one static full-duration box")
        crop_risks = tuple(
            risk for risk in self.risks if risk.style is RedactionStyle.CROP
        )
        for action in self.actions:
            if action.kind is PrivacyActionKind.CROP and not any(
                risk.start_seconds == action.start_seconds
                and risk.end_seconds == action.end_seconds
                and risk.box == action.box
                for risk in crop_risks
            ):
                raise ValueError(
                    "CROP action requires a matching static full-duration risk"
                )
        expected = make_privacy_plan_digest(
            self.input_hash,
            self.profile,
            self.effective_config,
            self.risks,
            self.actions,
            self.artifacts,
            duration_seconds=self.duration_seconds,
        )
        if self.digest != expected:
            raise ValueError("privacy plan digest does not match the effective plan")
        return self


class PrivacyChangeLog(PrivacyModel):
    """The executed privacy actions and any public artifacts they produced."""

    schema_version: Literal["0.1"] = PRIVACY_SCHEMA_VERSION
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    source_modified: Literal[False] = False
    processor: dict[str, JsonValue] = Field(default_factory=dict)
    actions: tuple[PrivacyAction, ...] = ()
    artifacts: tuple[PrivacyArtifact, ...] = ()

    @field_serializer("processor")
    def serialize_processor(
        self,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Expose frozen processor data through an ordinary JSON object."""
        return cast(dict[str, JsonValue], _deep_thaw(value))


class PrivacyVerificationCheck(PrivacyModel):
    """One independently visible Safe Sharing verification result."""

    check_id: str = Field(min_length=1)
    status: VerificationStatus
    message: str = Field(min_length=1)
    measured: dict[str, JsonValue] = Field(default_factory=dict)
    required: bool = True

    @field_serializer("measured")
    def serialize_measured(
        self,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        """Expose frozen measurements through an ordinary JSON object."""
        return cast(dict[str, JsonValue], _deep_thaw(value))


class PrivacyVerificationReport(PrivacyModel):
    """A conservative aggregate of independently visible verification checks."""

    schema_version: Literal["0.1"] = PRIVACY_SCHEMA_VERSION
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    status: PrivacyJobOutcome
    checks: tuple[PrivacyVerificationCheck, ...]

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        """Derive the report outcome from its required checks."""
        required_count = len(PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS)
        required = self.checks[:required_count]
        optional = self.checks[required_count:]
        required_ids = tuple(check.check_id for check in required)
        if required_ids != PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS:
            raise ValueError(
                "privacy verification checks must match the required check "
                "contract exactly once and in canonical order"
            )
        if any(not check.required for check in required):
            raise ValueError("every required privacy verification check is required")
        optional_ids = tuple(check.check_id for check in optional)
        if len(optional_ids) != len(set(optional_ids)) or any(
            check.required
            or not check.check_id.startswith("scanner_issue:")
            or not _is_scanner_issue_id(check.check_id.removeprefix("scanner_issue:"))
            for check in optional
        ):
            raise ValueError(
                "optional verification checks must be unique non-required "
                "scanner_issue IDs"
            )
        expected = _verification_outcome(self.checks)
        if self.status is not expected:
            raise ValueError(f"verification status must be {expected.value}")
        return self


class PrivacyTechnicalReport(PrivacyModel):
    """The public technical record for a verified or review-required share copy."""

    schema_version: Literal["0.1"] = PRIVACY_SCHEMA_VERSION
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    verification: PrivacyVerificationReport
    artifacts: tuple[PrivacyArtifact, ...] = ()

    @model_validator(mode="after")
    def validate_verification_digest(self) -> Self:
        """Bind the public verification record to this exact confirmed plan."""
        if self.verification.plan_digest != self.plan_digest:
            raise ValueError("verification plan digest does not match technical report")
        return self


def make_privacy_risk_id(
    input_hash: str,
    scanner_id: str,
    risk_type: PrivacyRiskType,
    start_seconds: float,
    end_seconds: float,
    box: NormalizedBox | None,
) -> str:
    """Return a deterministic risk ID from the observable risk identity fields."""
    if not _is_sha256(input_hash):
        raise ValueError("input_hash must be a lowercase SHA-256 hex digest")
    if not scanner_id:
        raise ValueError("scanner_id must not be empty")
    normalized_start = _normalize_seconds(start_seconds)
    normalized_end = _normalize_seconds(end_seconds)
    if normalized_end < normalized_start:
        raise ValueError("end_seconds must not be before start_seconds")
    payload = {
        "box": box.model_dump(mode="json") if box is not None else None,
        "end_seconds": normalized_end,
        "input_hash": input_hash,
        "risk_type": risk_type.value,
        "scanner_id": scanner_id,
        "start_seconds": normalized_start,
    }
    return f"privacy_risk_{_canonical_digest(payload)}"


def make_privacy_plan_digest(
    input_hash: str,
    profile: str,
    effective_config: PrivacyEffectiveConfig,
    risks: tuple[PrivacyRisk, ...],
    actions: tuple[PrivacyAction, ...],
    artifacts: tuple[PrivacyArtifact, ...],
    *,
    duration_seconds: float | None = None,
) -> str:
    """Return the stable confirmation digest for all effective plan inputs."""
    if not _is_sha256(input_hash):
        raise ValueError("input_hash must be a lowercase SHA-256 hex digest")
    if not profile:
        raise ValueError("profile must not be empty")
    if duration_seconds is not None:
        duration_seconds = _normalize_seconds(duration_seconds)
    payload = {
        "actions": [action.model_dump(mode="json") for action in actions],
        "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        "effective_config": effective_config.model_dump(mode="json"),
        "duration_seconds": duration_seconds,
        "input_hash": input_hash,
        "profile": profile,
        "risks": [
            {
                key: value
                for key, value in risk.model_dump(mode="json").items()
                if key != "private_evidence"
            }
            for risk in risks
        ],
        "schema_version": PRIVACY_SCHEMA_VERSION,
    }
    return _canonical_digest(payload)


def _validate_relative_posix_path(value: str, *, field_name: str) -> None:
    """Reject absolute, escaped, current-directory, and platform-specific paths."""
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


def _style_applies_to_risk(style: RedactionStyle, risk_type: PrivacyRiskType) -> bool:
    """Return whether a style fits the observable medium of a risk."""
    if risk_type is PrivacyRiskType.METADATA:
        return style is RedactionStyle.REMOVE_METADATA
    if risk_type is PrivacyRiskType.MANUAL_AUDIO:
        return style is RedactionStyle.MUTE
    return style in {
        RedactionStyle.BLUR,
        RedactionStyle.PIXELATE,
        RedactionStyle.SOLID_FILL,
        RedactionStyle.CROP,
    }


def privacy_risk_sort_key(risk: PrivacyRisk) -> tuple[float, int, str, str]:
    """Return the canonical published risk ordering key."""
    severity_order = {
        Severity.INFO: 0,
        Severity.LOW: 1,
        Severity.MEDIUM: 2,
        Severity.HIGH: 3,
        Severity.CRITICAL: 4,
    }
    return (risk.start_seconds, severity_order[risk.severity], risk.scanner_id, risk.id)


def _verification_outcome(
    checks: tuple[PrivacyVerificationCheck, ...],
) -> PrivacyJobOutcome:
    """Derive Safe Sharing status conservatively from required verification checks."""
    required = tuple(check for check in checks if check.required)
    required_statuses = {check.status for check in required}
    if VerificationStatus.FAILED in required_statuses:
        return PrivacyJobOutcome.FAILED
    if VerificationStatus.NEEDS_REVIEW in required_statuses:
        return PrivacyJobOutcome.NEEDS_REVIEW
    if any(
        check.status is not VerificationStatus.PASSED
        for check in checks
        if not check.required
    ):
        return PrivacyJobOutcome.PARTIAL
    return PrivacyJobOutcome.COMPLETED


def _canonical_digest(payload: object) -> str:
    """Hash one deterministic UTF-8 JSON value without allowing non-finite numbers."""
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(content.encode("utf-8")).hexdigest()


def _is_sha256(value: str) -> bool:
    """Return whether a value is exactly one lowercase SHA-256 hexadecimal digest."""
    if len(value) != 64 or value != value.lower():
        return False
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def _is_scanner_issue_id(value: str) -> bool:
    return (
        bool(value)
        and value[0].isalpha()
        and value[0].isascii()
        and all(
            character.isascii()
            and (character.islower() or character.isdigit() or character in "_.-")
            for character in value
        )
    )


def _normalize_seconds(value: float) -> float:
    """Return one canonical finite, non-negative float representation."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("risk timestamps must be finite and non-negative")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("risk timestamps must be finite and non-negative")
    if normalized == 0:
        return 0.0
    return normalized


def _deep_freeze(value: Any) -> Any:
    """Recursively detach mutable JSON containers from a validated model."""
    if isinstance(value, BaseModel):
        return value
    if isinstance(value, dict):
        return _FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    """Return canonical mutable JSON containers only for serialization."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        thawed = [_deep_thaw(item) for item in value]
        return sorted(
            thawed,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            ),
        )
    return value
