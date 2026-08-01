"""End-to-end deterministic local CPU analysis pipeline."""

from __future__ import annotations

import platform
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError

from videoscope import __version__
from videoscope.ai import MODEL_RUNTIME_CACHE_KEY, ModelRuntimeManager
from videoscope.analysis.config import AnalysisConfig
from videoscope.analysis.errors import (
    AnalysisCancelledError,
    AnalysisConfigError,
    AnalysisInputError,
    AnalysisInternalError,
    AnalysisProcessingError,
)
from videoscope.analysis.evidence import EvidenceManager
from videoscope.detectors import (
    DETECTOR_DIAGNOSTICS_CACHE_KEY,
    AnalysisContext,
    DetectorRegistry,
    DetectorRunner,
    create_builtin_detector_registry,
)
from videoscope.domain import (
    AnalysisReport,
    DetectorExecution,
    DetectorStatus,
    Finding,
    VideoMetadata,
    write_report_json,
)
from videoscope.reporting import HTMLReportRenderer
from videoscope.scenes import (
    PySceneDetectAdapter,
    SceneDetectionResult,
    SceneDetector,
)
from videoscope.video import (
    FrameSamplingResult,
    VideoProcessingError,
    compute_file_sha256,
    probe_video,
    sample_frames,
)

ProgressCallback = Callable[[str], None]
CancellationCallback = Callable[[], bool]
Clock = Callable[[], float]


class HashFunction(Protocol):
    def __call__(self, path: Path) -> str: ...


class ProbeFunction(Protocol):
    def __call__(self, path: Path, *, ffprobe: str) -> VideoMetadata: ...


class SampleFunction(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        sample_rate: float,
        max_edge: int,
        image_format: Literal["jpeg", "png"],
        workspace_parent: Path,
        ffmpeg: str,
    ) -> FrameSamplingResult: ...


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """Completed report plus its local artifact locations."""

    report: AnalysisReport
    report_path: Path
    html_report_path: Path | None
    bundled_video_path: Path | None
    workspace_directory: Path | None


class AnalysisPipeline:
    """Connect probe, sampling, scenes, detectors, evidence, and JSON."""

    def __init__(
        self,
        config: AnalysisConfig,
        *,
        registry: DetectorRegistry | None = None,
        scene_detector: SceneDetector | None = None,
        hash_function: HashFunction = compute_file_sha256,
        probe_function: ProbeFunction = probe_video,
        sample_function: SampleFunction = sample_frames,
        detector_clock: Clock = perf_counter,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        progress: ProgressCallback | None = None,
        html_renderer: HTMLReportRenderer | None = None,
        model_runtime: ModelRuntimeManager | None = None,
        cancellation_callback: CancellationCallback | None = None,
    ) -> None:
        self.config = config
        self.registry = registry or create_builtin_detector_registry()
        self.scene_detector = scene_detector or PySceneDetectAdapter()
        self.hash_function = hash_function
        self.probe_function = probe_function
        self.sample_function = sample_function
        self.detector_clock = detector_clock
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.progress = progress
        self.html_renderer = html_renderer or HTMLReportRenderer()
        self.model_runtime = model_runtime
        self.cancellation_callback = cancellation_callback

    def run(
        self,
        input_path: Path,
        *,
        prompt: str | None = None,
    ) -> AnalysisResult:
        """Run the complete analysis and atomically publish report artifacts."""
        source = Path(input_path)
        if not source.is_file():
            raise AnalysisInputError(f"Input video not found: {source.name}")
        selected_ids = self._selected_detector_ids()
        output_directory = self.config.output_directory.resolve()
        if output_directory.exists() and not output_directory.is_dir():
            raise AnalysisInputError(
                f"Output path is not a directory: {output_directory.name}"
            )

        output_existed = output_directory.exists()
        workspace_root: Path | None = None
        staging_directory: Path | None = None
        published = False
        stage = "initialization"
        try:
            output_directory.mkdir(parents=True, exist_ok=True)
            staging_directory = Path(
                tempfile.mkdtemp(
                    prefix=".videoscope-staging-",
                    dir=output_directory,
                )
            )
            workspace_root = self._create_workspace(output_directory)

            stage = "input hashing"
            self._check_cancelled()
            self._emit("Computing input hash")
            input_hash = self.hash_function(source)

            stage = "video probing"
            self._check_cancelled()
            self._emit("Probing video metadata")
            metadata = self.probe_function(source, ffprobe=self.ffprobe)

            stage = "frame sampling"
            self._check_cancelled()
            self._emit("Sampling analysis frames")
            sampling = self.sample_function(
                source,
                sample_rate=self.config.sample_fps,
                max_edge=self.config.thumbnail_max_size,
                image_format="jpeg",
                workspace_parent=workspace_root,
                ffmpeg=self.ffmpeg,
            )

            stage = "scene segmentation"
            self._check_cancelled()
            self._emit("Detecting scene boundaries")
            scene_result = self.scene_detector.detect(
                source,
                duration_seconds=metadata.duration_seconds,
            )

            stage = "detector execution"
            self._check_cancelled()
            self._emit("Running detectors")
            shared_cache: dict[str, object] = (
                {}
                if self.model_runtime is None
                else {MODEL_RUNTIME_CACHE_KEY: self.model_runtime}
            )
            context = AnalysisContext(
                input_path=source,
                input_hash=input_hash,
                metadata=metadata,
                prompt=prompt,
                frame_samples=sampling.samples,
                scenes=scene_result.scenes,
                workspace=sampling.work_directory,
                shared_cache=shared_cache,
                cancellation_callback=self.cancellation_callback,
            )
            detector_result = DetectorRunner(
                self.registry,
                clock=self.detector_clock,
                progress=self._emit,
            ).run(
                context,
                detector_ids=selected_ids,
                configurations=self.config.detector_configurations,
            )

            stage = "evidence materialization"
            self._check_cancelled()
            self._emit("Materializing evidence frames")
            findings = EvidenceManager(
                workspace=sampling.work_directory,
                output_directory=staging_directory,
                frame_samples=sampling.samples,
            ).materialize(detector_result.findings)

            stage = "report construction"
            self._check_cancelled()
            self._emit("Building analysis report")
            report = self._build_report(
                input_hash=input_hash,
                prompt=prompt,
                metadata=metadata,
                selected_ids=selected_ids,
                scene_result=scene_result,
                sample_count=len(sampling.samples),
                executions=detector_result.executions,
                findings=findings,
                detector_diagnostics=self._detector_diagnostics(context.shared_cache),
            )
            write_report_json(report, staging_directory / "report.json")

            bundled_video_relative_path: str | None = None
            if self.config.bundle_video:
                stage = "source video bundling"
                self._check_cancelled()
                self._emit("Bundling source video")
                bundled_video_relative_path = self._bundle_video(
                    source,
                    staging_directory,
                )

            if not self.config.json_only:
                stage = "HTML report rendering"
                self._check_cancelled()
                self._emit("Rendering offline HTML report")
                try:
                    self.html_renderer.render(
                        report,
                        staging_directory,
                        bundled_video_relative_path=bundled_video_relative_path,
                    )
                except Exception as exc:
                    report.warnings.append(
                        "HTML report rendering failed; report.json and evidence "
                        "were preserved."
                    )
                    write_report_json(report, staging_directory / "report.json")
                    self._publish(staging_directory, output_directory)
                    published = True
                    raise AnalysisInternalError(
                        "HTML report rendering failed; report.json was preserved"
                    ) from exc

            stage = "artifact publication"
            self._check_cancelled()
            self._publish(staging_directory, output_directory)
            published = True
            self._emit("Analysis complete")
            return AnalysisResult(
                report=report,
                report_path=output_directory / "report.json",
                html_report_path=(
                    None if self.config.json_only else output_directory / "report.html"
                ),
                bundled_video_path=(
                    None
                    if bundled_video_relative_path is None
                    else output_directory / bundled_video_relative_path
                ),
                workspace_directory=(
                    workspace_root if self.config.keep_workspace else None
                ),
            )
        except VideoProcessingError as exc:
            raise AnalysisProcessingError(str(exc)) from exc
        except (
            AnalysisCancelledError,
            AnalysisConfigError,
            AnalysisInputError,
            AnalysisInternalError,
        ):
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise AnalysisInternalError(
                f"Analysis failed during {stage}: {type(exc).__name__}"
            ) from exc
        finally:
            if staging_directory is not None:
                shutil.rmtree(staging_directory, ignore_errors=True)
            if workspace_root is not None and not self.config.keep_workspace:
                shutil.rmtree(workspace_root, ignore_errors=True)
            if not published and not output_existed and output_directory.is_dir():
                try:
                    output_directory.rmdir()
                except OSError:
                    pass

    def _selected_detector_ids(self) -> tuple[str, ...]:
        available = {detector.id for detector in self.registry.list_available()}
        configured_ids = set(self.config.detector_configurations)
        unknown_configurations = configured_ids - available
        if unknown_configurations:
            unknown_configuration_names = ", ".join(sorted(unknown_configurations))
            raise AnalysisConfigError(
                f"Unknown detector configuration ID(s): {unknown_configuration_names}"
            )
        if self.config.enabled_detectors is None:
            selected = tuple(
                detector.id for detector in self.registry.list_default_enabled()
            )
        else:
            selected = self.config.enabled_detectors
        unknown_detector_ids = set(selected) - available
        if unknown_detector_ids:
            names = ", ".join(sorted(unknown_detector_ids))
            raise AnalysisConfigError(f"Unknown detector ID(s): {names}")
        return tuple(sorted(selected))

    def _create_workspace(self, output_directory: Path) -> Path:
        if self.config.keep_workspace:
            workspace_parent = output_directory / "workspace"
            workspace_parent.mkdir(parents=True, exist_ok=True)
            return Path(
                tempfile.mkdtemp(
                    prefix="analysis-",
                    dir=workspace_parent,
                )
            )
        return Path(tempfile.mkdtemp(prefix="videoscope-analysis-"))

    def _effective_detector_configurations(
        self,
        selected_ids: tuple[str, ...],
    ) -> dict[str, JsonValue]:
        configurations: dict[str, JsonValue] = {}
        for detector_id in selected_ids:
            detector = self.registry.get(detector_id)
            raw = self.config.detector_configurations.get(detector_id, {})
            try:
                configurations[detector_id] = detector.config_model.model_validate(
                    raw
                ).model_dump(mode="json")
            except ValidationError:
                configurations[detector_id] = {"configuration_valid": False}
        return configurations

    def _build_report(
        self,
        *,
        input_hash: str,
        prompt: str | None,
        metadata: VideoMetadata,
        selected_ids: tuple[str, ...],
        scene_result: SceneDetectionResult,
        sample_count: int,
        executions: tuple[DetectorExecution, ...],
        findings: tuple[Finding, ...],
        detector_diagnostics: dict[str, JsonValue],
    ) -> AnalysisReport:
        warnings = list(scene_result.warnings)
        if any(
            execution.status is DetectorStatus.DETECTOR_ERROR
            for execution in executions
        ):
            warnings.append("One or more detectors failed; see detector_executions.")
        configuration: dict[str, JsonValue] = {
            "sample_fps": self.config.sample_fps,
            "thumbnail_max_size": self.config.thumbnail_max_size,
            "enabled_detectors": list(selected_ids),
            "detectors": self._effective_detector_configurations(selected_ids),
            "keep_workspace": self.config.keep_workspace,
            "output_directory": ".",
            "locale": self.config.locale,
            "json_only": self.config.json_only,
            "bundle_video": self.config.bundle_video,
        }
        runtime: dict[str, JsonValue] = {
            "python_version": platform.python_version(),
            "scene_segmentation_source": scene_result.source,
            "sample_count": sample_count,
            "scene_count": len(scene_result.scenes),
        }
        if self.model_runtime is not None:
            runtime["model_runtime"] = {
                "device": self.model_runtime.config.device.value,
                "precision": self.model_runtime.config.precision.value,
                "batch_size": self.model_runtime.config.batch_size,
                "memory_budget_bytes": (self.model_runtime.config.memory_budget_bytes),
                "allow_model_download": (
                    self.model_runtime.config.allow_model_download
                ),
            }
            runtime["model_runs"] = [
                record.model_dump(mode="json")
                for record in self.model_runtime.records()
            ]
        if detector_diagnostics:
            runtime["detector_diagnostics"] = detector_diagnostics
        return AnalysisReport(
            tool_version=__version__,
            analysis_id=uuid4().hex,
            created_at=datetime.now(UTC),
            input_hash=input_hash,
            prompt=prompt,
            metadata=metadata,
            configuration=configuration,
            detector_executions=list(executions),
            findings=list(findings),
            warnings=warnings,
            runtime=runtime,
        )

    @staticmethod
    def _detector_diagnostics(
        shared_cache: dict[str, object],
    ) -> dict[str, JsonValue]:
        diagnostics = shared_cache.get(DETECTOR_DIAGNOSTICS_CACHE_KEY)
        if diagnostics is None:
            return {}
        if not isinstance(diagnostics, dict):
            raise AnalysisInternalError("Detector diagnostics have an invalid type")
        try:
            adapter: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
            return {
                str(detector_id): adapter.validate_python(value)
                for detector_id, value in sorted(diagnostics.items())
            }
        except (TypeError, ValueError) as exc:
            raise AnalysisInternalError(
                "Detector diagnostics are not JSON-compatible"
            ) from exc

    @staticmethod
    def _publish(staging_directory: Path, output_directory: Path) -> None:
        backup_directory = staging_directory / "backup"
        published_destinations: list[Path] = []
        backups: list[tuple[Path, Path]] = []
        created_directories: list[Path] = []
        try:
            for directory_name in ("evidence", "media"):
                staged_directory = staging_directory / directory_name
                destination_directory = output_directory / directory_name
                if not staged_directory.is_dir():
                    continue
                if not destination_directory.exists():
                    created_directories.append(destination_directory)
                destination_directory.mkdir(parents=True, exist_ok=True)
                for source in sorted(staged_directory.iterdir()):
                    destination = destination_directory / source.name
                    AnalysisPipeline._backup_existing(
                        destination,
                        backup_directory / directory_name / source.name,
                        backups,
                    )
                    source.replace(destination)
                    published_destinations.append(destination)

            staged_html = staging_directory / "report.html"
            if staged_html.is_file():
                html_destination = output_directory / "report.html"
                AnalysisPipeline._backup_existing(
                    html_destination,
                    backup_directory / "report.html",
                    backups,
                )
                staged_html.replace(html_destination)
                published_destinations.append(html_destination)

            report_destination = output_directory / "report.json"
            AnalysisPipeline._backup_existing(
                report_destination,
                backup_directory / "report.json",
                backups,
            )
            (staging_directory / "report.json").replace(report_destination)
            published_destinations.append(report_destination)
        except BaseException:
            for destination in reversed(published_destinations):
                destination.unlink(missing_ok=True)
            for backup, destination in reversed(backups):
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup.replace(destination)
            for created_directory in reversed(created_directories):
                try:
                    created_directory.rmdir()
                except OSError:
                    pass
            raise

    @staticmethod
    def _bundle_video(source: Path, staging_directory: Path) -> str:
        suffix = source.suffix.lower()
        if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) is None:
            suffix = ".bin"
        relative_path = Path("media") / f"bundled-video{suffix}"
        destination = staging_directory / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return relative_path.as_posix()

    @staticmethod
    def _backup_existing(
        destination: Path,
        backup: Path,
        backups: list[tuple[Path, Path]],
    ) -> None:
        if not destination.exists():
            return
        backup.parent.mkdir(parents=True, exist_ok=True)
        destination.replace(backup)
        backups.append((backup, destination))

    def _emit(self, message: str) -> None:
        if self.progress is not None:
            self.progress(message)

    def _check_cancelled(self) -> None:
        if self.cancellation_callback is not None and self.cancellation_callback():
            raise AnalysisCancelledError("Analysis cancelled by caller")
