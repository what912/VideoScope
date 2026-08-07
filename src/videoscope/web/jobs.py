"""Thread-safe local job scheduling around the shared AnalysisPipeline."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeAlias

from videoscope.ai import (
    DevicePreference,
    ModelRuntimeConfig,
    ModelRuntimeManager,
    create_model_runtime,
)
from videoscope.analysis import (
    AnalysisCancelledError,
    AnalysisConfig,
    AnalysisError,
    AnalysisPipeline,
)
from videoscope.detectors import (
    DetectorRegistry,
    create_builtin_detector_registry,
    create_optional_detector_registry,
)
from videoscope.video.errors import sanitize_diagnostic
from videoscope.web.models import (
    JobEvent,
    JobResponse,
    JobStatus,
    WebServerConfig,
)
from videoscope.web.storage import LocalJobStore

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_AI_DETECTORS = {"prompt_alignment", "visual_semantic_drift"}
_OCR_DETECTORS = {"text_stability"}
_PROGRESS_STATES: tuple[tuple[str, JobStatus], ...] = (
    ("computing input hash", JobStatus.PROBING),
    ("probing video metadata", JobStatus.PROBING),
    ("sampling analysis frames", JobStatus.SAMPLING),
    ("detecting scene boundaries", JobStatus.DETECTING),
    ("running detectors", JobStatus.DETECTING),
    ("materializing evidence frames", JobStatus.DETECTING),
    ("building analysis report", JobStatus.RENDERING),
    ("bundling source video", JobStatus.RENDERING),
    ("rendering offline html report", JobStatus.RENDERING),
)


class CpuJobLimiter:
    """Bound CPU work shared by every manager in one local Web app."""

    def __init__(self, slots: int) -> None:
        self._semaphore = threading.BoundedSemaphore(slots)

    @contextmanager
    def slot(self, cancelled: Callable[[], bool]) -> Iterator[bool]:
        """Wait interruptibly for one shared slot and always release it."""
        acquired = False
        while not acquired:
            if cancelled():
                yield False
                return
            acquired = self._semaphore.acquire(timeout=0.05)
        try:
            yield True
        finally:
            self._semaphore.release()


_STAGE_PROGRESS: dict[JobStatus, int] = {
    JobStatus.QUEUED: 0,
    JobStatus.PROBING: 12,
    JobStatus.SAMPLING: 32,
    JobStatus.DETECTING: 55,
    JobStatus.RENDERING: 90,
    JobStatus.COMPLETED: 100,
    JobStatus.FAILED: 100,
    JobStatus.CANCELLED: 100,
}


class PipelineRunner(Protocol):
    """Small common surface shared by real and injected pipelines."""

    def run(self, input_path: Path, *, prompt: str | None = None) -> object: ...


PipelineFactory: TypeAlias = Callable[..., PipelineRunner]


@dataclass(slots=True)
class JobRecord:
    """Private mutable job state protected by one reentrant lock."""

    job_id: str
    directory: Path
    input_path: Path
    output_directory: Path
    prompt: str | None
    analysis_config: AnalysisConfig
    heavy: bool
    warnings: tuple[str, ...]
    status: JobStatus = JobStatus.QUEUED
    message: str = "Job queued"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    upload_size_bytes: int = 0
    progress_percent: int = 0
    current_detector: str | None = None
    error: str | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None
    events: list[JobEvent] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self._append_event(self.status, self.message)

    def _append_event(self, status: JobStatus, message: str) -> None:
        now = datetime.now(UTC)
        self.updated_at = now
        self.events.append(
            JobEvent(
                sequence=len(self.events) + 1,
                status=status,
                message=message,
                created_at=now,
            )
        )

    def update(
        self,
        status: JobStatus,
        message: str,
        *,
        error: str | None = None,
        progress_percent: int | None = None,
        current_detector: str | None = None,
    ) -> None:
        """Publish a state or progress event unless the job is terminal."""
        with self.lock:
            if self.status.terminal:
                return
            self.status = status
            self.message = message
            self.error = error
            self.progress_percent = (
                _STAGE_PROGRESS[status]
                if progress_percent is None
                else max(0, min(100, progress_percent))
            )
            self.current_detector = current_detector
            self._append_event(status, message)

    def update_upload_size(self, size: int) -> None:
        with self.lock:
            self.upload_size_bytes = size
            self.updated_at = datetime.now(UTC)

    def snapshot(self) -> JobResponse:
        """Return public state without local paths."""
        with self.lock:
            base = f"/api/jobs/{self.job_id}"
            links = {
                "self": base,
                "events": f"{base}/events",
                "report": f"{base}/report",
                "video": f"{base}/video",
                "artifacts": f"{base}/artifacts/{{path}}",
            }
            return JobResponse(
                job_id=self.job_id,
                status=self.status,
                message=self.message,
                created_at=self.created_at,
                updated_at=self.updated_at,
                upload_size_bytes=self.upload_size_bytes,
                progress_percent=self.progress_percent,
                current_detector=self.current_detector,
                warnings=self.warnings,
                error=self.error,
                links=links,
            )

    def events_after(self, sequence: int) -> tuple[JobEvent, ...]:
        with self.lock:
            return tuple(event for event in self.events if event.sequence > sequence)


class JobManager:
    """Own local job directories and separate CPU/heavy worker pools."""

    def __init__(
        self,
        config: WebServerConfig | None = None,
        *,
        pipeline_factory: PipelineFactory = AnalysisPipeline,
        cpu_limiter: CpuJobLimiter | None = None,
    ) -> None:
        self.config = config or WebServerConfig()
        self._store = LocalJobStore(self.config.job_root)
        self.job_root = self._store.root
        self.pipeline_factory = pipeline_factory
        self._cpu_limiter = cpu_limiter or CpuJobLimiter(self.config.cpu_concurrency)
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.RLock()
        self._cpu_executor = ThreadPoolExecutor(
            max_workers=self.config.cpu_concurrency,
            thread_name_prefix="videoscope-cpu",
        )
        self._heavy_executor = ThreadPoolExecutor(
            max_workers=self.config.heavy_ai_concurrency,
            thread_name_prefix="videoscope-heavy",
        )
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None

    def use_cpu_limiter(self, limiter: CpuJobLimiter) -> None:
        """Join the app-wide CPU budget before any work is submitted."""
        with self._lock:
            self._cpu_limiter = limiter

    @staticmethod
    def _optional_profiles(
        configuration: AnalysisConfig,
    ) -> tuple[bool, bool]:
        selected = set(configuration.enabled_detectors or ())
        selected.update(configuration.detector_configurations)
        return bool(selected & _AI_DETECTORS), bool(selected & _OCR_DETECTORS)

    def reserve_job(
        self,
        *,
        original_filename: str,
        prompt: str | None,
        analysis_config: AnalysisConfig,
        warnings: tuple[str, ...] = (),
    ) -> JobRecord:
        """Allocate a random path-safe job directory before streaming upload."""
        paths = self._store.reserve(original_filename)
        enable_ai, enable_ocr = self._optional_profiles(analysis_config)
        record = JobRecord(
            job_id=paths.job_id,
            directory=paths.directory,
            input_path=paths.input_path,
            output_directory=paths.output_directory,
            prompt=prompt,
            analysis_config=analysis_config,
            heavy=enable_ai or enable_ocr,
            warnings=warnings,
        )
        with self._lock:
            self._jobs[paths.job_id] = record
        return record

    def discard_reserved(self, job_id: str) -> None:
        """Remove an upload that never reached the queue."""
        with self._lock:
            record = self._jobs.pop(job_id, None)
        if record is not None:
            self._store.discard(record.job_id)

    def submit(self, job_id: str) -> JobResponse:
        """Queue a completed upload in the matching worker pool."""
        record = self.require(job_id)
        executor = self._heavy_executor if record.heavy else self._cpu_executor
        target = self._run_job if record.heavy else self._run_cpu_job
        future = executor.submit(target, job_id)
        with record.lock:
            record.future = future
        return record.snapshot()

    def _run_cpu_job(self, job_id: str) -> None:
        record = self.require(job_id)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if acquired:
                self._run_job(job_id)
            else:
                record.update(JobStatus.CANCELLED, "Job cancelled before execution")
                self._store.discard(record.job_id)

    def require(self, job_id: str) -> JobRecord:
        """Resolve only canonical random IDs."""
        if _JOB_ID.fullmatch(job_id) is None:
            raise KeyError(job_id)
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(job_id) from exc

    def snapshot(self, job_id: str) -> JobResponse:
        return self.require(job_id).snapshot()

    def active_job_count(self) -> int:
        with self._lock:
            records = tuple(self._jobs.values())
        return sum(not record.snapshot().status.terminal for record in records)

    def events_after(
        self,
        job_id: str,
        sequence: int,
    ) -> tuple[JobEvent, ...]:
        return self.require(job_id).events_after(sequence)

    def cancel(self, job_id: str) -> JobResponse:
        """Request cooperative cancellation and cancel a queued future."""
        record = self.require(job_id)
        record.cancellation.set()
        with record.lock:
            future = record.future
        if future is not None and future.cancel():
            record.update(JobStatus.CANCELLED, "Job cancelled before execution")
            self._store.discard(record.job_id)
        elif not record.snapshot().status.terminal:
            with record.lock:
                record._append_event(record.status, "Cancellation requested")
                record.message = "Cancellation requested"
        return record.snapshot()

    def delete_or_cancel(self, job_id: str) -> JobResponse | None:
        """Delete terminal data immediately or request active cancellation."""
        record = self.require(job_id)
        if not record.snapshot().status.terminal:
            return self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        self._store.discard(record.job_id)
        return None

    def resolve_artifact(self, job_id: str, requested_path: str) -> Path:
        """Resolve one file strictly inside a completed job artifact root."""
        record = self.require(job_id)
        if record.snapshot().status is not JobStatus.COMPLETED:
            raise FileNotFoundError("Job artifacts are not ready")
        return self._store.resolve_artifact(record.job_id, requested_path)

    def report_path(self, job_id: str) -> Path:
        return self.resolve_artifact(job_id, "report.json")

    def _runtime_and_registry(
        self,
        configuration: AnalysisConfig,
    ) -> tuple[DetectorRegistry, ModelRuntimeManager | None]:
        enable_ai, enable_ocr = self._optional_profiles(configuration)
        if not (enable_ai or enable_ocr):
            return create_builtin_detector_registry(), None
        registry = create_optional_detector_registry(
            enable_ai=enable_ai,
            enable_ocr=enable_ocr,
        )
        runtime = create_model_runtime(
            ModelRuntimeConfig(
                device=DevicePreference.AUTO,
                allow_model_download=False,
                interactive=False,
            )
        )
        return registry, runtime

    def _run_job(self, job_id: str) -> None:
        record = self.require(job_id)
        if record.cancellation.is_set():
            record.update(JobStatus.CANCELLED, "Job cancelled before execution")
            self._store.discard(record.job_id)
            return
        record.update(JobStatus.PROBING, "Starting local analysis")
        try:
            configuration = AnalysisConfig.model_validate(
                {
                    **record.analysis_config.model_dump(mode="python"),
                    "output_directory": record.output_directory,
                    "keep_workspace": False,
                    "bundle_video": False,
                }
            )
            registry, model_runtime = self._runtime_and_registry(configuration)
            pipeline = self.pipeline_factory(
                configuration,
                registry=registry,
                progress=lambda message: self._handle_progress(record, message),
                model_runtime=model_runtime,
                cancellation_callback=record.cancellation.is_set,
            )
            pipeline.run(record.input_path, prompt=record.prompt)
            if record.cancellation.is_set():
                record.update(JobStatus.CANCELLED, "Job cancelled")
            else:
                record.update(JobStatus.COMPLETED, "Analysis completed")
        except AnalysisCancelledError:
            record.update(JobStatus.CANCELLED, "Job cancelled")
        except AnalysisError as exc:
            record.update(
                JobStatus.FAILED,
                "Analysis failed",
                error=self._sanitize_error(record, exc),
            )
        except Exception as exc:
            record.update(
                JobStatus.FAILED,
                "Analysis failed",
                error=f"Internal analysis failure: {type(exc).__name__}",
            )
        finally:
            if record.snapshot().status is JobStatus.CANCELLED:
                self._store.discard(record.job_id)

    @staticmethod
    def _handle_progress(record: JobRecord, message: str) -> None:
        normalized = message.casefold()
        status = next(
            (
                candidate
                for prefix, candidate in _PROGRESS_STATES
                if normalized.startswith(prefix)
            ),
            record.snapshot().status,
        )
        current_detector = None
        progress_percent = None
        if normalized.startswith("running detector:"):
            current_detector = message.partition(":")[2].strip() or None
            status = JobStatus.DETECTING
            progress_percent = 68
        record.update(
            status,
            message,
            progress_percent=progress_percent,
            current_detector=current_detector,
        )

    @staticmethod
    def _sanitize_error(record: JobRecord, exc: Exception) -> str:
        message = sanitize_diagnostic(
            str(exc),
            sensitive_paths=(record.directory, record.input_path),
        )
        message = " ".join(message.split())
        return message or f"Analysis failed: {type(exc).__name__}"

    def cleanup_expired(self, *, now: datetime | None = None) -> tuple[str, ...]:
        """Delete terminal jobs older than the configured TTL."""
        current = now or datetime.now(UTC)
        cutoff = current - timedelta(seconds=self.config.job_ttl_seconds)
        with self._lock:
            expired = tuple(
                job_id
                for job_id, record in self._jobs.items()
                if record.snapshot().status.terminal
                and record.snapshot().updated_at <= cutoff
            )
            records = tuple(self._jobs.pop(job_id) for job_id in expired)
        for record in records:
            self._store.discard(record.job_id)
        with self._lock:
            active_ids = set(self._jobs)
        self._store.cleanup_orphans(cutoff=cutoff, active_job_ids=active_ids)
        return expired

    def start_cleanup(self) -> None:
        """Start one daemon retention loop."""
        with self._lock:
            if self._cleanup_thread is not None:
                return
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="videoscope-job-cleanup",
                daemon=True,
            )
            self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(self.config.cleanup_interval_seconds):
            self.cleanup_expired()

    def shutdown(self, *, wait: bool = True) -> None:
        """Stop cleanup and cooperatively cancel unfinished workers."""
        self._cleanup_stop.set()
        with self._lock:
            records = tuple(self._jobs.values())
            cleanup_thread = self._cleanup_thread
        for record in records:
            if not record.snapshot().status.terminal:
                record.cancellation.set()
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=2.0)
        self._cpu_executor.shutdown(wait=wait, cancel_futures=True)
        self._heavy_executor.shutdown(wait=wait, cancel_futures=True)
