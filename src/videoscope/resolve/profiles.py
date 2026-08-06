"""Immutable, versioned Publish Ready profile catalog."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Self

from pydantic import ConfigDict, Field, model_validator

from videoscope.resolve.models import PublishProfileId, ResolveModel


class PublishProfile(ResolveModel):
    """One versioned set of deterministic output compatibility requirements."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: PublishProfileId
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    maximum_fps: float = Field(gt=0, allow_inf_nan=False)
    video_codec: str = Field(min_length=1)
    audio_codec: str = Field(min_length=1)
    pixel_format: str = Field(min_length=1)
    container: str = Field(min_length=1)

    @model_validator(mode="after")
    def require_complete_canvas(self) -> Self:
        """Require target width and height together or preserve source dimensions."""
        if (self.width is None) != (self.height is None):
            raise ValueError("PublishProfile width and height must be set together")
        return self


COMPATIBLE_MP4 = PublishProfile(
    id=PublishProfileId.COMPATIBLE_MP4,
    version="1.0.0",
    width=None,
    height=None,
    maximum_fps=60.0,
    video_codec="h264",
    audio_codec="aac",
    pixel_format="yuv420p",
    container="mp4",
)

SOCIAL_VERTICAL = COMPATIBLE_MP4.model_copy(
    update={
        "id": PublishProfileId.SOCIAL_VERTICAL,
        "width": 1080,
        "height": 1920,
    }
)

SOCIAL_HORIZONTAL = COMPATIBLE_MP4.model_copy(
    update={
        "id": PublishProfileId.SOCIAL_HORIZONTAL,
        "width": 1920,
        "height": 1080,
    }
)


def _build_profile_catalog(
    profiles: Iterable[PublishProfile],
) -> Mapping[PublishProfileId, PublishProfile]:
    """Build an insertion-ordered immutable catalog and reject duplicate IDs."""
    catalog: dict[PublishProfileId, PublishProfile] = {}
    for profile in profiles:
        if profile.id in catalog:
            raise ValueError(f"duplicate PublishProfile id: {profile.id.value}")
        catalog[profile.id] = profile
    return MappingProxyType(catalog)


_PROFILE_CATALOG = _build_profile_catalog(
    (COMPATIBLE_MP4, SOCIAL_VERTICAL, SOCIAL_HORIZONTAL)
)


def list_publish_profiles() -> tuple[PublishProfile, ...]:
    """Return the exact built-in profiles in their stable public order."""
    return tuple(_PROFILE_CATALOG.values())


def get_publish_profile(profile_id: PublishProfileId) -> PublishProfile:
    """Return one built-in profile or reject an unsupported identifier."""
    try:
        return _PROFILE_CATALOG[profile_id]
    except KeyError as exc:
        raise KeyError(f"unknown PublishProfile id: {profile_id}") from exc


__all__ = [
    "COMPATIBLE_MP4",
    "SOCIAL_HORIZONTAL",
    "SOCIAL_VERTICAL",
    "PublishProfile",
    "get_publish_profile",
    "list_publish_profiles",
]
