"""Dummy detector used only by unit tests."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from videoscope.detectors import (
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


class DummyDetectorConfig(BaseModel):
    """Validated controls for deterministic dummy findings."""

    model_config = ConfigDict(extra="forbid")

    emit_finding: bool = True
    start_seconds: float = Field(default=1.0, ge=0)
    end_seconds: float = Field(default=2.0, ge=0)
    score: float = Field(default=0.5, ge=0, le=1)
    severity: Severity = Severity.LOW


class DummyDetector:
    """Small detector double which is never registered by production code."""

    display_name = "Dummy detector"
    version = "1.0.0"
    description = "Produces a deterministic observable test Finding."
    requirements = DetectorRequirements(
        estimated_cost=EstimatedCost.LOW,
    )
    config_model = DummyDetectorConfig

    def __init__(
        self,
        detector_id: str = "test.dummy",
        *,
        default_enabled: bool = True,
    ) -> None:
        self.id = detector_id
        self.default_enabled = default_enabled

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        effective = DummyDetectorConfig.model_validate(config.model_dump())
        if not effective.emit_finding:
            return []
        time_range = TimeRange(
            start_seconds=effective.start_seconds,
            end_seconds=effective.end_seconds,
        )
        relative_path = (
            context.frame_samples[0].relative_path if context.frame_samples else None
        )
        return [
            Finding(
                id=make_finding_id(
                    input_hash=context.input_hash,
                    detector_id=self.id,
                    time_range=time_range,
                ),
                detector_id=self.id,
                detector_version=self.version,
                title="Synthetic observable change",
                description=(
                    "A deterministic test-only observation was requested by config."
                ),
                severity=effective.severity,
                score=effective.score,
                confidence=1.0,
                time_range=time_range,
                evidence=[
                    Evidence(
                        evidence_type="frame",
                        timestamp_seconds=effective.start_seconds,
                        relative_path=relative_path,
                        description="Test-only evidence for the requested interval.",
                    )
                ],
                parameters=effective.model_dump(mode="json"),
                limitations=["This Finding is emitted only by a test double."],
            )
        ]
