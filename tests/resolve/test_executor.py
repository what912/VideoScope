"""Tests for safe native Publish Ready execution."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from videoscope.domain import VideoMetadata
from videoscope.resolve.errors import (
    PublishArtifactError,
    PublishCancelledError,
    PublishInputError,
    PublishMediaError,
)
from videoscope.resolve.executor import (
    CommandResult,
    NativePublishExecutor,
    run_external_command,
)
from videoscope.resolve.models import PublishPlan, PublishProfileId
from videoscope.resolve.planner import build_publish_plan


def _plan(*, duration_seconds: float = 20.0) -> PublishPlan:
    metadata = VideoMetadata(
        filename="source.mp4",
        container_format="matroska",
        codec="vp9",
        width=640,
        height=360,
        duration_seconds=duration_seconds,
        average_frame_rate=30.0,
        estimated_frame_count=int(duration_seconds * 30),
        has_audio=True,
        file_size_bytes=1024,
        raw_probe={"pixel_format": "yuv444p", "audio_codec": "opus"},
    )
    return build_publish_plan(
        metadata,
        "b" * 64,
        PublishProfileId.SOCIAL_HORIZONTAL,
    )


class WritingRunner:
    def __init__(self, *, fail_at: int | None = None, empty_at: int | None = None):
        self.calls: list[tuple[str, ...]] = []
        self.fail_at = fail_at
        self.empty_at = empty_at

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> CommandResult:
        del timeout_seconds, sensitive_paths
        self.calls.append(arguments)
        call_number = len(self.calls)
        if call_number == self.fail_at:
            return CommandResult(returncode=7, stderr_summary="sanitized failure")
        output = Path(arguments[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"" if call_number == self.empty_at else b"media")
        return CommandResult(returncode=0, stderr_summary="no diagnostic output")


def test_preview_is_generated_atomically_at_public_relative_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "源 视频.mp4"
    source.write_bytes(b"source")
    runner = WritingRunner()
    executor = NativePublishExecutor(runner=runner, timeout_seconds=45.0)

    preview = executor.generate_preview(_plan(), source, tmp_path / "任务 输出")

    assert preview.relative_to(tmp_path / "任务 输出").as_posix() == (
        "preview/publish-preview.mp4"
    )
    assert preview.read_bytes() == b"media"
    assert not (preview.parent / "publish-preview.partial.mp4").exists()
    assert len(runner.calls) == 1


def test_execute_publishes_video_and_cover_only_after_both_are_nonempty(
    tmp_path: Path,
) -> None:
    source = tmp_path / "input with spaces 中文.mp4"
    source.write_bytes(b"original source bytes")
    original = source.read_bytes()
    runner = WritingRunner()
    executor = NativePublishExecutor(runner=runner)
    work_directory = tmp_path / "任务 output"

    result = executor.execute(_plan(), source, work_directory)

    assert result.video_path == work_directory / "publish-ready.mp4"
    assert result.cover_path == work_directory / "cover.jpg"
    assert result.video_path.read_bytes() == b"media"
    assert result.cover_path.read_bytes() == b"media"
    assert source.read_bytes() == original
    assert [Path(call[-1]).name for call in runner.calls] == [
        "publish-ready.partial.mp4",
        "cover.partial.jpg",
    ]
    assert not list(work_directory.glob("*.partial.*"))


@pytest.mark.parametrize("fail_at", [1, 2])  # type: ignore[untyped-decorator]
def test_command_failure_cleans_untrusted_outputs(tmp_path: Path, fail_at: int) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_directory = tmp_path / "work"
    executor = NativePublishExecutor(runner=WritingRunner(fail_at=fail_at))

    with pytest.raises(PublishMediaError) as error:
        executor.execute(_plan(), source, work_directory)

    assert error.value.stderr_summary == "sanitized failure"
    assert not (work_directory / "publish-ready.mp4").exists()
    assert not (work_directory / "publish-ready.partial.mp4").exists()
    assert not (work_directory / "cover.jpg").exists()
    assert not (work_directory / "cover.partial.jpg").exists()


def test_empty_ffmpeg_output_is_rejected_and_cleaned(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_directory = tmp_path / "work"

    with pytest.raises(PublishMediaError, match="non-empty"):
        NativePublishExecutor(runner=WritingRunner(empty_at=1)).execute(
            _plan(), source, work_directory
        )

    assert not any(work_directory.glob("publish-ready*"))


def test_cancellation_before_each_command_cleans_partial_files(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    runner = WritingRunner()
    checks = 0

    def cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks == 2

    work_directory = tmp_path / "work"
    executor = NativePublishExecutor(runner=runner, is_cancelled=cancelled)

    with pytest.raises(PublishCancelledError):
        executor.execute(_plan(), source, work_directory)

    assert len(runner.calls) == 1
    assert not any(work_directory.glob("publish-ready*"))
    assert not any(work_directory.glob("cover*"))


def test_source_cannot_be_the_final_output(tmp_path: Path) -> None:
    source = tmp_path / "publish-ready.mp4"
    source.write_bytes(b"source")
    runner = WritingRunner()

    with pytest.raises(PublishInputError, match="same file"):
        NativePublishExecutor(runner=runner).execute(_plan(), source, tmp_path)

    assert runner.calls == []
    assert source.read_bytes() == b"source"


def test_source_cannot_be_overwritten_by_preview(tmp_path: Path) -> None:
    source = tmp_path / "work" / "preview" / "publish-preview.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    runner = WritingRunner()

    with pytest.raises(PublishInputError, match="same file"):
        NativePublishExecutor(runner=runner).generate_preview(
            _plan(), source, tmp_path / "work"
        )

    assert runner.calls == []
    assert source.read_bytes() == b"source"


def test_source_cannot_be_overwritten_by_cover(tmp_path: Path) -> None:
    source = tmp_path / "work" / "cover.jpg"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"source")
    runner = WritingRunner()

    with pytest.raises(PublishInputError, match="same file"):
        NativePublishExecutor(runner=runner).execute(_plan(), source, tmp_path / "work")

    assert runner.calls == []
    assert source.read_bytes() == b"source"


def test_work_directory_error_is_structured_without_absolute_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_directory = tmp_path / "not a directory"
    work_directory.write_bytes(b"blocker")

    with pytest.raises(PublishArtifactError) as error:
        NativePublishExecutor(runner=WritingRunner()).execute(
            _plan(), source, work_directory
        )

    assert str(work_directory) not in str(error.value)


def test_runner_uses_shell_false_and_sanitizes_bounded_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "秘密 输入.mp4"
    calls: list[dict[str, object]] = []

    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append({"arguments": arguments, **kwargs})
        return subprocess.CompletedProcess(
            arguments,
            9,
            stdout="ignored stdout",
            stderr=f"failed at {source} " + ("x" * 3000),
        )

    monkeypatch.setattr("videoscope.resolve.executor.subprocess.run", fake_run)

    result = run_external_command(
        ("ffmpeg", "-i", str(source)),
        timeout_seconds=12.0,
        sensitive_paths=(source,),
    )

    assert result.returncode == 9
    assert "<input>" in result.stderr_summary
    assert str(source) not in result.stderr_summary
    assert len(result.stderr_summary) <= 2003
    assert calls == [
        {
            "arguments": ["ffmpeg", "-i", str(source)],
            "shell": False,
            "check": False,
            "capture_output": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 12.0,
        }
    ]


def test_runner_timeout_sanitizes_exception_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "private source.mp4"

    def time_out(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        raise subprocess.TimeoutExpired(
            arguments,
            1.0,
            stderr=f"timeout while reading {source}",
        )

    monkeypatch.setattr("videoscope.resolve.executor.subprocess.run", time_out)

    with pytest.raises(PublishMediaError) as error:
        run_external_command(
            ("ffmpeg", "-i", str(source)),
            timeout_seconds=1.0,
            sensitive_paths=(source,),
        )

    assert error.value.stderr_summary is not None
    assert "<input>" in error.value.stderr_summary
    assert str(source) not in error.value.stderr_summary


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "interruption",
    [KeyboardInterrupt, SystemExit],
)
def test_base_interrupt_is_propagated_and_cleans_partial_outputs(
    tmp_path: Path,
    interruption: type[BaseException],
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_directory = tmp_path / "work"

    def interrupt(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> CommandResult:
        del timeout_seconds, sensitive_paths
        output = Path(arguments[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"partial media")
        raise interruption

    with pytest.raises(interruption):
        NativePublishExecutor(runner=interrupt).execute(_plan(), source, work_directory)

    assert not (work_directory / "publish-ready.partial.mp4").exists()
    assert not (work_directory / "publish-ready.mp4").exists()
    assert not (work_directory / "cover.partial.jpg").exists()
    assert not (work_directory / "cover.jpg").exists()


def test_preview_interrupt_is_propagated_and_cleans_partial_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_directory = tmp_path / "work"

    def interrupt(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> CommandResult:
        del timeout_seconds, sensitive_paths
        output = Path(arguments[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"partial preview")
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        NativePublishExecutor(runner=interrupt).generate_preview(
            _plan(), source, work_directory
        )

    preview_directory = work_directory / "preview"
    assert not (preview_directory / "publish-preview.partial.mp4").exists()
    assert not (preview_directory / "publish-preview.mp4").exists()


def test_keyboard_interrupt_after_cover_publication_cleans_side_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    work_directory = tmp_path / "work"
    executor = NativePublishExecutor(runner=WritingRunner())
    original_replace = executor._replace_artifact

    def interrupt_final_replace(partial: Path, final: Path, *, stage: str) -> None:
        if stage == "publish output":
            raise KeyboardInterrupt
        original_replace(partial, final, stage=stage)

    monkeypatch.setattr(executor, "_replace_artifact", interrupt_final_replace)

    with pytest.raises(KeyboardInterrupt):
        executor.execute(_plan(), source, work_directory)

    assert not (work_directory / "publish-ready.partial.mp4").exists()
    assert not (work_directory / "publish-ready.mp4").exists()
    assert not (work_directory / "cover.partial.jpg").exists()
    assert not (work_directory / "cover.jpg").exists()


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "failure_kind",
    ["missing", "timeout", "startup"],
)
def test_runner_sanitizes_absolute_ffmpeg_path_for_start_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure_kind: str,
) -> None:
    private_ffmpeg = tmp_path / "private tools" / "ffmpeg-secret.exe"

    def fail_to_start(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        if failure_kind == "missing":
            raise FileNotFoundError(f"missing executable {private_ffmpeg}")
        if failure_kind == "timeout":
            raise subprocess.TimeoutExpired(arguments, 1.0)
        raise OSError(f"could not start executable {private_ffmpeg}")

    monkeypatch.setattr("videoscope.resolve.executor.subprocess.run", fail_to_start)

    with pytest.raises(PublishMediaError) as error:
        run_external_command(
            (str(private_ffmpeg), "-version"),
            timeout_seconds=1.0,
            sensitive_paths=(),
        )

    assert error.value.stderr_summary is not None
    assert "<input>" in error.value.stderr_summary
    assert str(private_ffmpeg) not in error.value.stderr_summary
