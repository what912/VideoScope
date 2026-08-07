"""Optional, scene-local OCR proposals for suspicious visible text."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from difflib import SequenceMatcher
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoscope.ai.models import OCRImageInput, OCRObservation
from videoscope.ai.ocr import OCRRuntimeUnavailableError, detect_with_optional_ocr
from videoscope.ai.runtime import ModelRuntimeManager
from videoscope.domain import Severity
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyRisk,
    PrivacyRiskType,
    make_privacy_risk_id,
)
from videoscope.privacy.scanners import (
    PrivacyScanContext,
    PrivacyScannerRequirements,
    PrivacyScannerSkipped,
)

_SCANNER_VERSION = "1.0.0"
_MAX_PRIVATE_TEXT_LENGTH = 4096
_SPACE = re.compile(r"\s+")
_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.IGNORECASE)
_CHINESE_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EN_LABELLED_PHONE = re.compile(
    r"(?:phone|tel(?:ephone)?|mobile)\s*:?\s*\+?[\d() -]{7,20}",
    re.IGNORECASE,
)
_ZH_LABELLED_PHONE = re.compile(r"(?:电话|手机)\s*[:：]?\s*[\d() -]{7,20}")
_ZH_ADDRESS = re.compile(
    r"[\u4e00-\u9fff]{2,}(?:省|市|区|县|镇|乡|村|路|街道|大道|弄)"
    r"[\u4e00-\u9fffA-Za-z0-9 -]{0,80}\d+号"
)
_EN_ADDRESS = re.compile(
    r"\d+\s+[\w .'-]+\s(?:street|st|road|rd|avenue|ave|lane|ln)\b",
    re.IGNORECASE,
)
_EN_ACCOUNT = re.compile(
    r"(?:account|acct)\s*[:#]?\s*[A-Z0-9][A-Z0-9 -]{5,30}",
    re.IGNORECASE,
)
_ZH_ACCOUNT = re.compile(
    r"(?:账号|账户|卡号|银行卡)\s*[:：#]?\s*[A-Z0-9][A-Z0-9 -]{5,30}",
    re.IGNORECASE,
)
_EN_CODE = re.compile(
    r"(?:verification\s+code|one[- ]time\s+(?:password|code)|otp)"
    r"\s*:?\s*\d{4,8}\b",
    re.IGNORECASE,
)
_ZH_CODE = re.compile(r"验证码\s*[:：]?\s*\d{4,8}\b")
_WINDOWS_PATH = re.compile(r"\b[A-Za-z]:\\(?:[^\\\s]+\\)+[^\\\s]+")
_POSIX_PRIVATE_PATH = re.compile(
    r"/(?:Users|home|var|private|Documents|Desktop)/[^\s]+",
    re.IGNORECASE,
)
_URL = re.compile(r"\bhttps?://[^\s<>]+", re.IGNORECASE)


class _TextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SuspiciousTextKind(StrEnum):
    PHONE = "phone"
    EMAIL = "email"
    ADDRESS = "address"
    ACCOUNT = "account"
    CODE = "code"
    PATH = "path"
    URL = "url"


class SuspiciousTextConfig(_TextModel):
    """Conservative OCR filtering and scene-local association thresholds."""

    provider_id: str = Field(default="paddleocr", min_length=1)
    model_id: str = Field(default="PP-OCRv5-mobile/ch", min_length=1)
    locale: Literal["zh-CN", "en"] = "zh-CN"
    minimum_confidence: float = Field(default=0.75, ge=0, le=1)
    tracking_minimum_confidence: float = Field(default=0.5, ge=0, le=1)
    minimum_repeated_observations: int = Field(default=2, ge=2, le=100)
    tracking_iou: float = Field(default=0.25, ge=0, le=1)
    text_similarity: float = Field(default=0.75, ge=0, le=1)
    maximum_gap_seconds: float = Field(default=0.75, ge=0)
    guard_seconds: float = Field(default=0.1, ge=0)
    maximum_risks: int = Field(default=100, ge=1, le=10_000)


class TextObservation(_TextModel):
    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    sample_index: int = Field(ge=0)
    scene_index: int = Field(ge=0)
    relative_path: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=_MAX_PRIVATE_TEXT_LENGTH, exclude=True)
    private_texts: tuple[str, ...] = Field(min_length=1, exclude=True)
    normalized_text: str = Field(
        min_length=1,
        max_length=_MAX_PRIVATE_TEXT_LENGTH,
        exclude=True,
    )
    kind: SuspiciousTextKind
    box: NormalizedBox
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class TextRegionTrack(_TextModel):
    anonymous_id: str = Field(pattern=r"^text_track_[0-9]{2,}$")
    kind: SuspiciousTextKind
    scene_index: int = Field(ge=0)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    box: NormalizedBox
    observations: tuple[TextObservation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_track(self) -> Self:
        ordered = tuple(sorted(self.observations, key=_observation_sort_key))
        if ordered != self.observations:
            raise ValueError("text observations must be deterministically sorted")
        if any(item.scene_index != self.scene_index for item in ordered):
            raise ValueError("text track cannot cross a scene boundary")
        if any(item.kind is not self.kind for item in ordered):
            raise ValueError("text track observations must share one category")
        if self.start_seconds != ordered[0].timestamp_seconds:
            raise ValueError("text track start must match its first observation")
        if self.end_seconds != ordered[-1].timestamp_seconds:
            raise ValueError("text track end must match its final observation")
        return self


def classify_private_text(text: str, locale: str) -> SuspiciousTextKind | None:
    """Classify only explicit, conservative sensitive-text patterns."""
    if locale not in {"zh-CN", "en"}:
        raise ValueError("locale must be 'zh-CN' or 'en'")
    normalized = _normalize_text(text)
    if not normalized:
        return None
    common_checks = (
        (SuspiciousTextKind.EMAIL, _EMAIL),
        (SuspiciousTextKind.PHONE, _CHINESE_MOBILE),
        (SuspiciousTextKind.URL, _URL),
        (SuspiciousTextKind.PATH, _WINDOWS_PATH),
        (SuspiciousTextKind.PATH, _POSIX_PRIVATE_PATH),
    )
    localized_checks = (
        (
            (SuspiciousTextKind.PHONE, _ZH_LABELLED_PHONE),
            (SuspiciousTextKind.ADDRESS, _ZH_ADDRESS),
            (SuspiciousTextKind.ACCOUNT, _ZH_ACCOUNT),
            (SuspiciousTextKind.CODE, _ZH_CODE),
        )
        if locale == "zh-CN"
        else (
            (SuspiciousTextKind.PHONE, _EN_LABELLED_PHONE),
            (SuspiciousTextKind.ADDRESS, _EN_ADDRESS),
            (SuspiciousTextKind.ACCOUNT, _EN_ACCOUNT),
            (SuspiciousTextKind.CODE, _EN_CODE),
        )
    )
    for kind, pattern in (*common_checks, *localized_checks):
        if pattern.search(normalized):
            return kind
    return None


class SuspiciousTextScanner:
    """Propose visible suspicious text without publishing recognized content."""

    id = "suspicious_text"
    display_name = "Suspicious text proposals"
    version = _SCANNER_VERSION
    description = "Proposes OCR text regions that may expose private information."
    requirements = PrivacyScannerRequirements(
        optional_packages=("paddleocr", "paddlepaddle"),
        estimated_cost="high",
    )
    config_model: type[BaseModel] = SuspiciousTextConfig

    def __init__(self, runtime: ModelRuntimeManager | None) -> None:
        self._runtime = runtime

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        settings = SuspiciousTextConfig.model_validate(config.model_dump())
        if self._runtime is None:
            raise PrivacyScannerSkipped(fallback="manual_visual_region")
        images = tuple(
            OCRImageInput(
                path=context.resolve_frame_path(sample.relative_path),
                timestamp_seconds=sample.timestamp_seconds,
            )
            for sample in sorted(
                context.frame_samples,
                key=lambda item: (item.timestamp_seconds, item.sample_index),
            )
        )
        if not images:
            return []
        try:
            batch = detect_with_optional_ocr(
                self._runtime,
                provider_id=settings.provider_id,
                model_id=settings.model_id,
                images=images,
            )
        except OCRRuntimeUnavailableError as exc:
            raise PrivacyScannerSkipped(fallback="manual_visual_region") from exc
        observations = _prepare_observations(context, batch.observations, settings)
        tracks = tuple(
            track
            for track in track_text_regions(observations, settings)
            if _track_has_sufficient_confidence(track, settings)
        )
        return _risks_from_tracks(
            context,
            tracks[: settings.maximum_risks],
            settings.guard_seconds,
        )


def track_text_regions(
    observations: Iterable[TextObservation],
    config: SuspiciousTextConfig,
) -> tuple[TextRegionTrack, ...]:
    """Associate OCR rectangles only within one scene and sensitive category."""
    ordered = sorted(observations, key=_observation_sort_key)
    mutable: list[list[TextObservation]] = []
    for observation in ordered:
        candidates: list[tuple[float, float, int]] = []
        for index, items in enumerate(mutable):
            previous = items[-1]
            if previous.scene_index != observation.scene_index:
                continue
            if previous.kind is not observation.kind:
                continue
            if previous.sample_index == observation.sample_index:
                continue
            gap = observation.timestamp_seconds - previous.timestamp_seconds
            if gap < 0 or gap > config.maximum_gap_seconds:
                continue
            overlap = _intersection_over_union(previous.box, observation.box)
            if overlap < config.tracking_iou:
                continue
            similarity = SequenceMatcher(
                None,
                previous.normalized_text,
                observation.normalized_text,
            ).ratio()
            if similarity < config.text_similarity:
                continue
            candidates.append((-overlap, -similarity, index))
        if candidates:
            mutable[min(candidates)[-1]].append(observation)
        else:
            mutable.append([observation])

    prepared = sorted(mutable, key=lambda items: _observation_sort_key(items[0]))
    return tuple(
        TextRegionTrack(
            anonymous_id=f"text_track_{index:02d}",
            kind=items[0].kind,
            scene_index=items[0].scene_index,
            start_seconds=items[0].timestamp_seconds,
            end_seconds=items[-1].timestamp_seconds,
            box=_union_boxes(item.box for item in items),
            observations=tuple(items),
        )
        for index, items in enumerate(prepared, start=1)
    )


def _prepare_observations(
    context: PrivacyScanContext,
    raw_observations: Sequence[OCRObservation],
    config: SuspiciousTextConfig,
) -> tuple[TextObservation, ...]:
    samples = {sample.timestamp_seconds: sample for sample in context.frame_samples}
    prepared: list[TextObservation] = []
    for observation in raw_observations:
        if observation.confidence < config.tracking_minimum_confidence:
            continue
        normalized = _normalize_text(observation.text)
        kind = classify_private_text(normalized, config.locale)
        sample = samples.get(observation.timestamp_seconds)
        if kind is None or sample is None:
            continue
        prepared.append(
            TextObservation(
                timestamp_seconds=observation.timestamp_seconds,
                sample_index=sample.sample_index,
                scene_index=_scene_index(context, observation.timestamp_seconds),
                relative_path=sample.relative_path,
                text=observation.text[:_MAX_PRIVATE_TEXT_LENGTH],
                private_texts=(observation.text[:_MAX_PRIVATE_TEXT_LENGTH],),
                normalized_text=normalized[:_MAX_PRIVATE_TEXT_LENGTH],
                kind=kind,
                box=NormalizedBox.model_validate(
                    observation.bounding_box.model_dump(mode="python")
                ),
                confidence=observation.confidence,
            )
        )
    return _aggregate_same_frame_observations(prepared)


def _aggregate_same_frame_observations(
    observations: Iterable[TextObservation],
) -> tuple[TextObservation, ...]:
    grouped: dict[
        tuple[int, float, int, float, float, float, float],
        list[TextObservation],
    ] = {}
    for observation in observations:
        key = (
            observation.scene_index,
            observation.timestamp_seconds,
            observation.sample_index,
            observation.box.x_min,
            observation.box.y_min,
            observation.box.x_max,
            observation.box.y_max,
        )
        grouped.setdefault(key, []).append(observation)

    aggregated: list[TextObservation] = []
    for items in grouped.values():
        canonical = min(
            items,
            key=lambda item: (
                -item.confidence,
                item.kind.value,
                item.normalized_text,
                item.text,
            ),
        )
        aggregated.append(
            canonical.model_copy(
                update={
                    "private_texts": tuple(
                        sorted(
                            {
                                private_text
                                for item in items
                                for private_text in item.private_texts
                            }
                        )
                    )
                }
            )
        )
    return tuple(sorted(aggregated, key=_observation_sort_key))


def _track_has_sufficient_confidence(
    track: TextRegionTrack,
    config: SuspiciousTextConfig,
) -> bool:
    if any(item.confidence >= config.minimum_confidence for item in track.observations):
        return True
    consecutive = 1
    for previous, current in zip(
        track.observations,
        track.observations[1:],
        strict=False,
    ):
        consecutive = (
            consecutive + 1 if current.sample_index == previous.sample_index + 1 else 1
        )
        if consecutive >= config.minimum_repeated_observations:
            return True
    return False


def _risks_from_tracks(
    context: PrivacyScanContext,
    tracks: Sequence[TextRegionTrack],
    guard_seconds: float,
) -> list[PrivacyRisk]:
    risks: list[PrivacyRisk] = []
    for track in tracks:
        scene_start, scene_end = _scene_bounds(context, track.scene_index)
        start = max(scene_start, track.start_seconds - guard_seconds)
        end = min(scene_end, track.end_seconds + guard_seconds)
        risks.append(
            PrivacyRisk(
                id=make_privacy_risk_id(
                    context.input_hash,
                    SuspiciousTextScanner.id,
                    PrivacyRiskType.SUSPICIOUS_TEXT,
                    start,
                    end,
                    track.box,
                ),
                scanner_id=SuspiciousTextScanner.id,
                scanner_version=SuspiciousTextScanner.version,
                risk_type=PrivacyRiskType.SUSPICIOUS_TEXT,
                title="Suspicious text region proposed for review",
                public_description=(
                    f"OCR proposed a possible {track.kind.value}-like text region "
                    "for manual review."
                ),
                severity=Severity.MEDIUM,
                confidence=sum(item.confidence for item in track.observations)
                / len(track.observations),
                start_seconds=start,
                end_seconds=end,
                box=track.box,
                track_id=track.anonymous_id,
                recommended_style=context.profile.default_visual_style,
                limitations=(
                    "OCR recognition errors can create false positives.",
                    "Language, stylized text, motion, blur, and low resolution can "
                    "cause missed text.",
                    "Every proposal requires manual visual review before sharing.",
                ),
                evidence=tuple(
                    {
                        "category": item.kind.value,
                        "timestamp_seconds": item.timestamp_seconds,
                        "sample_index": item.sample_index,
                        "relative_path": item.relative_path,
                        "box": item.box.model_dump(mode="json"),
                        "ocr_confidence": item.confidence,
                    }
                    for item in track.observations
                ),
                private_evidence=tuple(
                    {
                        "timestamp_seconds": item.timestamp_seconds,
                        "ocr_text": private_text,
                    }
                    for item in track.observations
                    for private_text in item.private_texts
                ),
            )
        )
    return risks


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = "".join(
        " " if character in "\r\n\t" else character
        for character in normalized
        if not unicodedata.category(character).startswith("C") or character in "\r\n\t"
    )
    return _SPACE.sub(" ", normalized).strip()


def _scene_index(context: PrivacyScanContext, timestamp: float) -> int:
    if not context.scenes:
        return 0
    for index, scene in enumerate(context.scenes):
        if scene.start_seconds <= timestamp < scene.end_seconds or (
            index == len(context.scenes) - 1 and timestamp == scene.end_seconds
        ):
            return int(scene.scene_index)
    raise ValueError("OCR timestamp is outside declared scene intervals")


def _scene_bounds(context: PrivacyScanContext, scene_index: int) -> tuple[float, float]:
    if not context.scenes:
        return (0.0, context.duration_seconds)
    for scene in context.scenes:
        if scene.scene_index == scene_index:
            return (scene.start_seconds, scene.end_seconds)
    raise ValueError("text track references an unknown scene")


def _intersection_over_union(first: NormalizedBox, second: NormalizedBox) -> float:
    x_min = max(first.x_min, second.x_min)
    y_min = max(first.y_min, second.y_min)
    x_max = min(first.x_max, second.x_max)
    y_max = min(first.y_max, second.y_max)
    intersection = max(0.0, x_max - x_min) * max(0.0, y_max - y_min)
    first_area = (first.x_max - first.x_min) * (first.y_max - first.y_min)
    second_area = (second.x_max - second.x_min) * (second.y_max - second.y_min)
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def _union_boxes(boxes: Iterable[NormalizedBox]) -> NormalizedBox:
    values = tuple(boxes)
    if not values:
        raise ValueError("cannot union an empty box sequence")
    return NormalizedBox(
        x_min=min(item.x_min for item in values),
        y_min=min(item.y_min for item in values),
        x_max=max(item.x_max for item in values),
        y_max=max(item.y_max for item in values),
    )


def _observation_sort_key(
    observation: TextObservation,
) -> tuple[int, float, int, str, float, float, str]:
    return (
        observation.scene_index,
        observation.timestamp_seconds,
        observation.sample_index,
        observation.kind.value,
        observation.box.y_min,
        observation.box.x_min,
        observation.normalized_text,
    )


__all__ = [
    "SuspiciousTextConfig",
    "SuspiciousTextKind",
    "SuspiciousTextScanner",
    "TextObservation",
    "TextRegionTrack",
    "classify_private_text",
    "track_text_regions",
]
