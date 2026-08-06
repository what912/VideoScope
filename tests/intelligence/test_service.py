from __future__ import annotations

from pathlib import Path

import pytest

from tests.intelligence.helpers import content_map
from videoscope.intelligence import (
    AIRange,
    AIReviewDecision,
    AISourceEvidence,
    AISuggestionDraft,
    AITranscript,
    ContentIntelligenceRequest,
    FakeASRProvider,
    FakeContentIntelligenceProvider,
    IntelligenceError,
    IntelligenceGroundingError,
    ReviewDecisionKind,
    SuggestionKind,
    build_intelligence_request,
    build_review_manifest,
    normalize_asr_transcript,
    reviewed_content_ranges,
    reviewed_text_suggestions,
    run_content_intelligence,
)


def _prepared() -> tuple[AITranscript, ContentIntelligenceRequest]:
    transcript, _ = normalize_asr_transcript(
        FakeASRProvider(), Path("unused.mp4"), duration_seconds=10
    )
    request = build_intelligence_request(content_map(), transcript)
    return transcript, request


def test_fake_pipeline_is_deterministic_and_grounded() -> None:
    _, request = _prepared()
    provider = FakeContentIntelligenceProvider()
    first = run_content_intelligence(provider, request)
    second = run_content_intelligence(provider, request)
    assert first.batch_digest == second.batch_digest
    assert tuple(item.id for item in first.suggestions) == tuple(
        item.id for item in second.suggestions
    )
    assert {item.kind for item in first.suggestions} == set(SuggestionKind)
    assert all(item.evidence.transcript_cue_ids for item in first.suggestions)


def test_unknown_cue_and_out_of_range_are_rejected() -> None:
    _, request = _prepared()
    bad = AISuggestionDraft(
        kind=SuggestionKind.HIGHLIGHT,
        content="Bad range",
        rationale="Injected malformed provider output.",
        evidence=AISourceEvidence(
            source_ranges=(AIRange(start_seconds=9, end_seconds=12),),
            transcript_cue_ids=("missing",),
        ),
    )
    with pytest.raises(IntelligenceGroundingError):
        run_content_intelligence(FakeContentIntelligenceProvider((bad,)), request)


def test_provider_failure_is_visible_and_does_not_change_cpu_map() -> None:
    cpu_map = content_map()
    transcript, _ = normalize_asr_transcript(
        FakeASRProvider(), Path("unused.mp4"), duration_seconds=10
    )
    before = cpu_map.map_digest
    with pytest.raises(IntelligenceError, match="RuntimeError"):
        run_content_intelligence(
            FakeContentIntelligenceProvider(fail=True),
            build_intelligence_request(cpu_map, transcript),
        )
    assert cpu_map.map_digest == before


def test_exact_review_bridges_only_accepted_items() -> None:
    _, request = _prepared()
    batch = run_content_intelligence(FakeContentIntelligenceProvider(), request)
    decisions = tuple(
        AIReviewDecision(
            suggestion_id=item.id,
            decision=(
                ReviewDecisionKind.ACCEPT
                if item.kind
                in {
                    SuggestionKind.HIGHLIGHT,
                    SuggestionKind.SUMMARY,
                    SuggestionKind.TITLE,
                }
                else ReviewDecisionKind.REJECT
            ),
        )
        for item in batch.suggestions
    )
    review = build_review_manifest(batch, decisions)
    content_ranges = reviewed_content_ranges(batch, review)
    texts = reviewed_text_suggestions(batch, review)
    assert len(content_ranges) == 1
    assert content_ranges[0].kind.value == "keep"
    assert len(texts["summary"]) == 1
    assert len(texts["title"]) == 1


def test_review_must_cover_exact_batch() -> None:
    _, request = _prepared()
    batch = run_content_intelligence(FakeContentIntelligenceProvider(), request)
    with pytest.raises(IntelligenceGroundingError, match="exact"):
        build_review_manifest(batch, ())


def test_asr_overlap_and_failure_are_sanitized() -> None:
    from videoscope.intelligence import AITranscriptSegmentDraft

    overlap = (
        AITranscriptSegmentDraft(start_seconds=0, end_seconds=3, text="one"),
        AITranscriptSegmentDraft(start_seconds=2, end_seconds=4, text="two"),
    )
    with pytest.raises(IntelligenceError, match="IntelligenceGroundingError"):
        normalize_asr_transcript(
            FakeASRProvider(overlap), Path("unused.mp4"), duration_seconds=10
        )
    with pytest.raises(IntelligenceError, match="RuntimeError"):
        normalize_asr_transcript(
            FakeASRProvider(fail=True), Path("private-name.mp4"), duration_seconds=10
        )
