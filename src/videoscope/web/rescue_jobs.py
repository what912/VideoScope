"""Persisted local Web jobs around the shared Video Rescue pipeline."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath, PureWindowsPath
from secrets import token_hex
from typing import Literal, Protocol, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from videoscope.processes import PinnedDescriptorError, pinned_descriptor_path
from videoscope.rescue.errors import (
    RescueCancelledError,
    RescueConfirmationError,
    RescueError,
)
from videoscope.rescue.models import (
    MediaDamageMap,
    RescueConfirmation,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    RescueSymptom,
    RescueVerificationReport,
    RescueVerificationStatus,
    rescue_public_artifacts,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    RescuePreparation,
    RescueResult,
    RescueStatus,
    VideoRescuePipeline,
)
from videoscope.video.errors import sanitize_diagnostic
from videoscope.web.jobs import CpuJobLimiter
from videoscope.web.models import (
    RescueJobEvent,
    RescueJobResponse,
    RescueJobStatus,
    WebServerConfig,
)
from videoscope.web.storage import LocalJobStore

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_STATE_NAME = "rescue-web-job.json"
_PRIVATE_ROOT = "rescue-review-private"
_PUBLIC_ROOT = "rescue-output"
_EVENT_LIMIT = 128
_TERMINAL_SOURCE_PREVIEW = re.compile(r"^source-[0-9]+\.mp4$")


def _os_name() -> str:
    return os.name


class _SnapshotState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    name: str = Field(pattern=r"^snapshot-[0-9a-f]{32}\.[a-z0-9]{1,10}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    device: int
    inode: int


class _ArtifactManifestEntry(BaseModel):
    """One strict relative artifact binding, including pinned file identity."""

    model_config = ConfigDict(extra="forbid", strict=True)

    relative_path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    device: int
    inode: int

    @model_validator(mode="after")
    def _validate_relative_path(self) -> _ArtifactManifestEntry:
        normalized = self.relative_path.replace("\\", "/")
        windows = PureWindowsPath(self.relative_path)
        if (
            PurePath(self.relative_path).is_absolute()
            or windows.is_absolute()
            or any(part in {"", ".", ".."} for part in normalized.split("/"))
        ):
            raise ValueError("artifact manifest path must be relative and safe")
        return self


class _PersistedRescueState(BaseModel):
    """Path-free versioned recovery; inconsistent payloads fail closed."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["0.2"]
    job_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    strategy: RescueStrategy
    symptoms: tuple[RescueSymptom, ...]
    locked_ranges: tuple[tuple[float, float], ...]
    balanced_strength_limit: float = Field(default=1.0, gt=0, le=1)
    status: RescueJobStatus
    message: str = Field(min_length=1, max_length=512)
    created_at: datetime
    updated_at: datetime
    upload_size_bytes: int = Field(ge=0)
    progress_percent: int = Field(ge=0, le=100)
    warnings: tuple[str, ...]
    error: str | None
    confirmation_submitted: bool
    input_snapshot: _SnapshotState | None
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    plan: RescuePlan | None
    damage_map: MediaDamageMap | None
    verification: RescueVerificationReport | None
    issued_public_artifacts: tuple[str, ...] = ()
    private_artifacts: tuple[str, ...]
    public_artifacts: tuple[str, ...]
    private_manifest: tuple[_ArtifactManifestEntry, ...] = ()
    public_manifest: tuple[_ArtifactManifestEntry, ...] = ()
    events: tuple[RescueJobEvent, ...] = Field(min_length=1, max_length=_EVENT_LIMIT)

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> _PersistedRescueState:
        events = self.events
        if events[0].sequence != 1 or events[0].status is not RescueJobStatus.QUEUED:
            raise ValueError("Rescue events must start with the queued event")
        if any(
            event.sequence != events[0].sequence + index
            for index, event in enumerate(events)
        ):
            raise ValueError("Rescue events must be strictly ordered and unique")
        if any(
            event.created_at > following.created_at
            for event, following in zip(events, events[1:], strict=False)
        ):
            raise ValueError("Rescue event timestamps cannot decrease")
        if any(
            event.progress_percent > following.progress_percent
            for event, following in zip(events, events[1:], strict=False)
        ):
            raise ValueError("Rescue event progress cannot decrease")
        if any(
            not _valid_rescue_event_transition(event.status, following.status)
            for event, following in zip(events, events[1:], strict=False)
        ):
            raise ValueError("Rescue events contain an illegal state transition")
        if (
            events[-1].status is not self.status
            or events[-1].message != self.message
            or events[-1].progress_percent != self.progress_percent
            or events[-1].created_at != self.updated_at
        ):
            raise ValueError("Rescue state must match its terminal event")
        if self.status.terminal and self.progress_percent != 100:
            raise ValueError("Terminal Rescue state must be complete")
        if self.status in {
            RescueJobStatus.COMPLETED,
            RescueJobStatus.NEEDS_REVIEW,
            RescueJobStatus.PARTIAL,
        }:
            if (
                self.input_snapshot is None
                or not self.plan_digest
                or self.plan is None
                or self.damage_map is None
                or self.plan.plan_digest != self.plan_digest
                or self.damage_map.input_hash != self.input_snapshot.sha256
                or self.plan.input_hash != self.input_snapshot.sha256
                or self.plan.strategy is not self.strategy
                or self.plan.requested_symptoms != self.symptoms
                or self.plan.damage_intervals != self.damage_map.intervals
                or self.verification is None
                or self.verification.plan_digest != self.plan_digest
                or not self.confirmation_submitted
                or not self.issued_public_artifacts
                or self.public_artifacts != self.issued_public_artifacts
                or self.plan.public_artifacts
                != rescue_public_artifacts(
                    include_improved=(
                        "improved-viewing.mp4" in self.plan.public_artifacts
                    )
                )
                or self.public_artifacts
                != rescue_public_artifacts(
                    include_improved=(
                        self.verification.improved_status
                        in {
                            RescueVerificationStatus.PASSED,
                            RescueVerificationStatus.NEEDS_REVIEW,
                        }
                    )
                )
                or len(self.public_manifest) != len(self.public_artifacts)
                or set(self.public_artifacts)
                != {item.relative_path for item in self.public_manifest}
            ):
                raise ValueError(
                    "Completed Rescue state lacks an exact artifact manifest"
                )
            public_bindings = {
                item.relative_path: item for item in self.public_manifest
            }
            expected_media = tuple(
                name
                for name in ("faithful-rescue.mp4", "improved-viewing.mp4")
                if name in self.public_artifacts
            )
            verification_bindings = {
                item.relative_path: item for item in self.verification.artifacts
            }
            if set(expected_media) - set(verification_bindings) or any(
                public_bindings[name].sha256 != verification_bindings[name].sha256
                for name in expected_media
            ):
                raise ValueError(
                    "Rescue verification does not bind the exact public media"
                )
        if len(self.private_manifest) != len(self.private_artifacts) or set(
            self.private_artifacts
        ) != {item.relative_path for item in self.private_manifest}:
            raise ValueError("Private Rescue manifest does not match its allowlist")
        return self


_PROGRESS = {
    RescueJobStatus.QUEUED: 0,
    RescueJobStatus.SCANNING: 20,
    RescueJobStatus.PLANNING: 45,
    RescueJobStatus.PREVIEWING: 65,
    RescueJobStatus.AWAITING_CONFIRMATION: 70,
    RescueJobStatus.PROCESSING: 75,
    RescueJobStatus.VERIFYING: 90,
    RescueJobStatus.COMPLETED: 100,
    RescueJobStatus.NEEDS_REVIEW: 100,
    RescueJobStatus.PARTIAL: 100,
    RescueJobStatus.FAILED: 100,
    RescueJobStatus.CANCELLED: 100,
}
_MESSAGES = {
    RescueJobStatus.QUEUED: "Video Rescue job queued",
    RescueJobStatus.SCANNING: "Scanning the local source",
    RescueJobStatus.PLANNING: "Building the Rescue plan",
    RescueJobStatus.PREVIEWING: "Rendering private review previews",
    RescueJobStatus.AWAITING_CONFIRMATION: "Awaiting exact plan confirmation",
    RescueJobStatus.PROCESSING: "Rendering confirmed Rescue outputs",
    RescueJobStatus.VERIFYING: "Verifying Rescue outputs",
    RescueJobStatus.COMPLETED: "Video Rescue completed",
    RescueJobStatus.NEEDS_REVIEW: "Video Rescue output needs review",
    RescueJobStatus.PARTIAL: "Video Rescue completed with partial output",
    RescueJobStatus.FAILED: "Video Rescue failed",
    RescueJobStatus.CANCELLED: "Video Rescue cancelled",
}
_CORE_TO_WEB = {
    RescueStatus.SCANNING: RescueJobStatus.SCANNING,
    RescueStatus.PLANNING: RescueJobStatus.PLANNING,
    RescueStatus.PREVIEWING: RescueJobStatus.PREVIEWING,
    RescueStatus.AWAITING_CONFIRMATION: RescueJobStatus.AWAITING_CONFIRMATION,
    RescueStatus.PROCESSING: RescueJobStatus.PROCESSING,
    RescueStatus.VERIFYING: RescueJobStatus.VERIFYING,
    RescueStatus.COMPLETED: RescueJobStatus.COMPLETED,
    RescueStatus.PARTIAL: RescueJobStatus.PARTIAL,
    RescueStatus.NEEDS_REVIEW: RescueJobStatus.NEEDS_REVIEW,
    RescueStatus.FAILED: RescueJobStatus.FAILED,
    RescueStatus.CANCELLED: RescueJobStatus.CANCELLED,
}


def _valid_rescue_event_transition(
    current: RescueJobStatus, following: RescueJobStatus
) -> bool:
    """Restore only the lifecycle edges emitted by this manager."""
    if current is following:
        return True
    if following in {
        RescueJobStatus.FAILED,
        RescueJobStatus.CANCELLED,
        RescueJobStatus.NEEDS_REVIEW,
        RescueJobStatus.PARTIAL,
    }:
        return not current.terminal
    return following in {
        RescueJobStatus.QUEUED: {RescueJobStatus.SCANNING},
        RescueJobStatus.SCANNING: {RescueJobStatus.PLANNING},
        RescueJobStatus.PLANNING: {RescueJobStatus.PREVIEWING},
        RescueJobStatus.PREVIEWING: {RescueJobStatus.AWAITING_CONFIRMATION},
        RescueJobStatus.AWAITING_CONFIRMATION: {RescueJobStatus.PROCESSING},
        RescueJobStatus.PROCESSING: {RescueJobStatus.VERIFYING},
        RescueJobStatus.VERIFYING: {
            RescueJobStatus.COMPLETED,
            RescueJobStatus.NEEDS_REVIEW,
        },
    }.get(current, set())


class RescuePipeline(Protocol):
    def prepare(self, source: Path) -> RescuePreparation: ...

    def confirm(
        self,
        preparation: RescuePreparation,
        confirmation: RescueConfirmation,
    ) -> RescuePreparation: ...

    def execute(
        self, preparation: RescuePreparation, confirmation: RescueConfirmation
    ) -> RescueResult: ...

    def cancel(self) -> None: ...

    def abort(self, preparation: RescuePreparation | None = None) -> None: ...

    def close(self) -> None: ...


RescuePipelineFactory: TypeAlias = Callable[..., RescuePipeline]


class RescueJobStateError(RuntimeError):
    """The requested operation is invalid for the current Rescue state."""


class RescueConfirmationMismatchError(RescueJobStateError):
    """The submitted confirmation differs from the issued plan."""


class RescueArtifactUnavailableError(RescueJobStateError):
    """The requested artifact is not available at this lifecycle point."""


@dataclass(frozen=True, slots=True)
class PinnedRescueArtifact:
    """An already-open, verified artifact; callers must close its descriptor."""

    descriptor: int
    size_bytes: int
    name: str


@dataclass(slots=True)
class RescueJobRecord:
    job_id: str
    directory: Path
    input_path: Path
    output_directory: Path
    strategy: RescueStrategy
    symptoms: tuple[RescueSymptom, ...]
    locked_ranges: tuple[tuple[float, float], ...] = ()
    balanced_strength_limit: float = 1.0
    warnings: tuple[str, ...] = ()
    status: RescueJobStatus = RescueJobStatus.QUEUED
    message: str = _MESSAGES[RescueJobStatus.QUEUED]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    upload_size_bytes: int = 0
    input_sha256: str | None = None
    input_device: int | None = None
    input_inode: int | None = None
    input_descriptor: int | None = None
    progress_percent: int = 0
    error: str | None = None
    cancellation: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None
    prepare_worker_active: bool = False
    pipeline: RescuePipeline | None = None
    preparation: RescuePreparation | None = None
    result: RescueResult | None = None
    completion_cutoff_reached: bool = False
    confirmation_submitted: bool = False
    private_artifacts: tuple[str, ...] = ()
    public_artifacts: tuple[str, ...] = ()
    issued_public_artifacts: tuple[str, ...] = ()
    persisted_plan: RescuePlan | None = None
    persisted_damage_map: MediaDamageMap | None = None
    persisted_verification: RescueVerificationReport | None = None
    private_manifest: dict[str, _ArtifactManifestEntry] = field(default_factory=dict)
    public_manifest: dict[str, _ArtifactManifestEntry] = field(default_factory=dict)
    events: list[RescueJobEvent] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if not self.events:
            self._append_event()

    def _append_event(self) -> None:
        now = datetime.now(UTC)
        self.updated_at = now
        sequence = self.events[-1].sequence + 1 if self.events else 1
        self.events.append(
            RescueJobEvent(
                sequence=sequence,
                status=self.status,
                message=self.message,
                progress_percent=self.progress_percent,
                created_at=now,
            )
        )
        if len(self.events) > _EVENT_LIMIT:
            del self.events[:-_EVENT_LIMIT]

    def snapshot(self) -> RescueJobResponse:
        with self.lock:
            base = f"/api/rescue/jobs/{self.job_id}"
            return RescueJobResponse(
                job_id=self.job_id,
                status=self.status,
                message=self.message,
                created_at=self.created_at,
                updated_at=self.updated_at,
                upload_size_bytes=self.upload_size_bytes,
                progress_percent=self.progress_percent,
                strategy=self.strategy,
                symptoms=self.symptoms,
                locked_ranges=self.locked_ranges,
                balanced_strength_limit=self.balanced_strength_limit,
                private_artifacts=self.private_artifacts,
                plan_digest=(
                    self.preparation.plan.plan_digest
                    if self.preparation is not None
                    else (
                        self.persisted_plan.plan_digest
                        if self.persisted_plan is not None
                        else None
                    )
                ),
                warnings=self.warnings,
                error=self.error,
                links={
                    "self": base,
                    "events": f"{base}/events",
                    "damage_map": f"{base}/damage-map",
                    "plan": f"{base}/plan",
                    "artifacts": f"{base}/artifacts/{{path}}",
                },
            )

    def events_after(self, sequence: int) -> tuple[RescueJobEvent, ...]:
        with self.lock:
            return tuple(event for event in self.events if event.sequence > sequence)


class RescueJobManager:
    """Own local Rescue job roots; the core pipeline does all media work."""

    def __init__(
        self,
        config: WebServerConfig | None = None,
        *,
        pipeline_factory: RescuePipelineFactory = VideoRescuePipeline,
        cpu_limiter: CpuJobLimiter | None = None,
    ) -> None:
        self.config = config or WebServerConfig()
        self.job_root = Path(self.config.job_root / "rescue").resolve(strict=False)
        self.pipeline_factory = pipeline_factory
        self._cpu_limiter = cpu_limiter or CpuJobLimiter(self.config.cpu_concurrency)
        self._jobs: dict[str, RescueJobRecord] = {}
        self._lock = threading.RLock()
        self._store: LocalJobStore | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=self.config.cpu_concurrency,
            thread_name_prefix="videoscope-rescue-cpu",
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
        strategy: RescueStrategy,
        symptoms: tuple[RescueSymptom, ...] = (),
        locked_ranges: tuple[tuple[float, float], ...] = (),
        balanced_strength_limit: float = 1.0,
        warnings: tuple[str, ...] = (),
    ) -> RescueJobRecord:
        paths = self._job_store().reserve(original_filename)
        record = RescueJobRecord(
            job_id=paths.job_id,
            directory=paths.directory,
            input_path=paths.input_path,
            output_directory=paths.output_directory,
            strategy=strategy,
            symptoms=symptoms,
            locked_ranges=locked_ranges,
            balanced_strength_limit=balanced_strength_limit,
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
            self._release_pipeline(record)
            self._close_input_descriptor(record)
        self._job_store().discard(job_id)

    def persist(self, job_id: str) -> None:
        self._persist(self.require(job_id))

    def commit_input_snapshot(self, job_id: str, staging_path: Path) -> None:
        """Pin an upload in an unpredictable, private snapshot before processing."""
        record = self.require(job_id)
        staging = Path(staging_path)
        destination = record.directory / f"snapshot-{token_hex(16)}{staging.suffix}"
        descriptor = _secure_read_open(staging)
        os.set_inheritable(descriptor, False)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RescueJobStateError("Uploaded video snapshot is not safe")
            digest = _hash_descriptor(descriptor)
        finally:
            os.close(descriptor)
        # `replace` changes only the name in this owned job directory.  The
        # subsequent no-follow open verifies the identity after publication.
        staging.replace(destination)
        if _os_name() == "posix":
            destination.chmod(stat.S_IRUSR)
        descriptor = _secure_read_open(destination)
        os.set_inheritable(descriptor, False)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_size <= 0
                or _hash_descriptor(descriptor) != digest
            ):
                raise RescueJobStateError("Uploaded video snapshot is not safe")
        except BaseException:
            os.close(descriptor)
            raise
        with record.lock:
            record.input_path = destination
            record.upload_size_bytes = metadata.st_size
            record.input_sha256 = digest
            record.input_device = metadata.st_dev
            record.input_inode = metadata.st_ino
            record.input_descriptor = descriptor
        self._persist(record)

    def _assert_input_snapshot_integrity(self, record: RescueJobRecord) -> None:
        """Fail closed if the path-based pipeline source stopped being our snapshot."""
        if (
            record.input_sha256 is None
            or record.input_device is None
            or record.input_inode is None
        ):
            raise RescueJobStateError("Rescue input snapshot is unavailable")
        descriptor = record.input_descriptor
        if descriptor is None:
            raise RescueJobStateError("Rescue input snapshot is not pinned")
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_dev != record.input_device
                or metadata.st_ino != record.input_inode
                or metadata.st_size != record.upload_size_bytes
                or _hash_descriptor(descriptor) != record.input_sha256
            ):
                raise RescueJobStateError(
                    "Rescue input snapshot integrity check failed"
                )
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)

    @staticmethod
    def _pipeline_source(record: RescueJobRecord) -> Path:
        if record.input_descriptor is None:
            raise RescueJobStateError("Rescue input snapshot is not pinned")
        if _os_name() == "posix":
            try:
                return pinned_descriptor_path(record.input_descriptor)
            except PinnedDescriptorError as exc:
                raise RescueJobStateError(str(exc)) from exc
        return record.input_path

    def submit_prepare(self, job_id: str) -> RescueJobResponse:
        record = self.require(job_id)
        with record.lock:
            if record.future is not None:
                raise RescueJobStateError("Rescue job is already queued")
            record.prepare_worker_active = True
            try:
                record.future = self._executor.submit(self._run_prepare_bounded, job_id)
            except BaseException:
                record.prepare_worker_active = False
                raise
        return record.snapshot()

    def _run_prepare_bounded(self, job_id: str) -> None:
        record = self.require(job_id)
        try:
            with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
                if acquired:
                    self._run_prepare(job_id)
                else:
                    self._finish(record, RescueJobStatus.CANCELLED)
        finally:
            with record.lock:
                record.prepare_worker_active = False
                finish_cancelled = (
                    record.cancellation.is_set() and not record.status.terminal
                )
            if finish_cancelled:
                self._finish(record, RescueJobStatus.CANCELLED)

    def _build_pipeline(self, record: RescueJobRecord) -> RescuePipeline:
        config = RescueConfig(
            output_directory=record.output_directory,
            strategy=record.strategy,
            symptoms=record.symptoms,
            locked_ranges=record.locked_ranges,
            effective_config=RescueEffectiveConfig(
                balanced_strength_limit=record.balanced_strength_limit
            ),
            keep_workspace=True,
        )
        return self.pipeline_factory(
            config,
            progress=lambda state: self._on_pipeline_progress(record, state),
        )

    def _on_pipeline_progress(
        self, record: RescueJobRecord, state: RescueStatus
    ) -> None:
        mapped = _CORE_TO_WEB[state]
        # The pipeline may announce this state before returning its immutable
        # preparation.  Only publish it after that plan has been assigned.
        if mapped.terminal or mapped is RescueJobStatus.AWAITING_CONFIRMATION:
            return
        self._update(record, mapped)

    def _run_prepare(self, job_id: str) -> None:
        record = self.require(job_id)
        try:
            with record.lock:
                cancelled = record.cancellation.is_set()
                if not cancelled:
                    self._assert_input_snapshot_integrity(record)
                    pipeline = self._build_pipeline(record)
                    record.pipeline = pipeline
            if cancelled:
                self._finish(record, RescueJobStatus.CANCELLED)
                return
            preparation = pipeline.prepare(self._pipeline_source(record))
            with record.lock:
                cancelled_after_prepare = record.cancellation.is_set()
                if not cancelled_after_prepare:
                    self._assert_input_snapshot_integrity(record)
                    record.preparation = preparation
                    record.persisted_plan = (
                        preparation.plan
                        if isinstance(preparation.plan, RescuePlan)
                        else None
                    )
                    record.persisted_damage_map = (
                        preparation.damage_map
                        if isinstance(preparation.damage_map, MediaDamageMap)
                        else None
                    )
                    record.issued_public_artifacts = tuple(
                        preparation.plan.public_artifacts
                    )
                    record.private_artifacts = self._preview_paths(record, preparation)
                    self._update(record, RescueJobStatus.AWAITING_CONFIRMATION)
            if cancelled_after_prepare:
                try:
                    pipeline.abort(preparation)
                finally:
                    self._finish(record, RescueJobStatus.CANCELLED)
        except RescueCancelledError:
            self._finish(record, RescueJobStatus.CANCELLED)
        except RescueError as exc:
            self._finish(
                record, RescueJobStatus.FAILED, error=self._sanitize_error(record, exc)
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            self._finish(
                record,
                RescueJobStatus.FAILED,
                error=f"Internal Video Rescue failure: {type(exc).__name__}",
            )

    def _preview_paths(
        self, record: RescueJobRecord, preparation: RescuePreparation
    ) -> tuple[str, ...]:
        previews = preparation.previews
        if previews is None:
            return ()
        private_root = record.output_directory / _PRIVATE_ROOT
        paths: list[str] = []
        for variant in (previews.source, previews.faithful, previews.improved):
            if variant is None:
                continue
            for path in variant.paths:
                try:
                    relative = (
                        path.resolve(strict=False)
                        .relative_to(private_root.resolve())
                        .as_posix()
                    )
                    paths.append(relative)
                    record.private_manifest[relative] = self._manifest_entry(
                        path, relative
                    )
                except ValueError:
                    continue
        return tuple(sorted(set(paths)))

    def snapshot(self, job_id: str) -> RescueJobResponse:
        return self.require(job_id).snapshot()

    def active_job_count(self) -> int:
        """Return the number of non-terminal Rescue jobs for local health."""
        with self._lock:
            return sum(
                not record.snapshot().status.terminal for record in self._jobs.values()
            )

    def events_after(self, job_id: str, sequence: int) -> tuple[RescueJobEvent, ...]:
        return self.require(job_id).events_after(sequence)

    def damage_map(self, job_id: str) -> MediaDamageMap:
        record = self.require(job_id)
        with record.lock:
            if record.preparation is None:
                raise RescueJobStateError("Damage map is not available")
            return record.preparation.damage_map

    def plan(self, job_id: str) -> RescuePlan:
        record = self.require(job_id)
        with record.lock:
            if record.preparation is None:
                raise RescueJobStateError("Rescue plan is not available")
            return record.preparation.plan

    def confirm(
        self, job_id: str, confirmation: RescueConfirmation
    ) -> RescueJobResponse:
        record = self.require(job_id)
        with record.lock:
            preparation = record.preparation
            if (
                record.status is not RescueJobStatus.AWAITING_CONFIRMATION
                or preparation is None
            ):
                raise RescueJobStateError("Rescue plan is not awaiting confirmation")
            if record.confirmation_submitted or not hmac.compare_digest(
                confirmation.plan_digest, preparation.plan.plan_digest
            ):
                raise RescueConfirmationMismatchError(
                    "Confirmation does not match the issued Rescue plan"
                )
            try:
                preparation.plan.validate_confirmation(confirmation)
            except ValueError as exc:
                raise RescueConfirmationMismatchError(
                    "Confirmation choices do not match the issued Rescue plan"
                ) from exc
            pipeline = record.pipeline
            if pipeline is None:
                raise RescueJobStateError("Rescue pipeline is unavailable")
            try:
                pipeline.confirm(preparation, confirmation)
            except RescueConfirmationError as exc:
                raise RescueConfirmationMismatchError(
                    "Confirmation does not match the issued Rescue plan"
                ) from exc
            record.confirmation_submitted = True
            self._update(record, RescueJobStatus.PROCESSING)
            record.future = self._executor.submit(
                self._run_execute_bounded, job_id, confirmation
            )
        return record.snapshot()

    def _run_execute_bounded(
        self, job_id: str, confirmation: RescueConfirmation
    ) -> None:
        record = self.require(job_id)
        with self._cpu_limiter.slot(record.cancellation.is_set) as acquired:
            if acquired:
                self._run_execute(job_id, confirmation)
            else:
                self._finish(record, RescueJobStatus.CANCELLED)

    def _run_execute(self, job_id: str, confirmation: RescueConfirmation) -> None:
        record = self.require(job_id)
        with record.lock:
            cancelled = record.cancellation.is_set()
            pipeline, preparation = record.pipeline, record.preparation
        if cancelled:
            self._finish(record, RescueJobStatus.CANCELLED)
            return
        if pipeline is None or preparation is None:
            self._finish(
                record,
                RescueJobStatus.FAILED,
                error="Rescue preparation is unavailable",
            )
            return
        try:
            self._assert_input_snapshot_integrity(record)
            result = pipeline.execute(preparation, confirmation)
            terminal = self._accept_execute_result(record, result)
            self._finish(record, terminal)
        except RescueCancelledError:
            self._finish(record, RescueJobStatus.CANCELLED)
        except RescueError as exc:
            self._finish(
                record, RescueJobStatus.FAILED, error=self._sanitize_error(record, exc)
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if record.cancellation.is_set() and not record.completion_cutoff_reached:
                self._finish(record, RescueJobStatus.CANCELLED)
            else:
                self._finish(
                    record,
                    RescueJobStatus.FAILED,
                    error=f"Internal Video Rescue failure: {type(exc).__name__}",
                )

    def cancel(self, job_id: str) -> RescueJobResponse:
        record = self.require(job_id)
        with record.lock:
            if record.status.terminal:
                return record.snapshot()
            record.cancellation.set()
            future = record.future
            prepare_worker_active = record.prepare_worker_active
            status = record.status
        future_cancelled = future.cancel() if future is not None else False
        self._release_pipeline(record, abort=True, cancel=True)
        if (
            future is None
            or future_cancelled
            or future.done()
            or (
                status is RescueJobStatus.AWAITING_CONFIRMATION
                and not prepare_worker_active
            )
        ):
            self._finish(record, RescueJobStatus.CANCELLED)
        else:
            with record.lock:
                if record.status.terminal:
                    return record.snapshot()
                record.message = "Cancellation requested"
                record._append_event()
                self._persist(record)
        return record.snapshot()

    def delete_or_cancel(self, job_id: str) -> RescueJobResponse | None:
        record = self.require(job_id)
        if not record.snapshot().status.terminal:
            return self.cancel(job_id)
        with self._lock:
            self._jobs.pop(job_id, None)
        self._release_pipeline(record)
        self._close_input_descriptor(record)
        self._job_store().discard(job_id)
        return None

    def resolve_public_artifact(self, job_id: str, requested_path: str) -> Path:
        record = self.require(job_id)
        if requested_path not in record.public_artifacts:
            raise FileNotFoundError("Artifact not found")
        if not record.status.terminal:
            raise RescueArtifactUnavailableError("Rescue artifacts are not available")
        path = Path(
            self._job_store().resolve_artifact(
                job_id,
                requested_path,
                artifact_root=record.output_directory / _PUBLIC_ROOT,
            )
        )
        if path.stat().st_nlink != 1:
            raise FileNotFoundError("Artifact not found")
        return path

    def open_public_artifact(
        self, job_id: str, requested_path: str
    ) -> PinnedRescueArtifact:
        """Open a manifest-listed public artifact once, without following links."""
        record = self.require(job_id)
        path = self.resolve_public_artifact(job_id, requested_path)
        return self._open_pinned_artifact(
            path,
            requested_path,
            "Artifact not found",
            expected=record.public_manifest.get(requested_path),
        )

    def resolve_private_artifact(self, job_id: str, requested_path: str) -> Path:
        record = self.require(job_id)
        if requested_path not in record.private_artifacts:
            raise FileNotFoundError("Private preview not found")
        if (
            record.status.terminal
            and _TERMINAL_SOURCE_PREVIEW.fullmatch(requested_path) is None
        ):
            raise RescueArtifactUnavailableError(
                "Private Rescue previews are not available"
            )
        path = Path(
            self._job_store().resolve_artifact(
                job_id,
                requested_path,
                artifact_root=record.output_directory / _PRIVATE_ROOT,
            )
        )
        if path.stat().st_nlink != 1:
            raise FileNotFoundError("Private preview not found")
        return path

    def open_private_artifact(
        self, job_id: str, requested_path: str
    ) -> PinnedRescueArtifact:
        """Open a private preview once after its allowlist and root validation."""
        path = self.resolve_private_artifact(job_id, requested_path)
        return self._open_pinned_artifact(
            path,
            requested_path,
            "Private preview not found",
            expected=self.require(job_id).private_manifest.get(requested_path),
        )

    @staticmethod
    def _open_pinned_artifact(
        path: Path,
        requested_path: str,
        message: str,
        *,
        expected: _ArtifactManifestEntry | None = None,
    ) -> PinnedRescueArtifact:
        descriptor = _secure_read_open(path)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise FileNotFoundError(message)
            if expected is None or expected != _manifest_entry_from_descriptor(
                descriptor, requested_path
            ):
                raise FileNotFoundError(message)
            return PinnedRescueArtifact(
                descriptor=descriptor, size_bytes=metadata.st_size, name=requested_path
            )
        except BaseException:
            os.close(descriptor)
            raise

    def _validate_public_bundle(
        self, record: RescueJobRecord, declared: tuple[str, ...]
    ) -> None:
        """Require the exact issued Task 9 manifest to exist as ordinary files."""
        if not declared or len(declared) != len(set(declared)):
            raise RescueJobStateError("Issued Rescue artifact manifest is invalid")
        root = record.output_directory / _PUBLIC_ROOT
        if not root.is_dir() or {entry.name for entry in root.iterdir()} != set(
            declared
        ):
            raise RescueJobStateError("Issued Rescue artifact manifest is invalid")
        manifest: dict[str, _ArtifactManifestEntry] = {}
        for relative_path in declared:
            path = Path(
                self._job_store().resolve_artifact(
                    record.job_id, relative_path, artifact_root=root
                )
            )
            manifest[relative_path] = self._manifest_entry(path, relative_path)
        record.public_manifest = manifest

    @staticmethod
    def _manifest_entry(path: Path, relative_path: str) -> _ArtifactManifestEntry:
        descriptor = _secure_read_open(path)
        try:
            return _manifest_entry_from_descriptor(descriptor, relative_path)
        finally:
            os.close(descriptor)

    def require(self, job_id: str) -> RescueJobRecord:
        if _JOB_ID.fullmatch(job_id) is None:
            raise KeyError(job_id)
        with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(job_id) from exc

    def _update(self, record: RescueJobRecord, status: RescueJobStatus) -> None:
        with record.lock:
            if record.status.terminal or record.cancellation.is_set():
                return
            if status is record.status:
                return
            record.status = status
            record.message = _MESSAGES[status]
            record.progress_percent = max(record.progress_percent, _PROGRESS[status])
            record._append_event()
            self._persist(record)

    def _finish(
        self,
        record: RescueJobRecord,
        status: RescueJobStatus,
        *,
        error: str | None = None,
    ) -> None:
        if not status.terminal:
            raise ValueError("terminal Rescue status required")
        with record.lock:
            if record.status.terminal:
                return
        try:
            self._release_pipeline(record)
        finally:
            self._close_input_descriptor(record)
        with record.lock:
            if record.status.terminal:
                return
            if record.cancellation.is_set() and not record.completion_cutoff_reached:
                status, error = RescueJobStatus.CANCELLED, None
            record.status = status
            record.message = _MESSAGES[status]
            record.progress_percent = 100
            record.error = error
            record._append_event()
            self._persist(record)

    def _accept_execute_result(
        self, record: RescueJobRecord, result: RescueResult
    ) -> RescueJobStatus:
        """Accept a core result after the irrevocable publication cutoff."""
        with record.lock:
            record.completion_cutoff_reached = True
            self._assert_input_snapshot_integrity(record)
            record.result = result
            verification = getattr(result, "verification", None)
            record.persisted_verification = (
                verification
                if isinstance(verification, RescueVerificationReport)
                else None
            )
            # A failed optional improvement may be absent from the exact public
            # bundle while the full confirmation-bound plan remains persisted.
            declared = (
                rescue_public_artifacts(
                    include_improved=(
                        verification.improved_status
                        in {
                            RescueVerificationStatus.PASSED,
                            RescueVerificationStatus.NEEDS_REVIEW,
                        }
                    )
                )
                if isinstance(verification, RescueVerificationReport)
                else record.issued_public_artifacts
            )
            self._validate_public_bundle(record, declared)
            record.issued_public_artifacts = declared
            record.public_artifacts = declared
            return _CORE_TO_WEB[result.status]

    @staticmethod
    def _release_pipeline(
        record: RescueJobRecord,
        *,
        abort: bool = False,
        cancel: bool = False,
    ) -> None:
        with record.lock:
            pipeline, preparation = record.pipeline, record.preparation
            record.pipeline = None
            record.preparation = None
        if pipeline is None:
            return
        try:
            if cancel:
                pipeline.cancel()
        finally:
            if abort:
                pipeline.abort(preparation)
            else:
                pipeline.close()

    @staticmethod
    def _close_input_descriptor(record: RescueJobRecord) -> None:
        with record.lock:
            descriptor = record.input_descriptor
            record.input_descriptor = None
        if descriptor is None:
            return
        try:
            # POSIX snapshots are owner-read-only while a job is active. Restore
            # owner write access on the pinned inode at the terminal boundary so
            # the application's retained private source is no longer left in an
            # artificial locked state. ``fchmod`` avoids following a replaced
            # path while the descriptor still proves which inode we own.
            fchmod = getattr(os, "fchmod", None)
            if _os_name() == "posix" and fchmod is not None:
                try:
                    fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
                except OSError:
                    pass
            os.close(descriptor)
        except OSError:
            pass

    @staticmethod
    def _sanitize_error(record: RescueJobRecord, exc: Exception) -> str:
        message = sanitize_diagnostic(
            str(exc), sensitive_paths=(record.directory, record.input_path)
        )
        message = " ".join(message.split())
        return message or f"Video Rescue failed: {type(exc).__name__}"

    def _persist(self, record: RescueJobRecord) -> None:
        with record.lock:
            payload = {
                "schema_version": "0.2",
                "job_id": record.job_id,
                "strategy": record.strategy.value,
                "symptoms": [item.value for item in record.symptoms],
                "locked_ranges": [list(item) for item in record.locked_ranges],
                "balanced_strength_limit": record.balanced_strength_limit,
                "status": record.status.value,
                "message": record.message,
                "created_at": record.created_at.isoformat(),
                "updated_at": record.updated_at.isoformat(),
                "upload_size_bytes": record.upload_size_bytes,
                "input_snapshot": (
                    {
                        "name": record.input_path.name,
                        "sha256": record.input_sha256,
                        "size_bytes": record.upload_size_bytes,
                        "device": record.input_device,
                        "inode": record.input_inode,
                    }
                    if (
                        record.input_sha256 is not None
                        and record.input_device is not None
                        and record.input_inode is not None
                    )
                    else None
                ),
                "plan_digest": (
                    record.preparation.plan.plan_digest
                    if record.preparation is not None
                    else (
                        record.persisted_plan.plan_digest
                        if record.persisted_plan is not None
                        else None
                    )
                ),
                "plan": (
                    record.persisted_plan.model_dump(mode="json")
                    if record.persisted_plan is not None
                    else None
                ),
                "damage_map": (
                    record.persisted_damage_map.model_dump(mode="json")
                    if record.persisted_damage_map is not None
                    else None
                ),
                "verification": (
                    record.persisted_verification.model_dump(mode="json")
                    if record.persisted_verification is not None
                    else None
                ),
                "issued_public_artifacts": list(record.issued_public_artifacts),
                "progress_percent": record.progress_percent,
                "warnings": list(record.warnings),
                "error": record.error,
                "confirmation_submitted": record.confirmation_submitted,
                "private_artifacts": list(record.private_artifacts),
                "public_artifacts": list(record.public_artifacts),
                "private_manifest": [
                    item.model_dump(mode="json")
                    for item in record.private_manifest.values()
                ],
                "public_manifest": [
                    item.model_dump(mode="json")
                    for item in record.public_manifest.values()
                ],
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
        if not self.job_root.is_dir():
            return
        for child in sorted(self.job_root.iterdir(), key=lambda path: path.name):
            if _JOB_ID.fullmatch(child.name) is None:
                continue
            try:
                directory = self._job_store().require_directory(child.name)
                persisted = _PersistedRescueState.model_validate_json(
                    (directory / _STATE_NAME).read_bytes()
                )
                if persisted.job_id != child.name:
                    raise ValueError("invalid persisted Rescue state")
                snapshot = persisted.input_snapshot
                if snapshot is None:
                    if persisted.status not in {
                        RescueJobStatus.CANCELLED,
                        RescueJobStatus.FAILED,
                    }:
                        raise ValueError("missing Rescue input snapshot")
                    input_path = directory / "unavailable-snapshot.bin"
                else:
                    input_path = self._job_store().resolve_artifact(
                        child.name, snapshot.name, artifact_root=directory
                    )
                    descriptor = _secure_read_open(input_path)
                    try:
                        metadata = os.fstat(descriptor)
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or metadata.st_nlink != 1
                            or metadata.st_dev != snapshot.device
                            or metadata.st_ino != snapshot.inode
                            or metadata.st_size != snapshot.size_bytes
                            or _hash_descriptor(descriptor) != snapshot.sha256
                        ):
                            raise ValueError("invalid Rescue input snapshot")
                    finally:
                        os.close(descriptor)
                status = persisted.status
                if persisted.plan is not None and (
                    persisted.plan.effective_config.locked_ranges
                    != persisted.locked_ranges
                ):
                    raise ValueError("persisted locked ranges do not match signed plan")
                private_root = directory / "artifacts" / _PRIVATE_ROOT
                for manifest_entry in persisted.private_manifest:
                    artifact = self._job_store().resolve_artifact(
                        child.name,
                        manifest_entry.relative_path,
                        artifact_root=private_root,
                    )
                    if (
                        self._manifest_entry(artifact, manifest_entry.relative_path)
                        != manifest_entry
                    ):
                        raise ValueError("invalid private Rescue artifact manifest")
                if status in {
                    RescueJobStatus.COMPLETED,
                    RescueJobStatus.NEEDS_REVIEW,
                    RescueJobStatus.PARTIAL,
                }:
                    public_root = directory / "artifacts" / _PUBLIC_ROOT
                    if not public_root.is_dir() or {
                        entry.name for entry in public_root.iterdir()
                    } != set(persisted.public_artifacts):
                        raise ValueError("invalid Rescue public artifact tree")
                    for manifest_entry in persisted.public_manifest:
                        artifact = self._job_store().resolve_artifact(
                            child.name,
                            manifest_entry.relative_path,
                            artifact_root=public_root,
                        )
                        if (
                            self._manifest_entry(artifact, manifest_entry.relative_path)
                            != manifest_entry
                        ):
                            raise ValueError("invalid Rescue artifact manifest")
                record = RescueJobRecord(
                    job_id=child.name,
                    directory=directory,
                    input_path=input_path,
                    output_directory=directory / "artifacts",
                    strategy=persisted.strategy,
                    symptoms=persisted.symptoms,
                    locked_ranges=persisted.locked_ranges,
                    balanced_strength_limit=persisted.balanced_strength_limit,
                    warnings=persisted.warnings,
                    status=status,
                    message=persisted.message,
                    created_at=persisted.created_at,
                    updated_at=persisted.updated_at,
                    upload_size_bytes=persisted.upload_size_bytes,
                    input_sha256=snapshot.sha256 if snapshot else None,
                    input_device=snapshot.device if snapshot else None,
                    input_inode=snapshot.inode if snapshot else None,
                    progress_percent=persisted.progress_percent,
                    error=persisted.error,
                    confirmation_submitted=persisted.confirmation_submitted,
                    private_artifacts=persisted.private_artifacts,
                    public_artifacts=persisted.public_artifacts,
                    issued_public_artifacts=persisted.issued_public_artifacts,
                    persisted_plan=persisted.plan,
                    persisted_damage_map=persisted.damage_map,
                    persisted_verification=persisted.verification,
                    private_manifest={
                        item.relative_path: item for item in persisted.private_manifest
                    },
                    public_manifest={
                        item.relative_path: item for item in persisted.public_manifest
                    },
                    events=list(persisted.events),
                )
                if not status.terminal:
                    record.status = RescueJobStatus.FAILED
                    record.message = "Interrupted Video Rescue work requires a new job"
                    record.progress_percent = 100
                    record.error = "Interrupted Video Rescue work requires a new job"
                    record._append_event()
                with self._lock:
                    self._jobs[record.job_id] = record
                self._persist(record)
            except (OSError, TypeError, ValueError, KeyError, ValidationError):
                continue

    def _job_store(self) -> LocalJobStore:
        with self._lock:
            if self._store is None:
                self._store = LocalJobStore(self.job_root)
            return self._store

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
            self._release_pipeline(record)
            self._close_input_descriptor(record)
            self._job_store().discard(record.job_id)
        self._job_store().cleanup_orphans(cutoff=cutoff, active_job_ids=set(self._jobs))
        return expired

    def start_cleanup(self) -> None:
        with self._lock:
            if self._cleanup_thread is None:
                self._cleanup_thread = threading.Thread(
                    target=self._cleanup_loop,
                    name="videoscope-rescue-cleanup",
                    daemon=True,
                )
                self._cleanup_thread.start()

    def _cleanup_loop(self) -> None:
        while not self._cleanup_stop.wait(self.config.cleanup_interval_seconds):
            self.cleanup_expired()

    def shutdown(self, *, wait: bool = True) -> None:
        self._cleanup_stop.set()
        with self._lock:
            records, cleanup_thread = tuple(self._jobs.values()), self._cleanup_thread
        for record in records:
            if not record.snapshot().status.terminal:
                self.cancel(record.job_id)
        if cleanup_thread is not None:
            cleanup_thread.join(timeout=2)
        self._executor.shutdown(wait=wait, cancel_futures=True)
        for record in records:
            self._release_pipeline(record)
            self._close_input_descriptor(record)


def _hash_descriptor(descriptor: int) -> str:
    """Hash an open descriptor without trusting a pathname between reads."""
    hasher = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        hasher.update(block)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return hasher.hexdigest()


def _secure_read_open(path: Path) -> int:
    """Open one regular file without following links, retaining a safe descriptor."""
    if _os_name() != "nt":
        flag = getattr(os, "O_NOFOLLOW", None)
        if flag is None:
            raise RescueJobStateError(
                "No-follow file opens are unavailable on this platform"
            )
        return os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | flag)

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    win_dll = getattr(ctypes, "WinDLL")
    kernel32 = win_dll("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ: deny replacement or writes
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise FileNotFoundError("Local file could not be opened safely")
    information = _ByHandleFileInformation()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(information)):
        kernel32.CloseHandle(handle)
        raise FileNotFoundError("Local file could not be inspected safely")
    if information.dwFileAttributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
        kernel32.CloseHandle(handle)
        raise FileNotFoundError("Local file reparse points are not allowed")
    open_osfhandle = getattr(msvcrt, "open_osfhandle")
    return int(open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)))


def _manifest_entry_from_descriptor(
    descriptor: int, relative_path: str
) -> _ArtifactManifestEntry:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise FileNotFoundError("Artifact is not a private regular file")
    return _ArtifactManifestEntry(
        relative_path=relative_path,
        sha256=_hash_descriptor(descriptor),
        size_bytes=metadata.st_size,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )


__all__ = [
    "PinnedRescueArtifact",
    "RescueArtifactUnavailableError",
    "RescueConfirmationMismatchError",
    "RescueJobManager",
    "RescueJobRecord",
    "RescueJobStateError",
]
