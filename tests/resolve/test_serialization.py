"""Tests for deterministic Publish Ready JSON serialization."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import videoscope.resolve.serialization as serialization_module
from videoscope.domain import VideoMetadata
from videoscope.resolve import (
    EXPECTED_PUBLISH_ARTIFACTS,
    PUBLISH_PREVIEW_ARTIFACT,
    PublishAction,
    PublishActionKind,
    PublishArtifact,
    PublishBackend,
    PublishChangeLog,
    PublishEffectiveConfig,
    PublishPlan,
    PublishProfileId,
    PublishTechnicalReport,
    VerificationCheck,
    VerificationReport,
    VerificationStatus,
    make_publish_plan_digest,
    publish_change_log_from_json,
    publish_change_log_to_json,
    publish_plan_from_json,
    publish_plan_to_json,
    publish_technical_report_from_json,
    publish_technical_report_to_json,
    read_publish_change_log_json,
    read_publish_plan_json,
    read_publish_technical_report_json,
    write_publish_change_log_json,
    write_publish_plan_json,
    write_publish_technical_report_json,
)

_TASK_ID = "2" * 32
_EFFECTIVE_CONFIG = PublishEffectiveConfig(
    preview_seconds=6.0,
    keep_workspace=False,
    run_diagnostics=True,
)


def make_plan() -> PublishPlan:
    """Build a valid plan independently from serialization behavior."""
    metadata = VideoMetadata(
        filename="输入 视频.mp4",
        container_format="mp4",
        codec="h264",
        width=1920,
        height=1080,
        duration_seconds=4.0,
        average_frame_rate=30.0,
        estimated_frame_count=120,
        has_audio=False,
        file_size_bytes=1024,
    )
    actions = (
        PublishAction(
            action_id="remux",
            kind=PublishActionKind.REMUX,
            description="重新封装，不改变画面内容",
            parameters={"container": "mp4"},
            affects=("container",),
            changes_content_semantics=False,
            confirmation_required=False,
        ),
    )
    return PublishPlan(
        task_id=_TASK_ID,
        input_hash="b" * 64,
        source_metadata=metadata,
        source_read_only=True,
        profile_id=PublishProfileId.COMPATIBLE_MP4,
        profile_version="1.0.0",
        backend=PublishBackend.NATIVE_LOCAL,
        actions=actions,
        preview_artifact=PUBLISH_PREVIEW_ARTIFACT,
        confirmation_required=True,
        expected_artifacts=EXPECTED_PUBLISH_ARTIFACTS,
        effective_config=_EFFECTIVE_CONFIG,
        output_filename="publish-ready.mp4",
        plan_digest=make_publish_plan_digest(
            task_id=_TASK_ID,
            input_hash="b" * 64,
            source_read_only=True,
            profile_id=PublishProfileId.COMPATIBLE_MP4,
            profile_version="1.0.0",
            backend=PublishBackend.NATIVE_LOCAL,
            actions=actions,
            preview_artifact=PUBLISH_PREVIEW_ARTIFACT,
            confirmation_required=True,
            expected_artifacts=EXPECTED_PUBLISH_ARTIFACTS,
            effective_config=_EFFECTIVE_CONFIG,
            output_filename="publish-ready.mp4",
        ),
    )


def _winerror(code: int) -> PermissionError:
    error = PermissionError("injected Windows replace failure")
    error.winerror = code  # type: ignore[attr-defined]
    return error


def test_resolve_windows_replace_retries_transient_access_denial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.json.tmp"
    destination = tmp_path / "result.json"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    calls: list[tuple[Path, Path]] = []
    delays: list[float] = []

    def replace(observed_source: Path, observed_destination: Path) -> None:
        calls.append((observed_source, observed_destination))
        if len(calls) == 1:
            raise _winerror(5)

    serialization_module._retry_windows_replace(
        source,
        destination,
        replace=replace,
        sleep=delays.append,
    )

    assert calls == [(source, destination), (source, destination)]
    assert delays == [0.01]
    assert source.read_bytes() == b"new"
    assert destination.read_bytes() == b"old"


def test_resolve_windows_replace_surfaces_exhausted_access_denial(
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.json.tmp"
    destination = tmp_path / "result.json"
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
        serialization_module._retry_windows_replace(
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


def test_resolve_windows_replace_does_not_retry_other_errors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.json.tmp"
    destination = tmp_path / "result.json"
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
        serialization_module._retry_windows_replace(
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


def test_resolve_windows_replace_does_not_retry_after_source_disappears(
    tmp_path: Path,
) -> None:
    source = tmp_path / "result.json.tmp"
    destination = tmp_path / "result.json"
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
        serialization_module._retry_windows_replace(
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


def test_resolve_writer_retries_transient_access_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = make_plan()
    artifact = PublishArtifact(
        relative_path="publish-ready.mp4",
        sha256="c" * 64,
        description="可发布视频",
    )
    change_log = PublishChangeLog(
        plan_digest=plan.plan_digest,
        actions=plan.actions,
        artifacts=(artifact,),
    )
    destination = tmp_path / "发布 报告 ü" / "changes.json"
    destination.parent.mkdir()
    destination.write_bytes(b"old")
    real_replace = os.replace
    attempts = 0
    delays: list[float] = []

    def replace(source: Path, observed_destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _winerror(5)
        real_replace(source, observed_destination)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(time, "sleep", delays.append)

    write_publish_change_log_json(change_log, destination)

    assert read_publish_change_log_json(destination) == change_log
    assert attempts == 2
    assert delays == [0.01]
    assert list(destination.parent.glob("*.tmp")) == []


def test_plan_json_is_canonical_and_round_trips(tmp_path: Path) -> None:
    """Repeated plan serialization is stable and retains UTF-8 descriptions."""
    plan = make_plan()

    first = publish_plan_to_json(plan)
    second = publish_plan_to_json(plan)
    destination = tmp_path / "输出 目录" / "plan.json"
    write_publish_plan_json(plan, destination)

    assert first == second
    assert "重新封装" in first
    assert first.endswith("\n") is False
    assert read_publish_plan_json(destination) == plan
    assert destination.read_text(encoding="utf-8") == f"{first}\n"
    assert publish_plan_from_json(first) == plan
    assert list(json.loads(first)) == sorted(json.loads(first))


def test_public_plan_serializer_rejects_caller_controlled_indentation() -> None:
    """Canonical plan JSON must keep its fixed two-space indentation."""
    with pytest.raises(TypeError):
        publish_plan_to_json(make_plan(), indent=4)  # type: ignore[call-arg]


def test_change_log_and_technical_report_round_trip(tmp_path: Path) -> None:
    """Each artifact type has its own validated deterministic codec."""
    plan = make_plan()
    artifact = PublishArtifact(
        relative_path="publish-ready.mp4",
        sha256="c" * 64,
        description="可发布视频",
    )
    change_log = PublishChangeLog(
        plan_digest=plan.plan_digest,
        actions=plan.actions,
        artifacts=(artifact,),
    )
    verification = VerificationReport(
        profile_id=plan.profile_id,
        profile_version=plan.profile_version,
        status=VerificationStatus.PASSED,
        checks=(
            VerificationCheck(
                check_id="decodable",
                status=VerificationStatus.PASSED,
                message="输出可解码",
                measured={"frames": 120},
            ),
        ),
    )
    technical_report = PublishTechnicalReport(
        plan_digest=plan.plan_digest,
        verification=verification,
        artifacts=(artifact,),
    )

    with pytest.raises(TypeError):
        publish_change_log_to_json(change_log, indent=4)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        publish_technical_report_to_json(  # type: ignore[call-arg]
            technical_report,
            indent=4,
        )
    assert (
        publish_change_log_from_json(publish_change_log_to_json(change_log))
        == change_log
    )
    assert (
        publish_technical_report_from_json(
            publish_technical_report_to_json(technical_report)
        )
        == technical_report
    )

    output = tmp_path / "发布 报告 ü"
    change_path = output / "changes.json"
    technical_path = output / "technical-report.json"
    change_path.parent.mkdir(parents=True)
    change_path.write_text("stale", encoding="utf-8")
    technical_path.write_text("stale", encoding="utf-8")
    for _ in range(2):
        write_publish_change_log_json(change_log, change_path)
        write_publish_technical_report_json(technical_report, technical_path)

    assert read_publish_change_log_json(change_path) == change_log
    assert read_publish_technical_report_json(technical_path) == technical_report
    assert list(output.glob(".*.tmp")) == []
