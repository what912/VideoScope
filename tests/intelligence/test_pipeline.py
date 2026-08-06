from __future__ import annotations

from pathlib import Path

import pytest

from tests.intelligence.helpers import content_map
from videoscope.content import (
    ContentPreparation,
    build_content_actions,
    build_storyboard,
)
from videoscope.domain import VideoMetadata
from videoscope.intelligence import (
    AdvancedAICancelledError,
    AdvancedAIConfig,
    AdvancedAIContentPipeline,
    AdvancedAIDependencies,
    AIReviewDecision,
    FakeASRProvider,
    FakeContentIntelligenceProvider,
    ReviewDecisionKind,
    read_ai_transcript,
    read_review_manifest,
    read_suggestion_batch,
)


class _ContentPipeline:
    def __init__(self) -> None:
        self.closed = False

    def prepare(self, input_path: Path) -> ContentPreparation:
        del input_path
        value = content_map()
        storyboard = build_storyboard(value)
        return ContentPreparation(
            content_map=value,
            storyboard=storyboard,
            actions=build_content_actions(value, storyboard),
            metadata=VideoMetadata(
                filename="source.mp4",
                container_format="mp4",
                codec="h264",
                width=320,
                height=180,
                duration_seconds=10,
                average_frame_rate=10,
                estimated_frame_count=100,
                has_audio=True,
                file_size_bytes=100,
            ),
            warnings=(),
        )

    def close(self) -> None:
        self.closed = True


def test_pipeline_uses_private_artifacts_and_shared_fake_providers(
    tmp_path: Path,
) -> None:
    source = tmp_path / "输入 视频.mp4"
    source.write_bytes(b"fixture")
    local_content = _ContentPipeline()
    asr = FakeASRProvider()
    semantic = FakeContentIntelligenceProvider()
    pipeline = AdvancedAIContentPipeline(
        AdvancedAIConfig(
            output_directory=tmp_path / "输出",
            semantic_model_id=semantic.model_id,
            keep_workspace=True,
        ),
        dependencies=AdvancedAIDependencies(
            content_pipeline_factory=lambda _config: local_content,
            asr_provider=asr,
            content_provider=semantic,
        ),
    )
    preparation = pipeline.prepare(source)
    assert local_content.closed
    assert asr.load_count == 1
    assert semantic.load_count == 1
    assert (
        read_ai_transcript(preparation.private_root / "transcript.json")
        == preparation.transcript
    )
    assert (
        read_suggestion_batch(preparation.private_root / "suggestions.json")
        == preparation.suggestions
    )
    assert "ai-review-private" in preparation.private_root.parts

    decisions = tuple(
        AIReviewDecision(
            suggestion_id=item.id,
            decision=ReviewDecisionKind.REJECT,
        )
        for item in preparation.suggestions.suggestions
    )
    review = pipeline.review(preparation, decisions)
    assert (
        read_review_manifest(preparation.private_root / "review-manifest.json")
        == review.manifest
    )


def test_pipeline_prefers_trusted_transcript_over_asr(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    transcript_path = tmp_path / "trusted.vtt"
    transcript_path.write_text(
        "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nTrusted local cue.\n",
        encoding="utf-8",
    )
    asr = FakeASRProvider(fail=True)
    pipeline = AdvancedAIContentPipeline(
        AdvancedAIConfig(
            output_directory=tmp_path / "output",
            transcript_path=transcript_path,
            semantic_model_id="fake-content-intelligence-v1",
            keep_workspace=True,
        ),
        dependencies=AdvancedAIDependencies(
            content_pipeline_factory=lambda _config: _ContentPipeline(),
            asr_provider=asr,
            content_provider=FakeContentIntelligenceProvider(),
        ),
    )
    prepared = pipeline.prepare(source)
    assert prepared.transcript.provider_id == "trusted_timed_transcript"
    assert asr.load_count == 0


def test_pipeline_honors_cancellation_before_provider_work(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    asr = FakeASRProvider()
    pipeline = AdvancedAIContentPipeline(
        AdvancedAIConfig(
            output_directory=tmp_path / "output",
            semantic_model_id="fake-content-intelligence-v1",
            cancellation_callback=lambda: True,
        ),
        dependencies=AdvancedAIDependencies(
            content_pipeline_factory=lambda _config: _ContentPipeline(),
            asr_provider=asr,
            content_provider=FakeContentIntelligenceProvider(),
        ),
    )
    with pytest.raises(AdvancedAICancelledError):
        pipeline.prepare(source)
    assert asr.load_count == 0
    assert not (tmp_path / "output" / "ai-review-private").exists()
