"""Core preparation, confirmation, processing, and publication orchestration."""

from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from videoscope.analysis import (
    AnalysisCancelledError,
    AnalysisConfig,
    AnalysisError,
    AnalysisInputError,
    AnalysisPipeline,
    AnalysisProcessingError,
    AnalysisResult,
)
from videoscope.domain import AnalysisReport, VideoMetadata
from videoscope.resolve.errors import (
    PublishArtifactError,
    PublishCancelledError,
    PublishConfirmationError,
    PublishInputError,
    ResolveError,
)
from videoscope.resolve.executor import NativePublishExecutor, NativePublishResult
from videoscope.resolve.models import (
    PublishArtifact,
    PublishChangeLog,
    PublishPlan,
    PublishProfileId,
    PublishTechnicalReport,
    VerificationReport,
    VerificationStatus,
    make_publish_plan_digest,
)
from videoscope.resolve.planner import build_publish_plan
from videoscope.resolve.profiles import PublishProfile, get_publish_profile
from videoscope.resolve.serialization import (
    publish_plan_to_json,
    write_publish_change_log_json,
    write_publish_plan_json,
    write_publish_technical_report_json,
)
from videoscope.resolve.verification import PublishVerifier
from videoscope.video import VideoProcessingError, compute_file_sha256

_WINDOWS_NO_REPLACE_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08, 0.16)

ProgressCallback = Callable[[str], None]
CancellationCallback = Callable[[], bool]

_PLAN_PATH = "plan.json"
_PREVIEW_PATH = "preview/publish-preview.mp4"
_VIDEO_PATH = "publish-ready.mp4"
_COVER_PATH = "cover.jpg"
_CHANGES_PATH = "changes.json"
_TECHNICAL_REPORT_PATH = "technical-report.json"
_ANALYSIS_BEFORE_PATH = "analysis-before/report.json"
_ANALYSIS_AFTER_PATH = "analysis-after/report.json"


class PublishReadyStatus(StrEnum):
    """Observable states in the fixed Publish Ready lifecycle."""

    CREATED = "created"
    INSPECTING = "inspecting"
    PLANNING = "planning"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    PROCESSING = "processing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PublishReadyConfig(BaseModel):
    """Strict public configuration for one local Publish Ready task."""

    model_config = ConfigDict(extra="forbid")

    profile_id: PublishProfileId
    output_directory: Path = Path("videoscope-publish-output")
    preview_seconds: float = Field(default=6.0, gt=0, le=10, allow_inf_nan=False)
    keep_workspace: bool = False
    run_diagnostics: bool = True


@dataclass(frozen=True, slots=True)
class PublishPreparation:
    """Path-bearing, in-process state that is never serialized as public JSON."""

    plan: PublishPlan
    source_path: Path
    workspace_directory: Path
    output_directory: Path
    preview_path: Path
    plan_path: Path
    analysis_before: AnalysisReport
    analysis_before_report_path: Path


@dataclass(frozen=True, slots=True)
class PublishResult:
    """A published, verified output and its public artifact locations."""

    status: PublishReadyStatus
    output_directory: Path
    video_path: Path
    cover_path: Path
    preview_path: Path
    change_log_path: Path
    technical_report_path: Path
    analysis_before_report_path: Path
    analysis_after_report_path: Path
    change_log: PublishChangeLog
    technical_report: PublishTechnicalReport


@dataclass(frozen=True, slots=True)
class _IssuedPreparation:
    """Immutable confirmation state retained only by the issuing pipeline."""

    preparation: PublishPreparation
    canonical_plan: str
    plan_digest: str


class AnalysisRunner(Protocol):
    def run(self, input_path: Path, *, prompt: str | None = None) -> AnalysisResult: ...


class AnalysisPipelineFactory(Protocol):
    def __call__(self, output_directory: Path) -> AnalysisRunner: ...


class PlanBuilder(Protocol):
    def __call__(
        self,
        metadata: VideoMetadata,
        input_hash: str,
        profile_id: PublishProfileId,
        *,
        preview_seconds: float,
        keep_workspace: bool,
        run_diagnostics: bool,
    ) -> PublishPlan: ...


class PublishExecutor(Protocol):
    def generate_preview(
        self, plan: PublishPlan, source_path: Path, work_directory: Path
    ) -> Path: ...

    def execute(
        self, plan: PublishPlan, source_path: Path, work_directory: Path
    ) -> NativePublishResult: ...


class PublishOutputVerifier(Protocol):
    def verify(
        self,
        *,
        source_metadata: VideoMetadata,
        output_metadata: VideoMetadata | None,
        profile: PublishProfile,
        before: AnalysisReport,
        after: AnalysisReport | None,
    ) -> VerificationReport: ...


class HashFunction(Protocol):
    def __call__(self, path: Path) -> str: ...


class PublishReadyPipeline:
    """Run the single core Publish Ready service used by later adapters."""

    def __init__(
        self,
        config: PublishReadyConfig,
        *,
        analysis_pipeline_factory: AnalysisPipelineFactory | None = None,
        planner: PlanBuilder = build_publish_plan,
        executor: PublishExecutor | None = None,
        verifier: PublishOutputVerifier | None = None,
        progress: ProgressCallback | None = None,
        cancellation_callback: CancellationCallback | None = None,
        hash_function: HashFunction = compute_file_sha256,
    ) -> None:
        self.config = config
        self._progress = progress
        self._cancellation_callback = cancellation_callback
        self._analysis_pipeline_factory = (
            analysis_pipeline_factory or self._create_analysis_pipeline
        )
        self._planner = planner
        self._executor = executor or NativePublishExecutor(
            preview_seconds=config.preview_seconds, is_cancelled=cancellation_callback
        )
        self._verifier = verifier or PublishVerifier()
        self._hash_function = hash_function
        self._issued_preparations: dict[int, _IssuedPreparation] = {}

    def prepare(self, input_path: Path) -> PublishPreparation:
        """Inspect the source and materialize a confirmable plan and preview."""
        self._emit(PublishReadyStatus.CREATED)
        self._emit(PublishReadyStatus.INSPECTING)
        source = Path(input_path)
        workspace: Path | None = None
        prepared = False
        try:
            self._check_cancelled()
            self._validate_source(source)
            output = self._resolved_output_directory()
            self._reject_output_collision(output)
            workspace = self._create_workspace(output)

            self._check_cancelled()
            input_hash = self._hash_function(source)
            before = self._run_analysis(
                source,
                workspace / "analysis-before",
                stage="baseline",
            )
            if before.report.input_hash != input_hash:
                raise PublishArtifactError(
                    "Baseline analysis input hash did not match the inspected source"
                )

            self._emit(PublishReadyStatus.PLANNING)
            self._check_cancelled()
            plan = self._planner(
                before.report.metadata,
                input_hash,
                self.config.profile_id,
                preview_seconds=self.config.preview_seconds,
                keep_workspace=self.config.keep_workspace,
                run_diagnostics=self.config.run_diagnostics,
            )
            plan_path = workspace / _PLAN_PATH
            write_publish_plan_json(plan, plan_path)
            preview_path = self._executor.generate_preview(plan, source, workspace)
            self._require_file(preview_path, "preview")

            preparation = PublishPreparation(
                plan=plan,
                source_path=source,
                workspace_directory=workspace,
                output_directory=output,
                preview_path=preview_path,
                plan_path=plan_path,
                analysis_before=before.report,
                analysis_before_report_path=before.report_path,
            )
            self._emit(PublishReadyStatus.AWAITING_CONFIRMATION)
            self._issued_preparations[id(preparation)] = _IssuedPreparation(
                preparation=preparation,
                canonical_plan=publish_plan_to_json(plan),
                plan_digest=plan.plan_digest,
            )
            prepared = True
            return preparation
        except PublishCancelledError:
            self._emit(PublishReadyStatus.CANCELLED, terminal=True)
            raise
        except ResolveError:
            self._emit(PublishReadyStatus.FAILED, terminal=True)
            raise
        except AnalysisCancelledError as exc:
            self._emit(PublishReadyStatus.CANCELLED, terminal=True)
            raise PublishCancelledError(
                "Publish Ready processing was cancelled"
            ) from exc
        except AnalysisError as exc:
            self._emit(PublishReadyStatus.FAILED, terminal=True)
            raise self._map_analysis_error(exc, stage="baseline") from exc
        except VideoProcessingError as exc:
            self._emit(PublishReadyStatus.FAILED, terminal=True)
            raise PublishInputError("Source video could not be read") from exc
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._emit(PublishReadyStatus.FAILED, terminal=True)
            raise PublishArtifactError(
                "Publish Ready preparation failed during local orchestration"
            ) from exc
        finally:
            if (
                workspace is not None
                and not prepared
                and not self.config.keep_workspace
            ):
                self._cleanup_workspace(workspace)

    def execute(
        self,
        preparation: PublishPreparation,
        confirmed_plan_digest: str,
    ) -> PublishResult:
        """Execute one exactly confirmed plan and atomically publish its artifacts."""
        self._validate_confirmation(preparation, confirmed_plan_digest)
        issued = self._claim_preparation(preparation)
        published = False
        workspace = preparation.workspace_directory
        try:
            self._check_cancelled()
            self._validate_preparation(preparation)
            self._reject_output_collision(preparation.output_directory)
            self._emit(PublishReadyStatus.PROCESSING)
            self._validate_issued_plan(preparation, issued)
            native_result = self._executor.execute(
                preparation.plan,
                preparation.source_path,
                workspace,
            )
            self._require_file(native_result.video_path, "publish output")
            self._require_file(native_result.cover_path, "cover")

            self._check_cancelled()
            after = self._run_analysis(
                native_result.video_path,
                workspace / "analysis-after",
                stage="output",
            )
            self._emit(PublishReadyStatus.VERIFYING)
            self._check_cancelled()
            profile = get_publish_profile(preparation.plan.profile_id)
            verification = self._verifier.verify(
                source_metadata=preparation.plan.source_metadata,
                output_metadata=after.report.metadata,
                profile=profile,
                before=preparation.analysis_before,
                after=after.report,
            )
            self._check_cancelled()
            change_log, technical_report = self._write_public_reports(
                preparation,
                verification,
            )
            if verification.status is VerificationStatus.FAILED:
                raise PublishArtifactError("Publish Ready output failed verification")
            self._publish_directory(workspace, preparation.output_directory)
            published = True
            terminal = (
                PublishReadyStatus.COMPLETED
                if verification.status is VerificationStatus.PASSED
                else PublishReadyStatus.NEEDS_REVIEW
            )
            self._emit(terminal, terminal=True)
            return self._published_result(
                preparation.output_directory,
                terminal,
                change_log,
                technical_report,
            )
        except PublishCancelledError:
            self._emit(PublishReadyStatus.CANCELLED, terminal=True)
            raise
        except ResolveError:
            self._emit(PublishReadyStatus.FAILED, terminal=True)
            raise
        except AnalysisCancelledError as exc:
            self._emit(PublishReadyStatus.CANCELLED, terminal=True)
            raise PublishCancelledError(
                "Publish Ready processing was cancelled"
            ) from exc
        except AnalysisError as exc:
            self._emit(PublishReadyStatus.FAILED, terminal=True)
            raise self._map_analysis_error(exc, stage="output") from exc
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._emit(PublishReadyStatus.FAILED, terminal=True)
            raise PublishArtifactError(
                "Publish Ready execution failed during local orchestration"
            ) from exc
        finally:
            if not published and not self.config.keep_workspace:
                self._cleanup_workspace(workspace)

    def discard(self, preparation: PublishPreparation) -> None:
        """Dispose one still-issued preparation without following forged paths."""
        issued = self._issued_preparations.pop(id(preparation), None)
        if issued is None or issued.preparation is not preparation:
            return
        self._cleanup_workspace(preparation.workspace_directory)

    def publish_preview(self, preparation: PublishPreparation) -> Path:
        """Publish a preview-only preparation at the requested stable output root."""
        issued = self._claim_preparation(preparation)
        published = False
        try:
            self._validate_preparation(preparation)
            self._validate_issued_plan(preparation, issued)
            self._reject_output_collision(preparation.output_directory)
            self._publish_directory(
                preparation.workspace_directory,
                preparation.output_directory,
            )
            published = True
            preview = preparation.output_directory / Path(_PREVIEW_PATH)
            self._require_file(preview, "preview")
            return preview
        finally:
            if not published:
                self._cleanup_workspace(preparation.workspace_directory)

    def _create_analysis_pipeline(self, output_directory: Path) -> AnalysisPipeline:
        enabled_detectors = None if self.config.run_diagnostics else ()
        return AnalysisPipeline(
            AnalysisConfig(
                output_directory=output_directory,
                enabled_detectors=enabled_detectors,
                json_only=True,
            ),
            cancellation_callback=self._cancellation_callback,
        )

    def _run_analysis(
        self,
        source: Path,
        output_directory: Path,
        *,
        stage: str,
    ) -> AnalysisResult:
        try:
            result = self._analysis_pipeline_factory(output_directory).run(source)
        except AnalysisError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise PublishArtifactError(
                f"Publish Ready {stage} analysis failed"
            ) from exc
        self._require_file(result.report_path, f"{stage} analysis report")
        return result

    def _write_public_reports(
        self,
        preparation: PublishPreparation,
        verification: VerificationReport,
    ) -> tuple[PublishChangeLog, PublishTechnicalReport]:
        root = preparation.workspace_directory
        base_artifacts = tuple(
            self._artifact(root, relative_path, description)
            for relative_path, description in (
                (_PLAN_PATH, "Confirmed Publish Ready plan."),
                (_PREVIEW_PATH, "Short local preview of the planned transform."),
                (_VIDEO_PATH, "Processed Publish Ready video."),
                (_COVER_PATH, "Representative cover image."),
                (_ANALYSIS_BEFORE_PATH, "Baseline VideoScope analysis report."),
                (_ANALYSIS_AFTER_PATH, "Post-processing VideoScope analysis report."),
            )
        )
        change_log = PublishChangeLog(
            plan_digest=preparation.plan.plan_digest,
            actions=preparation.plan.actions,
            artifacts=base_artifacts,
        )
        changes_path = root / _CHANGES_PATH
        write_publish_change_log_json(change_log, changes_path)
        technical_report = PublishTechnicalReport(
            plan_digest=preparation.plan.plan_digest,
            verification=verification,
            artifacts=(
                *base_artifacts,
                self._artifact(root, _CHANGES_PATH, "Executed action change log."),
            ),
        )
        write_publish_technical_report_json(
            technical_report,
            root / _TECHNICAL_REPORT_PATH,
        )
        return change_log, technical_report

    def _artifact(
        self,
        root: Path,
        relative_path: str,
        description: str,
    ) -> PublishArtifact:
        path = root / Path(relative_path)
        self._require_file(path, description)
        return PublishArtifact(
            relative_path=relative_path,
            sha256=self._hash_function(path),
            description=description,
        )

    def _validate_preparation(self, preparation: PublishPreparation) -> None:
        if preparation.output_directory != self._resolved_output_directory():
            raise PublishConfirmationError(
                "Preparation output does not match the pipeline configuration"
            )
        if not preparation.workspace_directory.is_dir():
            raise PublishArtifactError(
                "Publish Ready preparation is no longer available"
            )
        self._validate_source(preparation.source_path)
        current_hash = self._hash_function(preparation.source_path)
        if current_hash != preparation.plan.input_hash:
            raise PublishConfirmationError(
                "Source video changed after the Publish Ready plan was prepared"
            )

    @staticmethod
    def _validate_issued_plan(
        preparation: PublishPreparation,
        issued: _IssuedPreparation,
    ) -> None:
        try:
            effective_digest = make_publish_plan_digest(
                schema_version=preparation.plan.schema_version,
                task_id=preparation.plan.task_id,
                input_hash=preparation.plan.input_hash,
                source_read_only=preparation.plan.source_read_only,
                profile_id=preparation.plan.profile_id,
                profile_version=preparation.plan.profile_version,
                backend=preparation.plan.backend,
                actions=preparation.plan.actions,
                preview_artifact=preparation.plan.preview_artifact,
                confirmation_required=preparation.plan.confirmation_required,
                expected_artifacts=preparation.plan.expected_artifacts,
                effective_config=preparation.plan.effective_config,
                output_filename=preparation.plan.output_filename,
            )
            canonical_plan = publish_plan_to_json(preparation.plan)
        except (TypeError, ValueError) as exc:
            raise PublishConfirmationError(
                "Prepared Publish Ready plan changed after confirmation"
            ) from exc
        if (
            preparation.plan.plan_digest != issued.plan_digest
            or effective_digest != issued.plan_digest
            or canonical_plan != issued.canonical_plan
        ):
            raise PublishConfirmationError(
                "Prepared Publish Ready plan changed after confirmation"
            )

    @staticmethod
    def _validate_confirmation(
        preparation: PublishPreparation,
        confirmed_plan_digest: str,
    ) -> None:
        if not confirmed_plan_digest:
            raise PublishConfirmationError("A confirmed plan digest is required")
        if confirmed_plan_digest != preparation.plan.plan_digest:
            raise PublishConfirmationError(
                "Confirmed plan digest does not match the prepared plan digest"
            )

    def _claim_preparation(self, preparation: PublishPreparation) -> _IssuedPreparation:
        issued = self._issued_preparations.pop(id(preparation), None)
        if issued is None or issued.preparation is not preparation:
            raise PublishConfirmationError(
                "Publish Ready preparation was not issued by this pipeline"
            )
        return issued

    @staticmethod
    def _validate_source(source: Path) -> None:
        if not source.is_file():
            raise PublishInputError(f"Source video was not found: {source.name}")

    def _resolved_output_directory(self) -> Path:
        try:
            candidate = self.config.output_directory
            return candidate.parent.resolve(strict=False) / candidate.name
        except OSError as exc:
            raise PublishInputError(
                "Publish output path could not be resolved"
            ) from exc

    @staticmethod
    def _reject_output_collision(output: Path) -> None:
        if os.path.lexists(output):
            raise PublishArtifactError("Publish Ready output already exists")

    @staticmethod
    def _create_workspace(output: Path) -> Path:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            return Path(
                tempfile.mkdtemp(
                    prefix=f".{output.name}.staging-",
                    dir=output.parent,
                )
            )
        except OSError as exc:
            raise PublishArtifactError(
                "Publish Ready staging workspace could not be created"
            ) from exc

    @classmethod
    def _publish_directory(cls, workspace: Path, output: Path) -> None:
        cls._reject_output_collision(output)
        try:
            cls._rename_directory_no_replace(workspace, output)
        except FileExistsError:
            raise PublishArtifactError("Publish Ready output already exists")
        except OSError as exc:
            if os.path.lexists(output):
                raise PublishArtifactError(
                    "Publish Ready output already exists"
                ) from exc
            raise PublishArtifactError(
                "Publish Ready artifact directory could not be published"
            ) from exc

    @staticmethod
    def _rename_directory_no_replace(workspace: Path, output: Path) -> None:
        """Atomically move a staging directory without replacing an output root."""
        if os.name == "nt":
            win_dll = getattr(ctypes, "WinDLL")
            kernel32 = win_dll("kernel32", use_last_error=True)
            retry_delays: tuple[float | None, ...] = (
                *_WINDOWS_NO_REPLACE_RETRY_DELAYS_SECONDS,
                None,
            )
            for retry_delay in retry_delays:
                if kernel32.MoveFileExW(str(workspace), str(output), 0x00000008) != 0:
                    return
                error_code = int(getattr(ctypes, "get_last_error")())
                if error_code in {80, 183}:
                    raise FileExistsError(error_code, os.strerror(error_code), output)
                error = OSError(error_code, os.strerror(error_code), output)
                if (
                    error_code != 5
                    or retry_delay is None
                    or not os.path.lexists(workspace)
                    or os.path.lexists(output)
                ):
                    raise error
                time.sleep(retry_delay)
            raise AssertionError("Windows directory rename retry loop exhausted")
        if sys.platform == "linux":
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is None:
                raise OSError("Atomic no-replace publication is unavailable")
            if (
                renameat2(
                    -100,
                    os.fsencode(workspace),
                    -100,
                    os.fsencode(output),
                    1,
                )
                != 0
            ):
                error_code = ctypes.get_errno()
                if error_code in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise FileExistsError(error_code, os.strerror(error_code), output)
                raise OSError(error_code, os.strerror(error_code), output)
            return
        if sys.platform == "darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            if (
                libc.renamex_np(os.fsencode(workspace), os.fsencode(output), 0x00000004)
                != 0
            ):
                error_code = ctypes.get_errno()
                if error_code in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise FileExistsError(error_code, os.strerror(error_code), output)
                raise OSError(error_code, os.strerror(error_code), output)
            return
        raise OSError("Atomic no-replace publication is unavailable on this platform")

    @staticmethod
    def _require_file(path: Path, artifact_name: str) -> None:
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                raise PublishArtifactError(
                    f"Publish Ready {artifact_name} artifact is missing or empty"
                )
        except OSError as exc:
            raise PublishArtifactError(
                f"Publish Ready {artifact_name} artifact could not be inspected"
            ) from exc

    @staticmethod
    def _remove_tree(path: Path) -> None:
        shutil.rmtree(path)

    def _cleanup_workspace(self, workspace: Path) -> None:
        if not os.path.lexists(workspace):
            return
        try:
            self._remove_tree(workspace)
        except OSError as exc:
            raise PublishArtifactError(
                "Publish Ready workspace cleanup failed"
            ) from exc
        if os.path.lexists(workspace):
            raise PublishArtifactError("Publish Ready workspace cleanup was incomplete")

    def _check_cancelled(self) -> None:
        if self._cancellation_callback is not None and self._cancellation_callback():
            raise PublishCancelledError("Publish Ready processing was cancelled")

    def _emit(self, status: PublishReadyStatus, *, terminal: bool = False) -> None:
        if self._progress is not None:
            try:
                self._progress(status.value)
            except Exception:
                if not terminal:
                    raise

    @staticmethod
    def _map_analysis_error(exc: AnalysisError, *, stage: str) -> ResolveError:
        if isinstance(exc, AnalysisCancelledError):
            return PublishCancelledError("Publish Ready processing was cancelled")
        if isinstance(exc, AnalysisInputError):
            if stage == "output":
                from videoscope.resolve.errors import PublishMediaError

                return PublishMediaError(
                    "Publish Ready output analysis could not process the media"
                )
            return PublishInputError(
                f"Publish Ready {stage} analysis rejected the input"
            )
        if isinstance(exc, AnalysisProcessingError):
            from videoscope.resolve.errors import PublishMediaError

            return PublishMediaError(
                f"Publish Ready {stage} analysis could not process the media"
            )
        return PublishArtifactError(f"Publish Ready {stage} analysis failed")

    @staticmethod
    def _published_result(
        output: Path,
        status: PublishReadyStatus,
        change_log: PublishChangeLog,
        technical_report: PublishTechnicalReport,
    ) -> PublishResult:
        return PublishResult(
            status=status,
            output_directory=output,
            video_path=output / _VIDEO_PATH,
            cover_path=output / _COVER_PATH,
            preview_path=output / Path(_PREVIEW_PATH),
            change_log_path=output / _CHANGES_PATH,
            technical_report_path=output / _TECHNICAL_REPORT_PATH,
            analysis_before_report_path=output / Path(_ANALYSIS_BEFORE_PATH),
            analysis_after_report_path=output / Path(_ANALYSIS_AFTER_PATH),
            change_log=change_log,
            technical_report=technical_report,
        )


__all__ = [
    "AnalysisPipelineFactory",
    "CancellationCallback",
    "ProgressCallback",
    "PublishPreparation",
    "PublishReadyConfig",
    "PublishReadyPipeline",
    "PublishReadyStatus",
    "PublishResult",
]
