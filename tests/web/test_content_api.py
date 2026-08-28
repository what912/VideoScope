"""Local Web API acceptance for useful-content review jobs."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from fastapi.testclient import TestClient

import videoscope.web.content_jobs as content_jobs_module
from videoscope.content import (
    ContentCancelledError,
    ContentConfirmation,
    ContentFeatureBundle,
    ContentInputError,
    ContentJoinPreview,
    ContentPipelineConfig,
    ContentPreparation,
    ContentProviderExecution,
    ContentProviderStatus,
    ContentResult,
    ContentReview,
    ContentStatus,
    build_content_actions,
    build_content_map,
    build_content_plan,
    build_storyboard,
    revise_storyboard,
)
from videoscope.domain import VideoMetadata
from videoscope.intelligence import (
    AdvancedAIPreparation,
    ContentIntelligenceRequest,
    FakeASRProvider,
    FakeContentIntelligenceProvider,
    normalize_asr_transcript,
    run_content_intelligence,
)
from videoscope.scenes import VideoScene
from videoscope.video.hashing import compute_file_sha256
from videoscope.web.app import create_app
from videoscope.web.content_jobs import ContentJobManager
from videoscope.web.models import ContentJobStatus, WebServerConfig


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


class FakeContentPipeline:
    instances: ClassVar[list[FakeContentPipeline]] = []
    block_execute: ClassVar[bool] = False
    execute_started: ClassVar[threading.Event] = threading.Event()

    @classmethod
    def reset(cls) -> None:
        cls.instances = []
        cls.block_execute = False
        cls.execute_started = threading.Event()

    def __init__(
        self,
        config: ContentPipelineConfig,
        *,
        progress: Any,
    ) -> None:
        self.config = config
        self.progress = progress
        self.cancelled = False
        type(self).instances.append(self)

    def prepare(self, input_path: Path) -> ContentPreparation:
        self.progress(ContentStatus.PROBING)
        if input_path.read_bytes() == b"invalid":
            raise ContentInputError(f"invalid source at {input_path}")
        digest = compute_file_sha256(input_path)
        metadata = VideoMetadata(
            filename=input_path.name,
            container_format="mp4",
            codec="h264",
            width=320,
            height=180,
            duration_seconds=10,
            average_frame_rate=10,
            estimated_frame_count=100,
            has_audio=False,
            file_size_bytes=input_path.stat().st_size,
            raw_probe={},
        )
        bundle = ContentFeatureBundle(
            metadata=metadata,
            scenes=(
                VideoScene(
                    scene_index=0,
                    start_seconds=0,
                    end_seconds=10,
                    duration_seconds=10,
                    representative_timestamp=5,
                ),
            ),
            frame_samples=(),
            frame_workspace=self.config.output_directory / "frames",
            observations=(),
            executions=(
                ContentProviderExecution(
                    provider_id="metadata",
                    provider_version="1",
                    status=ContentProviderStatus.OK,
                ),
            ),
            warnings=(),
        )
        self.progress(ContentStatus.MAPPING)
        content_map = build_content_map(
            bundle,
            input_hash=digest,
            effective_config=self.config.content,
            user_ranges=self.config.user_ranges,
        )
        self.progress(ContentStatus.PLANNING)
        storyboard = build_storyboard(content_map)
        return ContentPreparation(
            content_map=content_map,
            storyboard=storyboard,
            actions=build_content_actions(content_map, storyboard),
            metadata=metadata,
            warnings=(),
        )

    def revise(
        self,
        preparation: ContentPreparation,
        *,
        selected_range_order: tuple[str, ...] = (),
        reorder_acknowledged: bool = False,
        chapter_titles: dict[str, str] | None = None,
    ) -> ContentPreparation:
        storyboard = revise_storyboard(
            preparation.content_map,
            selected_range_order=selected_range_order,
            reorder_acknowledged=reorder_acknowledged,
            chapter_titles=chapter_titles,
        )
        return ContentPreparation(
            content_map=preparation.content_map,
            storyboard=storyboard,
            actions=build_content_actions(preparation.content_map, storyboard),
            metadata=preparation.metadata,
            warnings=(),
        )

    def preview(self, preparation: ContentPreparation) -> ContentReview:
        self.progress(ContentStatus.PREVIEWING)
        root = self.config.output_directory / "content-review-private" / "preview"
        root.mkdir(parents=True, exist_ok=True)
        previews = tuple(
            ContentJoinPreview(
                action_id=action.id,
                action_ranges=action.source_ranges,
                context_ranges=action.source_ranges,
                relative_paths=(f"preview/{action.id}.mp4",),
                artifact_hashes=("f" * 64,),
                encoding_parameters={"codec": "fake"},
                identity=f"preview-{action.id}",
            )
            for action in preparation.actions
            if action.changes_content and action.requires_confirmation
        )
        for preview in previews:
            (
                self.config.output_directory
                / "content-review-private"
                / preview.relative_paths[0]
            ).write_bytes(b"private-preview")
        plan = build_content_plan(
            preparation.content_map,
            preparation.storyboard,
            preview_identities={item.action_id: item.identity for item in previews},
        )
        self.progress(ContentStatus.READY_TO_CONFIRM)
        return ContentReview(preparation, previews, plan)

    def confirm(
        self,
        review: ContentReview,
        *,
        accepted_action_ids: tuple[str, ...],
    ) -> ContentConfirmation:
        required = tuple(
            item.id
            for item in review.plan.actions
            if item.changes_content and item.requires_confirmation
        )
        assert accepted_action_ids == required
        return cast(ContentConfirmation, object())

    def execute(
        self,
        review: ContentReview,
        confirmation: ContentConfirmation,
    ) -> ContentResult:
        del confirmation
        type(self).execute_started.set()
        self.progress(ContentStatus.RENDERING)
        if type(self).block_execute:
            while not self.cancelled:
                time.sleep(0.005)
            raise ContentCancelledError("cancelled")
        self.progress(ContentStatus.VERIFYING)
        public = self.config.output_directory / "content-output"
        for declared in review.plan.public_artifacts:
            target = public / declared.removeprefix("content-output/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"media" if target.suffix == ".mp4" else b"{}")
        self.progress(ContentStatus.COMPLETED)
        return cast(
            ContentResult,
            SimpleNamespace(status=ContentStatus.COMPLETED),
        )

    def cancel(self) -> None:
        self.cancelled = True

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    FakeContentPipeline.reset()


def _manager(config: WebServerConfig) -> ContentJobManager:
    return ContentJobManager(config, pipeline_factory=FakeContentPipeline)


def _wait(
    client: TestClient,
    job_id: str,
    statuses: set[str],
    *,
    timeout: float = 3,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/content/jobs/{job_id}")
        assert response.status_code == 200
        payload = cast(dict[str, Any], response.json())
        if payload["status"] in statuses:
            return payload
        time.sleep(0.01)
    raise AssertionError(f"content job did not reach the expected status: {payload}")


def _upload(client: TestClient, *, body: bytes = b"video") -> dict[str, Any]:
    response = client.post(
        "/api/content/jobs",
        files={"video": ("中文 source.mp4", body, "video/mp4")},
        data={"goal": "selected_clips"},
    )
    assert response.status_code == 202
    return cast(dict[str, Any], response.json())


def _winerror(code: int) -> PermissionError:
    error = PermissionError("injected Windows replace failure")
    error.winerror = code  # type: ignore[attr-defined]
    return error


def test_content_job_windows_replace_retries_transient_access_denial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "content-job-state.json.tmp"
    destination = tmp_path / "content-job-state.json"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    attempts = 0
    delays: list[float] = []
    calls: list[tuple[Path, Path]] = []

    def replace(observed_source: Path, observed_destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        calls.append((observed_source, observed_destination))
        if attempts == 1:
            raise _winerror(5)

    content_jobs_module._retry_windows_replace(
        source,
        destination,
        replace=replace,
        sleep=delays.append,
    )

    assert attempts == 2
    assert delays == [0.01]
    assert calls == [(source, destination), (source, destination)]


def test_content_job_windows_replace_surfaces_exhausted_access_denial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "content-job-state.json.tmp"
    destination = tmp_path / "content-job-state.json"
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
        content_jobs_module._retry_windows_replace(
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


def test_content_job_windows_replace_does_not_retry_other_errors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "content-job-state.json.tmp"
    destination = tmp_path / "content-job-state.json"
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
        content_jobs_module._retry_windows_replace(
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


def test_content_job_windows_replace_does_not_retry_after_source_disappears(
    tmp_path: Path,
) -> None:
    source = tmp_path / "content-job-state.json.tmp"
    destination = tmp_path / "content-job-state.json"
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
        content_jobs_module._retry_windows_replace(
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


def test_content_prepare_recovers_from_transient_state_replace_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = os.replace
    state_attempts = 0

    def replace(source: Path, destination: Path) -> None:
        nonlocal state_attempts
        if Path(destination).name == "content-job-state.json":
            state_attempts += 1
            if state_attempts == 3:
                raise _winerror(5)
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    config = _config(tmp_path)
    with TestClient(create_app(config, content_manager=_manager(config))) as client:
        job_id = cast(str, _upload(client)["job_id"])
        prepared = _wait(client, job_id, {"awaiting_review", "failed"})

    assert prepared["status"] == "awaiting_review"
    assert state_attempts >= 4


def test_content_persistent_state_replace_denial_is_path_free_and_preserves_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_replace = os.replace
    state_attempts = 0
    final_error = _winerror(5)

    def replace(source: Path, destination: Path) -> None:
        nonlocal state_attempts
        if Path(destination).name == "content-job-state.json":
            state_attempts += 1
            if state_attempts >= 3:
                raise final_error
        original_replace(source, destination)

    monkeypatch.setattr(os, "replace", replace)
    config = _config(tmp_path)
    manager = _manager(config)
    with TestClient(create_app(config, content_manager=manager)) as client:
        job_id = cast(str, _upload(client)["job_id"])
        failed = _wait(client, job_id, {"failed"})
        record = manager.require(job_id)
        persisted = json.loads(
            (record.directory / "content-job-state.json").read_text(encoding="utf-8")
        )

    assert (
        failed["error"] == "Internal useful-content failure: ContentJobPersistenceError"
    )
    assert "\\" not in failed["error"]
    assert "/" not in failed["error"]
    assert persisted["status"] == "queued"
    assert state_attempts >= 14


def _revise_preview(client: TestClient, job_id: str) -> dict[str, Any]:
    _wait(client, job_id, {"awaiting_review"})
    revision = client.put(
        f"/api/content/jobs/{job_id}/storyboard",
        json={
            "expected_revision": 0,
            "ranges": [
                {
                    "kind": "keep",
                    "start_seconds": 2,
                    "end_seconds": 4,
                    "label": "重点",
                }
            ],
        },
    )
    assert revision.status_code == 200
    preview = client.post(f"/api/content/jobs/{job_id}/previews")
    assert preview.status_code == 200
    assert preview.json()["status"] == "ready_to_confirm"
    plan = client.get(f"/api/content/jobs/{job_id}/plan")
    assert plan.status_code == 200
    return cast(dict[str, Any], plan.json())


def test_content_ai_prepare_review_and_apply_uses_revision_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    manager = _manager(config)

    class FakeAdvancedPipeline:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        def prepare(self, source: Path) -> AdvancedAIPreparation:
            transcript, _ = normalize_asr_transcript(
                FakeASRProvider(), source, duration_seconds=10
            )
            request = ContentIntelligenceRequest(
                input_hash=compute_file_sha256(source),
                transcript_hash=transcript.transcript_hash,
                duration_seconds=10,
                transcript_segments=transcript.segments,
            )
            batch = run_content_intelligence(FakeContentIntelligenceProvider(), request)
            private_root = tmp_path / "ai-review-private"
            private_root.mkdir(parents=True, exist_ok=True)
            return AdvancedAIPreparation(
                transcript=transcript,
                suggestions=batch,
                private_root=private_root,
                cpu_map_digest="c" * 64,
                cpu_warnings=(),
            )

    monkeypatch.setattr(
        "videoscope.web.app.AdvancedAIContentPipeline",
        FakeAdvancedPipeline,
    )
    with TestClient(create_app(config, content_manager=manager)) as client:
        created = _upload(client)
        job_id = cast(str, created["job_id"])
        _wait(client, job_id, {"awaiting_review"})
        prepared = client.post(
            f"/api/content/jobs/{job_id}/ai/prepare",
            json={"semantic_model_id": "fake-local-model"},
        )
        assert prepared.status_code == 200
        suggestions = cast(list[dict[str, Any]], prepared.json()["suggestions"])
        review = client.put(
            f"/api/content/jobs/{job_id}/ai/review",
            json={
                "decisions": [
                    {"suggestion_id": item["id"], "decision": "accept"}
                    for item in suggestions
                ]
            },
        )
        assert review.status_code == 200
        applied = client.post(
            f"/api/content/jobs/{job_id}/ai/apply",
            json={"expected_revision": 0},
        )
        assert applied.status_code == 200
        assert applied.json()["revision"] == 1
        record = manager.require(job_id)
        assert {item.kind.value for item in record.config.user_ranges} == {
            "chapter",
            "keep",
        }


def test_content_ai_byok_requires_consent_and_injects_memory_only_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    manager = _manager(config)
    captured: dict[str, Any] = {}

    class FakeAdvancedPipeline:
        def __init__(self, settings: object, *, dependencies: Any) -> None:
            captured["settings"] = settings
            captured["dependencies"] = dependencies

        def prepare(self, source: Path) -> AdvancedAIPreparation:
            dependencies = captured["dependencies"]
            provider = dependencies.content_provider
            captured["provider"] = provider
            transcript, _ = normalize_asr_transcript(
                FakeASRProvider(), source, duration_seconds=10
            )
            request = ContentIntelligenceRequest(
                input_hash=compute_file_sha256(source),
                transcript_hash=transcript.transcript_hash,
                duration_seconds=10,
                transcript_segments=transcript.segments,
            )
            batch = run_content_intelligence(FakeContentIntelligenceProvider(), request)
            private_root = tmp_path / "byok-ai-private"
            private_root.mkdir(parents=True, exist_ok=True)
            return AdvancedAIPreparation(
                transcript=transcript,
                suggestions=batch,
                private_root=private_root,
                cpu_map_digest="c" * 64,
                cpu_warnings=(),
            )

    monkeypatch.setattr(
        "videoscope.web.app.AdvancedAIContentPipeline", FakeAdvancedPipeline
    )
    provider_payload = {
        "profile_id": "my-ai",
        "display_name": "My provider",
        "provider_id": "custom-openai",
        "protocol": "openai_compatible",
        "api_base_url": "https://provider.example/v1",
        "model_id": "user-paid-model",
        "api_key": "memory-only-test-key",
        "capabilities": ["structured_text"],
        "request_json_object": True,
    }
    with TestClient(
        create_app(config, content_manager=manager),
        base_url="http://127.0.0.1:8765",
    ) as client:
        stored = client.put(
            "/api/connector/providers/my-ai",
            headers={"Origin": "http://127.0.0.1:8765"},
            json=provider_payload,
        )
        assert stored.status_code == 200
        assert "memory-only-test-key" not in stored.text
        job_id = cast(str, _upload(client)["job_id"])
        _wait(client, job_id, {"awaiting_review"})

        denied = client.post(
            f"/api/content/jobs/{job_id}/ai/prepare",
            json={
                "semantic_model_id": "user-paid-model",
                "provider_profile_id": "my-ai",
                "remote_data_consent": False,
            },
        )
        assert denied.status_code == 422
        assert captured == {}

        prepared = client.post(
            f"/api/content/jobs/{job_id}/ai/prepare",
            json={
                "semantic_model_id": "user-paid-model",
                "provider_profile_id": "my-ai",
                "remote_data_consent": True,
            },
        )
        assert prepared.status_code == 200
        assert "memory-only-test-key" not in prepared.text
        provider = captured["provider"]
        assert provider.provider_id == "custom-openai"
        assert provider.model_id == "user-paid-model"


def test_content_ai_review_state_can_be_cancelled_and_discarded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    manager = _manager(config)

    class FakeAdvancedPipeline:
        def __init__(self, settings: object) -> None:
            self.settings = settings

        def prepare(self, source: Path) -> AdvancedAIPreparation:
            transcript, _ = normalize_asr_transcript(
                FakeASRProvider(), source, duration_seconds=10
            )
            request = ContentIntelligenceRequest(
                input_hash=compute_file_sha256(source),
                transcript_hash=transcript.transcript_hash,
                duration_seconds=10,
                transcript_segments=transcript.segments,
            )
            batch = run_content_intelligence(FakeContentIntelligenceProvider(), request)
            private_root = tmp_path / "cancel-ai-private"
            private_root.mkdir(parents=True, exist_ok=True)
            return AdvancedAIPreparation(
                transcript=transcript,
                suggestions=batch,
                private_root=private_root,
                cpu_map_digest="c" * 64,
                cpu_warnings=(),
            )

    monkeypatch.setattr(
        "videoscope.web.app.AdvancedAIContentPipeline", FakeAdvancedPipeline
    )
    with TestClient(create_app(config, content_manager=manager)) as client:
        job_id = cast(str, _upload(client)["job_id"])
        _wait(client, job_id, {"awaiting_review"})
        assert (
            client.post(
                f"/api/content/jobs/{job_id}/ai/prepare",
                json={"semantic_model_id": "fake-local-model"},
            ).status_code
            == 200
        )
        assert client.delete(f"/api/content/jobs/{job_id}/ai").status_code == 204
        assert (
            client.get(f"/api/content/jobs/{job_id}/ai/suggestions").status_code == 404
        )


def test_content_upload_revision_preview_confirmation_and_artifact(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with TestClient(create_app(config, content_manager=_manager(config))) as client:
        created = _upload(client)
        job_id = cast(str, created["job_id"])
        plan = _revise_preview(client, job_id)
        preview_manifest = client.get(f"/api/content/jobs/{job_id}/previews")
        assert preview_manifest.status_code == 200
        assert preview_manifest.json()[0]["action_id"] == next(
            item["id"] for item in plan["actions"] if item["changes_content"]
        )
        required = [
            item["id"]
            for item in plan["actions"]
            if item["changes_content"] and item["requires_confirmation"]
        ]
        confirmed = client.post(
            f"/api/content/jobs/{job_id}/confirm",
            json={
                "plan_digest": plan["plan_digest"],
                "revision": 1,
                "accepted_action_ids": required,
            },
        )
        assert confirmed.status_code == 202
        terminal = _wait(client, job_id, {"completed"})
        assert terminal["progress_percent"] == 100

        artifact = client.get(
            f"/api/content/jobs/{job_id}/artifacts/useful-content.mp4"
        )
        assert artifact.status_code == 200
        assert artifact.content == b"media"
        source_map = client.get(f"/api/content/jobs/{job_id}/map")
        assert source_map.status_code == 200
        assert source_map.json()["user_ranges"][0]["label"] == "重点"


def test_content_rejects_stale_revision_and_inexact_confirmation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with TestClient(create_app(config, content_manager=_manager(config))) as client:
        job_id = cast(str, _upload(client)["job_id"])
        plan = _revise_preview(client, job_id)
        stale = client.put(
            f"/api/content/jobs/{job_id}/storyboard",
            json={"expected_revision": 0, "ranges": []},
        )
        mismatch = client.post(
            f"/api/content/jobs/{job_id}/confirm",
            json={
                "plan_digest": "f" * 64,
                "revision": 1,
                "accepted_action_ids": [],
            },
        )

    assert stale.status_code == 409
    assert mismatch.status_code == 409
    assert plan["plan_digest"] != "f" * 64


def test_content_upload_limits_invalid_media_and_sanitized_errors(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, max_upload_bytes=8)
    with TestClient(create_app(config, content_manager=_manager(config))) as client:
        oversized = client.post(
            "/api/content/jobs",
            files={"video": ("large.mp4", b"x" * 9, "video/mp4")},
            data={"goal": "faithful_clean"},
        )
        invalid = _upload(client, body=b"invalid")
        failed = _wait(client, cast(str, invalid["job_id"]), {"failed"})

    assert oversized.status_code == 413
    assert failed["error"] == "The useful-content input or configuration is not valid."
    assert "\\" not in failed["error"]
    assert "/" not in failed["error"]


def test_content_sse_order_path_traversal_and_private_allowlist(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    with TestClient(create_app(config, content_manager=_manager(config))) as client:
        job_id = cast(str, _upload(client)["job_id"])
        plan = _revise_preview(client, job_id)
        action = next(item for item in plan["actions"] if item["changes_content"])
        preview_path = f"preview/{action['id']}.mp4"
        preview = client.get(f"/api/content/jobs/{job_id}/previews/{preview_path}")
        traversal = client.get(
            f"/api/content/jobs/{job_id}/previews/%2e%2e/content-map.json"
        )
        required = [item["id"] for item in plan["actions"] if item["changes_content"]]
        client.post(
            f"/api/content/jobs/{job_id}/confirm",
            json={
                "plan_digest": plan["plan_digest"],
                "revision": 1,
                "accepted_action_ids": required,
            },
        )
        _wait(client, job_id, {"completed"})
        events = client.get(f"/api/content/jobs/{job_id}/events?after=0")

    assert preview.status_code == 200
    assert preview.content == b"private-preview"
    assert traversal.status_code == 404
    sequences = [
        int(line.removeprefix("id: "))
        for line in events.text.splitlines()
        if line.startswith("id: ")
    ]
    assert sequences == sorted(set(sequences))
    assert "ready_to_confirm" in events.text


def test_content_cancel_delete_cleanup_and_restart_recovery(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = _manager(config)
    with TestClient(create_app(config, content_manager=manager)) as client:
        job_id = cast(str, _upload(client)["job_id"])
        plan = _revise_preview(client, job_id)
        required = [item["id"] for item in plan["actions"] if item["changes_content"]]
        FakeContentPipeline.block_execute = True
        client.post(
            f"/api/content/jobs/{job_id}/confirm",
            json={
                "plan_digest": plan["plan_digest"],
                "revision": 1,
                "accepted_action_ids": required,
            },
        )
        assert FakeContentPipeline.execute_started.wait(timeout=2)
        cancelled = client.delete(f"/api/content/jobs/{job_id}")
        assert cancelled.status_code == 200
        _wait(client, job_id, {"cancelled"})
        deleted = client.delete(f"/api/content/jobs/{job_id}")
        assert deleted.status_code == 204

    completed_manager = _manager(config)
    with TestClient(create_app(config, content_manager=completed_manager)) as client:
        FakeContentPipeline.block_execute = False
        completed_id = cast(str, _upload(client)["job_id"])
        completed_plan = _revise_preview(client, completed_id)
        required = [
            item["id"] for item in completed_plan["actions"] if item["changes_content"]
        ]
        client.post(
            f"/api/content/jobs/{completed_id}/confirm",
            json={
                "plan_digest": completed_plan["plan_digest"],
                "revision": 1,
                "accepted_action_ids": required,
            },
        )
        _wait(client, completed_id, {"completed"})

    restored = _manager(config)
    try:
        assert restored.snapshot(completed_id).status is ContentJobStatus.COMPLETED
        assert restored.resolve_public_artifact(
            completed_id,
            "useful-content.mp4",
        ).is_file()
    finally:
        restored.shutdown()


def test_content_persistence_is_path_free_and_interrupted_jobs_fail_closed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    manager = _manager(config)
    record = manager.reserve_job(
        original_filename="private source.mp4",
        config=ContentPipelineConfig(output_directory=tmp_path / "ignored"),
        transcript_filename="private transcript.srt",
    )
    record.input_path.write_bytes(b"video")
    assert record.transcript_path is not None
    record.transcript_path.write_text("private words", encoding="utf-8")
    manager.commit_upload(record.job_id)
    record.update(ContentJobStatus.MAPPING)
    manager._persist(record)
    state_path = record.directory / "content-job-state.json"
    persisted = state_path.read_text(encoding="utf-8")

    assert str(tmp_path) not in persisted
    assert "private source.mp4" not in persisted
    assert "private transcript.srt" not in persisted
    manager.shutdown()

    restored = _manager(config)
    try:
        snapshot = restored.snapshot(record.job_id)
        assert snapshot.status is ContentJobStatus.FAILED
        assert snapshot.error == "The previous local process ended before completion."
        events = restored.events_after(record.job_id, 0)
        assert events[-1].status is ContentJobStatus.FAILED
        assert events[-1].sequence == len(events)
        restored_payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert restored_payload["events"][-1]["status"] == "failed"
    finally:
        restored.shutdown()


def test_content_cleanup_expires_terminal_jobs_and_rejects_tampered_source(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, job_ttl_seconds=1)
    manager = _manager(config)
    record = manager.reserve_job(
        original_filename="source.mp4",
        config=ContentPipelineConfig(output_directory=tmp_path / "ignored"),
        transcript_filename=None,
    )
    record.input_path.write_bytes(b"video")
    manager.commit_upload(record.job_id)
    record.finish(ContentJobStatus.FAILED, error="expected")
    record.updated_at = datetime.now(UTC) - timedelta(seconds=5)
    manager._persist(record)
    expired_directory = record.directory

    expired = manager.cleanup_expired(now=datetime.now(UTC))
    assert expired == (record.job_id,)
    assert not expired_directory.exists()
    manager.shutdown()

    tampered = _manager(config)
    record = tampered.reserve_job(
        original_filename="source.mp4",
        config=ContentPipelineConfig(output_directory=tmp_path / "ignored"),
        transcript_filename=None,
    )
    record.input_path.write_bytes(b"original")
    tampered.commit_upload(record.job_id)
    record.finish(ContentJobStatus.FAILED, error="expected")
    tampered._persist(record)
    job_id = record.job_id
    record.input_path.write_bytes(b"tampered")
    tampered.shutdown()

    restored = _manager(config)
    try:
        with pytest.raises(KeyError):
            restored.snapshot(job_id)
    finally:
        restored.shutdown()
