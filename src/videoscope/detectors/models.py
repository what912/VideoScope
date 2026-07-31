"""Shared models for the detector plugin boundary."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
)

from videoscope.domain import DetectorExecution, Finding, VideoMetadata
from videoscope.scenes import VideoScene
from videoscope.video import FrameSample

CancellationCallback = Callable[[], bool]
DETECTOR_DIAGNOSTICS_CACHE_KEY = "detector_diagnostics"


class DetectorModel(BaseModel):
    """Strict base model for detector plugin data."""

    model_config = ConfigDict(extra="forbid")


class EstimatedCost(StrEnum):
    """Coarse relative cost category declared by a detector."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DetectorRequirements(DetectorModel):
    """Capabilities and optional packages required by a detector."""

    requires_prompt: bool = False
    requires_gpu: bool = False
    requires_network: bool = False
    optional_packages: tuple[str, ...] = ()
    estimated_cost: EstimatedCost = EstimatedCost.LOW

    @field_validator("optional_packages")
    @classmethod
    def normalize_packages(cls, packages: tuple[str, ...]) -> tuple[str, ...]:
        """Reject blank names and store package declarations deterministically."""
        if any(not package.strip() for package in packages):
            raise ValueError("optional package names must not be blank")
        return tuple(sorted(set(packages)))


class AnalysisContext(DetectorModel):
    """Prepared, local analysis inputs shared with each detector."""

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        frozen=True,
    )

    input_path: Path
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: VideoMetadata
    prompt: str | None = None
    frame_samples: tuple[FrameSample, ...] = ()
    scenes: tuple[VideoScene, ...] = ()
    workspace: Path
    shared_cache: dict[str, Any] = Field(default_factory=dict)
    cancellation_callback: CancellationCallback | None = Field(
        default=None,
        exclude=True,
    )

    def is_cancelled(self) -> bool:
        """Query cancellation without imposing runner policy on detectors."""
        return (
            self.cancellation_callback()
            if self.cancellation_callback is not None
            else False
        )


class DetectorRunResult(DetectorModel):
    """Deterministically ordered outputs from a sequential detector run."""

    executions: tuple[DetectorExecution, ...] = ()
    findings: tuple[Finding, ...] = ()
