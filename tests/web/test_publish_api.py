"""Confirmation-gated local Publish Ready Web API contracts."""

from __future__ import annotations

import importlib.util
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from videoscope.domain import VideoMetadata
from videoscope.resolve import (
    PublishArtifactError,
    PublishCancelledError,
    PublishInputError,
    PublishProfileId,
    PublishReadyStatus,
)
from videoscope.resolve.planner import build_publish_plan
from videoscope.resolve.serialization import write_publish_plan_json
from videoscope.web import models as web_models
from videoscope.web import publish_jobs
from videoscope.web.app import create_app
from videoscope.web.jobs import JobManager
from videoscope.web.models import PublishJobStatus, WebServerConfig


def _config(tmp_path: Path, **overrides: object) -> WebServerConfig:
    return WebServerConfig.model_validate(
        {
            "job_root": tmp_path / "应用 数据" / "jobs",
            "max_upload_bytes": 1024,
            "upload_chunk_bytes": 4096,
            "cpu_concurrency": 2,
            "heavy_ai_concurrency": 1,
            "job_ttl_seconds": 60,
            "cleanup_interval_seconds": 60,
            **overrides,
        }
    )


def test_publish_profiles_are_the_exact_versioned_local_catalog(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with TestClient(create_app(config)) as client:
        response = client.get("/api/publish/profiles")

    assert response.status_code == 200
    assert [profile["id"] for profile in response.json()] == [
        "compatible_mp4",
        "social_vertical_9_16",
        "social_horizontal_16_9",
    ]
    assert all(profile["version"] == "1.0.0" for profile in response.json())
    assert all("path" not in key for profile in response.json() for key in profile)


def test_publish_models_validate_confirmation_and_never_expose_paths() -> None:
    assert hasattr(web_models, "PublishConfirmation")
    assert hasattr(web_models, "PublishJobResponse")
    assert hasattr(web_models, "PublishJobStatus")
    confirmation_type = web_models.PublishConfirmation
    response_type = web_models.PublishJobResponse
    status_type = web_models.PublishJobStatus

    with pytest.raises(ValidationError):
        confirmation_type(plan_digest="A" * 64)

    response = response_type(
        job_id="a" * 32,
        status=status_type.AWAITING_CONFIRMATION,
        message="Review the exact local plan",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        updated_at=datetime(2026, 8, 1, tzinfo=UTC),
        upload_size_bytes=12,
        progress_percent=55,
        profile_id=PublishProfileId.COMPATIBLE_MP4,
        warnings=(),
        error=None,
        links={"self": "/api/publish/jobs/" + "a" * 32},
    )

    assert confirmation_type(plan_digest="0" * 64).plan_digest == "0" * 64
    assert response.status.terminal is False
    assert status_type.NEEDS_REVIEW.terminal is True
    serialized = response.model_dump_json()
    assert "C:\\" not in serialized
    assert "path" not in response.model_dump()


def test_publish_jobs_have_a_separate_manager_module() -> None:
    assert importlib.util.find_spec("videoscope.web.publish_jobs") is not None


class FakePublishPipeline:
    """Offline fake of the already-tested PublishReadyPipeline boundary."""

    instances: ClassVar[list[FakePublishPipeline]] = []
    outcome: ClassVar[PublishReadyStatus] = PublishReadyStatus.COMPLETED
    reject_prepare: ClassVar[bool] = False
    block_execute: ClassVar[bool] = False
    execute_started: ClassVar[threading.Event] = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.outcome = PublishReadyStatus.COMPLETED
        cls.reject_prepare = False
        cls.block_execute = False
        cls.execute_started = threading.Event()

    def __init__(
        self,
        config: Any,
        *,
        progress: Any,
        cancellation_callback: Any,
    ) -> None:
        self.config = config
        self.progress = progress
        self.cancellation_callback = cancellation_callback
        self.execute_calls = 0
        type(self).instances.append(self)

    def prepare(self, input_path: Path) -> object:
        self.progress("created")
        self.progress("inspecting")
        if type(self).reject_prepare:
            self.progress("failed")
            raise PublishInputError(f"ffprobe rejected {input_path}")
        input_bytes = input_path.read_bytes()
        metadata = VideoMetadata(
            filename=input_path.name,
            container_format="mp4",
            codec="h264",
            width=1920,
            height=1080,
            duration_seconds=2.0,
            average_frame_rate=30.0,
            estimated_frame_count=60,
            has_audio=True,
            file_size_bytes=len(input_bytes),
        )
        self.progress("planning")
        plan = build_publish_plan(
            metadata,
            sha256(input_bytes).hexdigest(),
            self.config.profile_id,
        )
        self.progress("awaiting_confirmation")
        return SimpleNamespace(plan=plan)

    def execute(self, preparation: Any, confirmed_plan_digest: str) -> object:
        assert confirmed_plan_digest == preparation.plan.plan_digest
        self.execute_calls += 1
        type(self).execute_started.set()
        self.progress("processing")
        if type(self).block_execute:
            while not self.cancellation_callback():
                time.sleep(0.005)
            self.progress("cancelled")
            raise PublishCancelledError("cancelled")
        self.progress("verifying")
        if type(self).outcome is PublishReadyStatus.FAILED:
            self.progress("failed")
            raise PublishArtifactError("verification failed")
        output = Path(self.config.output_directory)
        output.mkdir(parents=True)
        write_publish_plan_json(preparation.plan, output / "plan.json")
        (output / "publish-ready.mp4").write_bytes(b"publish-ready")
        (output / "technical-report.json").write_text("{}", encoding="utf-8")
        self.progress(type(self).outcome.value)
        return SimpleNamespace(status=type(self).outcome)


class ConcurrentPreparePipeline(FakePublishPipeline):
    """Hold two preparations to observe the configured CPU worker bound."""

    lock: ClassVar[threading.Lock] = threading.Lock()
    release: ClassVar[threading.Event] = threading.Event()
    two_started: ClassVar[threading.Event] = threading.Event()
    active: ClassVar[int] = 0
    maximum_active: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.release = threading.Event()
        cls.two_started = threading.Event()
        cls.active = 0
        cls.maximum_active = 0

    def prepare(self, input_path: Path) -> object:
        with type(self).lock:
            type(self).active += 1
            type(self).maximum_active = max(
                type(self).maximum_active,
                type(self).active,
            )
            if type(self).active == 2:
                type(self).two_started.set()
        try:
            assert type(self).release.wait(timeout=3)
            return super().prepare(input_path)
        finally:
            with type(self).lock:
                type(self).active -= 1


class SecondPrepareBlockingPipeline(FakePublishPipeline):
    """Occupy the only worker after the first plan is ready to confirm."""

    lock: ClassVar[threading.Lock] = threading.Lock()
    release: ClassVar[threading.Event] = threading.Event()
    second_started: ClassVar[threading.Event] = threading.Event()
    prepare_calls: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.release = threading.Event()
        cls.second_started = threading.Event()
        cls.prepare_calls = 0

    def prepare(self, input_path: Path) -> object:
        with type(self).lock:
            type(self).prepare_calls += 1
            call_number = type(self).prepare_calls
        if call_number == 2:
            type(self).second_started.set()
            assert type(self).release.wait(timeout=3)
        return super().prepare(input_path)


class MixedCpuTracker:
    """Observe mixed analysis and Publish Ready CPU work."""

    lock: ClassVar[threading.Lock] = threading.Lock()
    release: ClassVar[threading.Event] = threading.Event()
    first_started: ClassVar[threading.Event] = threading.Event()
    second_started: ClassVar[threading.Event] = threading.Event()
    active: ClassVar[int] = 0
    maximum_active: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        cls.release = threading.Event()
        cls.first_started = threading.Event()
        cls.second_started = threading.Event()
        cls.active = 0
        cls.maximum_active = 0

    @classmethod
    def hold_cpu_slot(cls) -> None:
        with cls.lock:
            cls.active += 1
            cls.maximum_active = max(cls.maximum_active, cls.active)
            if cls.active == 1:
                cls.first_started.set()
            elif cls.active == 2:
                cls.second_started.set()
        try:
            assert cls.release.wait(timeout=3)
        finally:
            with cls.lock:
                cls.active -= 1


class MixedAnalysisPipeline:
    """Minimal analysis fake that blocks only at the pipeline boundary."""

    def __init__(self, config: Any, **kwargs: Any) -> None:
        del kwargs
        self.config = config

    def run(self, input_path: Path, *, prompt: str | None = None) -> object:
        del input_path, prompt
        MixedCpuTracker.hold_cpu_slot()
        output_directory = Path(self.config.output_directory)
        output_directory.mkdir(parents=True, exist_ok=True)
        report_path = output_directory / "report.json"
        report_path.write_text("{}", encoding="utf-8")
        return SimpleNamespace(report_path=report_path)


class MixedPublishPipeline(FakePublishPipeline):
    """Publish fake sharing the mixed-work concurrency tracker."""

    def prepare(self, input_path: Path) -> object:
        MixedCpuTracker.hold_cpu_slot()
        return super().prepare(input_path)


class PreProgressBlockingPipeline(FakePublishPipeline):
    """Expose the cancellation window before processing progress is emitted."""

    cancellation_seen: ClassVar[threading.Event] = threading.Event()
    release: ClassVar[threading.Event] = threading.Event()

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.cancellation_seen = threading.Event()
        cls.release = threading.Event()

    def execute(self, preparation: Any, confirmed_plan_digest: str) -> object:
        assert confirmed_plan_digest == preparation.plan.plan_digest
        self.execute_calls += 1
        type(self).execute_started.set()
        while not self.cancellation_callback():
            time.sleep(0.005)
        type(self).cancellation_seen.set()
        assert type(self).release.wait(timeout=3)
        self.progress("cancelled")
        raise PublishCancelledError("cancelled")


class PostPrepareWindowPipeline(FakePublishPipeline):
    """Return a plan without publishing the adapter's final state first."""

    prepared: ClassVar[threading.Event] = threading.Event()

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.prepared = threading.Event()

    def prepare(self, input_path: Path) -> object:
        original_progress = self.progress
        self.progress = lambda value: (
            None if value == "awaiting_confirmation" else original_progress(value)
        )
        try:
            preparation = super().prepare(input_path)
        finally:
            self.progress = original_progress
        type(self).prepared.set()
        return preparation


class TrackingRLock:
    """RLock test double that exposes ownership without private runtime APIs."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: int | None = None
        self._depth = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            owner = threading.get_ident()
            if self._owner == owner:
                self._depth += 1
            else:
                self._owner = owner
                self._depth = 1
        return acquired

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    def owned_by_current_thread(self) -> bool:
        return self._owner == threading.get_ident()

    def __enter__(self) -> TrackingRLock:
        self.acquire()
        return self

    def __exit__(self, *args: object) -> None:
        del args
        self.release()


class PostPrepareCancellation:
    """Deterministically open the worker's post-prepare settlement window."""

    def __init__(self, record: Any) -> None:
        self._record = record
        self._event = threading.Event()
        self._intercepted = False
        self.after_prepare_check = threading.Event()
        self.cancel_requested = threading.Event()

    def is_set(self) -> bool:
        if PostPrepareWindowPipeline.prepared.is_set() and not self._intercepted:
            self._intercepted = True
            self.after_prepare_check.set()
            lock_is_owned = self._record.lock.owned_by_current_thread()
            if lock_is_owned:
                return False
            assert self._event.wait(timeout=2)
            deadline = time.monotonic() + 2
            while (
                self._record.message != "Cancellation requested"
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
            assert self._record.message == "Cancellation requested"
            return False
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()
        self.cancel_requested.set()


def _publish_client(
    tmp_path: Path,
    *,
    pipeline_factory: Any = FakePublishPipeline,
    **config_overrides: object,
) -> tuple[TestClient, Any, WebServerConfig]:
    config = _config(tmp_path, **config_overrides)
    manager = publish_jobs.PublishJobManager(
        config,
        pipeline_factory=pipeline_factory,
    )
    return (
        TestClient(create_app(config, publish_manager=manager)),
        manager,
        config,
    )


def _submit_publish(
    client: TestClient,
    *,
    payload: bytes = b"valid local video",
    filename: str = "测试 video.mp4",
    content_type: str = "video/mp4",
    profile_id: str = "compatible_mp4",
) -> Any:
    return client.post(
        "/api/publish/jobs",
        files={"video": (filename, payload, content_type)},
        data={"profile_id": profile_id},
    )


def _wait_for_status(
    client: TestClient,
    job_id: str,
    statuses: set[str],
    *,
    timeout: float = 3.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/publish/jobs/{job_id}")
        assert response.status_code == 200
        payload = cast(dict[str, Any], response.json())
        if payload["status"] in statuses:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"publish job did not reach one of {sorted(statuses)}")


def _wait_for_analysis_status(
    client: TestClient,
    job_id: str,
    statuses: set[str],
    *,
    timeout: float = 3.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = cast(dict[str, Any], response.json())
        if payload["status"] in statuses:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"analysis job did not reach one of {sorted(statuses)}")


def test_publish_job_requires_exact_confirmation_and_streams_ordered_events(
    tmp_path: Path,
) -> None:
    FakePublishPipeline.reset()
    client, manager, config = _publish_client(tmp_path)
    source = b"immutable source bytes"

    with client:
        submitted = _submit_publish(client, payload=source)
        assert submitted.status_code == 202
        job_id = submitted.json()["job_id"]
        awaiting = _wait_for_status(client, job_id, {"awaiting_confirmation"})
        plan_response = client.get(f"/api/publish/jobs/{job_id}/plan")
        digest = plan_response.json()["plan_digest"]
        mismatch = client.post(
            f"/api/publish/jobs/{job_id}/confirm",
            json={"plan_digest": "0" * 64},
        )
        assert mismatch.status_code == 409
        confirmed = client.post(
            f"/api/publish/jobs/{job_id}/confirm",
            json={"plan_digest": digest},
        )
        assert confirmed.status_code == 202
        duplicate = client.post(
            f"/api/publish/jobs/{job_id}/confirm",
            json={"plan_digest": digest},
        )
        terminal = _wait_for_status(client, job_id, {"completed"})
        with client.stream(
            "GET", f"/api/publish/jobs/{job_id}/events"
        ) as event_response:
            event_body = "".join(event_response.iter_text())
        artifact = client.get(f"/api/publish/jobs/{job_id}/artifacts/publish-ready.mp4")
        traversal = client.get(
            f"/api/publish/jobs/{job_id}/artifacts/%2e%2e%2finput.mp4"
        )

    assert plan_response.status_code == 200
    assert awaiting["profile_id"] == "compatible_mp4"
    assert awaiting["progress_percent"] < 100
    assert duplicate.status_code == 409
    assert FakePublishPipeline.instances[0].execute_calls == 1
    statuses = [
        json.loads(line.removeprefix("data: "))["status"]
        for line in event_body.splitlines()
        if line.startswith("data: ")
    ]
    assert list(dict.fromkeys(statuses)) == [
        "queued",
        "inspecting",
        "planning",
        "awaiting_confirmation",
        "processing",
        "verifying",
        "completed",
    ]
    assert terminal["progress_percent"] == 100
    assert str(config.job_root) not in json.dumps(terminal)
    assert artifact.content == b"publish-ready"
    assert traversal.status_code == 404
    assert manager.require(job_id).input_path.read_bytes() == source


def test_only_prepared_preview_is_available_before_confirmation(
    tmp_path: Path,
) -> None:
    FakePublishPipeline.reset()
    client, manager, _ = _publish_client(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"outside")

    with client:
        submitted = _submit_publish(client)
        job_id = submitted.json()["job_id"]
        _wait_for_status(client, job_id, {"awaiting_confirmation"})
        record = manager.require(job_id)
        workspace = record.directory / ".artifacts.staging-test"
        preview = workspace / "preview" / "publish-preview.mp4"
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"six-second-preview")
        assert record.preparation is not None
        record.preparation.preview_path = preview

        available = client.get(
            f"/api/publish/jobs/{job_id}/artifacts/preview/publish-preview.mp4"
        )
        hidden_plan = client.get(f"/api/publish/jobs/{job_id}/artifacts/plan.json")
        traversal = client.get(
            f"/api/publish/jobs/{job_id}/artifacts/preview/%2e%2e%2finput.mp4"
        )

        preview.unlink()
        try:
            preview.symlink_to(outside)
        except OSError:
            symlink_escape_status = 404
        else:
            symlink_escape_status = client.get(
                f"/api/publish/jobs/{job_id}/artifacts/preview/publish-preview.mp4"
            ).status_code

        cancelled = client.delete(f"/api/publish/jobs/{job_id}")
        cancelled_preview = client.get(
            f"/api/publish/jobs/{job_id}/artifacts/preview/publish-preview.mp4"
        )

    assert available.status_code == 200
    assert available.content == b"six-second-preview"
    assert hidden_plan.status_code == 409
    assert traversal.status_code in {404, 409}
    assert symlink_escape_status == 404
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled_preview.status_code == 409


def test_failed_publish_job_cannot_expose_prepared_preview(tmp_path: Path) -> None:
    FakePublishPipeline.reset()
    client, manager, _ = _publish_client(tmp_path)

    with client:
        submitted = _submit_publish(client)
        job_id = submitted.json()["job_id"]
        _wait_for_status(client, job_id, {"awaiting_confirmation"})
        record = manager.require(job_id)
        workspace = record.directory / ".artifacts.staging-test"
        preview = workspace / "preview" / "publish-preview.mp4"
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"six-second-preview")
        assert record.preparation is not None
        record.preparation.preview_path = preview
        record.finish(PublishJobStatus.FAILED, error="sanitized failure")

        response = client.get(
            f"/api/publish/jobs/{job_id}/artifacts/preview/publish-preview.mp4"
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "Publish artifacts are not available."}


def test_confirmation_is_monotonic_and_publicly_streamed_before_execution(
    tmp_path: Path,
) -> None:
    SecondPrepareBlockingPipeline.reset()
    client, _, _ = _publish_client(
        tmp_path,
        pipeline_factory=SecondPrepareBlockingPipeline,
        cpu_concurrency=1,
    )

    with client:
        first_job_id = _submit_publish(client).json()["job_id"]
        _wait_for_status(client, first_job_id, {"awaiting_confirmation"})
        plan = client.get(f"/api/publish/jobs/{first_job_id}/plan").json()

        second_job_id = _submit_publish(client).json()["job_id"]
        assert SecondPrepareBlockingPipeline.second_started.wait(timeout=2)

        confirmed = client.post(
            f"/api/publish/jobs/{first_job_id}/confirm",
            json={"plan_digest": plan["plan_digest"]},
        )
        immediate = client.get(f"/api/publish/jobs/{first_job_id}")
        duplicate = client.post(
            f"/api/publish/jobs/{first_job_id}/confirm",
            json={"plan_digest": plan["plan_digest"]},
        )
        execution_started_before_sse = (
            SecondPrepareBlockingPipeline.execute_started.is_set()
        )
        cancelled = client.delete(f"/api/publish/jobs/{first_job_id}")
        with client.stream(
            "GET", f"/api/publish/jobs/{first_job_id}/events"
        ) as event_response:
            event_body = "".join(event_response.iter_text())
        execution_started_after_sse = (
            SecondPrepareBlockingPipeline.execute_started.is_set()
        )

        SecondPrepareBlockingPipeline.release.set()
        _wait_for_status(client, second_job_id, {"awaiting_confirmation"})
        client.delete(f"/api/publish/jobs/{second_job_id}")

    assert confirmed.status_code == 202
    assert confirmed.json()["status"] == "processing"
    assert confirmed.json()["message"] == "Confirmation accepted; execution queued"
    assert immediate.json()["status"] == "processing"
    assert immediate.json()["message"] == "Confirmation accepted; execution queued"
    assert duplicate.status_code == 409
    assert cancelled.status_code == 200
    assert execution_started_before_sse is False
    assert execution_started_after_sse is False

    public_events = [
        json.loads(line.removeprefix("data: "))
        for line in event_body.splitlines()
        if line.startswith("data: ")
    ]
    accepted_event = next(
        event
        for event in public_events
        if event["message"] == "Confirmation accepted; execution queued"
    )
    assert accepted_event["status"] == "processing"
    assert accepted_event["sequence"] < public_events[-1]["sequence"]
    assert public_events[-1]["status"] == "cancelled"

    status_order = {
        "queued": 0,
        "inspecting": 1,
        "planning": 2,
        "awaiting_confirmation": 3,
        "processing": 4,
        "verifying": 5,
        "completed": 6,
        "needs_review": 6,
        "failed": 6,
        "cancelled": 6,
    }
    observed_order = [status_order[event["status"]] for event in public_events]
    assert observed_order == sorted(observed_order)


def test_publish_upload_limits_profile_validation_and_ffprobe_rejection(
    tmp_path: Path,
) -> None:
    FakePublishPipeline.reset()
    client, manager, config = _publish_client(tmp_path, max_upload_bytes=4)

    with client:
        too_large = _submit_publish(client, payload=b"12345")
        invalid_profile = _submit_publish(client, payload=b"ok", profile_id="other")

    assert too_large.status_code == 413
    assert invalid_profile.status_code == 422
    assert list(manager.job_root.iterdir()) == []

    FakePublishPipeline.reset()
    FakePublishPipeline.reject_prepare = True
    client, _, _ = _publish_client(tmp_path / "probe")
    with client:
        submitted = _submit_publish(
            client,
            filename="video.txt",
            content_type="text/plain",
        )
        terminal = _wait_for_status(client, submitted.json()["job_id"], {"failed"})

    assert len(terminal["warnings"]) == 2
    assert terminal["error"] == "ffprobe rejected <input>"
    assert str(config.job_root) not in terminal["error"]


def test_publish_cancel_before_confirmation_and_during_processing(
    tmp_path: Path,
) -> None:
    FakePublishPipeline.reset()
    client, manager, _ = _publish_client(tmp_path / "before")
    with client:
        submitted = _submit_publish(client)
        job_id = submitted.json()["job_id"]
        _wait_for_status(client, job_id, {"awaiting_confirmation"})
        cancelled = client.delete(f"/api/publish/jobs/{job_id}")
        terminal = _wait_for_status(client, job_id, {"cancelled"})
        deleted = client.delete(f"/api/publish/jobs/{job_id}")

    assert cancelled.status_code == 200
    assert terminal["status"] == "cancelled"
    assert FakePublishPipeline.instances[0].execute_calls == 0
    assert deleted.status_code == 204
    assert not manager.job_root.joinpath(job_id).exists()

    FakePublishPipeline.reset()
    FakePublishPipeline.block_execute = True
    client, _, _ = _publish_client(tmp_path / "during")
    with client:
        submitted = _submit_publish(client)
        job_id = submitted.json()["job_id"]
        _wait_for_status(client, job_id, {"awaiting_confirmation"})
        plan = client.get(f"/api/publish/jobs/{job_id}/plan").json()
        client.post(
            f"/api/publish/jobs/{job_id}/confirm",
            json={"plan_digest": plan["plan_digest"]},
        )
        assert FakePublishPipeline.execute_started.wait(timeout=2)
        client.delete(f"/api/publish/jobs/{job_id}")
        terminal = _wait_for_status(client, job_id, {"cancelled"})

    assert terminal["status"] == "cancelled"


def test_delete_wins_the_post_prepare_settlement_race(tmp_path: Path) -> None:
    PostPrepareWindowPipeline.reset()
    client, manager, _ = _publish_client(
        tmp_path,
        pipeline_factory=PostPrepareWindowPipeline,
    )

    with client:
        record = manager.reserve_job(
            original_filename="settlement-race.mp4",
            profile_id=PublishProfileId.COMPATIBLE_MP4,
        )
        record.input_path.write_bytes(b"valid local video")
        record.update_upload_size(record.input_path.stat().st_size)
        cast(Any, record).lock = TrackingRLock()
        cancellation = PostPrepareCancellation(record)
        cast(Any, record).cancellation = cancellation
        manager.submit_prepare(record.job_id)
        assert cancellation.after_prepare_check.wait(timeout=2)
        delete_response = client.delete(f"/api/publish/jobs/{record.job_id}")
        assert cancellation.cancel_requested.wait(timeout=2)
        terminal = _wait_for_status(
            client,
            record.job_id,
            {"awaiting_confirmation", "cancelled"},
        )

    assert delete_response.status_code == 200
    assert terminal["status"] == "cancelled"


def test_cancel_does_not_delete_a_running_pre_progress_workspace(
    tmp_path: Path,
) -> None:
    PreProgressBlockingPipeline.reset()
    client, manager, _ = _publish_client(
        tmp_path,
        pipeline_factory=PreProgressBlockingPipeline,
    )
    with client:
        submitted = _submit_publish(client)
        job_id = submitted.json()["job_id"]
        _wait_for_status(client, job_id, {"awaiting_confirmation"})
        plan = client.get(f"/api/publish/jobs/{job_id}/plan").json()
        client.post(
            f"/api/publish/jobs/{job_id}/confirm",
            json={"plan_digest": plan["plan_digest"]},
        )
        assert PreProgressBlockingPipeline.execute_started.wait(timeout=2)
        cancellation = client.delete(f"/api/publish/jobs/{job_id}")
        assert PreProgressBlockingPipeline.cancellation_seen.wait(timeout=2)
        directory_during_cancel = manager.require(job_id).directory.exists()
        PreProgressBlockingPipeline.release.set()
        terminal = _wait_for_status(client, job_id, {"cancelled"})

    assert cancellation.status_code == 200
    assert directory_during_cancel is True
    assert terminal["status"] == "cancelled"
    assert not manager.require(job_id).directory.exists()


def _assert_verification_outcome(
    tmp_path: Path,
    pipeline_status: PublishReadyStatus,
    expected_status: str,
    artifact_available: bool,
) -> None:
    FakePublishPipeline.reset()
    FakePublishPipeline.outcome = pipeline_status
    client, _, _ = _publish_client(tmp_path)
    with client:
        submitted = _submit_publish(client)
        job_id = submitted.json()["job_id"]
        _wait_for_status(client, job_id, {"awaiting_confirmation"})
        plan = client.get(f"/api/publish/jobs/{job_id}/plan").json()
        client.post(
            f"/api/publish/jobs/{job_id}/confirm",
            json={"plan_digest": plan["plan_digest"]},
        )
        terminal = _wait_for_status(client, job_id, {expected_status})
        artifact = client.get(f"/api/publish/jobs/{job_id}/artifacts/publish-ready.mp4")

    assert terminal["status"] == expected_status
    assert artifact.status_code == (200 if artifact_available else 409)


def test_needs_review_keeps_artifacts_without_claiming_completion(
    tmp_path: Path,
) -> None:
    _assert_verification_outcome(
        tmp_path,
        PublishReadyStatus.NEEDS_REVIEW,
        "needs_review",
        True,
    )


def test_failed_verification_never_claims_completion(tmp_path: Path) -> None:
    _assert_verification_outcome(
        tmp_path,
        PublishReadyStatus.FAILED,
        "failed",
        False,
    )


def test_publish_ttl_cleanup_and_analysis_api_regression(tmp_path: Path) -> None:
    FakePublishPipeline.reset()
    client, manager, _ = _publish_client(tmp_path)
    with client:
        analysis_health = client.get("/api/health")
        submitted = _submit_publish(client)
        job_id = submitted.json()["job_id"]
        _wait_for_status(client, job_id, {"awaiting_confirmation"})
        client.delete(f"/api/publish/jobs/{job_id}")
        terminal = _wait_for_status(client, job_id, {"cancelled"})
        record = manager.require(job_id)
        expired = manager.cleanup_expired(
            now=record.snapshot().updated_at + timedelta(seconds=61)
        )

    assert analysis_health.status_code == 200
    assert analysis_health.json()["service"] == "VideoScope local API"
    assert terminal["status"] == "cancelled"
    assert expired == (job_id,)


def test_publish_preparation_uses_configured_cpu_concurrency(tmp_path: Path) -> None:
    ConcurrentPreparePipeline.reset()
    client, _, _ = _publish_client(
        tmp_path,
        pipeline_factory=ConcurrentPreparePipeline,
        cpu_concurrency=2,
    )
    with client:
        first = _submit_publish(client).json()["job_id"]
        second = _submit_publish(client).json()["job_id"]
        assert ConcurrentPreparePipeline.two_started.wait(timeout=2)
        ConcurrentPreparePipeline.release.set()
        _wait_for_status(client, first, {"awaiting_confirmation"})
        _wait_for_status(client, second, {"awaiting_confirmation"})
        client.delete(f"/api/publish/jobs/{first}")
        client.delete(f"/api/publish/jobs/{second}")

    assert ConcurrentPreparePipeline.maximum_active == 2


def test_analysis_and_publish_share_one_configured_cpu_budget(
    tmp_path: Path,
) -> None:
    MixedCpuTracker.reset()
    MixedPublishPipeline.reset()
    config = _config(tmp_path, cpu_concurrency=1)
    analysis_manager = JobManager(
        config,
        pipeline_factory=MixedAnalysisPipeline,
    )
    publish_manager = publish_jobs.PublishJobManager(
        config,
        pipeline_factory=cast(Any, MixedPublishPipeline),
    )
    client = TestClient(
        create_app(
            config,
            manager=analysis_manager,
            publish_manager=publish_manager,
        )
    )

    with client:
        analysis = client.post(
            "/api/jobs",
            files={"video": ("analysis.mp4", b"analysis source", "video/mp4")},
        )
        assert analysis.status_code == 202
        analysis_job_id = analysis.json()["job_id"]
        assert MixedCpuTracker.first_started.wait(timeout=2)

        publish = _submit_publish(client)
        assert publish.status_code == 202
        publish_job_id = publish.json()["job_id"]
        second_started_while_first_active = MixedCpuTracker.second_started.wait(
            timeout=0.25
        )
        MixedCpuTracker.release.set()

        _wait_for_analysis_status(client, analysis_job_id, {"completed"})
        _wait_for_status(client, publish_job_id, {"awaiting_confirmation"})
        client.delete(f"/api/publish/jobs/{publish_job_id}")

    assert second_started_while_first_active is False
    assert MixedCpuTracker.maximum_active == 1
