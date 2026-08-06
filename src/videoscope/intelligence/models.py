"""Strict, path-free contracts for reviewable Advanced AI suggestions."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

INTELLIGENCE_SCHEMA_VERSION: Literal["0.1"] = "0.1"
_SHA256 = r"^[0-9a-f]{64}$"


class IntelligenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SuggestionKind(StrEnum):
    CHAPTER = "chapter"
    HIGHLIGHT = "highlight"
    SUMMARY = "summary"
    TITLE = "title"


class ReviewDecisionKind(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
    EDIT = "edit"


class AIExecutionStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class AIRange(IntelligenceModel):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_duration(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("AI evidence range must have positive duration")
        return self


class AITranscriptSegmentDraft(IntelligenceModel):
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1, max_length=4_000)
    language: str | None = Field(default=None, min_length=1, max_length=32)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_duration(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("AI transcript segment must have positive duration")
        return self


class AITranscriptSegment(AITranscriptSegmentDraft):
    id: str = Field(pattern=r"^ai_cue_[0-9a-f]{64}$")
    order_index: int = Field(ge=0)


class AITranscript(IntelligenceModel):
    schema_version: Literal["0.1"] = INTELLIGENCE_SCHEMA_VERSION
    provider_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=300)
    segments: tuple[AITranscriptSegment, ...]
    transcript_hash: str = Field(pattern=_SHA256)
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_transcript(self) -> Self:
        if not self.segments:
            raise ValueError("AI transcript requires at least one segment")
        previous_end = 0.0
        for index, segment in enumerate(self.segments):
            if segment.order_index != index:
                raise ValueError("AI transcript order must be contiguous")
            if segment.start_seconds < previous_end:
                raise ValueError("AI transcript segments must not overlap")
            expected = make_ai_transcript_segment_id(
                index,
                segment.start_seconds,
                segment.end_seconds,
                segment.text,
                segment.language,
            )
            if segment.id != expected:
                raise ValueError("AI transcript segment ID does not match content")
            previous_end = segment.end_seconds
        expected_hash = make_ai_transcript_hash(
            self.provider_id, self.model_id, self.segments
        )
        if self.transcript_hash != expected_hash:
            raise ValueError("AI transcript hash does not match segments")
        return self


class AISourceEvidence(IntelligenceModel):
    source_ranges: tuple[AIRange, ...] = ()
    transcript_cue_ids: tuple[str, ...] = ()
    frame_timestamps_seconds: tuple[float, ...] = ()

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if not (
            self.source_ranges
            or self.transcript_cue_ids
            or self.frame_timestamps_seconds
        ):
            raise ValueError("AI suggestion evidence must not be empty")
        if len(self.transcript_cue_ids) != len(set(self.transcript_cue_ids)):
            raise ValueError("duplicate transcript cue evidence")
        if any(value < 0 for value in self.frame_timestamps_seconds):
            raise ValueError("frame evidence timestamp must be non-negative")
        return self


class AISuggestionDraft(IntelligenceModel):
    kind: SuggestionKind
    content: str = Field(min_length=1, max_length=8_000)
    rationale: str = Field(min_length=1, max_length=4_000)
    evidence: AISourceEvidence
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    limitations: tuple[str, ...] = ()


class AISuggestion(AISuggestionDraft):
    id: str = Field(pattern=r"^suggestion_[0-9a-f]{64}$")


class ContentIntelligenceRequest(IntelligenceModel):
    input_hash: str = Field(pattern=_SHA256)
    transcript_hash: str = Field(pattern=_SHA256)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    locale: Literal["en", "zh-CN"] = "en"
    transcript_segments: tuple[AITranscriptSegment, ...]
    structural_ranges: tuple[AIRange, ...] = ()
    requested_kinds: tuple[SuggestionKind, ...] = tuple(SuggestionKind)
    maximum_suggestions: int = Field(default=24, ge=1, le=200)


class AIExecutionRecord(IntelligenceModel):
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    status: AIExecutionStatus
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    device: str = Field(min_length=1)
    precision: str = Field(min_length=1)
    error_type: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_error(self) -> Self:
        if self.status is AIExecutionStatus.FAILED and self.error_type is None:
            raise ValueError("failed AI execution requires an error type")
        if self.status is not AIExecutionStatus.FAILED and self.error_type is not None:
            raise ValueError("successful or skipped AI execution cannot have an error")
        return self


class AISuggestionBatch(IntelligenceModel):
    schema_version: Literal["0.1"] = INTELLIGENCE_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256)
    transcript_hash: str = Field(pattern=_SHA256)
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    prompt_contract_version: str = Field(min_length=1)
    effective_parameters: dict[str, JsonValue] = Field(default_factory=dict)
    suggestions: tuple[AISuggestion, ...]
    execution: AIExecutionRecord
    warnings: tuple[str, ...] = ()
    batch_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        ids = tuple(item.id for item in self.suggestions)
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate AI suggestion ID")
        if self.suggestions != tuple(sorted(self.suggestions, key=suggestion_sort_key)):
            raise ValueError("AI suggestions must use canonical order")
        for suggestion in self.suggestions:
            expected = make_suggestion_id(
                self.input_hash,
                self.transcript_hash,
                self.provider_id,
                self.model_id,
                suggestion.kind,
                suggestion.content,
                suggestion.evidence,
            )
            if suggestion.id != expected:
                raise ValueError("AI suggestion ID does not match grounded content")
        expected_digest = make_batch_digest(
            self.model_dump(mode="json", exclude={"batch_digest", "execution"})
        )
        if self.batch_digest != expected_digest:
            raise ValueError("AI suggestion batch digest does not match content")
        return self


class AIReviewDecision(IntelligenceModel):
    suggestion_id: str = Field(pattern=r"^suggestion_[0-9a-f]{64}$")
    decision: ReviewDecisionKind
    edited_content: str | None = Field(default=None, min_length=1, max_length=8_000)
    edited_source_range: AIRange | None = None

    @model_validator(mode="after")
    def validate_edit(self) -> Self:
        has_edit = (
            self.edited_content is not None or self.edited_source_range is not None
        )
        if self.decision is ReviewDecisionKind.EDIT and not has_edit:
            raise ValueError("edit decision requires edited content or range")
        if self.decision is not ReviewDecisionKind.EDIT and has_edit:
            raise ValueError("only edit decisions may contain edited values")
        return self


class AIReviewManifest(IntelligenceModel):
    schema_version: Literal["0.1"] = INTELLIGENCE_SCHEMA_VERSION
    input_hash: str = Field(pattern=_SHA256)
    batch_digest: str = Field(pattern=_SHA256)
    decisions: tuple[AIReviewDecision, ...]
    review_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        ids = tuple(item.suggestion_id for item in self.decisions)
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate AI review decision")
        if ids != tuple(sorted(ids)):
            raise ValueError("AI review decisions must use suggestion ID order")
        expected = make_review_digest(
            self.model_dump(mode="json", exclude={"review_digest"})
        )
        if self.review_digest != expected:
            raise ValueError("AI review digest does not match decisions")
        return self


def make_ai_transcript_segment_id(
    order_index: int,
    start_seconds: float,
    end_seconds: float,
    text: str,
    language: str | None,
) -> str:
    return "ai_cue_" + _digest(
        {
            "order_index": order_index,
            "start_hex": float(start_seconds).hex(),
            "end_hex": float(end_seconds).hex(),
            "text": text,
            "language": language,
        }
    )


def make_ai_transcript_hash(
    provider_id: str,
    model_id: str,
    segments: tuple[AITranscriptSegment, ...],
) -> str:
    return _digest(
        {
            "schema_version": INTELLIGENCE_SCHEMA_VERSION,
            "provider_id": provider_id,
            "model_id": model_id,
            "segments": [item.model_dump(mode="json") for item in segments],
        }
    )


def make_suggestion_id(
    input_hash: str,
    transcript_hash: str,
    provider_id: str,
    model_id: str,
    kind: SuggestionKind,
    content: str,
    evidence: AISourceEvidence,
) -> str:
    return "suggestion_" + _digest(
        {
            "input_hash": input_hash,
            "transcript_hash": transcript_hash,
            "provider_id": provider_id,
            "model_id": model_id,
            "kind": kind.value,
            "content": content,
            "evidence": evidence.model_dump(mode="json"),
        }
    )


def suggestion_sort_key(value: AISuggestion) -> tuple[float, str, str]:
    start = (
        value.evidence.source_ranges[0].start_seconds
        if value.evidence.source_ranges
        else float("inf")
    )
    return start, value.kind.value, value.id


def make_batch_digest(payload: object) -> str:
    return _digest(payload)


def make_review_digest(payload: object) -> str:
    return _digest(payload)


def _digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_json(value: IntelligenceModel) -> str:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    )


__all__ = [
    name
    for name in globals()
    if name.startswith("AI")
    or name
    in {
        "ContentIntelligenceRequest",
        "INTELLIGENCE_SCHEMA_VERSION",
        "IntelligenceModel",
        "ReviewDecisionKind",
        "SuggestionKind",
        "canonical_json",
        "make_ai_transcript_hash",
        "make_ai_transcript_segment_id",
        "make_batch_digest",
        "make_review_digest",
        "make_suggestion_id",
        "suggestion_sort_key",
    }
]
