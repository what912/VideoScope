"""Deterministic offline providers for Advanced AI tests and demos."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from videoscope.ai.models import Device, ModelHealth, ModelHealthStatus, Precision
from videoscope.intelligence.models import (
    AIRange,
    AISourceEvidence,
    AISuggestionDraft,
    AITranscriptSegmentDraft,
    ContentIntelligenceRequest,
    SuggestionKind,
)


class _FakeLifecycle:
    provider_id = "fake"
    model_id = "fake-content-intelligence-v1"
    device = Device.CPU
    precision = Precision.FLOAT32

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.loaded = False
        self.load_count = 0

    def load(self) -> None:
        self.loaded = True
        self.load_count += 1

    def unload(self) -> None:
        self.loaded = False

    def health(self) -> ModelHealth:
        return ModelHealth(
            status=(
                ModelHealthStatus.READY if self.loaded else ModelHealthStatus.UNLOADED
            ),
            local_files_available=True,
            message="Deterministic fake provider uses no model files.",
        )


class FakeASRProvider(_FakeLifecycle):
    model_id = "fake-asr-v1"

    def __init__(
        self,
        segments: Sequence[AITranscriptSegmentDraft] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        super().__init__(fail=fail)
        self.segments = tuple(
            segments
            or (
                AITranscriptSegmentDraft(
                    start_seconds=0,
                    end_seconds=4,
                    text="An introduction to the local video.",
                    language="en",
                    confidence=0.9,
                ),
                AITranscriptSegmentDraft(
                    start_seconds=4,
                    end_seconds=9,
                    text="The speaker demonstrates the central workflow.",
                    language="en",
                    confidence=0.88,
                ),
            )
        )

    def transcribe(self, media_path: Path) -> Sequence[AITranscriptSegmentDraft]:
        del media_path
        if self.fail:
            raise RuntimeError("injected fake ASR failure")
        return self.segments


class FakeContentIntelligenceProvider(_FakeLifecycle):
    def __init__(
        self,
        suggestions: Sequence[AISuggestionDraft] | None = None,
        *,
        fail: bool = False,
    ) -> None:
        super().__init__(fail=fail)
        self.suggestions = tuple(suggestions) if suggestions is not None else None

    def suggest(
        self, request: ContentIntelligenceRequest
    ) -> Sequence[AISuggestionDraft]:
        if self.fail:
            raise RuntimeError("injected fake semantic failure")
        if self.suggestions is not None:
            return self.suggestions
        first = request.transcript_segments[0]
        last = request.transcript_segments[-1]
        full = AIRange(
            start_seconds=first.start_seconds,
            end_seconds=last.end_seconds,
        )
        evidence = AISourceEvidence(
            source_ranges=(full,),
            transcript_cue_ids=tuple(item.id for item in request.transcript_segments),
        )
        return (
            AISuggestionDraft(
                kind=SuggestionKind.CHAPTER,
                content="Introduction and workflow",
                rationale=(
                    "The cited transcript moves from introduction to demonstration."
                ),
                evidence=evidence,
                confidence=0.8,
                limitations=("Chapter wording is a model-generated draft.",),
            ),
            AISuggestionDraft(
                kind=SuggestionKind.HIGHLIGHT,
                content="Core workflow demonstration",
                rationale="This range contains the described central workflow.",
                evidence=AISourceEvidence(
                    source_ranges=(
                        AIRange(
                            start_seconds=last.start_seconds,
                            end_seconds=last.end_seconds,
                        ),
                    ),
                    transcript_cue_ids=(last.id,),
                ),
                confidence=0.75,
            ),
            AISuggestionDraft(
                kind=SuggestionKind.SUMMARY,
                content="The video introduces and demonstrates a local workflow.",
                rationale="The statement is limited to the cited transcript.",
                evidence=evidence,
            ),
            AISuggestionDraft(
                kind=SuggestionKind.TITLE,
                content="A practical local video workflow",
                rationale=(
                    "The title describes the cited introduction and demonstration."
                ),
                evidence=evidence,
            ),
        )


__all__ = ["FakeASRProvider", "FakeContentIntelligenceProvider"]
