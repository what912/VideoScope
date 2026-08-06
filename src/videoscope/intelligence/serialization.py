"""Canonical JSON readers and atomic writers for intelligence contracts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TypeVar

from videoscope.intelligence.models import (
    AIReviewManifest,
    AISuggestionBatch,
    AITranscript,
    IntelligenceModel,
    canonical_json,
)

T = TypeVar("T", bound=IntelligenceModel)


def write_intelligence_json(value: IntelligenceModel, path: Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        temporary.write_text(canonical_json(value), encoding="utf-8", newline="\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path, model: type[T]) -> T:
    return model.model_validate_json(Path(path).read_text(encoding="utf-8"))


def read_ai_transcript(path: Path) -> AITranscript:
    return _read(path, AITranscript)


def read_suggestion_batch(path: Path) -> AISuggestionBatch:
    return _read(path, AISuggestionBatch)


def read_review_manifest(path: Path) -> AIReviewManifest:
    return _read(path, AIReviewManifest)


__all__ = [
    "read_ai_transcript",
    "read_review_manifest",
    "read_suggestion_batch",
    "write_intelligence_json",
]
