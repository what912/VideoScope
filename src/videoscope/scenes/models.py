"""Vendor-neutral scene segmentation domain models."""

from __future__ import annotations

import math
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_TIME_TOLERANCE = 1e-9


class SceneModel(BaseModel):
    """Strict base model for scene segmentation data."""

    model_config = ConfigDict(extra="forbid")


class VideoScene(SceneModel):
    """One deterministic, half-open scene interval."""

    scene_index: int = Field(ge=0)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    representative_timestamp: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        """Ensure redundant scene timing fields remain consistent."""
        if self.end_seconds < self.start_seconds:
            raise ValueError(
                "end_seconds must be greater than or equal to start_seconds"
            )
        expected_duration = self.end_seconds - self.start_seconds
        if not math.isclose(
            self.duration_seconds,
            expected_duration,
            rel_tol=0,
            abs_tol=_TIME_TOLERANCE,
        ):
            raise ValueError("duration_seconds must equal end_seconds - start_seconds")
        if not (
            self.start_seconds - _TIME_TOLERANCE
            <= self.representative_timestamp
            <= self.end_seconds + _TIME_TOLERANCE
        ):
            raise ValueError("representative_timestamp must be inside the scene")
        return self


class SceneDetectionConfig(SceneModel):
    """All configurable scene detection and fallback thresholds."""

    adaptive_threshold: float = Field(
        default=3.0,
        gt=0,
        allow_inf_nan=False,
    )
    min_content_value: float = Field(
        default=15.0,
        ge=0,
        allow_inf_nan=False,
    )
    window_width: int = Field(default=2, ge=1)
    minimum_scene_duration_seconds: float = Field(
        default=0.5,
        ge=0,
        allow_inf_nan=False,
    )
    fallback_window_seconds: float = Field(
        default=10.0,
        gt=0,
        allow_inf_nan=False,
    )


class SceneDetectionResult(SceneModel):
    """Scene context and non-fatal warnings from one segmentation run."""

    source: str = Field(min_length=1)
    scenes: tuple[VideoScene, ...] = Field(min_length=1)
    warnings: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_scene_sequence(self) -> Self:
        """Require ordered, indexed, continuous, non-overlapping scenes."""
        for expected_index, scene in enumerate(self.scenes):
            if scene.scene_index != expected_index:
                raise ValueError("scene_index values must be contiguous from zero")
            if expected_index == 0:
                if not math.isclose(
                    scene.start_seconds,
                    0.0,
                    rel_tol=0,
                    abs_tol=_TIME_TOLERANCE,
                ):
                    raise ValueError("the first scene must start at zero")
                continue
            previous = self.scenes[expected_index - 1]
            if not math.isclose(
                previous.end_seconds,
                scene.start_seconds,
                rel_tol=0,
                abs_tol=_TIME_TOLERANCE,
            ):
                raise ValueError("scene intervals must be continuous")
        return self
