"""Tests for bounded, private, same-range Rescue previews."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from videoscope.domain import VideoMetadata
from videoscope.rescue.commands import (
    build_improved_viewing_command,
    build_preview_commands,
)
from videoscope.rescue.deblur import BlurKernelEstimate, DeblurConfig
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueMediaError,
)
from videoscope.rescue.executor import CommandResult, SourceMapping
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    canonical_video_encode_contract,
    make_damage_id,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.planner import _apply_sharpen_qualification, build_rescue_plan
from videoscope.rescue.preview import RescuePreviewBuilder, SubprocessPreviewRunner
from videoscope.rescue.qualification import (
    SharpenProfileMeasurementV1,
    SharpenQualificationEvidenceV1,
    SharpenQualificationMetricsV1,
    SharpenQualificationThresholdsV1,
    build_sharpen_qualification_evidence,
)
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationAssessment,
    StabilizationConfig,
)
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
    render_tonal_interference_reduced_audio,
)
from videoscope.rescue.tonal_qualification import (
    TonalAudioEncodeContractV2,
    TonalAudioTimelineV1,
    TonalAudioTopologyV2,
    TonalEncodedCandidateAttemptV2,
    TonalEncodedMetricsV2,
    TonalEncodedProfileQualificationV2,
    TonalEncodedQualificationEvidenceV3,
    TonalEncodedThresholdsV2,
    TonalRangeMappingV2,
)
from videoscope.rescue.visual import (
    SharpenConfig,
    VisualAssessment,
    VisualAssessmentConfig,
    VisualEvidence,
    VisualMetrics,
)


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command: list[str]) -> None:
        self.commands.append(command)
        Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(command[-1]).write_bytes(b"preview")


def _tonal_qualification(
    duration_seconds: float, *, channel_count: int
) -> TonalRenderQualification:
    return TonalRenderQualification(
        boundary_mode="full_interval_v1",
        notch_q=8.0,
        complete_window_count=(int(duration_seconds / 0.05 + 1e-9) * channel_count),
        minimum_target_reduction_db=25.0,
        maximum_non_target_attenuation_db=0.1,
        maximum_boundary_energy_jump_db=0.1,
        maximum_boundary_crest_jump_db=0.1,
        maximum_boundary_adjacent_delta=0.01,
    )


def _qualified_tonal_evidence(plan: RescuePlan) -> TonalEncodedQualificationEvidenceV3:
    draft = next(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
        and action.parameters.get("interference_profiles")
    )
    config = TonalInterferenceConfig.model_validate_json(
        json.dumps(draft.parameters["config"])
    )
    profiles = tuple(
        InterferenceTone.model_validate_json(json.dumps(item))
        for item in cast(list[object], draft.parameters["interference_profiles"])
    )
    topology_payload = {
        "codec_name": "aac",
        "codec_tag_string": "mp4a",
        "profile": "LC",
        "sample_fmt": "fltp",
        "sample_rate_hz": 48_000,
        "channels": 2,
        "channel_layout": "stereo",
        "time_base": "1/48000",
    }
    topology = TonalAudioTopologyV2.model_validate(
        {
            **topology_payload,
            "topology_sha256": hashlib.sha256(
                json.dumps(
                    topology_payload,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        }
    )
    timeline_tokens = ["0", "20"]
    timeline = TonalAudioTimelineV1(
        packet_count=2,
        first_normalized_pts_seconds=0.0,
        last_normalized_pts_seconds=20.0,
        normalized_pts_sha256=hashlib.sha256(
            json.dumps(timeline_tokens, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
    )
    thresholds = tuple(
        TonalEncodedThresholdsV2(
            minimum_target_reduction_db=profile.attenuation_target_db,
            maximum_non_target_attenuation_db=(
                config.max_non_target_band_attenuation_db
            ),
            maximum_boundary_energy_jump_db=config.max_boundary_energy_jump_db,
            maximum_boundary_crest_jump_db=config.max_boundary_crest_jump_db,
            maximum_boundary_adjacent_delta=config.max_boundary_adjacent_delta,
        )
        for profile in profiles
    )
    metrics = tuple(
        TonalEncodedMetricsV2(
            range_coverage_ratio=1.0,
            measured_windows=cast(
                TonalRenderQualification, profile.render_qualification
            ).complete_window_count,
            excluded_transition_windows=0,
            minimum_target_reduction_db=profile.attenuation_target_db + 1.0,
            minimum_target_margin_db=1.0,
            maximum_non_target_attenuation_db=0.1,
            maximum_boundary_energy_jump_db=0.1,
            maximum_boundary_crest_jump_db=0.1,
            maximum_boundary_adjacent_delta=0.01,
        )
        for profile in profiles
    )
    attempts = tuple(
        TonalEncodedCandidateAttemptV2(
            notch_q=config.render_qualification_notch_q_values[0],
            candidate_sha256=f"{index + 3:x}" * 64,
            candidate_audio_topology=topology,
            metrics=profile_metrics,
            thresholds=profile_thresholds,
        )
        for index, (profile_metrics, profile_thresholds) in enumerate(
            zip(metrics, thresholds, strict=True)
        )
    )
    selected_profiles = tuple(
        profile.model_copy(
            update={
                "render_qualification": TonalRenderQualification(
                    boundary_mode="full_interval_v1",
                    notch_q=attempt.notch_q,
                    complete_window_count=profile_metrics.measured_windows,
                    minimum_target_reduction_db=(
                        profile_metrics.minimum_target_reduction_db
                    ),
                    maximum_non_target_attenuation_db=(
                        profile_metrics.maximum_non_target_attenuation_db
                    ),
                    maximum_boundary_energy_jump_db=(
                        profile_metrics.maximum_boundary_energy_jump_db
                    ),
                    maximum_boundary_crest_jump_db=(
                        profile_metrics.maximum_boundary_crest_jump_db
                    ),
                    maximum_boundary_adjacent_delta=(
                        profile_metrics.maximum_boundary_adjacent_delta
                    ),
                )
            }
        )
        for profile, attempt, profile_metrics in zip(
            profiles, attempts, metrics, strict=True
        )
    )
    return TonalEncodedQualificationEvidenceV3(
        input_hash=plan.input_hash,
        draft_action_id=draft.id,
        draft_parameters=dict(draft.parameters),
        source_ranges=draft.source_ranges,
        output_ranges=draft.source_ranges,
        range_mappings=(
            TonalRangeMappingV2(
                source_start=0.0,
                source_end=20.0,
                output_start=0.0,
                output_end=20.0,
            ),
        ),
        audio_encode_contract=TonalAudioEncodeContractV2(
            parent_bitrate_kbps=192,
            candidate_bitrate_kbps=192,
        ),
        parent_sha256="1" * 64,
        parent_audio_topology=topology,
        boundary_control_sha256="2" * 64,
        boundary_control_audio_topology=topology,
        boundary_control_audio_timeline=timeline,
        profile_candidate_audio_timelines=tuple((timeline,) for _ in profiles),
        combined_audio_timeline=timeline,
        profile_qualifications=tuple(
            TonalEncodedProfileQualificationV2(
                profile_index=index,
                attempts=(attempt,),
                selected_notch_q=attempt.notch_q,
            )
            for index, attempt in enumerate(attempts)
        ),
        combined_candidate_sha256="f" * 64,
        combined_audio_topology=topology,
        combined_metrics=metrics,
        combined_thresholds=thresholds,
        selected_profiles=selected_profiles,
    )


def _qualified_sharpen_plan(plan: RescuePlan) -> RescuePlan:
    draft = next(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )
    parameters = draft.parameters
    thresholds = SharpenQualificationThresholdsV1(
        minimum_aggregate_gain_ratio=float(
            cast(float, parameters["minimum_perceptible_sharpness_gain_ratio"])
        ),
        minimum_recovered_baseline_ratio=float(
            cast(float, parameters["minimum_recovered_baseline_ratio"])
        ),
        minimum_improved_frame_fraction=float(
            cast(float, parameters["minimum_improved_frame_fraction"])
        ),
        maximum_noise_increase=cast(float, parameters["maximum_noise_increase"]),
        maximum_edge_overshoot_ratio=float(
            cast(float, parameters["maximum_edge_overshoot_ratio"])
        ),
        maximum_edge_overshoot_amplitude=float(
            cast(float, parameters["maximum_edge_overshoot_amplitude"])
        ),
        maximum_ringing_ratio=cast(float, parameters["maximum_ringing_ratio"]),
    )
    measurements = tuple(
        SharpenProfileMeasurementV1(
            profile=profile,
            baseline_sha256="1" * 64,
            visibility_control_sha256=f"{index + 4:x}" * 64,
            candidate_sha256=f"{index + 7:x}" * 64,
            normalized_pts_digest="a" * 64,
            stream_topology_digest="b" * 64,
            decoded_width=64,
            decoded_height=64,
            generation_count=1,
            inventory_frame_count=24,
            metrics=SharpenQualificationMetricsV1(
                range_coverage_ratio=1.0,
                expected_frames=24,
                compared_frames=24,
                range_count=len(draft.source_ranges),
                passing_range_count=len(draft.source_ranges),
                minimum_aggregate_gain_ratio=1.0,
                minimum_recovered_baseline_ratio=1.0,
                minimum_improved_frame_fraction=1.0,
                maximum_noise_increase=0.0,
                maximum_edge_overshoot_ratio=0.0,
                maximum_edge_overshoot_amplitude=0.0,
                maximum_ringing_ratio=0.0,
            ),
            thresholds=thresholds,
        )
        for index, profile in enumerate(
            plan.effective_config.sharpen_qualification_profiles
        )
    )
    evidence = build_sharpen_qualification_evidence(
        input_hash=plan.input_hash,
        draft_action_id=draft.id,
        draft_parameters=parameters,
        source_ranges=draft.source_ranges,
        output_ranges=draft.source_ranges,
        encode_contract=canonical_video_encode_contract(plan.effective_config),
        configured_profiles=plan.effective_config.sharpen_qualification_profiles,
        measurements=measurements,
    )
    qualified = _apply_sharpen_qualification(
        (draft,), evidence, input_hash=plan.input_hash, config=plan.effective_config
    )[0]
    payload = plan.model_dump(mode="json", exclude={"plan_digest"})
    payload["actions"] = [
        (qualified if action.id == draft.id else action).model_dump(mode="json")
        for action in plan.actions
    ]
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _stale_sharpen_output_mapping_plan(plan: RescuePlan) -> RescuePlan:
    """Rehash a plan after changing only its qualified output-range mapping."""
    sharpen = next(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )
    evidence = SharpenQualificationEvidenceV1.model_validate(
        sharpen.parameters["qualification"]
    )
    stale_evidence = evidence.model_copy(
        update={
            "output_ranges": tuple(
                (start + 0.25, end + 0.25) for start, end in evidence.output_ranges
            )
        }
    )
    parameters = {
        **sharpen.parameters,
        "qualification": stale_evidence.model_dump(mode="json"),
    }
    stale_action = sharpen.model_copy(
        update={
            "id": make_rescue_action_id(
                kind=sharpen.kind,
                parameters=parameters,
                source_ranges=sharpen.source_ranges,
                strategy=sharpen.strategy,
                version=sharpen.version,
            ),
            "parameters": parameters,
        }
    )
    payload = plan.model_dump(mode="json", exclude={"plan_digest"})
    payload["actions"] = [
        (stale_action if action.id == sharpen.id else action).model_dump(mode="json")
        for action in plan.actions
    ]
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _dark_visual_assessment(
    *ranges: tuple[float, float],
) -> VisualAssessment:
    return VisualAssessment(
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
        evidence=tuple(
            VisualEvidence(
                action=RescueActionKind.ADJUST_LUMA,
                timestamp_seconds=(start + end) / 2.0,
                metric="luma_p10",
                observed=0.05,
                threshold=0.18,
                context_luma_p50=0.08,
            )
            for start, end in ranges
        ),
        preview_required=True,
        public_explanation="Measured dark samples support a preview.",
    )


def _planner_multirange_sharpen_plan(
    *, delete_first_range: bool, sharpen_config: SharpenConfig | None = None
) -> tuple[RescuePlan, MediaDamageMap]:
    source_hash = "6" * 64
    ranges = ((2.0, 3.0), (4.0, 5.0))
    soft = tuple(
        DamageInterval(
            id=make_damage_id(
                source_hash, "video:0", DamageKind.SOFT_DETAIL, start, end
            ),
            stream_id="video:0",
            kind=DamageKind.SOFT_DETAIL,
            start_seconds=start,
            end_seconds=end,
        )
        for start, end in ranges
    )
    undecodable = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.UNDECODABLE, 2.0, 3.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=2.0,
        end_seconds=3.0,
    )
    damage_map = MediaDamageMap(
        input_hash=source_hash,
        duration_seconds=6.0,
        intervals=((*((undecodable,) if delete_first_range else ()), *soft)),
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=6.0,
            average_frame_rate=24.0,
            estimated_frame_count=144,
            has_audio=False,
            file_size_bytes=1,
        ),
        damage_map=damage_map,
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters=(
            {
                "visual_config": VisualAssessmentConfig(
                    sharpen=sharpen_config
                ).model_dump(mode="json")
            }
            if sharpen_config is not None
            else None
        ),
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
    return _qualified_sharpen_plan(plan), damage_map


def test_deleted_multirange_sharpen_is_rejected_before_preview_runner(
    tmp_path: Path,
) -> None:
    plan, _damage_map = _planner_multirange_sharpen_plan(delete_first_range=True)
    repeated, _ = _planner_multirange_sharpen_plan(delete_first_range=True)
    sharpen = next(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )
    runner = FakeRunner()

    assert sharpen.source_ranges == ((2.0, 3.0), (4.0, 5.0))
    assert plan.plan_digest == repeated.plan_digest
    assert tuple(action.id for action in plan.actions) == tuple(
        action.id for action in repeated.actions
    )

    with pytest.raises(RescueMediaError):
        RescuePreviewBuilder(runner=runner).build(
            plan, tmp_path / "source.mp4", tmp_path / "private"
        )

    assert runner.commands == []


def test_preview_rejects_tampered_action_encode_contract_before_writing(
    tmp_path: Path,
) -> None:
    plan, _damage_map = _planner_multirange_sharpen_plan(delete_first_range=False)
    sharpen = next(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )
    raw_contract = sharpen.parameters["video_encode_contract"]
    assert isinstance(raw_contract, dict)
    tampered_contract = dict(raw_contract)
    tampered_contract["track_timescale"] = 12288
    object.__setattr__(
        sharpen,
        "parameters",
        {**sharpen.parameters, "video_encode_contract": tampered_contract},
    )
    private_root = tmp_path / "private"

    with pytest.raises(RescueMediaError, match="could not be processed locally") as exc:
        RescuePreviewBuilder(runner=FakeRunner()).build(
            plan, tmp_path / "source.mp4", private_root
        )

    assert exc.value.internal_message == (
        "confirmed preview video encode contract is invalid"
    )
    assert not private_root.exists()


def test_preview_command_rejects_rehashed_stale_sharpen_output_mapping(
    tmp_path: Path,
) -> None:
    plan, _damage_map = _planner_multirange_sharpen_plan(delete_first_range=False)
    stale = _stale_sharpen_output_mapping_plan(plan)

    with pytest.raises(ValueError, match="output ranges differ"):
        build_preview_commands(
            stale, tmp_path / "source.mp4", tmp_path / "private command"
        )


def test_improved_command_rejects_rehashed_stale_sharpen_output_mapping(
    tmp_path: Path,
) -> None:
    plan, _damage_map = _planner_multirange_sharpen_plan(delete_first_range=False)
    stale = _stale_sharpen_output_mapping_plan(plan)

    with pytest.raises(ValueError, match="output ranges differ"):
        build_improved_viewing_command(
            stale,
            tmp_path / "faithful.mp4",
            tmp_path / "improved.mp4",
            source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
        )


def test_preview_rejects_stale_sharpen_output_mapping_before_runner(
    tmp_path: Path,
) -> None:
    plan, _damage_map = _planner_multirange_sharpen_plan(delete_first_range=False)
    stale = _stale_sharpen_output_mapping_plan(plan)
    runner = FakeRunner()

    with pytest.raises(RescueMediaError):
        RescuePreviewBuilder(runner=runner).build(
            stale, tmp_path / "source.mp4", tmp_path / "private preview"
        )

    assert runner.commands == []


def test_retained_multirange_sharpen_is_bound_only_to_rendered_improved(
    tmp_path: Path,
) -> None:
    plan, _damage_map = _planner_multirange_sharpen_plan(delete_first_range=False)
    sharpen = next(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )
    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan, tmp_path / "source.mp4", tmp_path / "private"
    )

    assert previews.improved is not None
    assert sharpen.id in previews.previewed_action_ids


def test_custom_sharpen_threshold_action_id_is_preview_bound(tmp_path: Path) -> None:
    default, _damage_map = _planner_multirange_sharpen_plan(delete_first_range=False)
    strict, _damage_map = _planner_multirange_sharpen_plan(
        delete_first_range=False,
        sharpen_config=SharpenConfig(
            minimum_recovered_baseline_ratio=0.91,
            minimum_improved_frame_fraction=0.95,
            maximum_noise_increase=0.007,
            maximum_edge_overshoot_ratio=0.02,
            maximum_ringing_ratio=0.03,
        ),
    )
    default_action = next(
        action for action in default.actions if action.kind is RescueActionKind.SHARPEN
    )
    strict_action = next(
        action for action in strict.actions if action.kind is RescueActionKind.SHARPEN
    )
    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        strict, tmp_path / "source.mp4", tmp_path / "private"
    )

    assert strict_action.id != default_action.id
    assert strict.plan_digest != default.plan_digest
    assert strict_action.id in previews.previewed_action_ids
    assert default_action.id not in previews.previewed_action_ids


def test_mixed_deblur_and_sharpen_preview_preserves_roles_and_action_ids(
    tmp_path: Path,
) -> None:
    source_hash = "8" * 64
    soft = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SOFT_DETAIL, 0.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.SOFT_DETAIL,
        start_seconds=0.0,
        end_seconds=2.0,
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
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=8.0,
            estimated_frame_count=16,
            has_audio=False,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            intervals=(soft,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters={
            "deblur_measurements": [
                {
                    "algorithm_version": "1",
                    "source_ranges": [[0.0, 0.75]],
                    "estimate": estimate.model_dump(mode="json"),
                    "config": DeblurConfig().model_dump(mode="json"),
                }
            ]
        },
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
            public_explanation="Measured soft detail supports bounded restoration.",
        ),
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    plan = _qualified_sharpen_plan(plan)

    def render_deblur(
        render_source: Path,
        render_output: Path,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        render_output.write_bytes(render_source.read_bytes() + b"deblur")

    previews = RescuePreviewBuilder(
        runner=FakeRunner(), deblur_renderer=render_deblur
    ).build(plan, source, tmp_path / "private")

    assert previews.improved is not None
    assert previews.previewed_action_ids == tuple(
        action.id for action in plan.actions if action.requires_confirmation
    )
    assert any(
        path.read_bytes().endswith(b"deblur") for path in previews.faithful.paths
    )
    assert any(
        improved.read_bytes() != faithful.read_bytes()
        for faithful, improved in zip(
            previews.faithful.paths, previews.improved.paths, strict=True
        )
    )


def test_native_tonal_preview_uses_actual_pcm_inventory_for_long_float_endpoint(
    tmp_path: Path,
) -> None:
    """Production preview must not reject a valid sub-sample float remainder."""
    config = TonalInterferenceConfig(stream_block_samples=4096)
    first_tone = InterferenceTone(
        start_seconds=0.0,
        end_seconds=1.0,
        center_frequency_hz=440.0,
        confidence=0.95,
        baseline_before_dbfs=-55.0,
        baseline_after_dbfs=-54.0,
        peak_dbfs=-18.0,
        local_peak_over_baseline_db=36.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.5,
        channel_indices=(0, 1),
        attenuation_target_db=config.attenuation_db,
        render_qualification=_tonal_qualification(1.0, channel_count=2),
    )
    second_tone = first_tone.model_copy(
        update={
            "start_seconds": 10.0,
            "end_seconds": 20.0,
            "center_frequency_hz": 880.0,
            "render_qualification": _tonal_qualification(10.0, channel_count=2),
        }
    )
    long_duration = 3.333333666666661
    metadata = VideoMetadata(
        filename="source.mp4",
        container_format="mp4",
        codec="h264",
        width=64,
        height=64,
        duration_seconds=20.0,
        average_frame_rate=24.0,
        estimated_frame_count=480,
        has_audio=True,
        file_size_bytes=1,
    )
    source_hash = "9" * 64
    intervals = tuple(
        DamageInterval(
            id=make_damage_id(
                source_hash, "audio:0", DamageKind.AUDIO_NOISE, start, end
            ),
            stream_id="audio:0",
            kind=DamageKind.AUDIO_NOISE,
            start_seconds=start,
            end_seconds=end,
        )
        for start, end in ((0.0, 1.0), (10.0, 20.0))
    )
    planner_inputs: dict[str, Any] = {
        "metadata": metadata,
        "damage_map": MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=20.0,
            intervals=intervals,
        ),
        "strategy": RescueStrategy.BALANCED,
        "config": RescueEffectiveConfig(
            max_preview_ranges=2,
            max_preview_total_seconds=1.0 + long_duration,
        ),
        "assessment_parameters": {
            "tonal_interference_measurements": [
                {
                    "algorithm_version": "1",
                    "source_ranges": [[0.0, 1.0], [10.0, 20.0]],
                    "interference_profiles": [
                        first_tone.model_dump(mode="json"),
                        second_tone.model_dump(mode="json"),
                    ],
                    "config": config.model_dump(mode="json"),
                }
            ]
        },
    }
    draft = build_rescue_plan(**planner_inputs)
    plan = build_rescue_plan(
        **planner_inputs,
        tonal_qualification=_qualified_tonal_evidence(draft),
        require_tonal_qualification=True,
    )
    assert plan.preview_ranges == ((0.0, 1.0), (10.0, 10.0 + long_duration))
    rendered_tones: list[tuple[InterferenceTone, ...]] = []

    def native_runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        is_long = any("faithful-01" in value for value in arguments)
        duration = "3.333333" if is_long else "1.000000"
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                json.dumps(
                    {
                        "format": {"duration": duration},
                        "streams": [
                            {"codec_type": "video", "codec_name": "h264"},
                            {
                                "codec_type": "audio",
                                "sample_rate": "48000",
                                "channels": 2,
                                "channel_layout": "stereo",
                            },
                        ],
                    }
                ),
            )
        if "pcm_f32le" in arguments:
            sample_count = 160_768 if is_long else 48_000
            Path(arguments[-1]).write_bytes(b"\0" * (sample_count * 2 * 4))
        elif "null" not in arguments:
            Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    def tonal_renderer(
        source: Path,
        output: Path,
        tones: tuple[InterferenceTone, ...],
        renderer_config: TonalInterferenceConfig,
        **kwargs: Any,
    ) -> None:
        rendered_tones.append(tones)
        render_tonal_interference_reduced_audio(
            source,
            output,
            tones,
            renderer_config,
            ffmpeg_path=Path(kwargs["ffmpeg_path"]),
            ffprobe_path=Path(kwargs["ffprobe_path"]),
            runner=native_runner,
            cancellation_callback=kwargs.get("cancellation_callback"),
        )

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    previews = RescuePreviewBuilder(
        runner=FakeRunner(),
        tonal_renderer=tonal_renderer,
        native_runner=native_runner,
    ).build(plan, source, tmp_path / "previews")

    assert tuple(
        tuple(tone.center_frequency_hz for tone in tones) for tones in rendered_tones
    ) == ((440.0,), (880.0,))
    assert rendered_tones[1][0].end_seconds == long_duration
    assert previews.previewed_action_ids == tuple(
        action.id for action in plan.actions if action.requires_confirmation
    )
    assert not any("partial" in path.name for path in (tmp_path / "previews").iterdir())


def test_subprocess_preview_decodes_non_utf8_stderr_stably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert arguments == ["ffmpeg", "中文 source.mp4"]
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        stderr = b"\xff\xfe local failure".decode(
            str(kwargs["encoding"]), str(kwargs["errors"])
        )
        return subprocess.CompletedProcess(arguments, 1, "", stderr)

    monkeypatch.setattr("videoscope.rescue.preview.subprocess.run", fake_run)

    with pytest.raises(RescueMediaError) as error:
        SubprocessPreviewRunner().run(["ffmpeg", "中文 source.mp4"])
    assert error.value.internal_message == "ffmpeg preview command failed"


def _plan(strategy: RescueStrategy) -> RescuePlan:
    damage = DamageInterval(
        id=make_damage_id("c" * 64, "video:0", DamageKind.DARK, 2.0, 8.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=2.0,
        end_seconds=8.0,
    )
    visual = _dark_visual_assessment((2.0, 8.0))
    return build_rescue_plan(
        metadata=VideoMetadata(
            filename="private-customer-video.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=10.0,
            average_frame_rate=30.0,
            estimated_frame_count=300,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash="c" * 64, duration_seconds=10.0, intervals=(damage,)
        ),
        strategy=strategy,
        config=RescueEffectiveConfig(),
        visual_assessment=visual,
    )


def _plan_deleting_2_to_3(
    *,
    undecodable_range: tuple[float, float] = (2.0, 3.0),
    dark_ranges: tuple[tuple[float, float], ...] = ((1.0, 4.0),),
    max_preview_total_seconds: float = 3.0,
) -> RescuePlan:
    source_hash = "a" * 64
    undecodable_start, undecodable_end = undecodable_range
    undecodable = DamageInterval(
        id=make_damage_id(
            source_hash,
            "video:0",
            DamageKind.UNDECODABLE,
            undecodable_start,
            undecodable_end,
        ),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=undecodable_start,
        end_seconds=undecodable_end,
    )
    dark = tuple(
        DamageInterval(
            id=make_damage_id(source_hash, "video:0", DamageKind.DARK, start, end),
            stream_id="video:0",
            kind=DamageKind.DARK,
            start_seconds=start,
            end_seconds=end,
        )
        for start, end in dark_ranges
    )
    return build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=6.0,
            average_frame_rate=30.0,
            estimated_frame_count=180,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=6.0,
            intervals=(undecodable, *dark),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(
            max_preview_total_seconds=max_preview_total_seconds
        ),
        visual_assessment=_dark_visual_assessment(*dark_ranges),
    )


def test_preview_ranges_are_identical_across_variants(tmp_path: Path) -> None:
    """Catches an improved preview that compares different content than faithful."""
    runner = FakeRunner()
    source = tmp_path / "private-customer-video.mp4"
    source.write_bytes(b"original")

    previews = RescuePreviewBuilder(runner=runner).build(
        plan=_plan(RescueStrategy.BALANCED),
        source=source,
        private_review_root=tmp_path / "private review",
    )

    assert previews.improved is not None
    assert previews.source.time_ranges == previews.faithful.time_ranges
    assert previews.source.time_ranges == previews.improved.time_ranges
    assert previews.previewed_action_ids == tuple(
        action.id
        for action in _plan(RescueStrategy.BALANCED).actions
        if action.requires_confirmation
    )
    improved_commands = [
        command for command in runner.commands if "improved-" in Path(command[-1]).name
    ]
    assert improved_commands
    for command in improved_commands:
        input_path = Path(command[command.index("-i") + 1])
        assert input_path.name.startswith("faithful-")
        assert input_path.parent == Path(command[-1]).parent
    assert source.read_bytes() == b"original"
    assert all(source.name not in str(path) for path in previews.all_paths())


def test_faithful_preview_records_local_source_mappings(tmp_path: Path) -> None:
    """Catches private faithful media losing its rebased source lineage."""
    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        _plan_deleting_2_to_3(),
        tmp_path / "source.mp4",
        tmp_path / "private review",
    )

    mappings = previews.faithful.source_mappings
    assert [
        (item.source_start, item.source_end, item.output_start, item.output_end)
        for item in mappings
    ] == [
        (1.0, 2.0, 0.0, 1.0),
        (3.0, 4.0, 1.0, 2.0),
    ]
    assert {item.output_relative_path for item in mappings} == {"faithful-00.mp4"}


def test_deleted_only_overlap_does_not_mark_improvement_as_previewed(
    tmp_path: Path,
) -> None:
    """Catches deleted pixels authorizing an unseen retained-content change."""
    plan = _plan_deleting_2_to_3(dark_ranges=((2.0, 3.0), (4.0, 5.0)))

    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan,
        tmp_path / "source.mp4",
        tmp_path / "private review",
    )

    adjust_luma = next(
        action for action in plan.actions if action.kind is RescueActionKind.ADJUST_LUMA
    )
    assert adjust_luma.id not in previews.previewed_action_ids


def test_wholly_removed_preview_is_review_gated_with_bounded_reason(
    tmp_path: Path,
) -> None:
    """Structural preview includes retained context instead of deleted-only media."""
    plan = _plan_deleting_2_to_3(
        undecodable_range=(1.0, 5.0),
        dark_ranges=((1.0, 5.0),),
        max_preview_total_seconds=4.0,
    )

    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan,
        tmp_path / "source.mp4",
        tmp_path / "private review",
    )

    salvage = next(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.SALVAGE_SEGMENTS
    )
    assert tuple(path.name for path in previews.all_paths()) == (
        "source-00.mp4",
        "faithful-00.mp4",
    )
    assert previews.faithful.source_mappings[0].source_start == 0.0
    assert previews.faithful.source_mappings[0].source_end == 1.0
    assert previews.previewed_action_ids == (salvage.id,)
    assert previews.review_reasons == ()


def test_preview_records_only_actions_intersecting_an_issued_preview(
    tmp_path: Path,
) -> None:
    """An unrelated bounded preview cannot authorize an unshown action."""
    source_hash = "d" * 64
    structural = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.UNDECODABLE, 1.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=1.0,
        end_seconds=2.0,
    )
    dark = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.DARK, 7.0, 8.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=7.0,
        end_seconds=8.0,
    )
    visual = _dark_visual_assessment((7.0, 8.0))
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=10.0,
            average_frame_rate=30.0,
            estimated_frame_count=300,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=10.0,
            intervals=(structural, dark),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(
            max_preview_ranges=1,
            max_preview_total_seconds=2.0,
        ),
        visual_assessment=visual,
    )

    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan,
        tmp_path / "source.mp4",
        tmp_path / "private review",
    )

    previewed = set(previews.previewed_action_ids)
    salvage = next(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.SALVAGE_SEGMENTS
    )
    assert salvage.id in previewed
    assert RescueActionKind.ADJUST_LUMA not in {action.kind for action in plan.actions}
    assert (
        "Automatic adjust_luma action needs review: preview_range_uncovered."
        in plan.assessment_warnings
    )


def test_stabilization_preview_identity_requires_a_rendered_correction(
    tmp_path: Path,
) -> None:
    """A range-only intersection cannot stand in for an actual anchor correction."""
    source_hash = "9" * 64
    shake = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SHAKE, 0.0, 10.0),
        stream_id="video:0",
        kind=DamageKind.SHAKE,
        start_seconds=0.0,
        end_seconds=10.0,
    )
    correction = MotionTransform(
        timestamp_seconds=9.5,
        translation_x=-2.0,
        translation_y=0.5,
        rotation_degrees=0.0,
        scale=1.0,
        inlier_ratio=0.95,
        residual_pixels=0.2,
        scene_boundary=False,
        semantics="frame_correction",
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=10.0,
            average_frame_rate=30.0,
            estimated_frame_count=300,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=10.0,
            scan_coverage=((0.0, 10.0),),
            intervals=(shake,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(
            max_preview_ranges=1,
            max_preview_total_seconds=1.0,
        ),
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="measured_anchor_correction",
            crop_ratio=0.04,
            parameters={
                "affected_ranges": [[0.0, 10.0]],
                "config": StabilizationConfig(
                    frame_width=1280,
                    frame_height=720,
                    accepted_ranges=((0.0, 10.0),),
                ).model_dump(mode="json"),
            },
            transforms=(correction,),
        ),
    )
    rendered: list[tuple[MotionTransform, ...]] = []
    encode_configs: list[RescueEffectiveConfig] = []

    def render_anchor(
        source: Path,
        output: Path,
        transforms: tuple[MotionTransform, ...],
        _config: StabilizationConfig,
        **_kwargs: object,
    ) -> None:
        rendered.append(transforms)
        encode_config = _kwargs.get("encode_config")
        assert isinstance(encode_config, RescueEffectiveConfig)
        encode_configs.append(encode_config)
        output.write_bytes(source.read_bytes() + b"anchor")

    previews = RescuePreviewBuilder(
        runner=FakeRunner(), stabilization_renderer=render_anchor
    ).build(plan, tmp_path / "source.mp4", tmp_path / "private")
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.STABILIZE
    )

    assert rendered
    assert encode_configs == [plan.effective_config]
    assert any(
        start <= correction.timestamp_seconds < end
        for start, end in plan.preview_ranges
    )
    assert action.id in previews.previewed_action_ids


def test_preview_omits_improved_when_no_supported_improvement_exists(
    tmp_path: Path,
) -> None:
    """Catches cloning the faithful preview into a misleading improved variant."""
    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan=_plan(RescueStrategy.CONSERVATIVE),
        source=tmp_path / "private-customer-video.mp4",
        private_review_root=tmp_path / "private review",
    )

    assert previews.improved is None


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "reserved_name",
    ("source-00.mp4", "faithful-00.mp4", "improved-00.mp4"),
)
def test_preview_rejects_every_reserved_output_collision_before_running(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    """Catches a private preview command overwriting its read-only source."""
    runner = FakeRunner()
    source = tmp_path / reserved_name
    source.write_bytes(b"original")

    with pytest.raises(RescueArtifactError):
        RescuePreviewBuilder(runner=runner).build(
            plan=_plan(RescueStrategy.BALANCED),
            source=source,
            private_review_root=tmp_path,
        )

    assert runner.commands == []
    assert source.read_bytes() == b"original"


def test_preview_rejects_hard_linked_reserved_output_before_running(
    tmp_path: Path,
) -> None:
    """Catches a distinct preview pathname referring to the read-only source."""
    runner = FakeRunner()
    source = tmp_path / "source.mp4"
    reserved_output = tmp_path / "source-00.mp4"
    source.write_bytes(b"original")
    try:
        reserved_output.hardlink_to(source)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable on this filesystem: {exc}")
    assert os.path.samefile(source, reserved_output)

    with pytest.raises(RescueArtifactError):
        RescuePreviewBuilder(runner=runner).build(
            plan=_plan(RescueStrategy.BALANCED),
            source=source,
            private_review_root=tmp_path,
        )

    assert runner.commands == []
    assert source.read_bytes() == b"original"


@pytest.mark.parametrize("stabilization_method", ("anchor_v1", "transition_anchor_v1"))
def test_preview_uses_native_perceptual_restorers_once_in_fixed_order(
    tmp_path: Path,
    stabilization_method: str,
) -> None:
    source_hash = "d" * 64
    estimate = BlurKernelEstimate(
        kernel_kind="box",
        radius=2,
        regularization=0.003,
        confidence=0.93,
        edge_width_before=4.0,
        predicted_edge_width_after=2.0,
        edge_continuity_ratio=0.9,
        reblur_error_ratio=0.01,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )
    deblur_config = DeblurConfig(candidate_radii=(2,))
    second_estimate = estimate.model_copy(update={"radius": 3})
    second_deblur_config = DeblurConfig(candidate_radii=(3,))
    tone = InterferenceTone(
        start_seconds=0.25,
        end_seconds=1.75,
        center_frequency_hz=913.0,
        confidence=0.91,
        baseline_before_dbfs=-58.0,
        baseline_after_dbfs=-57.0,
        peak_dbfs=-13.0,
        local_peak_over_baseline_db=44.0,
        persistence_window_count=24,
        frequency_standard_deviation_hz=1.25,
        channel_indices=(0,),
        attenuation_target_db=24.0,
        render_qualification=_tonal_qualification(1.5, channel_count=1),
    )
    tonal_config = TonalInterferenceConfig(render_attenuation_headroom_db=4.0)
    transform = MotionTransform(
        timestamp_seconds=1.5,
        rotation_degrees=0.0,
        scale=1.0,
        translation_x=1.0,
        translation_y=0.0,
        inlier_ratio=0.9,
        residual_pixels=0.5,
        semantics="frame_correction",
    )
    first_transform = transform.model_copy(update={"timestamp_seconds": 0.5})
    first_boundary_transform = transform.model_copy(update={"timestamp_seconds": 0.75})
    second_boundary_transform = transform.model_copy(update={"timestamp_seconds": 1.75})
    stabilization_config = StabilizationConfig(
        frame_width=64,
        frame_height=64,
        accepted_ranges=((0.0, 2.0),),
    )
    locked_ranges = ((0.8, 1.0),)
    intervals = tuple(
        DamageInterval(
            id=make_damage_id(source_hash, stream, kind, 0.0, 2.0),
            stream_id=stream,
            kind=kind,
            start_seconds=0.0,
            end_seconds=2.0,
        )
        for stream, kind in (
            ("video:0", DamageKind.SOFT_DETAIL),
            ("audio:0", DamageKind.AUDIO_NOISE),
            ("video:0", DamageKind.SHAKE),
        )
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=2.0,
            estimated_frame_count=4,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            intervals=intervals,
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(locked_ranges=locked_ranges),
        locked_ranges=locked_ranges,
        assessment_parameters={
            "deblur_measurements": [
                {
                    "algorithm_version": "1",
                    "source_ranges": [[0.0, 0.75]],
                    "estimate": estimate.model_dump(mode="json"),
                    "config": deblur_config.model_dump(mode="json"),
                },
                {
                    "algorithm_version": "1",
                    "source_ranges": [[1.25, 2.0]],
                    "estimate": second_estimate.model_dump(mode="json"),
                    "config": second_deblur_config.model_dump(mode="json"),
                },
            ],
            "tonal_interference_measurements": [
                {
                    "algorithm_version": "1",
                    "source_ranges": [[0.25, 1.75]],
                    "interference_profiles": [tone.model_dump(mode="json")],
                    "config": tonal_config.model_dump(mode="json"),
                }
            ],
        },
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="measured_affine_motion",
            crop_ratio=0.02,
            transforms=(
                first_transform,
                first_boundary_transform,
                transform,
                second_boundary_transform,
            ),
            parameters={
                "method": stabilization_method,
                "algorithm_version": "1",
                "config": stabilization_config.model_dump(mode="json"),
                "affected_ranges": [[0.0, 2.0]],
            },
        ),
    )
    calls: list[tuple[str, object]] = []

    def publish(kind: str, source: Path, output: Path, payload: object) -> None:
        calls.append((kind, payload))
        output.write_bytes(source.read_bytes() + kind.encode("ascii"))

    source = tmp_path / "源 视频 ü.mp4"
    source.write_bytes(b"source")
    preview_root = tmp_path / "预览 空间"
    previews = RescuePreviewBuilder(
        runner=FakeRunner(),
        deblur_renderer=lambda source, output, ranges, estimate, config, **_kwargs: (
            publish("deblur", source, output, (ranges, estimate, config))
        ),
        tonal_renderer=lambda source, output, tones, config, **_kwargs: publish(
            "tonal", source, output, (tones, config)
        ),
        stabilization_renderer=lambda source, output, transforms, config, **_kwargs: (
            publish("anchor", source, output, (transforms, config))
        ),
    ).build(plan, source, preview_root)

    assert [kind for kind, _payload in calls] == [
        "deblur",
        "anchor",
        "deblur",
        "anchor",
    ]
    assert calls[0][1] == (
        ((0.0, 0.25),),
        estimate,
        deblur_config,
    )
    assert calls[1][1] == (
        (first_transform.model_copy(update={"timestamp_seconds": 0.0}),),
        stabilization_config.model_copy(update={"accepted_ranges": ((0.0, 0.25),)}),
    )
    assert calls[2][1] == (
        ((0.0, 0.5),),
        second_estimate,
        second_deblur_config,
    )
    assert calls[3][1] == (
        (
            transform.model_copy(update={"timestamp_seconds": 0.0}),
            second_boundary_transform.model_copy(update={"timestamp_seconds": 0.25}),
        ),
        stabilization_config.model_copy(update={"accepted_ranges": ((0.0, 0.5),)}),
    )
    assert previews.improved is None
    assert previews.previewed_action_ids == tuple(
        action.id for action in plan.actions if action.requires_confirmation
    )
    assert previews.faithful.paths[0].read_bytes().endswith(b"debluranchor")
    assert previews.faithful.paths[1].read_bytes().endswith(b"debluranchor")

    class CallbackFailure(RuntimeError):
        pass

    for cancelled_stage in ("deblur", "anchor"):
        cancelled_root = tmp_path / f"cancelled {cancelled_stage} preview"

        def raise_from_callback() -> bool:
            raise CallbackFailure(f"stop {cancelled_stage} preview")

        def renderer_for(kind: str) -> Callable[..., None]:
            def render(
                render_source: Path,
                render_output: Path,
                *_args: object,
                **kwargs: object,
            ) -> None:
                if kind == cancelled_stage:
                    callback = kwargs["cancellation_callback"]
                    assert callable(callback)
                    callback()
                render_output.write_bytes(
                    render_source.read_bytes() + kind.encode("ascii")
                )

            return render

        with pytest.raises(CallbackFailure, match=f"stop {cancelled_stage} preview"):
            RescuePreviewBuilder(
                runner=FakeRunner(),
                deblur_renderer=renderer_for("deblur"),
                tonal_renderer=renderer_for("tonal"),
                stabilization_renderer=renderer_for("anchor"),
            ).build(
                plan,
                source,
                cancelled_root,
                cancellation_callback=raise_from_callback,
            )

        assert source.read_bytes() == b"source"
        assert not tuple(cancelled_root.glob("*.mp4"))
