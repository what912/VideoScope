"""Private, same-range local preview construction for Rescue review."""

from __future__ import annotations

import math
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from videoscope.processes import pinned_subprocess_options
from videoscope.rescue.action_roles import (
    action_artifact_role,
    faithful_restoration_action_ids,
    remaining_improvement_action_ids,
)
from videoscope.rescue.commands import (
    build_preview_commands,
    previewed_improvement_action_ids,
)
from videoscope.rescue.deblur import render_deblurred_video
from videoscope.rescue.errors import RescueMediaError
from videoscope.rescue.executor import (
    ExternalCommandRunner,
    _deblur_operations,
    _stabilization_operation,
    _tonal_operation,
    run_external_command,
)
from videoscope.rescue.models import (
    RescueAction,
    RescueActionKind,
    RescuePlan,
    validate_plan_video_encode_contracts,
    validate_rescue_plan_identity_contract,
)
from videoscope.rescue.qualification import (
    validate_plan_sharpen_output_range_contracts,
)
from videoscope.rescue.stabilization import render_stabilized_video
from videoscope.rescue.timeline import (
    SourceMapping,
    mappings_for_ranges,
    preview_source_mappings,
    retained_source_ranges,
)
from videoscope.rescue.tonal import render_tonal_interference_reduced_audio

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

    def __init__(
        self,
        runner: PreviewRunner | None = None,
        *,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
        deblur_renderer: Callable[..., None] = render_deblurred_video,
        tonal_renderer: Callable[..., None] = render_tonal_interference_reduced_audio,
        stabilization_renderer: Callable[..., None] = render_stabilized_video,
        native_runner: ExternalCommandRunner = run_external_command,
        native_timeout_seconds: float = 60.0,
    ) -> None:
        if not math.isfinite(native_timeout_seconds) or native_timeout_seconds <= 0:
            raise ValueError("native_timeout_seconds must be finite and positive")
        self._runner = runner or SubprocessPreviewRunner()
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._deblur_renderer = deblur_renderer
        self._tonal_renderer = tonal_renderer
        self._stabilization_renderer = stabilization_renderer
        self._native_runner = native_runner
        self._native_timeout_seconds = native_timeout_seconds

    def build(
        self,
        plan: RescuePlan,
        source: Path,
        private_review_root: Path,
        *,
        cancellation_callback: Callable[[], bool] | None = None,
    ) -> RescuePreviewSet:
        """Execute only plan-selected ranges with non-identifying artifact names."""
        try:
            validate_plan_video_encode_contracts(plan)
            validate_rescue_plan_identity_contract(plan)
            validate_plan_sharpen_output_range_contracts(
                plan,
                mappings_for_ranges(
                    retained_source_ranges(plan), "faithful-rescue.mp4"
                ),
            )
        except ValueError as exc:
            raise RescueMediaError(
                "confirmed preview video encode contract is invalid"
            ) from exc
        private_review_root.mkdir(parents=True, exist_ok=True)
        commands = build_preview_commands(plan, source, private_review_root)
        retained_previews = tuple(
            (index, window, mappings)
            for index, window in enumerate(plan.preview_ranges)
            if (
                mappings := preview_source_mappings(
                    plan, window, f"faithful-{index:02d}.mp4"
                )
            )
        )
        mappings_by_faithful_name = {
            f"faithful-{index:02d}.mp4": mappings
            for index, _window, mappings in retained_previews
        }
        improved_command_names = frozenset(
            Path(command[-1]).name
            for command in commands
            if Path(command[-1]).name.startswith("improved-")
        )
        rendered_native_by_faithful_name: dict[str, frozenset[str]] = {}
        active_cancellation_callback = cancellation_callback or (lambda: False)
        try:
            for command in commands:
                self._runner.run(command)
                output = Path(command[-1])
                output_mappings = mappings_by_faithful_name.get(output.name)
                if output_mappings is not None:
                    rendered_native_by_faithful_name[output.name] = (
                        self._apply_native_restoration_preview(
                            plan,
                            output_mappings,
                            output,
                            active_cancellation_callback,
                        )
                    )
        except Exception:
            for command in commands:
                Path(command[-1]).unlink(missing_ok=True)
            raise
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
        faithful_action_ids = faithful_restoration_action_ids(plan)
        improved_action_ids = remaining_improvement_action_ids(plan)
        rendered_faithful_action_ids = frozenset(
            action_id
            for index, _window, mappings in retained_previews
            for action_id in previewed_improvement_action_ids(
                plan,
                mappings,
                rendered_native_action_ids=rendered_native_by_faithful_name.get(
                    f"faithful-{index:02d}.mp4", frozenset()
                ),
                included_action_ids=faithful_action_ids,
            )
        )
        rendered_improved_action_ids = frozenset(
            action_id
            for index, _window, mappings in retained_previews
            if f"improved-{index:02d}.mp4" in improved_command_names
            for action_id in previewed_improvement_action_ids(
                plan,
                mappings,
                included_action_ids=improved_action_ids,
            )
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
                rendered_faithful_action_ids,
                faithful_mappings,
                rendered_improved_action_ids,
                improved_mappings if improved is not None else (),
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

    def _apply_native_restoration_preview(
        self,
        plan: RescuePlan,
        mappings: tuple[SourceMapping, ...],
        target_path: Path,
        cancellation_callback: Callable[[], bool],
    ) -> frozenset[str]:
        source_ranges = tuple(
            (mapping.source_start, mapping.source_end) for mapping in mappings
        )
        rendered_action_ids: set[str] = set()
        for kind in (
            RescueActionKind.DEBLUR,
            RescueActionKind.DENOISE_AUDIO,
            RescueActionKind.STABILIZE,
        ):
            selected = tuple(
                action
                for action in plan.actions
                if action.kind is kind
                and _ranges_intersect(action.source_ranges, source_ranges)
            )
            if not selected:
                continue
            action = selected[0]
            temporary = target_path.with_name(
                f".{target_path.name}.{kind.value}.partial.mp4"
            )
            if temporary.exists() or temporary.is_symlink():
                raise RescueMediaError("private restoration preview path is reserved")
            try:
                if kind is RescueActionKind.DEBLUR:
                    self._render_deblur_preview(
                        plan,
                        action,
                        mappings,
                        target_path,
                        temporary,
                        cancellation_callback,
                    )
                elif kind is RescueActionKind.DENOISE_AUDIO:
                    if "interference_profiles" not in action.parameters:
                        continue
                    self._render_tonal_preview(
                        plan,
                        action,
                        mappings,
                        target_path,
                        temporary,
                        cancellation_callback,
                    )
                else:
                    self._render_anchor_preview(
                        plan,
                        action,
                        mappings,
                        target_path,
                        temporary,
                        cancellation_callback,
                    )
                if not temporary.is_file():
                    raise RescueMediaError(
                        "private restoration preview was not created"
                    )
                os.replace(temporary, target_path)
                rendered_action_ids.add(action.id)
            finally:
                temporary.unlink(missing_ok=True)
        return frozenset(rendered_action_ids)

    def _render_deblur_preview(
        self,
        plan: RescuePlan,
        action: RescueAction,
        mappings: tuple[SourceMapping, ...],
        source: Path,
        output: Path,
        cancellation_callback: Callable[[], bool],
    ) -> None:
        operations = _deblur_operations(
            action.parameters,
            action.source_ranges,
            mappings,
            expected_version=plan.effective_config.deblur_algorithm_version,
        )
        intermediate_paths: list[Path] = []
        candidate = source
        try:
            for index, (ranges, estimate, config) in enumerate(operations):
                operation_output = (
                    output
                    if index == len(operations) - 1
                    else output.with_name(
                        f".{output.name}.deblur-{index:02d}.partial.mp4"
                    )
                )
                if operation_output.exists() or operation_output.is_symlink():
                    raise RescueMediaError("private deblur preview path is reserved")
                if operation_output != output:
                    intermediate_paths.append(operation_output)
                self._deblur_renderer(
                    candidate,
                    operation_output,
                    ranges,
                    estimate,
                    config,
                    ffmpeg_path=Path(self._ffmpeg),
                    ffprobe_path=Path(self._ffprobe),
                    runner=self._native_runner,
                    cancellation_callback=cancellation_callback,
                    encode_config=plan.effective_config,
                )
                if not operation_output.is_file():
                    raise RescueMediaError("private deblur preview was not created")
                candidate = operation_output
        finally:
            for intermediate in intermediate_paths:
                intermediate.unlink(missing_ok=True)

    def _render_tonal_preview(
        self,
        plan: RescuePlan,
        action: RescueAction,
        mappings: tuple[SourceMapping, ...],
        source: Path,
        output: Path,
        cancellation_callback: Callable[[], bool],
    ) -> None:
        mapped_tones, config = _tonal_operation(
            action.parameters,
            action.source_ranges,
            mappings,
            expected_version=plan.effective_config.tonal_algorithm_version,
        )
        self._tonal_renderer(
            source,
            output,
            mapped_tones,
            config,
            ffmpeg_path=Path(self._ffmpeg),
            ffprobe_path=Path(self._ffprobe),
            runner=self._native_runner,
            cancellation_callback=cancellation_callback,
        )

    def _render_anchor_preview(
        self,
        plan: RescuePlan,
        action: RescueAction,
        mappings: tuple[SourceMapping, ...],
        source: Path,
        output: Path,
        cancellation_callback: Callable[[], bool],
    ) -> None:
        mapped, config = _stabilization_operation(
            action.parameters,
            action.source_ranges,
            mappings,
            expected_version=(
                plan.effective_config.anchor_stabilization_algorithm_version
            ),
        )
        self._stabilization_renderer(
            source,
            output,
            mapped,
            config,
            runner=self._native_runner,
            cancellation_callback=cancellation_callback,
            ffmpeg=self._ffmpeg,
            timeout_seconds=self._native_timeout_seconds,
            encode_config=plan.effective_config,
        )


def _ranges_intersect(
    left: tuple[tuple[float, float], ...], right: tuple[tuple[float, float], ...]
) -> bool:
    return any(
        start < right_end and right_start < end
        for start, end in left
        for right_start, right_end in right
    )


def _action_is_previewed(
    action: RescueAction,
    ranges: tuple[tuple[float, float], ...],
    rendered_faithful_action_ids: frozenset[str],
    faithful_mappings: tuple[SourceMapping, ...],
    rendered_improved_action_ids: frozenset[str],
    improved_mappings: tuple[SourceMapping, ...],
) -> bool:
    artifact_role = action_artifact_role(action.kind)
    if artifact_role == "faithful":
        return action.id in rendered_faithful_action_ids and _ranges_are_represented(
            action.source_ranges,
            tuple(
                (mapping.source_start, mapping.source_end)
                for mapping in faithful_mappings
            ),
        )
    if artifact_role == "improved":
        return action.id in rendered_improved_action_ids and _ranges_are_represented(
            action.source_ranges,
            tuple(
                (mapping.source_start, mapping.source_end)
                for mapping in improved_mappings
            ),
        )
    return _ranges_are_represented(action.source_ranges, ranges)


def _ranges_are_represented(
    action_ranges: tuple[tuple[float, float], ...],
    rendered_ranges: tuple[tuple[float, float], ...],
) -> bool:
    return all(
        any(
            action_start < rendered_end and rendered_start < action_end
            for rendered_start, rendered_end in rendered_ranges
        )
        for action_start, action_end in action_ranges
    )


__all__ = [
    "PreviewRunner",
    "RescuePreviewBuilder",
    "RescuePreviewSet",
    "RescuePreviewVariant",
    "SubprocessPreviewRunner",
]
