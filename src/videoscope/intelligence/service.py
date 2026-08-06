"""Ground local model suggestions in a deterministic CPU content map."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Literal

from pydantic import JsonValue

from videoscope.content import ContentMap
from videoscope.content.transcript import NormalizedTranscript
from videoscope.intelligence.models import (
    AIExecutionRecord,
    AIExecutionStatus,
    AIRange,
    AIReviewDecision,
    AIReviewManifest,
    AISuggestion,
    AISuggestionBatch,
    AISuggestionDraft,
    AITranscript,
    AITranscriptSegment,
    ContentIntelligenceRequest,
    ReviewDecisionKind,
    SuggestionKind,
    make_ai_transcript_hash,
    make_ai_transcript_segment_id,
    make_batch_digest,
    make_review_digest,
    make_suggestion_id,
    suggestion_sort_key,
)
from videoscope.intelligence.protocols import ASRProvider, ContentIntelligenceProvider


class IntelligenceError(RuntimeError):
    """Safe base error for optional intelligence work."""


class IntelligenceGroundingError(IntelligenceError):
    """A provider response could not be tied to the supplied evidence."""


def normalize_asr_transcript(
    provider: ASRProvider,
    media_path: Path,
    *,
    duration_seconds: float,
    maximum_segments: int = 20_000,
) -> tuple[AITranscript, AIExecutionRecord]:
    started = perf_counter()
    try:
        drafts = tuple(provider.transcribe(Path(media_path)))
        if not drafts or len(drafts) > maximum_segments:
            raise IntelligenceGroundingError("ASR segment count is outside limits")
        segments: list[AITranscriptSegment] = []
        previous_end = 0.0
        for index, draft in enumerate(drafts):
            if draft.start_seconds < previous_end:
                raise IntelligenceGroundingError(
                    "ASR segments overlap or move backwards"
                )
            if draft.end_seconds > duration_seconds:
                raise IntelligenceGroundingError("ASR segment exceeds media duration")
            segments.append(
                AITranscriptSegment(
                    **draft.model_dump(),
                    id=make_ai_transcript_segment_id(
                        index,
                        draft.start_seconds,
                        draft.end_seconds,
                        draft.text,
                        draft.language,
                    ),
                    order_index=index,
                )
            )
            previous_end = draft.end_seconds
        segment_tuple = tuple(segments)
        transcript = AITranscript(
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            segments=segment_tuple,
            transcript_hash=make_ai_transcript_hash(
                provider.provider_id, provider.model_id, segment_tuple
            ),
            limitations=(
                "Automatic speech recognition can omit or misrecognize speech.",
                "Transcript timing and wording require human review.",
            ),
        )
        return transcript, _execution(provider, "transcribe", started)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise IntelligenceError(f"Local ASR failed: {type(exc).__name__}") from None


def normalize_trusted_transcript(value: NormalizedTranscript) -> AITranscript:
    """Adapt a validated local SRT/WebVTT transcript without model inference."""
    segments = tuple(
        AITranscriptSegment(
            id=make_ai_transcript_segment_id(
                index,
                cue.start_seconds,
                cue.end_seconds,
                cue.text,
                None,
            ),
            order_index=index,
            start_seconds=cue.start_seconds,
            end_seconds=cue.end_seconds,
            text=cue.text,
        )
        for index, cue in enumerate(value.cues)
    )
    provider_id = "trusted_timed_transcript"
    model_id = f"videoscope-{value.source_format.value}-parser-v1"
    return AITranscript(
        provider_id=provider_id,
        model_id=model_id,
        segments=segments,
        transcript_hash=make_ai_transcript_hash(provider_id, model_id, segments),
        limitations=(
            "VideoScope validated timing and syntax but did not verify "
            "transcript text.",
        ),
    )


def build_intelligence_request(
    content_map: ContentMap,
    transcript: AITranscript,
    *,
    locale: Literal["en", "zh-CN"] = "en",
    maximum_suggestions: int = 24,
) -> ContentIntelligenceRequest:
    ranges = tuple(
        AIRange(
            start_seconds=segment.source_range.start_seconds,
            end_seconds=segment.source_range.end_seconds,
        )
        for segment in content_map.segments
    )
    return ContentIntelligenceRequest(
        input_hash=content_map.input_hash,
        transcript_hash=transcript.transcript_hash,
        duration_seconds=content_map.duration_seconds,
        locale=locale,
        transcript_segments=transcript.segments,
        structural_ranges=ranges,
        maximum_suggestions=maximum_suggestions,
    )


def run_content_intelligence(
    provider: ContentIntelligenceProvider,
    request: ContentIntelligenceRequest,
    *,
    prompt_contract_version: str = "1",
    effective_parameters: Mapping[str, JsonValue] | None = None,
) -> AISuggestionBatch:
    started = perf_counter()
    try:
        drafts = tuple(provider.suggest(request))
        if len(drafts) > request.maximum_suggestions:
            raise IntelligenceGroundingError("provider returned too many suggestions")
        suggestions = tuple(
            sorted(
                (_ground_draft(provider, request, draft) for draft in drafts),
                key=suggestion_sort_key,
            )
        )
        execution = _execution(provider, "suggest", started)
        payload = {
            "schema_version": "0.1",
            "input_hash": request.input_hash,
            "transcript_hash": request.transcript_hash,
            "duration_seconds": request.duration_seconds,
            "provider_id": provider.provider_id,
            "model_id": provider.model_id,
            "prompt_contract_version": prompt_contract_version,
            "effective_parameters": dict(effective_parameters or {}),
            "suggestions": [item.model_dump(mode="json") for item in suggestions],
            "warnings": [
                "AI suggestions are unverified proposals and require human review."
            ],
        }
        return AISuggestionBatch(
            schema_version="0.1",
            input_hash=request.input_hash,
            transcript_hash=request.transcript_hash,
            duration_seconds=request.duration_seconds,
            provider_id=provider.provider_id,
            model_id=provider.model_id,
            prompt_contract_version=prompt_contract_version,
            effective_parameters=dict(effective_parameters or {}),
            suggestions=suggestions,
            execution=execution,
            warnings=(
                "AI suggestions are unverified proposals and require human review.",
            ),
            batch_digest=make_batch_digest(payload),
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except IntelligenceError:
        raise
    except Exception as exc:
        raise IntelligenceError(
            f"Local content intelligence failed: {type(exc).__name__}"
        ) from None


def build_review_manifest(
    batch: AISuggestionBatch,
    decisions: Sequence[AIReviewDecision],
) -> AIReviewManifest:
    by_id = {item.id for item in batch.suggestions}
    decision_tuple = tuple(sorted(decisions, key=lambda item: item.suggestion_id))
    if {item.suggestion_id for item in decision_tuple} != by_id:
        raise IntelligenceGroundingError(
            "review decisions must cover the exact suggestion batch"
        )
    for decision in decision_tuple:
        suggestion = next(
            item for item in batch.suggestions if item.id == decision.suggestion_id
        )
        edited_range = decision.edited_source_range
        if edited_range is not None:
            if suggestion.kind not in {
                SuggestionKind.CHAPTER,
                SuggestionKind.HIGHLIGHT,
            }:
                raise IntelligenceGroundingError(
                    "only ranged suggestions may edit a source range"
                )
            _require_range_in_duration(edited_range, _batch_duration(batch))
    payload = {
        "schema_version": "0.1",
        "input_hash": batch.input_hash,
        "batch_digest": batch.batch_digest,
        "decisions": [item.model_dump(mode="json") for item in decision_tuple],
    }
    return AIReviewManifest(
        schema_version="0.1",
        input_hash=batch.input_hash,
        batch_digest=batch.batch_digest,
        decisions=decision_tuple,
        review_digest=make_review_digest(payload),
    )


def accepted_suggestions(
    batch: AISuggestionBatch,
    review: AIReviewManifest,
) -> tuple[tuple[AISuggestion, AIReviewDecision], ...]:
    if (
        review.input_hash != batch.input_hash
        or review.batch_digest != batch.batch_digest
    ):
        raise IntelligenceGroundingError("review does not match suggestion batch")
    suggestions = {item.id: item for item in batch.suggestions}
    return tuple(
        (suggestions[decision.suggestion_id], decision)
        for decision in review.decisions
        if decision.decision in {ReviewDecisionKind.ACCEPT, ReviewDecisionKind.EDIT}
    )


def _ground_draft(
    provider: ContentIntelligenceProvider,
    request: ContentIntelligenceRequest,
    draft: AISuggestionDraft,
) -> AISuggestion:
    if draft.kind not in request.requested_kinds:
        raise IntelligenceGroundingError("provider returned an unrequested kind")
    ranged = draft.kind in {SuggestionKind.CHAPTER, SuggestionKind.HIGHLIGHT}
    if ranged and not draft.evidence.source_ranges:
        raise IntelligenceGroundingError("chapter and highlight require source ranges")
    known_cues = {item.id for item in request.transcript_segments}
    if any(item not in known_cues for item in draft.evidence.transcript_cue_ids):
        raise IntelligenceGroundingError(
            "suggestion references an unknown transcript cue"
        )
    for source_range in draft.evidence.source_ranges:
        _require_range_in_duration(source_range, request.duration_seconds)
    if any(
        timestamp > request.duration_seconds
        for timestamp in draft.evidence.frame_timestamps_seconds
    ):
        raise IntelligenceGroundingError("frame evidence exceeds media duration")
    suggestion_id = make_suggestion_id(
        request.input_hash,
        request.transcript_hash,
        provider.provider_id,
        provider.model_id,
        draft.kind,
        draft.content,
        draft.evidence,
    )
    limitations = tuple(
        dict.fromkeys((*draft.limitations, _kind_limitation(draft.kind)))
    )
    return AISuggestion(
        **draft.model_dump(exclude={"limitations"}),
        id=suggestion_id,
        limitations=limitations,
    )


def _kind_limitation(kind: SuggestionKind) -> str:
    if kind is SuggestionKind.SUMMARY:
        return "The summary may omit context and is not a factual verification."
    if kind is SuggestionKind.TITLE:
        return "The title is a draft and may overstate or omit source context."
    return "The proposed boundary may need frame-accurate human adjustment."


def _require_range_in_duration(value: AIRange, duration_seconds: float) -> None:
    if value.end_seconds > duration_seconds:
        raise IntelligenceGroundingError("suggestion range exceeds media duration")


def _execution(provider: object, operation: str, started: float) -> AIExecutionRecord:
    return AIExecutionRecord(
        provider_id=str(getattr(provider, "provider_id")),
        model_id=str(getattr(provider, "model_id")),
        operation=operation,
        status=AIExecutionStatus.OK,
        elapsed_seconds=max(0.0, perf_counter() - started),
        device=str(getattr(provider, "device")),
        precision=str(getattr(provider, "precision")),
    )


def _batch_duration(batch: AISuggestionBatch) -> float:
    return batch.duration_seconds


__all__ = [
    "IntelligenceError",
    "IntelligenceGroundingError",
    "accepted_suggestions",
    "build_intelligence_request",
    "build_review_manifest",
    "normalize_asr_transcript",
    "normalize_trusted_transcript",
    "run_content_intelligence",
]
