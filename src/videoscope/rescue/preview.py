"""Private, same-range local preview construction for Rescue review."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from videoscope.processes import pinned_subprocess_options
from videoscope.rescue.commands import (
    build_preview_commands,
    previewed_improvement_action_ids,
)
from videoscope.rescue.errors import RescueMediaError
from videoscope.rescue.models import RescueAction, RescueActionKind, RescuePlan
from videoscope.rescue.timeline import (
    SourceMapping,
    mappings_for_ranges,
    preview_source_mappings,
)

_IMPROVEMENT_ACTION_KINDS = frozenset(
    {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.SHARPEN,
        RescueActionKind.DEFLICKER,
        RescueActionKind.STABILIZE,
        RescueActionKind.NORMALIZE_AUDIO,
        RescueActionKind.DENOISE_AUDIO,
    }
)
_EMPTY_RETAINED_PREVIEW_REASON = (
    "A selected preview window retained no media; confirmation requires a "
    "different representative window."
)


class PreviewRunner(Protocol):
    """The intentionally narrow external-process boundary for tests and execution."""

    def run(self, command: list[str]) -> None: ...


class SubprocessPreviewRunner:
    """Run a pre-built argument vector without a shell or network access."""

    def __init__(self, timeout_seconds: float = 60.0) -> None:
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
            raise RescueMediaError("ffmpeg executable was not found") from exc
        except subprocess.TimeoutExpired as exc:
            raise RescueMediaError("ffmpeg preview command timed out") from exc
        if completed.returncode != 0:
            raise RescueMediaError("ffmpeg preview command failed")


@dataclass(frozen=True, slots=True)
class RescuePreviewVariant:
    """Private artifact paths and their exact source time ranges."""

    variant: str
    time_ranges: tuple[tuple[float, float], ...]
    paths: tuple[Path, ...]
    source_mappings: tuple[SourceMapping, ...] = ()


@dataclass(frozen=True, slots=True)
class RescuePreviewSet:
    """Comparable private review previews; improved is optional by design."""

    source: RescuePreviewVariant
    faithful: RescuePreviewVariant
    improved: RescuePreviewVariant | None
    previewed_action_ids: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()

    def all_paths(self) -> tuple[Path, ...]:
        paths = self.source.paths + self.faithful.paths
        return paths if self.improved is None else paths + self.improved.paths


class RescuePreviewBuilder:
    """Build bounded local previews without writing to the source media path."""

    def __init__(self, runner: PreviewRunner | None = None) -> None:
        self._runner = runner or SubprocessPreviewRunner()

    def build(
        self,
        plan: RescuePlan,
        source: Path,
        private_review_root: Path,
    ) -> RescuePreviewSet:
        """Execute only plan-selected ranges with non-identifying artifact names."""
        private_review_root.mkdir(parents=True, exist_ok=True)
        commands = build_preview_commands(plan, source, private_review_root)
        for command in commands:
            self._runner.run(command)
        retained_previews = tuple(
            (index, window, mappings)
            for index, window in enumerate(plan.preview_ranges)
            if (
                mappings := preview_source_mappings(
                    plan, window, f"faithful-{index:02d}.mp4"
                )
            )
        )
        ranges = tuple(window for _index, window, _mappings in retained_previews)
        source_paths = tuple(
            private_review_root / f"source-{index:02d}.mp4"
            for index, _window, _mappings in retained_previews
        )
        faithful_paths = tuple(
            private_review_root / f"faithful-{index:02d}.mp4"
            for index, _window, _mappings in retained_previews
        )
        source_mappings = tuple(
            mapping
            for window, path in zip(ranges, source_paths, strict=True)
            for mapping in mappings_for_ranges((window,), path.name)
        )
        faithful_mappings = tuple(
            mapping
            for _index, _window, mappings in retained_previews
            for mapping in mappings
        )
        previewed_improvement_ids = frozenset(
            action_id
            for _index, _window, mappings in retained_previews
            for action_id in previewed_improvement_action_ids(plan, mappings)
        )
        source_variant = RescuePreviewVariant(
            "source", ranges, source_paths, source_mappings
        )
        faithful_variant = RescuePreviewVariant(
            "faithful", ranges, faithful_paths, faithful_mappings
        )
        improved_paths = tuple(
            private_review_root / f"improved-{index:02d}.mp4"
            for index, _window, _mappings in retained_previews
        )
        improved_mappings = tuple(
            mapping
            for window, path in zip(ranges, improved_paths, strict=True)
            for mapping in preview_source_mappings(plan, window, path.name)
        )
        improved = (
            RescuePreviewVariant("improved", ranges, improved_paths, improved_mappings)
            if improved_paths and len(commands) == len(retained_previews) * 3
            else None
        )
        previewed_action_ids = tuple(
            action.id
            for action in plan.actions
            if action.requires_confirmation
            and _action_is_previewed(
                action,
                ranges,
                previewed_improvement_ids,
                improved is not None,
            )
        )
        return RescuePreviewSet(
            source_variant,
            faithful_variant,
            improved,
            previewed_action_ids,
            (
                (_EMPTY_RETAINED_PREVIEW_REASON,)
                if len(retained_previews) != len(plan.preview_ranges)
                else ()
            ),
        )


def _action_is_previewed(
    action: RescueAction,
    ranges: tuple[tuple[float, float], ...],
    previewed_improvement_ids: frozenset[str],
    has_improved_variant: bool,
) -> bool:
    if action.kind in _IMPROVEMENT_ACTION_KINDS:
        return has_improved_variant and action.id in previewed_improvement_ids
    return any(
        action_start < preview_end and preview_start < action_end
        for action_start, action_end in action.source_ranges
        for preview_start, preview_end in ranges
    )


__all__ = [
    "PreviewRunner",
    "RescuePreviewBuilder",
    "RescuePreviewSet",
    "RescuePreviewVariant",
    "SubprocessPreviewRunner",
]
