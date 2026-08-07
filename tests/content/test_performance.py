"""Deterministic resource-bound gates for useful-content workflows."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from videoscope.content import ContentConfig, ContentTimeRange
from videoscope.content.models import ContentAction, ContentActionKind
from videoscope.content.preview import preview_context_ranges


def test_config_enforces_collection_and_preview_bounds() -> None:
    with pytest.raises(ValidationError):
        ContentConfig(maximum_transcript_cues=100_001)
    with pytest.raises(ValidationError):
        ContentConfig(maximum_chapters=2_001)
    with pytest.raises(ValidationError):
        ContentConfig(maximum_storyboard_items=10_001)
    with pytest.raises(ValidationError):
        ContentConfig(maximum_previews=1_001)
    with pytest.raises(ValidationError):
        ContentConfig(maximum_preview_seconds=30.1)


def test_join_preview_total_duration_never_exceeds_budget() -> None:
    action = ContentAction(
        id="action_" + "1" * 64,
        version="1",
        kind=ContentActionKind.CONCATENATE,
        description="Join three selected source intervals.",
        source_ranges=(
            ContentTimeRange(start_seconds=1, end_seconds=3),
            ContentTimeRange(start_seconds=5, end_seconds=7),
            ContentTimeRange(start_seconds=9, end_seconds=11),
        ),
        changes_content=True,
        requires_confirmation=True,
    )

    contexts = preview_context_ranges(
        action, duration_seconds=12, maximum_preview_seconds=6
    )

    assert sum(item.duration_seconds for item in contexts) <= 6
    assert all(0 <= item.start_seconds < item.end_seconds <= 12 for item in contexts)


def test_default_limits_are_finite_and_cpu_bounded() -> None:
    config = ContentConfig()

    assert config.maximum_transcript_cues == 20_000
    assert config.maximum_chapters == 500
    assert config.maximum_storyboard_items == 2_000
    assert config.maximum_previews == 100
    assert config.maximum_preview_seconds == 12
