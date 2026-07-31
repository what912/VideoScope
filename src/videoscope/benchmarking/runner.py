"""End-to-end benchmark orchestration over an annotated manifest."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from time import perf_counter
from typing import Protocol

from pydantic import JsonValue, ValidationError

from videoscope import __version__
from videoscope.analysis import (
    AnalysisConfig,
    AnalysisConfigError,
    AnalysisPipeline,
)
from videoscope.benchmarking.manifest import load_benchmark_manifest
from videoscope.benchmarking.metrics import (
    aggregate_evaluations,
    evaluate_intervals,
)
from videoscope.benchmarking.models import (
    BenchmarkCaseResult,
    BenchmarkInterval,
    BenchmarkProfile,
    BenchmarkProfileResult,
    BenchmarkReport,
    BenchmarkRuntime,
    DetectorBenchmarkResult,
    EventEvaluation,
)
from videoscope.benchmarking.serialization import write_benchmark_json
from videoscope.detectors import DetectorRegistry, create_builtin_detector_registry
from videoscope.domain import AnalysisReport, DetectorStatus

Clock = Callable[[], float]
ProgressCallback = Callable[[str], None]


class Analyzer(Protocol):
    """Injectable analysis boundary used by benchmark unit tests."""

    def __call__(
        self,
        input_path: Path,
        config: AnalysisConfig,
    ) -> AnalysisReport: ...


class BenchmarkRunner:
    """Run stable analysis profiles and aggregate each detector independently."""

    def __init__(
        self,
        *,
        registry: DetectorRegistry | None = None,
        analyzer: Analyzer | None = None,
        clock: Clock = perf_counter,
        ffmpeg_version: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self.registry = registry or create_builtin_detector_registry()
        self.analyzer = analyzer or self._analyze
        self.clock = clock
        self.ffmpeg_version = ffmpeg_version
        self.progress = progress

    def run(
        self,
        manifest_path: Path,
        *,
        output_directory: Path,
        profiles: Sequence[BenchmarkProfile],
        detector_ids: Sequence[str],
        minimum_iou: float = 0.1,
    ) -> BenchmarkReport:
        """Run all requested profiles and atomically write benchmark.json."""
        started = self.clock()
        if not 0 <= minimum_iou <= 1:
            raise AnalysisConfigError("minimum IoU must be in [0, 1]")
        manifest = load_benchmark_manifest(manifest_path)
        selected_ids = self._validate_detector_ids(detector_ids)
        if not profiles:
            raise AnalysisConfigError("At least one benchmark profile is required")

        output = Path(output_directory)
        if output.exists() and not output.is_dir():
            raise AnalysisConfigError(
                f"Benchmark output is not a directory: {output.name}"
            )
        output.mkdir(parents=True, exist_ok=True)
        profile_results: list[BenchmarkProfileResult] = []
        with tempfile.TemporaryDirectory(prefix="videoscope-benchmark-") as temporary:
            temporary_root = Path(temporary)
            for profile_index, profile in enumerate(profiles):
                public_config = self._public_configuration(
                    profile.config,
                    selected_ids,
                )
                config_id = _config_id(public_config)
                cases_by_detector: dict[str, list[BenchmarkCaseResult]] = {
                    detector_id: [] for detector_id in selected_ids
                }
                for video_index, (video_name, entry) in enumerate(
                    sorted(manifest.document.videos.items())
                ):
                    self._emit(
                        f"[{profile.label}] analyzing {video_name} "
                        f"({video_index + 1}/{len(manifest.document.videos)})"
                    )
                    run_output = (
                        temporary_root
                        / f"profile-{profile_index:03d}"
                        / f"video-{video_index:05d}"
                    )
                    effective = self._runtime_configuration(
                        profile.config,
                        selected_ids,
                        run_output,
                    )
                    report = self.analyzer(
                        manifest.video_path(video_name, entry),
                        effective,
                    )
                    for detector_id in selected_ids:
                        expectation = manifest.expectation(entry, detector_id)
                        predicted = [
                            BenchmarkInterval(
                                start_seconds=finding.time_range.start_seconds,
                                end_seconds=finding.time_range.end_seconds,
                            )
                            for finding in report.findings
                            if finding.detector_id == detector_id
                        ]
                        execution = next(
                            (
                                item
                                for item in report.detector_executions
                                if item.detector_id == detector_id
                            ),
                            None,
                        )
                        if expectation.scope == "excluded":
                            case = BenchmarkCaseResult(
                                video=video_name,
                                detector_id=detector_id,
                                annotation_scope="excluded",
                                status="excluded",
                                tolerance_seconds=expectation.tolerance_seconds,
                                expected_intervals=[],
                                predicted_intervals=predicted,
                            )
                        elif (
                            execution is None
                            or execution.status is DetectorStatus.DETECTOR_ERROR
                        ):
                            case = BenchmarkCaseResult(
                                video=video_name,
                                detector_id=detector_id,
                                annotation_scope=expectation.scope,
                                status="detector_error",
                                tolerance_seconds=expectation.tolerance_seconds,
                                expected_intervals=list(expectation.intervals),
                                predicted_intervals=predicted,
                                error_type=(
                                    execution.error_type
                                    if execution is not None
                                    else "MissingDetectorExecution"
                                ),
                                error_message=(
                                    execution.error_message
                                    if execution is not None
                                    else "Detector execution record was missing."
                                ),
                            )
                        else:
                            evaluation = evaluate_intervals(
                                expectation.intervals,
                                predicted,
                                minimum_iou=minimum_iou,
                                tolerance_seconds=expectation.tolerance_seconds,
                            )
                            case = BenchmarkCaseResult(
                                video=video_name,
                                detector_id=detector_id,
                                annotation_scope=expectation.scope,
                                status="ok",
                                tolerance_seconds=expectation.tolerance_seconds,
                                expected_intervals=list(expectation.intervals),
                                predicted_intervals=predicted,
                                evaluation=evaluation,
                            )
                        cases_by_detector[detector_id].append(case)

                detector_results = [
                    _summarize_detector(detector_id, cases_by_detector[detector_id])
                    for detector_id in selected_ids
                ]
                profile_results.append(
                    BenchmarkProfileResult(
                        config_id=config_id,
                        label=profile.label,
                        configuration=public_config,
                        detectors=detector_results,
                    )
                )

        matching: dict[str, JsonValue] = {
            "method": "deterministic_greedy_one_to_one",
            "minimum_temporal_iou": minimum_iou,
            "manifest_boundary_tolerance_enabled": True,
        }
        runtime = BenchmarkRuntime(
            platform=platform.system() or "unknown",
            machine=platform.machine() or "unknown",
            python_version=platform.python_version(),
            ffmpeg_version=self.ffmpeg_version or _read_ffmpeg_version(),
        )
        elapsed = max(0.0, self.clock() - started)
        result_fingerprint = _result_fingerprint(
            manifest_sha256=manifest.sha256,
            matching=matching,
            runtime=runtime,
            profiles=profile_results,
        )
        result = BenchmarkReport(
            tool_version=__version__,
            manifest_name=manifest.name,
            manifest_sha256=manifest.sha256,
            result_fingerprint=result_fingerprint,
            matching=matching,
            runtime=runtime,
            total_runtime_seconds=round(elapsed, 6),
            profiles=profile_results,
            limitations=[
                "Synthetic fixtures are engineering regression cases, not an "
                "estimate of accuracy on real generated videos.",
                "Metrics are reported independently per detector and must not be "
                "combined into an uncalibrated global quality score.",
            ],
        )
        write_benchmark_json(result, output / "benchmark.json")
        return result

    def _validate_detector_ids(
        self,
        detector_ids: Sequence[str],
    ) -> tuple[str, ...]:
        available = {item.id for item in self.registry.list_available()}
        selected = tuple(sorted(set(detector_ids)))
        if not selected:
            raise AnalysisConfigError("At least one detector must be selected")
        unknown = set(selected) - available
        if unknown:
            raise AnalysisConfigError(
                f"Unknown detector ID(s): {', '.join(sorted(unknown))}"
            )
        return selected

    def _public_configuration(
        self,
        config: AnalysisConfig,
        detector_ids: tuple[str, ...],
    ) -> dict[str, JsonValue]:
        detectors: dict[str, JsonValue] = {}
        for detector_id in detector_ids:
            detector = self.registry.get(detector_id)
            raw = config.detector_configurations.get(detector_id, {})
            try:
                detectors[detector_id] = detector.config_model.model_validate(
                    raw
                ).model_dump(mode="json")
            except ValidationError as exc:
                raise AnalysisConfigError(
                    f"Invalid configuration for detector {detector_id}: {exc}"
                ) from exc
        return {
            "sample_fps": config.sample_fps,
            "thumbnail_max_size": config.thumbnail_max_size,
            "enabled_detectors": list(detector_ids),
            "detectors": detectors,
            "locale": config.locale,
        }

    @staticmethod
    def _runtime_configuration(
        config: AnalysisConfig,
        detector_ids: tuple[str, ...],
        output_directory: Path,
    ) -> AnalysisConfig:
        data = config.model_dump(mode="python")
        data.update(
            {
                "enabled_detectors": detector_ids,
                "output_directory": output_directory,
                "keep_workspace": False,
                "json_only": True,
                "bundle_video": False,
            }
        )
        return AnalysisConfig.model_validate(data)

    def _analyze(
        self,
        input_path: Path,
        config: AnalysisConfig,
    ) -> AnalysisReport:
        report: AnalysisReport = (
            AnalysisPipeline(
                config,
                registry=self.registry,
                progress=None,
            )
            .run(input_path)
            .report
        )
        return report

    def _emit(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)


def run_benchmark(
    manifest_path: Path,
    *,
    output_directory: Path,
    profiles: Sequence[BenchmarkProfile],
    detector_ids: Sequence[str],
    minimum_iou: float = 0.1,
    progress: ProgressCallback | None = None,
) -> BenchmarkReport:
    """Convenience entry point used by CLI and calibration."""
    return BenchmarkRunner(progress=progress).run(
        manifest_path,
        output_directory=output_directory,
        profiles=profiles,
        detector_ids=detector_ids,
        minimum_iou=minimum_iou,
    )


def _config_id(configuration: dict[str, JsonValue]) -> str:
    payload = json.dumps(
        configuration,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"config_{hashlib.sha256(payload).hexdigest()[:16]}"


def _result_fingerprint(
    *,
    manifest_sha256: str,
    matching: dict[str, JsonValue],
    runtime: BenchmarkRuntime,
    profiles: Sequence[BenchmarkProfileResult],
) -> str:
    payload = {
        "tool_version": __version__,
        "manifest_sha256": manifest_sha256,
        "matching": matching,
        "runtime": runtime.model_dump(mode="json"),
        "profiles": [profile.model_dump(mode="json") for profile in profiles],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _summarize_detector(
    detector_id: str,
    cases: Sequence[BenchmarkCaseResult],
) -> DetectorBenchmarkResult:
    evaluations: list[EventEvaluation] = [
        case.evaluation
        for case in cases
        if case.status == "ok" and case.evaluation is not None
    ]
    negative_cases = [
        case
        for case in cases
        if case.status == "ok" and case.annotation_scope == "negative"
    ]
    return DetectorBenchmarkResult(
        detector_id=detector_id,
        evaluated_case_count=len(evaluations),
        excluded_unannotated_case_count=sum(
            case.status == "excluded" for case in cases
        ),
        detector_error_count=sum(case.status == "detector_error" for case in cases),
        negative_case_count=len(negative_cases),
        negative_false_positive_event_count=sum(
            case.evaluation.metrics.false_positive_events
            for case in negative_cases
            if case.evaluation is not None
        ),
        negative_false_positive_duration_seconds=sum(
            case.evaluation.metrics.false_positive_duration_seconds
            for case in negative_cases
            if case.evaluation is not None
        ),
        metrics=aggregate_evaluations(evaluations),
        cases=list(cases),
    )


def _read_ffmpeg_version() -> str:
    try:
        completed = subprocess.run(
            ["ffmpeg", "-version"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=5.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return "unavailable"
    if completed.returncode != 0:
        return f"unavailable (exit {completed.returncode})"
    output = completed.stdout.strip() or completed.stderr.strip()
    return output.splitlines()[0] if output else "available (version not reported)"
