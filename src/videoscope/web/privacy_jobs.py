"""Persisted local Web jobs around the shared Safe Sharing pipeline."""

from __future__ import annotations

import hmac
import json
import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, TypeAlias

from videoscope.privacy.errors import PrivacyCancelledError, PrivacyError
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
)
from videoscope.privacy.models import (
    PrivacyJobOutcome,
    PrivacyPlan,
    PrivacyReviewDecision,
    PrivacyRiskMap,
)
from videoscope.privacy.pipeline import (
    PrivacyPreparation,
    PrivacyReviewedResult,
    PrivacyScanResult,
    SafeSharingConfig,
    SafeSharingPipeline,
)
from videoscope.video.errors import sanitize_diagnostic
from videoscope.web.jobs import CpuJobLimiter
from videoscope.web.models import (
    PrivacyJobEvent,
    PrivacyJobResponse,
    PrivacyJobStatus,
    WebServerConfig,
)
from videoscope.web.storage import LocalJobStore

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_STATE_NAME = "privacy-web-job.json"
_PRIVATE_ROOT = "privacy-review-private"
_PUBLIC_ROOT = "share-package"
_PUBLIC_ARTIFACTS = frozenset(
    {
        "share-safe.mp4",
        "privacy-summary.json",
        "changes.json",
        "verification.json",
        "technical-report.json",
        "manifest.json",
    }
)
_PRIVATE_PREVIEW = "preview/privacy-preview.mp4"

_STATUS_PROGRESS: dict[PrivacyJobStatus, int] = {
    PrivacyJobStatus.QUEUED: 0,
    PrivacyJobStatus.INSPECTING: 5,
    PrivacyJobStatus.SCANNING: 25,
    PrivacyJobStatus.AWAITING_REVIEW: 45,
    PrivacyJobStatus.PLANNING: 55,
    PrivacyJobStatus.PREVIEWING: 65,
    PrivacyJobStatus.AWAITING_CONFIRMATION: 70,
    PrivacyJobStatus.PROCESSING: 75,
    PrivacyJobStatus.VERIFYING: 90,
    PrivacyJobStatus.COMPLETED: 100,
    PrivacyJobStatus.NEEDS_REVIEW: 100,
    PrivacyJobStatus.PARTIAL: 100,
    PrivacyJobStatus.FAILED: 100,
    PrivacyJobStatus.CANCELLED: 100,
}
_STATUS_ORDER = {
    status: index
    for index, status in enumerate(
        (
            PrivacyJobStatus.QUEUED,
            PrivacyJobStatus.INSPECTING,
            PrivacyJobStatus.SCANNING,
            PrivacyJobStatus.AWAITING_REVIEW,
            PrivacyJobStatus.PLANNING,
            PrivacyJobStatus.PREVIEWING,
            PrivacyJobStatus.AWAITING_CONFIRMATION,
            PrivacyJobStatus.PROCESSING,
            PrivacyJobStatus.VERIFYING,
        )
    )
}
_STATUS_MESSAGE: dict[PrivacyJobStatus, str] = {
    PrivacyJobStatus.QUEUED: "Safe Sharing job queued",
    PrivacyJobStatus.INSPECTING: "Inspecting the local source",
    PrivacyJobStatus.SCANNING: "Scanning for reviewable privacy risks",
    PrivacyJobStatus.AWAITING_REVIEW: "Awaiting human privacy review",
    PrivacyJobStatus.PLANNING: "Building the reviewed privacy plan",
    PrivacyJobStatus.PREVIEWING: "Rendering the private review preview",
    PrivacyJobStatus.AWAITING_CONFIRMATION: "Awaiting exact plan confirmation",
    PrivacyJobStatus.PROCESSING: "Rendering the confirmed share copy",
    PrivacyJobStatus.VERIFYING: "Verifying the isolated share package",
    PrivacyJobStatus.COMPLETED: "Safe Sharing package completed",
    PrivacyJobStatus.NEEDS_REVIEW: "Safe Sharing package needs human review",
    PrivacyJobStatus.PARTIAL: "Safe Sharing package has scanner uncertainty",
    PrivacyJobStatus.FAILED: "Safe Sharing job failed",
    PrivacyJobStatus.CANCELLED: "Safe Sharing job cancelled",
}


class PrivacyPipeline(Protocol):
    def scan(self, *, source: Path, config: SafeSharingConfig) -> PrivacyScanResult: ...

    def resume(
        self, *, source: Path, config: SafeSharingConfig
    ) -> PrivacyScanResult: ...

    def review(
        self,
        scan_id: str,
        reviews: Sequence[PrivacyReviewDecision],
        *,
        manual_visual_regions: Sequence[ManualVisualRegionInput] = (),
        manual_audio_intervals: Sequence[ManualAudioIntervalInput] = (),
    ) -> PrivacyReviewedResult: ...

    def prepare(self, review_id: str) -> PrivacyPreparation: ...

    def preview(self, preparation_id: str) -> Path: ...

    def confirm(self, preparation_id: str, plan_digest: str) -> object: ...

    def current_review(self, scan_id: str) -> PrivacyReviewedResult | None: ...

    def current_preparation(self, scan_id: str) -> PrivacyPreparation | None: ...


PrivacyPipelineFactory: TypeAlias = Callable[..., PrivacyPipeline]


class PrivacyJobStateError(RuntimeError):
    """The requested operation is invalid for the current privacy state."""


class PrivacyConfirmationMismatchError(PrivacyJobStateError):
    """The submitted digest differs from the exact prepared plan."""


class PrivacyArtifactUnavailableError(PrivacyJobStateError):
    """The requested scope is not available in the current lifecycle state."""


@dataclass(slots=True)
class PrivacyJobRecord:
    """Private mutable state for one local Safe Sharing job."""

    job_id: str
    directory: Path
    input_path: Path
    output_directory: Path
    profile_id: str
    config: SafeSharingConfig
    warnings: tuple[str, ...] = ()
    status: PrivacyJobStatus = PrivacyJobStatus.QUEUED
    message: str = _STATUS_MESSAGE[PrivacyJobStatus.QUEUED]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    upload_size_bytes: int = 0
    progress_percent: int = 0
    error: str | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None
    pipeline: PrivacyPipeline | None = None
    scan: PrivacyScanResult | None = None
    reviewed: PrivacyReviewedResult | None = None
    preparation: PrivacyPreparation | None = None
    execution_submitted: bool = False
    events: list[PrivacyJobEvent] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if not self.events:
            self._append_event()

    def _append_event(self) -> None:
        now = datetime.now(UTC)
        self.updated_at = now
        self.events.append(
            PrivacyJobEvent(
                sequence=len(self.events) + 1,
                status=self.status,
                message=self.message,
                progress_percent=self.progress_percent,
                created_at=now,
            )
        )

    def snapshot(self) -> PrivacyJobResponse:
        with self.lock:
            base = f"/api/privacy/jobs/{self.job_id}"
            return PrivacyJobResponse(
                job_id=self.job_id,
                status=self.status,
                message=self.message,
                created_at=self.created_at,
                updated_at=self.updated_at,
                upload_size_bytes=self.upload_size_bytes,
                progress_percent=self.progress_percent,
                profile_id=self.profile_id,
                plan_digest=(
                    self.preparation.plan.digest
                    if self.preparation is not None
                    else None
                ),
                warnings=self.warnings,
                error=self.error,
                links={
                    "self": base,
                    "events": f"{base}/events",
                    "risk_map": f"{base}/risk-map",
                    "plan": f"{base}/plan",
                    "artifacts": f"{base}/artifacts/{{path}}",
                    "private_artifacts": f"{base}/private-artifacts/{{path}}",
                },
            )

    def events_after(self, sequence: int) -> tuple[PrivacyJobEvent, ...]:
        with self.lock:
            return tuple(event for event in self.events if event.sequence > sequence)


class PrivacyJobManager:
    """Own review- and confirmation-gated Safe Sharing work on the CPU pool."""

    def __init__(
        self,
        config: WebServerConfig | None = None,
        *,
        pipeline_factory: PrivacyPipelineFactory = SafeSharingPipeline,
        cpu_limiter: CpuJobLimiter | None = None,
    ) -> None:
        self.config = config or WebServerConfig()
        self.job_root = Path(self.config.job_root / "privacy").resolve(strict=False)
        self.pipeline_factory = pipeline_factory
        self._cpu_limiter = cpu_limiter or CpuJobLimiter(self.config.cpu_concurrency)
        self._jobs: dict[str, PrivacyJobRecord] = {}
        self._lock = threading.RLock()
        self._store: LocalJobStore | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.cpu_concurrency,
            thread_name_prefix="videoscope-privacy-cpu",
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
        profile_id: str,
        warnings: tuple[str, ...] = (),
        enable_ocr: bool = False,
    ) -> PrivacyJobRecord:
        paths = self._job_store().reserve(original_filename)
        config = SafeSharingConfig(audience=profile_id, enable_ocr=enable_ocr)
        record = PrivacyJobRecord(
            job_id=paths.job_id,
            directory=paths.directory,
            input_path=paths.input_path,
            output_directory=paths.output_directory,
            profile_id=profile_id,
            config=config,
            warnings=warnings,
        )
        with self._lock:
            self._jobs[record.job_id] = record
        self._persist(record)
        return record

    def discard_reserved(self, job_id: str) -> None:
        with self._lock:
            record = self._jobs.pop(job_id, None)
        if record is not None:
            self._job_store().discard(job_id)

    def submit_scan(self, job_id: str) -> PrivacyJobResponse:
        record = self.require(job_id)
        future = self._executor.submit(self._run_scan_bounded, job_id)
        with record.lock:
            record.future = future
        return record.snapshot()

    def _run_scan_bounded(self, job_id: str) -> None:
        record = self.require(job_id)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if acquired:
                self._run_scan(job_id)
            else:
                self._finish(record, PrivacyJobStatus.CANCELLED)

    def require(self, job_id: str) -> PrivacyJobRecord:
        if _JOB_ID.fullmatch(job_id) is None:
            raise KeyError(job_id)
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(job_id) from exc

    def snapshot(self, job_id: str) -> PrivacyJobResponse:
        return self.require(job_id).snapshot()

    def persist(self, job_id: str) -> None:
        """Persist path-free job metadata after an atomic upload completes."""
        self._persist(self.require(job_id))

    def active_job_count(self) -> int:
        with self._lock:
            records = tuple(self._jobs.values())
        return sum(not record.snapshot().status.terminal for record in records)

    def events_after(self, job_id: str, sequence: int) -> tuple[PrivacyJobEvent, ...]:
        return self.require(job_id).events_after(sequence)

    def risk_map(self, job_id: str) -> PrivacyRiskMap:
        record = self.require(job_id)
        with record.lock:
            if record.scan is None or record.status in {
                PrivacyJobStatus.QUEUED,
                PrivacyJobStatus.INSPECTING,
                PrivacyJobStatus.SCANNING,
                PrivacyJobStatus.FAILED,
                PrivacyJobStatus.CANCELLED,
            }:
                raise PrivacyJobStateError("Privacy risk map is not available")
            return record.scan.risk_map

    def review(
        self,
        job_id: str,
        reviews: Sequence[PrivacyReviewDecision],
        *,
        manual_visual_regions: Sequence[ManualVisualRegionInput] = (),
        manual_audio_intervals: Sequence[ManualAudioIntervalInput] = (),
    ) -> PrivacyJobResponse:
        record = self.require(job_id)
        with record.lock:
            if (
                record.status is not PrivacyJobStatus.AWAITING_REVIEW
                or record.pipeline is None
                or record.scan is None
            ):
                raise PrivacyJobStateError("Privacy job is not awaiting review")
            try:
                record.reviewed = record.pipeline.review(
                    record.scan.scan_id,
                    reviews,
                    manual_visual_regions=manual_visual_regions,
                    manual_audio_intervals=manual_audio_intervals,
                )
            except PrivacyError as exc:
                raise PrivacyJobStateError("Privacy review was rejected") from exc
            record.preparation = None
            self._persist(record)
        return record.snapshot()

    def prepare(self, job_id: str) -> PrivacyJobResponse:
        record = self.require(job_id)
        with record.lock:
            if (
                record.status is not PrivacyJobStatus.AWAITING_REVIEW
                or record.pipeline is None
                or record.reviewed is None
            ):
                raise PrivacyJobStateError("Privacy job has no completed review")
            self._update(record, PrivacyJobStatus.PLANNING)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if not acquired:
                self._finish(record, PrivacyJobStatus.CANCELLED)
                return record.snapshot()
            try:
                preparation = record.pipeline.prepare(record.reviewed.review_id)
                with record.lock:
                    record.preparation = preparation
                    self._update(record, PrivacyJobStatus.PREVIEWING)
                    should_preview = not record.status.terminal
                if should_preview:
                    record.pipeline.preview(preparation.preparation_id)
                    self._update(record, PrivacyJobStatus.AWAITING_CONFIRMATION)
            except PrivacyCancelledError:
                self._finish(record, PrivacyJobStatus.CANCELLED)
            except PrivacyError as exc:
                self._finish(
                    record,
                    PrivacyJobStatus.FAILED,
                    error=self._sanitize_error(record, exc),
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                self._finish(
                    record,
                    PrivacyJobStatus.FAILED,
                    error=f"Internal Safe Sharing failure: {type(exc).__name__}",
                )
        if record.snapshot().status is not PrivacyJobStatus.AWAITING_CONFIRMATION:
            raise PrivacyJobStateError("Privacy preparation failed")
        return record.snapshot()

    def plan(self, job_id: str) -> PrivacyPlan:
        record = self.require(job_id)
        with record.lock:
            if record.preparation is None or record.status in {
                PrivacyJobStatus.QUEUED,
                PrivacyJobStatus.INSPECTING,
                PrivacyJobStatus.SCANNING,
                PrivacyJobStatus.AWAITING_REVIEW,
                PrivacyJobStatus.PLANNING,
                PrivacyJobStatus.FAILED,
                PrivacyJobStatus.CANCELLED,
            }:
                raise PrivacyJobStateError("Privacy plan is not available")
            return record.preparation.plan

    def confirm(self, job_id: str, plan_digest: str) -> PrivacyJobResponse:
        record = self.require(job_id)
        with record.lock:
            preparation = record.preparation
            if (
                record.status is not PrivacyJobStatus.AWAITING_CONFIRMATION
                or preparation is None
                or record.execution_submitted
            ):
                raise PrivacyJobStateError("Privacy job is not awaiting confirmation")
            if not hmac.compare_digest(plan_digest, preparation.plan.digest):
                raise PrivacyConfirmationMismatchError(
                    "Confirmed digest does not match the privacy plan"
                )
            record.execution_submitted = True
            self._update(
                record,
                PrivacyJobStatus.PROCESSING,
                message="Confirmation accepted; execution queued",
            )
            future = self._executor.submit(self._run_confirm_bounded, job_id)
            record.future = future
            self._persist(record)
        return record.snapshot()

    def _run_confirm_bounded(self, job_id: str) -> None:
        record = self.require(job_id)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if acquired:
                self._run_confirm(job_id)
            else:
                self._finish(record, PrivacyJobStatus.CANCELLED)

    def cancel(self, job_id: str) -> PrivacyJobResponse:
        record = self.require(job_id)
        with record.lock:
            if record.status.terminal:
                return record.snapshot()
            record.cancellation.set()
            future = record.future
            cancelled_before_run = future is not None and future.cancel()
            if record.status.terminal:
                return record.snapshot()
            if cancelled_before_run or record.status in {
                PrivacyJobStatus.AWAITING_REVIEW,
                PrivacyJobStatus.AWAITING_CONFIRMATION,
            }:
                self._finish(record, PrivacyJobStatus.CANCELLED)
            elif record.message != "Cancellation requested":
                record.message = "Cancellation requested"
                record._append_event()
                self._persist(record)
            return record.snapshot()

    def delete_or_cancel(self, job_id: str) -> PrivacyJobResponse | None:
        record = self.require(job_id)
        if not record.snapshot().status.terminal:
            return self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        self._job_store().discard(job_id)
        return None

    def resolve_public_artifact(self, job_id: str, requested_path: str) -> Path:
        record = self.require(job_id)
        if requested_path not in _PUBLIC_ARTIFACTS:
            raise FileNotFoundError("Safe Sharing artifact not found")
        if record.snapshot().status is not PrivacyJobStatus.COMPLETED:
            raise PrivacyArtifactUnavailableError(
                "Safe Sharing artifacts are not available"
            )
        return Path(
            self._job_store().resolve_artifact(
                job_id,
                requested_path,
                artifact_root=record.output_directory / _PUBLIC_ROOT,
            )
        )

    def resolve_private_artifact(self, job_id: str, requested_path: str) -> Path:
        record = self.require(job_id)
        status = record.snapshot().status
        if status in {PrivacyJobStatus.FAILED, PrivacyJobStatus.CANCELLED}:
            raise PrivacyArtifactUnavailableError(
                "Private review artifacts are not available"
            )
        if not (
            requested_path.startswith("evidence/") or requested_path == _PRIVATE_PREVIEW
        ):
            raise FileNotFoundError("Private review artifact not found")
        return Path(
            self._job_store().resolve_artifact(
                job_id,
                requested_path,
                artifact_root=record.output_directory / _PRIVATE_ROOT,
            )
        )

    def _run_scan(self, job_id: str) -> None:
        record = self.require(job_id)
        try:
            self._update(record, PrivacyJobStatus.INSPECTING)
            if record.snapshot().status.terminal:
                return
            pipeline = self.pipeline_factory(
                record.output_directory,
                cancellation=record.cancellation.is_set,
            )
            with record.lock:
                record.pipeline = pipeline
                self._update(record, PrivacyJobStatus.SCANNING)
                if record.status.terminal:
                    return
            scan = pipeline.scan(source=record.input_path, config=record.config)
            with record.lock:
                record.scan = scan
                record.warnings = tuple((*record.warnings, *scan.warnings))
                self._update(record, PrivacyJobStatus.AWAITING_REVIEW)
        except PrivacyCancelledError:
            self._finish(record, PrivacyJobStatus.CANCELLED)
        except PrivacyError as exc:
            self._finish(
                record,
                PrivacyJobStatus.FAILED,
                error=self._sanitize_error(record, exc),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._finish(
                record,
                PrivacyJobStatus.FAILED,
                error=f"Internal Safe Sharing failure: {type(exc).__name__}",
            )

    def _run_confirm(self, job_id: str) -> None:
        record = self.require(job_id)
        with record.lock:
            if record.cancellation.is_set():
                self._finish(record, PrivacyJobStatus.CANCELLED)
                return
            pipeline = record.pipeline
            preparation = record.preparation
        if pipeline is None or preparation is None:
            self._finish(
                record,
                PrivacyJobStatus.FAILED,
                error="Safe Sharing preparation is unavailable",
            )
            return
        try:
            result = pipeline.confirm(
                preparation.preparation_id,
                preparation.plan.digest,
            )
            self._update(record, PrivacyJobStatus.VERIFYING)
            if record.snapshot().status.terminal:
                return
            outcome = getattr(result, "status", None)
            if not isinstance(outcome, PrivacyJobOutcome):
                raise RuntimeError("core pipeline returned an invalid privacy status")
            terminal = {
                PrivacyJobOutcome.COMPLETED: PrivacyJobStatus.COMPLETED,
                PrivacyJobOutcome.NEEDS_REVIEW: PrivacyJobStatus.NEEDS_REVIEW,
                PrivacyJobOutcome.PARTIAL: PrivacyJobStatus.PARTIAL,
                PrivacyJobOutcome.FAILED: PrivacyJobStatus.FAILED,
            }.get(outcome)
            if terminal is None:
                raise RuntimeError("core pipeline returned an invalid privacy status")
            self._finish(record, terminal)
        except PrivacyCancelledError:
            self._finish(record, PrivacyJobStatus.CANCELLED)
        except PrivacyError as exc:
            self._finish(
                record,
                PrivacyJobStatus.FAILED,
                error=self._sanitize_error(record, exc),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if record.cancellation.is_set():
                self._finish(record, PrivacyJobStatus.CANCELLED)
            else:
                self._finish(
                    record,
                    PrivacyJobStatus.FAILED,
                    error=f"Internal Safe Sharing failure: {type(exc).__name__}",
                )

    def _update(
        self,
        record: PrivacyJobRecord,
        status: PrivacyJobStatus,
        *,
        message: str | None = None,
    ) -> None:
        with record.lock:
            if record.status.terminal:
                return
            if record.cancellation.is_set():
                self._finish(record, PrivacyJobStatus.CANCELLED)
                return
            if status.terminal:
                return
            if _STATUS_ORDER[status] < _STATUS_ORDER[record.status]:
                return
            next_message = message or _STATUS_MESSAGE[status]
            if status is record.status and next_message == record.message:
                return
            record.status = status
            record.message = next_message
            record.progress_percent = max(
                record.progress_percent,
                _STATUS_PROGRESS[status],
            )
            record._append_event()
            self._persist(record)

    def _finish(
        self,
        record: PrivacyJobRecord,
        status: PrivacyJobStatus,
        *,
        error: str | None = None,
    ) -> None:
        if not status.terminal:
            raise ValueError("finish requires a terminal privacy status")
        with record.lock:
            if record.status.terminal:
                return
            if record.cancellation.is_set():
                status = PrivacyJobStatus.CANCELLED
                error = None
            record.status = status
            record.message = _STATUS_MESSAGE[status]
            record.progress_percent = 100
            record.error = error
            record._append_event()
            self._persist(record)

    @staticmethod
    def _sanitize_error(record: PrivacyJobRecord, exc: Exception) -> str:
        message = sanitize_diagnostic(
            str(exc),
            sensitive_paths=(record.directory, record.input_path),
        )
        # Core errors are intentionally public-safe; unexpected OCR/text details
        # are never forwarded by the generic exception paths above.
        message = " ".join(message.split())
        return message or f"Safe Sharing failed: {type(exc).__name__}"

    def _persist(self, record: PrivacyJobRecord) -> None:
        with record.lock:
            payload = {
                "schema_version": "0.1",
                "job_id": record.job_id,
                "profile_id": record.profile_id,
                "config": record.config.model_dump(mode="json"),
                "status": record.status.value,
                "message": record.message,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
                "upload_size_bytes": record.upload_size_bytes,
                "progress_percent": record.progress_percent,
                "warnings": list(record.warnings),
                "error": record.error,
                "execution_submitted": record.execution_submitted,
                "events": [event.model_dump(mode="json") for event in record.events],
            }
        target = record.directory / _STATE_NAME
        temporary = target.with_suffix(".tmp")
        content = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        temporary.write_text(content + "\n", encoding="utf-8", newline="\n")
        temporary.replace(target)

    def _restore_records(self) -> None:
        root = self.job_root
        if not root.is_dir():
            return
        for child in sorted(root.iterdir(), key=lambda path: path.name):
            if _JOB_ID.fullmatch(child.name) is None:
                continue
            try:
                directory = self._job_store().require_directory(child.name)
                payload = json.loads((directory / _STATE_NAME).read_text("utf-8"))
                status = PrivacyJobStatus(payload["status"])
                input_candidates = tuple(directory.glob("input.*"))
                if len(input_candidates) != 1:
                    raise ValueError("privacy source identity is ambiguous")
                input_path = self._job_store().resolve_artifact(
                    child.name,
                    input_candidates[0].name,
                    artifact_root=directory,
                )
                config = SafeSharingConfig.model_validate(payload["config"])
                record = PrivacyJobRecord(
                    job_id=child.name,
                    directory=directory,
                    input_path=input_path,
                    output_directory=directory / "artifacts",
                    profile_id=payload["profile_id"],
                    config=config,
                    warnings=tuple(payload.get("warnings", ())),
                    status=status,
                    message=payload["message"],
                    created_at=datetime.fromisoformat(payload["created_at"]),
                    updated_at=datetime.fromisoformat(payload["updated_at"]),
                    upload_size_bytes=int(payload.get("upload_size_bytes", 0)),
                    progress_percent=int(payload.get("progress_percent", 0)),
                    error=payload.get("error"),
                    execution_submitted=bool(payload.get("execution_submitted", False)),
                    events=[
                        PrivacyJobEvent.model_validate(event)
                        for event in payload.get("events", ())
                    ],
                )
                if status in {
                    PrivacyJobStatus.AWAITING_REVIEW,
                    PrivacyJobStatus.AWAITING_CONFIRMATION,
                }:
                    pipeline = self.pipeline_factory(
                        record.output_directory,
                        cancellation=record.cancellation.is_set,
                    )
                    scan = pipeline.resume(source=record.input_path, config=config)
                    record.pipeline = pipeline
                    record.scan = scan
                    current_review = getattr(pipeline, "current_review", None)
                    if callable(current_review):
                        record.reviewed = current_review(scan.scan_id)
                    if status is PrivacyJobStatus.AWAITING_REVIEW:
                        record.preparation = None
                        record.message = _STATUS_MESSAGE[status]
                        record.progress_percent = max(
                            record.progress_percent,
                            _STATUS_PROGRESS[status],
                        )
                    else:
                        current_preparation = getattr(
                            pipeline,
                            "current_preparation",
                            None,
                        )
                        preparation = (
                            current_preparation(scan.scan_id)
                            if callable(current_preparation)
                            else None
                        )
                        if self._can_restore_confirmation(
                            record,
                            scan,
                            record.reviewed,
                            preparation,
                        ):
                            record.preparation = preparation
                            record.message = _STATUS_MESSAGE[status]
                            record.progress_percent = max(
                                record.progress_percent,
                                _STATUS_PROGRESS[status],
                            )
                        else:
                            self._fail_interrupted_restore(record)
                elif status in {
                    PrivacyJobStatus.QUEUED,
                    PrivacyJobStatus.INSPECTING,
                    PrivacyJobStatus.SCANNING,
                    PrivacyJobStatus.PLANNING,
                    PrivacyJobStatus.PREVIEWING,
                    PrivacyJobStatus.PROCESSING,
                    PrivacyJobStatus.VERIFYING,
                }:
                    self._fail_interrupted_restore(record)
                with self._lock:
                    self._jobs[record.job_id] = record
                self._persist(record)
            except (OSError, KeyError, TypeError, ValueError, PrivacyError):
                # Invalid persisted state is never executed or exposed as a job.
                continue

    def _can_restore_confirmation(
        self,
        record: PrivacyJobRecord,
        scan: PrivacyScanResult,
        reviewed: PrivacyReviewedResult | None,
        preparation: PrivacyPreparation | None,
    ) -> bool:
        if (
            record.execution_submitted
            or reviewed is None
            or preparation is None
            or reviewed.scan_id != scan.scan_id
            or preparation.review_id != reviewed.review_id
            or preparation.preview_relative_path != _PRIVATE_PREVIEW
            or preparation.plan.profile != record.profile_id
            or not hmac.compare_digest(
                preparation.plan.input_hash,
                scan.risk_map.input_hash,
            )
        ):
            return False
        try:
            self._job_store().resolve_artifact(
                record.job_id,
                _PRIVATE_PREVIEW,
                artifact_root=record.output_directory / _PRIVATE_ROOT,
            )
        except (FileNotFoundError, OSError):
            return False
        return True

    @staticmethod
    def _fail_interrupted_restore(record: PrivacyJobRecord) -> None:
        record.status = PrivacyJobStatus.FAILED
        record.message = _STATUS_MESSAGE[PrivacyJobStatus.FAILED]
        record.progress_percent = 100
        record.error = "Interrupted Safe Sharing work requires a new job"
        record._append_event()

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
                name="videoscope-privacy-cleanup",
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
            cleanup_thread.join(timeout=2)
        self._executor.shutdown(wait=wait, cancel_futures=True)


__all__ = [
    "PrivacyArtifactUnavailableError",
    "PrivacyConfirmationMismatchError",
    "PrivacyJobManager",
    "PrivacyJobRecord",
    "PrivacyJobStateError",
]
