"""Optional scene-level prompt/frame similarity diagnostics."""

from __future__ import annotations

import importlib
import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoscope.ai import (
    MODEL_RUNTIME_CACHE_KEY,
    ImageEmbeddingInput,
    ModelRuntimeManager,
)
from videoscope.ai.providers import (
    DEFAULT_OPENCLIP_MODEL_ID,
    OPENCLIP_PREPROCESSING_VERSION,
    OPENCLIP_PROVIDER_ID,
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

np: Any = importlib.import_module("numpy")
_TIME_TOLERANCE = 1e-9

LIMITATIONS = (
    "CLIP similarity is not a complete semantic verification of the prompt.",
    "Complex actions, negation, counts, and spatial relationships may be "
    "represented unreliably.",
)


class PromptAlignmentMode(StrEnum):
    """Whether the detector only describes scores or applies a user threshold."""

    DESCRIPTIVE = "descriptive"
    THRESHOLD = "threshold"


class PromptAlignmentConfig(BaseModel):
    """Explicit model and interpretation settings for prompt alignment."""

    model_config = ConfigDict(extra="forbid")

    mode: PromptAlignmentMode = PromptAlignmentMode.DESCRIPTIVE
    similarity_threshold: float | None = Field(
        default=None,
        ge=-1,
        le=1,
        allow_inf_nan=False,
    )
    representative_frames_per_scene: int = Field(default=3, ge=2, le=16)
    merge_consecutive_scenes: bool = True
    provider_id: str = OPENCLIP_PROVIDER_ID
    model_id: str = DEFAULT_OPENCLIP_MODEL_ID
    preprocessing_version: str = OPENCLIP_PREPROCESSING_VERSION
    severity: Severity = Severity.LOW

    @model_validator(mode="after")
    def validate_mode(self) -> PromptAlignmentConfig:
        """Require a user threshold only in explicit threshold mode."""
        if (
            self.mode is PromptAlignmentMode.THRESHOLD
            and self.similarity_threshold is None
        ):
            raise ValueError("threshold mode requires an explicit similarity_threshold")
        if (
            self.mode is PromptAlignmentMode.DESCRIPTIVE
            and self.similarity_threshold is not None
        ):
            raise ValueError("similarity_threshold is only valid in threshold mode")
        return self


@dataclass(frozen=True, slots=True)
class SceneFrameSelection:
    """Sample positions selected deterministically for one scene."""

    scene: VideoScene
    sample_positions: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SceneSimilarity:
    """Scene-level cosine similarity summary."""

    scene: VideoScene
    sample_positions: tuple[int, ...]
    similarities: tuple[float, ...]
    mean_similarity: float
    minimum_similarity: float
    lowest_sample_position: int

    def as_json(self, samples: tuple[FrameSample, ...]) -> dict[str, Any]:
        """Return report-safe diagnostics without local paths."""
        return {
            "scene_index": self.scene.scene_index,
            "start_seconds": self.scene.start_seconds,
            "end_seconds": self.scene.end_seconds,
            "mean_similarity": self.mean_similarity,
            "minimum_similarity": self.minimum_similarity,
            "lowest_frame_timestamp_seconds": samples[
                self.lowest_sample_position
            ].timestamp_seconds,
            "samples": [
                {
                    "timestamp_seconds": samples[position].timestamp_seconds,
                    "similarity": similarity,
                }
                for position, similarity in zip(
                    self.sample_positions,
                    self.similarities,
                    strict=True,
                )
            ],
        }


def select_scene_samples(
    samples: tuple[FrameSample, ...],
    scene: VideoScene,
    *,
    count: int,
    video_duration_seconds: float,
) -> SceneFrameSelection:
    """Select samples nearest evenly spaced interior scene timestamps."""
    candidates = [
        (position, sample)
        for position, sample in enumerate(samples)
        if scene.start_seconds <= sample.timestamp_seconds < scene.end_seconds
        or (
            math.isclose(
                scene.end_seconds,
                video_duration_seconds,
                rel_tol=0,
                abs_tol=_TIME_TOLERANCE,
            )
            and math.isclose(
                sample.timestamp_seconds,
                scene.end_seconds,
                rel_tol=0,
                abs_tol=_TIME_TOLERANCE,
            )
        )
    ]
    if not candidates:
        return SceneFrameSelection(scene=scene, sample_positions=())
    targets = tuple(
        scene.start_seconds + scene.duration_seconds * ((index + 0.5) / count)
        for index in range(count)
    )
    selected: list[int] = []
    for target in targets:
        position, _ = min(
            candidates,
            key=lambda item: (
                abs(item[1].timestamp_seconds - target),
                item[1].timestamp_seconds,
                item[1].sample_index,
            ),
        )
        if position not in selected:
            selected.append(position)
    return SceneFrameSelection(
        scene=scene,
        sample_positions=tuple(
            sorted(
                selected,
                key=lambda position: (
                    samples[position].timestamp_seconds,
                    samples[position].sample_index,
                ),
            )
        ),
    )


def cosine_similarities(image_embeddings: Any, text_embedding: Any) -> Any:
    """Return robust row-wise cosine similarities in ``[-1, 1]``."""
    images = np.asarray(image_embeddings, dtype=np.float64)
    text = np.asarray(text_embedding, dtype=np.float64)
    if images.ndim != 2 or text.ndim != 1 or images.shape[1] != text.shape[0]:
        raise ValueError("image and text embedding dimensions do not match")
    image_norms = np.linalg.norm(images, axis=1)
    text_norm = float(np.linalg.norm(text))
    if text_norm <= 0 or bool(np.any(image_norms <= 0)):
        raise ValueError("embedding vectors must have non-zero norms")
    values = (images @ text) / (image_norms * text_norm)
    return np.clip(values, -1.0, 1.0)


def summarize_scene_similarities(
    selections: tuple[SceneFrameSelection, ...],
    similarities: Any,
) -> tuple[SceneSimilarity, ...]:
    """Map the flat embedding result back to stable scene summaries."""
    values = np.asarray(similarities, dtype=np.float64)
    summaries: list[SceneSimilarity] = []
    offset = 0
    for selection in selections:
        length = len(selection.sample_positions)
        if length == 0:
            continue
        scene_values = values[offset : offset + length]
        offset += length
        minimum_offset = min(
            range(length),
            key=lambda index: (float(scene_values[index]), index),
        )
        summaries.append(
            SceneSimilarity(
                scene=selection.scene,
                sample_positions=selection.sample_positions,
                similarities=tuple(float(value) for value in scene_values),
                mean_similarity=float(np.mean(scene_values)),
                minimum_similarity=float(scene_values[minimum_offset]),
                lowest_sample_position=selection.sample_positions[minimum_offset],
            )
        )
    if offset != len(values):
        raise ValueError("similarity count does not match selected scene frames")
    return tuple(summaries)


def merge_low_similarity_scenes(
    scenes: tuple[SceneSimilarity, ...],
    *,
    threshold: float,
    merge_consecutive: bool,
) -> tuple[tuple[SceneSimilarity, ...], ...]:
    """Group below-threshold adjacent scenes without bridging missing context."""
    low = tuple(scene for scene in scenes if scene.mean_similarity < threshold)
    if not low:
        return ()
    if not merge_consecutive:
        return tuple((scene,) for scene in low)
    groups: list[list[SceneSimilarity]] = [[low[0]]]
    for scene in low[1:]:
        previous = groups[-1][-1]
        if scene.scene.scene_index == previous.scene.scene_index + 1 and math.isclose(
            scene.scene.start_seconds,
            previous.scene.end_seconds,
            rel_tol=0,
            abs_tol=_TIME_TOLERANCE,
        ):
            groups[-1].append(scene)
        else:
            groups.append([scene])
    return tuple(tuple(group) for group in groups)


class PromptAlignmentDetector:
    """Describe local CLIP prompt/frame similarities by scene."""

    id = "prompt_alignment"
    display_name = "Prompt/frame similarity"
    version = "1.0.0"
    description = (
        "Measures scene-level prompt/frame cosine similarity with a shared "
        "optional embedding provider."
    )
    requirements = DetectorRequirements(
        requires_prompt=True,
        requires_gpu=False,
        requires_network=False,
        optional_packages=("open-clip-torch", "torch"),
        estimated_cost=EstimatedCost.HIGH,
    )
    default_enabled = True
    config_model = PromptAlignmentConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        """Compute scene summaries and optional threshold-based Findings."""
        effective = PromptAlignmentConfig.model_validate(config.model_dump())
        prompt = (context.prompt or "").strip()
        if not prompt:
            return []
        runtime = context.shared_cache.get(MODEL_RUNTIME_CACHE_KEY)
        if not isinstance(runtime, ModelRuntimeManager):
            raise RuntimeError("prompt_alignment requires the shared AI model runtime")
        selections = tuple(
            select_scene_samples(
                context.frame_samples,
                scene,
                count=effective.representative_frames_per_scene,
                video_duration_seconds=context.metadata.duration_seconds,
            )
            for scene in context.scenes
        )
        selected_positions = tuple(
            position
            for selection in selections
            for position in selection.sample_positions
        )
        if not selected_positions:
            raise RuntimeError("prompt_alignment found no sampled scene frames")
        image_inputs = tuple(
            ImageEmbeddingInput(
                path=resolve_sample_path(
                    context.workspace,
                    context.frame_samples[position],
                ),
                video_hash=context.input_hash,
                timestamp_seconds=context.frame_samples[position].timestamp_seconds,
                preprocessing_version=effective.preprocessing_version,
            )
            for position in selected_positions
        )
        image_batch = runtime.encode_images(
            effective.provider_id,
            effective.model_id,
            image_inputs,
        )
        text_batch = runtime.encode_text(
            effective.provider_id,
            effective.model_id,
            [prompt],
        )
        values = cosine_similarities(
            image_batch.embeddings,
            text_batch.embeddings[0],
        )
        summaries = summarize_scene_similarities(selections, values)
        self._record_diagnostics(
            context,
            effective=effective,
            prompt=prompt,
            summaries=summaries,
        )
        if effective.mode is PromptAlignmentMode.DESCRIPTIVE:
            return []
        threshold = effective.similarity_threshold
        if threshold is None:
            raise ValueError("threshold mode requires similarity_threshold")
        groups = merge_low_similarity_scenes(
            summaries,
            threshold=threshold,
            merge_consecutive=effective.merge_consecutive_scenes,
        )
        return [
            self._finding_for_group(
                context,
                effective=effective,
                prompt=prompt,
                group=group,
            )
            for group in groups
        ]

    @staticmethod
    def _record_diagnostics(
        context: AnalysisContext,
        *,
        effective: PromptAlignmentConfig,
        prompt: str,
        summaries: tuple[SceneSimilarity, ...],
    ) -> None:
        store = context.shared_cache.setdefault(
            DETECTOR_DIAGNOSTICS_CACHE_KEY,
            {},
        )
        if not isinstance(store, dict):
            raise TypeError("detector diagnostics cache has an invalid type")
        lowest = (
            None
            if not summaries
            else min(
                summaries,
                key=lambda item: (
                    item.mean_similarity,
                    item.scene.scene_index,
                ),
            ).as_json(context.frame_samples)
        )
        store[PromptAlignmentDetector.id] = {
            "mode": effective.mode.value,
            "provider_id": effective.provider_id,
            "model_id": effective.model_id,
            "prompt": prompt,
            "scenes": [item.as_json(context.frame_samples) for item in summaries],
            "lowest_scene": lowest,
            "limitations": list(LIMITATIONS),
        }

    def _finding_for_group(
        self,
        context: AnalysisContext,
        *,
        effective: PromptAlignmentConfig,
        prompt: str,
        group: tuple[SceneSimilarity, ...],
    ) -> Finding:
        samples = context.frame_samples
        lowest = min(
            group,
            key=lambda item: (
                item.minimum_similarity,
                item.scene.scene_index,
            ),
        )
        group_values = [
            similarity for scene in group for similarity in scene.similarities
        ]
        group_mean = sum(group_values) / len(group_values)
        group_minimum = min(group_values)
        time_range = TimeRange(
            start_seconds=group[0].scene.start_seconds,
            end_seconds=group[-1].scene.end_seconds,
        )
        lowest_sample = samples[lowest.lowest_sample_position]
        expected_samples = len(group) * effective.representative_frames_per_scene
        return Finding(
            id=make_finding_id(
                input_hash=context.input_hash,
                detector_id=self.id,
                time_range=time_range,
            ),
            detector_id=self.id,
            detector_version=self.version,
            title="Low prompt-frame similarity",
            description=(
                "The scene-level mean cosine similarity between the prompt and "
                "sampled frames was below the user-provided threshold. This is "
                "an observable embedding comparison, not confirmation that the "
                "video violates the prompt."
            ),
            severity=effective.severity,
            score=max(0.0, min(1.0, 1.0 - ((group_mean + 1.0) / 2.0))),
            confidence=min(1.0, len(group_values) / expected_samples),
            time_range=time_range,
            evidence=[
                Evidence(
                    evidence_type="sampled_frame",
                    timestamp_seconds=lowest_sample.timestamp_seconds,
                    relative_path=lowest_sample.relative_path,
                    description=(
                        "Lowest-similarity sampled frame in the merged scene interval."
                    ),
                    metadata={
                        "prompt": prompt,
                        "scene_start_seconds": time_range.start_seconds,
                        "scene_end_seconds": time_range.end_seconds,
                        "scene_indices": [scene.scene.scene_index for scene in group],
                        "mean_similarity": group_mean,
                        "minimum_similarity": group_minimum,
                        "scene_mean_similarities": [
                            scene.mean_similarity for scene in group
                        ],
                    },
                )
            ],
            tags=["prompt_alignment", "clip_similarity"],
            parameters=effective.model_dump(mode="json"),
            limitations=list(LIMITATIONS),
        )
