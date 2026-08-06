"""Strict local SRT and WebVTT normalization without remote services."""

from __future__ import annotations

import json
import math
import re
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import Field, model_validator

from videoscope.content.errors import ContentTranscriptError
from videoscope.content.models import CONTENT_SCHEMA_VERSION, ContentModel

_SRT_TIMESTAMP: Final = re.compile(
    r"^(?P<hours>\d{2,}):(?P<minutes>\d{2}):(?P<seconds>\d{2}),"
    r"(?P<milliseconds>\d{3})$"
)
_VTT_TIMESTAMP: Final = re.compile(
    r"^(?:(?P<hours>\d{2,}):)?(?P<minutes>\d{2}):(?P<seconds>\d{2})\."
    r"(?P<milliseconds>\d{3})$"
)


class TranscriptFormat(StrEnum):
    SRT = "srt"
    WEBVTT = "webvtt"


class TranscriptCue(ContentModel):
    """One validated private cue with deterministic identity."""

    id: str = Field(pattern=r"^cue_[0-9a-f]{64}$")
    source_id: str = Field(min_length=1, max_length=200)
    order_index: int = Field(ge=0)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_duration(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("transcript cue must have positive duration")
        return self


class NormalizedTranscript(ContentModel):
    """Private, path-free canonical transcript evidence."""

    schema_version: Literal["0.1"] = CONTENT_SCHEMA_VERSION
    source_format: TranscriptFormat
    cues: tuple[TranscriptCue, ...]
    transcript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_transcript(self) -> Self:
        if not self.cues:
            raise ValueError("normalized transcript requires at least one cue")
        source_ids = tuple(cue.source_id for cue in self.cues)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("duplicate transcript cue source ID")
        previous_end = 0.0
        for index, cue in enumerate(self.cues):
            if cue.order_index != index:
                raise ValueError("transcript cues must use contiguous source order")
            if cue.start_seconds < previous_end:
                raise ValueError("transcript cues must not overlap or move backwards")
            expected_id = make_transcript_cue_id(
                cue.source_id,
                cue.order_index,
                cue.start_seconds,
                cue.end_seconds,
                cue.text,
            )
            if cue.id != expected_id:
                raise ValueError("transcript cue ID does not match normalized content")
            previous_end = cue.end_seconds
        expected_hash = make_transcript_hash(self.source_format, self.cues)
        if self.transcript_hash != expected_hash:
            raise ValueError("transcript_hash does not match normalized cues")
        return self


def load_timed_transcript(
    path: Path,
    *,
    duration_seconds: float | None = None,
    maximum_cues: int = 20_000,
    maximum_text_characters: int = 4_000,
    maximum_file_bytes: int = 16 * 1024 * 1024,
) -> NormalizedTranscript:
    """Read one bounded local UTF-8 transcript and normalize its contents."""
    source = Path(path)
    try:
        if maximum_file_bytes <= 0:
            raise ValueError("maximum file size must be positive")
        if not source.is_file() or source.stat().st_size > maximum_file_bytes:
            raise ValueError(
                "transcript file is missing or exceeds the configured limit"
            )
        content = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ContentTranscriptError(type(exc).__name__) from None
    return parse_timed_transcript(
        content,
        duration_seconds=duration_seconds,
        maximum_cues=maximum_cues,
        maximum_text_characters=maximum_text_characters,
    )


def parse_timed_transcript(
    content: str,
    *,
    duration_seconds: float | None = None,
    maximum_cues: int = 20_000,
    maximum_text_characters: int = 4_000,
) -> NormalizedTranscript:
    """Parse SRT or WebVTT according to content, not the supplied extension."""
    try:
        _validate_limits(
            duration_seconds,
            maximum_cues=maximum_cues,
            maximum_text_characters=maximum_text_characters,
        )
        normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip():
            raise ValueError("transcript is empty")
        first_line = normalized.split("\n", 1)[0].strip()
        if first_line.startswith("WEBVTT"):
            source_format = TranscriptFormat.WEBVTT
            raw_cues = _parse_webvtt_blocks(normalized)
        else:
            source_format = TranscriptFormat.SRT
            raw_cues = _parse_srt_blocks(normalized)
        if not raw_cues or len(raw_cues) > maximum_cues:
            raise ValueError("transcript cue count is outside the configured limit")
        cues: list[TranscriptCue] = []
        seen_source_ids: set[str] = set()
        previous_end = 0.0
        for index, (source_id, start, end, text) in enumerate(raw_cues):
            if source_id in seen_source_ids:
                raise ValueError("duplicate transcript cue source ID")
            seen_source_ids.add(source_id)
            if len(text) > maximum_text_characters:
                raise ValueError("transcript cue text exceeds the configured limit")
            if start < previous_end:
                raise ValueError("transcript cues overlap or move backwards")
            if duration_seconds is not None and end > duration_seconds:
                raise ValueError("transcript cue exceeds media duration")
            cue = TranscriptCue(
                id=make_transcript_cue_id(source_id, index, start, end, text),
                source_id=source_id,
                order_index=index,
                start_seconds=start,
                end_seconds=end,
                text=text,
            )
            cues.append(cue)
            previous_end = end
        cue_tuple = tuple(cues)
        return NormalizedTranscript(
            source_format=source_format,
            cues=cue_tuple,
            transcript_hash=make_transcript_hash(source_format, cue_tuple),
        )
    except ContentTranscriptError:
        raise
    except (TypeError, ValueError) as exc:
        raise ContentTranscriptError(type(exc).__name__) from None


def make_transcript_cue_id(
    source_id: str,
    order_index: int,
    start_seconds: float,
    end_seconds: float,
    text: str,
) -> str:
    """Return a stable private cue identity from normalized cue content."""
    payload = {
        "end_seconds": _finite_seconds(end_seconds),
        "order_index": order_index,
        "source_id": source_id,
        "start_seconds": _finite_seconds(start_seconds),
        "text": text,
    }
    if not source_id or order_index < 0 or not text or end_seconds <= start_seconds:
        raise ValueError("invalid normalized transcript cue identity inputs")
    return "cue_" + _canonical_digest(payload)


def make_transcript_hash(
    source_format: TranscriptFormat, cues: tuple[TranscriptCue, ...]
) -> str:
    """Hash a path-free normalized transcript representation."""
    return _canonical_digest(
        {
            "schema_version": CONTENT_SCHEMA_VERSION,
            "source_format": source_format.value,
            "cues": [cue.model_dump(mode="json") for cue in cues],
        }
    )


def _parse_srt_blocks(content: str) -> list[tuple[str, float, float, str]]:
    blocks = re.split(r"\n[ \t]*\n", content.strip())
    cues: list[tuple[str, float, float, str]] = []
    for generated_index, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if not lines:
            continue
        if "-->" in lines[0]:
            source_id = str(generated_index)
            timing_index = 0
        else:
            source_id = lines[0].strip()
            timing_index = 1
        if not source_id or len(lines) <= timing_index + 1:
            raise ValueError("malformed SRT cue block")
        start, end = _parse_timing_line(lines[timing_index], TranscriptFormat.SRT)
        text = "\n".join(lines[timing_index + 1 :]).strip()
        if not text:
            raise ValueError("SRT cue text is empty")
        cues.append((source_id, start, end, text))
    return cues


def _parse_webvtt_blocks(content: str) -> list[tuple[str, float, float, str]]:
    lines = content.split("\n")
    if not lines or not lines[0].strip().startswith("WEBVTT"):
        raise ValueError("WebVTT header is missing")
    body = "\n".join(lines[1:]).strip()
    if not body:
        return []
    blocks = re.split(r"\n[ \t]*\n", body)
    cues: list[tuple[str, float, float, str]] = []
    for generated_index, block in enumerate(blocks, start=1):
        cue_lines = block.split("\n")
        if cue_lines[0].startswith("NOTE"):
            continue
        if "-->" in cue_lines[0]:
            source_id = str(generated_index)
            timing_index = 0
        else:
            source_id = cue_lines[0].strip()
            timing_index = 1
        if not source_id or len(cue_lines) <= timing_index + 1:
            raise ValueError("malformed WebVTT cue block")
        start, end = _parse_timing_line(
            cue_lines[timing_index], TranscriptFormat.WEBVTT
        )
        text = "\n".join(cue_lines[timing_index + 1 :]).strip()
        if not text:
            raise ValueError("WebVTT cue text is empty")
        cues.append((source_id, start, end, text))
    return cues


def _parse_timing_line(
    value: str, source_format: TranscriptFormat
) -> tuple[float, float]:
    parts = value.split("-->")
    if len(parts) != 2:
        raise ValueError("timing line must contain one arrow")
    start_text = parts[0].strip()
    end_text = parts[1].strip().split(maxsplit=1)[0]
    pattern = (
        _SRT_TIMESTAMP if source_format is TranscriptFormat.SRT else _VTT_TIMESTAMP
    )
    start = _parse_timestamp(start_text, pattern)
    end = _parse_timestamp(end_text, pattern)
    if end <= start:
        raise ValueError("cue duration must be positive")
    return start, end


def _parse_timestamp(value: str, pattern: re.Pattern[str]) -> float:
    match = pattern.fullmatch(value)
    if match is None:
        raise ValueError("timestamp is malformed")
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    milliseconds = int(match.group("milliseconds"))
    if minutes >= 60 or seconds >= 60:
        raise ValueError("timestamp minutes and seconds must be below 60")
    return hours * 3600.0 + minutes * 60.0 + seconds + milliseconds / 1000.0


def _validate_limits(
    duration_seconds: float | None,
    *,
    maximum_cues: int,
    maximum_text_characters: int,
) -> None:
    if maximum_cues <= 0 or maximum_text_characters <= 0:
        raise ValueError("transcript limits must be positive")
    if duration_seconds is not None:
        duration = float(duration_seconds)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("media duration must be finite and positive")


def _finite_seconds(value: float) -> float:
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError("transcript time must be finite and non-negative")
    return 0.0 if normalized == 0 else normalized


def _canonical_digest(payload: object) -> str:
    content = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(content.encode("utf-8")).hexdigest()
