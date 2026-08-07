"""Revision-aware loopback Web jobs for Long Video to Useful Content."""

from __future__ import annotations

import hmac
import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeAlias

from videoscope.content import (
    ContentConfirmation,
    ContentError,
    ContentJoinPreview,
    ContentMap,
    ContentPipelineConfig,
    ContentPlan,
    ContentPreparation,
    ContentResult,
    ContentReview,
    ContentStatus,
    ContentTimeRange,
    ContentUserRange,
    LongVideoContentPipeline,
    make_user_range_id,
)
from videoscope.video.errors import sanitize_diagnostic
from videoscope.video.hashing import compute_file_sha256
from videoscope.web.jobs import CpuJobLimiter
from videoscope.web.models import (
    ContentConfirmationRequest,
    ContentJobEvent,
    ContentJobResponse,
    ContentJobStatus,
    ContentRangeInput,
    ContentStoryboardRevisionRequest,
    WebServerConfig,
)
from videoscope.web.storage import LocalJobStore

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_STATE_NAME = "content-job-state.json"
_STATUS_PROGRESS: dict[ContentJobStatus, int] = {
    ContentJobStatus.QUEUED: 0,
    ContentJobStatus.PROBING: 10,
    ContentJobStatus.MAPPING: 30,
    ContentJobStatus.PLANNING: 45,
    ContentJobStatus.AWAITING_REVIEW: 55,
    ContentJobStatus.PREVIEWING: 65,
    ContentJobStatus.READY_TO_CONFIRM: 72,
    ContentJobStatus.RENDERING: 80,
    ContentJobStatus.VERIFYING: 92,
    ContentJobStatus.COMPLETED: 100,
    ContentJobStatus.PARTIAL: 100,
    ContentJobStatus.NEEDS_REVIEW: 100,
    ContentJobStatus.FAILED: 100,
    ContentJobStatus.CANCELLED: 100,
}
_STATUS_MESSAGE: dict[ContentJobStatus, str] = {
    ContentJobStatus.QUEUED: "Useful-content job queued",
    ContentJobStatus.PROBING: "Inspecting the local source",
    ContentJobStatus.MAPPING: "Building the structural content map",
    ContentJobStatus.PLANNING: "Building a reviewable storyboard",
    ContentJobStatus.AWAITING_REVIEW: "Awaiting storyboard review",
    ContentJobStatus.PREVIEWING: "Creating bounded private join previews",
    ContentJobStatus.READY_TO_CONFIRM: "Awaiting exact plan confirmation",
    ContentJobStatus.RENDERING: "Rendering the confirmed local timeline",
    ContentJobStatus.VERIFYING: "Independently verifying the pending output",
    ContentJobStatus.COMPLETED: "Useful-content output completed",
    ContentJobStatus.PARTIAL: "Useful-content output completed with limitations",
    ContentJobStatus.NEEDS_REVIEW: "Useful-content output needs human review",
    ContentJobStatus.FAILED: "Useful-content job failed",
    ContentJobStatus.CANCELLED: "Useful-content job cancelled",
}
_PIPELINE_STATUS: dict[str, ContentJobStatus] = {
    status.value: ContentJobStatus(status.value)
    for status in ContentStatus
    if status.value in {item.value for item in ContentJobStatus}
}


class ContentPipeline(Protocol):
    def prepare(self, input_path: Path) -> ContentPreparation: ...

    def revise(
        self,
        preparation: ContentPreparation,
        *,
        selected_range_order: tuple[str, ...] = (),
        reorder_acknowledged: bool = False,
        chapter_titles: dict[str, str] | None = None,
    ) -> ContentPreparation: ...

    def preview(self, preparation: ContentPreparation) -> ContentReview: ...

    def confirm(
        self,
        review: ContentReview,
        *,
        accepted_action_ids: tuple[str, ...],
    ) -> ContentConfirmation: ...

    def execute(
        self,
        review: ContentReview,
        confirmation: ContentConfirmation,
    ) -> ContentResult: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


ContentPipelineFactory: TypeAlias = Callable[..., ContentPipeline]


class ContentJobStateError(RuntimeError):
    """The operation is not valid for the current local job state."""


class ContentRevisionConflictError(ContentJobStateError):
    """The submitted revision is stale."""


class ContentConfirmationMismatchError(ContentJobStateError):
    """The submitted confirmation is not bound to the current plan."""


class ContentArtifactUnavailableError(ContentJobStateError):
    """The requested private or public artifact is not available."""


@dataclass(slots=True)
class ContentJobRecord:
    job_id: str
    directory: Path
    input_path: Path
    output_directory: Path
    config: ContentPipelineConfig
    transcript_path: Path | None
    warnings: tuple[str, ...] = ()
    status: ContentJobStatus = ContentJobStatus.QUEUED
    message: str = _STATUS_MESSAGE[ContentJobStatus.QUEUED]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    upload_size_bytes: int = 0
    input_hash: str | None = None
    progress_percent: int = 0
    revision: int = 0
    error: str | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None
    pipeline: ContentPipeline | None = None
    preparation: ContentPreparation | None = None
    review: ContentReview | None = None
    confirmation_submitted: bool = False
    persisted_map: ContentMap | None = None
    persisted_plan: ContentPlan | None = None
    events: list[ContentJobEvent] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if not self.events:
            self._append_event()

    def _append_event(self) -> None:
        now = datetime.now(UTC)
        self.updated_at = now
        self.events.append(
            ContentJobEvent(
                sequence=len(self.events) + 1,
                status=self.status,
                message=self.message,
                progress_percent=self.progress_percent,
                revision=self.revision,
                created_at=now,
            )
        )

    def update(self, status: ContentJobStatus, *, message: str | None = None) -> None:
        with self.lock:
            if self.status.terminal:
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
        status: ContentJobStatus,
        *,
        error: str | None = None,
    ) -> None:
        if not status.terminal:
            raise ValueError("finish requires a terminal content status")
        with self.lock:
            if self.status.terminal:
                return
            self.status = status
            self.message = _STATUS_MESSAGE[status]
            self.progress_percent = 100
            self.error = error
            self._append_event()

    def snapshot(self) -> ContentJobResponse:
        with self.lock:
            base = f"/api/content/jobs/{self.job_id}"
            return ContentJobResponse(
                job_id=self.job_id,
                status=self.status,
                message=self.message,
                created_at=self.created_at,
                updated_at=self.updated_at,
                upload_size_bytes=self.upload_size_bytes,
                progress_percent=self.progress_percent,
                goal=self.config.content.goal,
                revision=self.revision,
                plan_digest=(
                    self.review.plan.plan_digest
                    if self.review
                    else self.persisted_plan.plan_digest
                    if self.persisted_plan
                    else None
                ),
                warnings=self.warnings,
                error=self.error,
                links={
                    "self": base,
                    "events": f"{base}/events",
                    "map": f"{base}/map",
                    "storyboard": f"{base}/storyboard",
                    "previews": f"{base}/previews",
                    "plan": f"{base}/plan",
                    "confirm": f"{base}/confirm",
                    "artifacts": f"{base}/artifacts/{{path}}",
                },
            )

    def events_after(self, sequence: int) -> tuple[ContentJobEvent, ...]:
        with self.lock:
            return tuple(event for event in self.events if event.sequence > sequence)


class ContentJobManager:
    """Own revision-gated useful-content work on the shared CPU budget."""

    def __init__(
        self,
        config: WebServerConfig | None = None,
        *,
        pipeline_factory: ContentPipelineFactory = LongVideoContentPipeline,
        cpu_limiter: CpuJobLimiter | None = None,
    ) -> None:
        self.config = config or WebServerConfig()
        self.job_root = Path(self.config.job_root / "content").resolve(strict=False)
        self.pipeline_factory = pipeline_factory
        self._cpu_limiter = cpu_limiter or CpuJobLimiter(self.config.cpu_concurrency)
        self._jobs: dict[str, ContentJobRecord] = {}
        self._lock = threading.RLock()
        self._store: LocalJobStore | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.cpu_concurrency,
            thread_name_prefix="videoscope-content-cpu",
        )
        self._cleanup_stop = threading.Event()
        self._cleanup_thread: threading.Thread | None = None
        self._restore_records()

    def use_cpu_limiter(self, limiter: CpuJobLimiter) -> None:
        with self._lock:
            self._cpu_limiter = limiter

    def reserve_job(
        self,
        *,
        original_filename: str,
        config: ContentPipelineConfig,
        transcript_filename: str | None,
        warnings: tuple[str, ...] = (),
    ) -> ContentJobRecord:
        paths = self._job_store().reserve(original_filename)
        transcript_suffix = (
            Path(transcript_filename).suffix.casefold()
            if transcript_filename is not None
            else ""
        )
        if transcript_suffix not in {".srt", ".vtt"}:
            transcript_suffix = ".srt"
        transcript_path = (
            paths.directory / f"transcript{transcript_suffix}"
            if transcript_filename is not None
            else None
        )
        effective = config.model_copy(
            update={"output_directory": paths.output_directory}
        )
        record = ContentJobRecord(
            job_id=paths.job_id,
            directory=paths.directory,
            input_path=paths.input_path,
            output_directory=paths.output_directory,
            config=effective.model_copy(update={"transcript_path": transcript_path}),
            transcript_path=transcript_path,
            warnings=warnings,
        )
        with self._lock:
            self._jobs[record.job_id] = record
        self._persist(record)
        return record

    def commit_upload(self, job_id: str) -> None:
        record = self.require(job_id)
        record.upload_size_bytes = record.input_path.stat().st_size
        record.input_hash = compute_file_sha256(record.input_path)
        self._persist(record)

    def discard_reserved(self, job_id: str) -> None:
        with self._lock:
            self._jobs.pop(job_id, None)
        self._job_store().discard(job_id)

    def submit_prepare(self, job_id: str) -> ContentJobResponse:
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
                self._finish(record, ContentJobStatus.CANCELLED)

    def _run_prepare(self, job_id: str) -> None:
        record = self.require(job_id)
        try:
            pipeline = self.pipeline_factory(
                record.config,
                progress=lambda value: self._handle_progress(record, value),
            )
            with record.lock:
                record.pipeline = pipeline
            preparation = pipeline.prepare(record.input_path)
            with record.lock:
                if record.cancellation.is_set():
                    raise ContentJobStateError("content job was cancelled")
                record.preparation = preparation
                record.persisted_map = preparation.content_map
                record.update(ContentJobStatus.AWAITING_REVIEW)
                self._persist(record)
        except ContentError as exc:
            terminal = (
                ContentJobStatus.CANCELLED
                if exc.exit_code == 130
                else ContentJobStatus.FAILED
            )
            self._finish(record, terminal, error=self._sanitize_error(record, exc))
        except ContentJobStateError:
            self._finish(record, ContentJobStatus.CANCELLED)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._finish(
                record,
                ContentJobStatus.FAILED,
                error=f"Internal useful-content failure: {type(exc).__name__}",
            )

    def revise(
        self,
        job_id: str,
        request: ContentStoryboardRevisionRequest,
    ) -> ContentJobResponse:
        record = self.require(job_id)
        with record.lock:
            if request.expected_revision != record.revision:
                raise ContentRevisionConflictError("storyboard revision is stale")
            if record.status is not ContentJobStatus.AWAITING_REVIEW:
                raise ContentJobStateError("content job is not awaiting review")
            input_hash = record.input_hash
            previous = record.pipeline
        if input_hash is None:
            raise ContentJobStateError("content input identity is unavailable")
        ranges = _content_ranges(input_hash, request.ranges)
        new_config = record.config.model_copy(update={"user_ranges": ranges})
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if not acquired:
                self._finish(record, ContentJobStatus.CANCELLED)
                return record.snapshot()
            if previous is not None:
                previous.close()
            pipeline = self.pipeline_factory(new_config, progress=lambda _value: None)
            try:
                preparation = pipeline.prepare(record.input_path)
                preparation = pipeline.revise(
                    preparation,
                    selected_range_order=request.selected_range_order,
                    reorder_acknowledged=request.reorder_acknowledged,
                    chapter_titles=request.chapter_titles,
                )
            except BaseException:
                pipeline.close()
                raise
        with record.lock:
            if record.revision != request.expected_revision:
                pipeline.close()
                raise ContentRevisionConflictError("storyboard revision changed")
            record.pipeline = pipeline
            record.preparation = preparation
            record.persisted_map = preparation.content_map
            record.review = None
            record.persisted_plan = None
            record.config = new_config
            record.revision += 1
            record.message = "Storyboard revision accepted"
            record._append_event()
            self._persist(record)
        return record.snapshot()

    def preview(self, job_id: str) -> ContentJobResponse:
        record = self.require(job_id)
        with record.lock:
            if (
                record.status is not ContentJobStatus.AWAITING_REVIEW
                or record.pipeline is None
                or record.preparation is None
            ):
                raise ContentJobStateError("content job is not ready for previews")
            pipeline = record.pipeline
            preparation = record.preparation
            record.update(ContentJobStatus.PREVIEWING)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if not acquired:
                self._finish(record, ContentJobStatus.CANCELLED)
                return record.snapshot()
            try:
                review = pipeline.preview(preparation)
            except ContentError as exc:
                self._finish(
                    record,
                    ContentJobStatus.FAILED,
                    error=self._sanitize_error(record, exc),
                )
                raise ContentJobStateError("content previews failed") from exc
        with record.lock:
            record.review = review
            record.persisted_plan = review.plan
            record.update(ContentJobStatus.READY_TO_CONFIRM)
            self._persist(record)
        return record.snapshot()

    def confirm(
        self,
        job_id: str,
        request: ContentConfirmationRequest,
    ) -> ContentJobResponse:
        record = self.require(job_id)
        with record.lock:
            review = record.review
            if (
                record.status is not ContentJobStatus.READY_TO_CONFIRM
                or record.pipeline is None
                or review is None
                or record.confirmation_submitted
            ):
                raise ContentJobStateError("content job is not ready to confirm")
            if request.revision != record.revision or not hmac.compare_digest(
                request.plan_digest,
                review.plan.plan_digest,
            ):
                raise ContentConfirmationMismatchError(
                    "confirmation does not match the current plan revision"
                )
            required = tuple(
                item.id
                for item in review.plan.actions
                if item.changes_content and item.requires_confirmation
            )
            if request.accepted_action_ids != required:
                raise ContentConfirmationMismatchError(
                    "confirmation must accept the exact action set"
                )
            confirmation = record.pipeline.confirm(
                review,
                accepted_action_ids=required,
            )
            record.confirmation_submitted = True
            record.update(
                ContentJobStatus.RENDERING,
                message="Confirmation accepted; rendering queued",
            )
            future = self._executor.submit(
                self._run_execute_bounded,
                job_id,
                confirmation,
            )
            record.future = future
            self._persist(record)
        return record.snapshot()

    def _run_execute_bounded(
        self,
        job_id: str,
        confirmation: ContentConfirmation,
    ) -> None:
        record = self.require(job_id)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if not acquired:
                self._finish(record, ContentJobStatus.CANCELLED)
                return
            self._run_execute(record, confirmation)

    def _run_execute(
        self,
        record: ContentJobRecord,
        confirmation: ContentConfirmation,
    ) -> None:
        with record.lock:
            pipeline = record.pipeline
            review = record.review
        if pipeline is None or review is None:
            self._finish(record, ContentJobStatus.FAILED, error="Plan is unavailable")
            return
        try:
            result = pipeline.execute(review, confirmation)
            terminal = ContentJobStatus(result.status.value)
            if not terminal.terminal:
                raise RuntimeError("core returned a non-terminal content result")
            self._finish(record, terminal)
        except ContentError as exc:
            terminal = (
                ContentJobStatus.CANCELLED
                if exc.exit_code == 130
                else ContentJobStatus.FAILED
            )
            self._finish(record, terminal, error=self._sanitize_error(record, exc))
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._finish(
                record,
                ContentJobStatus.FAILED,
                error=f"Internal useful-content failure: {type(exc).__name__}",
            )

    def require(self, job_id: str) -> ContentJobRecord:
        if _JOB_ID.fullmatch(job_id) is None:
            raise KeyError(job_id)
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(job_id) from exc

    def snapshot(self, job_id: str) -> ContentJobResponse:
        return self.require(job_id).snapshot()

    def events_after(self, job_id: str, sequence: int) -> tuple[ContentJobEvent, ...]:
        return self.require(job_id).events_after(sequence)

    def content_map(self, job_id: str) -> ContentMap:
        record = self.require(job_id)
        with record.lock:
            if record.preparation is not None:
                return record.preparation.content_map
            persisted_map = record.persisted_map
            if persisted_map is None:
                raise ContentJobStateError("content map is not available")
            return persisted_map

    def advanced_ai_context(
        self, job_id: str
    ) -> tuple[
        Path,
        Path | None,
        Path,
        str,
        int,
        tuple[ContentUserRange, ...],
    ]:
        """Return private in-process inputs for an explicit local AI extension."""
        record = self.require(job_id)
        with record.lock:
            if record.status is not ContentJobStatus.AWAITING_REVIEW:
                raise ContentJobStateError("content job is not awaiting review")
            if record.input_hash is None or record.preparation is None:
                raise ContentJobStateError("content evidence is not available")
            return (
                record.input_path,
                record.transcript_path,
                record.output_directory,
                record.input_hash,
                record.revision,
                record.config.user_ranges,
            )

    def plan(self, job_id: str) -> ContentPlan:
        record = self.require(job_id)
        with record.lock:
            if record.review is not None:
                return record.review.plan
            persisted_plan = record.persisted_plan
            if persisted_plan is None:
                raise ContentJobStateError("content plan is not available")
            return persisted_plan

    def previews(self, job_id: str) -> tuple[ContentJoinPreview, ...]:
        record = self.require(job_id)
        with record.lock:
            if record.review is None:
                raise ContentJobStateError("content previews are not available")
            return record.review.previews

    def resolve_preview(self, job_id: str, requested_path: str) -> Path:
        record = self.require(job_id)
        with record.lock:
            if record.review is None or record.status in {
                ContentJobStatus.FAILED,
                ContentJobStatus.CANCELLED,
            }:
                raise ContentArtifactUnavailableError("preview is not available")
            allowed = {
                path
                for preview in record.review.previews
                for path in preview.relative_paths
            }
        if requested_path not in allowed:
            raise FileNotFoundError("content preview not found")
        return Path(
            self._job_store().resolve_artifact(
                job_id,
                requested_path,
                artifact_root=record.output_directory / "content-review-private",
            )
        )

    def resolve_public_artifact(self, job_id: str, requested_path: str) -> Path:
        record = self.require(job_id)
        with record.lock:
            plan = record.review.plan if record.review else record.persisted_plan
            if plan is None or record.status not in {
                ContentJobStatus.COMPLETED,
                ContentJobStatus.PARTIAL,
            }:
                raise ContentArtifactUnavailableError(
                    "useful-content artifacts are not available"
                )
            allowed = {
                path.removeprefix("content-output/") for path in plan.public_artifacts
            }
        if requested_path not in allowed:
            raise FileNotFoundError("content artifact not found")
        return Path(
            self._job_store().resolve_artifact(
                job_id,
                requested_path,
                artifact_root=record.output_directory / "content-output",
            )
        )

    def cancel(self, job_id: str) -> ContentJobResponse:
        record = self.require(job_id)
        with record.lock:
            if record.status.terminal:
                return record.snapshot()
            record.cancellation.set()
            if record.pipeline is not None:
                record.pipeline.cancel()
            future = record.future
            cancelled_before_run = future is not None and future.cancel()
            if cancelled_before_run or record.status in {
                ContentJobStatus.AWAITING_REVIEW,
                ContentJobStatus.READY_TO_CONFIRM,
            }:
                self._finish(record, ContentJobStatus.CANCELLED)
            elif record.message != "Cancellation requested":
                record.message = "Cancellation requested"
                record._append_event()
                self._persist(record)
            return record.snapshot()

    def delete_or_cancel(self, job_id: str) -> ContentJobResponse | None:
        record = self.require(job_id)
        if not record.snapshot().status.terminal:
            return self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        if record.pipeline is not None:
            record.pipeline.close()
        self._job_store().discard(job_id)
        return None

    def active_job_count(self) -> int:
        with self._lock:
            return sum(
                not item.snapshot().status.terminal for item in self._jobs.values()
            )

    def cleanup_expired(self, *, now: datetime | None = None) -> tuple[str, ...]:
        cutoff = (now or datetime.now(UTC)) - timedelta(
            seconds=self.config.job_ttl_seconds
        )
        with self._lock:
            expired = tuple(
                key
                for key, record in self._jobs.items()
                if record.snapshot().status.terminal
                and record.snapshot().updated_at <= cutoff
            )
            records = tuple(self._jobs.pop(key) for key in expired)
        for record in records:
            if record.pipeline is not None:
                record.pipeline.close()
            self._job_store().discard(record.job_id)
        return expired

    def start_cleanup(self) -> None:
        with self._lock:
            if self._cleanup_thread is not None:
                return
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop,
                name="videoscope-content-cleanup",
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
                if record.pipeline is not None:
                    record.pipeline.cancel()
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=2)
        self._executor.shutdown(wait=wait, cancel_futures=True)
        for record in records:
            if record.pipeline is not None:
                record.pipeline.close()

    def _finish(
        self,
        record: ContentJobRecord,
        status: ContentJobStatus,
        *,
        error: str | None = None,
    ) -> None:
        record.finish(status, error=error)
        self._persist(record)

    def _handle_progress(self, record: ContentJobRecord, value: object) -> None:
        text = getattr(value, "value", value)
        status = _PIPELINE_STATUS.get(str(text).casefold())
        if status is not None and not status.terminal:
            record.update(status)
            self._persist(record)

    @staticmethod
    def _sanitize_error(record: ContentJobRecord, exc: Exception) -> str:
        message = sanitize_diagnostic(
            getattr(exc, "public_message", str(exc)),
            sensitive_paths=(record.directory, record.input_path),
        )
        return " ".join(message.split()) or f"Content failed: {type(exc).__name__}"

    def _persist(self, record: ContentJobRecord) -> None:
        with record.lock:
            payload = {
                "schema_version": "0.1",
                "job_id": record.job_id,
                "status": record.status.value,
                "message": record.message,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
                "upload_size_bytes": record.upload_size_bytes,
                "input_hash": record.input_hash,
                "progress_percent": record.progress_percent,
                "revision": record.revision,
                "error": record.error,
                "warnings": list(record.warnings),
                "config": {
                    "content": record.config.content.model_dump(mode="json"),
                    "features": record.config.features.model_dump(mode="json"),
                    "user_ranges": [
                        item.model_dump(mode="json")
                        for item in record.config.user_ranges
                    ],
                    "keep_workspace": record.config.keep_workspace,
                },
                "content_map": (
                    record.persisted_map.model_dump(mode="json")
                    if record.persisted_map is not None
                    else None
                ),
                "plan": (
                    record.persisted_plan.model_dump(mode="json")
                    if record.persisted_plan is not None
                    else None
                ),
                "events": [item.model_dump(mode="json") for item in record.events],
            }
        temporary = record.directory / f".{_STATE_NAME}.tmp"
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(record.directory / _STATE_NAME)

    def _restore_records(self) -> None:
        if not self.job_root.is_dir():
            return
        for state_path in self.job_root.glob(f"*/{_STATE_NAME}"):
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                job_id = str(payload["job_id"])
                directory = self._job_store().require_directory(job_id)
                input_path = next(
                    (
                        item
                        for item in sorted(directory.glob("input.*"))
                        if item.is_file() and item.suffix != ".upload"
                    ),
                    None,
                )
                expected_hash = payload.get("input_hash")
                if (
                    input_path is None
                    or not isinstance(expected_hash, str)
                    or not hmac.compare_digest(
                        compute_file_sha256(input_path), expected_hash
                    )
                ):
                    continue
                config_payload = payload["config"]
                config = ContentPipelineConfig.model_validate(
                    {
                        **config_payload,
                        "output_directory": directory / "artifacts",
                        "transcript_path": next(
                            (
                                item
                                for item in directory.glob("transcript.*")
                                if item.is_file()
                            ),
                            None,
                        ),
                    }
                )
                status = ContentJobStatus(payload["status"])
                interrupted = not status.terminal
                if not status.terminal:
                    status = ContentJobStatus.FAILED
                    payload["message"] = "Interrupted local content job; start again"
                    payload["error"] = (
                        "The previous local process ended before completion."
                    )
                record = ContentJobRecord(
                    job_id=job_id,
                    directory=directory,
                    input_path=input_path,
                    output_directory=directory / "artifacts",
                    config=config,
                    transcript_path=config.transcript_path,
                    warnings=tuple(payload.get("warnings", ())),
                    status=status,
                    message=str(payload["message"]),
                    created_at=datetime.fromisoformat(payload["created_at"]),
                    updated_at=datetime.fromisoformat(payload["updated_at"]),
                    upload_size_bytes=int(payload.get("upload_size_bytes", 0)),
                    input_hash=payload.get("input_hash"),
                    progress_percent=(
                        100 if status.terminal else int(payload["progress_percent"])
                    ),
                    revision=int(payload.get("revision", 0)),
                    error=payload.get("error"),
                    persisted_map=(
                        ContentMap.model_validate(payload["content_map"])
                        if payload.get("content_map") is not None
                        else None
                    ),
                    persisted_plan=(
                        ContentPlan.model_validate(payload["plan"])
                        if payload.get("plan") is not None
                        else None
                    ),
                    events=[
                        ContentJobEvent.model_validate(item)
                        for item in payload.get("events", ())
                    ],
                )
                if interrupted:
                    record._append_event()
                with self._lock:
                    self._jobs[job_id] = record
                self._persist(record)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue

    def _job_store(self) -> LocalJobStore:
        with self._lock:
            if self._store is None:
                self._store = LocalJobStore(self.job_root)
            return self._store


def _content_ranges(
    input_hash: str,
    values: tuple[ContentRangeInput, ...],
) -> tuple[ContentUserRange, ...]:
    return tuple(
        ContentUserRange(
            id=make_user_range_id(
                input_hash,
                item.kind,
                ContentTimeRange(
                    start_seconds=item.start_seconds,
                    end_seconds=item.end_seconds,
                ),
            ),
            kind=item.kind,
            source_range=ContentTimeRange(
                start_seconds=item.start_seconds,
                end_seconds=item.end_seconds,
            ),
            label=item.label,
        )
        for item in values
    )


__all__ = [
    "ContentArtifactUnavailableError",
    "ContentConfirmationMismatchError",
    "ContentJobManager",
    "ContentJobRecord",
    "ContentJobStateError",
    "ContentPipelineFactory",
    "ContentRevisionConflictError",
]
