"""Lifecycle contracts for the review-gated Video Rescue pipeline."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from pydantic import JsonValue

import videoscope.rescue.pipeline as rescue_pipeline_module
from videoscope.domain import VideoMetadata
from videoscope.rescue.artifacts import publish_verified_rescue
from videoscope.rescue.assessment import RescueAssessmentBundle, RescueAssessmentWarning
from videoscope.rescue.deblur import BlurKernelEstimate, DeblurConfig
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueConfirmationError,
    RescueInputError,
    RescueMediaError,
    RescuePlanError,
    RescueQualificationUnavailableError,
    RescueScanError,
)
from videoscope.rescue.executor import (
    RescuedSegment,
    RescueExecutionResult,
    SourceMapping,
)
from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    CanonicalVideoEncodeContract,
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueArtifact,
    RescueConfirmation,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueSymptom,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    canonical_video_encode_contract,
    make_damage_id,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    RescuePipelineDependencies,
    RescuePreparation,
    RescueResult,
    RescueStatus,
    VideoRescuePipeline,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.qualification import (
    SHARPEN_QUALIFICATION_LIMITATION,
    SHARPEN_QUALIFICATION_UNAVAILABLE_LIMITATION,
    SharpenProfileMeasurementV1,
    SharpenQualificationMetricsV1,
    SharpenQualificationThresholdsV1,
    build_sharpen_qualification_evidence,
)
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationConfig,
    StabilizationImmediateParentHandle,
    StabilizationProfileMeasurementV1,
    StabilizationQualificationMetricsV1,
    build_stabilization_qualification_evidence,
    stabilization_actual_pts_digest,
    stabilization_qualification_thresholds,
)
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
)
from videoscope.rescue.tonal_qualification import (
    TONAL_ENCODED_QUALIFICATION_UNAVAILABLE_LIMITATION,
)
from videoscope.rescue.visual import (
    LumaAdjustmentConfig,
    VisualAssessment,
    VisualEvidence,
    VisualMetrics,
)


def test_verification_control_cleanup_is_confined_and_transactional(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    private_root.mkdir()
    inside = private_root / "control.private.mp4"
    outside = tmp_path / "source.mp4"
    inside.write_bytes(b"control")
    outside.write_bytes(b"source")

    class Handle:
        cleanup_paths = (inside, outside)

    with pytest.raises(RescueArtifactError):
        rescue_pipeline_module._cleanup_verification_controls(private_root, (Handle(),))
    assert inside.read_bytes() == b"control"
    assert outside.read_bytes() == b"source"

    class SafeHandle:
        cleanup_paths = (inside,)

    rescue_pipeline_module._cleanup_verification_controls(private_root, (SafeHandle(),))
    assert not inside.exists()


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
            luma_config = LumaAdjustmentConfig()
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
                evidence=(
                    VisualEvidence(
                        action=RescueActionKind.ADJUST_LUMA,
                        timestamp_seconds=0.75,
                        metric="luma_p10",
                        observed=0.05,
                        threshold=luma_config.dark_percentile_threshold,
                        context_luma_p50=0.08,
                    ),
                ),
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


class _SharpenAssessmentService:
    def assess(self, *_args: object) -> RescueAssessmentBundle:
        return RescueAssessmentBundle(
            visual_assessment=VisualAssessment(
                metrics=VisualMetrics(
                    luma_p10=0.1,
                    luma_p50=0.2,
                    luma_p90=0.4,
                    low_clip_ratio=0.0,
                    high_clip_ratio=0.0,
                    noise_residual=0.01,
                    sharpness=0.01,
                ),
                recommended_actions=(RescueActionKind.SHARPEN,),
                evidence=(
                    VisualEvidence(
                        action=RescueActionKind.SHARPEN,
                        timestamp_seconds=0.75,
                        metric="scene_relative_sharpness",
                        observed=0.01,
                        threshold=0.03,
                        scene_baseline_sharpness=0.04,
                    ),
                ),
                preview_required=True,
                public_explanation="Measured soft detail supports qualification.",
            )
        )


class _TonalAssessmentService:
    def assess(self, *_args: object) -> RescueAssessmentBundle:
        config = TonalInterferenceConfig()
        profile = InterferenceTone(
            start_seconds=0.5,
            end_seconds=1.0,
            center_frequency_hz=880.0,
            confidence=0.95,
            baseline_before_dbfs=-50.0,
            baseline_after_dbfs=-50.0,
            peak_dbfs=-20.0,
            local_peak_over_baseline_db=30.0,
            persistence_window_count=20,
            frequency_standard_deviation_hz=0.0,
            channel_indices=(0, 1),
            attenuation_target_db=24.0,
            render_qualification=TonalRenderQualification(
                boundary_mode="full_interval_v1",
                notch_q=8.0,
                complete_window_count=20,
                minimum_target_reduction_db=25.0,
                maximum_non_target_attenuation_db=0.1,
                maximum_boundary_energy_jump_db=0.0,
                maximum_boundary_crest_jump_db=0.0,
                maximum_boundary_adjacent_delta=0.01,
            ),
        )
        return RescueAssessmentBundle(
            parameters={
                "tonal_interference_measurements": [
                    {
                        "source_ranges": [[0.5, 1.0]],
                        "algorithm_version": "1",
                        "interference_profiles": [profile.model_dump(mode="json")],
                        "config": config.model_dump(mode="json"),
                    }
                ]
            }
        )


class _CandidateQualifier:
    def __init__(self, *, pass_profiles: bool = True) -> None:
        self.pass_profiles = pass_profiles
        self.calls = 0
        self.draft_action_id: str | None = None

    def qualify(
        self,
        draft_plan: RescuePlan,
        _source: Path,
        _work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> object:
        assert not cancellation_callback()
        self.calls += 1
        action = next(
            item for item in draft_plan.actions if item.kind is RescueActionKind.SHARPEN
        )
        self.draft_action_id = action.id
        contract = CanonicalVideoEncodeContract.model_validate(
            action.parameters["video_encode_contract"]
        )
        thresholds = SharpenQualificationThresholdsV1(
            minimum_aggregate_gain_ratio=cast(
                float, action.parameters["minimum_perceptible_sharpness_gain_ratio"]
            ),
            minimum_recovered_baseline_ratio=cast(
                float, action.parameters["minimum_recovered_baseline_ratio"]
            ),
            minimum_improved_frame_fraction=cast(
                float, action.parameters["minimum_improved_frame_fraction"]
            ),
            maximum_noise_increase=cast(
                float, action.parameters["maximum_noise_increase"]
            ),
            maximum_edge_overshoot_ratio=cast(
                float, action.parameters["maximum_edge_overshoot_ratio"]
            ),
            maximum_edge_overshoot_amplitude=cast(
                float, action.parameters["maximum_edge_overshoot_amplitude"]
            ),
            maximum_ringing_ratio=cast(
                float, action.parameters["maximum_ringing_ratio"]
            ),
        )
        measurements = tuple(
            SharpenProfileMeasurementV1(
                profile=profile,
                baseline_sha256="1" * 64,
                visibility_control_sha256="2" * 64,
                candidate_sha256=f"{index + 3:x}" * 64,
                normalized_pts_digest="a" * 64,
                stream_topology_digest="b" * 64,
                decoded_width=16,
                decoded_height=16,
                generation_count=1,
                inventory_frame_count=1,
                metrics=SharpenQualificationMetricsV1(
                    range_coverage_ratio=1.0,
                    expected_frames=1,
                    compared_frames=1,
                    range_count=1,
                    passing_range_count=1,
                    minimum_aggregate_gain_ratio=0.1,
                    minimum_recovered_baseline_ratio=(
                        0.9 if self.pass_profiles else 0.79
                    ),
                    minimum_improved_frame_fraction=0.9,
                    maximum_noise_increase=0.01,
                    maximum_edge_overshoot_ratio=0.02,
                    maximum_edge_overshoot_amplitude=0.03,
                    maximum_ringing_ratio=0.04,
                ),
                thresholds=thresholds,
            )
            for index, profile in enumerate(
                draft_plan.effective_config.sharpen_qualification_profiles
            )
        )
        return build_sharpen_qualification_evidence(
            input_hash=draft_plan.input_hash,
            draft_action_id=action.id,
            draft_parameters=action.parameters,
            source_ranges=action.source_ranges,
            output_ranges=action.source_ranges,
            encode_contract=contract,
            configured_profiles=(
                draft_plan.effective_config.sharpen_qualification_profiles
            ),
            measurements=measurements,
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
        inherited_action_ids: frozenset[str] = frozenset(),
    ) -> Path:
        del plan, faithful, cancellation_callback, source_mappings, inherited_action_ids
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
    candidate_qualifier: object | None = None,
    tonal_candidate_qualifier: object | None = None,
    stabilization_candidate_qualifier: object | None = None,
    stabilization_parent_provider: object | None = None,
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
    if candidate_qualifier is not None:
        dependencies.candidate_qualifier = candidate_qualifier
    if tonal_candidate_qualifier is not None:
        dependencies.tonal_candidate_qualifier = tonal_candidate_qualifier
    if stabilization_candidate_qualifier is not None:
        dependencies.stabilization_candidate_qualifier = (
            stabilization_candidate_qualifier
        )
    if stabilization_parent_provider is not None:
        dependencies.stabilization_parent_provider = stabilization_parent_provider
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


def _transition_stabilization_planner(**inputs: Any) -> RescuePlan:
    damage_map = cast(MediaDamageMap, inputs["damage_map"])
    config = cast(RescueEffectiveConfig, inputs["config"])
    actual_pts = tuple(0.5 + index / 96.0 for index in range(96))
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            translation_x=0.25 if index % 2 else -0.25,
            translation_y=0.1,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            semantics="frame_correction",
        )
        for index, timestamp in enumerate(actual_pts)
    )
    stabilization_config = StabilizationConfig(accepted_ranges=((0.5, 1.5),))
    parameters: dict[str, JsonValue] = {
        "method": "transition_anchor_v1",
        "algorithm_version": "1",
        "estimator_algorithm_version": "transition_anchor_v1",
        "transition_range": [0.5, 1.0],
        "following_anchor_range": [1.0, 1.5],
        "transition_correction_count": len(transforms),
        "motion_transforms": [item.model_dump(mode="json") for item in transforms],
        "config": stabilization_config.model_dump(mode="json"),
        "crop_ratio": 0.05,
        "affected_ranges": [[0.5, 1.5]],
        "video_encode_contract": canonical_video_encode_contract(config).model_dump(
            mode="json"
        ),
    }
    action = RescueAction(
        id=make_rescue_action_id(
            kind=RescueActionKind.STABILIZE,
            parameters=parameters,
            source_ranges=((0.5, 1.5),),
            strategy=RescueStrategy.BALANCED,
            version="1",
        ),
        version="1",
        kind=RescueActionKind.STABILIZE,
        description="Apply exact transition-anchor corrections.",
        source_ranges=((0.5, 1.5),),
        parameters=parameters,
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    draft = RescuePlan.model_construct(
        input_hash=damage_map.input_hash,
        strategy=RescueStrategy.BALANCED,
        effective_config=config,
        actions=(action,),
        preview_ranges=((0.5, 1.5),),
        plan_digest="0" * 64,
    )
    payload = draft.model_dump(mode="python", exclude={"plan_digest"})
    plan = RescuePlan(**payload, plan_digest=make_rescue_plan_digest(payload))
    evidence = inputs.get("stabilization_qualification")
    if evidence is None:
        return plan
    from videoscope.rescue.planner import _apply_stabilization_qualification

    actions = _apply_stabilization_qualification(
        plan.actions,
        evidence,
        input_hash=plan.input_hash,
        config=config,
    )
    if actions == plan.actions:
        return plan
    changed = plan.model_dump(mode="python", exclude={"plan_digest"})
    changed["actions"] = [item.model_dump(mode="python") for item in actions]
    return RescuePlan(**changed, plan_digest=make_rescue_plan_digest(changed))


class _StabilizationParentProvider:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def provide(
        self,
        draft_plan: RescuePlan,
        _source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> StabilizationImmediateParentHandle:
        assert not cancellation_callback()
        assert work_root.is_dir()
        path = work_root / "immediate-parent.private"
        path.write_bytes(b"generated by preceding action chain")
        self.paths.append(path)
        action = next(
            item
            for item in draft_plan.actions
            if item.kind is RescueActionKind.STABILIZE
        )
        actual_pts = tuple(
            cast(float, item["timestamp_seconds"])
            for item in cast(
                list[dict[str, Any]], action.parameters["motion_transforms"]
            )
        )
        return StabilizationImmediateParentHandle(
            root=work_root,
            path=path,
            draft_plan_digest=draft_plan.plan_digest,
            stabilization_action_id=action.id,
            preceding_action_ids=tuple(
                item.id
                for item in draft_plan.actions[: draft_plan.actions.index(action)]
            ),
            sha256=sha256(path.read_bytes()).hexdigest(),
            encode_contract=canonical_video_encode_contract(
                draft_plan.effective_config
            ),
            actual_pts=actual_pts,
            normalized_pts_digest=stabilization_actual_pts_digest(actual_pts),
            stream_topology_digest="d" * 64,
            frame_count=len(actual_pts),
            cleanup_paths=(path,),
        )


class _NoPassStabilizationQualifier:
    def qualify(
        self,
        draft_plan: RescuePlan,
        parent: StabilizationImmediateParentHandle,
        _work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> object:
        assert not cancellation_callback()
        action = next(
            item
            for item in draft_plan.actions
            if item.kind is RescueActionKind.STABILIZE
        )
        profile = draft_plan.effective_config.stabilization_qualification_profiles[0]
        config = StabilizationConfig.model_validate_json(
            json.dumps(action.parameters["config"])
        )
        measurement = StabilizationProfileMeasurementV1(
            profile=profile,
            parent_sha256=parent.sha256,
            control_sha256="1" * 64,
            candidate_sha256="2" * 64,
            encode_contract=parent.encode_contract,
            source_ranges=action.source_ranges,
            actual_pts=parent.actual_pts,
            parent_normalized_pts_digest=parent.normalized_pts_digest,
            control_normalized_pts_digest=parent.normalized_pts_digest,
            candidate_normalized_pts_digest=parent.normalized_pts_digest,
            parent_stream_topology_digest=parent.stream_topology_digest,
            control_stream_topology_digest=parent.stream_topology_digest,
            candidate_stream_topology_digest=parent.stream_topology_digest,
            parent_frame_count=parent.frame_count,
            control_frame_count=parent.frame_count,
            candidate_frame_count=parent.frame_count,
            action_parameters=action.parameters,
            metrics=StabilizationQualificationMetricsV1(
                range_coverage_ratio=1.0,
                expected_frames=float(parent.frame_count),
                reliable_transforms=float(parent.frame_count),
                residual_median_pixels=0.6,
                residual_p90_pixels=0.8,
                crop_ratio=0.05,
                transition_consensus_coverage_ratio=1.0,
                transition_consensus_p90_pixels=0.1,
                transition_seam_residual_pixels=0.1,
                transition_expected_frames=float(
                    sum(0.5 <= value < 1.0 for value in parent.actual_pts)
                ),
                transition_reliable_frames=float(
                    sum(0.5 <= value < 1.0 for value in parent.actual_pts)
                ),
                transition_boundary_path_residual_pixels=0.1,
            ),
            thresholds=stabilization_qualification_thresholds(config),
        )
        return build_stabilization_qualification_evidence(
            draft_plan, (measurement,), parent=parent
        )


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RescueArtifactError("path escape"), RescueArtifactError),
        (RescueMediaError("hash or topology drift"), RescueMediaError),
        (ValueError("PTS contract drift"), RescuePlanError),
        (RuntimeError("measurement or cleanup failure"), RescuePlanError),
    ],
)
def test_prepare_stabilization_contract_failures_fail_closed(
    tmp_path: Path,
    failure: Exception,
    expected: type[Exception],
) -> None:
    class FailingQualifier:
        def qualify(self, *_args: object) -> object:
            raise failure

    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        planner=_transition_stabilization_planner,
        stabilization_candidate_qualifier=FailingQualifier(),
        stabilization_parent_provider=_StabilizationParentProvider(),
    )

    with pytest.raises(expected):
        pipeline.prepare(source)


def test_prepare_stabilization_default_unavailable_preserves_exact_draft(
    tmp_path: Path,
) -> None:
    class MustNotRunQualifier:
        def qualify(self, *_args: object) -> object:
            raise AssertionError("candidate qualifier must not run without a parent")

    drafts: list[RescuePlan] = []

    def planner(**inputs: Any) -> RescuePlan:
        plan = _transition_stabilization_planner(**inputs)
        drafts.append(plan)
        return plan

    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        planner=planner,
        stabilization_candidate_qualifier=MustNotRunQualifier(),
    )

    preparation = pipeline.prepare(source)

    assert len(drafts) == 1
    assert preparation.plan == drafts[0]
    pipeline.abort(preparation)


@pytest.mark.parametrize("no_pass", [False, True])
def test_prepare_stabilization_exact_fallback_cleans_immediate_parent(
    tmp_path: Path,
    no_pass: bool,
) -> None:
    class ExplicitUnavailableQualifier:
        def qualify(self, *_args: object) -> object:
            raise RescueQualificationUnavailableError("injected unavailable")

    drafts: list[RescuePlan] = []

    def planner(**inputs: Any) -> RescuePlan:
        plan = _transition_stabilization_planner(**inputs)
        drafts.append(plan)
        return plan

    provider = _StabilizationParentProvider()
    qualifier: object = (
        _NoPassStabilizationQualifier() if no_pass else ExplicitUnavailableQualifier()
    )
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        planner=planner,
        stabilization_candidate_qualifier=qualifier,
        stabilization_parent_provider=provider,
        keep_workspace=True,
    )

    preparation = pipeline.prepare(source)

    assert preparation.plan == drafts[0]
    assert preparation.plan.actions[0].id == drafts[0].actions[0].id
    assert preparation.plan.actions[0].parameters == drafts[0].actions[0].parameters
    assert preparation.plan.plan_digest == drafts[0].plan_digest
    assert provider.paths and all(not path.exists() for path in provider.paths)
    assert all(not path.parent.exists() for path in provider.paths)
    pipeline.abort(preparation)


def test_prepare_stabilization_cancellation_propagates_and_cleans_parent(
    tmp_path: Path,
) -> None:
    class CancelledQualifier:
        def qualify(self, *_args: object) -> object:
            raise RescueCancelledError("injected cancellation")

    provider = _StabilizationParentProvider()
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        planner=_transition_stabilization_planner,
        stabilization_candidate_qualifier=CancelledQualifier(),
        stabilization_parent_provider=provider,
        keep_workspace=True,
    )

    with pytest.raises(RescueCancelledError):
        pipeline.prepare(source)
    assert provider.paths and all(not path.exists() for path in provider.paths)
    assert all(not path.parent.exists() for path in provider.paths)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RescueQualificationUnavailableError("unavailable"), None),
        (RescueArtifactError("integrity"), RescueArtifactError),
        (RescueMediaError("integrity"), RescueMediaError),
        (RuntimeError("provider failed"), RescuePlanError),
        (RescueCancelledError("cancelled"), RescueCancelledError),
    ],
)
def test_prepare_cleans_partial_parent_when_provider_fails_before_handle(
    tmp_path: Path,
    failure: Exception,
    expected: type[Exception] | None,
) -> None:
    class PartialParentProvider:
        root: Path | None = None

        def provide(
            self,
            _draft_plan: RescuePlan,
            _source: Path,
            work_root: Path,
            _cancellation_callback: Callable[[], bool],
        ) -> object:
            self.root = work_root
            work_root.mkdir(parents=True, exist_ok=True)
            (work_root / "partial-parent.private").write_bytes(b"partial parent")
            raise failure

    drafts: list[RescuePlan] = []

    def planner(**inputs: Any) -> RescuePlan:
        plan = _transition_stabilization_planner(**inputs)
        drafts.append(plan)
        return plan

    provider = PartialParentProvider()
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        planner=planner,
        stabilization_parent_provider=provider,
        keep_workspace=True,
    )

    if expected is None:
        preparation = pipeline.prepare(source)
        assert preparation.plan == drafts[0]
        pipeline.abort(preparation)
    else:
        with pytest.raises(expected):
            pipeline.prepare(source)
    assert provider.root is not None
    assert not provider.root.exists()


def test_prepare_parent_cleanup_failure_overrides_unavailable_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial: Path | None = None

    class PartialUnavailableProvider:
        def provide(
            self,
            _draft_plan: RescuePlan,
            _source: Path,
            work_root: Path,
            _cancellation_callback: Callable[[], bool],
        ) -> object:
            nonlocal partial
            work_root.mkdir(parents=True, exist_ok=True)
            partial = work_root / "partial-parent.private"
            partial.write_bytes(b"partial parent")
            raise RescueQualificationUnavailableError("unavailable")

    original_unlink = Path.unlink

    def fail_partial_unlink(path: Path, missing_ok: bool = False) -> None:
        if partial is not None and path == partial:
            raise OSError("injected cleanup failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_partial_unlink)
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        planner=_transition_stabilization_planner,
        stabilization_parent_provider=PartialUnavailableProvider(),
        keep_workspace=True,
    )

    with pytest.raises(RescueArtifactError):
        pipeline.prepare(source)


@pytest.mark.parametrize("mutation", ["raw_source", "empty_cleanup", "symlink"])
def test_prepare_rejects_unowned_parent_before_candidate_qualifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    class MutatingParentProvider(_StabilizationParentProvider):
        def provide(
            self,
            draft_plan: RescuePlan,
            source: Path,
            work_root: Path,
            cancellation_callback: Callable[[], bool],
        ) -> StabilizationImmediateParentHandle:
            handle = super().provide(
                draft_plan, source, work_root, cancellation_callback
            )
            if mutation == "raw_source":
                object.__setattr__(handle, "path", source)
                object.__setattr__(handle, "cleanup_paths", (source,))
            elif mutation == "empty_cleanup":
                object.__setattr__(handle, "cleanup_paths", ())
            else:
                linked = work_root / "linked-parent.private"
                try:
                    linked.symlink_to(handle.path)
                except OSError:
                    linked.write_bytes(handle.path.read_bytes())
                    original_is_symlink = Path.is_symlink
                    monkeypatch.setattr(
                        Path,
                        "is_symlink",
                        lambda value: value == linked or original_is_symlink(value),
                    )
                object.__setattr__(handle, "path", linked)
                object.__setattr__(
                    handle, "cleanup_paths", (*handle.cleanup_paths, linked)
                )
            return handle

    class CountingQualifier:
        calls = 0

        def qualify(self, *_args: object) -> object:
            self.calls += 1
            raise AssertionError("unowned parent reached candidate qualifier")

    provider = MutatingParentProvider()
    qualifier = CountingQualifier()
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        planner=_transition_stabilization_planner,
        stabilization_candidate_qualifier=qualifier,
        stabilization_parent_provider=provider,
        keep_workspace=True,
    )

    with pytest.raises(RescueArtifactError):
        pipeline.prepare(source)
    assert qualifier.calls == 0
    assert source.read_bytes() == b"local video"
    assert provider.paths and all(not path.parent.exists() for path in provider.paths)


@pytest.mark.parametrize("existing_kind", ["directory", "file", "symlink"])
def test_prepare_no_clobber_failure_preserves_preexisting_parent_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_kind: str,
) -> None:
    parent_root = (
        tmp_path / "输出 job" / "rescue-review-private" / "stabilization-parent"
    )
    external = tmp_path / "preexisting-external"
    sentinel: Path | None = None
    initialized = False

    def planner(**inputs: Any) -> RescuePlan:
        nonlocal initialized, sentinel
        if not initialized:
            initialized = True
            if existing_kind == "directory":
                parent_root.mkdir()
                sentinel = parent_root / "preexisting.private"
                sentinel.write_bytes(b"do not touch")
            elif existing_kind == "file":
                parent_root.write_bytes(b"do not touch")
                sentinel = parent_root
            else:
                external.mkdir()
                sentinel = external / "preexisting.private"
                sentinel.write_bytes(b"do not touch")
                try:
                    parent_root.symlink_to(external, target_is_directory=True)
                except OSError:
                    parent_root.write_bytes(b"junction-like placeholder")
                    original_is_symlink = Path.is_symlink
                    monkeypatch.setattr(
                        Path,
                        "is_symlink",
                        lambda value: (
                            value == parent_root or original_is_symlink(value)
                        ),
                    )
        return _transition_stabilization_planner(**inputs)

    class CountingBoundary:
        calls = 0

        def provide(self, *_args: object) -> object:
            self.calls += 1
            raise AssertionError("provider ran without root ownership")

        def qualify(self, *_args: object) -> object:
            self.calls += 1
            raise AssertionError("qualifier ran without root ownership")

    provider = CountingBoundary()
    qualifier = CountingBoundary()
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        planner=planner,
        stabilization_parent_provider=provider,
        stabilization_candidate_qualifier=qualifier,
        keep_workspace=True,
    )

    with pytest.raises(RescuePlanError):
        pipeline.prepare(source)
    assert provider.calls == 0
    assert qualifier.calls == 0
    assert parent_root.exists() or parent_root.is_symlink()
    assert sentinel is not None
    assert sentinel.read_bytes() == b"do not touch"


def test_progress_failure_before_verifier_still_cleans_runtime_controls(
    tmp_path: Path,
) -> None:
    retained: list[Path] = []

    class ControlExecutor(_Executor):
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
            control = work_root / "staging" / "runtime-control.private.mp4"
            control.write_bytes(b"control")
            retained.append(control)

            class Handle:
                cleanup_paths = (control,)

            return replace(result, verification_controls=cast(Any, (Handle(),)))

    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        executor=ControlExecutor(),
        keep_workspace=True,
    )
    preparation = pipeline.prepare(source)
    confirmation = _confirmation(preparation)
    pipeline.confirm(preparation, confirmation)

    def progress(status: RescueStatus) -> None:
        if status is RescueStatus.VERIFYING:
            raise RuntimeError("injected verification progress failure")

    pipeline._progress = progress
    with pytest.raises(RuntimeError, match="progress failure"):
        pipeline.execute(preparation, confirmation)

    assert retained and all(not path.exists() for path in retained)


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
        publish_improved="improved-viewing.mp4" in preparation.plan.public_artifacts,
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


def test_prepare_qualifies_sharpen_before_final_plan_and_preview(
    tmp_path: Path,
) -> None:
    qualifier = _CandidateQualifier()
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.SOFT_DETAIL,
        assessment_service=_SharpenAssessmentService(),
        candidate_qualifier=qualifier,
    )

    preparation = pipeline.prepare(source)

    action = next(
        item
        for item in preparation.plan.actions
        if item.kind is RescueActionKind.SHARPEN
    )
    assert qualifier.calls == 1
    assert action.id != qualifier.draft_action_id
    assert action.parameters["qualification_version"] == "1"
    assert action.parameters["qualification_profile_id"] == "full"
    assert all(
        any(
            start < preview_end and preview_start < end
            for preview_start, preview_end in preparation.plan.preview_ranges
        )
        for start, end in action.source_ranges
    )
    base_payload = preparation.plan.model_dump(mode="json")
    for mutation in (
        "nested_threshold",
        "top_profile",
        "top_metrics",
        "draft_action_id",
        "top_strength",
    ):
        payload = copy.deepcopy(base_payload)
        action_payload = next(
            item for item in payload["actions"] if item["kind"] == "sharpen"
        )
        parameters = action_payload["parameters"]
        if mutation == "nested_threshold":
            parameters["qualification"]["profile_measurements"][0]["thresholds"][
                "maximum_noise_increase"
            ] = 0.5
        elif mutation == "top_profile":
            parameters["qualification_profile_id"] = "gentle"
        elif mutation == "top_metrics":
            parameters["qualification_metrics"]["maximum_noise_increase"] = 0.5
        elif mutation == "draft_action_id":
            parameters["qualification"]["draft_action_id"] = "tampered-draft"
        else:
            parameters["amount"] = float(parameters["amount"]) + 0.1
        action_payload["id"] = make_rescue_action_id(
            kind=RescueActionKind(action_payload["kind"]),
            parameters=parameters,
            source_ranges=tuple(
                tuple(value) for value in action_payload["source_ranges"]
            ),
            strategy=RescueStrategy(action_payload["strategy"]),
            version=action_payload["version"],
        )
        payload["plan_digest"] = make_rescue_plan_digest(
            {key: value for key, value in payload.items() if key != "plan_digest"}
        )
        with pytest.raises(ValueError, match="SHARPEN"):
            RescuePlan.model_validate_json(json.dumps(payload, ensure_ascii=False))
    pipeline.abort(preparation)


def test_prepare_omits_sharpen_when_no_full_range_profile_passes(
    tmp_path: Path,
) -> None:
    qualifier = _CandidateQualifier(pass_profiles=False)
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.SOFT_DETAIL,
        assessment_service=_SharpenAssessmentService(),
        candidate_qualifier=qualifier,
    )

    preparation = pipeline.prepare(source)

    assert qualifier.calls == 1
    assert all(
        action.kind is not RescueActionKind.SHARPEN
        for action in preparation.plan.actions
    )
    assert SHARPEN_QUALIFICATION_LIMITATION in (preparation.plan.assessment_limitations)
    pipeline.abort(preparation)


def test_prepare_omits_sharpen_when_qualification_provider_fails(
    tmp_path: Path,
) -> None:
    class FailingQualifier:
        def qualify(self, *_args: object) -> object:
            raise RescueMediaError("injected qualification failure")

    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.SOFT_DETAIL,
        assessment_service=_SharpenAssessmentService(),
        candidate_qualifier=FailingQualifier(),
    )
    preparation = pipeline.prepare(source)
    assert all(
        action.kind is not RescueActionKind.SHARPEN
        for action in preparation.plan.actions
    )
    assert SHARPEN_QUALIFICATION_UNAVAILABLE_LIMITATION in (
        preparation.plan.assessment_limitations
    )
    pipeline.abort(preparation)


def test_prepare_omits_tonal_action_when_encoded_qualification_is_unavailable(
    tmp_path: Path,
) -> None:
    class FailingTonalQualifier:
        def __init__(self) -> None:
            self.calls = 0

        def qualify(self, *_args: object) -> object:
            self.calls += 1
            raise RescueMediaError("injected encoded tonal qualification failure")

    qualifier = FailingTonalQualifier()
    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.AUDIO_NOISE,
        assessment_service=_TonalAssessmentService(),
        tonal_candidate_qualifier=qualifier,
    )

    preparation = pipeline.prepare(source)

    assert qualifier.calls == 1
    assert all(
        not (
            action.kind is RescueActionKind.DENOISE_AUDIO
            and action.parameters.get("interference_profiles")
        )
        for action in preparation.plan.actions
    )
    assert TONAL_ENCODED_QUALIFICATION_UNAVAILABLE_LIMITATION in (
        preparation.plan.assessment_limitations
    )
    pipeline.abort(preparation)


def test_prepare_persists_returned_failed_tonal_evidence_before_replanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[tuple[Any, Path]] = []
    monkeypatch.setattr(
        rescue_pipeline_module,
        "write_tonal_encoded_qualification_json",
        lambda evidence, path: writes.append((evidence, path)),
    )

    class FailedTonalQualifier:
        def qualify(self, draft: RescuePlan, *_args: object) -> object:
            action = next(
                item
                for item in draft.actions
                if item.kind is RescueActionKind.DENOISE_AUDIO
                and item.parameters.get("interference_profiles")
            )
            return SimpleNamespace(
                input_hash=draft.input_hash,
                draft_action_id=action.id,
                draft_parameters=action.parameters,
                source_ranges=action.source_ranges,
                passed=False,
            )

    pipeline, source, _executor, _verifier, _damage = _pipeline(
        tmp_path,
        strategy="balanced",
        damage_kind=DamageKind.AUDIO_NOISE,
        assessment_service=_TonalAssessmentService(),
        tonal_candidate_qualifier=FailedTonalQualifier(),
    )

    preparation = pipeline.prepare(source)

    assert len(writes) == 1
    evidence, path = writes[0]
    assert evidence.passed is False
    assert path.name == "tonal-qualification-evidence-private.json"
    assert path.parent.name == "rescue-review-private"
    assert path.is_relative_to(tmp_path)
    assert all(
        not (
            action.kind is RescueActionKind.DENOISE_AUDIO
            and action.parameters.get("interference_profiles")
        )
        for action in preparation.plan.actions
    )
    pipeline.abort(preparation)


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


def test_sharpen_supports_improved_while_deblur_remains_faithful(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"local video")
    source_hash = sha256(source.read_bytes()).hexdigest()
    plan = build_rescue_plan(
        metadata=_metadata(source),
        damage_map=_damage_map(source_hash, kind=DamageKind.SOFT_DETAIL),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=VisualAssessment(
            metrics=VisualMetrics(
                luma_p10=0.1,
                luma_p50=0.2,
                luma_p90=0.4,
                low_clip_ratio=0.0,
                high_clip_ratio=0.0,
                noise_residual=0.01,
                sharpness=0.04,
            ),
            recommended_actions=(RescueActionKind.SHARPEN,),
            preview_required=True,
            public_explanation="Measured soft detail supports sharpening.",
        ),
    )
    sharpen = next(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )

    assert rescue_pipeline_module._has_supported_improvement(plan)
    sharpen_ledger = rescue_pipeline_module._action_execution_ledger(
        plan,
        improved_path=Path("improved-viewing.mp4"),
        improvement_failure=False,
    )
    assert (
        next(
            item for item in sharpen_ledger if item.action_id == sharpen.id
        ).artifact_role
        == "improved"
    )

    estimate = BlurKernelEstimate(
        kernel_kind="gaussian",
        radius=3,
        regularization=0.003,
        confidence=0.95,
        edge_width_before=5.0,
        predicted_edge_width_after=2.5,
        edge_continuity_ratio=0.95,
        reblur_error_ratio=0.02,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )
    deblur_plan = build_rescue_plan(
        metadata=_metadata(source),
        damage_map=_damage_map(source_hash, kind=DamageKind.SOFT_DETAIL),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters={
            "deblur_measurements": [
                {
                    "algorithm_version": "1",
                    "source_ranges": [[0.5, 1.0]],
                    "estimate": estimate.model_dump(mode="json"),
                    "config": DeblurConfig().model_dump(mode="json"),
                }
            ]
        },
    )
    deblur = next(
        action
        for action in deblur_plan.actions
        if action.kind is RescueActionKind.DEBLUR
    )
    assert not rescue_pipeline_module._has_supported_improvement(deblur_plan)
    deblur_ledger = rescue_pipeline_module._action_execution_ledger(
        deblur_plan,
        improved_path=None,
        improvement_failure=False,
    )
    assert (
        next(
            item for item in deblur_ledger if item.action_id == deblur.id
        ).artifact_role
        == "faithful"
    )


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
    assert result.improved_path is None
    assert result.technical_report is not None
    assert result.technical_report.outcome.value == expected
    assert result.technical_report.verification.improved_status is improved_status
    if improved_status is RescueVerificationStatus.NEEDS_REVIEW:
        assert any(
            check.artifact == "improved"
            and check.status is RescueVerificationStatus.NEEDS_REVIEW
            for check in result.technical_report.verification.checks
        )


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
