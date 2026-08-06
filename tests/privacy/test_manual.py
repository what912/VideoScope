"""Validation tests for explicit manual Safe Sharing risks."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
    build_manual_audio_risk,
    build_manual_visual_risk,
)
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyDecision,
    PrivacyRiskType,
    RedactionStyle,
)


def test_manual_visual_risk_preserves_reviewed_box_and_interval() -> None:
    """A user's visual selection remains the exact actionable proposal."""
    box = NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4)

    risk = build_manual_visual_risk(
        input_hash="b" * 64,
        value=ManualVisualRegionInput(
            start_seconds=2.0,
            end_seconds=4.0,
            box=box,
            style=RedactionStyle.PIXELATE,
        ),
    )

    assert risk.decision is PrivacyDecision.REDACT
    assert risk.risk_type is PrivacyRiskType.MANUAL_VISUAL
    assert risk.style is RedactionStyle.PIXELATE
    assert risk.box == box
    assert (risk.start_seconds, risk.end_seconds) == (2.0, 4.0)
    assert risk.evidence


def test_manual_visual_risk_id_is_deterministic() -> None:
    """Equivalent explicit inputs must create one reproducible risk identity."""
    value = ManualVisualRegionInput(
        start_seconds=1.0,
        end_seconds=2.0,
        box=NormalizedBox(x_min=0.2, y_min=0.2, x_max=0.5, y_max=0.6),
        style=RedactionStyle.BLUR,
    )

    assert (
        build_manual_visual_risk("b" * 64, value).id
        == build_manual_visual_risk("b" * 64, value).id
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "style",
    [RedactionStyle.MUTE, RedactionStyle.REMOVE_METADATA],
)
def test_manual_visual_interval_rejects_nonvisual_style(style: RedactionStyle) -> None:
    """Audio and metadata actions cannot be attached to a visual rectangle."""
    with pytest.raises(ValidationError, match="visual redaction style"):
        ManualVisualRegionInput(
            start_seconds=1.0,
            end_seconds=2.0,
            box=NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4),
            style=style,
        )


def test_manual_crop_requires_static_full_duration_rectangle() -> None:
    """A partial crop cannot silently alter framing for only one interval."""
    with pytest.raises(ValidationError, match="static full-duration"):
        ManualVisualRegionInput(
            start_seconds=1.0,
            end_seconds=4.0,
            source_duration_seconds=5.0,
            box=NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.9, y_max=0.9),
            style=RedactionStyle.CROP,
        )


def test_manual_crop_accepts_static_full_duration_rectangle() -> None:
    """An explicit whole-video crop is a valid visual action."""
    value = ManualVisualRegionInput(
        start_seconds=0.0,
        end_seconds=5.0,
        source_duration_seconds=5.0,
        box=NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.9, y_max=0.9),
        style=RedactionStyle.CROP,
    )

    risk = build_manual_visual_risk("b" * 64, value)

    assert risk.style is RedactionStyle.CROP
    assert risk.track_id is None


def test_manual_audio_interval_rejects_visual_style() -> None:
    """A visual redaction style cannot become an audio action."""
    with pytest.raises(ValidationError, match="MUTE"):
        ManualAudioIntervalInput(
            start_seconds=1.0,
            end_seconds=2.0,
            style=RedactionStyle.BLUR,
        )


def test_manual_audio_interval_rejects_zero_duration() -> None:
    """A zero-length mute would not produce an actionable result."""
    with pytest.raises(ValidationError, match="positive duration"):
        ManualAudioIntervalInput(
            start_seconds=2.0,
            end_seconds=2.0,
            style=RedactionStyle.MUTE,
        )


def test_manual_audio_risk_is_explicit_and_deterministic() -> None:
    """The exact user-selected mute interval is reviewable and reproducible."""
    value = ManualAudioIntervalInput(
        start_seconds=1.25,
        end_seconds=2.75,
        style=RedactionStyle.MUTE,
    )

    first = build_manual_audio_risk("b" * 64, value)
    second = build_manual_audio_risk("b" * 64, value)

    assert first.id == second.id
    assert first.risk_type is PrivacyRiskType.MANUAL_AUDIO
    assert first.decision is PrivacyDecision.REDACT
    assert first.style is RedactionStyle.MUTE
    assert first.box is None
    assert first.evidence
