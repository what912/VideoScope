"""Staged, shell-free native execution for Safe Sharing."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from pydantic import JsonValue

from videoscope.privacy.artifacts import PrivacyArtifactLayout
from videoscope.privacy.commands import (
    build_privacy_audio_arguments,
    build_privacy_preview_arguments,
    build_privacy_remux_arguments,
)
from videoscope.privacy.errors import (
    PrivacyArtifactError,
    PrivacyCancelledError,
    PrivacyMediaError,
)
from videoscope.privacy.models import (
    PrivacyActionKind,
    PrivacyArtifact,
    PrivacyChangeLog,
    PrivacyPlan,
)
from videoscope.privacy.renderer import VisualRedactionRenderer, VisualRenderResult
from videoscope.privacy.serialization import write_privacy_change_log_json
from videoscope.video.errors import sanitize_diagnostic
from videoscope.video.probe import probe_video

DEFAULT_PRIVACY_TIMEOUT_SECONDS = 3600.0
_FINAL_VIDEO_NAME = "share-safe.mp4"
_CHANGES_NAME = "changes.json"
_VISUAL_STAGE_NAME = "visual-redacted.mp4"
_AUDIO_STAGE_NAME = "audio-muted.mp4"
_FINAL_STAGE_NAME = "share-safe.partial.mp4"
_COMPLETE_PACKAGE_FILES = frozenset(
    {
        "changes.json",
        "manifest.json",
        "privacy-summary.json",
        "share-safe.mp4",
        "technical-report.json",
        "verification.json",
    }
)
_PENDING_PREFIX = "pending-package-"


@dataclass(frozen=True)
class CommandResult:
    """Sanitized result from one external command."""

    returncode: int
    stderr_summary: str
    stdout_summary: str = ""


class ExternalCommandRunner(Protocol):
    """Shell-free external command boundary."""

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> CommandResult: ...


class PrivacyVisualRenderer(Protocol):
    """Streaming visual renderer consumed by the staged executor."""

    def render(
        self,
        source: Path,
        output: Path,
        plan: PrivacyPlan,
        cancellation: Callable[[], bool],
    ) -> VisualRenderResult: ...


CandidateProbe = Callable[[Path], object]
BeforePublishHook = Callable[[Path, Path], None]


@dataclass(frozen=True)
class PrivacyNativeResult:
    """Staged or published artifacts from one confirmed Safe Sharing plan."""

    staged_video: Path
    change_log: PrivacyChangeLog
    pending_root: Path | None = None


def run_external_command(
    arguments: tuple[str, ...],
    *,
    timeout_seconds: float,
    sensitive_paths: tuple[Path, ...],
) -> CommandResult:
    """Run one bounded argument array without exposing local paths."""
    if not arguments:
        raise ValueError("external command arguments cannot be empty")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    diagnostic_paths = sensitive_paths
    executable = Path(arguments[0])
    if executable.is_absolute():
        diagnostic_paths = (*diagnostic_paths, executable)
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
        raise PrivacyMediaError("required FFmpeg executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise PrivacyMediaError("FFmpeg privacy processing timed out") from exc
    except OSError as exc:
        raise PrivacyMediaError("FFmpeg privacy processing could not start") from exc
    return CommandResult(
        returncode=completed.returncode,
        stderr_summary=sanitize_diagnostic(
            completed.stderr,
            sensitive_paths=diagnostic_paths,
        ),
        stdout_summary=sanitize_diagnostic(
            completed.stdout,
            sensitive_paths=diagnostic_paths,
        ),
    )


class NativePrivacyExecutor:
    """Execute one immutable reviewed plan without modifying the source."""

    def __init__(
        self,
        *,
        renderer: PrivacyVisualRenderer | None = None,
        runner: ExternalCommandRunner = run_external_command,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        timeout_seconds: float = DEFAULT_PRIVACY_TIMEOUT_SECONDS,
        ffmpeg_version: str | None = None,
        candidate_probe: CandidateProbe | None = None,
        before_publish: BeforePublishHook | None = None,
    ) -> None:
        if not ffmpeg:
            raise ValueError("ffmpeg cannot be empty")
        if not ffprobe:
            raise ValueError("ffprobe cannot be empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        self._renderer = renderer or VisualRedactionRenderer(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
        )
        self._runner = runner
        self._ffmpeg = ffmpeg
        self._timeout_seconds = timeout_seconds
        self._ffmpeg_version = ffmpeg_version
        self._candidate_probe = candidate_probe or (
            lambda candidate: probe_video(candidate, ffprobe=ffprobe)
        )
        self._before_publish = before_publish or (lambda pending, public: None)

    def preview(
        self,
        plan: PrivacyPlan,
        source: Path,
        output: Path,
        cancellation: Callable[[], bool],
    ) -> Path:
        """Create one bounded private preview without a publishable package."""
        source = Path(source)
        output = Path(output)
        self._validate_source(plan, source)
        if source.resolve(strict=False) == output.resolve(strict=False):
            raise PrivacyArtifactError("Safe Sharing preview cannot overwrite source")
        if output.exists() or output.is_symlink():
            raise PrivacyArtifactError("Safe Sharing refuses to overwrite preview")
        output.parent.mkdir(parents=True, exist_ok=True)
        stage_root = Path(tempfile.mkdtemp(prefix="preview-stage-", dir=output.parent))
        succeeded = False
        try:
            self._check_cancelled(cancellation)
            bounded_source = stage_root / "bounded-source.mp4"
            self._run(
                build_privacy_preview_arguments(
                    plan,
                    source,
                    bounded_source,
                    ffmpeg=self._ffmpeg,
                ),
                source=source,
                workspace=output.parent,
                stage="bounded preview",
            )
            self._require_nonempty(bounded_source, stage="bounded preview")
            candidate = bounded_source
            if self._has_visual_actions(plan):
                self._check_cancelled(cancellation)
                redacted_video = stage_root / "redacted-preview-video.mp4"
                self._renderer.render(
                    bounded_source,
                    redacted_video,
                    plan,
                    cancellation,
                )
                self._require_nonempty(
                    redacted_video,
                    stage="preview visual redaction",
                )
                candidate = stage_root / "private-preview.mp4"
                self._run(
                    build_privacy_preview_arguments(
                        plan,
                        bounded_source,
                        candidate,
                        ffmpeg=self._ffmpeg,
                        video_source=redacted_video,
                    ),
                    source=source,
                    workspace=output.parent,
                    stage="preview audio merge",
                )
                self._require_nonempty(candidate, stage="preview audio merge")
            self._check_cancelled(cancellation)
            self._probe_candidate(candidate)
            self._require_source_unchanged(plan, source)
            _replace_new(candidate, output)
            succeeded = True
            return output
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)
            if not succeeded and (output.exists() or output.is_symlink()):
                try:
                    output.unlink()
                except OSError:
                    pass

    def execute(
        self,
        plan: PrivacyPlan,
        source: Path,
        workspace: Path,
        cancellation: Callable[[], bool],
    ) -> PrivacyNativeResult:
        """Render a private pending package without publishing it."""
        source = Path(source)
        self._validate_source(plan, source)
        layout = PrivacyArtifactLayout.create(workspace)
        self._require_empty_public_root(layout.public_root)

        staging_root = Path(
            tempfile.mkdtemp(prefix="staging-", dir=layout.private_root)
        )
        pending_root: Path | None = None
        succeeded = False
        executed_stages: list[str] = []
        try:
            self._check_cancelled(cancellation)
            current_media = source
            if self._has_visual_actions(plan):
                visual_output = staging_root / _VISUAL_STAGE_NAME
                self._renderer.render(
                    source,
                    visual_output,
                    plan,
                    cancellation,
                )
                self._require_nonempty(visual_output, stage="visual redaction")
                current_media = visual_output
                executed_stages.append("visual_redaction")

            if self._has_audio_actions(plan):
                self._check_cancelled(cancellation)
                audio_output = staging_root / _AUDIO_STAGE_NAME
                self._run(
                    build_privacy_audio_arguments(
                        plan,
                        current_media,
                        audio_output,
                        ffmpeg=self._ffmpeg,
                        audio_source=source if current_media != source else None,
                    ),
                    source=source,
                    workspace=layout.job_root,
                    stage="audio muting",
                )
                self._require_nonempty(audio_output, stage="audio muting")
                current_media = audio_output
                executed_stages.append("audio_mute")

            self._check_cancelled(cancellation)
            staged_video = staging_root / _FINAL_STAGE_NAME
            self._run(
                build_privacy_remux_arguments(
                    plan,
                    current_media,
                    staged_video,
                    ffmpeg=self._ffmpeg,
                ),
                source=source,
                workspace=layout.job_root,
                stage="metadata-free remux",
            )
            self._require_nonempty(staged_video, stage="metadata-free remux")
            self._probe_candidate(staged_video)
            executed_stages.append("metadata_free_remux")
            artifact = PrivacyArtifact(
                relative_path=_FINAL_VIDEO_NAME,
                sha256=_sha256_file(staged_video),
                description="Locally reviewed privacy-safe sharing copy",
            )
            change_log = PrivacyChangeLog(
                plan_digest=plan.digest,
                source_modified=False,
                processor={
                    "executable": Path(self._ffmpeg).name,
                    "ffmpeg_version": self._effective_ffmpeg_version(),
                    "execution_order": [
                        cast(JsonValue, stage) for stage in executed_stages
                    ],
                },
                actions=plan.actions,
                artifacts=(artifact,),
            )
            staged_changes = staging_root / _CHANGES_NAME
            write_privacy_change_log_json(change_log, staged_changes)
            layout.validate_share_manifest(change_log.model_dump(mode="json"))

            pending_root = Path(
                tempfile.mkdtemp(
                    prefix=_PENDING_PREFIX,
                    dir=layout.private_root,
                )
            )
            pending_video = pending_root / _FINAL_VIDEO_NAME
            pending_changes = pending_root / _CHANGES_NAME
            _replace_new(staged_video, pending_video)
            _replace_new(staged_changes, pending_changes)
            pending_layout = PrivacyArtifactLayout(
                job_root=layout.job_root,
                private_root=layout.private_root,
                public_root=pending_root.resolve(strict=True),
            )
            pending_layout.validate_public_tree()
            self._check_cancelled(cancellation)
            self._require_source_unchanged(plan, source)
            retained_pending = pending_root
            result = PrivacyNativeResult(
                staged_video=pending_video,
                change_log=change_log,
                pending_root=retained_pending,
            )
            pending_root = None
            succeeded = True
            return result
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            if pending_root is not None:
                shutil.rmtree(pending_root, ignore_errors=True)
            if not succeeded and not layout.public_root.exists():
                layout.public_root.mkdir(exist_ok=True)

    def publish_pending(
        self,
        pending_root: Path,
        plan: PrivacyPlan,
        source: Path,
        workspace: Path,
        cancellation: Callable[[], bool],
    ) -> Path:
        """Validate and publish one retained package with a single directory swap."""
        source = Path(source)
        self._validate_source(plan, source)
        layout = PrivacyArtifactLayout.create(workspace)
        self._require_empty_public_root(layout.public_root)
        pending = Path(pending_root)
        try:
            resolved_pending = pending.resolve(strict=True)
            if (
                resolved_pending.parent != layout.private_root
                or not resolved_pending.name.startswith(_PENDING_PREFIX)
                or not resolved_pending.is_dir()
            ):
                raise PrivacyArtifactError("pending Safe Sharing package is invalid")
        except PrivacyArtifactError:
            raise
        except OSError as exc:
            raise PrivacyArtifactError(
                "pending Safe Sharing package could not be inspected"
            ) from exc
        pending_layout = PrivacyArtifactLayout(
            job_root=layout.job_root,
            private_root=layout.private_root,
            public_root=resolved_pending,
        )
        self._require_complete_package(
            pending_layout,
            plan,
        )
        self._before_publish(resolved_pending, layout.public_root)
        self._check_cancelled(cancellation)
        self._require_source_unchanged(plan, source)
        _publish_pending_package(resolved_pending, layout.public_root)
        return Path(layout.public_root) / _FINAL_VIDEO_NAME

    @staticmethod
    def _require_complete_package(
        pending_layout: PrivacyArtifactLayout,
        plan: PrivacyPlan,
    ) -> None:
        files = set(pending_layout.validate_public_tree())
        try:
            entries = tuple(pending_layout.public_root.iterdir())
        except OSError as exc:
            raise PrivacyArtifactError(
                "pending Safe Sharing package could not be inspected"
            ) from exc
        if (
            files != _COMPLETE_PACKAGE_FILES
            or {entry.name for entry in entries} != _COMPLETE_PACKAGE_FILES
            or any(not entry.is_file() for entry in entries)
        ):
            raise PrivacyArtifactError(
                "pending Safe Sharing package is not the exact complete package"
            )
        try:
            expected_candidate_sha256 = _sha256_file(
                pending_layout.public_root / _FINAL_VIDEO_NAME
            )
        except OSError as exc:
            raise PrivacyArtifactError(
                "pending Safe Sharing candidate could not be inspected"
            ) from exc
        for name in (
            "changes.json",
            "manifest.json",
            "privacy-summary.json",
            "technical-report.json",
            "verification.json",
        ):
            try:
                payload = json.loads(
                    (pending_layout.public_root / name).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise PrivacyArtifactError(
                    "pending Safe Sharing report is invalid"
                ) from exc
            if (
                not isinstance(payload, dict)
                or payload.get("plan_digest") != plan.digest
            ):
                raise PrivacyArtifactError(
                    "pending Safe Sharing report does not bind the confirmed plan"
                )
            if name == "manifest.json":
                artifacts = payload.get("artifacts")
                if not isinstance(artifacts, list) or not any(
                    isinstance(artifact, dict)
                    and artifact.get("relative_path") == _FINAL_VIDEO_NAME
                    and artifact.get("sha256") == expected_candidate_sha256
                    for artifact in artifacts
                ):
                    raise PrivacyArtifactError(
                        "pending Safe Sharing manifest does not bind the candidate"
                    )

    def _run(
        self,
        arguments: list[str],
        *,
        source: Path,
        workspace: Path,
        stage: str,
    ) -> None:
        result = self._runner(
            tuple(arguments),
            timeout_seconds=self._timeout_seconds,
            sensitive_paths=(source, workspace),
        )
        if result.returncode != 0:
            raise PrivacyMediaError(
                f"FFmpeg failed during {stage}: {result.stderr_summary}"
            )

    def _effective_ffmpeg_version(self) -> str:
        if self._ffmpeg_version is not None:
            return self._ffmpeg_version
        if self._runner is not run_external_command:
            return "injected command runner"
        result = self._runner(
            (self._ffmpeg, "-version"),
            timeout_seconds=min(self._timeout_seconds, 30.0),
            sensitive_paths=(),
        )
        if result.returncode != 0:
            return "unavailable"
        first_line = result.stdout_summary.splitlines()
        return first_line[0] if first_line else "unavailable"

    def _probe_candidate(self, candidate: Path) -> None:
        try:
            self._candidate_probe(candidate)
        except (KeyboardInterrupt, SystemExit):
            raise
        except PrivacyMediaError:
            raise
        except Exception as exc:
            raise PrivacyMediaError(
                "staged privacy candidate could not be probed"
            ) from exc

    @staticmethod
    def _validate_source(plan: PrivacyPlan, source: Path) -> None:
        try:
            if not source.is_file():
                raise PrivacyArtifactError("Safe Sharing source is not a file")
            actual_hash = _sha256_file(source)
        except PrivacyArtifactError:
            raise
        except OSError as exc:
            raise PrivacyArtifactError("Safe Sharing source could not be read") from exc
        if actual_hash != plan.input_hash:
            raise PrivacyArtifactError("confirmed plan does not match source content")

    @staticmethod
    def _require_source_unchanged(plan: PrivacyPlan, source: Path) -> None:
        try:
            current_hash = _sha256_file(source)
        except OSError as exc:
            raise PrivacyArtifactError(
                "Safe Sharing source could not be rechecked"
            ) from exc
        if current_hash != plan.input_hash:
            raise PrivacyArtifactError(
                "Safe Sharing source changed during local processing"
            )

    @staticmethod
    def _require_empty_public_root(public_root: Path) -> None:
        try:
            if not public_root.is_dir() or any(public_root.iterdir()):
                raise PrivacyArtifactError(
                    "Safe Sharing refuses to overwrite public artifacts"
                )
        except PrivacyArtifactError:
            raise
        except OSError as exc:
            raise PrivacyArtifactError(
                "Safe Sharing public root could not be inspected"
            ) from exc

    @staticmethod
    def _require_nonempty(path: Path, *, stage: str) -> None:
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                raise PrivacyMediaError(f"{stage} did not produce non-empty media")
        except OSError as exc:
            raise PrivacyArtifactError(
                f"{stage} output could not be inspected"
            ) from exc

    @staticmethod
    def _check_cancelled(cancellation: Callable[[], bool]) -> None:
        if cancellation():
            raise PrivacyCancelledError("Safe Sharing execution was cancelled")

    @staticmethod
    def _has_visual_actions(plan: PrivacyPlan) -> bool:
        return any(
            action.kind in {PrivacyActionKind.CROP, PrivacyActionKind.VISUAL_REDACTION}
            for action in plan.actions
        )

    @staticmethod
    def _has_audio_actions(plan: PrivacyPlan) -> bool:
        return any(
            action.kind is PrivacyActionKind.AUDIO_MUTE for action in plan.actions
        )


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _replace_new(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise PrivacyArtifactError("Safe Sharing refuses to overwrite artifacts")
    try:
        source.replace(destination)
    except OSError as exc:
        raise PrivacyArtifactError(
            "Safe Sharing artifact could not be published"
        ) from exc


def _publish_pending_package(pending: Path, public_root: Path) -> None:
    """Publish one already validated complete directory with a single rename."""
    try:
        if any(public_root.iterdir()):
            raise PrivacyArtifactError(
                "Safe Sharing refuses to overwrite public artifacts"
            )
        public_root.rmdir()
        os.replace(pending, public_root)
    except PrivacyArtifactError:
        raise
    except OSError as exc:
        raise PrivacyArtifactError(
            "Safe Sharing package could not be published atomically"
        ) from exc


__all__ = [
    "CommandResult",
    "DEFAULT_PRIVACY_TIMEOUT_SECONDS",
    "ExternalCommandRunner",
    "NativePrivacyExecutor",
    "PrivacyNativeResult",
    "run_external_command",
]
