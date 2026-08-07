"""Real FFmpeg flow with deterministic Fake AI providers and no network."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from videoscope.content import (
    ContentConfig,
    ContentGoal,
    ContentPipelineConfig,
    ContentStatus,
    LongVideoContentPipeline,
)
from videoscope.intelligence import (
    AdvancedAIConfig,
    AdvancedAIContentPipeline,
    AdvancedAIDependencies,
    AdvancedAIPreparation,
    AIReviewDecision,
    FakeASRProvider,
    FakeContentIntelligenceProvider,
    ReviewDecisionKind,
    SuggestionKind,
    reviewed_content_ranges,
)

GENERATED = Path(__file__).resolve().parents[1] / "fixtures" / "generated"


def _require_media() -> tuple[Path, Path]:
    source = GENERATED / "content_meeting_structure.mp4"
    transcript = GENERATED / "content_meeting_valid.srt"
    if not source.is_file() or not transcript.is_file():
        pytest.skip("generate content fixtures before native Advanced AI gates")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for native Advanced AI gates")
    return source, transcript


def _prepare(
    tmp_path: Path, run_name: str
) -> tuple[Path, AdvancedAIContentPipeline, AdvancedAIPreparation]:
    source, transcript = _require_media()
    pipeline = AdvancedAIContentPipeline(
        AdvancedAIConfig(
            output_directory=tmp_path / run_name,
            transcript_path=transcript,
            semantic_model_id="fake-content-intelligence-v1",
            keep_workspace=False,
        ),
        dependencies=AdvancedAIDependencies(
            asr_provider=FakeASRProvider(fail=True),
            content_provider=FakeContentIntelligenceProvider(),
        ),
    )
    return source, pipeline, pipeline.prepare(source)


def test_native_fake_ai_is_deterministic_and_renders_reviewed_highlight(
    tmp_path: Path,
) -> None:
    source, _first_pipeline, first = _prepare(tmp_path, "第一次")
    _source, _second_pipeline, second = _prepare(tmp_path, "second run")
    assert first.suggestions.batch_digest == second.suggestions.batch_digest
    assert first.suggestions.suggestions == second.suggestions.suggestions

    decisions = tuple(
        AIReviewDecision(
            suggestion_id=item.id,
            decision=(
                ReviewDecisionKind.ACCEPT
                if item.kind is SuggestionKind.HIGHLIGHT
                else ReviewDecisionKind.REJECT
            ),
        )
        for item in first.suggestions.suggestions
    )
    reviewed = AdvancedAIContentPipeline(
        AdvancedAIConfig(
            output_directory=tmp_path / "review",
            semantic_model_id="unused-fake",
        )
    ).review(first, decisions)
    ranges = reviewed_content_ranges(first.suggestions, reviewed.manifest)
    assert len(ranges) == 1
    assert ranges[0].source_range.start_seconds == pytest.approx(8.1)
    assert ranges[0].source_range.end_seconds == pytest.approx(11.8)

    content = LongVideoContentPipeline(
        ContentPipelineConfig(
            output_directory=tmp_path / "rendered",
            content=ContentConfig(goal=ContentGoal.SELECTED_CLIPS, export_clips=True),
            user_ranges=ranges,
        )
    )
    try:
        prepared = content.prepare(source)
        preview = content.preview(prepared)
        accepted = tuple(
            action.id for action in preview.plan.actions if action.changes_content
        )
        confirmation = content.confirm(preview, accepted_action_ids=accepted)
        result = content.execute(preview, confirmation)
    finally:
        content.close()

    assert result.status is ContentStatus.COMPLETED
    mappings = result.technical_report.source_mappings
    assert [
        (item.source_range.start_seconds, item.source_range.end_seconds)
        for item in mappings
    ] == [(8.1, 11.8)]
    assert result.public_root is not None
    assert (result.public_root / "useful-content.mp4").is_file()
