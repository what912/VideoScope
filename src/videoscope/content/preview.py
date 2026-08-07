"""Bounded private join previews and exact preview identities."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from videoscope.content.errors import (
    ContentCancelledError,
    ContentPreviewError,
)
from videoscope.content.models import (
    ContentAction,
    ContentActionKind,
    ContentTimeRange,
)
from videoscope.processes import (
    PinnedDescriptorError,
    hash_descriptor,
    pinned_descriptor_path,
    pinned_subprocess_options,
    secure_read_open,
)
from videoscope.video.hashing import compute_file_sha256

_ENCODING_PARAMETERS = {
    "video_codec": "libx264",
    "audio_codec": "aac",
    "pixel_format": "yuv420p",
    "preset": "veryfast",
}


class ContentPreviewRunner(Protocol):
    """Narrow command boundary used by the private preview builder."""

    def run(self, command: list[str]) -> None: ...


class SubprocessContentPreviewRunner:
    """Run one pre-built argument vector without a shell."""

    def __init__(self, *, timeout_seconds: float = 120.0) -> None:
        self._timeout_seconds = timeout_seconds

    def run(self, command: list[str]) -> None:
        try:
            completed = subprocess.run(
                command,
                check=False,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                **pinned_subprocess_options(command),
            )
        except FileNotFoundError as exc:
            raise ContentPreviewError("ffmpeg executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise ContentPreviewError("ffmpeg preview timed out") from exc
        if completed.returncode != 0:
            raise ContentPreviewError("ffmpeg preview command failed")


@dataclass(frozen=True, slots=True)
class ContentJoinPreview:
    """One action-bound set of private join artifacts."""

    action_id: str
    action_ranges: tuple[ContentTimeRange, ...]
    context_ranges: tuple[ContentTimeRange, ...]
    relative_paths: tuple[str, ...]
    artifact_hashes: tuple[str, ...]
    encoding_parameters: Mapping[str, str]
    identity: str


@dataclass(frozen=True, slots=True)
class PreviewAssessment:
    """Per-action preview readiness without hiding unaffected actions."""

    identities: Mapping[str, str]
    blocked_action_ids: tuple[str, ...]
    reasons: Mapping[str, str]


class RetainedContentSource:
    """Explicit ownership for one source that must not change during review."""

    def __init__(self, path: Path, *, expected_hash: str) -> None:
        self._original_path = Path(path)
        self._descriptor = -1
        try:
            self._descriptor = secure_read_open(self._original_path)
            actual_hash = hash_descriptor(self._descriptor)
        except (OSError, PinnedDescriptorError) as exc:
            self.close()
            raise ContentPreviewError("source could not be retained safely") from exc
        if actual_hash != expected_hash:
            self.close()
            raise ContentPreviewError("retained source hash does not match the plan")
        self.source_hash = actual_hash

    @property
    def closed(self) -> bool:
        return self._descriptor < 0

    @property
    def input_path(self) -> Path:
        if self.closed:
            raise ContentPreviewError("retained source is closed")
        if os.name == "posix":
            return pinned_descriptor_path(self._descriptor)
        return self._original_path

    def verify_unchanged(self) -> None:
        """Fail if the bytes retained for review no longer match their identity."""
        if self.closed:
            raise ContentPreviewError("retained source is closed")
        try:
            current_hash = hash_descriptor(self._descriptor)
        except (OSError, PinnedDescriptorError) as exc:
            raise ContentPreviewError("retained source could not be verified") from exc
        if current_hash != self.source_hash:
            raise ContentPreviewError("retained source changed after review")

    def close(self) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1

    def __enter__(self) -> RetainedContentSource:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class ContentPreviewBuilder:
    """Create bounded action previews under the private review tree only."""

    def __init__(
        self,
        runner: ContentPreviewRunner | None = None,
        *,
        ffmpeg_executable: str = "ffmpeg",
    ) -> None:
        self._runner = runner or SubprocessContentPreviewRunner()
        self._ffmpeg = ffmpeg_executable

    def build(
        self,
        *,
        source: RetainedContentSource,
        transcript_hash: str | None,
        actions: Sequence[ContentAction],
        duration_seconds: float,
        private_review_root: Path,
        maximum_preview_seconds: float,
        has_audio: bool,
        cancelled: Callable[[], bool] | None = None,
    ) -> tuple[ContentJoinPreview, ...]:
        preview_root = Path(private_review_root) / "preview"
        _require_private_preview_root(preview_root)
        preview_root.mkdir(parents=True, exist_ok=True)
        created: list[Path] = []
        previews: list[ContentJoinPreview] = []
        try:
            required = tuple(
                item
                for item in actions
                if item.changes_content and item.requires_confirmation
            )
            for action_index, action in enumerate(required):
                if cancelled is not None and cancelled():
                    raise ContentCancelledError("preview cancelled")
                contexts = preview_context_ranges(
                    action,
                    duration_seconds=duration_seconds,
                    maximum_preview_seconds=maximum_preview_seconds,
                )
                action_paths: list[Path] = []
                for context_index, context in enumerate(contexts):
                    output = preview_root / (
                        f"action-{action_index:03d}-context-{context_index:03d}.mp4"
                    )
                    created.append(output)
                    self._runner.run(
                        build_preview_extract_command(
                            self._ffmpeg,
                            source.input_path,
                            output,
                            context,
                        )
                    )
                    _require_preview_output(output)
                    action_paths.append(output)
                joined = preview_root / f"action-{action_index:03d}-joined.mp4"
                created.append(joined)
                self._runner.run(
                    build_preview_join_command(
                        self._ffmpeg,
                        action_paths,
                        joined,
                        has_audio=has_audio,
                    )
                )
                _require_preview_output(joined)
                action_paths.append(joined)
                hashes = tuple(compute_file_sha256(path) for path in action_paths)
                relative_paths = tuple(
                    path.relative_to(private_review_root).as_posix()
                    for path in action_paths
                )
                identity = make_preview_identity(
                    input_hash=source.source_hash,
                    transcript_hash=transcript_hash,
                    action=action,
                    context_ranges=contexts,
                    encoding_parameters=_ENCODING_PARAMETERS,
                    artifact_hashes=hashes,
                )
                previews.append(
                    ContentJoinPreview(
                        action_id=action.id,
                        action_ranges=action.source_ranges,
                        context_ranges=contexts,
                        relative_paths=relative_paths,
                        artifact_hashes=hashes,
                        encoding_parameters=dict(_ENCODING_PARAMETERS),
                        identity=identity,
                    )
                )
            return tuple(previews)
        except BaseException:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            if preview_root.exists() and not any(preview_root.iterdir()):
                preview_root.rmdir()
            raise


def preview_context_ranges(
    action: ContentAction,
    *,
    duration_seconds: float,
    maximum_preview_seconds: float,
) -> tuple[ContentTimeRange, ...]:
    """Select bounded source context around the joins produced by an action."""
    if duration_seconds <= 0 or maximum_preview_seconds <= 0:
        raise ContentPreviewError("preview bounds must be positive")
    if action.kind is ContentActionKind.CONCATENATE and len(action.source_ranges) == 1:
        selected = action.source_ranges[0]
        if selected.duration_seconds <= maximum_preview_seconds:
            return (selected,)
        midpoint = (selected.start_seconds + selected.end_seconds) / 2.0
        half = maximum_preview_seconds / 2.0
        return (
            ContentTimeRange(
                start_seconds=midpoint - half,
                end_seconds=midpoint + half,
            ),
        )
    joins = _join_edges(action)
    if not joins:
        raise ContentPreviewError("content-changing action has no previewable join")
    per_join = maximum_preview_seconds / len(joins)
    side_seconds = per_join / 2.0
    contexts: list[ContentTimeRange] = []
    for left_edge, right_edge in joins:
        left_start = max(0.0, left_edge - side_seconds)
        right_end = min(duration_seconds, right_edge + side_seconds)
        if left_edge > left_start:
            contexts.append(
                ContentTimeRange(start_seconds=left_start, end_seconds=left_edge)
            )
        if right_end > right_edge:
            contexts.append(
                ContentTimeRange(start_seconds=right_edge, end_seconds=right_end)
            )
    if not contexts:
        raise ContentPreviewError("content-changing action consumes the full source")
    if sum(item.duration_seconds for item in contexts) > maximum_preview_seconds + 1e-9:
        raise ContentPreviewError("preview duration exceeds configured maximum")
    return tuple(contexts)


def build_preview_extract_command(
    executable: str,
    source: Path,
    output: Path,
    source_range: ContentTimeRange,
) -> list[str]:
    """Build a bounded, path-safe FFmpeg extraction command."""
    return [
        executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        _seconds(source_range.start_seconds),
        "-i",
        str(source),
        "-t",
        _seconds(source_range.duration_seconds),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        _ENCODING_PARAMETERS["video_codec"],
        "-preset",
        _ENCODING_PARAMETERS["preset"],
        "-pix_fmt",
        _ENCODING_PARAMETERS["pixel_format"],
        "-c:a",
        _ENCODING_PARAMETERS["audio_codec"],
        "-movflags",
        "+faststart",
        "-y",
        str(output),
    ]


def build_preview_join_command(
    executable: str,
    sources: Sequence[Path],
    output: Path,
    *,
    has_audio: bool,
) -> list[str]:
    """Build a private concatenation preview from already-bounded clips."""
    if not sources:
        raise ContentPreviewError("join preview requires at least one context clip")
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error"]
    for source in sources:
        command.extend(("-i", str(source)))
    if has_audio:
        inputs = "".join(f"[{index}:v:0][{index}:a:0]" for index in range(len(sources)))
        graph = f"{inputs}concat=n={len(sources)}:v=1:a=1[v][a]"
    else:
        inputs = "".join(f"[{index}:v:0]" for index in range(len(sources)))
        graph = f"{inputs}concat=n={len(sources)}:v=1:a=0[v]"
    command.extend(
        (
            "-filter_complex",
            graph,
            "-map",
            "[v]",
        )
    )
    if has_audio:
        command.extend(("-map", "[a]", "-c:a", _ENCODING_PARAMETERS["audio_codec"]))
    else:
        command.append("-an")
    command.extend(
        (
            "-c:v",
            _ENCODING_PARAMETERS["video_codec"],
            "-preset",
            _ENCODING_PARAMETERS["preset"],
            "-pix_fmt",
            _ENCODING_PARAMETERS["pixel_format"],
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        )
    )
    return command


def make_preview_identity(
    *,
    input_hash: str,
    transcript_hash: str | None,
    action: ContentAction,
    context_ranges: tuple[ContentTimeRange, ...],
    encoding_parameters: Mapping[str, str],
    artifact_hashes: tuple[str, ...],
) -> str:
    """Bind one preview to all inputs that can change its review meaning."""
    payload = {
        "action": action.model_dump(mode="json"),
        "artifact_hashes": list(artifact_hashes),
        "context_ranges": [item.model_dump(mode="json") for item in context_ranges],
        "encoding_parameters": dict(sorted(encoding_parameters.items())),
        "input_hash": input_hash,
        "transcript_hash": transcript_hash,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "preview_" + sha256(encoded.encode("utf-8")).hexdigest()


def assess_previews(
    required_actions: Sequence[ContentAction],
    previews: Sequence[ContentJoinPreview],
) -> PreviewAssessment:
    """Report readiness per action so one failed preview does not hide peers."""
    by_action = {item.action_id: item for item in previews}
    identities: dict[str, str] = {}
    blocked: list[str] = []
    reasons: dict[str, str] = {}
    for action in required_actions:
        if not action.changes_content or not action.requires_confirmation:
            continue
        preview = by_action.get(action.id)
        if preview is None:
            blocked.append(action.id)
            reasons[action.id] = "A matching private preview is missing."
            continue
        if preview.action_ranges != action.source_ranges:
            blocked.append(action.id)
            reasons[action.id] = "The private preview uses stale action ranges."
            continue
        identities[action.id] = preview.identity
    return PreviewAssessment(
        identities=dict(sorted(identities.items())),
        blocked_action_ids=tuple(blocked),
        reasons=reasons,
    )


def clear_private_previews(private_review_root: Path) -> None:
    """Delete only the known private preview subtree."""
    preview_root = Path(private_review_root) / "preview"
    _require_private_preview_root(preview_root)
    if preview_root.exists():
        shutil.rmtree(preview_root)


def _join_edges(action: ContentAction) -> tuple[tuple[float, float], ...]:
    ranges = action.source_ranges
    if action.kind is ContentActionKind.REMOVE:
        return tuple((item.start_seconds, item.end_seconds) for item in ranges)
    if action.kind is ContentActionKind.CONCATENATE:
        return tuple(
            (left.end_seconds, right.start_seconds)
            for left, right in zip(ranges, ranges[1:])
        )
    return tuple((item.start_seconds, item.end_seconds) for item in ranges)


def _require_private_preview_root(path: Path) -> None:
    parts = path.parts
    if len(parts) < 2 or parts[-2:] != ("content-review-private", "preview"):
        raise ContentPreviewError("preview output must remain in the private tree")


def _require_preview_output(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ContentPreviewError("preview command produced no media")


def _seconds(value: float) -> str:
    return f"{value:.6f}"


__all__ = [
    "ContentJoinPreview",
    "ContentPreviewBuilder",
    "ContentPreviewRunner",
    "PreviewAssessment",
    "RetainedContentSource",
    "SubprocessContentPreviewRunner",
    "assess_previews",
    "build_preview_extract_command",
    "build_preview_join_command",
    "clear_private_previews",
    "make_preview_identity",
    "preview_context_ranges",
]
