"""Tests for the immutable built-in Publish Ready profile catalog."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from videoscope.resolve.models import PublishProfileId
from videoscope.resolve.profiles import (
    COMPATIBLE_MP4,
    SOCIAL_HORIZONTAL,
    SOCIAL_VERTICAL,
    PublishProfile,
    _build_profile_catalog,
    get_publish_profile,
    list_publish_profiles,
)


def test_builtin_profiles_have_exact_stable_order_and_v1_values() -> None:
    profiles = list_publish_profiles()

    assert profiles == (
        COMPATIBLE_MP4,
        SOCIAL_VERTICAL,
        SOCIAL_HORIZONTAL,
    )
    assert [profile.id for profile in profiles] == [
        PublishProfileId.COMPATIBLE_MP4,
        PublishProfileId.SOCIAL_VERTICAL,
        PublishProfileId.SOCIAL_HORIZONTAL,
    ]
    assert [profile.model_dump(mode="json") for profile in profiles] == [
        {
            "id": "compatible_mp4",
            "version": "1.0.0",
            "width": None,
            "height": None,
            "maximum_fps": 60.0,
            "video_codec": "h264",
            "audio_codec": "aac",
            "pixel_format": "yuv420p",
            "container": "mp4",
        },
        {
            "id": "social_vertical_9_16",
            "version": "1.0.0",
            "width": 1080,
            "height": 1920,
            "maximum_fps": 60.0,
            "video_codec": "h264",
            "audio_codec": "aac",
            "pixel_format": "yuv420p",
            "container": "mp4",
        },
        {
            "id": "social_horizontal_16_9",
            "version": "1.0.0",
            "width": 1920,
            "height": 1080,
            "maximum_fps": 60.0,
            "video_codec": "h264",
            "audio_codec": "aac",
            "pixel_format": "yuv420p",
            "container": "mp4",
        },
    ]


def test_profile_catalog_rejects_duplicate_ids() -> None:
    duplicate = COMPATIBLE_MP4.model_copy()

    with pytest.raises(ValueError, match="duplicate PublishProfile id"):
        _build_profile_catalog((COMPATIBLE_MP4, duplicate))


def test_profile_lookup_rejects_unknown_profile() -> None:
    unknown = cast(PublishProfileId, "unknown_profile")

    with pytest.raises(KeyError, match="unknown_profile"):
        get_publish_profile(unknown)


def test_publish_profile_is_immutable_and_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="frozen"):
        COMPATIBLE_MP4.width = 123

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PublishProfile.model_validate(
            {
                **COMPATIBLE_MP4.model_dump(mode="json"),
                "unexpected": True,
            }
        )
