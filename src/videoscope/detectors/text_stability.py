"""Optional scene-local temporal OCR stability diagnostics."""

from __future__ import annotations

import math
import statistics
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoscope.ai import (
    MODEL_RUNTIME_CACHE_KEY,
    ModelRuntimeManager,
    NormalizedBoundingBox,
    OCRImageInput,
    OCRObservation,
)
from videoscope.ai.providers import (
    PADDLEOCR_CHINESE_MODEL_ID,
    PADDLEOCR_ENGLISH_MODEL_ID,
    PADDLEOCR_PREPROCESSING_VERSION,
    PADDLEOCR_PROVIDER_ID,
)
from videoscope.detectors.image_features import resolve_sample_path
from videoscope.detectors.models import (
    DETECTOR_DIAGNOSTICS_CACHE_KEY,
    AnalysisContext,
    DetectorRequirements,
    EstimatedCost,
)
from videoscope.domain import (
    Evidence,
    Finding,
    Severity,
    TimeRange,
    make_finding_id,
)
from videoscope.scenes import VideoScene
from videoscope.video import FrameSample

_TIME_TOLERANCE = 1e-9
LIMITATIONS = (
    "OCR recognition errors can themselves create apparent temporal instability.",
    "Stylized, curved, very small, occluded, or motion-blurred text may be "
    "recognized inconsistently.",
    "Normal content changes with timing unlike the configured subtitle rules "
    "may still require human review.",
)


class TextStabilityConfig(BaseModel):
    """OCR model selection, tracking gates, and instability thresholds."""

    model_config = ConfigDict(extra="forbid")

    language: Literal["ch", "en"] = "ch"
    provider_id: str = PADDLEOCR_PROVIDER_ID
    model_id: str | None = None
    preprocessing_version: str = PADDLEOCR_PREPROCESSING_VERSION
    minimum_ocr_confidence: float = Field(
        default=0.65,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    minimum_box_iou: float = Field(default=0.35, ge=0, le=1, allow_inf_nan=False)
    high_iou_text_override: float = Field(
        default=0.75,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    minimum_track_text_similarity: float = Field(
        default=0.3,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    stable_text_similarity: float = Field(
        default=0.8,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    maximum_track_gap_seconds: float = Field(
        default=1.25,
        gt=0,
        allow_inf_nan=False,
    )
    minimum_track_observations: int = Field(default=3, ge=2)
    minimum_text_changes: int = Field(default=2, ge=2)
    minimum_edit_distance: int = Field(default=1, ge=1)
    missing_gap_factor: float = Field(default=1.5, gt=1, allow_inf_nan=False)
    maximum_flash_observations: int = Field(default=2, ge=1)
    maximum_flash_duration_seconds: float = Field(
        default=0.75,
        gt=0,
        allow_inf_nan=False,
    )
    flash_boundary_guard_seconds: float = Field(
        default=0.5,
        ge=0,
        allow_inf_nan=False,
    )
    scrolling_displacement_threshold: float = Field(
        default=0.18,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    scrolling_monotonic_ratio: float = Field(
        default=0.8,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    severity: Severity = Severity.MEDIUM

    @model_validator(mode="after")
    def resolve_model(self) -> Self:
        """Resolve the stable built-in model identity for the language."""
        if not self.provider_id.strip():
            raise ValueError("provider_id must not be blank")
        if not self.preprocessing_version.strip():
            raise ValueError("preprocessing_version must not be blank")
        if self.model_id is None:
            if self.provider_id != PADDLEOCR_PROVIDER_ID:
                raise ValueError("custom OCR providers require model_id")
            self.model_id = (
                PADDLEOCR_CHINESE_MODEL_ID
                if self.language == "ch"
                else PADDLEOCR_ENGLISH_MODEL_ID
            )
        elif not self.model_id.strip():
            raise ValueError("model_id must not be blank")
        if self.high_iou_text_override < self.minimum_box_iou:
            raise ValueError("high_iou_text_override must not be below minimum_box_iou")
        return self

    def resolved_model_id(self) -> str:
        """Return the validated OCR model ID."""
        if self.model_id is None:
            raise ValueError("model_id was not resolved")
        return self.model_id


@dataclass(slots=True)
class TextTrack:
    """A deterministic sequence of overlapping OCR observations in one scene."""

    scene: VideoScene
    observations: list[OCRObservation] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TextTransition:
    """One textual change inside a matched spatial track."""

    before: OCRObservation
    after: OCRObservation
    edit_distance: int
    similarity: float


@dataclass(frozen=True, slots=True)
class TextInstabilityCandidate:
    """One track-level observable temporal instability."""

    scene: VideoScene
    reason: str
    start_seconds: float
    end_seconds: float
    evidence_observations: tuple[OCRObservation | None, ...]
    evidence_timestamps: tuple[float, ...]
    edit_distance: int
    text_similarity: float
    track_observation_count: int
    change_count: int


def normalize_ocr_text(value: str) -> str:
    """Normalize OCR text for deterministic edit-distance comparisons."""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(normalized.split())


def levenshtein_distance(left: str, right: str) -> int:
    """Return Unicode-codepoint edit distance with bounded row memory."""
    left_normalized = normalize_ocr_text(left)
    right_normalized = normalize_ocr_text(right)
    if len(left_normalized) < len(right_normalized):
        left_normalized, right_normalized = right_normalized, left_normalized
    previous = list(range(len(right_normalized) + 1))
    for left_index, left_character in enumerate(left_normalized, start=1):
        current = [left_index]
        for right_index, right_character in enumerate(
            right_normalized,
            start=1,
        ):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_character != right_character),
                )
            )
        previous = current
    return previous[-1]


def text_similarity(left: str, right: str) -> float:
    """Map edit distance to a normalized similarity in ``[0, 1]``."""
    normalized_left = normalize_ocr_text(left)
    normalized_right = normalize_ocr_text(right)
    denominator = max(len(normalized_left), len(normalized_right))
    if denominator == 0:
        return 1.0
    return 1.0 - levenshtein_distance(left, right) / denominator


def bounding_box_iou(
    left: NormalizedBoundingBox,
    right: NormalizedBoundingBox,
) -> float:
    """Return intersection-over-union for two normalized text boxes."""
    intersection_width = max(
        0.0,
        min(left.x_max, right.x_max) - max(left.x_min, right.x_min),
    )
    intersection_height = max(
        0.0,
        min(left.y_max, right.y_max) - max(left.y_min, right.y_min),
    )
    intersection = intersection_width * intersection_height
    left_area = (left.x_max - left.x_min) * (left.y_max - left.y_min)
    right_area = (right.x_max - right.x_min) * (right.y_max - right.y_min)
    union = left_area + right_area - intersection
    return 0.0 if union <= 0 else intersection / union


def _observation_scene(
    observation: OCRObservation,
    scenes: tuple[VideoScene, ...],
    *,
    video_duration_seconds: float,
) -> VideoScene | None:
    for scene in scenes:
        is_final = math.isclose(
            scene.end_seconds,
            video_duration_seconds,
            rel_tol=0,
            abs_tol=_TIME_TOLERANCE,
        )
        if scene.start_seconds <= observation.timestamp_seconds and (
            observation.timestamp_seconds < scene.end_seconds - _TIME_TOLERANCE
            or (
                is_final
                and observation.timestamp_seconds <= scene.end_seconds + _TIME_TOLERANCE
            )
        ):
            return scene
    return None


def build_text_tracks(
    observations: tuple[OCRObservation, ...],
    scenes: tuple[VideoScene, ...],
    *,
    video_duration_seconds: float,
    minimum_confidence: float,
    minimum_box_iou: float,
    high_iou_text_override: float,
    minimum_text_similarity: float,
    maximum_gap_seconds: float,
) -> tuple[TextTrack, ...]:
    """Greedily match text by scene, time, box overlap, and text similarity."""
    tracks: list[TextTrack] = []
    for scene in scenes:
        scene_observations = sorted(
            (
                observation
                for observation in observations
                if observation.confidence >= minimum_confidence
                and _observation_scene(
                    observation,
                    (scene,),
                    video_duration_seconds=video_duration_seconds,
                )
                is not None
            ),
            key=lambda item: (
                item.timestamp_seconds,
                item.bounding_box.y_min,
                item.bounding_box.x_min,
                item.text,
            ),
        )
        by_timestamp: dict[float, list[OCRObservation]] = {}
        for observation in scene_observations:
            by_timestamp.setdefault(observation.timestamp_seconds, []).append(
                observation
            )
        scene_tracks: list[TextTrack] = []
        for timestamp in sorted(by_timestamp):
            detections = by_timestamp[timestamp]
            choices: list[tuple[float, float, float, int, int]] = []
            for track_index, track in enumerate(scene_tracks):
                previous = track.observations[-1]
                gap = timestamp - previous.timestamp_seconds
                if gap <= 0 or gap > maximum_gap_seconds:
                    continue
                for detection_index, detection in enumerate(detections):
                    overlap = bounding_box_iou(
                        previous.bounding_box,
                        detection.bounding_box,
                    )
                    similarity = text_similarity(previous.text, detection.text)
                    if overlap < minimum_box_iou or (
                        similarity < minimum_text_similarity
                        and overlap < high_iou_text_override
                    ):
                        continue
                    choices.append(
                        (
                            overlap * 0.7 + similarity * 0.3,
                            overlap,
                            similarity,
                            track_index,
                            detection_index,
                        )
                    )
            used_tracks: set[int] = set()
            used_detections: set[int] = set()
            for _, _, _, track_index, detection_index in sorted(
                choices,
                key=lambda item: (
                    -item[0],
                    -item[1],
                    -item[2],
                    item[3],
                    item[4],
                ),
            ):
                if track_index in used_tracks or detection_index in used_detections:
                    continue
                scene_tracks[track_index].observations.append(
                    detections[detection_index]
                )
                used_tracks.add(track_index)
                used_detections.add(detection_index)
            for detection_index, detection in enumerate(detections):
                if detection_index not in used_detections:
                    scene_tracks.append(
                        TextTrack(scene=scene, observations=[detection])
                    )
        tracks.extend(scene_tracks)
    return tuple(
        sorted(
            tracks,
            key=lambda track: (
                track.scene.scene_index,
                track.observations[0].timestamp_seconds,
                track.observations[0].bounding_box.y_min,
                track.observations[0].bounding_box.x_min,
            ),
        )
    )


def _track_is_scrolling(
    track: TextTrack,
    *,
    displacement_threshold: float,
    monotonic_ratio: float,
) -> bool:
    if len(track.observations) < 3:
        return False
    centers = [
        (
            (item.bounding_box.x_min + item.bounding_box.x_max) / 2.0,
            (item.bounding_box.y_min + item.bounding_box.y_max) / 2.0,
        )
        for item in track.observations
    ]
    total_displacement = math.dist(centers[0], centers[-1])
    if total_displacement < displacement_threshold:
        return False
    x_steps = [right[0] - left[0] for left, right in zip(centers, centers[1:])]
    y_steps = [right[1] - left[1] for left, right in zip(centers, centers[1:])]

    def directional_ratio(steps: list[float]) -> float:
        meaningful = [step for step in steps if abs(step) > _TIME_TOLERANCE]
        if not meaningful:
            return 0.0
        positive = sum(step > 0 for step in meaningful)
        negative = sum(step < 0 for step in meaningful)
        return max(positive, negative) / len(meaningful)

    return max(directional_ratio(x_steps), directional_ratio(y_steps)) >= (
        monotonic_ratio
    )


def _track_transitions(track: TextTrack) -> tuple[TextTransition, ...]:
    transitions: list[TextTransition] = []
    for before, after in zip(
        track.observations,
        track.observations[1:],
    ):
        distance = levenshtein_distance(before.text, after.text)
        if distance == 0:
            continue
        transitions.append(
            TextTransition(
                before=before,
                after=after,
                edit_distance=distance,
                similarity=text_similarity(before.text, after.text),
            )
        )
    return tuple(transitions)


def _scene_sample_times(
    scene: VideoScene,
    samples: tuple[FrameSample, ...],
    *,
    video_duration_seconds: float,
) -> tuple[float, ...]:
    is_final = math.isclose(
        scene.end_seconds,
        video_duration_seconds,
        rel_tol=0,
        abs_tol=_TIME_TOLERANCE,
    )
    return tuple(
        sample.timestamp_seconds
        for sample in samples
        if scene.start_seconds <= sample.timestamp_seconds
        and (
            sample.timestamp_seconds < scene.end_seconds - _TIME_TOLERANCE
            or (
                is_final
                and sample.timestamp_seconds <= scene.end_seconds + _TIME_TOLERANCE
            )
        )
    )


def find_track_instability(
    track: TextTrack,
    samples: tuple[FrameSample, ...],
    *,
    video_duration_seconds: float,
    config: TextStabilityConfig,
) -> TextInstabilityCandidate | None:
    """Classify one non-scrolling track into one strongest instability."""
    if _track_is_scrolling(
        track,
        displacement_threshold=config.scrolling_displacement_threshold,
        monotonic_ratio=config.scrolling_monotonic_ratio,
    ):
        return None
    observations = track.observations
    scene_times = _scene_sample_times(
        track.scene,
        samples,
        video_duration_seconds=video_duration_seconds,
    )
    transitions = _track_transitions(track)
    if (
        len(observations) >= config.minimum_track_observations
        and len(transitions) >= config.minimum_text_changes
    ):
        peak = max(
            transitions,
            key=lambda item: (
                item.edit_distance,
                -item.similarity,
                -item.before.timestamp_seconds,
            ),
        )
        if peak.edit_distance >= config.minimum_edit_distance:
            return TextInstabilityCandidate(
                scene=track.scene,
                reason="frequent_text_change",
                start_seconds=transitions[0].before.timestamp_seconds,
                end_seconds=transitions[-1].after.timestamp_seconds,
                evidence_observations=(peak.before, peak.after),
                evidence_timestamps=(
                    peak.before.timestamp_seconds,
                    peak.after.timestamp_seconds,
                ),
                edit_distance=peak.edit_distance,
                text_similarity=peak.similarity,
                track_observation_count=len(observations),
                change_count=len(transitions),
            )

    if len(observations) >= 2 and len(scene_times) >= 3:
        sample_intervals = [
            right - left
            for left, right in zip(scene_times, scene_times[1:])
            if right > left
        ]
        typical_interval = (
            statistics.median(sample_intervals) if sample_intervals else 0.0
        )
        for before, after in zip(observations, observations[1:]):
            gap = after.timestamp_seconds - before.timestamp_seconds
            if (
                typical_interval > 0
                and gap >= typical_interval * config.missing_gap_factor
                and gap <= config.maximum_track_gap_seconds
                and text_similarity(before.text, after.text)
                >= config.stable_text_similarity
            ):
                return TextInstabilityCandidate(
                    scene=track.scene,
                    reason="brief_disappearance",
                    start_seconds=before.timestamp_seconds,
                    end_seconds=after.timestamp_seconds,
                    evidence_observations=(before, after),
                    evidence_timestamps=(
                        before.timestamp_seconds,
                        after.timestamp_seconds,
                    ),
                    edit_distance=levenshtein_distance(
                        before.text,
                        after.text,
                    ),
                    text_similarity=text_similarity(
                        before.text,
                        after.text,
                    ),
                    track_observation_count=len(observations),
                    change_count=len(transitions),
                )

    duration = observations[-1].timestamp_seconds - observations[0].timestamp_seconds
    if (
        len(observations) <= config.maximum_flash_observations
        and duration <= config.maximum_flash_duration_seconds
        and observations[0].timestamp_seconds
        > track.scene.start_seconds + config.flash_boundary_guard_seconds
        and observations[-1].timestamp_seconds
        < track.scene.end_seconds - config.flash_boundary_guard_seconds
    ):
        before_times = [
            timestamp
            for timestamp in scene_times
            if timestamp < observations[0].timestamp_seconds
        ]
        after_times = [
            timestamp
            for timestamp in scene_times
            if timestamp > observations[-1].timestamp_seconds
        ]
        if before_times and after_times:
            return TextInstabilityCandidate(
                scene=track.scene,
                reason="brief_text_flash",
                start_seconds=max(before_times),
                end_seconds=min(after_times),
                evidence_observations=(
                    None,
                    observations[0],
                    None,
                ),
                evidence_timestamps=(
                    max(before_times),
                    observations[0].timestamp_seconds,
                    min(after_times),
                ),
                edit_distance=len(normalize_ocr_text(observations[0].text)),
                text_similarity=0.0,
                track_observation_count=len(observations),
                change_count=0,
            )
    return None


def deduplicate_candidates(
    candidates: tuple[TextInstabilityCandidate, ...],
) -> tuple[TextInstabilityCandidate, ...]:
    """Keep one deterministic strongest candidate for an identical interval."""
    selected: dict[tuple[int, float, float], TextInstabilityCandidate] = {}
    for candidate in candidates:
        key = (
            candidate.scene.scene_index,
            candidate.start_seconds,
            candidate.end_seconds,
        )
        current = selected.get(key)
        if current is None or (
            candidate.change_count,
            candidate.edit_distance,
            candidate.track_observation_count,
            candidate.reason,
        ) > (
            current.change_count,
            current.edit_distance,
            current.track_observation_count,
            current.reason,
        ):
            selected[key] = candidate
    return tuple(
        sorted(
            selected.values(),
            key=lambda item: (
                item.start_seconds,
                item.end_seconds,
                item.reason,
            ),
        )
    )


def _observation_metadata(
    observation: OCRObservation | None,
    *,
    candidate: TextInstabilityCandidate,
) -> dict[str, Any]:
    common: dict[str, Any] = {
        "instability_type": candidate.reason,
        "text_edit_distance": candidate.edit_distance,
        "text_similarity": candidate.text_similarity,
        "track_observation_count": candidate.track_observation_count,
        "change_count": candidate.change_count,
        "scene_index": candidate.scene.scene_index,
    }
    if observation is None:
        return {
            **common,
            "ocr_text": "",
            "ocr_confidence": None,
            "bounding_box": None,
            "ocr_boxes": [],
        }
    box = observation.bounding_box.model_dump(mode="json")
    return {
        **common,
        "ocr_text": observation.text,
        "ocr_confidence": observation.confidence,
        "bounding_box": box,
        "ocr_boxes": [
            {
                "text": observation.text,
                "confidence": observation.confidence,
                "bounding_box": box,
            }
        ],
    }


class TextStabilityDetector:
    """Diagnose unstable OCR tracks independently within each scene."""

    id = "text_stability"
    display_name = "Temporal text stability"
    version = "1.0.0"
    description = (
        "Tracks local OCR observations by position, text similarity, and time "
        "to find potential within-scene text instability."
    )
    requirements = DetectorRequirements(
        requires_prompt=False,
        requires_gpu=False,
        requires_network=False,
        optional_packages=("paddleocr", "paddlepaddle"),
        estimated_cost=EstimatedCost.HIGH,
    )
    default_enabled = True
    config_model = TextStabilityConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        """Run shared OCR and convert unstable scene-local tracks to Findings."""
        effective = TextStabilityConfig.model_validate(config.model_dump())
        if not context.frame_samples or not context.scenes:
            self._record_diagnostics(context, effective, (), ())
            return []
        runtime = context.shared_cache.get(MODEL_RUNTIME_CACHE_KEY)
        if not isinstance(runtime, ModelRuntimeManager):
            raise RuntimeError("text_stability requires the shared model runtime")
        batch = runtime.detect_and_recognize(
            effective.provider_id,
            effective.resolved_model_id(),
            tuple(
                OCRImageInput(
                    path=resolve_sample_path(context.workspace, sample),
                    timestamp_seconds=sample.timestamp_seconds,
                )
                for sample in context.frame_samples
            ),
        )
        tracks = build_text_tracks(
            batch.observations,
            context.scenes,
            video_duration_seconds=context.metadata.duration_seconds,
            minimum_confidence=effective.minimum_ocr_confidence,
            minimum_box_iou=effective.minimum_box_iou,
            high_iou_text_override=effective.high_iou_text_override,
            minimum_text_similarity=effective.minimum_track_text_similarity,
            maximum_gap_seconds=effective.maximum_track_gap_seconds,
        )
        candidates = deduplicate_candidates(
            tuple(
                candidate
                for track in tracks
                if (
                    candidate := find_track_instability(
                        track,
                        context.frame_samples,
                        video_duration_seconds=context.metadata.duration_seconds,
                        config=effective,
                    )
                )
                is not None
            )
        )
        self._record_diagnostics(context, effective, tracks, candidates)
        return [
            self._finding(context, effective=effective, candidate=candidate)
            for candidate in candidates
        ]

    @staticmethod
    def _record_diagnostics(
        context: AnalysisContext,
        effective: TextStabilityConfig,
        tracks: tuple[TextTrack, ...],
        candidates: tuple[TextInstabilityCandidate, ...],
    ) -> None:
        store = context.shared_cache.setdefault(
            DETECTOR_DIAGNOSTICS_CACHE_KEY,
            {},
        )
        if not isinstance(store, dict):
            raise TypeError("detector diagnostics cache has an invalid type")
        store[TextStabilityDetector.id] = {
            "provider_id": effective.provider_id,
            "model_id": effective.resolved_model_id(),
            "language": effective.language,
            "track_count": len(tracks),
            "candidate_count": len(candidates),
            "tracks": [
                {
                    "scene_index": track.scene.scene_index,
                    "observations": [
                        {
                            "timestamp_seconds": item.timestamp_seconds,
                            "text": item.text,
                            "confidence": item.confidence,
                            "bounding_box": item.bounding_box.model_dump(mode="json"),
                        }
                        for item in track.observations
                    ],
                }
                for track in tracks
            ],
            "candidates": [
                {
                    "scene_index": candidate.scene.scene_index,
                    "reason": candidate.reason,
                    "start_seconds": candidate.start_seconds,
                    "end_seconds": candidate.end_seconds,
                    "text_edit_distance": candidate.edit_distance,
                    "text_similarity": candidate.text_similarity,
                }
                for candidate in candidates
            ],
            "limitations": list(LIMITATIONS),
        }

    def _finding(
        self,
        context: AnalysisContext,
        *,
        effective: TextStabilityConfig,
        candidate: TextInstabilityCandidate,
    ) -> Finding:
        time_range = TimeRange(
            start_seconds=candidate.start_seconds,
            end_seconds=candidate.end_seconds,
        )
        evidence = [
            Evidence(
                evidence_type="ocr_frame",
                timestamp_seconds=timestamp,
                relative_path=min(
                    context.frame_samples,
                    key=lambda sample: (
                        abs(sample.timestamp_seconds - timestamp),
                        sample.timestamp_seconds,
                        sample.sample_index,
                    ),
                ).relative_path,
                description=(
                    "OCR evidence frame before, during, or after the observed "
                    "temporal text change."
                ),
                metadata=_observation_metadata(
                    observation,
                    candidate=candidate,
                ),
            )
            for timestamp, observation in zip(
                candidate.evidence_timestamps,
                candidate.evidence_observations,
                strict=True,
            )
        ]
        average_confidence = statistics.mean(
            observation.confidence
            for observation in candidate.evidence_observations
            if observation is not None
        )
        edit_strength = min(
            1.0,
            candidate.edit_distance
            / max(
                1,
                max(
                    (
                        len(normalize_ocr_text(observation.text))
                        for observation in candidate.evidence_observations
                        if observation is not None
                    ),
                    default=1,
                ),
            ),
        )
        score = (
            max(0.5, edit_strength)
            if candidate.reason != "brief_disappearance"
            else 0.6
        )
        return Finding(
            id=make_finding_id(
                input_hash=context.input_hash,
                detector_id=self.id,
                time_range=time_range,
            ),
            detector_id=self.id,
            detector_version=self.version,
            title="Potential temporal text instability",
            description=(
                "OCR observations in the same scene and overlapping image "
                "region changed or disappeared with a temporal pattern that "
                "may warrant review. This is an OCR-based heuristic, not a "
                "confirmed rendering defect."
            ),
            severity=effective.severity,
            score=score,
            confidence=average_confidence,
            time_range=time_range,
            evidence=evidence,
            tags=["text_stability", "ocr", candidate.reason],
            parameters=effective.model_dump(mode="json"),
            limitations=list(LIMITATIONS),
        )
