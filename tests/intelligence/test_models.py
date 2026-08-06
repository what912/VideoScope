from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from videoscope.intelligence import (
    AIRange,
    AIReviewDecision,
    AITranscriptSegmentDraft,
    ReviewDecisionKind,
)


def test_range_rejects_reverse_and_non_finite_values() -> None:
    with pytest.raises(ValidationError):
        AIRange(start_seconds=2, end_seconds=1)
    with pytest.raises(ValidationError):
        AIRange(start_seconds=0, end_seconds=float("inf"))


def test_review_edit_requires_an_actual_edit() -> None:
    with pytest.raises(ValidationError):
        AIReviewDecision(
            suggestion_id="suggestion_" + "1" * 64,
            decision=ReviewDecisionKind.EDIT,
        )


def test_transcript_draft_preserves_chinese() -> None:
    value = AITranscriptSegmentDraft(
        start_seconds=0,
        end_seconds=1,
        text="这是本地视频的简介。",
        language="zh-CN",
    )
    assert "这是" in json.dumps(value.model_dump(mode="json"), ensure_ascii=False)
