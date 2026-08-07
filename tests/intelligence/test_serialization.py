from __future__ import annotations

from pathlib import Path

from tests.intelligence.helpers import content_map
from videoscope.intelligence import (
    FakeASRProvider,
    FakeContentIntelligenceProvider,
    build_intelligence_request,
    normalize_asr_transcript,
    read_ai_transcript,
    read_suggestion_batch,
    run_content_intelligence,
    write_intelligence_json,
)


def test_unicode_atomic_roundtrip(tmp_path: Path) -> None:
    transcript, _ = normalize_asr_transcript(
        FakeASRProvider(), tmp_path / "本地 视频.mp4", duration_seconds=10
    )
    batch = run_content_intelligence(
        FakeContentIntelligenceProvider(),
        build_intelligence_request(content_map(), transcript, locale="zh-CN"),
    )
    transcript_path = tmp_path / "中文 输出" / "transcript.json"
    batch_path = tmp_path / "中文 输出" / "suggestions.json"
    write_intelligence_json(transcript, transcript_path)
    write_intelligence_json(batch, batch_path)
    assert read_ai_transcript(transcript_path) == transcript
    assert read_suggestion_batch(batch_path) == batch
    assert not list(transcript_path.parent.glob("*.tmp"))
