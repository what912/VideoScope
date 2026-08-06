"""Immutable, versioned Safe Sharing audience-profile catalog."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from videoscope.privacy.models import PrivacyModel, RedactionStyle

MetadataCategory = Literal[
    "attachment",
    "author",
    "creation_time",
    "device",
    "filename",
    "location",
    "software",
    "title",
]
ManualReviewCategory = Literal["audio", "metadata", "qr_barcode", "text", "visual"]
QrHandling = Literal["review", "redact_by_default"]

KNOWN_METADATA_CATEGORIES = frozenset(
    {
        "attachment",
        "author",
        "creation_time",
        "device",
        "filename",
        "location",
        "software",
        "title",
    }
)
KNOWN_MANUAL_REVIEW_CATEGORIES = frozenset(
    {"audio", "metadata", "qr_barcode", "text", "visual"}
)


class ShareAudienceProfile(PrivacyModel):
    """One immutable policy for preparing a video for a stated audience."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    version: str = Field(pattern=r"^\d+$")
    forbidden_metadata_categories: tuple[MetadataCategory, ...]
    required_manual_review_categories: tuple[ManualReviewCategory, ...]
    default_visual_style: RedactionStyle
    qr_handling: QrHandling
    final_human_review_required: bool

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        """Reject duplicate or medium-incompatible policy values."""
        metadata = tuple(self.forbidden_metadata_categories)
        manual = tuple(self.required_manual_review_categories)
        if len(metadata) != len(set(metadata)):
            raise ValueError("duplicate forbidden metadata category")
        if len(manual) != len(set(manual)):
            raise ValueError("duplicate manual review category")
        if self.default_visual_style not in {
            RedactionStyle.BLUR,
            RedactionStyle.PIXELATE,
            RedactionStyle.SOLID_FILL,
        }:
            raise ValueError("default visual style must be a region redaction style")
        return self


_ALL_METADATA: tuple[MetadataCategory, ...] = (
    "attachment",
    "author",
    "creation_time",
    "device",
    "filename",
    "location",
    "software",
    "title",
)
_STRICT_MANUAL: tuple[ManualReviewCategory, ...] = (
    "metadata",
    "visual",
    "qr_barcode",
    "text",
    "audio",
)

PUBLIC = ShareAudienceProfile(
    id="public",
    version="1",
    forbidden_metadata_categories=_ALL_METADATA,
    required_manual_review_categories=_STRICT_MANUAL,
    default_visual_style=RedactionStyle.BLUR,
    qr_handling="redact_by_default",
    final_human_review_required=True,
)
WORK_CLIENT = ShareAudienceProfile(
    id="work_client",
    version="1",
    forbidden_metadata_categories=(
        "attachment",
        "author",
        "creation_time",
        "device",
        "filename",
        "location",
        "software",
    ),
    required_manual_review_categories=_STRICT_MANUAL,
    default_visual_style=RedactionStyle.BLUR,
    qr_handling="review",
    final_human_review_required=True,
)
SCHOOL = ShareAudienceProfile(
    id="school",
    version="1",
    forbidden_metadata_categories=(
        "attachment",
        "author",
        "creation_time",
        "device",
        "filename",
        "location",
    ),
    required_manual_review_categories=_STRICT_MANUAL,
    default_visual_style=RedactionStyle.SOLID_FILL,
    qr_handling="redact_by_default",
    final_human_review_required=True,
)
FAMILY = ShareAudienceProfile(
    id="family",
    version="1",
    forbidden_metadata_categories=(
        "attachment",
        "creation_time",
        "device",
        "filename",
        "location",
    ),
    required_manual_review_categories=("metadata", "visual", "audio"),
    default_visual_style=RedactionStyle.BLUR,
    qr_handling="review",
    final_human_review_required=True,
)
EXTERNAL_AI = ShareAudienceProfile(
    id="external_ai",
    version="1",
    forbidden_metadata_categories=_ALL_METADATA,
    required_manual_review_categories=_STRICT_MANUAL,
    default_visual_style=RedactionStyle.SOLID_FILL,
    qr_handling="redact_by_default",
    final_human_review_required=True,
)


def _build_catalog(
    profiles: Iterable[ShareAudienceProfile],
) -> Mapping[str, ShareAudienceProfile]:
    catalog: dict[str, ShareAudienceProfile] = {}
    for profile in profiles:
        if profile.id in catalog:
            raise ValueError(f"duplicate Safe Sharing audience profile: {profile.id}")
        catalog[profile.id] = profile
    return MappingProxyType(catalog)


_PROFILE_CATALOG = _build_catalog((PUBLIC, WORK_CLIENT, SCHOOL, FAMILY, EXTERNAL_AI))


def list_share_audience_profiles() -> tuple[ShareAudienceProfile, ...]:
    """Return built-in profiles in their stable user-facing order."""
    return tuple(_PROFILE_CATALOG.values())


def get_share_audience_profile(profile_id: str) -> ShareAudienceProfile:
    """Return exactly one profile without guessing an unsupported fallback."""
    try:
        return _PROFILE_CATALOG[profile_id]
    except KeyError as exc:
        raise KeyError(f"unknown Safe Sharing audience profile: {profile_id}") from exc


__all__ = [
    "EXTERNAL_AI",
    "FAMILY",
    "KNOWN_METADATA_CATEGORIES",
    "PUBLIC",
    "SCHOOL",
    "WORK_CLIENT",
    "ShareAudienceProfile",
    "get_share_audience_profile",
    "list_share_audience_profiles",
]
