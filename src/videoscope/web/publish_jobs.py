"""Confirmation-gated local jobs around the shared PublishReadyPipeline."""

from __future__ import annotations

import hmac
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeAlias

from videoscope.resolve import (
    PublishCancelledError,
    PublishPlan,
    PublishPreparation,
    PublishProfileId,
    PublishReadyConfig,
    PublishReadyPipeline,
    PublishReadyStatus,
    PublishResult,
    ResolveError,
)
from videoscope.video.errors import sanitize_diagnostic
from videoscope.web.jobs import CpuJobLimiter
from videoscope.web.models import (
    PublishJobEvent,
    PublishJobResponse,
    PublishJobStatus,
    WebServerConfig,
)
from videoscope.web.storage import LocalJobStore

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_STATUS_PROGRESS: dict[PublishJobStatus, int] = {
    PublishJobStatus.QUEUED: 0,
    PublishJobStatus.INSPECTING: 10,
    PublishJobStatus.PLANNING: 40,
    PublishJobStatus.AWAITING_CONFIRMATION: 55,
    PublishJobStatus.PROCESSING: 65,
    PublishJobStatus.VERIFYING: 90,
    PublishJobStatus.COMPLETED: 100,
    PublishJobStatus.NEEDS_REVIEW: 100,
    PublishJobStatus.FAILED: 100,
    PublishJobStatus.CANCELLED: 100,
}
_STATUS_ORDER: dict[PublishJobStatus, int] = {
    status: index
    for index, status in enumerate(
        (
            PublishJobStatus.QUEUED,
            PublishJobStatus.INSPECTING,
            PublishJobStatus.PLANNING,
            PublishJobStatus.AWAITING_CONFIRMATION,
            PublishJobStatus.PROCESSING,
            PublishJobStatus.VERIFYING,
        )
    )
}
_PIPELINE_STATUS: dict[str, PublishJobStatus] = {
    "created": PublishJobStatus.INSPECTING,
    "inspecting": PublishJobStatus.INSPECTING,
    "planning": PublishJobStatus.PLANNING,
    "awaiting_confirmation": PublishJobStatus.AWAITING_CONFIRMATION,
    "processing": PublishJobStatus.PROCESSING,
    "verifying": PublishJobStatus.VERIFYING,
}
_STATUS_MESSAGE: dict[PublishJobStatus, str] = {
    PublishJobStatus.QUEUED: "Publish Ready job queued",
    PublishJobStatus.INSPECTING: "Inspecting the local source",
    PublishJobStatus.PLANNING: "Building the Publish Ready plan",
    PublishJobStatus.AWAITING_CONFIRMATION: "Awaiting exact plan confirmation",
    PublishJobStatus.PROCESSING: "Processing the confirmed local plan",
    PublishJobStatus.VERIFYING: "Verifying the local output",
    PublishJobStatus.COMPLETED: "Publish Ready output completed",
    PublishJobStatus.NEEDS_REVIEW: "Publish output needs human review",
    PublishJobStatus.FAILED: "Publish Ready job failed",
    PublishJobStatus.CANCELLED: "Publish Ready job cancelled",
}


class PublishPipeline(Protocol):
    """The existing core pipeline surface consumed by this Web adapter."""

    def prepare(self, input_path: Path) -> PublishPreparation: ...

    def execute(
        self,
        preparation: PublishPreparation,
        confirmed_plan_digest: str,
    ) -> PublishResult: ...


PublishPipelineFactory: TypeAlias = Callable[..., PublishPipeline]


class PublishJobStateError(RuntimeError):
    """The requested operation is invalid for the current job state."""


class PublishConfirmationMismatchError(PublishJobStateError):
    """The submitted digest is not the exact prepared plan digest."""


class PublishArtifactUnavailableError(PublishJobStateError):
    """Public artifacts are not available in the current lifecycle state."""


_PREVIEW_ARTIFACT_PATH = "preview/publish-preview.mp4"
_PREVIEW_AVAILABLE_STATUSES = {
    PublishJobStatus.AWAITING_CONFIRMATION,
    PublishJobStatus.PROCESSING,
    PublishJobStatus.VERIFYING,
}


@dataclass(slots=True)
class PublishJobRecord:
    """Private mutable state for one local Publish Ready job."""

    job_id: str
    directory: Path
    input_path: Path
    output_directory: Path
    profile_id: PublishProfileId
    warnings: tuple[str, ...]
    status: PublishJobStatus = PublishJobStatus.QUEUED
    message: str = _STATUS_MESSAGE[PublishJobStatus.QUEUED]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    upload_size_bytes: int = 0
    progress_percent: int = 0
    error: str | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None
    pipeline: PublishPipeline | None = None
    preparation: PublishPreparation | None = None
    execution_submitted: bool = False
    events: list[PublishJobEvent] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self._append_event()

    def _append_event(self) -> None:
        now = datetime.now(UTC)
        self.updated_at = now
        self.events.append(
            PublishJobEvent(
                sequence=len(self.events) + 1,
                status=self.status,
                message=self.message,
                progress_percent=self.progress_percent,
                created_at=now,
            )
        )

    def update(
        self,
        status: PublishJobStatus,
        *,
        message: str | None = None,
    ) -> None:
        """Append only monotonic, non-terminal pipeline progress."""
        with self.lock:
            if self.status.terminal or status.terminal:
                return
            current_order = _STATUS_ORDER[self.status]
            candidate_order = _STATUS_ORDER[status]
            if candidate_order < current_order:
                return
            next_message = message or _STATUS_MESSAGE[status]
            if status is self.status and next_message == self.message:
                return
            self.status = status
            self.message = next_message
            self.progress_percent = max(
                self.progress_percent,
                _STATUS_PROGRESS[status],
            )
            self._append_event()

    def finish(
        self,
        status: PublishJobStatus,
        *,
        error: str | None = None,
    ) -> None:
        """Record one terminal result after the core call returns or raises."""
        if not status.terminal:
            raise ValueError("finish requires a terminal Publish job status")
        with self.lock:
            if self.status.terminal:
                if self.status is status and error is not None and self.error is None:
                    self.error = error
                    self.message = _STATUS_MESSAGE[status]
                    self.updated_at = datetime.now(UTC)
                return
            self.status = status
            self.message = _STATUS_MESSAGE[status]
            self.progress_percent = 100
            self.error = error
            self._append_event()

    def update_upload_size(self, size: int) -> None:
        with self.lock:
            self.upload_size_bytes = size
            self.updated_at = datetime.now(UTC)

    def snapshot(self) -> PublishJobResponse:
        with self.lock:
            base = f"/api/publish/jobs/{self.job_id}"
            return PublishJobResponse(
                job_id=self.job_id,
                status=self.status,
                message=self.message,
                created_at=self.created_at,
                updated_at=self.updated_at,
                upload_size_bytes=self.upload_size_bytes,
                progress_percent=self.progress_percent,
                profile_id=self.profile_id,
                warnings=self.warnings,
                error=self.error,
                links={
                    "self": base,
                    "events": f"{base}/events",
                    "plan": f"{base}/plan",
                    "confirm": f"{base}/confirm",
                    "artifacts": f"{base}/artifacts/{{path}}",
                },
            )

    def events_after(self, sequence: int) -> tuple[PublishJobEvent, ...]:
        with self.lock:
            return tuple(event for event in self.events if event.sequence > sequence)


class PublishJobManager:
    """Own confirmation-gated Publish Ready work on the bounded CPU pool."""

    def __init__(
        self,
        config: WebServerConfig | None = None,
        *,
        pipeline_factory: PublishPipelineFactory = PublishReadyPipeline,
        cpu_limiter: CpuJobLimiter | None = None,
    ) -> None:
        self.config = config or WebServerConfig()
        self.job_root = Path(self.config.job_root / "publish").resolve(strict=False)
        self.pipeline_factory = pipeline_factory
        self._cpu_limiter = cpu_limiter or CpuJobLimiter(self.config.cpu_concurrency)
        self._jobs: dict[str, PublishJobRecord] = {}
        self._lock = threading.RLock()
        self._store: LocalJobStore | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.cpu_concurrency,
            thread_name_prefix="videoscope-publish-cpu",
        )
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None

    def use_cpu_limiter(self, limiter: CpuJobLimiter) -> None:
        """Join the app-wide CPU budget before any work is submitted."""
        with self._lock:
            self._cpu_limiter = limiter

    def reserve_job(
        self,
        *,
        original_filename: str,
        profile_id: PublishProfileId,
        warnings: tuple[str, ...] = (),
    ) -> PublishJobRecord:
        paths = self._job_store().reserve(original_filename)
        record = PublishJobRecord(
            job_id=paths.job_id,
            directory=paths.directory,
            input_path=paths.input_path,
            output_directory=paths.output_directory,
            profile_id=profile_id,
            warnings=warnings,
        )
        with self._lock:
            self._jobs[record.job_id] = record
        return record

    def discard_reserved(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.pop(job_id, None)
        if record is not None:
            self._job_store().discard(job_id)

    def submit_prepare(self, job_id: str) -> PublishJobResponse:
        record = self.require(job_id)
        future = self._executor.submit(self._run_prepare_bounded, job_id)
        with record.lock:
            record.future = future
        return record.snapshot()

    def _run_prepare_bounded(self, job_id: str) -> None:
        record = self.require(job_id)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if acquired:
                self._run_prepare(job_id)
            else:
                record.finish(PublishJobStatus.CANCELLED)
                self._job_store().discard(job_id)

    def require(self, job_id: str) -> PublishJobRecord:
        if _JOB_ID.fullmatch(job_id) is None:
            raise KeyError(job_id)
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(job_id) from exc

    def snapshot(self, job_id: str) -> PublishJobResponse:
        return self.require(job_id).snapshot()

    def active_job_count(self) -> int:
        with self._lock:
            records = tuple(self._jobs.values())
        return sum(not record.snapshot().status.terminal for record in records)

    def events_after(
        self,
        job_id: str,
        sequence: int,
    ) -> tuple[PublishJobEvent, ...]:
        return self.require(job_id).events_after(sequence)

    def plan(self, job_id: str) -> PublishPlan:
        record = self.require(job_id)
        with record.lock:
            preparation = record.preparation
            status = record.status
        if preparation is None or status in {
            PublishJobStatus.QUEUED,
            PublishJobStatus.INSPECTING,
            PublishJobStatus.PLANNING,
            PublishJobStatus.FAILED,
            PublishJobStatus.CANCELLED,
        }:
            raise PublishJobStateError("Publish plan is not available")
        return preparation.plan

    def confirm(self, job_id: str, plan_digest: str) -> PublishJobResponse:
        record = self.require(job_id)
        with record.lock:
            preparation = record.preparation
            if (
                record.status is not PublishJobStatus.AWAITING_CONFIRMATION
                or preparation is None
                or record.execution_submitted
            ):
                raise PublishJobStateError("Publish job is not awaiting confirmation")
            if not hmac.compare_digest(
                plan_digest,
                preparation.plan.plan_digest,
            ):
                raise PublishConfirmationMismatchError(
                    "Confirmed digest does not match the prepared plan"
                )
            future = self._executor.submit(self._run_execute_bounded, job_id)
            record.execution_submitted = True
            record.future = future
            record.update(
                PublishJobStatus.PROCESSING,
                message="Confirmation accepted; execution queued",
            )
        return record.snapshot()

    def _run_execute_bounded(self, job_id: str) -> None:
        record = self.require(job_id)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if acquired:
                self._run_execute(job_id)
            else:
                record.finish(PublishJobStatus.CANCELLED)
                self._job_store().discard(job_id)

    def cancel(self, job_id: str) -> PublishJobResponse:
        record = self.require(job_id)
        record.cancellation.set()
        with record.lock:
            future = record.future
            status = record.status
            execution_submitted = record.execution_submitted
        cancelled_before_run = future is not None and future.cancel()
        if cancelled_before_run or (
            status is PublishJobStatus.AWAITING_CONFIRMATION and not execution_submitted
        ):
            record.finish(PublishJobStatus.CANCELLED)
            self._job_store().discard(job_id)
        elif not status.terminal:
            with record.lock:
                record.message = "Cancellation requested"
                record._append_event()
        return record.snapshot()

    def delete_or_cancel(self, job_id: str) -> PublishJobResponse | None:
        record = self.require(job_id)
        if not record.snapshot().status.terminal:
            return self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        self._job_store().discard(job_id)
        return None

    def resolve_artifact(self, job_id: str, requested_path: str) -> Path:
        record = self.require(job_id)
        status = record.snapshot().status
        if (
            requested_path == _PREVIEW_ARTIFACT_PATH
            and status in _PREVIEW_AVAILABLE_STATUSES
        ):
            with record.lock:
                preparation = record.preparation
            if preparation is None:
                raise PublishArtifactUnavailableError(
                    "Publish preview is not available"
                )
            preview_root = Path(preparation.preview_path).parent.parent
            return Path(
                self._job_store().resolve_artifact(
                    job_id,
                    requested_path,
                    artifact_root=preview_root,
                )
            )
        if status not in {
            PublishJobStatus.COMPLETED,
            PublishJobStatus.NEEDS_REVIEW,
        }:
            raise PublishArtifactUnavailableError("Publish artifacts are not available")
        return Path(self._job_store().resolve_artifact(job_id, requested_path))

    def _run_prepare(self, job_id: str) -> None:
        record = self.require(job_id)
        if record.cancellation.is_set():
            record.finish(PublishJobStatus.CANCELLED)
            self._job_store().discard(job_id)
            return
        try:
            pipeline = self.pipeline_factory(
                PublishReadyConfig(
                    profile_id=record.profile_id,
                    output_directory=record.output_directory,
                ),
                progress=lambda value: self._handle_progress(record, value),
                cancellation_callback=record.cancellation.is_set,
            )
            with record.lock:
                record.pipeline = pipeline
            preparation = pipeline.prepare(record.input_path)
            with record.lock:
                cancelled = record.cancellation.is_set()
                if not cancelled:
                    record.preparation = preparation
                    record.update(PublishJobStatus.AWAITING_CONFIRMATION)
            if cancelled:
                record.finish(PublishJobStatus.CANCELLED)
                self._job_store().discard(job_id)
                return
        except PublishCancelledError:
            record.finish(PublishJobStatus.CANCELLED)
            self._job_store().discard(job_id)
        except ResolveError as exc:
            record.finish(
                PublishJobStatus.FAILED,
                error=self._sanitize_error(record, exc),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            record.finish(
                PublishJobStatus.FAILED,
                error=f"Internal Publish Ready failure: {type(exc).__name__}",
            )

    def _run_execute(self, job_id: str) -> None:
        record = self.require(job_id)
        with record.lock:
            pipeline = record.pipeline
            preparation = record.preparation
        if pipeline is None or preparation is None:
            record.finish(
                PublishJobStatus.FAILED,
                error="Publish Ready preparation is unavailable",
            )
            return
        try:
            result = pipeline.execute(
                preparation,
                confirmed_plan_digest=preparation.plan.plan_digest,
            )
            if record.cancellation.is_set():
                record.finish(PublishJobStatus.CANCELLED)
                self._job_store().discard(job_id)
                return
            terminal = {
                PublishReadyStatus.COMPLETED: PublishJobStatus.COMPLETED,
                PublishReadyStatus.NEEDS_REVIEW: PublishJobStatus.NEEDS_REVIEW,
            }.get(result.status)
            if terminal is None:
                raise RuntimeError("core pipeline returned a non-published status")
            record.finish(terminal)
        except PublishCancelledError:
            record.finish(PublishJobStatus.CANCELLED)
            self._job_store().discard(job_id)
        except ResolveError as exc:
            record.finish(
                PublishJobStatus.FAILED,
                error=self._sanitize_error(record, exc),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            record.finish(
                PublishJobStatus.FAILED,
                error=f"Internal Publish Ready failure: {type(exc).__name__}",
            )

    @staticmethod
    def _handle_progress(record: PublishJobRecord, value: str) -> None:
        status = _PIPELINE_STATUS.get(value.casefold())
        if status is not None:
            record.update(status)

    @staticmethod
    def _sanitize_error(record: PublishJobRecord, exc: Exception) -> str:
        message = sanitize_diagnostic(
            str(exc),
            sensitive_paths=(record.directory, record.input_path),
        )
        message = " ".join(message.split())
        return message or f"Publish Ready failed: {type(exc).__name__}"

    def cleanup_expired(self, *, now: datetime | None = None) -> tuple[str, ...]:
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
            self._job_store().discard(record.job_id)
        with self._lock:
            active_ids = set(self._jobs)
            store = self._store
        if store is not None:
            store.cleanup_orphans(cutoff=cutoff, active_job_ids=active_ids)
        return expired

    def _job_store(self) -> LocalJobStore:
        """Create the Publish root only when a Publish request needs storage."""
        with self._lock:
            if self._store is None:
                self._store = LocalJobStore(self.job_root)
            return self._store

    def start_cleanup(self) -> None:
        with self._lock:
            if self._cleanup_thread is not None:
                return
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="videoscope-publish-cleanup",
                daemon=True,
            )
            self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(self.config.cleanup_interval_seconds):
            self.cleanup_expired()

    def shutdown(self, *, wait: bool = True) -> None:
        self._cleanup_stop.set()
        with self._lock:
            records = tuple(self._jobs.values())
            cleanup_thread = self._cleanup_thread
        for record in records:
            if not record.snapshot().status.terminal:
                record.cancellation.set()
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=2.0)
        self._executor.shutdown(wait=wait, cancel_futures=True)


__all__ = [
    "PublishArtifactUnavailableError",
    "PublishConfirmationMismatchError",
    "PublishJobManager",
    "PublishJobRecord",
    "PublishJobStateError",
    "PublishPipelineFactory",
]
