"""In-process security and lifecycle tests for the optional local API."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from tests.analysis.helpers import FakeMedia, FixedSceneDetector, TickClock
from tests.detectors.dummy import DummyDetector
from videoscope.analysis import (
    AnalysisCancelledError,
    AnalysisInternalError,
    AnalysisPipeline,
    AnalysisProcessingError,
)
from videoscope.detectors import DetectorRegistry
from videoscope.web.app import create_app
from videoscope.web.jobs import JobManager
from videoscope.web.models import WebServerConfig
from videoscope.web.publish_jobs import PublishJobManager


class SuccessfulPipeline:
    """Write small artifacts while exercising every public progress stage."""

    def __init__(
        self,
        config: Any,
        **kwargs: Any,
    ) -> None:
        self.config = config
        self.progress = kwargs["progress"]
        self.cancellation_callback = kwargs["cancellation_callback"]

    def run(self, input_path: Path, *, prompt: str | None = None) -> object:
        del prompt
        if input_path.read_bytes() != b"valid-video":
            raise AnalysisProcessingError(f"ffprobe rejected local input {input_path}")
        for message in (
            "Computing input hash",
            "Probing video metadata",
            "Sampling analysis frames",
            "Detecting scene boundaries",
            "Running detectors",
            "Materializing evidence frames",
            "Building analysis report",
            "Rendering offline HTML report",
        ):
            if self.cancellation_callback():
                raise AnalysisCancelledError("cancelled")
            self.progress(message)
        output = Path(self.config.output_directory)
        evidence = output / "evidence"
        evidence.mkdir(parents=True)
        (output / "report.json").write_text(
            json.dumps({"status": "ok"}, sort_keys=True),
            encoding="utf-8",
        )
        (output / "report.html").write_text(
            "<!doctype html><title>VideoScope</title>",
            encoding="utf-8",
        )
        (evidence / "frame.jpg").write_bytes(b"evidence")
        return SimpleNamespace(report_path=output / "report.json")


class FailingPipeline(SuccessfulPipeline):
    """Raise a path-bearing internal error for response sanitization."""

    def run(self, input_path: Path, *, prompt: str | None = None) -> object:
        del prompt
        raise AnalysisInternalError(f"failure at {input_path.resolve()}")


class BlockingPipeline(SuccessfulPipeline):
    """Wait until cooperative cancellation is requested."""

    started: ClassVar[threading.Event] = threading.Event()

    def run(self, input_path: Path, *, prompt: str | None = None) -> object:
        del input_path, prompt
        type(self).started.set()
        while not self.cancellation_callback():
            time.sleep(0.01)
        raise AnalysisCancelledError("cancelled")


class ConcurrencyPipeline(SuccessfulPipeline):
    """Record simultaneous executions until the test releases both workers."""

    lock: ClassVar[threading.Lock] = threading.Lock()
    release: ClassVar[threading.Event] = threading.Event()
    first_started: ClassVar[threading.Event] = threading.Event()
    active: ClassVar[int] = 0
    maximum_active: ClassVar[int] = 0

    @classmethod
    def reset(cls) -> None:
        cls.release = threading.Event()
        cls.first_started = threading.Event()
        cls.active = 0
        cls.maximum_active = 0

    def run(self, input_path: Path, *, prompt: str | None = None) -> object:
        del input_path, prompt
        with type(self).lock:
            type(self).active += 1
            type(self).maximum_active = max(
                type(self).maximum_active,
                type(self).active,
            )
            type(self).first_started.set()
        try:
            assert type(self).release.wait(timeout=3)
            output = Path(self.config.output_directory)
            output.mkdir(parents=True)
            (output / "report.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(report_path=output / "report.json")
        finally:
            with type(self).lock:
                type(self).active -= 1


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


def _client(
    tmp_path: Path,
    pipeline_factory: Any = SuccessfulPipeline,
    **config_overrides: object,
) -> tuple[TestClient, JobManager, WebServerConfig]:
    config = _config(tmp_path, **config_overrides)
    manager = JobManager(config, pipeline_factory=pipeline_factory)
    return TestClient(create_app(config, manager=manager)), manager, config


def _submit(
    client: TestClient,
    *,
    payload: bytes = b"valid-video",
    filename: str = "测试 video.mp4",
    content_type: str = "video/mp4",
    configuration: Mapping[str, object] | None = None,
) -> Response:
    data: dict[str, str] = {"prompt": "本地 prompt"}
    if configuration is not None:
        data["config"] = json.dumps(configuration)
    return client.post(
        "/api/jobs",
        files={"video": (filename, payload, content_type)},
        data=data,
    )


def _wait_for_terminal(
    client: TestClient,
    job_id: str,
    *,
    timeout: float = 3.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = cast(dict[str, Any], response.json())
        if payload["status"] in {"completed", "failed", "cancelled"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("job did not reach a terminal status")


def test_health_detectors_openapi_and_no_wildcard_cors(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    with client:
        health = client.get("/api/health")
        detectors = client.get("/api/detectors")
        schema = client.get("/openapi.json")
        docs = client.get("/docs")
        cors = client.options(
            "/api/health",
            headers={"Origin": "https://example.invalid"},
        )

    assert health.json()["local_only_default"] is True
    detector_ids = {item["id"] for item in detectors.json()}
    assert {"near_black", "text_stability", "visual_semantic_drift"} <= detector_ids
    manifests = {item["id"]: item for item in detectors.json()}
    assert manifests["near_black"]["available"] is True
    assert manifests["near_black"]["category"] == "cpu"
    assert manifests["text_stability"]["category"] == "ocr"
    assert "/api/jobs/{job_id}/events" in schema.json()["paths"]
    assert docs.status_code == 200
    assert "OpenAPI JSON" in docs.text
    assert "http://" not in docs.text
    assert "https://" not in docs.text
    assert cors.headers.get("access-control-allow-origin") != "*"
    assert cors.status_code == 403


def test_health_counts_active_analysis_and_publish_jobs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The shared health total must include both local workbench managers."""
    config = _config(tmp_path)
    analysis_manager = JobManager(config, pipeline_factory=SuccessfulPipeline)
    publish_manager = PublishJobManager(config)
    monkeypatch.setattr(analysis_manager, "active_job_count", lambda: 2)
    monkeypatch.setattr(publish_manager, "active_job_count", lambda: 3)

    with TestClient(
        create_app(
            config,
            manager=analysis_manager,
            publish_manager=publish_manager,
        )
    ) as client:
        health = client.get("/api/health")

    assert health.status_code == 200
    assert health.json()["active_jobs"] == 5


def test_default_server_rejects_untrusted_host_and_cross_site_upload(
    tmp_path: Path,
) -> None:
    client, _, config = _client(tmp_path)
    with client:
        untrusted_host = client.get(
            "/api/health",
            headers={"Host": "attacker.invalid"},
        )
        cross_site_upload = client.post(
            "/api/jobs",
            headers={"Origin": "https://attacker.invalid"},
            files={"video": ("video.mp4", b"valid-video", "video/mp4")},
        )

    assert untrusted_host.status_code == 400
    assert cross_site_upload.status_code == 403
    assert list(config.job_root.iterdir()) == []


def test_production_dashboard_static_build_is_served(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path)
    with client:
        dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "<title>VideoScope</title>" in dashboard.text
    assert 'src="/assets/' in dashboard.text
    assert "http://" not in dashboard.text
    assert "https://" not in dashboard.text


def test_upload_job_sse_report_and_artifact_end_to_end(tmp_path: Path) -> None:
    client, manager, _ = _client(tmp_path)
    with client:
        submitted = _submit(client)
        assert submitted.status_code == 202
        job_id = submitted.json()["job_id"]
        terminal = _wait_for_terminal(client, job_id)
        with client.stream("GET", f"/api/jobs/{job_id}/events") as events:
            event_body = "".join(events.iter_text())
        report = client.get(f"/api/jobs/{job_id}/report")
        html = client.get(f"/api/jobs/{job_id}/artifacts/report.html")
        evidence = client.get(f"/api/jobs/{job_id}/artifacts/evidence/frame.jpg")
        video = client.get(f"/api/jobs/{job_id}/video")

    assert terminal["status"] == "completed"
    statuses = [
        json.loads(line.removeprefix("data: "))["status"]
        for line in event_body.splitlines()
        if line.startswith("data: ")
    ]
    ordered_unique = list(dict.fromkeys(statuses))
    assert ordered_unique == [
        "queued",
        "probing",
        "sampling",
        "detecting",
        "rendering",
        "completed",
    ]
    assert report.json() == {"status": "ok"}
    assert "VideoScope" in html.text
    assert evidence.content == b"evidence"
    assert video.content == b"valid-video"
    assert terminal["progress_percent"] == 100
    record = manager.require(job_id)
    assert record.input_path.name == "input.mp4"
    assert record.input_path.read_bytes() == b"valid-video"


def test_testclient_job_reuses_real_analysis_pipeline(tmp_path: Path) -> None:
    media = FakeMedia()

    def real_pipeline_factory(config: Any, **kwargs: Any) -> AnalysisPipeline:
        return AnalysisPipeline(
            config,
            registry=DetectorRegistry([DummyDetector()]),
            scene_detector=FixedSceneDetector(),
            hash_function=lambda path: "a" * 64,
            probe_function=media.probe,
            sample_function=media.sample,
            detector_clock=TickClock(),
            ffmpeg="unused",
            ffprobe="unused",
            progress=kwargs["progress"],
            cancellation_callback=kwargs["cancellation_callback"],
        )

    client, _, _ = _client(
        tmp_path,
        pipeline_factory=real_pipeline_factory,
    )
    with client:
        submitted = _submit(client)
        job_id = submitted.json()["job_id"]
        terminal = _wait_for_terminal(client, job_id)
        report = client.get(f"/api/jobs/{job_id}/report").json()

    assert terminal["status"] == "completed"
    assert report["input_hash"] == "a" * 64
    assert report["detector_executions"][0]["detector_id"] == "test.dummy"
    assert report["findings"][0]["detector_id"] == "test.dummy"


def test_invalid_file_fails_without_exposing_absolute_path(tmp_path: Path) -> None:
    client, _, config = _client(tmp_path)
    with client:
        submitted = _submit(client, payload=b"not-a-video")
        terminal = _wait_for_terminal(client, submitted.json()["job_id"])

    assert terminal["status"] == "failed"
    assert str(config.job_root) not in terminal["error"]
    assert "ffprobe rejected" in terminal["error"]


def test_extension_and_mime_are_warnings_not_authoritative_rejection(
    tmp_path: Path,
) -> None:
    client, _, _ = _client(tmp_path)
    with client:
        submitted = _submit(
            client,
            filename="video.txt",
            content_type="text/plain",
        )
        terminal = _wait_for_terminal(client, submitted.json()["job_id"])

    assert terminal["status"] == "completed"
    assert len(terminal["warnings"]) == 2
    assert all("ffprobe" in warning for warning in terminal["warnings"])


def test_max_upload_and_invalid_configuration_are_rejected(tmp_path: Path) -> None:
    client, _, config = _client(tmp_path, max_upload_bytes=4)
    with client:
        too_large = _submit(client, payload=b"12345")
        invalid_config = client.post(
            "/api/jobs",
            files={"video": ("video.mp4", b"ok", "video/mp4")},
            data={"config": "{bad"},
        )

    assert too_large.status_code == 413
    assert invalid_config.status_code == 422
    assert list(config.job_root.iterdir()) == []


def test_upload_filename_and_artifact_path_cannot_traverse(tmp_path: Path) -> None:
    client, manager, config = _client(tmp_path)
    with client:
        submitted = _submit(client, filename="../../outside.mp4")
        job_id = submitted.json()["job_id"]
        _wait_for_terminal(client, job_id)
        traversal = client.get(f"/api/jobs/{job_id}/artifacts/%2e%2e%2finput.mp4")

    record = manager.require(job_id)
    assert record.input_path.parent == record.directory
    assert record.input_path.name == "input.mp4"
    assert not (config.job_root.parent / "outside.mp4").exists()
    assert traversal.status_code == 404
    assert str(config.job_root) not in traversal.text


def test_delete_requests_cancellation_then_removes_terminal_job(
    tmp_path: Path,
) -> None:
    BlockingPipeline.started = threading.Event()
    client, _, _ = _client(tmp_path, pipeline_factory=BlockingPipeline)
    with client:
        submitted = _submit(client)
        job_id = submitted.json()["job_id"]
        assert BlockingPipeline.started.wait(timeout=2)
        cancellation = client.delete(f"/api/jobs/{job_id}")
        terminal = _wait_for_terminal(client, job_id)
        deletion = client.delete(f"/api/jobs/{job_id}")
        missing = client.get(f"/api/jobs/{job_id}")

    assert cancellation.status_code == 200
    assert terminal["status"] == "cancelled"
    assert deletion.status_code == 204
    assert missing.status_code == 404


def test_cleanup_removes_expired_terminal_job(tmp_path: Path) -> None:
    client, manager, _ = _client(tmp_path)
    with client:
        submitted = _submit(client)
        job_id = submitted.json()["job_id"]
        _wait_for_terminal(client, job_id)
        record = manager.require(job_id)
        expired = manager.cleanup_expired(
            now=record.snapshot().updated_at + timedelta(seconds=61)
        )

        assert expired == (job_id,)
        assert not record.directory.exists()


def test_unexpected_analysis_failure_is_generic(tmp_path: Path) -> None:
    client, _, config = _client(tmp_path, pipeline_factory=FailingPipeline)
    with client:
        submitted = _submit(client)
        terminal = _wait_for_terminal(client, submitted.json()["job_id"])

    assert terminal["status"] == "failed"
    assert terminal["error"] == "failure at <input>"
    assert str(config.job_root) not in terminal["error"]


def test_heavy_model_jobs_use_separate_single_worker_pool(
    tmp_path: Path,
) -> None:
    ConcurrencyPipeline.reset()
    client, _, _ = _client(
        tmp_path,
        pipeline_factory=ConcurrencyPipeline,
        heavy_ai_concurrency=1,
        cpu_concurrency=2,
    )
    configuration = {"enabled_detectors": ["visual_semantic_drift"]}
    with client:
        first = _submit(client, configuration=configuration).json()["job_id"]
        second = _submit(client, configuration=configuration).json()["job_id"]
        assert ConcurrencyPipeline.first_started.wait(timeout=2)
        time.sleep(0.1)
        second_while_first_runs = client.get(f"/api/jobs/{second}").json()
        ConcurrencyPipeline.release.set()
        _wait_for_terminal(client, first)
        _wait_for_terminal(client, second)

    assert second_while_first_runs["status"] == "queued"
    assert ConcurrencyPipeline.maximum_active == 1
