"""Strict inputs and deterministic risks for manual Safe Sharing actions."""

from __future__ import annotations

import math
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from videoscope.domain import Severity
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyDecision,
    PrivacyModel,
    PrivacyRisk,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_risk_id,
)

_MANUAL_SCANNER_VERSION = "1.0.0"
_VISUAL_SCANNER_ID = "manual_visual_region"
_AUDIO_SCANNER_ID = "manual_audio_interval"
_VISUAL_STYLES = frozenset(
    {
        RedactionStyle.BLUR,
        RedactionStyle.PIXELATE,
        RedactionStyle.SOLID_FILL,
        RedactionStyle.CROP,
    }
)


class ManualVisualRegionInput(PrivacyModel):
    """One explicit visual rectangle and interval selected by the user."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    box: NormalizedBox
    style: RedactionStyle
    source_duration_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_interval_and_style(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("manual visual interval must have positive duration")
        if self.style not in _VISUAL_STYLES:
            raise ValueError("manual visual region requires a visual redaction style")
        if (
            self.source_duration_seconds is not None
            and self.end_seconds > self.source_duration_seconds
        ):
            raise ValueError("manual visual interval exceeds source duration")
        if self.style is RedactionStyle.CROP and (
            self.source_duration_seconds is None
            or not math.isclose(self.start_seconds, 0.0, rel_tol=0, abs_tol=1e-9)
            or not math.isclose(
                self.end_seconds,
                self.source_duration_seconds,
                rel_tol=0,
                abs_tol=1e-9,
            )
        ):
            raise ValueError("CROP requires one static full-duration rectangle")
        return self


class ManualAudioIntervalInput(PrivacyModel):
    """One explicit audio interval selected for local muting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    style: RedactionStyle = RedactionStyle.MUTE
    source_duration_seconds: float | None = Field(
        default=None,
        ge=0,
        allow_inf_nan=False,
    )

    @model_validator(mode="after")
    def validate_interval_and_style(self) -> Self:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("manual audio interval must have positive duration")
        if self.style is not RedactionStyle.MUTE:
            raise ValueError("manual audio interval supports only MUTE")
        if (
            self.source_duration_seconds is not None
            and self.end_seconds > self.source_duration_seconds
        ):
            raise ValueError("manual audio interval exceeds source duration")
        return self


def build_manual_visual_risk(
    input_hash: str,
    value: ManualVisualRegionInput,
) -> PrivacyRisk:
    """Build one reviewed visual risk without changing the selected rectangle."""
    return PrivacyRisk(
        id=make_privacy_risk_id(
            input_hash,
            _VISUAL_SCANNER_ID,
            PrivacyRiskType.MANUAL_VISUAL,
            value.start_seconds,
            value.end_seconds,
            value.box,
        ),
        scanner_id=_VISUAL_SCANNER_ID,
        scanner_version=_MANUAL_SCANNER_VERSION,
        risk_type=PrivacyRiskType.MANUAL_VISUAL,
        title="Manual visual region selected",
        public_description=(
            "A user-selected visual region is scheduled for local redaction."
        ),
        severity=Severity.MEDIUM,
        confidence=1.0,
        start_seconds=value.start_seconds,
        end_seconds=value.end_seconds,
        box=value.box,
        recommended_style=value.style,
        decision=PrivacyDecision.REDACT,
        style=value.style,
        limitations=(
            "This region reflects a manual selection and may not cover other "
            "privacy-sensitive content.",
        ),
        evidence=(
            {
                "selection": "manual_visual_region",
                "start_seconds": value.start_seconds,
                "end_seconds": value.end_seconds,
                "box": value.box.model_dump(mode="json"),
            },
        ),
    )


def build_manual_audio_risk(
    input_hash: str,
    value: ManualAudioIntervalInput,
) -> PrivacyRisk:
    """Build one reviewed mute risk for the exact user-selected interval."""
    return PrivacyRisk(
        id=make_privacy_risk_id(
            input_hash,
            _AUDIO_SCANNER_ID,
            PrivacyRiskType.MANUAL_AUDIO,
            value.start_seconds,
            value.end_seconds,
            None,
        ),
        scanner_id=_AUDIO_SCANNER_ID,
        scanner_version=_MANUAL_SCANNER_VERSION,
        risk_type=PrivacyRiskType.MANUAL_AUDIO,
        title="Manual audio interval selected",
        public_description=(
            "A user-selected audio interval is scheduled for local muting."
        ),
        severity=Severity.MEDIUM,
        confidence=1.0,
        start_seconds=value.start_seconds,
        end_seconds=value.end_seconds,
        recommended_style=RedactionStyle.MUTE,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.MUTE,
        limitations=(
            "Muting the selected interval can remove wanted speech or sound and "
            "does not identify other sensitive audio.",
        ),
        evidence=(
            {
                "selection": "manual_audio_interval",
                "start_seconds": value.start_seconds,
                "end_seconds": value.end_seconds,
            },
        ),
    )


__all__ = [
    "ManualAudioIntervalInput",
    "ManualVisualRegionInput",
    "build_manual_audio_risk",
    "build_manual_visual_risk",
]
