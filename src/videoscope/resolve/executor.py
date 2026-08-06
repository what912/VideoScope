"""Native shell-free Publish Ready execution."""

from __future__ import annotations

import math
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from videoscope.resolve.commands import (
    build_cover_arguments,
    build_preview_arguments,
    build_publish_arguments,
)
from videoscope.resolve.errors import (
    PublishArtifactError,
    PublishCancelledError,
    PublishInputError,
    PublishMediaError,
)
from videoscope.resolve.models import PublishPlan
from videoscope.video.errors import sanitize_diagnostic

DEFAULT_PUBLISH_TIMEOUT_SECONDS = 3600.0
_FINAL_VIDEO_NAME = "publish-ready.mp4"
_PARTIAL_VIDEO_NAME = "publish-ready.partial.mp4"
_COVER_NAME = "cover.jpg"
_PARTIAL_COVER_NAME = "cover.partial.jpg"
_PREVIEW_RELATIVE_PATH = Path("preview") / "publish-preview.mp4"
_PARTIAL_PREVIEW_NAME = "publish-preview.partial.mp4"


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stderr_summary: str


class ExternalCommandRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> CommandResult: ...


@dataclass(frozen=True)
class NativePublishResult:
    video_path: Path
    cover_path: Path


def run_external_command(
    arguments: tuple[str, ...],
    *,
    timeout_seconds: float,
    sensitive_paths: tuple[Path, ...],
) -> CommandResult:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    diagnostic_paths = _include_absolute_executable(arguments, sensitive_paths)
    try:
        completed = subprocess.run(
            list(arguments),
            shell=False,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        diagnostic = sanitize_diagnostic(str(exc), sensitive_paths=diagnostic_paths)
        raise PublishMediaError(
            "Required FFmpeg executable was not found",
            stderr_summary=diagnostic,
        ) from exc
    except subprocess.TimeoutExpired as exc:
        diagnostic = sanitize_diagnostic(
            _as_diagnostic_text(exc.stderr) or _timeout_diagnostic(arguments),
            sensitive_paths=diagnostic_paths,
        )
        raise PublishMediaError(
            "FFmpeg processing timed out",
            stderr_summary=diagnostic,
        ) from exc
    except OSError as exc:
        diagnostic = sanitize_diagnostic(str(exc), sensitive_paths=diagnostic_paths)
        raise PublishMediaError(
            "FFmpeg could not be started",
            stderr_summary=diagnostic,
        ) from exc

    return CommandResult(
        returncode=completed.returncode,
        stderr_summary=sanitize_diagnostic(
            completed.stderr,
            sensitive_paths=diagnostic_paths,
        ),
    )


def _include_absolute_executable(
    arguments: tuple[str, ...],
    sensitive_paths: tuple[Path, ...],
) -> tuple[Path, ...]:
    if not arguments:
        return sensitive_paths
    executable = Path(arguments[0])
    if executable.is_absolute():
        return (*sensitive_paths, executable)
    return sensitive_paths


def _timeout_diagnostic(arguments: tuple[str, ...]) -> str:
    executable = arguments[0] if arguments else "configured command"
    return f"Timed out while running {executable}"


def _as_diagnostic_text(value: str | bytes | None) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _never_cancelled() -> bool:
    return False


class NativePublishExecutor:
    def __init__(
        self,
        *,
        runner: ExternalCommandRunner = run_external_command,
        ffmpeg: str = "ffmpeg",
        timeout_seconds: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
        preview_seconds: float = 6.0,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if not ffmpeg:
            raise ValueError("ffmpeg cannot be empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        if not math.isfinite(preview_seconds) or not 0 < preview_seconds <= 10:
            raise ValueError("preview_seconds must be finite and between zero and ten")
        self._runner = runner
        self._ffmpeg = ffmpeg
        self._timeout_seconds = timeout_seconds
        self._preview_seconds = preview_seconds
        self._is_cancelled = is_cancelled or _never_cancelled

    def generate_preview(
        self, plan: PublishPlan, source_path: Path, work_directory: Path
    ) -> Path:
        source = Path(source_path)
        work = Path(work_directory)
        self._validate_source(source)
        preview = work / _PREVIEW_RELATIVE_PATH
        partial = preview.parent / _PARTIAL_PREVIEW_NAME
        self._reject_source_collisions(source, (preview, partial))
        succeeded = False
        try:
            self._make_directory(preview.parent)
            self._discard(partial)
            arguments = build_preview_arguments(
                plan,
                source,
                partial,
                ffmpeg=self._ffmpeg,
                preview_seconds=self._preview_seconds,
            )
            self._run(
                arguments,
                stage="preview",
                sensitive_paths=(source, work),
            )
            self._require_nonempty(partial, stage="preview")
            self._replace_artifact(partial, preview, stage="preview")
            succeeded = True
            return preview
        finally:
            if not succeeded:
                self._discard(partial)

    def execute(
        self, plan: PublishPlan, source_path: Path, work_directory: Path
    ) -> NativePublishResult:
        source = Path(source_path)
        work = Path(work_directory)
        self._validate_source(source)
        final_video = work / plan.output_filename
        if plan.output_filename != _FINAL_VIDEO_NAME:
            raise PublishInputError(
                "PublishPlan output filename must be publish-ready.mp4"
            )
        if self._same_resolved_path(source, final_video):
            raise PublishInputError("Source and publish output cannot be the same file")

        partial_video = work / _PARTIAL_VIDEO_NAME
        final_cover = work / _COVER_NAME
        partial_cover = work / _PARTIAL_COVER_NAME
        self._reject_source_collisions(
            source,
            (final_video, partial_video, final_cover, partial_cover),
        )
        cover_published = False
        video_published = False
        succeeded = False
        try:
            self._make_directory(work)
            self._discard(partial_video)
            self._discard(partial_cover)
            self._run(
                build_publish_arguments(
                    plan,
                    source,
                    partial_video,
                    ffmpeg=self._ffmpeg,
                ),
                stage="publish output",
                sensitive_paths=(source, work),
            )
            self._require_nonempty(partial_video, stage="publish output")
            self._run(
                build_cover_arguments(
                    partial_video,
                    partial_cover,
                    duration_seconds=plan.source_metadata.duration_seconds,
                    ffmpeg=self._ffmpeg,
                ),
                stage="cover",
                sensitive_paths=(source, work),
            )
            self._require_nonempty(partial_cover, stage="cover")
            self._replace_artifact(partial_cover, final_cover, stage="cover")
            cover_published = True
            self._replace_artifact(
                partial_video,
                final_video,
                stage="publish output",
            )
            video_published = True
            succeeded = True
            return NativePublishResult(
                video_path=final_video,
                cover_path=final_cover,
            )
        finally:
            if not succeeded:
                self._discard(partial_video)
                self._discard(partial_cover)
                if cover_published:
                    self._discard(final_cover)
                if video_published:
                    self._discard(final_video)

    def _run(
        self,
        arguments: tuple[str, ...],
        *,
        stage: str,
        sensitive_paths: tuple[Path, ...],
    ) -> None:
        if self._is_cancelled():
            raise PublishCancelledError("Publish Ready processing was cancelled")
        result = self._runner(
            arguments,
            timeout_seconds=self._timeout_seconds,
            sensitive_paths=sensitive_paths,
        )
        if result.returncode != 0:
            raise PublishMediaError(
                f"FFmpeg could not create the {stage}",
                stderr_summary=result.stderr_summary,
            )

    @staticmethod
    def _validate_source(source: Path) -> None:
        if not source.is_file():
            raise PublishInputError(f"Source video was not found: {source.name}")

    @staticmethod
    def _same_resolved_path(first: Path, second: Path) -> bool:
        try:
            return first.resolve(strict=False) == second.resolve(strict=False)
        except OSError as exc:
            raise PublishInputError(
                "Source or output path could not be resolved"
            ) from exc

    @classmethod
    def _reject_source_collisions(
        cls,
        source: Path,
        outputs: tuple[Path, ...],
    ) -> None:
        if any(cls._same_resolved_path(source, output) for output in outputs):
            raise PublishInputError("Source and publish output cannot be the same file")

    @staticmethod
    def _make_directory(path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PublishArtifactError(
                "Publish Ready work directory could not be created"
            ) from exc

    @staticmethod
    def _replace_artifact(source: Path, destination: Path, *, stage: str) -> None:
        try:
            source.replace(destination)
        except OSError as exc:
            raise PublishArtifactError(
                f"The {stage} artifact could not be published"
            ) from exc

    @staticmethod
    def _require_nonempty(path: Path, *, stage: str) -> None:
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                raise PublishMediaError(
                    f"FFmpeg did not create a non-empty {stage} artifact"
                )
        except OSError as exc:
            raise PublishArtifactError(
                f"Could not inspect the {stage} artifact"
            ) from exc

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "CommandResult",
    "DEFAULT_PUBLISH_TIMEOUT_SECONDS",
    "ExternalCommandRunner",
    "NativePublishExecutor",
    "NativePublishResult",
    "run_external_command",
]
