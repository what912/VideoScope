"""Offline API coverage for the local Video Rescue workflow."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.rescue.assessment import RescueAssessmentBundle
from videoscope.rescue.errors import RescueCancelledError, RescueInputError
from videoscope.rescue.executor import RescuedSegment, RescueExecutionResult
from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueArtifact,
    RescueConfirmation,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    make_damage_id,
    make_rescue_plan_digest,
    rescue_public_artifacts,
)
from videoscope.rescue.pipeline import (
    RescuePipelineDependencies,
    RescueStatus,
    VideoRescuePipeline,
)
from videoscope.web import rescue_jobs as rescue_jobs_module
from videoscope.web.app import create_app
from videoscope.web.models import RescueJobStatus, WebServerConfig
from videoscope.web.rescue_jobs import RescueJobManager, RescueJobStateError


def _config(tmp_path: Path, **overrides: object) -> WebServerConfig:
    return WebServerConfig.model_validate(
        {
            "job_root": tmp_path / "application data" / "jobs",
            "max_upload_bytes": 1024,
            "upload_chunk_bytes": 4096,
            "cpu_concurrency": 1,
            "job_ttl_seconds": 60,
            "cleanup_interval_seconds": 60,
            **overrides,
        }
    )


class _FakePlan:
    plan_digest = "a" * 64
    public_artifacts = (
        "changes.json",
        "technical-report.json",
        "report.html",
        "faithful-rescue.mp4",
    )

    def validate_confirmation(self, confirmation: Any) -> None:
        if (
            not confirmation.publish_faithful
            or confirmation.publish_improved
            or tuple(confirmation.accepted_action_ids) != ("faithful",)
            or tuple(confirmation.accepted_trim_damage_ids) != ()
        ):
            raise ValueError("confirmation choices differ")


class FakeRescuePipeline:
    """In-process substitute for the separately tested Rescue boundary."""

    reject_prepare = False
    execute_started = False
    block_prepare = False
    block_execute = False
    prepare_started = threading.Event()
    prepare_release = threading.Event()
    execute_started_event = threading.Event()
    execute_release = threading.Event()
    active_prepares = 0
    max_active_prepares = 0
    state_lock = threading.Lock()
    instances: list[FakeRescuePipeline] = []

    @classmethod
    def reset(cls) -> None:
        cls.reject_prepare = False
        cls.execute_started = False
        cls.block_prepare = False
        cls.block_execute = False
        cls.prepare_started = threading.Event()
        cls.prepare_release = threading.Event()
        cls.execute_started_event = threading.Event()
        cls.execute_release = threading.Event()
        cls.active_prepares = 0
        cls.max_active_prepares = 0
        cls.instances = []

    def __init__(self, config: Any, *, progress: Any) -> None:
        self.config = config
        self.progress = progress
        self.cancelled = False
        self.aborted = False
        self.closed = False
        self.executing = False
        self.source_descriptor: int | None = None
        self.preparation: Any = None
        self.confirmation: Any = None
        type(self).instances.append(self)

    def prepare(self, source: Path) -> Any:
        if type(self).reject_prepare:
            raise RescueInputError(f"ffprobe rejected {source}; private SECRET")
        self.source_descriptor = os.open(source, os.O_RDONLY)
        os.set_inheritable(self.source_descriptor, False)
        with type(self).state_lock:
            type(self).active_prepares += 1
            type(self).max_active_prepares = max(
                type(self).max_active_prepares, type(self).active_prepares
            )
        type(self).prepare_started.set()
        while type(self).block_prepare and not self.cancelled:
            if type(self).prepare_release.wait(0.005):
                break
        with type(self).state_lock:
            type(self).active_prepares -= 1
        if self.cancelled:
            raise RescueCancelledError("cancelled")
        self.progress(RescueStatus.SCANNING)
        self.progress(RescueStatus.PLANNING)
        private = self.config.output_directory / "rescue-review-private"
        preview = private / "source-00.mp4"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"private-preview")
        variant = SimpleNamespace(paths=(preview,))
        self.preparation = SimpleNamespace(
            plan=_FakePlan(),
            damage_map=SimpleNamespace(),
            previews=SimpleNamespace(source=variant, faithful=variant, improved=None),
        )
        self.progress(RescueStatus.PREVIEWING)
        self.progress(RescueStatus.AWAITING_CONFIRMATION)
        return self.preparation

    def confirm(self, preparation: Any, confirmation: Any) -> Any:
        assert preparation is self.preparation
        preparation.plan.validate_confirmation(confirmation)
        self.confirmation = confirmation
        return preparation

    def execute(self, preparation: Any, confirmation: Any) -> Any:
        self.executing = True
        try:
            return self._execute(preparation, confirmation)
        finally:
            self.executing = False
            self._release_source()

    def _execute(self, preparation: Any, confirmation: Any) -> Any:
        assert preparation is self.preparation
        assert confirmation is self.confirmation
        assert not self.cancelled
        type(self).execute_started = True
        type(self).execute_started_event.set()
        self.progress(RescueStatus.PROCESSING)
        while type(self).block_execute and not self.cancelled:
            if type(self).execute_release.wait(0.005):
                break
        if self.cancelled:
            raise RescueCancelledError("cancelled")
        public = self.config.output_directory / "rescue-output"
        public.mkdir(parents=True, exist_ok=True)
        (public / "faithful-rescue.mp4").write_bytes(b"faithful")
        (public / "changes.json").write_text("[]", encoding="utf-8")
        (public / "technical-report.json").write_text("{}", encoding="utf-8")
        (public / "report.html").write_text("<html></html>", encoding="utf-8")
        self.progress(RescueStatus.VERIFYING)
        return SimpleNamespace(
            status=RescueStatus.COMPLETED,
            technical_report=SimpleNamespace(
                artifacts=(
                    SimpleNamespace(relative_path="faithful-rescue.mp4"),
                    SimpleNamespace(relative_path="technical-report.json"),
                )
            ),
        )

    def cancel(self) -> None:
        self.cancelled = True
        self.abort()

    def abort(self, preparation: Any | None = None) -> None:
        self.aborted = True
        if not self.executing and (
            preparation is None or preparation is self.preparation
        ):
            self.preparation = None
            self.confirmation = None
            self._release_source()

    def close(self) -> None:
        self.closed = True
        self.cancel()

    def _release_source(self) -> None:
        descriptor, self.source_descriptor = self.source_descriptor, None
        if descriptor is not None:
            os.close(descriptor)


class ContractRescuePipeline:
    """Fake media boundary that still emits the real persisted domain contract."""

    def __init__(self, config: Any, *, progress: Any) -> None:
        self.config = config
        self.progress = progress
        self.cancelled = False
        self.aborted = False
        self.closed = False
        self.executing = False
        self.source_descriptor: int | None = None
        self.preparation: Any = None
        self.confirmation: Any = None

    def prepare(self, source: Path) -> Any:
        self.source_descriptor = os.open(source, os.O_RDONLY)
        os.set_inheritable(self.source_descriptor, False)
        source_hash = sha256(source.read_bytes()).hexdigest()
        action = RescueAction(
            id="remux",
            version="1",
            kind=RescueActionKind.REMUX,
            description="Write a faithful local copy.",
            source_ranges=((0.0, 1.0),),
            changes_content=False,
            requires_confirmation=False,
        )
        payload: dict[str, JsonValue] = {
            "input_hash": source_hash,
            "strategy": RescueStrategy.CONSERVATIVE,
            "effective_config": RescueEffectiveConfig().model_dump(mode="json"),
            "actions": [action.model_dump(mode="json")],
            "preview_ranges": [[0.0, 1.0]],
            "private_artifacts": ["source-00.mp4"],
            "public_artifacts": list(rescue_public_artifacts()),
            "damage_intervals": [],
        }
        plan = RescuePlan.model_validate(
            payload | {"plan_digest": make_rescue_plan_digest(payload)}
        )
        damage_map = MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=1.0,
            scan_coverage=((0.0, 1.0),),
        )
        private = self.config.output_directory / "rescue-review-private"
        preview = private / "source-00.mp4"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"private-preview")
        variant = SimpleNamespace(paths=(preview,))
        self.preparation = SimpleNamespace(
            plan=plan,
            damage_map=damage_map,
            previews=SimpleNamespace(source=variant, faithful=variant, improved=None),
        )
        for status in (
            RescueStatus.SCANNING,
            RescueStatus.PLANNING,
            RescueStatus.PREVIEWING,
        ):
            self.progress(status)
        return self.preparation

    def confirm(self, preparation: Any, confirmation: Any) -> Any:
        assert preparation is self.preparation
        preparation.plan.validate_confirmation(confirmation)
        self.confirmation = confirmation
        return preparation

    def execute(self, preparation: Any, confirmation: Any) -> Any:
        self.executing = True
        try:
            return self._execute(preparation, confirmation)
        finally:
            self.executing = False
            self._release_source()

    def _execute(self, preparation: Any, confirmation: Any) -> Any:
        assert preparation is self.preparation
        assert confirmation is self.confirmation
        assert not self.cancelled
        self.progress(RescueStatus.PROCESSING)
        public = self.config.output_directory / "rescue-output"
        public.mkdir(parents=True, exist_ok=True)
        for name in rescue_public_artifacts():
            content = b"faithful" if name == "faithful-rescue.mp4" else name.encode()
            (public / name).write_bytes(content)
        verification = RescueVerificationReport(
            plan_digest=preparation.plan.plan_digest,
            faithful_status=RescueVerificationStatus.PASSED,
            checks=tuple(
                RescueVerificationCheck(
                    check_id=check_id,
                    artifact="faithful",
                    status=RescueVerificationStatus.PASSED,
                    message="Measured check passed.",
                )
                for check_id in RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
            ),
            artifacts=(
                RescueArtifact(
                    artifact_role="faithful",
                    relative_path="faithful-rescue.mp4",
                    sha256=sha256(b"faithful").hexdigest(),
                    description="Measured faithful candidate.",
                ),
            ),
            outcome=RescueOutcome.COMPLETED,
        )
        self.progress(RescueStatus.VERIFYING)
        return SimpleNamespace(
            status=RescueStatus.COMPLETED,
            verification=verification,
            technical_report=SimpleNamespace(artifacts=verification.artifacts),
        )

    def cancel(self) -> None:
        self.cancelled = True
        self.abort()

    def abort(self, preparation: Any | None = None) -> None:
        self.aborted = True
        if not self.executing and (
            preparation is None or preparation is self.preparation
        ):
            self.preparation = None
            self.confirmation = None
            self._release_source()

    def close(self) -> None:
        self.closed = True
        self.cancel()

    def _release_source(self) -> None:
        descriptor, self.source_descriptor = self.source_descriptor, None
        if descriptor is not None:
            os.close(descriptor)


class FaithfulFallbackContractPipeline(ContractRescuePipeline):
    """Issue a Balanced plan but publish only its verified faithful fallback."""

    def prepare(self, source: Path) -> Any:
        preparation = super().prepare(source)
        remux = preparation.plan.actions[0].model_copy(
            update={"strategy": RescueStrategy.BALANCED}
        )
        improve = RescueAction(
            id="adjust-luma",
            version="1",
            kind=RescueActionKind.ADJUST_LUMA,
            description="Apply a bounded luminance adjustment.",
            source_ranges=((0.0, 1.0),),
            changes_content=True,
            requires_confirmation=True,
            depends_on=("remux",),
            strategy=RescueStrategy.BALANCED,
        )
        payload: dict[str, JsonValue] = {
            "input_hash": preparation.plan.input_hash,
            "strategy": RescueStrategy.BALANCED,
            "effective_config": RescueEffectiveConfig().model_dump(mode="json"),
            "actions": [
                remux.model_dump(mode="json"),
                improve.model_dump(mode="json"),
            ],
            "preview_ranges": [[0.0, 1.0]],
            "private_artifacts": ["source-00.mp4"],
            "public_artifacts": list(rescue_public_artifacts(include_improved=True)),
            "damage_intervals": [],
        }
        preparation.plan = RescuePlan.model_validate(
            payload | {"plan_digest": make_rescue_plan_digest(payload)}
        )
        return preparation


class NeedsReviewContractPipeline(ContractRescuePipeline):
    """Publish a complete contract whose measured status requires review."""

    def execute(self, preparation: Any, confirmation: Any) -> Any:
        result = super().execute(preparation, confirmation)
        checks = list(result.verification.checks)
        checks[0] = checks[0].model_copy(
            update={"status": RescueVerificationStatus.NEEDS_REVIEW}
        )
        verification = RescueVerificationReport(
            plan_digest=preparation.plan.plan_digest,
            faithful_status=RescueVerificationStatus.NEEDS_REVIEW,
            checks=tuple(checks),
            artifacts=result.verification.artifacts,
            outcome=RescueOutcome.NEEDS_REVIEW,
        )
        return SimpleNamespace(
            status=RescueStatus.NEEDS_REVIEW,
            verification=verification,
            technical_report=SimpleNamespace(artifacts=verification.artifacts),
        )


class SnapshotLockProbePipeline(FakeRescuePipeline):
    """Attempt hostile source replacement throughout prepare and execute."""

    denied: list[tuple[str, str]] = []
    source: Path | None = None

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.denied = []
        cls.source = None

    @classmethod
    def _probe(cls, stage: str) -> None:
        source = cls.source
        assert source is not None
        operations: dict[str, Callable[[], object]] = {
            "write": lambda: source.write_bytes(b"changed"),
            "delete": source.unlink,
        }
        replacement = source.with_name(f".{stage}-replacement.mp4")
        replacement.write_bytes(b"replacement")
        operations["replace"] = lambda: replacement.replace(source)
        for name, operation in operations.items():
            try:
                operation()
            except OSError:
                cls.denied.append((stage, name))

    def prepare(self, source: Path) -> Any:
        type(self).source = source
        type(self)._probe("prepare")
        return super().prepare(source)

    def execute(self, preparation: Any, confirmation: Any) -> Any:
        type(self)._probe("execute")
        return super().execute(preparation, confirmation)


class FailAfterRetainingPipeline(FakeRescuePipeline):
    """Fail preparation only after retaining a real source descriptor."""

    retained_descriptor: int | None = None

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.retained_descriptor = None

    def prepare(self, source: Path) -> Any:
        self.source_descriptor = os.open(source, os.O_RDONLY)
        os.set_inheritable(self.source_descriptor, False)
        type(self).retained_descriptor = self.source_descriptor
        raise RescueInputError("local preparation failed")


class PrepareReturnBarrierPipeline(FakeRescuePipeline):
    """Hold a completed preparation immediately before returning it to Web."""

    return_ready = threading.Event()
    return_release = threading.Event()
    retained_descriptor: int | None = None

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.return_ready = threading.Event()
        cls.return_release = threading.Event()
        cls.retained_descriptor = None

    def prepare(self, source: Path) -> Any:
        preparation = super().prepare(source)
        type(self).retained_descriptor = self.source_descriptor
        type(self).return_ready.set()
        if not type(self).return_release.wait(timeout=2):
            raise AssertionError("prepare return barrier was not released")
        return preparation


class PostPublicationBarrierManager(RescueJobManager):
    """Hold the prepare worker after publication but before Future completion."""

    publication_ready = threading.Event()
    worker_release = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.publication_ready = threading.Event()
        cls.worker_release = threading.Event()

    def _run_prepare(self, job_id: str) -> None:
        super()._run_prepare(job_id)
        if (
            self.require(job_id).snapshot().status
            is RescueJobStatus.AWAITING_CONFIRMATION
        ):
            type(self).publication_ready.set()
            if not type(self).worker_release.wait(timeout=2):
                raise AssertionError("post-publication worker barrier was not released")


class PostExecuteReturnBarrierManager(RescueJobManager):
    """Hold Web immediately after core execution returns its published result."""

    result_ready = threading.Event()
    worker_release = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.result_ready = threading.Event()
        cls.worker_release = threading.Event()

    def _accept_execute_result(self, record: Any, result: Any) -> RescueJobStatus:
        type(self).result_ready.set()
        if not type(self).worker_release.wait(timeout=2):
            raise AssertionError("post-execute result barrier was not released")
        return super()._accept_execute_result(record, result)


class _RealContractScanner:
    def scan(
        self,
        _source: Path,
        source_hash: str,
        metadata: VideoMetadata,
        _config: object,
    ) -> MediaDamageMap:
        return MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=metadata.duration_seconds,
            scan_coverage=((0.0, metadata.duration_seconds),),
        )


class _RealContractAssessment:
    def assess(self, *_args: object) -> RescueAssessmentBundle:
        return RescueAssessmentBundle()


class _RealContractPreviewBuilder:
    def build(self, *_args: object) -> None:
        return None


class _RealContractExecutor:
    def execute_faithful(
        self,
        _plan: RescuePlan,
        _source: Path,
        work_root: Path,
        _cancellation_callback: object,
    ) -> RescueExecutionResult:
        path = work_root / "staging" / "faithful-rescue.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"faithful")
        segment = RescuedSegment(
            0.0,
            1.0,
            0.0,
            1.0,
            "staging/faithful-rescue.mp4",
        )
        return RescueExecutionResult(
            output_path=path,
            output_relative_path="faithful-rescue.mp4",
            segments=(segment,),
            source_mappings=(segment.source_mapping,),
        )


class _RealContractVerifier:
    def verify(
        self,
        _source: Path,
        faithful: Path,
        _improved: Path | None,
        plan: RescuePlan,
        _mappings: tuple[object, ...],
        _cancellation_callback: object,
        *,
        faithful_render_mode: str,
    ) -> RescueVerificationReport:
        del faithful_render_mode
        return RescueVerificationReport(
            plan_digest=plan.plan_digest,
            faithful_status=RescueVerificationStatus.PASSED,
            checks=tuple(
                RescueVerificationCheck(
                    check_id=check_id,
                    artifact="faithful",
                    status=RescueVerificationStatus.PASSED,
                    message="Measured local contract check passed.",
                )
                for check_id in RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
            ),
            artifacts=(
                RescueArtifact(
                    artifact_role="faithful",
                    relative_path="faithful-rescue.mp4",
                    sha256=sha256(faithful.read_bytes()).hexdigest(),
                    description="Measured faithful output.",
                ),
            ),
            outcome=RescueOutcome.COMPLETED,
        )


def _publish_real_contract(
    layout: Any,
    *,
    verification: RescueVerificationReport,
    **_kwargs: object,
) -> tuple[RescueArtifact, ...]:
    layout.public_root.mkdir(parents=True, exist_ok=True)
    artifacts: list[RescueArtifact] = []
    for relative_path in rescue_public_artifacts():
        content = b"faithful" if relative_path == "faithful-rescue.mp4" else b"{}"
        (layout.public_root / relative_path).write_bytes(content)
        artifacts.append(
            RescueArtifact(
                artifact_role=(
                    "faithful" if relative_path == "faithful-rescue.mp4" else "document"
                ),
                relative_path=relative_path,
                sha256=sha256(content).hexdigest(),
                description=(
                    verification.artifacts[0].description
                    if relative_path == "faithful-rescue.mp4"
                    else "Local contract document."
                ),
            )
        )
    return tuple(artifacts)


def _real_contract_pipeline_factory(config: Any, *, progress: Any) -> Any:
    dependencies = RescuePipelineDependencies(
        probe=lambda source: VideoMetadata(
            filename=source.name,
            container_format="mp4",
            codec="h264",
            width=16,
            height=16,
            duration_seconds=1.0,
            average_frame_rate=1.0,
            estimated_frame_count=1,
            has_audio=False,
            file_size_bytes=source.stat().st_size,
        ),
        scanner=_RealContractScanner(),
        assessment_service=_RealContractAssessment(),
        preview_builder=_RealContractPreviewBuilder(),
        executor=_RealContractExecutor(),
        verifier=_RealContractVerifier(),
        report_renderer=lambda *_args: "<!doctype html><html></html>",
        publisher=_publish_real_contract,
    )
    return VideoRescuePipeline(config, dependencies=dependencies, progress=progress)


def _client(tmp_path: Path, **overrides: object) -> tuple[TestClient, RescueJobManager]:
    config = _config(tmp_path).model_copy(update=overrides)
    manager = RescueJobManager(config, pipeline_factory=FakeRescuePipeline)
    return TestClient(create_app(config, rescue_manager=manager)), manager


def _wait(client: TestClient, job_id: str, expected: set[str]) -> dict[str, Any]:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = client.get(f"/api/rescue/jobs/{job_id}")
        assert response.status_code == 200
        body = cast(dict[str, Any], response.json())
        if body["status"] in expected:
            return body
        time.sleep(0.01)
    raise AssertionError(f"rescue job did not reach {sorted(expected)}")


def _upload(client: TestClient) -> dict[str, Any]:
    response = client.post(
        "/api/rescue/jobs",
        files={"video": ("clip.mp4", b"video", "video/mp4")},
        data={"strategy": "conservative"},
    )
    assert response.status_code == 202
    return cast(dict[str, Any], response.json())


def _awaiting_fake_job(
    manager: RescueJobManager,
) -> tuple[Any, FakeRescuePipeline, int]:
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    manager.commit_input_snapshot(record.job_id, staging)
    manager.submit_prepare(record.job_id)
    deadline = time.monotonic() + 3
    while (
        time.monotonic() < deadline
        and record.snapshot().status is not RescueJobStatus.AWAITING_CONFIRMATION
    ):
        time.sleep(0.01)
    assert record.snapshot().status is RescueJobStatus.AWAITING_CONFIRMATION
    pipeline = cast(FakeRescuePipeline, record.pipeline)
    assert pipeline.source_descriptor is not None
    return record, pipeline, pipeline.source_descriptor


def _completed_contract_state(
    tmp_path: Path,
) -> tuple[WebServerConfig, Path, dict[str, Any]]:
    """Create one restorable terminal state using only real Rescue models."""
    config = _config(tmp_path)
    manager = RescueJobManager(config, pipeline_factory=ContractRescuePipeline)
    client = TestClient(create_app(config, rescue_manager=manager))
    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        manager.confirm(
            job_id,
            RescueConfirmation(
                plan_digest=awaiting["plan_digest"],
                publish_faithful=True,
                publish_improved=False,
                accepted_action_ids=(),
                accepted_trim_damage_ids=(),
            ),
        )
        _wait(client, job_id, {"completed"})
    state_path = config.job_root / "rescue" / job_id / "rescue-web-job.json"
    return config, state_path, cast(dict[str, Any], json.loads(state_path.read_text()))


def _restore_job_ids(
    config: WebServerConfig, *, factory: Any = ContractRescuePipeline
) -> set[str]:
    manager = RescueJobManager(config, pipeline_factory=factory)
    try:
        return set(manager._jobs)
    finally:
        manager.shutdown()


def test_rescue_job_upload_starts_a_local_job(tmp_path: Path) -> None:
    """The missing rescue route would make this assertion fail."""
    client = TestClient(create_app(_config(tmp_path)))

    with client:
        response = client.post(
            "/api/rescue/jobs",
            files={"video": ("clip.mp4", b"video", "video/mp4")},
            data={"strategy": "conservative"},
        )

    assert response.status_code == 202
    payload = response.json()
    assert len(payload["job_id"]) == 32
    assert payload["status"] in {"queued", "scanning"}


def test_web_confirmation_registers_with_real_video_rescue_pipeline(
    tmp_path: Path,
) -> None:
    """Skipping the core confirm gate makes the real pipeline fail execution."""
    config = _config(tmp_path)
    manager = RescueJobManager(
        config,
        pipeline_factory=_real_contract_pipeline_factory,
    )
    client = TestClient(create_app(config, rescue_manager=manager))

    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        response = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": [],
                "accepted_trim_damage_ids": [],
            },
        )
        terminal = _wait(client, job_id, {"completed", "failed"})

    assert response.status_code == 202
    assert terminal["status"] == "completed", terminal["error"]


def test_cancel_before_confirmation_releases_real_core_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dropping the core lifecycle release retains its pinned source descriptor."""
    captured_descriptors: list[int] = []
    secure_open = cast(
        Callable[[Path], int],
        VideoRescuePipeline.prepare.__globals__["secure_read_open"],
    )

    def capture_core_descriptor(source: Path) -> int:
        descriptor = secure_open(source)
        captured_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setitem(
        VideoRescuePipeline.prepare.__globals__,
        "secure_read_open",
        capture_core_descriptor,
    )
    config = _config(tmp_path)
    manager = RescueJobManager(
        config,
        pipeline_factory=_real_contract_pipeline_factory,
    )
    client = TestClient(create_app(config, rescue_manager=manager))

    with client:
        job_id = _upload(client)["job_id"]
        _wait(client, job_id, {"awaiting_confirmation"})
        assert len(captured_descriptors) == 1
        descriptor = captured_descriptors[0]
        assert os.fstat(descriptor).st_size > 0

        response = client.delete(f"/api/rescue/jobs/{job_id}")
        terminal = _wait(client, job_id, {"cancelled"})

        assert response.status_code == 200
        assert terminal["status"] == "cancelled"
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_cancel_while_prepare_returns_finishes_without_reattaching_source(
    tmp_path: Path,
) -> None:
    """A cancelled prepare return must not reattach preparation ownership."""
    PrepareReturnBarrierPipeline.reset()
    config = _config(tmp_path)
    manager = RescueJobManager(config, pipeline_factory=PrepareReturnBarrierPipeline)
    client = TestClient(create_app(config, rescue_manager=manager))

    with client:
        job_id = _upload(client)["job_id"]
        record = manager.require(job_id)
        try:
            assert PrepareReturnBarrierPipeline.return_ready.wait(timeout=2)
            descriptor = PrepareReturnBarrierPipeline.retained_descriptor
            assert descriptor is not None
            assert os.fstat(descriptor).st_size > 0

            response = client.delete(f"/api/rescue/jobs/{job_id}")
        finally:
            PrepareReturnBarrierPipeline.return_release.set()
        assert record.future is not None
        record.future.result(timeout=2)
        terminal = client.get(f"/api/rescue/jobs/{job_id}").json()
        events = manager.events_after(job_id, 0)

        assert response.status_code == 200
        assert terminal["status"] == "cancelled"
        assert record.pipeline is None
        assert record.preparation is None
        with pytest.raises(OSError):
            os.fstat(descriptor)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert events[-1].status is RescueJobStatus.CANCELLED
        assert all(not event.status.terminal for event in events[:-1])


def test_cancel_after_prepare_publication_finishes_terminal_once(
    tmp_path: Path,
) -> None:
    """A published prepare worker must own cancellation until it returns."""
    FakeRescuePipeline.reset()
    PostPublicationBarrierManager.reset()
    config = _config(tmp_path)
    manager = PostPublicationBarrierManager(config, pipeline_factory=FakeRescuePipeline)
    client = TestClient(create_app(config, rescue_manager=manager))

    with client:
        job_id = _upload(client)["job_id"]
        record = manager.require(job_id)
        try:
            assert PostPublicationBarrierManager.publication_ready.wait(timeout=2)
            pipeline = cast(FakeRescuePipeline, record.pipeline)
            descriptor = pipeline.source_descriptor
            assert descriptor is not None
            assert os.fstat(descriptor).st_size > 0

            response = client.delete(f"/api/rescue/jobs/{job_id}")
        finally:
            PostPublicationBarrierManager.worker_release.set()
        assert record.future is not None
        record.future.result(timeout=2)
        terminal = client.get(f"/api/rescue/jobs/{job_id}").json()
        events = manager.events_after(job_id, 0)

        assert response.status_code == 200
        assert terminal["status"] == "cancelled"
        assert record.pipeline is None
        assert record.preparation is None
        with pytest.raises(OSError):
            os.fstat(descriptor)
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert [event.status for event in events if event.status.terminal] == [
            RescueJobStatus.CANCELLED
        ]
        assert events[-1].status is RescueJobStatus.CANCELLED


def test_cancel_after_core_execute_return_terminalizes_published_result(
    tmp_path: Path,
) -> None:
    """Web cancellation cannot override a verified result returned by core."""
    FakeRescuePipeline.reset()
    PostExecuteReturnBarrierManager.reset()
    config = _config(tmp_path)
    manager = PostExecuteReturnBarrierManager(
        config, pipeline_factory=FakeRescuePipeline
    )
    client = TestClient(create_app(config, rescue_manager=manager))

    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        confirmed = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        assert confirmed.status_code == 202
        try:
            assert PostExecuteReturnBarrierManager.result_ready.wait(timeout=2)
            cancellation = client.delete(f"/api/rescue/jobs/{job_id}")
        finally:
            PostExecuteReturnBarrierManager.worker_release.set()
        record = manager.require(job_id)
        assert record.future is not None
        record.future.result(timeout=2)
        terminal = client.get(f"/api/rescue/jobs/{job_id}").json()
        events = manager.events_after(job_id, 0)

    assert cancellation.status_code == 200
    assert terminal["status"] == "completed"
    assert [event.status for event in events if event.status.terminal] == [
        RescueJobStatus.COMPLETED
    ]


def test_needs_review_survives_web_sse_and_persisted_restore(tmp_path: Path) -> None:
    """Collapsing the core review gate into partial loses a distinct outcome."""
    config = _config(tmp_path)
    manager = RescueJobManager(config, pipeline_factory=NeedsReviewContractPipeline)
    client = TestClient(create_app(config, rescue_manager=manager))

    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        response = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": [],
                "accepted_trim_damage_ids": [],
            },
        )
        terminal = _wait(client, job_id, {"needs_review", "partial"})
        with client.stream("GET", f"/api/rescue/jobs/{job_id}/events") as stream:
            event_text = "".join(stream.iter_text())

    assert response.status_code == 202
    assert terminal["status"] == "needs_review"
    assert '"status":"needs_review"' in event_text

    restored = RescueJobManager(config, pipeline_factory=NeedsReviewContractPipeline)
    try:
        assert restored.snapshot(job_id).status is RescueJobStatus.NEEDS_REVIEW
    finally:
        restored.shutdown()


def test_rescue_prepare_options_are_validated_persisted_and_exposed(
    tmp_path: Path,
) -> None:
    client, manager = _client(tmp_path)

    with client:
        response = client.post(
            "/api/rescue/jobs",
            files={"video": ("clip.mp4", b"video", "video/mp4")},
            data={
                "strategy": "balanced",
                "locked_ranges": "[[1.25, 2.5]]",
                "balanced_strength_limit": "0.4",
            },
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["locked_ranges"] == [[1.25, 2.5]]
    assert payload["balanced_strength_limit"] == 0.4
    record = manager.require(payload["job_id"])
    assert record.locked_ranges == ((1.25, 2.5),)
    assert record.balanced_strength_limit == 0.4


@pytest.mark.parametrize(
    "locked_ranges",
    ["not-json", "{}", "[[2, 1]]", '[[0, "1"]]', "[[NaN, 1]]"],
)
def test_rescue_prepare_rejects_invalid_locked_ranges_without_reserving_job(
    tmp_path: Path, locked_ranges: str
) -> None:
    client, manager = _client(tmp_path)

    with client:
        response = client.post(
            "/api/rescue/jobs",
            files={"video": ("clip.mp4", b"video", "video/mp4")},
            data={"locked_ranges": locked_ranges},
        )

    assert response.status_code == 422
    assert manager._jobs == {}


def test_rescue_default_manager_uses_the_task9_pipeline_boundary(
    tmp_path: Path,
) -> None:
    """Replacing the default pipeline boundary would change this failure path."""
    client = TestClient(create_app(_config(tmp_path)))

    with client:
        response = client.post(
            "/api/rescue/jobs",
            files={"video": ("invalid.mp4", b"not a media container", "video/mp4")},
            data={"strategy": "conservative"},
        )
        assert response.status_code == 202
        terminal = _wait(client, response.json()["job_id"], {"failed"})

    assert terminal["error"] is not None
    assert str(tmp_path) not in json.dumps(terminal)


def test_rescue_empty_upload_is_rejected_without_creating_a_job(tmp_path: Path) -> None:
    """Accepting an empty upload would create a job with no valid source."""
    client, manager = _client(tmp_path)
    with client:
        response = client.post(
            "/api/rescue/jobs",
            files={"video": ("empty.mp4", b"", "video/mp4")},
            data={"strategy": "conservative"},
        )

    assert response.status_code == 400
    assert not manager._jobs


def test_rescue_active_cancellation_calls_pipeline_and_reaches_cancelled(
    tmp_path: Path,
) -> None:
    """Removing cooperative pipeline.cancel support breaks active cancellation."""
    FakeRescuePipeline.reset()
    FakeRescuePipeline.block_execute = True
    client, _ = _client(tmp_path)
    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        response = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        assert response.status_code == 202
        assert FakeRescuePipeline.execute_started_event.wait(timeout=2)
        cancelled = client.delete(f"/api/rescue/jobs/{job_id}")
        terminal = _wait(client, job_id, {"cancelled"})

    assert cancelled.status_code == 200
    assert terminal["status"] == "cancelled"


def test_rescue_shared_cpu_limiter_caps_prepare_concurrency(tmp_path: Path) -> None:
    """Bypassing CpuJobLimiter would permit both fakes to prepare together."""
    FakeRescuePipeline.reset()
    FakeRescuePipeline.block_prepare = True
    client, _ = _client(tmp_path, cpu_concurrency=1)
    with client:
        first = _upload(client)["job_id"]
        assert FakeRescuePipeline.prepare_started.wait(timeout=2)
        second = _upload(client)["job_id"]
        time.sleep(0.05)
        second_state = client.get(f"/api/rescue/jobs/{second}").json()
        assert second_state["status"] == "queued"
        assert FakeRescuePipeline.max_active_prepares == 1
        FakeRescuePipeline.prepare_release.set()
        _wait(client, first, {"awaiting_confirmation"})
        _wait(client, second, {"awaiting_confirmation"})

    assert FakeRescuePipeline.max_active_prepares == 1


def test_rescue_confirmation_is_exact_single_use_and_artifacts_are_isolated(
    tmp_path: Path,
) -> None:
    """Dropping confirmation binding or artifact allowlists breaks this test."""
    FakeRescuePipeline.reset()
    client, manager = _client(tmp_path)

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        stale = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": "0" * 64,
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        private = client.get(
            f"/api/rescue/jobs/{job_id}/private-artifacts/source-00.mp4"
        )
        escaped = client.get(
            f"/api/rescue/jobs/{job_id}/artifacts/%252e%252e%252finput.mp4"
        )
        confirmed = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        duplicate = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        _wait(client, job_id, {"completed"})
        public = client.get(
            f"/api/rescue/jobs/{job_id}/artifacts/faithful-rescue.mp4",
            headers={"Range": "bytes=0-3"},
        )
        private_after = client.get(
            f"/api/rescue/jobs/{job_id}/private-artifacts/source-00.mp4"
        )
        with client.stream("GET", f"/api/rescue/jobs/{job_id}/events") as stream:
            event_text = "".join(stream.iter_text())
        state_text = (
            manager.require(job_id).directory / "rescue-web-job.json"
        ).read_text(encoding="utf-8")

    assert re.fullmatch(r"[0-9a-f]{32}", job_id)
    assert stale.status_code == 409
    assert private.status_code == 200 and private.content == b"private-preview"
    assert escaped.status_code in {400, 404, 409}
    assert confirmed.status_code == 202
    assert duplicate.status_code == 409
    assert FakeRescuePipeline.execute_started
    assert public.status_code == 206 and public.content == b"fait"
    assert private_after.status_code == 200
    assert private_after.content == b"private-preview"
    sequences = [
        int(line.removeprefix("id: "))
        for line in event_text.splitlines()
        if line.startswith("id: ")
    ]
    assert sequences == sorted(set(sequences))
    assert str(tmp_path) not in state_text


def test_terminal_rescue_serves_only_manifest_pinned_source_preview(
    tmp_path: Path,
) -> None:
    """Terminal comparison may retain source previews, never processed candidates."""
    FakeRescuePipeline.reset()
    client, manager = _client(tmp_path)
    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        record = manager.require(job_id)
        faithful = record.output_directory / "rescue-review-private" / "faithful-00.mp4"
        faithful.write_bytes(b"private-faithful")
        record.private_artifacts = (*record.private_artifacts, "faithful-00.mp4")
        record.private_manifest["faithful-00.mp4"] = manager._manifest_entry(
            faithful, "faithful-00.mp4"
        )
        manager.persist(job_id)
        response = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        assert response.status_code == 202
        _wait(client, job_id, {"completed"})

        source_response = client.get(
            f"/api/rescue/jobs/{job_id}/private-artifacts/source-00.mp4"
        )
        faithful_response = client.get(
            f"/api/rescue/jobs/{job_id}/private-artifacts/faithful-00.mp4"
        )

    assert source_response.status_code == 200
    assert source_response.content == b"private-preview"
    assert faithful_response.status_code == 409


def test_rescue_completed_bundle_uses_issued_plan_public_artifact_allowlist(
    tmp_path: Path,
) -> None:
    """Public documents are declared by the digest-bound plan, not report media."""
    FakeRescuePipeline.reset()
    client, _ = _client(tmp_path)
    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        confirmed = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        assert confirmed.status_code == 202
        _wait(client, job_id, {"completed"})
        report = client.get(f"/api/rescue/jobs/{job_id}/artifacts/report.html")
        technical = client.get(
            f"/api/rescue/jobs/{job_id}/artifacts/technical-report.json"
        )

    assert report.status_code == 200
    assert technical.status_code == 200


def test_rescue_recovery_rejects_forged_completed_state(tmp_path: Path) -> None:
    """A persisted terminal claim must include a valid terminal artifact manifest."""
    config = _config(tmp_path)
    root = config.job_root / "rescue" / ("b" * 32)
    root.mkdir(parents=True)
    (root / "snapshot.mp4").write_bytes(b"video")
    (root / "rescue-web-job.json").write_text(
        json.dumps(
            {
                "schema_version": "0.2",
                "job_id": "b" * 32,
                "strategy": "conservative",
                "symptoms": [],
                "locked_ranges": [],
                "status": "completed",
                "message": "Video Rescue completed",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:01+00:00",
                "upload_size_bytes": 5,
                "progress_percent": 100,
                "warnings": [],
                "error": None,
                "confirmation_submitted": True,
                "input_snapshot": {
                    "name": "snapshot.mp4",
                    "sha256": "0" * 64,
                    "size_bytes": 5,
                    "device": 1,
                    "inode": 1,
                },
                "private_artifacts": [],
                "public_artifacts": ["faithful-rescue.mp4"],
                "events": [
                    {
                        "sequence": 1,
                        "status": "completed",
                        "message": "Video Rescue completed",
                        "progress_percent": 100,
                        "created_at": "2026-01-01T00:00:01+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manager = RescueJobManager(config, pipeline_factory=FakeRescuePipeline)
    manager.shutdown()

    assert "b" * 32 not in manager._jobs


def test_rescue_snapshot_mutation_fails_closed_before_pipeline_prepare(
    tmp_path: Path,
) -> None:
    """Changing a captured upload must fail before the path-based pipeline uses it."""
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    manager.commit_input_snapshot(record.job_id, staging)
    with pytest.raises(PermissionError):
        record.input_path.write_bytes(b"changed")
    manager.shutdown()

    assert record.input_sha256 is not None


def test_rescue_pinned_artifact_rejects_swap_before_opening_new_content(
    tmp_path: Path,
) -> None:
    """A changed name cannot replace an already validated artifact descriptor."""
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    record.status = RescueJobStatus.COMPLETED
    record.public_artifacts = ("faithful-rescue.mp4",)
    public = record.output_directory / "rescue-output"
    public.mkdir(parents=True)
    target = public / "faithful-rescue.mp4"
    target.write_bytes(b"safe")
    record.public_manifest = {
        "faithful-rescue.mp4": manager._manifest_entry(target, "faithful-rescue.mp4")
    }
    pinned = manager.open_public_artifact(record.job_id, "faithful-rescue.mp4")
    replacement = public / "replacement.mp4"
    replacement.write_bytes(b"evil")
    try:
        replacement.replace(target)
    except OSError:
        os.close(pinned.descriptor)
        manager.shutdown()
        pytest.skip("local filesystem does not permit replacing an open artifact")
    try:
        assert os.read(pinned.descriptor, 4) == b"safe"
        with pytest.raises(FileNotFoundError):
            manager.open_public_artifact(record.job_id, "faithful-rescue.mp4")
    finally:
        os.close(pinned.descriptor)
        manager.shutdown()


def test_rescue_upload_limit_and_pipeline_error_are_sanitized(tmp_path: Path) -> None:
    """Exposing source paths or private pipeline diagnostics breaks this test."""
    FakeRescuePipeline.reject_prepare = True
    client, _ = _client(tmp_path, max_upload_bytes=4)

    with client:
        oversized = client.post(
            "/api/rescue/jobs",
            files={"video": ("clip.mp4", b"12345", "video/mp4")},
            data={"strategy": "conservative"},
        )
        created = client.post(
            "/api/rescue/jobs",
            files={"video": ("clip.mp4", b"1234", "video/mp4")},
            data={"strategy": "conservative"},
        ).json()
        terminal = _wait(client, created["job_id"], {"failed"})

    assert oversized.status_code == 413
    encoded = json.dumps(terminal)
    assert "private SECRET" not in encoded
    assert str(tmp_path) not in encoded


def test_rescue_rejects_encoded_and_absolute_artifact_escapes(tmp_path: Path) -> None:
    """Changing an allowlist miss into an artifact response breaks this test."""
    FakeRescuePipeline.reject_prepare = False
    client, _ = _client(tmp_path)

    with client:
        job_id = _upload(client)["job_id"]
        _wait(client, job_id, {"awaiting_confirmation"})
        attempts = [
            f"/api/rescue/jobs/{job_id}/artifacts/%2e%2e%2finput.mp4",
            f"/api/rescue/jobs/{job_id}/artifacts/%252e%252e%252finput.mp4",
            f"/api/rescue/jobs/{job_id}/artifacts/C:%5cWindows%5cwin.ini",
            f"/api/rescue/jobs/{job_id}/artifacts/%5c%5cserver%5cshare",
        ]
        responses = [client.get(path) for path in attempts]

    assert all(response.status_code in {400, 404} for response in responses)


def test_rescue_cancel_queued_job_is_terminal_and_restart_marks_interrupted_work(
    tmp_path: Path,
) -> None:
    """A queued cancellation or restart stuck state would break this test."""
    config = _config(tmp_path)
    manager = RescueJobManager(config, pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    record.input_path.write_bytes(b"video")
    manager.persist(record.job_id)

    cancelled = manager.cancel(record.job_id)
    restarted = RescueJobManager(config, pipeline_factory=FakeRescuePipeline)
    recovered = restarted.snapshot(record.job_id)
    manager.shutdown()
    restarted.shutdown()

    assert cancelled.status is RescueJobStatus.CANCELLED
    assert recovered.status is RescueJobStatus.CANCELLED


def test_rescue_public_resolver_rejects_hardlinked_artifact(tmp_path: Path) -> None:
    """Replacing an output with a hard link must not expose its other name."""
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    record.status = RescueJobStatus.COMPLETED
    record.public_artifacts = ("faithful-rescue.mp4",)
    public = record.output_directory / "rescue-output"
    public.mkdir(parents=True)
    sensitive = tmp_path / "private-source.txt"
    sensitive.write_text("private", encoding="utf-8")
    try:
        os.link(sensitive, public / "faithful-rescue.mp4")
    except OSError as exc:
        manager.shutdown()
        raise AssertionError(
            "hard-link capability is required for this local test"
        ) from exc

    try:
        manager.resolve_public_artifact(record.job_id, "faithful-rescue.mp4")
    except FileNotFoundError:
        rejected = True
    else:
        rejected = False
    manager.shutdown()

    assert rejected


def test_rescue_cleanup_deletes_only_expired_exact_job_root(tmp_path: Path) -> None:
    """A broad cleanup target would delete the sentinel sibling in this test."""
    manager = RescueJobManager(
        _config(tmp_path, job_ttl_seconds=1), pipeline_factory=FakeRescuePipeline
    )
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    record.status = RescueJobStatus.CANCELLED
    record.updated_at = datetime.now(UTC) - timedelta(seconds=2)
    manager.persist(record.job_id)
    sentinel = manager.job_root.parent / "rescue-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    removed = manager.cleanup_expired(now=datetime.now(UTC))
    manager.shutdown()

    assert removed == (record.job_id,)
    assert not record.directory.exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_rescue_ttl_and_shutdown_release_retained_core_sources(
    tmp_path: Path,
) -> None:
    """Removing Web ownership must close every fake core source descriptor."""
    FakeRescuePipeline.reset()
    ttl_manager = RescueJobManager(
        _config(tmp_path / "ttl", job_ttl_seconds=1),
        pipeline_factory=FakeRescuePipeline,
    )
    ttl_record, ttl_pipeline, ttl_descriptor = _awaiting_fake_job(ttl_manager)
    with ttl_record.lock:
        ttl_record.status = RescueJobStatus.CANCELLED
        ttl_record.updated_at = datetime.now(UTC) - timedelta(seconds=2)

    removed = ttl_manager.cleanup_expired(now=datetime.now(UTC))

    assert removed == (ttl_record.job_id,)
    assert ttl_record.pipeline is None
    assert ttl_pipeline.closed or ttl_pipeline.aborted
    with pytest.raises(OSError):
        os.fstat(ttl_descriptor)
    ttl_manager.shutdown()

    FakeRescuePipeline.reset()
    shutdown_manager = RescueJobManager(
        _config(tmp_path / "shutdown"), pipeline_factory=FakeRescuePipeline
    )
    shutdown_record, shutdown_pipeline, shutdown_descriptor = _awaiting_fake_job(
        shutdown_manager
    )

    shutdown_manager.shutdown()

    assert shutdown_record.pipeline is None
    assert shutdown_pipeline.closed or shutdown_pipeline.aborted
    with pytest.raises(OSError):
        os.fstat(shutdown_descriptor)


def test_rescue_failed_prepare_releases_retained_core_source(
    tmp_path: Path,
) -> None:
    """A preparation failure must close a source retained before the error."""
    FailAfterRetainingPipeline.reset()
    failed_manager = RescueJobManager(
        _config(tmp_path / "failed"), pipeline_factory=FailAfterRetainingPipeline
    )
    failed_record = failed_manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = failed_record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    failed_manager.commit_input_snapshot(failed_record.job_id, staging)
    failed_manager.submit_prepare(failed_record.job_id)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and not failed_record.snapshot().status.terminal:
        time.sleep(0.01)
    failed_pipeline = FailAfterRetainingPipeline.instances[-1]
    failed_descriptor = FailAfterRetainingPipeline.retained_descriptor

    assert failed_record.snapshot().status is RescueJobStatus.FAILED
    assert failed_record.pipeline is None
    assert failed_pipeline.closed
    assert failed_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(failed_descriptor)
    failed_manager.shutdown()


def test_rescue_terminal_delete_releases_retained_core_source(
    tmp_path: Path,
) -> None:
    """Deleting a terminal job must close its retained fake core source."""
    FakeRescuePipeline.reset()
    delete_manager = RescueJobManager(
        _config(tmp_path / "delete"), pipeline_factory=FakeRescuePipeline
    )
    delete_record, delete_pipeline, delete_descriptor = _awaiting_fake_job(
        delete_manager
    )
    with delete_record.lock:
        delete_record.status = RescueJobStatus.CANCELLED

    result = delete_manager.delete_or_cancel(delete_record.job_id)

    assert result is None
    assert delete_record.pipeline is None
    assert delete_pipeline.closed
    assert not delete_record.directory.exists()
    with pytest.raises(OSError):
        os.fstat(delete_descriptor)
    delete_manager.shutdown()


def test_rescue_ignores_corrupt_persisted_state(tmp_path: Path) -> None:
    """Corrupt JSON must never resurrect an executable local job."""
    config = _config(tmp_path)
    root = config.job_root / "rescue" / ("a" * 32)
    root.mkdir(parents=True)
    (root / "rescue-web-job.json").write_text("{not-json", encoding="utf-8")

    manager = RescueJobManager(config, pipeline_factory=FakeRescuePipeline)
    manager.shutdown()

    assert not manager._jobs


def test_rescue_sse_reconnect_is_ordered_and_history_is_bounded(tmp_path: Path) -> None:
    """Removing cursor filtering or the event cap breaks this test."""
    FakeRescuePipeline.reset()
    client, manager = _client(tmp_path)
    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        confirmed = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        assert confirmed.status_code == 202
        _wait(client, job_id, {"completed"})
        with client.stream("GET", f"/api/rescue/jobs/{job_id}/events") as stream:
            initial = "".join(stream.iter_text())
        with client.stream(
            "GET",
            f"/api/rescue/jobs/{job_id}/events",
            headers={"Last-Event-ID": "2"},
        ) as stream:
            resumed = "".join(stream.iter_text())
        record = manager.require(job_id)
        for _ in range(160):
            with record.lock:
                record._append_event()

    initial_ids = [
        int(line.removeprefix("id: "))
        for line in initial.splitlines()
        if line.startswith("id: ")
    ]
    resumed_ids = [
        int(line.removeprefix("id: "))
        for line in resumed.splitlines()
        if line.startswith("id: ")
    ]
    assert initial_ids == sorted(set(initial_ids))
    assert resumed_ids and all(item > 2 for item in resumed_ids)
    assert len(record.events) <= 128


def test_rescue_private_preview_is_loopback_only_and_cors_is_not_wildcard(
    tmp_path: Path,
) -> None:
    """A remote private-preview response or wildcard CORS breaks this test."""
    FakeRescuePipeline.reject_prepare = False
    config = _config(tmp_path)
    manager = RescueJobManager(config, pipeline_factory=FakeRescuePipeline)
    local = TestClient(create_app(config, rescue_manager=manager))
    with local:
        job_id = _upload(local)["job_id"]
        _wait(local, job_id, {"awaiting_confirmation"})
        local_preview = local.get(
            f"/api/rescue/jobs/{job_id}/private-artifacts/source-00.mp4"
        )
        cors = local.options(
            "/api/rescue/jobs",
            headers={"Origin": "http://127.0.0.1:3000"},
        )
    remote = TestClient(
        create_app(config, rescue_manager=manager), client=("10.0.0.8", 9000)
    )
    with remote:
        remote_preview = remote.get(
            f"/api/rescue/jobs/{job_id}/private-artifacts/source-00.mp4"
        )

    assert local_preview.status_code == 200
    assert remote_preview.status_code == 403
    assert cors.headers.get("access-control-allow-origin") != "*"


def test_rescue_public_resolver_rejects_external_symlink(tmp_path: Path) -> None:
    """Following a public artifact symlink would disclose the target file."""
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    record.status = RescueJobStatus.COMPLETED
    record.public_artifacts = ("faithful-rescue.mp4",)
    public = record.output_directory / "rescue-output"
    public.mkdir(parents=True)
    external = tmp_path / "outside.txt"
    external.write_text("outside", encoding="utf-8")
    try:
        (public / "faithful-rescue.mp4").symlink_to(external)
    except OSError as exc:
        manager.shutdown()
        pytest.skip(
            "Windows symlink capability is unavailable: "
            f"{getattr(exc, 'winerror', None)}"
        )

    try:
        manager.resolve_public_artifact(record.job_id, "faithful-rescue.mp4")
    except FileNotFoundError:
        rejected = True
    else:
        rejected = False
    manager.shutdown()

    assert rejected


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "tamper",
    [
        "stale_plan_digest",
        "noncanonical_public_contract",
        "forged_source_hash",
        "damage_plan_divergence",
        "forged_verification_hash",
        "locked_range_plan_divergence",
    ],
)
def test_rescue_restore_recomputes_nested_contract_and_artifact_bindings(
    tmp_path: Path, tamper: str
) -> None:
    """Internally plausible nested JSON must not resurrect a forged terminal job."""
    case_root = tmp_path / tamper
    config, state_path, payload = _completed_contract_state(case_root)
    job_id = payload["job_id"]

    if tamper == "stale_plan_digest":
        payload["plan"]["assessment_warnings"] = ["forged"]
    elif tamper == "noncanonical_public_contract":
        declared = ["faithful-rescue.mp4"]
        payload["plan"]["public_artifacts"] = declared
        digest = make_rescue_plan_digest(payload["plan"])
        payload["plan"]["plan_digest"] = digest
        payload["plan_digest"] = digest
        payload["verification"]["plan_digest"] = digest
        payload["issued_public_artifacts"] = declared
        payload["public_artifacts"] = declared
        payload["public_manifest"] = [
            item
            for item in payload["public_manifest"]
            if item["relative_path"] == "faithful-rescue.mp4"
        ]
    elif tamper == "forged_source_hash":
        forged_hash = "e" * 64
        payload["plan"]["input_hash"] = forged_hash
        payload["damage_map"]["input_hash"] = forged_hash
        digest = make_rescue_plan_digest(payload["plan"])
        payload["plan"]["plan_digest"] = digest
        payload["plan_digest"] = digest
        payload["verification"]["plan_digest"] = digest
    elif tamper == "damage_plan_divergence":
        source_hash = payload["input_snapshot"]["sha256"]
        interval = DamageInterval(
            id=make_damage_id(source_hash, "video:0", DamageKind.UNDECODABLE, 0.2, 0.3),
            stream_id="video:0",
            kind=DamageKind.UNDECODABLE,
            start_seconds=0.2,
            end_seconds=0.3,
        )
        payload["damage_map"]["intervals"] = [interval.model_dump(mode="json")]
    elif tamper == "locked_range_plan_divergence":
        payload["locked_ranges"] = [[0.25, 0.5]]
    else:
        payload["verification"]["artifacts"][0]["sha256"] = "f" * 64
        for item in payload["public_manifest"]:
            if item["relative_path"] == "faithful-rescue.mp4":
                item["sha256"] = "f" * 64

    state_path.write_text(json.dumps(payload), encoding="utf-8")

    assert job_id not in _restore_job_ids(config)


def test_rescue_restore_accepts_verified_faithful_fallback_from_balanced_plan(
    tmp_path: Path,
) -> None:
    """An unverified improved candidate is omitted without invalidating faithful."""
    config = _config(tmp_path)
    manager = RescueJobManager(
        config, pipeline_factory=FaithfulFallbackContractPipeline
    )
    client = TestClient(create_app(config, rescue_manager=manager))
    with client:
        created = client.post(
            "/api/rescue/jobs",
            files={"video": ("clip.mp4", b"video", "video/mp4")},
            data={"strategy": "balanced"},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        response = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": True,
                "accepted_action_ids": ["adjust-luma"],
                "accepted_trim_damage_ids": [],
            },
        )
        assert response.status_code == 202
        terminal = _wait(client, job_id, {"completed"})
        assert terminal["status"] == "completed"

    restored = RescueJobManager(
        config, pipeline_factory=FaithfulFallbackContractPipeline
    )
    try:
        record = restored.require(job_id)
        assert record.public_artifacts == rescue_public_artifacts()
        assert record.persisted_plan is not None
        assert record.persisted_plan.public_artifacts == rescue_public_artifacts(
            include_improved=True
        )
    finally:
        restored.shutdown()


def test_rescue_restore_fails_closed_for_legacy_v01_contract(tmp_path: Path) -> None:
    config, state_path, payload = _completed_contract_state(tmp_path)
    job_id = payload["job_id"]
    payload["plan"]["schema_version"] = "0.1"
    payload["damage_map"]["schema_version"] = "0.1"
    payload["verification"]["schema_version"] = "0.1"
    for artifact in payload["verification"]["artifacts"]:
        artifact.pop("artifact_role", None)
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    assert job_id not in _restore_job_ids(config)


def test_rescue_restore_requires_regeneration_without_preview_action_binding(
    tmp_path: Path,
) -> None:
    """A persisted pre-confirmation state without preview bindings is not executable."""
    config, state_path, payload = _completed_contract_state(tmp_path)
    awaiting_index = next(
        index
        for index, event in enumerate(payload["events"])
        if event["status"] == "awaiting_confirmation"
    )
    awaiting = payload["events"][awaiting_index]
    payload["events"] = payload["events"][: awaiting_index + 1]
    payload["status"] = "awaiting_confirmation"
    payload["message"] = awaiting["message"]
    payload["updated_at"] = awaiting["created_at"]
    payload["progress_percent"] = awaiting["progress_percent"]
    payload["confirmation_submitted"] = False
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    restored = RescueJobManager(config, pipeline_factory=ContractRescuePipeline)
    try:
        record = restored.require(payload["job_id"])
        assert record.status is RescueJobStatus.FAILED
        assert record.error == "Interrupted Video Rescue work requires a new job"
        assert record.preparation is None
    finally:
        restored.shutdown()


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "tamper",
    ["out_of_order", "duplicate", "illegal_transition", "unknown_field", "bad_start"],
)
def test_rescue_restore_rejects_forged_event_history(
    tmp_path: Path, tamper: str
) -> None:
    """Recovery accepts only manager-emitted event order, fields, and transitions."""
    config, state_path, payload = _completed_contract_state(tmp_path / tamper)
    job_id = payload["job_id"]
    events = payload["events"]
    assert len(events) >= 4
    if tamper == "out_of_order":
        events[1]["sequence"] = 99
    elif tamper == "duplicate":
        events[1]["sequence"] = events[0]["sequence"]
    elif tamper == "illegal_transition":
        events[1]["status"] = "processing"
    elif tamper == "unknown_field":
        events[1]["private_path"] = "C:/private/source.mp4"
    else:
        for event in events:
            event["sequence"] += 1
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    assert job_id not in _restore_job_ids(config)


def _prepared_private_job(
    tmp_path: Path,
) -> tuple[RescueJobManager, Any, Path]:
    FakeRescuePipeline.reset()
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    manager.commit_input_snapshot(record.job_id, staging)
    manager.submit_prepare(record.job_id)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if record.snapshot().status is RescueJobStatus.AWAITING_CONFIRMATION:
            break
        time.sleep(0.01)
    assert record.snapshot().status is RescueJobStatus.AWAITING_CONFIRMATION
    preview = record.output_directory / "rescue-review-private" / "source-00.mp4"
    return manager, record, preview


def test_rescue_private_preview_rejects_regular_file_replacement(
    tmp_path: Path,
) -> None:
    manager, record, preview = _prepared_private_job(tmp_path)
    replacement = preview.with_name("replacement.mp4")
    replacement.write_bytes(b"not-the-issued-preview")
    replacement.replace(preview)
    try:
        with pytest.raises(FileNotFoundError):
            manager.open_private_artifact(record.job_id, "source-00.mp4")
    finally:
        manager.shutdown()


def test_rescue_private_preview_rejects_hardlink_swap(tmp_path: Path) -> None:
    manager, record, preview = _prepared_private_job(tmp_path)
    external = tmp_path / "private-source.txt"
    external.write_bytes(b"private")
    preview.unlink()
    try:
        os.link(external, preview)
    except OSError as exc:
        manager.shutdown()
        pytest.skip(f"hard links are unavailable: {exc}")
    try:
        with pytest.raises(FileNotFoundError):
            manager.open_private_artifact(record.job_id, "source-00.mp4")
    finally:
        manager.shutdown()


def test_rescue_private_preview_rejects_symlink_or_reparse_swap(tmp_path: Path) -> None:
    manager, record, preview = _prepared_private_job(tmp_path)
    external = tmp_path / "private-source.txt"
    external.write_bytes(b"private")
    preview.unlink()
    try:
        preview.symlink_to(external)
    except OSError as exc:
        manager.shutdown()
        pytest.skip(f"symlinks are unavailable: {exc}")
    try:
        with pytest.raises((FileNotFoundError, OSError)):
            manager.open_private_artifact(record.job_id, "source-00.mp4")
    finally:
        manager.shutdown()


def test_rescue_private_manifest_identity_is_revalidated_on_restart(
    tmp_path: Path,
) -> None:
    manager, record, preview = _prepared_private_job(tmp_path)
    replacement = preview.with_name("replacement.mp4")
    replacement.write_bytes(b"replacement")
    replacement.replace(preview)
    restarted = RescueJobManager(manager.config, pipeline_factory=FakeRescuePipeline)
    try:
        assert record.job_id not in restarted._jobs
    finally:
        restarted.shutdown()
        manager.shutdown()


def test_rescue_private_route_never_exposes_input_snapshot(tmp_path: Path) -> None:
    FakeRescuePipeline.reset()
    client, manager = _client(tmp_path)
    with client:
        job_id = _upload(client)["job_id"]
        _wait(client, job_id, {"awaiting_confirmation"})
        snapshot_name = manager.require(job_id).input_path.name
        response = client.get(
            f"/api/rescue/jobs/{job_id}/private-artifacts/{snapshot_name}"
        )
    assert response.status_code == 404


def test_rescue_posix_open_without_no_follow_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(rescue_jobs_module, "_os_name", lambda: "posix")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(RescueJobStateError, match="No-follow"):
        rescue_jobs_module._secure_read_open(source)


def test_rescue_posix_pipeline_without_stable_proc_descriptor_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    manager.commit_input_snapshot(record.job_id, staging)
    monkeypatch.setattr(rescue_jobs_module, "_os_name", lambda: "posix")
    monkeypatch.setattr("videoscope.processes._os_name", lambda: "posix")
    monkeypatch.setattr(
        "videoscope.processes._stat_descriptor_path",
        lambda _path: (_ for _ in ()).throw(FileNotFoundError()),
    )
    try:
        with pytest.raises(RescueJobStateError, match="descriptor path"):
            manager._pipeline_source(record)
    finally:
        manager.shutdown()


def test_rescue_darwin_pipeline_uses_dev_fd_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches assuming Linux /proc descriptor paths exist on macOS."""
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    manager.commit_input_snapshot(record.job_id, staging)
    assert record.input_descriptor is not None
    monkeypatch.setattr(rescue_jobs_module, "_os_name", lambda: "posix")
    monkeypatch.setattr("videoscope.processes._os_name", lambda: "posix")
    monkeypatch.setattr("videoscope.processes._system_platform", lambda: "darwin")
    descriptor_metadata = os.fstat(record.input_descriptor)
    monkeypatch.setattr(
        "videoscope.processes._stat_descriptor_path", lambda _path: descriptor_metadata
    )
    try:
        assert manager._pipeline_source(record) == Path(
            f"/dev/fd/{record.input_descriptor}"
        )
    finally:
        manager.shutdown()


def test_rescue_snapshot_descriptor_is_forced_non_inheritable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an ambient child inheriting the private snapshot descriptor."""
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    secure_open = rescue_jobs_module._secure_read_open

    def inheritable_open(path: Path) -> int:
        descriptor = secure_open(path)
        os.set_inheritable(descriptor, True)
        return descriptor

    monkeypatch.setattr(rescue_jobs_module, "_secure_read_open", inheritable_open)
    try:
        manager.commit_input_snapshot(record.job_id, staging)
        assert record.input_descriptor is not None
        assert os.get_inheritable(record.input_descriptor) is False
    finally:
        manager.shutdown()


@pytest.mark.skipif(  # type: ignore[untyped-decorator]
    os.name != "nt", reason="Windows lifetime lock semantics"
)
def test_rescue_windows_snapshot_is_locked_for_prepare_and_execute_then_closed(
    tmp_path: Path,
) -> None:
    SnapshotLockProbePipeline.reset()
    client, manager = _client(tmp_path)
    manager.pipeline_factory = SnapshotLockProbePipeline
    with client:
        job_id = _upload(client)["job_id"]
        awaiting = _wait(client, job_id, {"awaiting_confirmation"})
        response = client.post(
            f"/api/rescue/jobs/{job_id}/confirm",
            json={
                "plan_digest": awaiting["plan_digest"],
                "publish_faithful": True,
                "publish_improved": False,
                "accepted_action_ids": ["faithful"],
                "accepted_trim_damage_ids": [],
            },
        )
        assert response.status_code == 202
        _wait(client, job_id, {"completed"})
        record = manager.require(job_id)
        assert record.input_descriptor is None
        record.input_path.write_bytes(b"closed-after-terminal")

    assert set(SnapshotLockProbePipeline.denied) == {
        ("prepare", "write"),
        ("prepare", "delete"),
        ("prepare", "replace"),
        ("execute", "write"),
        ("execute", "delete"),
        ("execute", "replace"),
    }


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "terminal", ["cancelled", "failed"]
)
def test_rescue_input_descriptor_closes_on_cancel_and_error(
    tmp_path: Path, terminal: str
) -> None:
    FakeRescuePipeline.reset()
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    manager.commit_input_snapshot(record.job_id, staging)
    if terminal == "cancelled":
        manager.cancel(record.job_id)
    else:
        FakeRescuePipeline.reject_prepare = True
        manager.submit_prepare(record.job_id)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not record.snapshot().status.terminal:
            time.sleep(0.01)
    try:
        assert record.snapshot().status.value == terminal
        assert record.input_descriptor is None
        record.input_path.write_bytes(b"closed-after-terminal")
    finally:
        manager.shutdown()


@pytest.mark.skipif(  # type: ignore[untyped-decorator]
    os.name != "nt", reason="Windows reparse lock semantics"
)
def test_rescue_windows_snapshot_rejects_reparse_replacement(tmp_path: Path) -> None:
    manager = RescueJobManager(_config(tmp_path), pipeline_factory=FakeRescuePipeline)
    record = manager.reserve_job(
        original_filename="clip.mp4", strategy=RescueStrategy.CONSERVATIVE
    )
    staging = record.directory / ".upload-test.mp4"
    staging.write_bytes(b"video")
    manager.commit_input_snapshot(record.job_id, staging)
    external = tmp_path / "external.mp4"
    external.write_bytes(b"external")
    reparse = record.directory / "replacement-link.mp4"
    try:
        reparse.symlink_to(external)
    except OSError as exc:
        manager.shutdown()
        pytest.skip(f"Windows symlink creation is unavailable: {exc}")
    try:
        with pytest.raises(OSError):
            reparse.replace(record.input_path)
        assert record.input_path.read_bytes() == b"video"
    finally:
        manager.cancel(record.job_id)
        manager.shutdown()
