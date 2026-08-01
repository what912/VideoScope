"""Register a small local Detector without modifying VideoScope core code."""

from __future__ import annotations

import argparse
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from videoscope.analysis import AnalysisConfig, AnalysisPipeline
from videoscope.detectors import (
    AnalysisContext,
    DetectorRequirements,
    EstimatedCost,
    create_builtin_detector_registry,
)
from videoscope.domain import Evidence, Finding, Severity, TimeRange, make_finding_id


class CustomObservationConfig(BaseModel):
    """Configuration stays explicit and is recorded in the report."""

    model_config = ConfigDict(extra="forbid")

    interval_seconds: float = Field(default=0.5, gt=0)
    severity: Severity = Severity.INFO


class CustomObservationDetector:
    """Demonstration detector that marks the beginning as an observation."""

    id = "example.custom_observation"
    display_name = "Example custom observation"
    version = "1.0.0"
    description = "Demonstrates the Detector contract without claiming a defect."
    requirements = DetectorRequirements(estimated_cost=EstimatedCost.LOW)
    default_enabled = True
    config_model = CustomObservationConfig

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        effective = CustomObservationConfig.model_validate(
            config.model_dump(mode="python")
        )
        if not context.frame_samples or context.metadata.duration_seconds <= 0:
            return []
        end_seconds = min(
            effective.interval_seconds,
            context.metadata.duration_seconds,
        )
        interval = TimeRange(start_seconds=0.0, end_seconds=end_seconds)
        first_sample = context.frame_samples[0]
        return [
            Finding(
                id=make_finding_id(
                    input_hash=context.input_hash,
                    detector_id=self.id,
                    time_range=interval,
                ),
                detector_id=self.id,
                detector_version=self.version,
                title="Custom observable interval",
                description=(
                    "This example marks a configured interval to demonstrate "
                    "plugin data flow; it is not a quality diagnosis."
                ),
                severity=effective.severity,
                score=0.0,
                confidence=0.0,
                time_range=interval,
                evidence=[
                    Evidence(
                        evidence_type="frame",
                        timestamp_seconds=first_sample.timestamp_seconds,
                        relative_path=first_sample.relative_path,
                        description="First sampled frame for the example interval.",
                    )
                ],
                parameters=effective.model_dump(mode="json"),
                limitations=[
                    "This detector is an API example and does not detect a defect."
                ],
                tags=["example"],
            )
        ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_video", type=Path)
    parser.add_argument("--output", type=Path, default=Path("runs/custom-detector"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry = create_builtin_detector_registry()
    registry.register(CustomObservationDetector())
    config = AnalysisConfig(
        enabled_detectors=(CustomObservationDetector.id,),
        output_directory=args.output,
    )
    result = AnalysisPipeline(config, registry=registry).run(args.input_video)
    print(result.report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
