"""Validated models for deterministic benchmark output."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from videoscope.analysis import AnalysisConfig

BENCHMARK_SCHEMA_VERSION = "0.1"


class BenchmarkModel(BaseModel):
    """Strict base model for benchmark artifacts."""

    model_config = ConfigDict(extra="forbid")


class BenchmarkInterval(BenchmarkModel):
    """One half-open annotated or predicted time interval."""

    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must be >= start_seconds")
        return self

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


class EventMatch(BenchmarkModel):
    """One deterministic one-to-one prediction/annotation match."""

    prediction_index: int = Field(ge=0)
    annotation_index: int = Field(ge=0)
    temporal_iou: float = Field(ge=0, le=1, allow_inf_nan=False)
    start_time_error_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_time_error_seconds: float = Field(ge=0, allow_inf_nan=False)


class EventMetrics(BenchmarkModel):
    """Event and boundary metrics for one case or aggregate."""

    true_positive_events: int = Field(ge=0)
    false_positive_events: int = Field(ge=0)
    false_negative_events: int = Field(ge=0)
    event_precision: float = Field(ge=0, le=1, allow_inf_nan=False)
    event_recall: float = Field(ge=0, le=1, allow_inf_nan=False)
    event_f1: float = Field(ge=0, le=1, allow_inf_nan=False)
    temporal_iou: float = Field(ge=0, le=1, allow_inf_nan=False)
    start_time_error_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    end_time_error_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )
    false_positive_duration_seconds: float = Field(
        ge=0,
        allow_inf_nan=False,
    )


class EventEvaluation(BenchmarkModel):
    """Detailed one-case interval evaluation."""

    matches: list[EventMatch] = Field(default_factory=list)
    metrics: EventMetrics


class BenchmarkCaseResult(BenchmarkModel):
    """One detector evaluated against one manifest video."""

    video: str = Field(min_length=1)
    detector_id: str = Field(min_length=1)
    annotation_scope: str = Field(pattern=r"^(positive|negative|excluded)$")
    status: str = Field(pattern=r"^(ok|detector_error|excluded)$")
    tolerance_seconds: float = Field(ge=0, allow_inf_nan=False)
    expected_intervals: list[BenchmarkInterval] = Field(default_factory=list)
    predicted_intervals: list[BenchmarkInterval] = Field(default_factory=list)
    evaluation: EventEvaluation | None = None
    error_type: str | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def validate_status_details(self) -> Self:
        if self.status == "detector_error":
            if not self.error_type or not self.error_message:
                raise ValueError("detector_error requires error details")
            if self.evaluation is not None:
                raise ValueError("detector_error cannot contain evaluated metrics")
        elif self.error_type is not None or self.error_message is not None:
            raise ValueError("error details are only valid for detector_error")
        if self.status == "excluded" and self.annotation_scope != "excluded":
            raise ValueError("excluded status requires excluded annotation scope")
        return self


class DetectorBenchmarkResult(BenchmarkModel):
    """Independent aggregate for one detector and one configuration."""

    detector_id: str = Field(min_length=1)
    evaluated_case_count: int = Field(ge=0)
    excluded_unannotated_case_count: int = Field(ge=0)
    detector_error_count: int = Field(ge=0)
    negative_case_count: int = Field(ge=0)
    negative_false_positive_event_count: int = Field(ge=0)
    negative_false_positive_duration_seconds: float = Field(
        ge=0,
        allow_inf_nan=False,
    )
    metrics: EventMetrics
    cases: list[BenchmarkCaseResult] = Field(default_factory=list)


class BenchmarkProfileResult(BenchmarkModel):
    """Results for one explicit threshold/configuration profile."""

    config_id: str = Field(pattern=r"^config_[0-9a-f]{16}$")
    label: str = Field(min_length=1)
    configuration: dict[str, JsonValue]
    detectors: list[DetectorBenchmarkResult]


class BenchmarkRuntime(BenchmarkModel):
    """Environment facts required to interpret a benchmark run."""

    platform: str = Field(min_length=1)
    machine: str = Field(min_length=1)
    python_version: str = Field(min_length=1)
    ffmpeg_version: str = Field(min_length=1)


class BenchmarkReport(BenchmarkModel):
    """Top-level machine-readable benchmark artifact."""

    schema_version: str = BENCHMARK_SCHEMA_VERSION
    tool_version: str = Field(min_length=1)
    manifest_name: str = Field(min_length=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    matching: dict[str, JsonValue]
    runtime: BenchmarkRuntime
    total_runtime_seconds: float = Field(ge=0, allow_inf_nan=False)
    profiles: list[BenchmarkProfileResult]
    limitations: list[str]


class BenchmarkProfile(BaseModel):
    """Input profile pairing a human label with an AnalysisConfig."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    label: str = Field(min_length=1)
    config: AnalysisConfig
