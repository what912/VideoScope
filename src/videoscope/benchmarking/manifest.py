"""Portable benchmark manifest loading and annotation scoping."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    model_validator,
)

from videoscope.analysis import AnalysisConfigError, AnalysisInputError
from videoscope.benchmarking.models import BenchmarkInterval

LEGACY_ANOMALY_DETECTORS = {
    "black_segment": "near_black",
    "freeze_segment": "possible_freeze",
    "blur_segment": "scene_relative_blur",
    "flicker_segment": "global_flicker",
}


class ManifestVideo(BaseModel):
    """One video and either legacy or per-detector annotations."""

    model_config = ConfigDict(extra="forbid")

    path: str | None = None
    duration_seconds: float = Field(gt=0, allow_inf_nan=False)
    tolerance_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    expected_anomaly_type: str | None = None
    expected_time_ranges: list[BenchmarkInterval] = Field(default_factory=list)
    expected_scene_cuts_seconds: list[float] = Field(default_factory=list)
    annotations: dict[str, list[BenchmarkInterval]] | None = None
    negative_detectors: list[str] = Field(default_factory=list)
    split: str | None = None

    @model_validator(mode="after")
    def validate_annotation_contract(self) -> ManifestVideo:
        if self.annotations is not None and self.expected_anomaly_type is not None:
            raise ValueError(
                "use either annotations or expected_anomaly_type, not both"
            )
        if self.annotations is None and self.expected_anomaly_type is None:
            raise ValueError("video requires annotations or expected_anomaly_type")
        if self.expected_anomaly_type is not None:
            supported = {"none", *LEGACY_ANOMALY_DETECTORS}
            if self.expected_anomaly_type not in supported:
                raise ValueError(
                    f"unsupported expected_anomaly_type: {self.expected_anomaly_type}"
                )
            if self.expected_anomaly_type != "none" and not self.expected_time_ranges:
                raise ValueError(
                    "legacy positive anomaly requires expected_time_ranges"
                )
        overlaps = set(self.annotations or {}) & set(self.negative_detectors)
        if overlaps:
            raise ValueError(
                "detector cannot appear in annotations and negative_detectors"
            )
        ranges = [
            interval
            for intervals in (self.annotations or {}).values()
            for interval in intervals
        ] + list(self.expected_time_ranges)
        if any(interval.end_seconds > self.duration_seconds for interval in ranges):
            raise ValueError("annotation interval exceeds video duration")
        return self


class ManifestDocument(BaseModel):
    """Accepted synthetic and real-dataset manifest structure."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    dataset_id: str | None = None
    video_root: str = "generated"
    generation: dict[str, JsonValue] = Field(default_factory=dict)
    videos: dict[str, ManifestVideo]


@dataclass(frozen=True, slots=True)
class AnnotationExpectation:
    """Normalized annotation scope for one video-detector pair."""

    scope: str
    intervals: tuple[BenchmarkInterval, ...]
    tolerance_seconds: float


@dataclass(frozen=True, slots=True)
class BenchmarkManifest:
    """Validated manifest plus its portable video resolution context."""

    name: str
    sha256: str
    directory: Path
    document: ManifestDocument

    def video_path(self, name: str, entry: ManifestVideo) -> Path:
        relative = entry.path or str(PurePosixPath(self.document.video_root) / name)
        path = PurePosixPath(relative.replace("\\", "/"))
        if (
            path.is_absolute()
            or not path.parts
            or ".." in path.parts
            or path.parts[0].endswith(":")
        ):
            raise AnalysisConfigError(
                f"Manifest video path must be relative and portable: {name}"
            )
        return self.directory.joinpath(*path.parts)

    @staticmethod
    def expectation(
        entry: ManifestVideo,
        detector_id: str,
    ) -> AnnotationExpectation:
        if entry.annotations is not None:
            if detector_id in entry.annotations:
                intervals = tuple(entry.annotations[detector_id])
                return AnnotationExpectation(
                    scope="positive" if intervals else "negative",
                    intervals=intervals,
                    tolerance_seconds=entry.tolerance_seconds,
                )
            if detector_id in entry.negative_detectors:
                return AnnotationExpectation(
                    scope="negative",
                    intervals=(),
                    tolerance_seconds=entry.tolerance_seconds,
                )
            return AnnotationExpectation(
                scope="excluded",
                intervals=(),
                tolerance_seconds=entry.tolerance_seconds,
            )

        anomaly_type = entry.expected_anomaly_type
        if anomaly_type == "none":
            return AnnotationExpectation(
                scope="negative",
                intervals=(),
                tolerance_seconds=entry.tolerance_seconds,
            )
        if (
            anomaly_type is not None
            and LEGACY_ANOMALY_DETECTORS.get(anomaly_type) == detector_id
        ):
            return AnnotationExpectation(
                scope="positive",
                intervals=tuple(entry.expected_time_ranges),
                tolerance_seconds=entry.tolerance_seconds,
            )
        return AnnotationExpectation(
            scope="excluded",
            intervals=(),
            tolerance_seconds=entry.tolerance_seconds,
        )


def load_benchmark_manifest(path: Path) -> BenchmarkManifest:
    """Load a strict UTF-8 manifest without exposing its absolute path."""
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise AnalysisInputError(f"Benchmark manifest not found: {manifest_path.name}")
    try:
        content = manifest_path.read_bytes()
        raw: object = json.loads(content.decode("utf-8"))
        document = ManifestDocument.model_validate(cast(dict[str, Any], raw))
    except UnicodeDecodeError as exc:
        raise AnalysisConfigError("Benchmark manifest must be UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise AnalysisConfigError("Benchmark manifest is not valid JSON") from exc
    except ValidationError as exc:
        raise AnalysisConfigError(f"Invalid benchmark manifest: {exc}") from exc
    if not document.videos:
        raise AnalysisConfigError("Benchmark manifest contains no videos")
    manifest = BenchmarkManifest(
        name=manifest_path.name,
        sha256=sha256(content).hexdigest(),
        directory=manifest_path.resolve().parent,
        document=document,
    )
    missing = [
        name
        for name, entry in sorted(document.videos.items())
        if not manifest.video_path(name, entry).is_file()
    ]
    if missing:
        preview = ", ".join(missing[:5])
        suffix = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
        raise AnalysisInputError(
            f"Benchmark video file(s) not found: {preview}{suffix}"
        )
    return manifest
