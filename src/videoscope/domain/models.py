"""Pydantic domain models for VideoScope analysis reports."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from struct import pack
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

SCHEMA_VERSION = "0.1"
FINDING_ID_PREFIX = "finding_"
_SHA256_HEX_LENGTH = 64
_MISSING_FRAME = -1


class DomainModel(BaseModel):
    """Base configuration shared by all public report models."""

    model_config = ConfigDict(extra="forbid")


class Severity(StrEnum):
    """Finding severity from least to most severe."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class DetectorStatus(StrEnum):
    """Outcome of one detector execution."""

    OK = "ok"
    DETECTOR_ERROR = "detector_error"
    SKIPPED = "skipped"


class TimeRange(DomainModel):
    """Half-open time interval measured in seconds."""

    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        """Reject reversed time or frame intervals."""
        if self.end_seconds < self.start_seconds:
            raise ValueError(
                "end_seconds must be greater than or equal to start_seconds"
            )
        if (
            self.start_frame is not None
            and self.end_frame is not None
            and self.end_frame < self.start_frame
        ):
            raise ValueError("end_frame must be greater than or equal to start_frame")
        return self


class Evidence(DomainModel):
    """Evidence supporting one observable finding."""

    evidence_type: str = Field(min_length=1)
    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    relative_path: str | None = None
    description: str = Field(min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class Finding(DomainModel):
    """One detector observation in the unified report format."""

    id: str = Field(pattern=r"^finding_[0-9a-f]{64}$")
    detector_id: str = Field(min_length=1)
    detector_version: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    severity: Severity
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    time_range: TimeRange
    evidence: list[Evidence] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)

    def sort_key(self) -> tuple[float, int, str, str]:
        """Return the normative deterministic Finding sort key."""
        return (
            self.time_range.start_seconds,
            SEVERITY_ORDER[self.severity],
            self.detector_id,
            self.id,
        )


class VideoMetadata(DomainModel):
    """Normalized metadata for the analyzed local video."""

    filename: str = Field(min_length=1)
    container_format: str = Field(min_length=1)
    codec: str = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    average_frame_rate: float = Field(ge=0, allow_inf_nan=False)
    estimated_frame_count: int = Field(ge=0)
    has_audio: bool
    file_size_bytes: int = Field(ge=0)
    creation_time: datetime | None = None
    raw_probe: dict[str, JsonValue] = Field(default_factory=dict)


class DetectorExecution(DomainModel):
    """Recorded outcome and timing for one detector."""

    detector_id: str = Field(min_length=1)
    status: DetectorStatus
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    findings_count: int = Field(ge=0)
    error_type: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_error_fields(self) -> Self:
        """Require error details exactly when execution failed."""
        has_error_details = (
            self.error_type is not None or self.error_message is not None
        )
        if self.status is DetectorStatus.DETECTOR_ERROR:
            if not self.error_type or not self.error_message:
                raise ValueError(
                    "detector_error requires both error_type and error_message"
                )
        elif has_error_details:
            raise ValueError(
                "error_type and error_message are only valid for detector_error"
            )
        return self


class AnalysisReport(DomainModel):
    """Top-level versioned VideoScope analysis report."""

    schema_version: str = Field(default=SCHEMA_VERSION, pattern=r"^\d+\.\d+$")
    tool_version: str = Field(min_length=1)
    analysis_id: str = Field(min_length=1)
    created_at: datetime
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt: str | None = None
    metadata: VideoMetadata
    configuration: dict[str, JsonValue] = Field(default_factory=dict)
    detector_executions: list[DetectorExecution] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    runtime: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_and_validate_findings(self) -> Self:
        """Validate Finding IDs and store Findings in normative order."""
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must include a timezone")

        seen_ids: set[str] = set()
        for finding in self.findings:
            expected_id = make_finding_id(
                input_hash=self.input_hash,
                detector_id=finding.detector_id,
                time_range=finding.time_range,
            )
            if finding.id != expected_id:
                raise ValueError(
                    f"Finding {finding.id!r} does not match its deterministic ID"
                )
            if finding.id in seen_ids:
                raise ValueError(f"duplicate Finding ID: {finding.id}")
            seen_ids.add(finding.id)

        self.findings = sorted(self.findings, key=Finding.sort_key)
        return self


def make_finding_id(
    *,
    input_hash: str,
    detector_id: str,
    time_range: TimeRange,
) -> str:
    """Create a reproducible Finding ID from video, detector, and interval."""
    if len(input_hash) != _SHA256_HEX_LENGTH or input_hash != input_hash.lower():
        raise ValueError("input_hash must be a lowercase SHA-256 hex digest")
    try:
        input_digest = bytes.fromhex(input_hash)
    except ValueError as exc:
        raise ValueError("input_hash must be a lowercase SHA-256 hex digest") from exc
    if len(input_digest) != _SHA256_HEX_LENGTH // 2:
        raise ValueError("input_hash must be a lowercase SHA-256 hex digest")
    if not detector_id:
        raise ValueError("detector_id must not be empty")

    start_frame = (
        time_range.start_frame if time_range.start_frame is not None else _MISSING_FRAME
    )
    end_frame = (
        time_range.end_frame if time_range.end_frame is not None else _MISSING_FRAME
    )
    payload = b"\x00".join(
        (
            input_digest,
            detector_id.encode("utf-8"),
            pack(
                "!ddqq",
                time_range.start_seconds,
                time_range.end_seconds,
                start_frame,
                end_frame,
            ),
        )
    )
    return f"{FINDING_ID_PREFIX}{sha256(payload).hexdigest()}"
