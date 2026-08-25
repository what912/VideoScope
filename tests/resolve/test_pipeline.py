"""Tests for the core Publish Ready orchestration service."""

from __future__ import annotations

import ctypes
import os
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from videoscope.analysis import (
    AnalysisCancelledError,
    AnalysisInputError,
    AnalysisInternalError,
    AnalysisProcessingError,
    AnalysisResult,
)
from videoscope.domain import (
    AnalysisReport,
    DetectorExecution,
    DetectorStatus,
    VideoMetadata,
    write_report_json,
)
from videoscope.resolve import (
    PublishArtifactError,
    PublishCancelledError,
    PublishConfirmationError,
    PublishInputError,
    PublishMediaError,
    PublishProfileId,
    PublishReadyConfig,
    PublishReadyPipeline,
    VerificationCheck,
    VerificationReport,
    VerificationStatus,
)
from videoscope.resolve.executor import NativePublishResult
from videoscope.resolve.profiles import PublishProfile


def _metadata(*, filename: str, width: int = 1920, height: int = 1080) -> VideoMetadata:
    return VideoMetadata(
        filename=filename,
        container_format="mov,mp4,m4a,3gp,3g2,mj2",
        codec="h264",
        width=width,
        height=height,
        duration_seconds=12.0,
        average_frame_rate=30.0,
        estimated_frame_count=360,
        has_audio=True,
        file_size_bytes=4096,
        raw_probe={"pixel_format": "yuv420p", "audio_codec": "aac"},
    )


def _analysis_report(path: Path, metadata: VideoMetadata) -> AnalysisReport:
    input_hash = sha256(path.read_bytes()).hexdigest()
    return AnalysisReport(
        tool_version="0.3.0.dev0",
        analysis_id=f"analysis-{input_hash[:8]}",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        input_hash=input_hash,
        metadata=metadata,
        detector_executions=[
            DetectorExecution(
                detector_id=detector_id,
                status=DetectorStatus.OK,
                elapsed_seconds=0.0,
                findings_count=0,
            )
            for detector_id in ("near_black", "possible_freeze")
        ],
    )


class FakeAnalysisPipeline:
    def __init__(
        self,
        output_directory: Path,
        metadata: VideoMetadata,
        failure: Exception | None,
        calls: list[tuple[Path, Path]],
    ) -> None:
        self._output_directory = output_directory
        self._metadata = metadata
        self._failure = failure
        self._calls = calls

    def run(self, input_path: Path, *, prompt: str | None = None) -> AnalysisResult:
        assert prompt is None
        source = Path(input_path)
        self._calls.append((source, self._output_directory))
        if self._failure is not None:
            raise self._failure
        report = _analysis_report(source, self._metadata)
        report_path = self._output_directory / "report.json"
        write_report_json(report, report_path)
        return AnalysisResult(
            report=report,
            report_path=report_path,
            html_report_path=None,
            bundled_video_path=None,
            workspace_directory=None,
        )


class FakeAnalysisFactory:
    def __init__(
        self,
        metadata: tuple[VideoMetadata, ...],
        *,
        failures: tuple[Exception | None, ...] = (),
    ) -> None:
        self.metadata = metadata
        self.failures = failures
        self.calls: list[tuple[Path, Path]] = []
        self.created_directories: list[Path] = []

    def __call__(self, output_directory: Path) -> FakeAnalysisPipeline:
        index = len(self.created_directories)
        self.created_directories.append(output_directory)
        failure = self.failures[index] if index < len(self.failures) else None
        return FakeAnalysisPipeline(
            output_directory,
            self.metadata[index],
            failure,
            self.calls,
        )


class FakeExecutor:
    def __init__(
        self,
        *,
        preview_failure: Exception | None = None,
        execute_failure: Exception | None = None,
    ) -> None:
        self.preview_failure = preview_failure
        self.execute_failure = execute_failure
        self.preview_calls: list[tuple[Path, Path]] = []
        self.execute_calls: list[tuple[Path, Path]] = []

    def generate_preview(
        self, plan: object, source_path: Path, work_directory: Path
    ) -> Path:
        del plan
        self.preview_calls.append((source_path, work_directory))
        if self.preview_failure is not None:
            raise self.preview_failure
        preview = work_directory / "preview" / "publish-preview.mp4"
        preview.parent.mkdir(parents=True, exist_ok=True)
        preview.write_bytes(b"preview media")
        return preview

    def execute(
        self, plan: object, source_path: Path, work_directory: Path
    ) -> NativePublishResult:
        del plan
        self.execute_calls.append((source_path, work_directory))
        if self.execute_failure is not None:
            raise self.execute_failure
        video = work_directory / "publish-ready.mp4"
        cover = work_directory / "cover.jpg"
        video.write_bytes(b"publish ready media")
        cover.write_bytes(b"cover image")
        return NativePublishResult(video_path=video, cover_path=cover)


class FakeVerifier:
    def __init__(self, status: VerificationStatus = VerificationStatus.PASSED) -> None:
        self.status = status

    def verify(
        self,
        *,
        source_metadata: VideoMetadata,
        output_metadata: VideoMetadata | None,
        profile: PublishProfile,
        before: AnalysisReport,
        after: AnalysisReport | None,
    ) -> VerificationReport:
        del source_metadata, output_metadata, before, after
        check = VerificationCheck(
            check_id="fake_verification",
            status=self.status,
            message=f"Fake verification returned {self.status.value}.",
        )
        return VerificationReport(
            profile_id=profile.id,
            profile_version=profile.version,
            status=self.status,
            checks=(check,),
            manual_review_reasons=(
                (check.message,)
                if self.status is VerificationStatus.NEEDS_REVIEW
                else ()
            ),
        )


class CancellingVerifier(FakeVerifier):
    def __init__(self, cancel: Callable[[], None]) -> None:
        super().__init__()
        self._cancel = cancel

    def verify(
        self,
        *,
        source_metadata: VideoMetadata,
        output_metadata: VideoMetadata | None,
        profile: PublishProfile,
        before: AnalysisReport,
        after: AnalysisReport | None,
    ) -> VerificationReport:
        result = super().verify(
            source_metadata=source_metadata,
            output_metadata=output_metadata,
            profile=profile,
            before=before,
            after=after,
        )
        self._cancel()
        return result


def _pipeline(
    tmp_path: Path,
    *,
    keep_workspace: bool = False,
    analysis_failures: tuple[Exception | None, ...] = (),
    executor: FakeExecutor | None = None,
    verifier: FakeVerifier | None = None,
    verification_status: VerificationStatus = VerificationStatus.PASSED,
    progress: Callable[[str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[PublishReadyPipeline, FakeAnalysisFactory, FakeExecutor]:
    analysis_factory = FakeAnalysisFactory(
        (
            _metadata(filename="源 视频 ü.mp4"),
            _metadata(filename="publish-ready.mp4"),
        ),
        failures=analysis_failures,
    )
    fake_executor = executor or FakeExecutor()
    pipeline = PublishReadyPipeline(
        PublishReadyConfig(
            profile_id=PublishProfileId.COMPATIBLE_MP4,
            output_directory=tmp_path / "发布 output",
            keep_workspace=keep_workspace,
        ),
        analysis_pipeline_factory=analysis_factory,
        executor=fake_executor,
        verifier=verifier or FakeVerifier(verification_status),
        progress=progress,
        cancellation_callback=is_cancelled,
    )
    return pipeline, analysis_factory, fake_executor


def _source(tmp_path: Path) -> tuple[Path, bytes]:
    source = tmp_path / "输入 文件 中文 ü.mp4"
    original = b"immutable source bytes"
    source.write_bytes(original)
    return source, original


def test_config_rejects_unknown_fields_and_invalid_preview_seconds(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        PublishReadyConfig.model_validate(
            {
                "profile_id": PublishProfileId.COMPATIBLE_MP4,
                "output_directory": tmp_path,
                "unknown": True,
            }
        )
    with pytest.raises(ValueError):
        PublishReadyConfig(
            profile_id=PublishProfileId.COMPATIBLE_MP4,
            preview_seconds=10.1,
        )


def test_missing_input_fails_without_creating_output(tmp_path: Path) -> None:
    events: list[str] = []
    pipeline, analysis, executor = _pipeline(tmp_path, progress=events.append)

    with pytest.raises(PublishInputError, match="not found"):
        pipeline.prepare(tmp_path / "不存在.mp4")

    assert analysis.calls == []
    assert executor.preview_calls == []
    assert not (tmp_path / "发布 output").exists()
    assert events == ["created", "inspecting", "failed"]


def test_prepare_handles_unicode_paths_without_mutating_source(tmp_path: Path) -> None:
    source, original = _source(tmp_path)
    events: list[str] = []
    pipeline, analysis, executor = _pipeline(tmp_path, progress=events.append)

    preparation = pipeline.prepare(source)

    assert preparation.plan.profile_id is PublishProfileId.COMPATIBLE_MP4
    assert preparation.preview_path.read_bytes() == b"preview media"
    assert preparation.plan_path.is_file()
    assert preparation.analysis_before_report_path.is_file()
    assert source.read_bytes() == original
    assert analysis.calls[0][0] == source
    assert executor.preview_calls[0][0] == source
    assert not preparation.output_directory.exists()
    assert events == ["created", "inspecting", "planning", "awaiting_confirmation"]


def test_discard_claims_and_cleans_an_unconfirmed_preparation(
    tmp_path: Path,
) -> None:
    """Declining confirmation must remove source-derived staging data."""
    source, original = _source(tmp_path)
    pipeline, _, _ = _pipeline(tmp_path, keep_workspace=True)
    preparation = pipeline.prepare(source)

    pipeline.discard(preparation)
    pipeline.discard(preparation)

    assert not preparation.workspace_directory.exists()
    assert not preparation.output_directory.exists()
    assert source.read_bytes() == original


def test_publish_preview_moves_prepared_artifacts_to_the_requested_output(
    tmp_path: Path,
) -> None:
    """Preview-only output must have a stable path users can open after exit."""
    source, original = _source(tmp_path)
    pipeline, _, _ = _pipeline(tmp_path)
    preparation = pipeline.prepare(source)

    preview = pipeline.publish_preview(preparation)

    assert preview == preparation.output_directory / "preview/publish-preview.mp4"
    assert preview.read_bytes() == b"preview media"
    assert {
        path.relative_to(preparation.output_directory).as_posix()
        for path in preparation.output_directory.rglob("*")
        if path.is_file()
    } == {
        "analysis-before/report.json",
        "plan.json",
        "preview/publish-preview.mp4",
    }
    assert not preparation.workspace_directory.exists()
    assert source.read_bytes() == original


def test_prepare_rejects_an_existing_output_directory(tmp_path: Path) -> None:
    source, original = _source(tmp_path)
    output = tmp_path / "发布 output"
    output.mkdir()
    marker = output / "user-file.txt"
    marker.write_text("keep", encoding="utf-8")
    pipeline, analysis, executor = _pipeline(tmp_path)

    with pytest.raises(PublishArtifactError, match="already exists"):
        pipeline.prepare(source)

    assert marker.read_text(encoding="utf-8") == "keep"
    assert source.read_bytes() == original
    assert analysis.calls == []
    assert executor.preview_calls == []


def test_prepare_rejects_a_dangling_output_symlink(tmp_path: Path) -> None:
    source, _ = _source(tmp_path)
    output = tmp_path / "发布 output"
    try:
        output.symlink_to(tmp_path / "missing output root", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"creating a directory symlink is unavailable: {exc}")
    pipeline, analysis, executor = _pipeline(tmp_path)

    with pytest.raises(PublishArtifactError, match="already exists"):
        pipeline.prepare(source)

    assert output.is_symlink()
    assert analysis.calls == []
    assert executor.preview_calls == []


def test_prepare_rejects_lstat_only_output_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _source(tmp_path)
    output = tmp_path / "发布 output"
    pipeline, analysis, executor = _pipeline(tmp_path)
    original_lexists = os.path.lexists

    def lstat_collision(path: str | os.PathLike[str]) -> bool:
        return Path(path) == output or original_lexists(path)

    monkeypatch.setattr(os.path, "lexists", lstat_collision)

    with pytest.raises(PublishArtifactError, match="already exists"):
        pipeline.prepare(source)

    assert analysis.calls == []
    assert executor.preview_calls == []


def test_digest_mismatch_does_not_run_full_processing(tmp_path: Path) -> None:
    source, original = _source(tmp_path)
    pipeline, _, executor = _pipeline(tmp_path)
    preparation = pipeline.prepare(source)

    with pytest.raises(PublishConfirmationError, match="digest"):
        pipeline.execute(preparation, confirmed_plan_digest="0" * 64)

    assert executor.execute_calls == []
    assert source.read_bytes() == original
    assert preparation.workspace_directory.is_dir()
    assert not preparation.output_directory.exists()


def test_in_place_plan_mutation_is_rejected_before_processing(tmp_path: Path) -> None:
    source, _ = _source(tmp_path)
    pipeline, _, executor = _pipeline(tmp_path)
    preparation = pipeline.prepare(source)
    preparation.plan.actions[0].parameters["container"] = "attacker-value"

    with pytest.raises(PublishConfirmationError, match="changed"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert executor.execute_calls == []
    assert not preparation.output_directory.exists()


def test_processing_callback_plan_mutation_is_rejected_before_executor(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    issued: list[object] = []

    def mutate_on_processing(status: str) -> None:
        if status == "processing":
            preparation = issued[0]
            assert hasattr(preparation, "plan")
            preparation.plan.actions[0].parameters["container"] = "callback-value"

    pipeline, _, executor = _pipeline(tmp_path, progress=mutate_on_processing)
    preparation = pipeline.prepare(source)
    issued.append(preparation)

    with pytest.raises(PublishConfirmationError, match="changed"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert executor.execute_calls == []
    assert not preparation.output_directory.exists()


def test_source_change_after_preparation_is_rejected_before_processing(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    pipeline, _, executor = _pipeline(tmp_path)
    preparation = pipeline.prepare(source)
    source.write_bytes(b"changed after confirmation preview")

    with pytest.raises(PublishConfirmationError, match="changed"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert executor.execute_calls == []
    assert not preparation.output_directory.exists()


def test_forged_preparation_cannot_process_or_delete_an_unowned_directory(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    pipeline, _, executor = _pipeline(tmp_path)
    preparation = pipeline.prepare(source)
    protected = tmp_path / "protected user directory"
    protected.mkdir()
    marker = protected / "keep.txt"
    marker.write_text("do not delete", encoding="utf-8")
    forged = replace(preparation, workspace_directory=protected)

    with pytest.raises(PublishConfirmationError, match="not issued"):
        pipeline.execute(
            forged,
            confirmed_plan_digest=forged.plan.plan_digest,
        )

    assert marker.read_text(encoding="utf-8") == "do not delete"
    assert executor.execute_calls == []


def test_success_atomically_publishes_complete_relative_artifact_set(
    tmp_path: Path,
) -> None:
    source, original = _source(tmp_path)
    events: list[str] = []
    pipeline, analysis, _ = _pipeline(tmp_path, progress=events.append)
    preparation = pipeline.prepare(source)

    result = pipeline.execute(
        preparation,
        confirmed_plan_digest=preparation.plan.plan_digest,
    )

    output = tmp_path / "发布 output"
    expected_files = {
        "analysis-after/report.json",
        "analysis-before/report.json",
        "changes.json",
        "cover.jpg",
        "plan.json",
        "preview/publish-preview.mp4",
        "publish-ready.mp4",
        "technical-report.json",
    }
    actual_files = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files
    assert result.video_path == output / "publish-ready.mp4"
    assert result.technical_report.verification.status is VerificationStatus.PASSED
    assert result.status == "completed"
    assert source.read_bytes() == original
    assert not preparation.workspace_directory.exists()
    assert len(analysis.calls) == 2
    assert events == [
        "created",
        "inspecting",
        "planning",
        "awaiting_confirmation",
        "processing",
        "verifying",
        "completed",
    ]

    public_json = "\n".join(
        path.read_text(encoding="utf-8") for path in output.rglob("*.json")
    )
    assert str(source.resolve()) not in public_json
    assert str(preparation.workspace_directory.resolve()) not in public_json
    assert "\\" not in result.change_log.model_dump_json()
    assert [artifact.relative_path for artifact in result.change_log.artifacts] == [
        "plan.json",
        "preview/publish-preview.mp4",
        "publish-ready.mp4",
        "cover.jpg",
        "analysis-before/report.json",
        "analysis-after/report.json",
    ]
    assert [
        artifact.relative_path for artifact in result.technical_report.artifacts
    ] == [
        "plan.json",
        "preview/publish-preview.mp4",
        "publish-ready.mp4",
        "cover.jpg",
        "analysis-before/report.json",
        "analysis-after/report.json",
        "changes.json",
    ]


def test_needs_review_publishes_output_without_claiming_completion(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    events: list[str] = []
    pipeline, _, _ = _pipeline(
        tmp_path,
        verification_status=VerificationStatus.NEEDS_REVIEW,
        progress=events.append,
    )
    preparation = pipeline.prepare(source)

    result = pipeline.execute(
        preparation,
        confirmed_plan_digest=preparation.plan.plan_digest,
    )

    assert result.status == "needs_review"
    assert result.video_path.is_file()
    assert result.technical_report.verification.status is (
        VerificationStatus.NEEDS_REVIEW
    )
    assert events[-1] == "needs_review"


def test_raced_destination_is_not_overwritten_during_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _source(tmp_path)
    pipeline, _, executor = _pipeline(tmp_path)
    preparation = pipeline.prepare(source)
    original_move = PublishReadyPipeline._rename_directory_no_replace

    def race_destination(workspace: Path, output: Path) -> None:
        output.mkdir()
        (output / "user-owned.txt").write_text("keep", encoding="utf-8")
        original_move(workspace, output)

    monkeypatch.setattr(
        PublishReadyPipeline,
        "_rename_directory_no_replace",
        staticmethod(race_destination),
    )

    with pytest.raises(PublishArtifactError, match="already exists"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert (preparation.output_directory / "user-owned.txt").read_text(
        encoding="utf-8"
    ) == "keep"
    assert executor.execute_calls


def _install_fake_windows_directory_rename(
    monkeypatch: pytest.MonkeyPatch,
    move_file_ex: Callable[[str, str, int], int],
    *,
    error_code: int,
) -> None:
    class FakeKernel32:
        @staticmethod
        def MoveFileExW(source: str, destination: str, flags: int) -> int:
            return move_file_ex(source, destination, flags)

    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: FakeKernel32(),
        raising=False,
    )
    monkeypatch.setattr(
        ctypes,
        "get_last_error",
        lambda: error_code,
        raising=False,
    )


def test_windows_no_replace_directory_rename_retries_access_denied_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "staging"
    output = tmp_path / "published"
    workspace.mkdir()
    calls: list[tuple[str, str, int]] = []
    delays: list[float] = []

    def move_file_ex(source: str, destination: str, flags: int) -> int:
        calls.append((source, destination, flags))
        if len(calls) == 1:
            return 0
        os.rename(source, destination)
        return 1

    _install_fake_windows_directory_rename(
        monkeypatch,
        move_file_ex,
        error_code=5,
    )
    monkeypatch.setattr(time, "sleep", delays.append)

    PublishReadyPipeline._rename_directory_no_replace(workspace, output)

    assert len(calls) == 2
    assert delays == [0.01]
    assert output.is_dir()
    assert not workspace.exists()


def test_windows_no_replace_directory_rename_stops_after_retry_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "staging"
    output = tmp_path / "published"
    workspace.mkdir()
    calls = 0
    delays: list[float] = []

    def move_file_ex(_source: str, _destination: str, _flags: int) -> int:
        nonlocal calls
        calls += 1
        return 0

    _install_fake_windows_directory_rename(
        monkeypatch,
        move_file_ex,
        error_code=5,
    )
    monkeypatch.setattr(time, "sleep", delays.append)

    with pytest.raises(OSError) as captured:
        PublishReadyPipeline._rename_directory_no_replace(workspace, output)

    assert captured.value.errno == 5
    assert calls == 6
    assert delays == [0.01, 0.02, 0.04, 0.08, 0.16]
    assert workspace.is_dir()
    assert not output.exists()


@pytest.mark.parametrize("error_code", [32, 80, 183])  # type: ignore[untyped-decorator]
def test_windows_no_replace_directory_rename_does_not_retry_other_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    workspace = tmp_path / "staging"
    output = tmp_path / "published"
    workspace.mkdir()
    calls = 0
    delays: list[float] = []

    def move_file_ex(_source: str, _destination: str, _flags: int) -> int:
        nonlocal calls
        calls += 1
        return 0

    _install_fake_windows_directory_rename(
        monkeypatch,
        move_file_ex,
        error_code=error_code,
    )
    monkeypatch.setattr(time, "sleep", delays.append)

    expected_error = FileExistsError if error_code in {80, 183} else OSError
    with pytest.raises(expected_error):
        PublishReadyPipeline._rename_directory_no_replace(workspace, output)

    assert calls == 1
    assert delays == []


@pytest.mark.parametrize("changed_path", ["source", "destination"])  # type: ignore[untyped-decorator]
def test_windows_no_replace_directory_rename_requires_stable_path_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_path: str,
) -> None:
    workspace = tmp_path / "staging"
    output = tmp_path / "published"
    workspace.mkdir()
    calls = 0
    delays: list[float] = []

    def move_file_ex(_source: str, _destination: str, _flags: int) -> int:
        nonlocal calls
        calls += 1
        if changed_path == "source":
            workspace.rmdir()
        else:
            output.mkdir()
        return 0

    _install_fake_windows_directory_rename(
        monkeypatch,
        move_file_ex,
        error_code=5,
    )
    monkeypatch.setattr(time, "sleep", delays.append)

    with pytest.raises(OSError):
        PublishReadyPipeline._rename_directory_no_replace(workspace, output)

    assert calls == 1
    assert delays == []


def test_cancellation_after_verification_keeps_artifacts_unpublished(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    cancelled = False
    events: list[str] = []

    def mark_cancelled() -> None:
        nonlocal cancelled
        cancelled = True

    pipeline, _, _ = _pipeline(
        tmp_path,
        progress=events.append,
        is_cancelled=lambda: cancelled,
        verifier=CancellingVerifier(mark_cancelled),
    )
    preparation = pipeline.prepare(source)

    with pytest.raises(PublishCancelledError):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert not preparation.output_directory.exists()
    assert not preparation.workspace_directory.exists()
    assert events[-1] == "cancelled"


def test_terminal_progress_callback_failure_does_not_retract_publication(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    events: list[str] = []

    def progress(status: str) -> None:
        events.append(status)
        if status == "completed":
            raise RuntimeError("observer unavailable")

    pipeline, _, _ = _pipeline(tmp_path, progress=progress)
    preparation = pipeline.prepare(source)

    result = pipeline.execute(
        preparation,
        confirmed_plan_digest=preparation.plan.plan_digest,
    )

    assert result.status == "completed"
    assert result.video_path.is_file()
    assert events[-1] == "completed"


def test_failed_verification_never_publishes_video_or_output_root(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    events: list[str] = []
    pipeline, _, _ = _pipeline(
        tmp_path,
        verification_status=VerificationStatus.FAILED,
        progress=events.append,
    )
    preparation = pipeline.prepare(source)

    with pytest.raises(PublishArtifactError, match="failed verification"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert not preparation.output_directory.exists()
    assert not preparation.workspace_directory.exists()
    assert events[-1] == "failed"


def test_keep_workspace_records_failed_verification_without_publication(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    pipeline, _, _ = _pipeline(
        tmp_path,
        keep_workspace=True,
        verification_status=VerificationStatus.FAILED,
    )
    preparation = pipeline.prepare(source)

    with pytest.raises(PublishArtifactError, match="failed verification"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    technical_report = preparation.workspace_directory / "technical-report.json"
    assert technical_report.is_file()
    assert '"status": "failed"' in technical_report.read_text(encoding="utf-8")
    assert not preparation.output_directory.exists()


def test_analysis_processing_failure_is_mapped_and_cleans_workspace(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    events: list[str] = []
    pipeline, analysis, _ = _pipeline(
        tmp_path,
        analysis_failures=(AnalysisProcessingError("private details"),),
        progress=events.append,
    )

    with pytest.raises(PublishMediaError, match="analysis") as error:
        pipeline.prepare(source)

    workspace = analysis.created_directories[0].parent
    assert str(source.resolve()) not in str(error.value)
    assert not workspace.exists()
    assert not (tmp_path / "发布 output").exists()
    assert events[-1] == "failed"


def test_unexpected_analysis_failure_is_an_artifact_error(tmp_path: Path) -> None:
    source, _ = _source(tmp_path)
    pipeline, analysis, _ = _pipeline(
        tmp_path,
        analysis_failures=(AnalysisInternalError("private internals"),),
    )

    with pytest.raises(PublishArtifactError, match="analysis"):
        pipeline.prepare(source)

    assert not analysis.created_directories[0].parent.exists()


def test_analysis_cancellation_uses_cancelled_terminal_state(tmp_path: Path) -> None:
    source, _ = _source(tmp_path)
    events: list[str] = []
    pipeline, analysis, _ = _pipeline(
        tmp_path,
        analysis_failures=(AnalysisCancelledError("cancelled"),),
        progress=events.append,
    )

    with pytest.raises(PublishCancelledError):
        pipeline.prepare(source)

    assert events[-1] == "cancelled"
    assert not analysis.created_directories[0].parent.exists()


def test_executor_failure_cleans_half_created_product(tmp_path: Path) -> None:
    source, original = _source(tmp_path)
    executor = FakeExecutor(execute_failure=PublishMediaError("fake FFmpeg failure"))
    pipeline, _, _ = _pipeline(tmp_path, executor=executor)
    preparation = pipeline.prepare(source)

    with pytest.raises(PublishMediaError, match="fake FFmpeg"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert not preparation.workspace_directory.exists()
    assert not preparation.output_directory.exists()
    assert source.read_bytes() == original


def test_cleanup_failure_is_reported_without_deleting_the_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _ = _source(tmp_path)
    executor = FakeExecutor(execute_failure=PublishMediaError("fake FFmpeg failure"))
    pipeline, _, _ = _pipeline(tmp_path, executor=executor)
    preparation = pipeline.prepare(source)

    def fail_cleanup(path: Path) -> None:
        raise OSError(f"cannot remove {path.name}")

    monkeypatch.setattr(
        PublishReadyPipeline,
        "_remove_tree",
        staticmethod(fail_cleanup),
    )

    with pytest.raises(PublishArtifactError, match="cleanup"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert preparation.workspace_directory.is_dir()
    assert not preparation.output_directory.exists()


def test_cancellation_cleans_unpublished_workspace(tmp_path: Path) -> None:
    source, _ = _source(tmp_path)
    cancelled = False
    events: list[str] = []
    pipeline, _, executor = _pipeline(
        tmp_path,
        progress=events.append,
        is_cancelled=lambda: cancelled,
    )
    preparation = pipeline.prepare(source)
    cancelled = True

    with pytest.raises(PublishCancelledError):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert executor.execute_calls == []
    assert not preparation.workspace_directory.exists()
    assert not preparation.output_directory.exists()
    assert events[-1] == "cancelled"


def test_keep_workspace_preserves_failed_unpublished_work(tmp_path: Path) -> None:
    source, _ = _source(tmp_path)
    executor = FakeExecutor(execute_failure=PublishMediaError("fake FFmpeg failure"))
    pipeline, _, _ = _pipeline(
        tmp_path,
        keep_workspace=True,
        executor=executor,
    )
    preparation = pipeline.prepare(source)

    with pytest.raises(PublishMediaError):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert preparation.workspace_directory.is_dir()
    assert preparation.plan_path.is_file()
    assert not preparation.output_directory.exists()


def test_output_collision_after_prepare_preserves_existing_directory(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    pipeline, _, executor = _pipeline(tmp_path)
    preparation = pipeline.prepare(source)
    preparation.output_directory.mkdir()
    marker = preparation.output_directory / "existing.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(PublishArtifactError, match="already exists"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert marker.read_text(encoding="utf-8") == "user data"
    assert executor.execute_calls == []


def test_output_analysis_input_error_is_reported_as_media_failure(
    tmp_path: Path,
) -> None:
    source, _ = _source(tmp_path)
    pipeline, _, _ = _pipeline(
        tmp_path,
        analysis_failures=(None, AnalysisInputError("output probe failed")),
    )
    preparation = pipeline.prepare(source)

    with pytest.raises(PublishMediaError, match="output analysis"):
        pipeline.execute(
            preparation,
            confirmed_plan_digest=preparation.plan.plan_digest,
        )

    assert not preparation.output_directory.exists()
