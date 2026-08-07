from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from tests.intelligence.helpers import content_map
from videoscope.cli import app
from videoscope.intelligence import (
    AIReviewDecision,
    AISuggestionBatch,
    AITranscript,
    FakeASRProvider,
    FakeContentIntelligenceProvider,
    ReviewDecisionKind,
    build_intelligence_request,
    build_review_manifest,
    normalize_asr_transcript,
    run_content_intelligence,
)

runner = CliRunner()


@dataclass(frozen=True)
class _Preparation:
    transcript: AITranscript
    suggestions: AISuggestionBatch
    private_root: Path


def _preparation(tmp_path: Path) -> _Preparation:
    transcript, _ = normalize_asr_transcript(
        FakeASRProvider(), tmp_path / "unused.mp4", duration_seconds=10
    )
    batch = run_content_intelligence(
        FakeContentIntelligenceProvider(),
        build_intelligence_request(content_map(), transcript),
    )
    return _Preparation(
        transcript=transcript,
        suggestions=batch,
        private_root=tmp_path / "ai-review-private",
    )


def test_assist_prepares_without_implicit_acceptance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    preparation = _preparation(tmp_path)

    class Pipeline:
        def __init__(self, _config: object) -> None:
            pass

        def prepare(self, _path: Path) -> _Preparation:
            return preparation

    monkeypatch.setattr("videoscope.cli.AdvancedAIContentPipeline", Pipeline)
    monkeypatch.setattr("videoscope.cli._is_interactive_stdin", lambda: False)
    result = runner.invoke(
        app,
        [
            "assist",
            str(source),
            "--output",
            str(tmp_path / "output"),
            "--semantic-model",
            "local-model",
        ],
    )
    assert result.exit_code == 0
    assert "Suggestions prepared only" in result.stdout
    assert "Suggestion batch" in result.stdout


def test_assist_accept_all_writes_exact_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    preparation = _preparation(tmp_path)
    captured: list[tuple[AIReviewDecision, ...]] = []

    class Pipeline:
        def __init__(self, _config: object) -> None:
            pass

        def prepare(self, _path: Path) -> _Preparation:
            return preparation

        def review(
            self, _preparation: object, decisions: tuple[AIReviewDecision, ...]
        ) -> object:
            captured.append(decisions)
            manifest = build_review_manifest(preparation.suggestions, decisions)
            return SimpleNamespace(manifest=manifest)

    monkeypatch.setattr("videoscope.cli.AdvancedAIContentPipeline", Pipeline)
    result = runner.invoke(
        app,
        [
            "assist",
            str(source),
            "--output",
            str(tmp_path / "output"),
            "--semantic-model",
            "local-model",
            "--accept-all",
        ],
    )
    assert result.exit_code == 0
    assert captured
    assert all(item.decision is ReviewDecisionKind.ACCEPT for item in captured[0])
    assert "Review manifest" in result.stdout
