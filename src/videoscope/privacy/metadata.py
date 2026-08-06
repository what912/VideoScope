"""Sanitized private probe summaries and deterministic metadata-risk scanning."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator

from videoscope.domain import Severity
from videoscope.privacy.models import (
    PrivacyModel,
    PrivacyRisk,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_risk_id,
)
from videoscope.privacy.profiles import MetadataCategory, ShareAudienceProfile

MetadataScope = Literal[
    "global",
    "video_stream",
    "audio_stream",
    "subtitle_stream",
    "data_stream",
    "attachment",
    "chapter",
    "filename",
]

_SCANNER_VERSION = "1.0.0"
_MAX_KEY_LENGTH = 128
_MAX_VALUE_LENGTH = 1024
_WHITESPACE = re.compile(r"\s+")

_CATEGORY_KEYS: dict[MetadataCategory, frozenset[str]] = {
    "attachment": frozenset({"attachment", "filename", "mimetype"}),
    "author": frozenset(
        {"artist", "author", "copyright", "creator", "owner", "publisher"}
    ),
    "creation_time": frozenset({"creation_time", "date", "date_recorded"}),
    "device": frozenset({"camera", "device", "make", "model"}),
    "filename": frozenset({"filename"}),
    "location": frozenset(
        {
            "com.apple.quicktime.location.iso6709",
            "gps",
            "latitude",
            "location",
            "location-eng",
            "longitude",
        }
    ),
    "software": frozenset({"encoded_by", "encoder", "software", "writing_application"}),
    "title": frozenset({"comment", "description", "synopsis", "title"}),
}
_CATEGORY_PRIORITY: tuple[MetadataCategory, ...] = (
    "location",
    "author",
    "creation_time",
    "device",
    "software",
    "title",
    "attachment",
    "filename",
)
_SEVERITY: dict[MetadataCategory, Severity] = {
    "attachment": Severity.HIGH,
    "author": Severity.MEDIUM,
    "creation_time": Severity.LOW,
    "device": Severity.MEDIUM,
    "filename": Severity.LOW,
    "location": Severity.HIGH,
    "software": Severity.LOW,
    "title": Severity.LOW,
}


class PrivateProbeTagSet(PrivacyModel):
    """Sanitized tags from one private ffprobe scope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: MetadataScope
    index: int = Field(ge=0)
    tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("tags", mode="before")
    @classmethod
    def sanitize_tags(cls, value: object) -> dict[str, str]:
        return _sanitize_tags(value)


class PrivateProbeSummary(PrivacyModel):
    """Private-only structured metadata retained without raw ffprobe JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    filename: str = Field(min_length=1, max_length=_MAX_VALUE_LENGTH)
    global_tags: dict[str, str] = Field(default_factory=dict)
    stream_tags: tuple[PrivateProbeTagSet, ...] = ()
    chapter_tags: tuple[PrivateProbeTagSet, ...] = ()
    attachment_tags: tuple[PrivateProbeTagSet, ...] = ()

    @field_validator("filename", mode="before")
    @classmethod
    def sanitize_filename(cls, value: object) -> str:
        sanitized = _sanitize_value(value)
        if not sanitized:
            return "source"
        return sanitized

    @field_validator("global_tags", mode="before")
    @classmethod
    def sanitize_global_tags(cls, value: object) -> dict[str, str]:
        return _sanitize_tags(value)


def private_probe_summary_from_ffprobe(
    payload: Mapping[str, Any],
    *,
    filename: str,
    duration_seconds: float,
) -> PrivateProbeSummary:
    """Extract only bounded structured tags for a private Safe Sharing job."""
    media_format = _mapping(payload.get("format"))
    stream_tags: list[PrivateProbeTagSet] = []
    attachment_tags: list[PrivateProbeTagSet] = []
    raw_streams = payload.get("streams")
    if isinstance(raw_streams, list):
        for fallback_index, raw_stream in enumerate(raw_streams):
            stream = _mapping(raw_stream)
            tags = _sanitize_tags(stream.get("tags"))
            index = _non_negative_int(stream.get("index"), fallback_index)
            codec_type = str(stream.get("codec_type") or "data").casefold()
            disposition = _mapping(stream.get("disposition"))
            is_attached_picture = (
                _non_negative_int(disposition.get("attached_pic"), 0) == 1
            )
            if codec_type == "attachment" or is_attached_picture:
                if not tags:
                    tags = {"attachment": "attached_picture"}
                attachment_tags.append(
                    PrivateProbeTagSet(scope="attachment", index=index, tags=tags)
                )
                continue
            if not tags:
                continue
            scope: MetadataScope = {
                "audio": "audio_stream",
                "subtitle": "subtitle_stream",
                "video": "video_stream",
            }.get(codec_type, "data_stream")  # type: ignore[assignment]
            stream_tags.append(PrivateProbeTagSet(scope=scope, index=index, tags=tags))

    chapter_tags: list[PrivateProbeTagSet] = []
    raw_chapters = payload.get("chapters")
    if isinstance(raw_chapters, list):
        for fallback_index, raw_chapter in enumerate(raw_chapters):
            chapter = _mapping(raw_chapter)
            tags = _sanitize_tags(chapter.get("tags"))
            if tags:
                chapter_tags.append(
                    PrivateProbeTagSet(
                        scope="chapter",
                        index=_non_negative_int(chapter.get("id"), fallback_index),
                        tags=tags,
                    )
                )

    return PrivateProbeSummary(
        duration_seconds=duration_seconds,
        filename=filename,
        global_tags=_sanitize_tags(media_format.get("tags")),
        stream_tags=tuple(
            sorted(stream_tags, key=lambda item: (item.index, item.scope))
        ),
        chapter_tags=tuple(sorted(chapter_tags, key=lambda item: item.index)),
        attachment_tags=tuple(
            sorted(attachment_tags, key=lambda item: (item.index, item.scope))
        ),
    )


class MetadataPrivacyScanner:
    """Report removable metadata observations without publishing tag values."""

    scanner_id = "metadata_privacy"
    version = _SCANNER_VERSION

    def scan(
        self,
        metadata: PrivateProbeSummary,
        input_hash: str,
        profile: ShareAudienceProfile,
    ) -> list[PrivacyRisk]:
        """Return stable private review risks selected by profile policy."""
        observations = list(_iter_observations(metadata))
        forbidden = set(profile.forbidden_metadata_categories)
        grouped: dict[
            tuple[MetadataScope, int, MetadataCategory], list[tuple[str, str]]
        ] = {}
        for scope, index, key, value in observations:
            category = _metadata_category(scope, key)
            if category is None or category not in forbidden:
                continue
            grouped.setdefault((scope, index, category), []).append((key, value))

        risks: list[PrivacyRisk] = []
        for (scope, index, category), private_tags in sorted(
            grouped.items(), key=lambda item: item[0]
        ):
            observation_scanner = f"{self.scanner_id}:{scope}:{index}:{category}"
            risk_id = make_privacy_risk_id(
                input_hash,
                observation_scanner,
                PrivacyRiskType.METADATA,
                0.0,
                metadata.duration_seconds,
                None,
            )
            risks.append(
                PrivacyRisk(
                    id=risk_id,
                    scanner_id=observation_scanner,
                    scanner_version=self.version,
                    risk_type=PrivacyRiskType.METADATA,
                    title="Removable metadata detected",
                    public_description=(
                        f"A {category.replace('_', ' ')} metadata field was observed "
                        f"in the {scope.replace('_', ' ')} scope."
                    ),
                    severity=_SEVERITY[category],
                    confidence=1.0,
                    start_seconds=0.0,
                    end_seconds=metadata.duration_seconds,
                    metadata_scope=scope,
                    metadata_key=category,
                    recommended_style=RedactionStyle.REMOVE_METADATA,
                    limitations=(
                        "The field may be intentional; removal follows the selected "
                        "sharing profile and still requires human review.",
                    ),
                    evidence=(
                        {
                            "metadata_scope": scope,
                            "metadata_key": category,
                            "observed_fields_count": len(private_tags),
                        },
                    ),
                    private_evidence=tuple(
                        {
                            "sanitized_metadata_key": private_key,
                            "sanitized_metadata_value": private_value,
                        }
                        for private_key, private_value in sorted(private_tags)
                    ),
                )
            )
        return sorted(
            risks,
            key=lambda risk: (
                risk.metadata_scope or "",
                risk.metadata_key or "",
                risk.id,
            ),
        )


def _iter_observations(
    summary: PrivateProbeSummary,
) -> tuple[tuple[MetadataScope, int, str, str], ...]:
    observations: list[tuple[MetadataScope, int, str, str]] = [
        ("filename", 0, "filename", summary.filename)
    ]
    observations.extend(
        ("global", 0, key, value) for key, value in summary.global_tags.items()
    )
    for tag_set in (
        *summary.stream_tags,
        *summary.chapter_tags,
        *summary.attachment_tags,
    ):
        observations.extend(
            (tag_set.scope, tag_set.index, key, value)
            for key, value in tag_set.tags.items()
        )
    return tuple(sorted(observations, key=lambda item: (item[0], item[1], item[2])))


def _metadata_category(
    scope: MetadataScope,
    key: str,
) -> MetadataCategory | None:
    if scope == "attachment":
        return "attachment"
    if scope == "filename":
        return "filename"
    for category in _CATEGORY_PRIORITY:
        if key in _CATEGORY_KEYS[category]:
            return category
    return None


def _sanitize_tags(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    sanitized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _sanitize_key(raw_key)
        item = _sanitize_value(raw_value)
        if key and item:
            sanitized[key] = item
    return dict(sorted(sanitized.items()))


def _sanitize_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    text = "".join(character for character in text if not _is_control(character))
    return _WHITESPACE.sub("_", text)[:_MAX_KEY_LENGTH]


def _sanitize_value(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value))
    text = "".join(
        " " if character in "\r\n\t" else character
        for character in text
        if not _is_control(character) or character in "\r\n\t"
    )
    return _WHITESPACE.sub(" ", text).strip()[:_MAX_VALUE_LENGTH]


def _is_control(character: str) -> bool:
    return unicodedata.category(character).startswith("C")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _non_negative_int(value: object, fallback: int) -> int:
    try:
        converted = int(str(value))
    except (TypeError, ValueError):
        return fallback
    return converted if converted >= 0 else fallback


__all__ = [
    "MetadataPrivacyScanner",
    "PrivateProbeSummary",
    "PrivateProbeTagSet",
    "private_probe_summary_from_ffprobe",
]
