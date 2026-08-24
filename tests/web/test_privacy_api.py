"""Offline API coverage for the local Safe Sharing workflow."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from fastapi.testclient import TestClient

import videoscope.web.privacy_jobs as privacy_jobs_module
from videoscope.privacy.errors import PrivacyInputError
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
    build_manual_audio_risk,
    build_manual_visual_risk,
)
from videoscope.privacy.models import (
    PrivacyEffectiveConfig,
    PrivacyJobOutcome,
    PrivacyReviewDecision,
    PrivacyRiskMap,
)
from videoscope.privacy.pipeline import (
    PrivacyPreparation,
    PrivacyReviewedResult,
    PrivacyScanResult,
    SafeSharingConfig,
)
from videoscope.privacy.planner import build_privacy_plan
from videoscope.privacy.profiles import get_share_audience_profile
from videoscope.web.app import create_app
from videoscope.web.models import PrivacyJobStatus, WebServerConfig
from videoscope.web.privacy_jobs import PrivacyJobManager


def _config(tmp_path: Path, **overrides: object) -> WebServerConfig:
    return WebServerConfig.model_validate(
        {
            "job_root": tmp_path / "应用 数据" / "jobs",
            "max_upload_bytes": 1024,
            "upload_chunk_bytes": 4096,
            "cpu_concurrency": 2,
            "job_ttl_seconds": 60,
            "cleanup_interval_seconds": 60,
            **overrides,
        }
    )


class FakePrivacyPipeline:
    """Small local fake of the already-tested SafeSharingPipeline boundary."""

    instances: ClassVar[list[FakePrivacyPipeline]] = []
    block_prepare: ClassVar[bool] = False
    block_confirm: ClassVar[bool] = False
    reject_scan: ClassVar[bool] = False
    outcome: ClassVar[PrivacyJobOutcome] = PrivacyJobOutcome.COMPLETED
    confirm_started: ClassVar[threading.Event] = threading.Event()
    prepare_started: ClassVar[threading.Event] = threading.Event()
    prepare_release: ClassVar[threading.Event] = threading.Event()
    preview_calls: ClassVar[int] = 0
    states: ClassVar[
        dict[
            str,
            tuple[
                PrivacyScanResult,
                PrivacyReviewedResult | None,
                PrivacyPreparation | None,
            ],
        ]
    ] = {}

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.block_prepare = False
        cls.block_confirm = False
        cls.reject_scan = False
        cls.outcome = PrivacyJobOutcome.COMPLETED
        cls.confirm_started = threading.Event()
        cls.prepare_started = threading.Event()
        cls.prepare_release = threading.Event()
        cls.preview_calls = 0
        cls.states = {}

    def __init__(
        self,
        output_directory: Path,
        *,
        cancellation: Any,
    ) -> None:
        self.output_directory = Path(output_directory)
        self.cancellation = cancellation
        self.scan_result: PrivacyScanResult | None = None
        self.reviewed: PrivacyReviewedResult | None = None
        self.preparation: PrivacyPreparation | None = None
        self.confirm_calls = 0
        type(self).instances.append(self)

    def scan(self, *, source: Path, config: SafeSharingConfig) -> PrivacyScanResult:
        if type(self).reject_scan:
            raise PrivacyInputError(
                f"ffprobe rejected {source}; private OCR: SECRET-ACCOUNT-1234"
            )
        payload = source.read_bytes()
        risk_map = PrivacyRiskMap(
            input_hash=sha256(payload).hexdigest(),
            profile=config.audience,
            duration_seconds=2.0,
            risks=(),
        )
        private = self.output_directory / "privacy-review-private"
        evidence = private / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "risk_01.png").write_bytes(b"private-review-frame")
        self.scan_result = PrivacyScanResult(scan_id="1" * 32, risk_map=risk_map)
        type(self).states[str(self.output_directory)] = (
            self.scan_result,
            None,
            None,
        )
        return self.scan_result

    def resume(self, *, source: Path, config: SafeSharingConfig) -> PrivacyScanResult:
        del source, config
        self.scan_result, self.reviewed, self.preparation = type(self).states[
            str(self.output_directory)
        ]
        return self.scan_result

    def review(
        self,
        scan_id: str,
        reviews: Sequence[PrivacyReviewDecision],
        *,
        manual_visual_regions: Sequence[ManualVisualRegionInput] = (),
        manual_audio_intervals: Sequence[ManualAudioIntervalInput] = (),
    ) -> PrivacyReviewedResult:
        assert self.scan_result is not None and scan_id == self.scan_result.scan_id
        risk_map = self.scan_result.risk_map
        manual_risks = tuple(
            build_manual_visual_risk(risk_map.input_hash, region)
            for region in manual_visual_regions
        ) + tuple(
            build_manual_audio_risk(risk_map.input_hash, interval)
            for interval in manual_audio_intervals
        )
        self.reviewed = PrivacyReviewedResult(
            review_id="2" * 32,
            scan_id=scan_id,
            reviews=tuple(reviews),
            manual_risks=manual_risks,
        )
        type(self).states[str(self.output_directory)] = (
            self.scan_result,
            self.reviewed,
            None,
        )
        return self.reviewed

    def prepare(self, review_id: str) -> PrivacyPreparation:
        assert self.scan_result is not None
        assert self.reviewed is not None and review_id == self.reviewed.review_id
        type(self).prepare_started.set()
        while type(self).block_prepare and not type(self).prepare_release.wait(0.005):
            pass
        risk_map = self.scan_result.risk_map.model_copy(
            update={
                "risks": self.scan_result.risk_map.risks + self.reviewed.manual_risks
            }
        )
        plan = build_privacy_plan(
            risk_map,
            self.reviewed.reviews,
            get_share_audience_profile(self.scan_result.risk_map.profile),
            PrivacyEffectiveConfig(),
        )
        self.preparation = PrivacyPreparation(
            preparation_id="3" * 32,
            review_id=review_id,
            plan=plan,
        )
        type(self).states[str(self.output_directory)] = (
            self.scan_result,
            self.reviewed,
            self.preparation,
        )
        return self.preparation

    def preview(self, preparation_id: str) -> Path:
        assert self.preparation is not None
        assert self.scan_result is not None
        assert preparation_id == self.preparation.preparation_id
        type(self).preview_calls += 1
        preview = (
            self.output_directory
            / "privacy-review-private"
            / "preview"
            / "privacy-preview.mp4"
        )
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"private-preview")
        self.preparation = self.preparation.model_copy(
            update={"preview_relative_path": "preview/privacy-preview.mp4"}
        )
        type(self).states[str(self.output_directory)] = (
            self.scan_result,
            self.reviewed,
            self.preparation,
        )
        return preview

    def current_review(self, scan_id: str) -> PrivacyReviewedResult | None:
        assert self.scan_result is not None and scan_id == self.scan_result.scan_id
        return self.reviewed

    def current_preparation(self, scan_id: str) -> PrivacyPreparation | None:
        assert self.scan_result is not None and scan_id == self.scan_result.scan_id
        return self.preparation

    def confirm(self, preparation_id: str, plan_digest: str) -> object:
        assert self.preparation is not None
        assert preparation_id == self.preparation.preparation_id
        assert plan_digest == self.preparation.plan.digest
        self.confirm_calls += 1
        type(self).confirm_started.set()
        while type(self).block_confirm and not self.cancellation():
            time.sleep(0.005)
        if self.cancellation():
            raise RuntimeError("cancelled")
        public = self.output_directory / "share-package"
        public.mkdir(parents=True)
        (public / "share-safe.mp4").write_bytes(b"share-safe")
        (public / "privacy-summary.json").write_text("{}", encoding="utf-8")
        return SimpleNamespace(status=type(self).outcome)


class _SettlingFuture:
    """Settle a worker transition exactly while cancellation arbitrates it."""

    def __init__(self, settle: Any) -> None:
        self._settle = settle

    def cancel(self) -> bool:
        self._settle()
        return False


def _client(
    tmp_path: Path,
    **overrides: object,
) -> tuple[TestClient, PrivacyJobManager, WebServerConfig]:
    config = _config(tmp_path, **overrides)
    manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    return TestClient(create_app(config, privacy_manager=manager)), manager, config


def _upload(client: TestClient, *, payload: bytes = b"video") -> dict[str, Any]:
    response = client.post(
        "/api/privacy/jobs",
        files={"video": ("含 空格.mp4", payload, "video/mp4")},
        data={"profile_id": "public"},
    )
    assert response.status_code == 202
    return cast(dict[str, Any], response.json())


def _wait(
    client: TestClient,
    job_id: str,
    expected: set[str],
    *,
    timeout: float = 3,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/privacy/jobs/{job_id}")
        assert response.status_code == 200
        payload = cast(dict[str, Any], response.json())
        if payload["status"] in expected:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"privacy job did not reach {sorted(expected)}")


def _winerror(code: int) -> PermissionError:
    error = PermissionError("injected Windows replace failure")
    setattr(error, "winerror", code)
    return error


def test_privacy_job_windows_replace_retries_transient_access_denial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "privacy-web-job.json.tmp"
    destination = tmp_path / "privacy-web-job.json"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    attempts = 0
    delays: list[float] = []

    def replace(observed_source: Path, observed_destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _winerror(5)
        os.replace(observed_source, observed_destination)

    privacy_jobs_module._retry_windows_replace(
        source,
        destination,
        replace=replace,
        sleep=delays.append,
    )

    assert attempts == 2
    assert delays == [0.01]
    assert destination.read_bytes() == b"new"
    assert not source.exists()


def test_privacy_job_windows_replace_surfaces_exhausted_access_denial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "privacy-web-job.json.tmp"
    destination = tmp_path / "privacy-web-job.json"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    attempts = 0
    delays: list[float] = []
    final_error = _winerror(5)

    def replace(_source: Path, _destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise final_error

    with pytest.raises(PermissionError) as caught:
        privacy_jobs_module._retry_windows_replace(
            source,
            destination,
            replace=replace,
            sleep=delays.append,
        )

    assert caught.value is final_error
    assert attempts == 6
    assert delays == [0.01, 0.02, 0.04, 0.08, 0.16]
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"


def test_privacy_job_windows_replace_does_not_retry_other_errors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "privacy-web-job.json.tmp"
    destination = tmp_path / "privacy-web-job.json"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    attempts = 0
    delays: list[float] = []
    final_error = _winerror(32)

    def replace(_source: Path, _destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise final_error

    with pytest.raises(PermissionError) as caught:
        privacy_jobs_module._retry_windows_replace(
            source,
            destination,
            replace=replace,
            sleep=delays.append,
        )

    assert caught.value is final_error
    assert attempts == 1
    assert delays == []
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"


def test_privacy_job_windows_replace_does_not_retry_after_source_disappears(
    tmp_path: Path,
) -> None:
    source = tmp_path / "privacy-web-job.json.tmp"
    destination = tmp_path / "privacy-web-job.json"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    attempts = 0
    delays: list[float] = []
    final_error = _winerror(5)

    def replace(observed_source: Path, _destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        observed_source.unlink()
        raise final_error

    with pytest.raises(PermissionError) as caught:
        privacy_jobs_module._retry_windows_replace(
            source,
            destination,
            replace=replace,
            sleep=delays.append,
        )

    assert caught.value is final_error
    assert attempts == 1
    assert delays == []
    assert not source.exists()
    assert destination.read_bytes() == b"old"


def test_privacy_prepare_recovers_from_transient_state_replace_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakePrivacyPipeline.reset()
    client, _, _ = _client(tmp_path)
    original_replace = os.replace
    state_attempts = 0

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        reviewed = client.put(
            f"/api/privacy/jobs/{job_id}/review",
            json={"reviews": []},
        )
        assert reviewed.status_code == 200

        def replace(source: Path, destination: Path) -> None:
            nonlocal state_attempts
            if Path(destination).name == "privacy-web-job.json":
                state_attempts += 1
                if state_attempts == 1:
                    raise _winerror(5)
            original_replace(source, destination)

        monkeypatch.setattr(os, "replace", replace)
        prepared = client.post(f"/api/privacy/jobs/{job_id}/prepare")

    assert prepared.status_code == 200
    assert state_attempts >= 2


def test_privacy_profiles_and_lifecycle_require_exact_confirmation(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    client, manager, config = _client(tmp_path)

    with client:
        profiles = client.get("/api/privacy/profiles")
        created = _upload(client)
        job_id = created["job_id"]
        awaiting_review = _wait(client, job_id, {"awaiting_review"})
        risk_map = client.get(f"/api/privacy/jobs/{job_id}/risk-map")
        reviewed = client.put(
            f"/api/privacy/jobs/{job_id}/review",
            json={"reviews": []},
        )
        prepared = client.post(f"/api/privacy/jobs/{job_id}/prepare")
        digest = prepared.json()["plan_digest"]
        mismatch = client.post(
            f"/api/privacy/jobs/{job_id}/confirm",
            json={"plan_digest": "0" * 64},
        )
        confirmed = client.post(
            f"/api/privacy/jobs/{job_id}/confirm",
            json={"plan_digest": digest},
        )
        duplicate = client.post(
            f"/api/privacy/jobs/{job_id}/confirm",
            json={"plan_digest": digest},
        )
        terminal = _wait(client, job_id, {"completed"})
        plan = client.get(f"/api/privacy/jobs/{job_id}/plan")
        public_video = client.get(
            f"/api/privacy/jobs/{job_id}/artifacts/share-safe.mp4"
        )
        hidden_private_state = client.get(
            f"/api/privacy/jobs/{job_id}/private-artifacts/pipeline-state.json"
        )
        with client.stream("GET", f"/api/privacy/jobs/{job_id}/events") as stream:
            event_text = "".join(stream.iter_text())
        with client.stream(
            "GET",
            f"/api/privacy/jobs/{job_id}/events",
            headers={"Last-Event-ID": "2"},
        ) as resumed_stream:
            resumed_event_text = "".join(resumed_stream.iter_text())
        invalid_cursor = client.get(
            f"/api/privacy/jobs/{job_id}/events",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        state_text = (
            manager.require(job_id).directory / "privacy-web-job.json"
        ).read_text(encoding="utf-8")

    assert profiles.status_code == 200
    assert [item["id"] for item in profiles.json()] == [
        "public",
        "work_client",
        "school",
        "family",
        "external_ai",
    ]
    assert awaiting_review["profile_id"] == "public"
    assert re.fullmatch(r"[0-9a-f]{32}", job_id)
    assert risk_map.status_code == 200
    assert risk_map.headers["cache-control"] == "no-store"
    assert reviewed.status_code == 200
    assert prepared.status_code == 200
    assert mismatch.status_code == 409
    assert confirmed.status_code == 202
    assert duplicate.status_code == 409
    assert FakePrivacyPipeline.instances[0].confirm_calls == 1
    assert plan.json()["digest"] == digest
    assert public_video.status_code == 200
    assert public_video.content == b"share-safe"
    assert hidden_private_state.status_code == 404
    assert str(config.job_root) not in json.dumps(terminal)
    assert str(config.job_root) not in state_text
    assert "SECRET-ACCOUNT-1234" not in state_text
    assert invalid_cursor.status_code == 400
    sequences = [
        int(line.removeprefix("id: "))
        for line in event_text.splitlines()
        if line.startswith("id: ")
    ]
    assert sequences == sorted(set(sequences))
    resumed_sequences = [
        int(line.removeprefix("id: "))
        for line in resumed_event_text.splitlines()
        if line.startswith("id: ")
    ]
    assert resumed_sequences and all(sequence > 2 for sequence in resumed_sequences)
    statuses = [
        json.loads(line.removeprefix("data: "))["status"]
        for line in event_text.splitlines()
        if line.startswith("data: ")
    ]
    assert list(dict.fromkeys(statuses)) == [
        "queued",
        "inspecting",
        "scanning",
        "awaiting_review",
        "planning",
        "previewing",
        "awaiting_confirmation",
        "processing",
        "verifying",
        "completed",
    ]


def test_privacy_review_accepts_manual_visual_and_audio_intervals(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    client, _, _ = _client(tmp_path)

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        reviewed = client.put(
            f"/api/privacy/jobs/{job_id}/review",
            json={
                "reviews": [],
                "manual_visual_regions": [
                    {
                        "start_seconds": 0.25,
                        "end_seconds": 1.25,
                        "box": {
                            "x_min": 0.1,
                            "y_min": 0.2,
                            "x_max": 0.4,
                            "y_max": 0.6,
                        },
                        "style": "pixelate",
                    }
                ],
                "manual_audio_intervals": [
                    {"start_seconds": 1.25, "end_seconds": 1.75, "style": "mute"}
                ],
            },
        )
        assert reviewed.status_code == 200

        prepared = client.post(f"/api/privacy/jobs/{job_id}/prepare")
        plan = client.get(f"/api/privacy/jobs/{job_id}/plan")

    assert prepared.status_code == 200
    assert [risk["risk_type"] for risk in plan.json()["risks"]] == [
        "manual_visual",
        "manual_audio",
    ]
    assert plan.json()["risks"][0]["box"] == {
        "x_min": 0.1,
        "y_min": 0.2,
        "x_max": 0.4,
        "y_max": 0.6,
    }


def test_public_and_private_artifact_routes_are_strictly_isolated(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    client, _, _ = _client(tmp_path)

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        private = client.get(
            f"/api/privacy/jobs/{job_id}/private-artifacts/evidence/risk_01.png"
        )
        public_escape = client.get(
            f"/api/privacy/jobs/{job_id}/artifacts/%2e%2e%2fprivacy-review-private%2fevidence%2frisk_01.png"
        )
        public_before_confirm = client.get(
            f"/api/privacy/jobs/{job_id}/artifacts/share-safe.mp4"
        )
        private_escape = client.get(
            f"/api/privacy/jobs/{job_id}/private-artifacts/%2e%2e%2finput.mp4"
        )

    assert private.status_code == 200
    assert private.content == b"private-review-frame"
    assert private.headers["cache-control"] == "no-store"
    assert public_escape.status_code in {400, 404}
    assert public_before_confirm.status_code == 409
    assert private_escape.status_code in {400, 404}


def test_upload_limit_validation_and_errors_do_not_leak_private_details(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    client, _, config = _client(tmp_path, max_upload_bytes=4)

    with client:
        oversized = client.post(
            "/api/privacy/jobs",
            files={"video": ("clip.mp4", b"12345", "video/mp4")},
            data={"profile_id": "public"},
        )
        invalid_profile = client.post(
            "/api/privacy/jobs",
            files={"video": ("clip.mp4", b"1234", "video/mp4")},
            data={"profile_id": "not_a_profile"},
        )
        FakePrivacyPipeline.reject_scan = True
        failed = _upload(client, payload=b"1234")
        failure = _wait(client, failed["job_id"], {"failed"})

    encoded = json.dumps(failure)
    assert oversized.status_code == 413
    assert invalid_profile.status_code == 422
    assert "SECRET-ACCOUNT-1234" not in encoded
    assert str(config.job_root) not in encoded
    assert "含 空格" not in encoded


def test_cancelled_privacy_job_stops_confirmation_and_hides_artifacts(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    FakePrivacyPipeline.block_confirm = True
    client, _, _ = _client(tmp_path)

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        prepared = client.post(f"/api/privacy/jobs/{job_id}/prepare").json()
        client.post(
            f"/api/privacy/jobs/{job_id}/confirm",
            json={"plan_digest": prepared["plan_digest"]},
        )
        assert FakePrivacyPipeline.confirm_started.wait(timeout=2)
        cancelled = client.delete(f"/api/privacy/jobs/{job_id}")
        terminal = _wait(client, job_id, {"cancelled"})
        artifact = client.get(f"/api/privacy/jobs/{job_id}/artifacts/share-safe.mp4")

    assert cancelled.status_code == 200
    assert terminal["status"] == "cancelled"
    assert artifact.status_code == 409


def test_scan_completion_racing_cancel_has_one_terminal_event_and_never_sticks(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    manager = PrivacyJobManager(
        _config(tmp_path),
        pipeline_factory=FakePrivacyPipeline,
    )
    record = manager.reserve_job(
        original_filename="scan-race.mp4",
        profile_id="public",
    )
    manager._update(record, PrivacyJobStatus.SCANNING)
    with record.lock:
        record.future = cast(
            Any,
            _SettlingFuture(
                lambda: manager._update(
                    record,
                    PrivacyJobStatus.AWAITING_REVIEW,
                )
            ),
        )

    try:
        cancelled = manager.cancel(record.job_id)
        terminal_events = [event for event in record.events if event.status.terminal]
    finally:
        manager.shutdown()

    assert cancelled.status is PrivacyJobStatus.CANCELLED
    assert [event.status for event in terminal_events] == [PrivacyJobStatus.CANCELLED]


def test_confirm_completion_racing_cancel_emits_only_one_terminal_event(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    manager = PrivacyJobManager(
        _config(tmp_path),
        pipeline_factory=FakePrivacyPipeline,
    )
    record = manager.reserve_job(
        original_filename="confirm-race.mp4",
        profile_id="public",
    )
    manager._update(record, PrivacyJobStatus.PROCESSING)
    manager._update(record, PrivacyJobStatus.VERIFYING)
    with record.lock:
        record.future = cast(
            Any,
            _SettlingFuture(
                lambda: manager._finish(
                    record,
                    PrivacyJobStatus.COMPLETED,
                )
            ),
        )

    try:
        cancelled = manager.cancel(record.job_id)
        terminal_events = [event for event in record.events if event.status.terminal]
    finally:
        manager.shutdown()

    assert cancelled.status is PrivacyJobStatus.CANCELLED
    assert [event.status for event in terminal_events] == [PrivacyJobStatus.CANCELLED]


def test_blocked_prepare_keeps_privacy_status_route_serviceable(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    FakePrivacyPipeline.block_prepare = True
    client, _, _ = _client(tmp_path)
    prepare_responses: list[Any] = []
    status_responses: list[Any] = []
    status_done = threading.Event()

    def request_prepare(job_id: str) -> None:
        prepare_responses.append(client.post(f"/api/privacy/jobs/{job_id}/prepare"))

    def request_status(job_id: str) -> None:
        status_responses.append(client.get(f"/api/privacy/jobs/{job_id}"))
        status_done.set()

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        prepare_thread = threading.Thread(target=request_prepare, args=(job_id,))
        prepare_thread.start()
        assert FakePrivacyPipeline.prepare_started.wait(timeout=2)
        status_thread = threading.Thread(target=request_status, args=(job_id,))
        status_thread.start()
        remained_serviceable = status_done.wait(timeout=1)
        FakePrivacyPipeline.prepare_release.set()
        prepare_thread.join(timeout=2)
        status_thread.join(timeout=2)

    assert remained_serviceable is True
    assert not prepare_thread.is_alive()
    assert not status_thread.is_alive()
    assert prepare_responses[0].status_code == 200
    assert status_responses[0].status_code == 200
    assert status_responses[0].json()["status"] == "planning"


def test_cancel_during_blocked_prepare_returns_before_release_and_skips_preview(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    FakePrivacyPipeline.block_prepare = True
    client, _, _ = _client(tmp_path)
    prepare_responses: list[Any] = []
    cancel_responses: list[Any] = []
    cancel_done = threading.Event()

    def request_prepare(job_id: str) -> None:
        prepare_responses.append(client.post(f"/api/privacy/jobs/{job_id}/prepare"))

    def request_cancel(job_id: str) -> None:
        cancel_responses.append(client.delete(f"/api/privacy/jobs/{job_id}"))
        cancel_done.set()

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        prepare_thread = threading.Thread(target=request_prepare, args=(job_id,))
        prepare_thread.start()
        assert FakePrivacyPipeline.prepare_started.wait(timeout=2)
        cancel_thread = threading.Thread(target=request_cancel, args=(job_id,))
        cancel_thread.start()
        cancel_was_serviceable = cancel_done.wait(timeout=1)
        FakePrivacyPipeline.prepare_release.set()
        prepare_thread.join(timeout=2)
        cancel_thread.join(timeout=2)
        terminal = _wait(client, job_id, {"cancelled"})

    assert cancel_was_serviceable is True
    assert not prepare_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert cancel_responses[0].status_code == 200
    assert prepare_responses[0].status_code == 409
    assert terminal["status"] == "cancelled"
    assert FakePrivacyPipeline.preview_calls == 0


def test_restart_recovers_awaiting_review_job_without_rescanning(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    config = _config(tmp_path)
    first_manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    first_client = TestClient(create_app(config, privacy_manager=first_manager))

    with first_client:
        created = _upload(first_client)
        job_id = created["job_id"]
        _wait(first_client, job_id, {"awaiting_review"})
    first_count = len(FakePrivacyPipeline.instances)

    second_manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    with TestClient(
        create_app(config, privacy_manager=second_manager)
    ) as second_client:
        restored = second_client.get(f"/api/privacy/jobs/{job_id}")

    assert restored.status_code == 200
    assert restored.json()["status"] == "awaiting_review"
    assert len(FakePrivacyPipeline.instances) == first_count + 1


def test_restart_recovers_exact_awaiting_confirmation_plan(tmp_path: Path) -> None:
    FakePrivacyPipeline.reset()
    config = _config(tmp_path)
    manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    client = TestClient(create_app(config, privacy_manager=manager))

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        prepared = client.post(f"/api/privacy/jobs/{job_id}/prepare").json()
        digest = prepared["plan_digest"]

    restored_manager = PrivacyJobManager(
        config,
        pipeline_factory=FakePrivacyPipeline,
    )
    with TestClient(
        create_app(config, privacy_manager=restored_manager)
    ) as restored_client:
        restored = restored_client.get(f"/api/privacy/jobs/{job_id}")
        plan = restored_client.get(f"/api/privacy/jobs/{job_id}/plan")

    assert restored.status_code == 200
    assert restored.json()["status"] == "awaiting_confirmation"
    assert restored.json()["plan_digest"] == digest
    assert plan.json()["digest"] == digest


def test_restart_fails_closed_after_prepare_before_preview(tmp_path: Path) -> None:
    FakePrivacyPipeline.reset()
    config = _config(tmp_path)
    manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    client = TestClient(create_app(config, privacy_manager=manager))

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        client.post(f"/api/privacy/jobs/{job_id}/prepare")
        record = manager.require(job_id)
        state_path = record.directory / "privacy-web-job.json"
        preview_path = (
            record.output_directory
            / "privacy-review-private"
            / "preview"
            / "privacy-preview.mp4"
        )

    preview_path.unlink()
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "planning",
            "message": "Interrupted after preparing the plan",
            "progress_percent": 55,
        }
    )
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    restored_manager = PrivacyJobManager(
        config,
        pipeline_factory=FakePrivacyPipeline,
    )
    with TestClient(
        create_app(config, privacy_manager=restored_manager)
    ) as restored_client:
        restored = restored_client.get(f"/api/privacy/jobs/{job_id}")

    assert restored.status_code == 200
    assert restored.json()["status"] == "failed"
    assert "new job" in restored.json()["error"]


def test_restart_fails_closed_when_preview_was_interrupted(tmp_path: Path) -> None:
    FakePrivacyPipeline.reset()
    config = _config(tmp_path)
    manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    client = TestClient(create_app(config, privacy_manager=manager))

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        client.post(f"/api/privacy/jobs/{job_id}/prepare")
        record = manager.require(job_id)
        state_path = record.directory / "privacy-web-job.json"
        preview_path = (
            record.output_directory
            / "privacy-review-private"
            / "preview"
            / "privacy-preview.mp4"
        )

    preview_path.write_bytes(b"interrupted-preview")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "previewing",
            "message": "Interrupted while rendering the preview",
            "progress_percent": 65,
        }
    )
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    restored_manager = PrivacyJobManager(
        config,
        pipeline_factory=FakePrivacyPipeline,
    )
    with TestClient(
        create_app(config, privacy_manager=restored_manager)
    ) as restored_client:
        restored = restored_client.get(f"/api/privacy/jobs/{job_id}")

    assert restored.status_code == 200
    assert restored.json()["status"] == "failed"
    assert "new job" in restored.json()["error"]


def test_restart_fails_closed_when_awaiting_confirmation_preview_is_missing(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    config = _config(tmp_path)
    manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    client = TestClient(create_app(config, privacy_manager=manager))

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        client.post(f"/api/privacy/jobs/{job_id}/prepare")
        record = manager.require(job_id)
        preview_path = (
            record.output_directory
            / "privacy-review-private"
            / "preview"
            / "privacy-preview.mp4"
        )

    preview_path.unlink()
    restored_manager = PrivacyJobManager(
        config,
        pipeline_factory=FakePrivacyPipeline,
    )
    with TestClient(
        create_app(config, privacy_manager=restored_manager)
    ) as restored_client:
        restored = restored_client.get(f"/api/privacy/jobs/{job_id}")

    assert restored.status_code == 200
    assert restored.json()["status"] == "failed"
    assert "new job" in restored.json()["error"]


def test_restart_fails_closed_inflight_confirmation_without_resubmitting(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    config = _config(tmp_path)
    manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    client = TestClient(create_app(config, privacy_manager=manager))

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        client.post(f"/api/privacy/jobs/{job_id}/prepare")
        record = manager.require(job_id)
        state_path = record.directory / "privacy-web-job.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "processing",
            "message": "Interrupted during processing",
            "progress_percent": 75,
            "execution_submitted": True,
        }
    )
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    instance_count = len(FakePrivacyPipeline.instances)

    restored_manager = PrivacyJobManager(
        config,
        pipeline_factory=FakePrivacyPipeline,
    )
    with TestClient(
        create_app(config, privacy_manager=restored_manager)
    ) as restored_client:
        restored = restored_client.get(f"/api/privacy/jobs/{job_id}")

    assert restored.status_code == 200
    assert restored.json()["status"] == "failed"
    assert "new job" in restored.json()["error"]
    assert len(FakePrivacyPipeline.instances) == instance_count
    assert all(
        instance.confirm_calls == 0 for instance in FakePrivacyPipeline.instances
    )


def test_restart_fails_closed_ambiguous_planning_without_persisted_plan(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    config = _config(tmp_path)
    manager = PrivacyJobManager(config, pipeline_factory=FakePrivacyPipeline)
    client = TestClient(create_app(config, privacy_manager=manager))

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        state_path = manager.require(job_id).directory / "privacy-web-job.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "planning",
            "message": "Interrupted during planning",
            "progress_percent": 55,
        }
    )
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    restored_manager = PrivacyJobManager(
        config,
        pipeline_factory=FakePrivacyPipeline,
    )
    with TestClient(
        create_app(config, privacy_manager=restored_manager)
    ) as restored_client:
        restored = restored_client.get(f"/api/privacy/jobs/{job_id}")

    assert restored.status_code == 200
    assert restored.json()["status"] == "failed"
    assert "new job" in restored.json()["error"]


def _assert_noncompleted_job_has_no_public_artifact(
    tmp_path: Path,
    outcome: PrivacyJobOutcome,
    terminal_status: str,
) -> None:
    FakePrivacyPipeline.reset()
    FakePrivacyPipeline.outcome = outcome
    client, _, _ = _client(tmp_path)

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        client.put(f"/api/privacy/jobs/{job_id}/review", json={"reviews": []})
        prepared = client.post(f"/api/privacy/jobs/{job_id}/prepare").json()
        client.post(
            f"/api/privacy/jobs/{job_id}/confirm",
            json={"plan_digest": prepared["plan_digest"]},
        )
        _wait(client, job_id, {terminal_status})
        artifact = client.get(f"/api/privacy/jobs/{job_id}/artifacts/share-safe.mp4")

    assert artifact.status_code == 409


def test_needs_review_job_does_not_authorize_public_artifact_download(
    tmp_path: Path,
) -> None:
    _assert_noncompleted_job_has_no_public_artifact(
        tmp_path,
        PrivacyJobOutcome.NEEDS_REVIEW,
        "needs_review",
    )


def test_partial_job_does_not_authorize_public_artifact_download(
    tmp_path: Path,
) -> None:
    _assert_noncompleted_job_has_no_public_artifact(
        tmp_path,
        PrivacyJobOutcome.PARTIAL,
        "partial",
    )


def test_failed_job_does_not_authorize_public_artifact_download(
    tmp_path: Path,
) -> None:
    _assert_noncompleted_job_has_no_public_artifact(
        tmp_path,
        PrivacyJobOutcome.FAILED,
        "failed",
    )


def test_terminal_privacy_job_is_removed_after_retention_deadline(
    tmp_path: Path,
) -> None:
    FakePrivacyPipeline.reset()
    client, manager, _ = _client(tmp_path, job_ttl_seconds=1)

    with client:
        created = _upload(client)
        job_id = created["job_id"]
        _wait(client, job_id, {"awaiting_review"})
        cancelled = client.delete(f"/api/privacy/jobs/{job_id}")
        assert cancelled.json()["status"] == "cancelled"
        record = manager.require(job_id)
        with record.lock:
            record.updated_at = datetime.now(UTC) - timedelta(seconds=2)
        manager.persist(job_id)

        removed = manager.cleanup_expired(now=datetime.now(UTC))

    assert removed == (job_id,)
    assert not record.directory.exists()
