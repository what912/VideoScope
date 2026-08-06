"""Tests for immutable, versioned Safe Sharing audience profiles."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from videoscope.privacy.models import RedactionStyle
from videoscope.privacy.profiles import (
    ShareAudienceProfile,
    get_share_audience_profile,
    list_share_audience_profiles,
)


def test_profile_catalog_is_versioned_and_deterministic() -> None:
    profiles = list_share_audience_profiles()

    assert [profile.id for profile in profiles] == [
        "public",
        "work_client",
        "school",
        "family",
        "external_ai",
    ]
    assert all(profile.version == "1" for profile in profiles)
    assert all(profile.final_human_review_required for profile in profiles)


def test_profile_catalog_is_immutable() -> None:
    profile = get_share_audience_profile("public")

    with pytest.raises(ValidationError, match="frozen"):
        profile.default_visual_style = RedactionStyle.SOLID_FILL


def test_unknown_profile_is_rejected_without_fallback() -> None:
    with pytest.raises(KeyError, match="unknown Safe Sharing audience profile"):
        get_share_audience_profile("private_guess")


def test_profile_requires_known_metadata_categories() -> None:
    with pytest.raises(ValidationError, match="Input should be"):
        ShareAudienceProfile.model_validate(
            {
                "id": "custom",
                "version": "1",
                "forbidden_metadata_categories": ("unclassified-secret",),
                "required_manual_review_categories": ("visual",),
                "default_visual_style": RedactionStyle.BLUR,
                "qr_handling": "review",
                "final_human_review_required": True,
            }
        )
