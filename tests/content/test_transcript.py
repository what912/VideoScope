"""Local SRT/WebVTT parsing is strict, deterministic, and private."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoscope.content.errors import ContentTranscriptError
from videoscope.content.transcript import (
    TranscriptFormat,
    load_timed_transcript,
    parse_timed_transcript,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "content"


def test_loads_unicode_multiline_srt_with_comma_milliseconds() -> None:
    transcript = load_timed_transcript(
        FIXTURES / "valid_中文.srt", duration_seconds=4.0
    )

    assert transcript.source_format is TranscriptFormat.SRT
    assert len(transcript.cues) == 2
    assert transcript.cues[0].text == "欢迎使用 VideoScope。"
    assert transcript.cues[1].text == "第二行\n仍属于同一个 cue。"
    assert transcript.cues[0].start_seconds == 0.0
    assert transcript.cues[0].end_seconds == 1.25
    assert (
        transcript.transcript_hash
        == load_timed_transcript(
            FIXTURES / "valid_中文.srt", duration_seconds=4.0
        ).transcript_hash
    )


def test_detects_webvtt_from_content_and_preserves_markup_as_text(
    tmp_path: Path,
) -> None:
    misleading_path = tmp_path / "字幕.srt"
    misleading_path.write_text(
        (FIXTURES / "valid.vtt").read_text(encoding="utf-8"), encoding="utf-8"
    )

    transcript = load_timed_transcript(misleading_path, duration_seconds=3.0)

    assert transcript.source_format is TranscriptFormat.WEBVTT
    assert transcript.cues[0].source_id == "intro"
    assert transcript.cues[0].text == "<b>Markup-looking text remains text.</b>"
    assert transcript.cues[1].end_seconds == 2.5


def test_accepts_bom_crlf_and_normalizes_equivalent_srt() -> None:
    lf = "1\n00:00:00,000 --> 00:00:01,000\n你好\n"
    crlf_bom = "\ufeff1\r\n00:00:00,000 --> 00:00:01,000\r\n你好\r\n"

    first = parse_timed_transcript(lf, duration_seconds=2.0)
    second = parse_timed_transcript(crlf_bom, duration_seconds=2.0)

    assert first == second


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "content",
    [
        "1\n00:00:02,000 --> 00:00:01,000\nreverse\n",
        "1\n00:61:00,000 --> 00:61:01,000\ninvalid minute\n",
        "1\n00:00:00,000 --> 00:00:00,000\nzero\n",
        "1\n00:00:00,000 --> 00:00:03,000\nout of range\n",
        "1\n00:00:00,000 --> 00:00:02,000\nfirst\n\n"
        "2\n00:00:01,500 --> 00:00:03,000\noverlap\n",
        "1\n00:00:00,000 --> 00:00:01,000\nfirst\n\n"
        "1\n00:00:01,100 --> 00:00:02,000\nduplicate id\n",
    ],
)
def test_rejects_invalid_timing_overlap_and_duplicate_ids(content: str) -> None:
    with pytest.raises(ContentTranscriptError):
        parse_timed_transcript(content, duration_seconds=2.5)


def test_rejects_malformed_file_with_sanitized_error() -> None:
    with pytest.raises(ContentTranscriptError) as captured:
        load_timed_transcript(FIXTURES / "malformed.srt", duration_seconds=4.0)

    assert str(captured.value) == ContentTranscriptError.public_message
    assert str(FIXTURES) not in str(captured.value)


def test_enforces_cue_text_and_collection_limits() -> None:
    content = (
        "1\n00:00:00,000 --> 00:00:01,000\nfirst\n\n"
        "2\n00:00:01,000 --> 00:00:02,000\nsecond\n"
    )

    with pytest.raises(ContentTranscriptError):
        parse_timed_transcript(content, maximum_cues=1)
    with pytest.raises(ContentTranscriptError):
        parse_timed_transcript(content, maximum_text_characters=3)


def test_rejects_empty_and_oversized_inputs(tmp_path: Path) -> None:
    empty = tmp_path / "empty.srt"
    empty.write_text("", encoding="utf-8")
    oversized = tmp_path / "large.srt"
    oversized.write_text("x" * 100, encoding="utf-8")

    with pytest.raises(ContentTranscriptError):
        load_timed_transcript(empty)
    with pytest.raises(ContentTranscriptError):
        load_timed_transcript(oversized, maximum_file_bytes=50)
