"""Confirmed native execution into a verification-only pending tree."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from videoscope.content.commands import (
    build_chapter_mux_command,
    build_content_clip_command,
    build_content_concat_command,
    build_content_duration_probe_command,
    build_content_segment_command,
)
from videoscope.content.errors import (
    ContentCancelledError,
    ContentConfirmationError,
    ContentMediaError,
)
from videoscope.content.models import (
    ContentActionExecution,
    ContentActionKind,
    ContentConfirmation,
    ContentExecutionStatus,
    ContentMappingState,
    ContentPlan,
    ContentSourceMapping,
    ContentTimeRange,
    ContentTransition,
    StoryboardItem,
    make_mapping_id,
)
from videoscope.content.preview import RetainedContentSource
from videoscope.processes import pinned_subprocess_options
from videoscope.video.errors import sanitize_diagnostic

DEFAULT_CONTENT_TIMEOUT_SECONDS = 3600.0


@dataclass(frozen=True, slots=True)
class ContentCommandResult:
    returncode: int
    stdout: str = ""
    stderr_summary: str = ""


class ContentCommandRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> ContentCommandResult: ...


@dataclass(frozen=True, slots=True)
class NativeContentResult:
    pending_root: Path
    video_path: Path
    source_map_path: Path
    source_mappings: tuple[ContentSourceMapping, ...]
    clip_paths: tuple[Path, ...]
    action_executions: tuple[ContentActionExecution, ...]
    source_hash_before: str
    source_hash_after: str


def run_content_command(
    arguments: tuple[str, ...],
    *,
    timeout_seconds: float,
    sensitive_paths: tuple[Path, ...],
) -> ContentCommandResult:
    """Run a bounded local media command and sanitize diagnostics."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    try:
        completed = subprocess.run(
            list(arguments),
            check=False,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            **pinned_subprocess_options(arguments),
        )
    except FileNotFoundError as exc:
        raise ContentMediaError("required media executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContentMediaError("native media command timed out") from exc
    except OSError as exc:
        raise ContentMediaError("native media command could not start") from exc
    return ContentCommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr_summary=sanitize_diagnostic(
            completed.stderr,
            sensitive_paths=sensitive_paths,
        ),
    )


def probe_content_duration(
    path: Path,
    *,
    runner: ContentCommandRunner = run_content_command,
    ffprobe: str = "ffprobe",
    timeout_seconds: float = 60.0,
    sensitive_paths: tuple[Path, ...] = (),
) -> float:
    """Measure one encoded artifact duration independently with ffprobe."""
    result = runner(
        tuple(build_content_duration_probe_command(path, ffprobe=ffprobe)),
        timeout_seconds=timeout_seconds,
        sensitive_paths=(*sensitive_paths, path),
    )
    if result.returncode != 0:
        raise ContentMediaError("duration probe failed")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise ContentMediaError("duration probe returned invalid data") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ContentMediaError("duration probe returned an invalid duration")
    return duration


class NativeContentExecutor:
    """Render only a confirmed plan, without publishing pending artifacts."""

    def __init__(
        self,
        *,
        runner: ContentCommandRunner = run_content_command,
        duration_probe: Callable[[Path], float] | None = None,
        ffmpeg: str = "ffmpeg",
        timeout_seconds: float = DEFAULT_CONTENT_TIMEOUT_SECONDS,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if not ffmpeg:
            raise ValueError("ffmpeg executable cannot be empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        self._runner = runner
        self._duration_probe = duration_probe
        self._ffmpeg = ffmpeg
        self._timeout_seconds = timeout_seconds
        self._is_cancelled = is_cancelled or (lambda: False)

    def execute(
        self,
        *,
        plan: ContentPlan,
        confirmation: ContentConfirmation,
        source: RetainedContentSource,
        transcript_hash: str | None,
        work_root: Path,
        has_audio: bool,
    ) -> NativeContentResult:
        self._validate_confirmation(plan, confirmation, source, transcript_hash)
        pending_root = Path(work_root) / "content-pending"
        _require_pending_root(pending_root)
        if pending_root.exists():
            raise ContentMediaError("content pending tree already exists")
        segment_root = pending_root / "segments"
        clip_root = pending_root / "clips"
        segment_root.mkdir(parents=True, exist_ok=False)
        succeeded = False
        source_hash_before = source.source_hash
        try:
            kept = tuple(
                sorted(
                    (
                        item
                        for item in plan.storyboard.items
                        if item.output_order_index is not None
                    ),
                    key=lambda item: (
                        item.output_order_index
                        if item.output_order_index is not None
                        else -1
                    ),
                )
            )
            if not kept:
                raise ContentConfirmationError("confirmed storyboard has no output")
            self._validate_output_order(plan, kept)
            segment_paths: list[Path] = []
            measured_durations: list[float] = []
            for index, item in enumerate(kept):
                self._check_cancelled()
                segment_path = segment_root / f"segment-{index:04d}.mp4"
                fade = self._confirmed_fade(plan, item)
                self._run(
                    build_content_segment_command(
                        source.input_path,
                        segment_path,
                        item.source_range,
                        has_audio=has_audio,
                        audio_fade_seconds=fade,
                        ffmpeg=self._ffmpeg,
                    ),
                    source=source.input_path,
                    work_root=work_root,
                )
                _require_nonempty(segment_path)
                segment_paths.append(segment_path)
                measured_durations.append(self._measure_duration(segment_path))

            joined_path = pending_root / "useful-content.joined.mp4"
            self._run(
                build_content_concat_command(
                    segment_paths,
                    joined_path,
                    has_audio=has_audio,
                    ffmpeg=self._ffmpeg,
                ),
                source=source.input_path,
                work_root=work_root,
            )
            _require_nonempty(joined_path)
            final_video = pending_root / "useful-content.mp4"
            if plan.storyboard.chapters:
                metadata_path = pending_root / "chapters.ffmeta"
                _write_chapter_metadata(plan, metadata_path)
                self._run(
                    build_chapter_mux_command(
                        joined_path,
                        metadata_path,
                        final_video,
                        ffmpeg=self._ffmpeg,
                    ),
                    source=source.input_path,
                    work_root=work_root,
                )
                _require_nonempty(final_video)
            else:
                joined_path.replace(final_video)

            clip_paths = self._export_clips(
                plan,
                kept,
                source,
                clip_root,
                work_root,
                has_audio=has_audio,
            )
            mappings = _measured_mappings(
                plan,
                kept,
                tuple(measured_durations),
            )
            source_map_path = pending_root / "source-map.json"
            _write_source_map(mappings, source_map_path)
            source.verify_unchanged()
            source_hash_after = source.source_hash
            executions = tuple(
                ContentActionExecution(
                    action_id=action.id,
                    status=ContentExecutionStatus.SUCCEEDED,
                )
                for action in plan.actions
            )
            succeeded = True
            return NativeContentResult(
                pending_root=pending_root,
                video_path=final_video,
                source_map_path=source_map_path,
                source_mappings=mappings,
                clip_paths=clip_paths,
                action_executions=executions,
                source_hash_before=source_hash_before,
                source_hash_after=source_hash_after,
            )
        finally:
            if not succeeded and pending_root.exists():
                shutil.rmtree(pending_root)

    def _export_clips(
        self,
        plan: ContentPlan,
        kept: Sequence[StoryboardItem],
        source: RetainedContentSource,
        clip_root: Path,
        work_root: Path,
        *,
        has_audio: bool,
    ) -> tuple[Path, ...]:
        if not plan.effective_config.export_clips:
            return ()
        clip_root.mkdir(parents=True, exist_ok=False)
        paths: list[Path] = []
        for index, item in enumerate(kept):
            self._check_cancelled()
            path = clip_root / f"clip-{index + 1:04d}.mp4"
            self._run(
                build_content_clip_command(
                    source.input_path,
                    path,
                    item.source_range,
                    has_audio=has_audio,
                    ffmpeg=self._ffmpeg,
                ),
                source=source.input_path,
                work_root=work_root,
            )
            _require_nonempty(path)
            paths.append(path)
        return tuple(paths)

    def _run(self, command: list[str], *, source: Path, work_root: Path) -> None:
        self._check_cancelled()
        result = self._runner(
            tuple(command),
            timeout_seconds=self._timeout_seconds,
            sensitive_paths=(source, work_root),
        )
        if result.returncode != 0:
            raise ContentMediaError(
                f"native content command failed: {result.stderr_summary}"
            )

    def _measure_duration(self, path: Path) -> float:
        if self._duration_probe is not None:
            value = self._duration_probe(path)
        else:
            value = probe_content_duration(
                path,
                runner=self._runner,
                timeout_seconds=min(self._timeout_seconds, 60.0),
                sensitive_paths=(path.parent,),
            )
        if not math.isfinite(value) or value <= 0:
            raise ContentMediaError("measured segment duration is invalid")
        return value

    def _check_cancelled(self) -> None:
        if self._is_cancelled():
            raise ContentCancelledError("content execution cancelled")

    @staticmethod
    def _confirmed_fade(plan: ContentPlan, item: StoryboardItem) -> float:
        fade_actions = tuple(
            action
            for action in plan.actions
            if action.kind is ContentActionKind.APPLY_AUDIO_FADE
            and item.source_range in action.source_ranges
        )
        if not fade_actions:
            return 0.0
        return plan.effective_config.audio_fade_seconds

    @staticmethod
    def _validate_confirmation(
        plan: ContentPlan,
        confirmation: ContentConfirmation,
        source: RetainedContentSource,
        transcript_hash: str | None,
    ) -> None:
        if source.source_hash != plan.input_hash:
            raise ContentConfirmationError("retained source hash is stale")
        if transcript_hash != plan.transcript_hash:
            raise ContentConfirmationError("transcript hash is stale")
        try:
            plan.validate_confirmation(confirmation)
        except ValueError as exc:
            raise ContentConfirmationError(str(exc)) from exc

    @staticmethod
    def _validate_output_order(
        plan: ContentPlan,
        kept: Sequence[StoryboardItem],
    ) -> None:
        source_indices = tuple(item.source_order_index for item in kept)
        if not plan.storyboard.reorder_acknowledged and source_indices != tuple(
            sorted(source_indices)
        ):
            raise ContentConfirmationError("source order changed without confirmation")
        if plan.storyboard.reorder_acknowledged and not confirmation_reorders(plan):
            raise ContentConfirmationError("reorder acknowledgement has no reorder")


def confirmation_reorders(plan: ContentPlan) -> bool:
    kept = sorted(
        (item for item in plan.storyboard.items if item.output_order_index is not None),
        key=lambda item: (
            item.output_order_index if item.output_order_index is not None else -1
        ),
    )
    source_indices = tuple(item.source_order_index for item in kept)
    return source_indices != tuple(sorted(source_indices))


def _measured_mappings(
    plan: ContentPlan,
    kept: Sequence[StoryboardItem],
    durations: tuple[float, ...],
) -> tuple[ContentSourceMapping, ...]:
    if len(kept) != len(durations):
        raise ContentMediaError("segment duration count does not match storyboard")
    output_cursor = 0.0
    mappings: list[ContentSourceMapping] = []
    for output_index, (item, duration) in enumerate(zip(kept, durations, strict=True)):
        output_range = ContentTimeRange(
            start_seconds=output_cursor,
            end_seconds=output_cursor + duration,
        )
        action_id = next(
            (
                action.id
                for action in plan.actions
                if action.kind is ContentActionKind.RETAIN
                and action.source_ranges == (item.source_range,)
            ),
            None,
        )
        mappings.append(
            ContentSourceMapping(
                id=make_mapping_id(
                    plan.input_hash,
                    output_range,
                    item.source_range,
                    output_index,
                ),
                output_range=output_range,
                source_range=item.source_range,
                source_order_index=item.source_order_index,
                output_order_index=output_index,
                transition=ContentTransition.HARD_JOIN,
                state=ContentMappingState.UNCHANGED,
                storyboard_item_id=item.id,
                action_id=action_id,
            )
        )
        output_cursor = output_range.end_seconds
    return tuple(mappings)


def _write_source_map(
    mappings: tuple[ContentSourceMapping, ...],
    path: Path,
) -> None:
    payload = {
        "schema_version": "0.1",
        "mappings": [item.model_dump(mode="json") for item in mappings],
    }
    _write_atomic_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _write_chapter_metadata(plan: ContentPlan, path: Path) -> None:
    lines = [";FFMETADATA1"]
    for chapter in plan.storyboard.chapters:
        lines.extend(
            (
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={round(chapter.source_range.start_seconds * 1000)}",
                f"END={round(chapter.source_range.end_seconds * 1000)}",
                f"title={_escape_metadata(chapter.title)}",
            )
        )
    _write_atomic_text(path, "\n".join(lines) + "\n")


def _write_atomic_text(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _escape_metadata(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _require_pending_root(path: Path) -> None:
    if path.name != "content-pending":
        raise ContentMediaError("content execution must use the pending tree")


def _require_nonempty(path: Path) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise ContentMediaError("native media command produced no artifact")


__all__ = [
    "ContentCommandResult",
    "ContentCommandRunner",
    "NativeContentExecutor",
    "NativeContentResult",
    "confirmation_reorders",
    "probe_content_duration",
    "run_content_command",
]
