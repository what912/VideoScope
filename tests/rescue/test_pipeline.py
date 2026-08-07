"""Lifecycle contracts for the review-gated Video Rescue pipeline."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import pytest

import videoscope.rescue.pipeline as rescue_pipeline_module
from videoscope.domain import VideoMetadata
from videoscope.rescue.artifacts import publish_verified_rescue
from videoscope.rescue.assessment import RescueAssessmentBundle, RescueAssessmentWarning
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueConfirmationError,
    RescueInputError,
    RescueMediaError,
    RescuePlanError,
    RescueScanError,
)
from videoscope.rescue.executor import (
    RescuedSegment,
    RescueExecutionResult,
    SourceMapping,
)
from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueActionKind,
    RescueArtifact,
    RescueConfirmation,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueSymptom,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    make_damage_id,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    RescuePipelineDependencies,
    RescuePreparation,
    RescueResult,
    RescueStatus,
    VideoRescuePipeline,
)
from videoscope.rescue.visual import VisualAssessment, VisualMetrics


def _metadata(source: Path, duration: float = 2.0) -> VideoMetadata:
    return VideoMetadata(
        filename=source.name,
        container_format="mp4",
        codec="h264",
        width=16,
        height=16,
        duration_seconds=duration,
        average_frame_rate=2.0,
        estimated_frame_count=4,
        has_audio=True,
        file_size_bytes=source.stat().st_size,
    )


def _damage_map(
    source_hash: str,
    *,
    duration: float = 2.0,
    kind: DamageKind | None = None,
) -> MediaDamageMap:
    intervals: tuple[DamageInterval, ...] = ()
    if kind is not None:
        start, end = (0.0, 0.5) if kind is DamageKind.UNDECODABLE else (0.5, 1.0)
        intervals = (
            DamageInterval(
                id=make_damage_id(source_hash, "video:0", kind, start, end),
                stream_id="video:0",
                kind=kind,
                start_seconds=start,
                end_seconds=end,
                description="Observable test interval.",
                measurements={"origin": "scanner"},
            ),
        )
    return MediaDamageMap(
        input_hash=source_hash,
        duration_seconds=duration,
        scan_coverage=((0.0, duration),),
        intervals=intervals,
    )


class _Scanner:
    def __init__(self, damage_map: MediaDamageMap, *, error: Exception | None = None):
        self.damage_map = damage_map
        self.error = error
        self.calls = 0

    def scan(self, *_args: object) -> MediaDamageMap:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.damage_map


class _PreviewBuilder:
    def __init__(self, *, error: Exception | None = None):
        self.error = error

    def build(self, *_args: object) -> None:
        if self.error is not None:
            raise self.error


class _AssessmentService:
    def __init__(self, kind: DamageKind | None) -> None:
        self.kind = kind

    def assess(self, *_args: object) -> RescueAssessmentBundle:
        visual = None
        if self.kind is DamageKind.DARK:
            visual = VisualAssessment(
                metrics=VisualMetrics(
                    luma_p10=0.05,
                    luma_p50=0.08,
                    luma_p90=0.12,
                    low_clip_ratio=0.0,
                    high_clip_ratio=0.0,
                    noise_residual=0.0,
                    sharpness=0.1,
                ),
                recommended_actions=(RescueActionKind.ADJUST_LUMA,),
                preview_required=True,
                public_explanation="Measured dark samples support a preview.",
            )
        return RescueAssessmentBundle(visual_assessment=visual)


class _UnavailableAssessmentService:
    def assess(self, *_args: object) -> RescueAssessmentBundle:
        return RescueAssessmentBundle(
            warnings=(
                RescueAssessmentWarning(
                    component="stabilization",
                    error_type="RuntimeError",
                    message="The local stabilization assessment was unavailable.",
                ),
            ),
            limitations=("No stabilization action was inferred.",),
        )


class _Executor:
    def __init__(
        self,
        *,
        faithful_error: Exception | None = None,
        improved_error: Exception | None = None,
        partial: bool = False,
    ) -> None:
        self.faithful_error = faithful_error
        self.improved_error = improved_error
        self.partial = partial
        self.faithful_calls = 0
        self.improved_calls = 0
        self.faithful_action_kinds: tuple[str, ...] = ()
        self.faithful_source_bytes: bytes | None = None

    def execute_faithful(
        self,
        plan: object,
        source: Path,
        work_root: Path,
        cancellation_callback: object,
    ) -> RescueExecutionResult:
        self.faithful_action_kinds = tuple(
            action.kind.value for action in getattr(plan, "actions", ())
        )
        self.faithful_source_bytes = source.read_bytes()
        del cancellation_callback
        self.faithful_calls += 1
        if self.faithful_error is not None:
            raise self.faithful_error
        path = work_root / "staging" / "faithful-rescue.mp4"
        path.write_bytes(b"faithful")
        segment = RescuedSegment(
            0.0,
            2.0,
            0.0,
            2.0,
            "staging/faithful-rescue.mp4",
        )
        return RescueExecutionResult(
            path,
            "faithful-rescue.mp4",
            (segment,),
            (segment.source_mapping,),
            ((0.5, 1.0),) if self.partial else (),
        )

    def execute_improved(
        self,
        plan: object,
        faithful: Path,
        work_root: Path,
        cancellation_callback: object,
        source_mappings: tuple[SourceMapping, ...] = (),
    ) -> Path:
        del plan, faithful, cancellation_callback, source_mappings
        self.improved_calls += 1
        if self.improved_error is not None:
            raise self.improved_error
        path = work_root / "staging" / "improved-viewing.mp4"
        path.write_bytes(b"improved")
        return path


class _GapExecutor(_Executor):
    def execute_faithful(
        self,
        plan: object,
        source: Path,
        work_root: Path,
        cancellation_callback: object,
    ) -> RescueExecutionResult:
        result = super().execute_faithful(
            plan, source, work_root, cancellation_callback
        )
        return replace(
            result,
            source_mappings=(
                SourceMapping(0.0, 0.5, 0.0, 0.5, result.output_relative_path),
                SourceMapping(1.0, 2.0, 0.5, 1.5, result.output_relative_path),
            ),
            failed_source_ranges=(),
        )


class _ReencodeExecutor(_Executor):
    def execute_faithful(
        self,
        plan: object,
        source: Path,
        work_root: Path,
        cancellation_callback: object,
    ) -> RescueExecutionResult:
        result = super().execute_faithful(
            plan, source, work_root, cancellation_callback
        )
        return replace(result, render_mode="single_reencode")


def _checks(
    artifact: Literal["faithful", "improved"],
    status: RescueVerificationStatus,
) -> tuple[RescueVerificationCheck, ...]:
    return tuple(
        RescueVerificationCheck(
            check_id=check_id,
            artifact=artifact,
            status=status,
            message="Measured locally.",
            measured={"observed": True},
        )
        for check_id in RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
    )


class _Verifier:
    def __init__(
        self,
        *,
        faithful_status: RescueVerificationStatus = RescueVerificationStatus.PASSED,
        improved_status: RescueVerificationStatus = RescueVerificationStatus.PASSED,
        error: Exception | None = None,
    ) -> None:
        self.faithful_status = faithful_status
        self.improved_status = improved_status
        self.error = error
        self.improved_paths: list[Path | None] = []
        self.source_mappings: list[tuple[object, ...]] = []
        self.render_modes: list[str] = []

    def verify(
        self,
        source: Path,
        faithful: Path,
        improved: Path | None,
        plan: Any,
        mappings: tuple[object, ...],
        cancellation_callback: object,
        *,
        faithful_render_mode: str,
    ) -> RescueVerificationReport:
        del source, cancellation_callback
        if self.error is not None:
            raise self.error
        self.improved_paths.append(improved)
        self.source_mappings.append(mappings)
        self.render_modes.append(faithful_render_mode)
        checks = list(_checks("faithful", self.faithful_status))
        artifacts = [
            RescueArtifact(
                artifact_role="faithful",
                relative_path="faithful-rescue.mp4",
                sha256=sha256(faithful.read_bytes()).hexdigest(),
                description="Measured faithful output.",
            )
        ]
        if improved is not None:
            checks.extend(_checks("improved", self.improved_status))
            artifacts.append(
                RescueArtifact(
                    artifact_role="improved",
                    relative_path="improved-viewing.mp4",
                    sha256=sha256(improved.read_bytes()).hexdigest(),
                    description="Measured improved output.",
                )
            )
        return RescueVerificationReport(
            plan_digest=plan.plan_digest,
            faithful_status=self.faithful_status,
            improved_status=self.improved_status if improved is not None else None,
            checks=tuple(checks),
            artifacts=tuple(artifacts),
            outcome=RescueOutcome.COMPLETED,
        )


def _pipeline(
    tmp_path: Path,
    *,
    strategy: str = "conservative",
    damage_kind: DamageKind | None = None,
    scanner_error: Exception | None = None,
    preview_error: Exception | None = None,
    executor: _Executor | None = None,
    verifier: _Verifier | None = None,
    publisher: Callable[..., tuple[Any, ...]] | None = None,
    keep_workspace: bool = False,
    progress: list[RescueStatus] | None = None,
    planner: Callable[..., RescuePlan] | None = None,
    assessment_service: object | None = None,
    symptoms: tuple[str, ...] = (),
    locked_ranges: tuple[tuple[float, float], ...] = (),
) -> tuple[VideoRescuePipeline, Path, _Executor, _Verifier, MediaDamageMap]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "视频 source.mp4"
    source.write_bytes(b"local video")
    source_hash = sha256(source.read_bytes()).hexdigest()
    damage_map = _damage_map(source_hash, kind=damage_kind)
    fake_executor = executor or _Executor()
    fake_verifier = verifier or _Verifier()
    dependencies = RescuePipelineDependencies(
        probe=lambda candidate: _metadata(candidate),
        scanner=_Scanner(damage_map, error=scanner_error),
        assessment_service=assessment_service or _AssessmentService(damage_kind),
        preview_builder=_PreviewBuilder(error=preview_error),
        executor=fake_executor,
        verifier=fake_verifier,
    )
    if publisher is not None:
        dependencies.publisher = publisher
    if planner is not None:
        dependencies.planner = planner
    pipeline = VideoRescuePipeline(
        RescueConfig(
            output_directory=tmp_path / "输出 job",
            strategy=RescueStrategy(strategy),
            symptoms=cast(tuple[RescueSymptom, ...], symptoms),
            locked_ranges=locked_ranges,
            keep_workspace=keep_workspace,
        ),
        dependencies=dependencies,
        progress=progress.append if progress is not None else None,
    )
    return pipeline, source, fake_executor, fake_verifier, damage_map


def _confirmation(preparation: RescuePreparation) -> RescueConfirmation:
    required = tuple(
        action.id for action in preparation.plan.actions if action.requires_confirmation
    )
    trim_damage_ids = tuple(
        value
        for action in preparation.plan.actions
        if action.kind.value == "trim_damaged_edges"
        for values in (action.parameters.get("damage_ids"),)
        if isinstance(values, list)
        for value in values
        if isinstance(value, str)
    )
    return RescueConfirmation(
        plan_digest=preparation.plan.plan_digest,
        publish_faithful=True,
        publish_improved=any(
            action.changes_content and action.strategy.value == "balanced"
            for action in preparation.plan.actions
        ),
        accepted_action_ids=required,
        accepted_trim_damage_ids=trim_damage_ids,
    )


def _prepare_confirm_execute(
    pipeline: VideoRescuePipeline,
    source: Path,
) -> RescueResult:
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)
    return pipeline.execute(preparation, confirmation)


def _prepare_with_observable_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **pipeline_kwargs: Any,
) -> tuple[VideoRescuePipeline, Path, RescuePreparation, list[int]]:
    descriptors: list[int] = []

    def observable_open(path: Path) -> int:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        os.set_inheritable(descriptor, False)
        descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(rescue_pipeline_module, "secure_read_open", observable_open)
    pipeline, source, _, _, _ = _pipeline(tmp_path, **pipeline_kwargs)
    preparation = pipeline.prepare(source)
    return pipeline, source, preparation, descriptors


def test_pipeline_exposes_immutable_lifecycle_contract() -> None:
    assert getattr(RescueConfig, "__dataclass_params__").frozen
    assert getattr(RescuePreparation, "__dataclass_params__").frozen
    assert getattr(RescueResult, "__dataclass_params__").frozen
    assert RescueStatus.AWAITING_CONFIRMATION.value == "awaiting_confirmation"
    with pytest.raises(FrozenInstanceError):
        RescueConfig(Path("out")).preview_seconds = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"preview_seconds": float("nan")},
        {"preview_seconds": float("inf")},
        {"locked_ranges": ((0.0, float("inf")),)},
        {"locked_ranges": ((float("nan"), 1.0),)},
    ],
)
def test_rescue_config_rejects_non_finite_seconds(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RescueConfig(Path("out"), **kwargs)  # type: ignore[arg-type]


def test_rescue_config_binds_canonical_symptoms_and_rejects_invalid_hints() -> None:
    config = RescueConfig(
        Path("out"),
        symptoms=cast(tuple[RescueSymptom, ...], ("dark", "shake")),
    )

    assert config.symptoms == (RescueSymptom.DARK, RescueSymptom.SHAKE)
    for symptoms in (
        ("",),
        ("unknown",),
        ("dark", "dark"),
        ("missing_audio", "audio_noise"),
    ):
        with pytest.raises(RescueInputError):
            RescueConfig(
                Path("out"), symptoms=cast(tuple[RescueSymptom, ...], symptoms)
            )


def test_confirmation_is_bound_to_exact_issued_plan_source_and_pipeline(
    tmp_path: Path,
) -> None:
    pipeline, source, executor, _, _ = _pipeline(tmp_path)
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    altered = replace(preparation, plan=preparation.plan.model_copy())
    second, _, _, _, _ = _pipeline(tmp_path / "second")

    with pytest.raises(RescueConfirmationError):
        pipeline.confirm(altered, confirmation)
    with pytest.raises(RescueConfirmationError):
        second.confirm(preparation, confirmation)
    try:
        source.write_bytes(b"changed after preview")
    except OSError:
        # Windows denies mutation while the pinned read handle is alive.
        pipeline.confirm(preparation, confirmation)
        pipeline.execute(preparation, confirmation)
        assert executor.faithful_calls == 1
    else:
        with pytest.raises(RescueConfirmationError):
            pipeline.confirm(preparation, confirmation)
        assert executor.faithful_calls == 0


def test_direct_pipeline_keeps_one_pinned_source_identity_across_replacement(
    tmp_path: Path,
) -> None:
    """Catches reopening a user pathname between scan, preview, and execution."""
    pipeline, source, executor, _, _ = _pipeline(tmp_path)
    original = source.read_bytes()
    preparation = pipeline.prepare(source)
    replacement = source.with_name("replacement.mp4")
    replacement.write_bytes(b"different video bytes")
    try:
        replacement.replace(source)
    except OSError:
        # Windows is expected to deny replacement while the pinned handle is held.
        pass
    confirmation = _confirmation(preparation)

    pipeline.confirm(preparation, confirmation)
    result = pipeline.execute(preparation, confirmation)

    assert result.status is RescueStatus.COMPLETED
    assert executor.faithful_source_bytes == original


def test_locked_ranges_are_bound_into_the_issued_plan_digest(tmp_path: Path) -> None:
    first, first_source, _, _, _ = _pipeline(
        tmp_path / "first",
        locked_ranges=((0.75, 1.0), (0.25, 0.5), (0.75, 1.0)),
    )
    second, second_source, _, _, _ = _pipeline(
        tmp_path / "second", locked_ranges=((1.25, 1.5),)
    )

    first_plan = first.prepare(first_source).plan
    second_plan = second.prepare(second_source).plan

    assert first_plan.actions == second_plan.actions
    assert first_plan.effective_config.locked_ranges == ((0.25, 0.5), (0.75, 1.0))
    assert second_plan.effective_config.locked_ranges == ((1.25, 1.5),)
    assert first_plan.plan_digest != second_plan.plan_digest


def test_confirmation_rejects_post_preview_action_subset_and_unknown_ids(
    tmp_path: Path,
) -> None:
    pipeline, source, _, _, _ = _pipeline(
        tmp_path, strategy="balanced", damage_kind=DamageKind.DARK
    )
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    required = confirmation.accepted_action_ids

    subset = confirmation.model_copy(
        update={
            "accepted_action_ids": required[:-1],
            "publish_improved": False,
        }
    )
    with pytest.raises(RescueConfirmationError):
        pipeline.confirm(preparation, subset)

    second, second_source, _, _, _ = _pipeline(
        tmp_path / "invalid", strategy="balanced", damage_kind=DamageKind.DARK
    )
    second_preparation = second.prepare(second_source)
    invalid = _confirmation(second_preparation).model_copy(
        update={"accepted_action_ids": (*required, "unknown-action")}
    )
    for candidate in (
        invalid.model_copy(update={"plan_digest": "f" * 64}),
        invalid,
    ):
        with pytest.raises(RescueConfirmationError):
            second.confirm(second_preparation, candidate)


def test_confirmation_binds_trim_damage_ids_to_the_selected_trim_action(
    tmp_path: Path,
) -> None:
    pipeline, source, _, _, _ = _pipeline(tmp_path, damage_kind=DamageKind.UNDECODABLE)
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    assert confirmation.accepted_trim_damage_ids

    trim_action_ids = tuple(
        action.id
        for action in preparation.plan.actions
        if action.kind is RescueActionKind.TRIM_DAMAGED_EDGES
    )
    without_trim = confirmation.model_copy(
        update={
            "accepted_action_ids": tuple(
                action_id
                for action_id in confirmation.accepted_action_ids
                if action_id not in trim_action_ids
            ),
            "accepted_trim_damage_ids": (),
        }
    )
    with pytest.raises(RescueConfirmationError):
        pipeline.confirm(preparation, without_trim)

    second, second_source, _, _, _ = _pipeline(
        tmp_path / "invalid", damage_kind=DamageKind.UNDECODABLE
    )
    second_preparation = second.prepare(second_source)
    invalid_confirmation = _confirmation(second_preparation)
    for values in (
        (),
        (*invalid_confirmation.accepted_trim_damage_ids, "damage_" + "f" * 64),
    ):
        candidate = invalid_confirmation.model_copy(
            update={"accepted_trim_damage_ids": values}
        )
        with pytest.raises(RescueConfirmationError):
            second.confirm(second_preparation, candidate)


def test_deselected_balanced_action_cannot_execute_under_the_original_digest(
    tmp_path: Path,
) -> None:
    pipeline, source, executor, _, _ = _pipeline(
        tmp_path, strategy="balanced", damage_kind=DamageKind.DARK
    )
    preparation = pipeline.prepare(source)
    confirmation = RescueConfirmation(
        plan_digest=preparation.plan.plan_digest,
        publish_faithful=True,
        publish_improved=False,
        accepted_action_ids=(),
        accepted_trim_damage_ids=(),
    )

    with pytest.raises(RescueConfirmationError):
        pipeline.confirm(preparation, confirmation)

    assert executor.faithful_calls == 0
    assert executor.improved_calls == 0


def test_preparation_is_single_use_even_after_execution_failure(tmp_path: Path) -> None:
    executor = _Executor(faithful_error=RescueMediaError("processor failed"))
    pipeline, source, _, _, _ = _pipeline(tmp_path, executor=executor)
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)

    with pytest.raises(RescueMediaError):
        pipeline.execute(preparation, confirmation)
    with pytest.raises(RescueConfirmationError):
        pipeline.execute(preparation, confirmation)


def test_new_preparation_invalidates_an_older_unconsumed_preparation(
    tmp_path: Path,
) -> None:
    pipeline, source, _, _, _ = _pipeline(tmp_path)
    first = pipeline.prepare(source)
    second = pipeline.prepare(source)

    with pytest.raises(RescueConfirmationError):
        pipeline.confirm(first, _confirmation(first))
    pipeline.confirm(second, _confirmation(second))


def test_prepare_closes_descriptor_when_source_hashing_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, source, _, _, _ = _pipeline(tmp_path)
    descriptors: list[int] = []

    def fail_hash(descriptor: int) -> str:
        descriptors.append(descriptor)
        raise OSError("forced source hashing failure")

    monkeypatch.setattr(rescue_pipeline_module, "hash_descriptor", fail_hash)

    with pytest.raises(RescueInputError):
        pipeline.prepare(source)

    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


def test_abort_releases_awaiting_confirmation_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, _, preparation, descriptors = _prepare_with_observable_descriptor(
        tmp_path, monkeypatch
    )

    pipeline.abort(preparation)

    with pytest.raises(OSError):
        os.fstat(descriptors[-1])
    pipeline.abort(preparation)


def test_cancel_before_confirmation_releases_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, _, preparation, descriptors = _prepare_with_observable_descriptor(
        tmp_path, monkeypatch
    )

    pipeline.cancel()

    with pytest.raises(OSError):
        os.fstat(descriptors[-1])
    with pytest.raises(RescueConfirmationError):
        pipeline.confirm(preparation, _confirmation(preparation))


def test_failed_execute_releases_descriptor_registry_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, _, preparation, descriptors = _prepare_with_observable_descriptor(
        tmp_path,
        monkeypatch,
        executor=_Executor(faithful_error=RescueMediaError("processor failed")),
    )
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)

    with pytest.raises(RescueMediaError):
        pipeline.execute(preparation, confirmation)
    with pytest.raises(OSError):
        os.fstat(descriptors[-1])

    unrelated_path = tmp_path / "unrelated-after-failure.bin"
    unrelated_path.write_bytes(b"unrelated")
    unrelated = os.open(unrelated_path, os.O_RDONLY)
    assert unrelated == descriptors[-1]
    second_source = tmp_path / "second-after-failure.mp4"
    second_source.write_bytes(b"local video")
    try:
        pipeline.prepare(second_source)
        assert os.fstat(unrelated).st_size == len(b"unrelated")
    finally:
        pipeline.close()
        try:
            os.close(unrelated)
        except OSError:
            pass


def test_replacement_prepare_releases_superseded_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, _, first, descriptors = _prepare_with_observable_descriptor(
        tmp_path, monkeypatch
    )
    first_descriptor = descriptors[-1]
    second_source = tmp_path / "second.mp4"
    second_source.write_bytes(b"local video")

    second = pipeline.prepare(second_source)

    with pytest.raises(OSError):
        os.fstat(first_descriptor)
    assert os.fstat(descriptors[-1]).st_size == len(b"local video")
    with pytest.raises(RescueConfirmationError):
        pipeline.confirm(first, _confirmation(first))
    pipeline.abort(second)


def test_close_repeatedly_releases_retained_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, _, _, descriptors = _prepare_with_observable_descriptor(
        tmp_path, monkeypatch
    )

    pipeline.close()
    pipeline.close()

    with pytest.raises(OSError):
        os.fstat(descriptors[-1])


def test_close_is_idempotent_without_preparation(tmp_path: Path) -> None:
    pipeline, _, _, _, _ = _pipeline(tmp_path)

    pipeline.close()
    pipeline.close()


def test_execute_then_prepare_cannot_close_reused_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipeline, _, first, descriptors = _prepare_with_observable_descriptor(
        tmp_path, monkeypatch
    )
    confirmation = _confirmation(first)
    pipeline.confirm(first, confirmation)
    pipeline.execute(first, confirmation)
    first_descriptor = descriptors[-1]
    with pytest.raises(OSError):
        os.fstat(first_descriptor)

    unrelated_path = tmp_path / "unrelated.bin"
    unrelated_path.write_bytes(b"unrelated")
    unrelated = os.open(unrelated_path, os.O_RDONLY)
    assert unrelated == first_descriptor
    second_source = tmp_path / "second.mp4"
    second_source.write_bytes(b"local video")
    try:
        pipeline.prepare(second_source)
        assert os.fstat(unrelated).st_size == len(b"unrelated")
    finally:
        pipeline.close()
        try:
            os.close(unrelated)
        except OSError:
            pass


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("scanning", [RescueStatus.SCANNING, RescueStatus.CANCELLED]),
        (
            "planning",
            [RescueStatus.SCANNING, RescueStatus.PLANNING, RescueStatus.CANCELLED],
        ),
        (
            "previewing",
            [
                RescueStatus.SCANNING,
                RescueStatus.PLANNING,
                RescueStatus.PREVIEWING,
                RescueStatus.CANCELLED,
            ],
        ),
        (
            "processing",
            [RescueStatus.PROCESSING, RescueStatus.CANCELLED],
        ),
        (
            "verifying",
            [
                RescueStatus.PROCESSING,
                RescueStatus.VERIFYING,
                RescueStatus.CANCELLED,
            ],
        ),
    ],
)
def test_cancellation_at_every_stage_is_terminal_and_cleans_private_workspace(
    tmp_path: Path,
    stage: str,
    expected: list[RescueStatus],
) -> None:
    events: list[RescueStatus] = []
    pipeline, source, executor, verifier, _ = _pipeline(tmp_path, progress=events)

    if stage == "scanning":
        scanner = pipeline._dependencies.scanner
        original_scan = scanner.scan

        def scan(*args: object) -> MediaDamageMap:
            pipeline.cancel()
            return cast(MediaDamageMap, cast(Any, original_scan)(*args))

        scanner.scan = scan
    elif stage == "planning":
        original = pipeline._dependencies.planner

        def plan(**kwargs: object) -> object:
            pipeline.cancel()
            return original(**kwargs)

        pipeline._dependencies.planner = cast(Callable[..., RescuePlan], plan)
    elif stage == "previewing":
        original = pipeline._dependencies.preview_builder.build

        def preview(*args: object) -> None:
            pipeline.cancel()
            original(*args)

        pipeline._dependencies.preview_builder.build = preview

    if stage in {"scanning", "planning", "previewing"}:
        with pytest.raises(RescueCancelledError):
            pipeline.prepare(source)
        assert events == expected
    else:
        preparation = pipeline.prepare(source)
        confirmation = _confirmation(preparation)
        pipeline.confirm(preparation, confirmation)
        events.clear()
        if stage == "processing":
            original_execute = executor.execute_faithful

            def execute(*args: object, **kwargs: object) -> RescueExecutionResult:
                pipeline.cancel()
                return cast(
                    RescueExecutionResult,
                    cast(Any, original_execute)(*args, **kwargs),
                )

            executor.execute_faithful = execute  # type: ignore[method-assign]
        else:
            original_verify = verifier.verify

            def verify(*args: object, **kwargs: object) -> RescueVerificationReport:
                pipeline.cancel()
                return cast(
                    RescueVerificationReport,
                    cast(Any, original_verify)(*args, **kwargs),
                )

            verifier.verify = verify  # type: ignore[method-assign]
        with pytest.raises(RescueCancelledError):
            pipeline.execute(preparation, confirmation)
        assert events == expected
    assert not (tmp_path / "输出 job" / "rescue-output").exists()
    assert not (tmp_path / "输出 job" / "rescue-review-private").exists()


def test_cancellation_after_atomic_publication_returns_verified_result(
    tmp_path: Path,
) -> None:
    """Atomic publication is the core's irrevocable completion cutoff."""
    progress: list[RescueStatus] = []
    pipeline: VideoRescuePipeline

    def publish_then_cancel(*args: object, **kwargs: object) -> tuple[Any, ...]:
        artifacts = cast(
            tuple[Any, ...],
            cast(Any, publish_verified_rescue)(*args, **kwargs),
        )
        assert (tmp_path / "输出 job" / "rescue-output").is_dir()
        pipeline.cancel()
        return artifacts

    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        publisher=publish_then_cancel,
        progress=progress,
    )
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)

    result = pipeline.execute(preparation, confirmation)

    assert result.status is RescueStatus.COMPLETED
    assert result.public_root is not None and result.public_root.is_dir()
    assert progress[-1] is RescueStatus.COMPLETED


@pytest.mark.parametrize(
    ("boundary", "error"),
    [
        ("scanner", RescueScanError("scan failed")),
        ("planner", RescuePlanError("plan failed")),
        ("preview", RescueMediaError("preview failed")),
        ("executor", RescueMediaError("execution failed")),
        ("verifier", RescueMediaError("verification failed")),
        ("publisher", RescueArtifactError("publication failed")),
    ],
)
def test_boundary_failures_never_expose_partial_public_output(
    tmp_path: Path,
    boundary: str,
    error: Exception,
) -> None:
    kwargs: dict[str, object] = {}
    if boundary == "scanner":
        kwargs["scanner_error"] = error
    elif boundary == "planner":
        kwargs["planner"] = lambda **_kwargs: (_ for _ in ()).throw(error)
    elif boundary == "preview":
        kwargs["preview_error"] = error
    elif boundary == "executor":
        kwargs["executor"] = _Executor(faithful_error=error)
    elif boundary == "verifier":
        kwargs["verifier"] = _Verifier(error=error)
    else:
        kwargs["publisher"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(error)
    pipeline, source, _, _, _ = cast(Any, _pipeline)(tmp_path, **kwargs)

    with pytest.raises(type(error)):
        if boundary in {"scanner", "planner", "preview"}:
            pipeline.prepare(source)
        else:
            preparation = pipeline.prepare(source)
            confirmation = _confirmation(preparation)
            pipeline.confirm(preparation, confirmation)
            pipeline.execute(preparation, confirmation)
    assert not (tmp_path / "输出 job" / "rescue-output").exists()


def test_balanced_supported_improvement_is_verified_and_published(
    tmp_path: Path,
) -> None:
    pipeline, source, executor, verifier, damage_map = _pipeline(
        tmp_path, strategy="balanced", damage_kind=DamageKind.DARK
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.COMPLETED
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert result.improved_path is not None and result.improved_path.is_file()
    assert executor.improved_calls == 1
    assert verifier.improved_paths and verifier.improved_paths[0] is not None
    assert result.technical_report is not None
    assert result.technical_report.damage_map.input_hash == damage_map.input_hash
    assert result.technical_report.damage_map.intervals == damage_map.intervals
    assert result.technical_report.damage_map.scanner_version.endswith("assessment-1")


def test_improved_execution_failure_is_not_reported_as_executed(
    tmp_path: Path,
) -> None:
    """Catches rendering planned actions as successful after an atomic failure."""
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.DARK,
        executor=_Executor(improved_error=RescueMediaError("private failure")),
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.PARTIAL
    assert result.technical_report is not None
    executions = {item.kind: item for item in result.technical_report.action_executions}
    assert executions[RescueActionKind.REMUX].status.value == "succeeded"
    assert executions[RescueActionKind.ADJUST_LUMA].status.value == "failed"
    assert executions[RescueActionKind.ADJUST_LUMA].artifact_role == "improved"
    assert executions[RescueActionKind.ADJUST_LUMA].reason == (
        "The improved candidate could not be completed."
    )
    assert result.public_root is not None
    changes = (result.public_root / "changes.json").read_text(encoding="utf-8")
    assert '"status":"failed"' in changes


def test_balanced_clean_input_delivers_faithful_only_with_limitation(
    tmp_path: Path,
) -> None:
    pipeline, source, executor, _, _ = _pipeline(tmp_path, strategy="balanced")

    result = _prepare_confirm_execute(pipeline, source)

    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert result.improved_path is None
    assert executor.improved_calls == 0
    assert result.technical_report is not None
    assert any(
        "no supported improvement" in value
        for value in result.technical_report.limitations
    )


def test_assessment_warning_retains_faithful_and_requires_review(
    tmp_path: Path,
) -> None:
    pipeline, source, executor, _, _ = _pipeline(
        tmp_path,
        strategy="balanced",
        assessment_service=_UnavailableAssessmentService(),
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.NEEDS_REVIEW
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert result.improved_path is None
    assert executor.faithful_calls == 1
    assert result.technical_report is not None
    assert result.technical_report.assessment_warnings == (
        "The local stabilization assessment was unavailable.",
    )
    assert "No stabilization action was inferred." in (
        result.technical_report.assessment_limitations
    )
    assert (
        "improved candidate"
        not in " ".join(result.technical_report.manual_review_reasons).lower()
    )


def test_plan_capability_warning_retains_faithful_and_requires_review(
    tmp_path: Path,
) -> None:
    """Catches completing when an unsupported planned action was review-gated."""
    pipeline, source, executor, _, _ = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.MISSING_STREAM,
    )

    preparation = pipeline.prepare(source)
    assert RescueActionKind.SELECT_TRACKS not in {
        action.kind for action in preparation.plan.actions
    }
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)
    result = pipeline.execute(preparation, confirmation)

    expected = (
        "Automatic select_tracks action needs review: preview_renderer_unavailable."
    )
    assert result.status is RescueStatus.NEEDS_REVIEW
    assert executor.faithful_calls == 1
    assert result.technical_report is not None
    assert expected in result.technical_report.assessment_warnings
    assert result.technical_report.manual_review_reasons == (expected,)


@pytest.mark.parametrize("symptom", ["audio_video_offset", "audio_noise"])
def test_requested_audio_fix_without_native_evidence_requires_review(
    tmp_path: Path,
    symptom: str,
) -> None:
    """Catches silently completing when the requested audio evidence is absent."""
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        strategy="balanced",
        symptoms=(symptom,),
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.NEEDS_REVIEW
    assert result.technical_report is not None
    assert any(
        "evidence was unavailable" in reason.lower()
        for reason in result.technical_report.manual_review_reasons
    )


@pytest.mark.parametrize(
    "damage_kind", [DamageKind.FIXED_AV_OFFSET, DamageKind.AUDIO_NOISE]
)
def test_observed_audio_issue_without_native_evidence_requires_review(
    tmp_path: Path,
    damage_kind: DamageKind,
) -> None:
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=damage_kind,
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.NEEDS_REVIEW


def test_symptoms_are_classified_and_bound_to_preparation_and_report(
    tmp_path: Path,
) -> None:
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.DARK,
        symptoms=("dark",),
    )

    preparation = pipeline.prepare(source)
    assert preparation.plan.requested_symptoms == (RescueSymptom.DARK,)
    assert preparation.symptom_assessments[0].symptom is RescueSymptom.DARK
    assert preparation.symptom_assessments[0].status.value == "observed"
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)
    result = pipeline.execute(preparation, confirmation)

    assert result.technical_report is not None
    assert result.technical_report.requested_symptoms == (RescueSymptom.DARK,)


@pytest.mark.parametrize(
    ("improved_status", "expected"),
    [
        (RescueVerificationStatus.FAILED, "partial"),
        (RescueVerificationStatus.NEEDS_REVIEW, "needs_review"),
    ],
)
def test_improved_verification_failure_retains_faithful_with_truthful_status(
    tmp_path: Path,
    improved_status: RescueVerificationStatus,
    expected: str,
) -> None:
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.DARK,
        verifier=_Verifier(improved_status=improved_status),
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status.value == expected
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert (result.improved_path is not None) is (
        improved_status is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert result.technical_report is not None
    assert result.technical_report.outcome.value == expected


def test_partial_salvage_preserves_mapping_and_reports_partial(tmp_path: Path) -> None:
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        damage_kind=DamageKind.UNDECODABLE,
        executor=_Executor(partial=True),
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.PARTIAL
    assert result.technical_report is not None
    assert result.technical_report.outcome is RescueOutcome.PARTIAL
    assert result.source_mappings[0].output_relative_path == "faithful-rescue.mp4"
    assert result.report_path is not None and result.report_path.is_file()
    assert result.public_root is not None
    damaged = (result.public_root / "damaged-segments.json").read_text("utf-8")
    assert "staging" not in damaged
    assert '"source_start":0.0' in damaged
    assert '"damaged_ranges":[[0.5,1.0]]' in damaged


def test_source_mapping_gap_is_reported_as_partial_even_without_runner_failure(
    tmp_path: Path,
) -> None:
    pipeline, source, _, _, _ = _pipeline(tmp_path, executor=_GapExecutor())

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.PARTIAL
    assert result.failed_source_ranges == ((0.5, 1.0),)


def test_improved_needs_review_takes_precedence_over_partial_mapping_gap(
    tmp_path: Path,
) -> None:
    verifier = _Verifier(improved_status=RescueVerificationStatus.NEEDS_REVIEW)
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.DARK,
        executor=_GapExecutor(),
        verifier=verifier,
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.NEEDS_REVIEW
    assert result.technical_report is not None
    assert result.technical_report.outcome is RescueOutcome.NEEDS_REVIEW
    assert result.technical_report.verification.outcome is RescueOutcome.NEEDS_REVIEW
    assert result.failed_source_ranges == ((0.5, 1.0),)
    reasons = result.technical_report.manual_review_reasons
    assert any("not retained" in reason for reason in reasons)
    assert any("improved candidate" in reason for reason in reasons)


def test_verifier_receives_only_public_source_mapping_paths(tmp_path: Path) -> None:
    verifier = _Verifier()
    pipeline, source, _, _, _ = _pipeline(tmp_path, verifier=verifier)

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.COMPLETED
    assert len(verifier.source_mappings) == 1
    assert tuple(
        getattr(mapping, "output_relative_path")
        for mapping in verifier.source_mappings[0]
    ) == ("faithful-rescue.mp4",)


def test_verifier_receives_execution_recorded_faithful_render_mode(
    tmp_path: Path,
) -> None:
    verifier = _Verifier()
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        executor=_ReencodeExecutor(),
        verifier=verifier,
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.COMPLETED
    assert verifier.render_modes == ["single_reencode"]


def test_failed_faithful_verification_publishes_nothing(tmp_path: Path) -> None:
    pipeline, source, _, _, _ = _pipeline(
        tmp_path,
        verifier=_Verifier(faithful_status=RescueVerificationStatus.FAILED),
    )

    result = _prepare_confirm_execute(pipeline, source)

    assert result.status is RescueStatus.FAILED
    assert result.faithful_path is None
    assert result.public_root is None
    assert not (tmp_path / "输出 job" / "rescue-output").exists()


def test_workspace_retention_is_explicit_and_progress_has_one_terminal_state(
    tmp_path: Path,
) -> None:
    events: list[RescueStatus] = []
    pipeline, source, _, _, _ = _pipeline(
        tmp_path, keep_workspace=True, progress=events
    )

    _prepare_confirm_execute(pipeline, source)

    assert (tmp_path / "输出 job" / "rescue-review-private").is_dir()
    assert events == [
        RescueStatus.SCANNING,
        RescueStatus.PLANNING,
        RescueStatus.PREVIEWING,
        RescueStatus.AWAITING_CONFIRMATION,
        RescueStatus.PROCESSING,
        RescueStatus.VERIFYING,
        RescueStatus.COMPLETED,
    ]
