"""Tests for deterministic Conservative and Balanced Rescue planning."""

from __future__ import annotations

import json
import math
import warnings
from typing import Any

import pytest
from pydantic import JsonValue

import videoscope.rescue.capabilities as rescue_capabilities
import videoscope.rescue.planner as rescue_planner
from videoscope.domain import VideoMetadata
from videoscope.rescue.audio import FixedOffsetAssessment
from videoscope.rescue.capabilities import (
    ActionCapabilityReason,
    evaluate_action_capabilities,
)
from videoscope.rescue.deblur import BlurKernelEstimate, DeblurConfig
from videoscope.rescue.errors import RescuePlanError
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    RescueSymptom,
    make_damage_id,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationAssessment,
    StabilizationConfig,
)
from videoscope.rescue.timeline import preview_source_mappings
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
)
from videoscope.rescue.visual import (
    FlickerCorrectionPlan,
    LumaAdjustmentConfig,
    SharpenConfig,
    VisualAssessment,
    VisualAssessmentConfig,
    VisualEvidence,
    VisualMetrics,
    VisualSample,
    assess_visual_samples,
)


def test_explicit_luma_config_is_bound_into_action_identity_and_rejects_drift() -> None:
    custom = LumaAdjustmentConfig(
        contrast_noise_guard_threshold=0.015,
        minimum_brightness=0.03,
        maximum_brightness=0.09,
        minimum_perceptible_luma_delta=0.05,
        maximum_luma_improvement_delta=0.09,
        maximum_noise_increase=0.0,
        maximum_chroma_shift=0.008,
        noise_guard_video_crf=23,
        noise_guard_chroma_qp_offset=-8,
    )
    metadata = video_metadata()
    measured_map = damage_map(interval(DamageKind.DARK, 2.0, 5.0))
    effective_config = RescueEffectiveConfig()
    assessment = measured_dark_assessment().model_copy(
        update={
            "metrics": measured_dark_assessment().metrics.model_copy(
                update={"noise_residual": 0.02}
            )
        }
    )
    custom_parameters: dict[str, JsonValue] = {
        "visual_config": VisualAssessmentConfig(luma=custom).model_dump(mode="json")
    }

    def build(parameters: dict[str, JsonValue] | None = None) -> RescuePlan:
        return build_rescue_plan(
            metadata=metadata,
            damage_map=measured_map,
            strategy=RescueStrategy.BALANCED,
            config=effective_config,
            visual_assessment=assessment,
            assessment_parameters=parameters,
        )

    first = build(custom_parameters)
    second = build(custom_parameters)
    default = build()
    offset_only = build(
        {
            "visual_config": VisualAssessmentConfig(
                luma=LumaAdjustmentConfig(noise_guard_chroma_qp_offset=-8)
            ).model_dump(mode="json")
        }
    )
    action = next(
        item for item in first.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    default_action = next(
        item for item in default.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    offset_only_action = next(
        item
        for item in offset_only.actions
        if item.kind is RescueActionKind.ADJUST_LUMA
    )

    assert action.parameters["luma_config"] == custom.model_dump(mode="json")
    assert action.parameters["contrast_noise_guard_threshold"] == pytest.approx(0.015)
    assert action.parameters["minimum_perceptible_luma_delta"] == pytest.approx(0.05)
    assert action.parameters["maximum_chroma_shift"] == pytest.approx(0.008)
    assert action.parameters["noise_guard_chroma_qp_offset"] == -8
    assert default_action.parameters["noise_guard_chroma_qp_offset"] == -6
    assert action.id != default_action.id
    assert first.plan_digest != default.plan_digest
    assert offset_only_action.parameters["noise_guard_chroma_qp_offset"] == -8
    assert offset_only_action.id != default_action.id
    assert offset_only.plan_digest != default.plan_digest
    assert first == second

    for mutation in ("missing", "extra"):
        raw_visual = VisualAssessmentConfig().model_dump(mode="json")
        raw_luma = raw_visual["luma"]
        assert isinstance(raw_luma, dict)
        if mutation == "missing":
            raw_luma.pop("maximum_chroma_shift")
        else:
            raw_luma["unexpected"] = 1
        with pytest.raises(RescuePlanError):
            build({"visual_config": raw_visual})


def video_metadata() -> VideoMetadata:
    return VideoMetadata(
        filename="private source.mp4",
        container_format="mp4",
        codec="h264",
        width=1280,
        height=720,
        duration_seconds=20.0,
        average_frame_rate=30.0,
        estimated_frame_count=600,
        has_audio=True,
        file_size_bytes=1024,
    )


def interval(
    kind: DamageKind, start_seconds: float, end_seconds: float
) -> DamageInterval:
    return DamageInterval(
        id=make_damage_id("a" * 64, "video:0", kind, start_seconds, end_seconds),
        stream_id="video:0",
        kind=kind,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )


def damage_map(*intervals: DamageInterval) -> MediaDamageMap:
    return MediaDamageMap(
        input_hash="a" * 64,
        duration_seconds=20.0,
        scan_coverage=((0.0, 20.0),),
        intervals=intervals,
    )


def _json_array(value: JsonValue) -> list[JsonValue]:
    assert isinstance(value, list)
    return value


def _json_object(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _passing_tonal_qualification(
    duration_seconds: float, *, channel_count: int = 1, notch_q: float = 8.0
) -> TonalRenderQualification:
    return TonalRenderQualification(
        boundary_mode="full_interval_v1",
        notch_q=notch_q,
        complete_window_count=(
            math.floor(duration_seconds / 0.05 + 1e-9) * channel_count
        ),
        minimum_target_reduction_db=25.0,
        maximum_non_target_attenuation_db=0.1,
        maximum_boundary_energy_jump_db=0.1,
        maximum_boundary_crest_jump_db=0.1,
        maximum_boundary_adjacent_delta=0.01,
    )


def _v15_preview_case(
    *,
    include_broad_unrelated_damage: bool = False,
) -> tuple[
    VideoMetadata,
    MediaDamageMap,
    dict[str, Any],
    StabilizationAssessment,
]:
    """Build one generic three-capability case without fixture identity coupling."""
    metadata = video_metadata().model_copy(
        update={
            "duration_seconds": 42.0,
            "estimated_frame_count": 1260,
        }
    )
    measured = _perceptual_measurements(radius=3, frequency_hz=880.0)
    deblur_profile = dict(measured["deblur_measurements"][0])
    deblur_profile["source_ranges"] = [[4.75, 10.25]]
    tonal_profile = dict(measured["tonal_interference_measurements"][0])
    raw_tone = dict(tonal_profile["interference_profiles"][0])
    raw_tone["channel_indices"] = tuple(raw_tone["channel_indices"])
    first_tone = InterferenceTone.model_validate(raw_tone).model_copy(
        update={
            "start_seconds": 5.025,
            "end_seconds": 9.975,
            "render_qualification": _passing_tonal_qualification(4.95),
        }
    )
    second_tone = first_tone.model_copy(
        update={
            "start_seconds": 25.0,
            "end_seconds": 32.0,
            "center_frequency_hz": 117.0,
            "render_qualification": _passing_tonal_qualification(7.0),
        }
    )
    tonal_profile["source_ranges"] = [[5.025, 9.975], [25.0, 32.0]]
    tonal_profile["interference_profiles"] = [
        first_tone.model_dump(mode="json"),
        second_tone.model_dump(mode="json"),
    ]
    intervals = [
        interval(DamageKind.SOFT_DETAIL, 4.75, 10.25),
        interval(DamageKind.AUDIO_NOISE, 5.025, 9.975),
        interval(DamageKind.AUDIO_NOISE, 25.0, 32.0),
        interval(DamageKind.SHAKE, 33.0, 36.0),
    ]
    if include_broad_unrelated_damage:
        intervals.append(interval(DamageKind.DARK, 0.0, 42.0))
    map_ = MediaDamageMap(
        input_hash="a" * 64,
        duration_seconds=42.0,
        scan_coverage=((0.0, 42.0),),
        intervals=tuple(intervals),
    )
    stabilization = StabilizationAssessment(
        recommended=True,
        reason="measured_anchor_correction",
        crop_ratio=0.04,
        parameters={
            "affected_ranges": [[33.0, 36.0]],
            "config": StabilizationConfig(
                frame_width=1280,
                frame_height=720,
                accepted_ranges=((33.0, 36.0),),
            ).model_dump(mode="json"),
        },
        transforms=(
            MotionTransform(
                timestamp_seconds=33.0,
                translation_x=-4.0,
                translation_y=1.0,
                rotation_degrees=0.0,
                scale=1.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
                scene_boundary=False,
                semantics="frame_correction",
            ),
            MotionTransform(
                timestamp_seconds=35.5,
                translation_x=-2.0,
                translation_y=0.5,
                rotation_degrees=0.0,
                scale=1.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
                scene_boundary=False,
                semantics="frame_correction",
            ),
        ),
    )
    return (
        metadata,
        map_,
        {
            "deblur_measurements": [deblur_profile],
            "tonal_interference_measurements": [tonal_profile],
        },
        stabilization,
    )


def _range_has_private_preview(
    source_range: tuple[float, float],
    preview_ranges: tuple[tuple[float, float], ...],
) -> bool:
    start, end = source_range
    return any(
        start < preview_end and preview_start < end
        for preview_start, preview_end in preview_ranges
    )


def measured_dark_assessment() -> VisualAssessment:
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
                timestamp_seconds=timestamp,
                metric="luma_p10",
                observed=0.05,
                threshold=0.18,
                context_luma_p50=0.08,
            )
            for timestamp in (1.5, 2.5, 4.5, 7.5)
        ),
        preview_required=True,
        public_explanation="Measured dark samples support a preview.",
    )


def _perceptual_measurements(
    *,
    radius: int,
    frequency_hz: float,
    tonal_headroom_db: float = 3.0,
    tonal_render_notch_q: float = 8.0,
) -> dict[str, Any]:
    deblur_config = DeblurConfig()
    tonal_config = TonalInterferenceConfig(
        render_attenuation_headroom_db=tonal_headroom_db
    )
    estimate = BlurKernelEstimate(
        kernel_kind="box",
        radius=radius,
        regularization=0.003,
        confidence=0.91,
        edge_width_before=9.0,
        predicted_edge_width_after=4.0,
        edge_continuity_ratio=0.88,
        reblur_error_ratio=0.02,
        ringing_ratio=0.01,
        noise_gain_ratio=1.2,
        temporal_change_ratio=0.03,
    )
    tone = InterferenceTone(
        start_seconds=12.0,
        end_seconds=14.0,
        center_frequency_hz=frequency_hz,
        confidence=0.94,
        baseline_before_dbfs=-52.0,
        baseline_after_dbfs=-51.0,
        peak_dbfs=-14.0,
        local_peak_over_baseline_db=37.0,
        persistence_window_count=40,
        frequency_standard_deviation_hz=1.5,
        channel_indices=(0,),
        attenuation_target_db=tonal_config.attenuation_db,
        render_qualification=TonalRenderQualification(
            **_passing_tonal_qualification(
                2.0, notch_q=tonal_render_notch_q
            ).model_dump(mode="python")
        ),
    )
    return {
        "deblur_measurements": [
            {
                "source_ranges": [[5.0, 10.0]],
                "algorithm_version": "1",
                "estimate": estimate.model_dump(mode="json"),
                "config": deblur_config.model_dump(mode="json"),
            }
        ],
        "tonal_interference_measurements": [
            {
                "source_ranges": [[12.0, 14.0]],
                "algorithm_version": "1",
                "interference_profiles": [tone.model_dump(mode="json")],
                "config": tonal_config.model_dump(mode="json"),
            }
        ],
    }


def test_perceptual_planner_copies_measured_parameters_and_changes_digest() -> None:
    """Catches re-deriving one generic repair from distinct measured inputs."""
    common: dict[str, Any] = {
        "metadata": video_metadata(),
        "damage_map": damage_map(
            interval(DamageKind.SOFT_DETAIL, 5.0, 10.0),
            interval(DamageKind.AUDIO_NOISE, 12.0, 14.0),
        ),
        "strategy": RescueStrategy.BALANCED,
        "config": RescueEffectiveConfig(),
    }

    first = build_rescue_plan(
        **common,
        assessment_parameters=_perceptual_measurements(radius=3, frequency_hz=880.0),
    )
    second = build_rescue_plan(
        **common,
        assessment_parameters=_perceptual_measurements(radius=4, frequency_hz=940.0),
    )

    assert "improved-viewing.mp4" not in first.public_artifacts
    assert "improved-viewing.mp4" not in second.public_artifacts

    first_deblur = next(
        action for action in first.actions if action.kind is RescueActionKind.DEBLUR
    )
    first_tonal = next(
        action
        for action in first.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )
    second_deblur = next(
        action for action in second.actions if action.kind is RescueActionKind.DEBLUR
    )
    second_tonal = next(
        action
        for action in second.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )
    first_tonal_profiles = _json_array(first_tonal.parameters["interference_profiles"])
    first_tonal_profile = _json_object(first_tonal_profiles[0])
    assert first_deblur.source_ranges == ((5.0, 10.0),)
    assert (
        first_deblur.parameters["estimate"]
        == _perceptual_measurements(radius=3, frequency_hz=880.0)[
            "deblur_measurements"
        ][0]["estimate"]
    )
    assert first_tonal.source_ranges == ((12.0, 14.0),)
    assert first_tonal_profile["center_frequency_hz"] == 880.0
    assert second_deblur.parameters["estimate"] != first_deblur.parameters["estimate"]
    assert (
        second_tonal.parameters["interference_profiles"]
        != first_tonal.parameters["interference_profiles"]
    )
    assert first.plan_digest != second.plan_digest


def test_canonical_video_contract_binds_only_content_action_identity() -> None:
    metadata, map_, measured, stabilization = _v15_preview_case()
    measured = {
        "tonal_interference_measurements": measured["tonal_interference_measurements"]
    }
    visual = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.1,
            luma_p50=0.4,
            luma_p90=0.9,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.005,
            sharpness=0.04,
        ),
        recommended_actions=(RescueActionKind.SHARPEN,),
        preview_required=True,
        public_explanation="Measured soft detail supports bounded sharpening.",
    )

    def plan_for(crf: int) -> RescuePlan:
        return build_rescue_plan(
            metadata=metadata,
            damage_map=map_,
            strategy=RescueStrategy.BALANCED,
            config=RescueEffectiveConfig(improved_video_crf=crf),
            assessment_parameters=measured,
            visual_assessment=visual,
            stabilization_assessment=stabilization,
        )

    first = plan_for(16)
    identical = plan_for(16)
    changed = plan_for(18)
    first_ids = {action.kind: action.id for action in first.actions}
    changed_ids = {action.kind: action.id for action in changed.actions}
    assert first == identical
    with pytest.raises(ValueError, match="qualification is missing"):
        RescuePlan.model_validate_json(first.model_dump_json())
    assert first.plan_digest != changed.plan_digest
    for kind in (
        RescueActionKind.SHARPEN,
        RescueActionKind.STABILIZE,
        RescueActionKind.DENOISE_AUDIO,
    ):
        action = next(item for item in first.actions if item.kind is kind)
        contract = _json_object(action.parameters["video_encode_contract"])
        assert contract["contract_version"] == "1"
        assert contract["crf"] == 16
        assert first_ids[kind] != changed_ids[kind]
    assert first_ids[RescueActionKind.REMUX] == changed_ids[RescueActionKind.REMUX]
    assert first_ids[RescueActionKind.VERIFY] == changed_ids[RescueActionKind.VERIFY]


@pytest.mark.parametrize("mutation", ("missing", "mismatched"))
def test_plan_rejects_missing_or_mismatched_action_encode_contract(
    mutation: str,
) -> None:
    visual = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.1,
            luma_p50=0.4,
            luma_p90=0.9,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.005,
            sharpness=0.04,
        ),
        recommended_actions=(RescueActionKind.SHARPEN,),
        preview_required=True,
        public_explanation="Measured soft detail supports bounded sharpening.",
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.SOFT_DETAIL, 5.0, 10.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=visual,
    )
    payload = plan.model_dump(mode="json")
    action = next(item for item in payload["actions"] if item["kind"] == "sharpen")
    if mutation == "missing":
        action["parameters"].pop("video_encode_contract")
    else:
        action["parameters"]["video_encode_contract"]["crf"] = 18
    payload["plan_digest"] = make_rescue_plan_digest(payload)

    with pytest.raises(ValueError, match="video encode contract"):
        RescuePlan.model_validate_json(json.dumps(payload))


def test_tonal_render_headroom_is_bound_into_action_and_plan_digest() -> None:
    common: dict[str, Any] = {
        "metadata": video_metadata(),
        "damage_map": damage_map(interval(DamageKind.AUDIO_NOISE, 12.0, 14.0)),
        "strategy": RescueStrategy.BALANCED,
        "config": RescueEffectiveConfig(),
    }
    first = build_rescue_plan(
        **common,
        assessment_parameters=_perceptual_measurements(
            radius=3, frequency_hz=880.0, tonal_headroom_db=3.0
        ),
    )
    second = build_rescue_plan(
        **common,
        assessment_parameters=_perceptual_measurements(
            radius=3, frequency_hz=880.0, tonal_headroom_db=4.0
        ),
    )
    first_action = next(
        action
        for action in first.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )
    second_action = next(
        action
        for action in second.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )
    first_config = _json_object(first_action.parameters["config"])
    second_config = _json_object(second_action.parameters["config"])

    assert first_config["render_attenuation_headroom_db"] == 3.0
    assert second_config["render_attenuation_headroom_db"] == 4.0
    assert first.plan_digest != second.plan_digest


def test_tonal_render_qualification_is_bound_into_action_and_plan_digest() -> None:
    common: dict[str, Any] = {
        "metadata": video_metadata(),
        "damage_map": damage_map(interval(DamageKind.AUDIO_NOISE, 12.0, 14.0)),
        "strategy": RescueStrategy.BALANCED,
        "config": RescueEffectiveConfig(),
    }
    first = build_rescue_plan(
        **common,
        assessment_parameters=_perceptual_measurements(
            radius=3,
            frequency_hz=880.0,
            tonal_render_notch_q=8.0,
        ),
    )
    second = build_rescue_plan(
        **common,
        assessment_parameters=_perceptual_measurements(
            radius=3,
            frequency_hz=880.0,
            tonal_render_notch_q=6.0,
        ),
    )
    first_action = next(
        action
        for action in first.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )
    second_action = next(
        action
        for action in second.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )

    assert first_action.id != second_action.id
    assert first.plan_digest != second.plan_digest


def test_plan_rejects_recomputed_unqualified_tonal_profile() -> None:
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.AUDIO_NOISE, 12.0, 14.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters=_perceptual_measurements(
            radius=3,
            frequency_hz=880.0,
        ),
    )
    payload = plan.model_dump(mode="json")
    action = next(
        item for item in payload["actions"] if item["kind"] == "denoise_audio"
    )
    profile = action["parameters"]["interference_profiles"][0]
    profile["render_qualification"]["notch_q"] = 7.0
    action["id"] = make_rescue_action_id(
        kind=RescueActionKind.DENOISE_AUDIO,
        parameters=action["parameters"],
        source_ranges=tuple(tuple(item) for item in action["source_ranges"]),
        strategy=RescueStrategy(action["strategy"]),
        version=action["version"],
    )
    payload["plan_digest"] = make_rescue_plan_digest(payload)

    with pytest.raises(ValueError, match="tonal action qualification"):
        RescuePlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    ("field", "value"),
    (("attenuation_target_db", 1.0), ("center_frequency_hz", 79.0)),
)
def test_plan_rejects_recomputed_tonal_profile_semantic_tamper(
    field: str, value: float
) -> None:
    """Qualification evidence cannot bypass its bound detector configuration."""
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.AUDIO_NOISE, 12.0, 14.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters=_perceptual_measurements(
            radius=3,
            frequency_hz=880.0,
        ),
    )
    payload = plan.model_dump(mode="json")
    action = next(
        item for item in payload["actions"] if item["kind"] == "denoise_audio"
    )
    profile = action["parameters"]["interference_profiles"][0]
    profile[field] = value
    if field == "attenuation_target_db":
        profile["render_qualification"]["minimum_target_reduction_db"] = value
    action["id"] = make_rescue_action_id(
        kind=RescueActionKind.DENOISE_AUDIO,
        parameters=action["parameters"],
        source_ranges=tuple(tuple(item) for item in action["source_ranges"]),
        strategy=RescueStrategy(action["strategy"]),
        version=action["version"],
    )
    payload["plan_digest"] = make_rescue_plan_digest(payload)

    with pytest.raises(ValueError, match="tonal action qualification"):
        RescuePlan.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("profile_layout", ("adjacent", "separate_channels"))
def test_multiple_raw_qualified_tonal_profiles_remain_an_internal_draft(
    profile_layout: str,
) -> None:
    """Raw PCM profiles retain exact ranges but cannot become a final plan."""
    measured = _perceptual_measurements(radius=3, frequency_hz=880.0)
    tonal_measurement = measured["tonal_interference_measurements"][0]
    first = InterferenceTone.model_validate_json(
        json.dumps(tonal_measurement["interference_profiles"][0])
    )
    expected_ranges: tuple[tuple[float, float], ...]
    if profile_layout == "adjacent":
        second = first.model_copy(
            update={
                "start_seconds": 14.0,
                "end_seconds": 16.0,
                "center_frequency_hz": 1200.0,
            }
        )
        source_ranges = [[12.0, 16.0]]
        damage = interval(DamageKind.AUDIO_NOISE, 12.0, 16.0)
        expected_ranges = ((12.0, 14.0), (14.0, 16.0))
    else:
        second = first.model_copy(
            update={
                "center_frequency_hz": 1200.0,
                "channel_indices": (1,),
            }
        )
        source_ranges = [[12.0, 14.0]]
        damage = interval(DamageKind.AUDIO_NOISE, 12.0, 14.0)
        expected_ranges = ((12.0, 14.0),)
    tonal_measurement["source_ranges"] = source_ranges
    tonal_measurement["interference_profiles"] = [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]

    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(damage),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters={"tonal_interference_measurements": [tonal_measurement]},
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.DENOISE_AUDIO
    )
    assert action.source_ranges == expected_ranges
    assert len(_json_array(action.parameters["interference_profiles"])) == 2
    with pytest.raises(ValueError, match="encoded candidate qualification is missing"):
        RescuePlan.model_validate_json(plan.model_dump_json())


def test_overlapping_same_channel_tonal_profiles_are_omitted_fail_closed() -> None:
    measured = _perceptual_measurements(radius=3, frequency_hz=880.0)
    tonal_measurement = measured["tonal_interference_measurements"][0]
    first = InterferenceTone.model_validate_json(
        json.dumps(tonal_measurement["interference_profiles"][0])
    )
    second = first.model_copy(
        update={
            "start_seconds": 13.0,
            "end_seconds": 15.0,
            "center_frequency_hz": 1200.0,
        }
    )
    tonal_measurement["source_ranges"] = [[12.0, 15.0]]
    tonal_measurement["interference_profiles"] = [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]

    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.AUDIO_NOISE, 12.0, 15.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters={"tonal_interference_measurements": [tonal_measurement]},
    )

    assert RescueActionKind.DENOISE_AUDIO not in {item.kind for item in plan.actions}


def test_unaccepted_observable_deblur_is_omitted_with_honest_limitation() -> None:
    limitation = (
        "Deblur was omitted because the measured soft-detail interval did not pass "
        "all conservative acceptance gates."
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.SOFT_DETAIL, 5.0, 10.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        assessment_parameters={"deblur_measurements": []},
        assessment_limitations=(limitation,),
    )

    assert RescueActionKind.DEBLUR not in {action.kind for action in plan.actions}
    assert plan.assessment_limitations == (limitation,)


def test_tonal_profile_crossing_locked_range_is_omitted_without_requalification() -> (
    None
):
    """A newly clipped boundary cannot reuse full-range qualification evidence."""
    locked_ranges = ((13.0, 13.5),)
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.AUDIO_NOISE, 12.0, 14.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(locked_ranges=locked_ranges),
        locked_ranges=locked_ranges,
        assessment_parameters=_perceptual_measurements(radius=3, frequency_hz=880.0),
    )

    assert RescueActionKind.DENOISE_AUDIO not in {item.kind for item in plan.actions}


def test_multiple_nonoverlapping_deblur_profiles_are_safely_merged() -> None:
    """Catches valid measured intervals being silently dropped in planning."""
    first = _perceptual_measurements(radius=2, frequency_hz=880.0)[
        "deblur_measurements"
    ][0]
    second = _perceptual_measurements(radius=4, frequency_hz=880.0)[
        "deblur_measurements"
    ][0]
    second = {**second, "source_ranges": [[15.0, 18.0]]}
    common: dict[str, Any] = {
        "metadata": video_metadata(),
        "damage_map": damage_map(
            interval(DamageKind.SOFT_DETAIL, 5.0, 10.0),
            interval(DamageKind.SOFT_DETAIL, 15.0, 18.0),
        ),
        "strategy": RescueStrategy.BALANCED,
        "config": RescueEffectiveConfig(),
    }

    plan = build_rescue_plan(
        **common,
        assessment_parameters={"deblur_measurements": [first, second]},
    )

    action = next(item for item in plan.actions if item.kind is RescueActionKind.DEBLUR)
    assert action.source_ranges == ((5.0, 10.0), (15.0, 18.0))
    operations = tuple(
        _json_object(operation)
        for operation in _json_array(action.parameters["operations"])
    )
    assert [operation["source_ranges"] for operation in operations] == [
        [[5.0, 10.0]],
        [[15.0, 18.0]],
    ]
    assert [
        _json_object(operation["estimate"])["radius"] for operation in operations
    ] == [2, 4]

    overlapping = build_rescue_plan(
        **common,
        assessment_parameters={
            "deblur_measurements": [
                first,
                {**second, "source_ranges": [[9.0, 18.0]]},
            ]
        },
    )
    assert RescueActionKind.DEBLUR not in {item.kind for item in overlapping.actions}


def test_deblur_capability_is_previewable_but_remains_review_gated() -> None:
    """Catches either hiding native preview or claiming Task 7 verification early."""
    action = RescueAction(
        id="deblur-action",
        version="1",
        kind=RescueActionKind.DEBLUR,
        description="Measured deblur.",
        source_ranges=((5.0, 10.0),),
        parameters={},
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )

    decision = evaluate_action_capabilities(
        (action,),
        ((5.0, 10.0),),
        duration_seconds=20.0,
        locked_ranges=(),
    )[0]

    assert decision.preview_supported is True
    assert decision.preview_covered is True
    assert decision.range_exact is True
    assert decision.verification_mode == "needs_review"
    assert decision.automatic is False


def test_anchor_plan_copies_direct_corrections_without_strength_rederivation() -> None:
    """Catches scaling measured direct anchor corrections in the planner."""
    transforms = (
        MotionTransform(
            timestamp_seconds=2.0,
            translation_x=-4.0,
            translation_y=1.0,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            scene_boundary=False,
            semantics="frame_correction",
        ),
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.SHAKE, 2.0, 3.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(balanced_strength_limit=0.5),
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="measured_anchor_correction",
            crop_ratio=0.04,
            parameters={
                "affected_ranges": [[2.0, 3.0]],
                "frame_width": 1280,
                "frame_height": 720,
                "maximum_timeline_gap_seconds": 0.05,
                "smoothing_window_samples": 1,
                "crop_ratio": 0.04,
                "max_crop_ratio": 0.12,
            },
            transforms=transforms,
        ),
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.STABILIZE
    )
    assert action.source_ranges == ((2.0, 3.0),)
    assert action.parameters["method"] == "anchor_v1"
    assert action.parameters["algorithm_version"] == "1"
    assert action.parameters["motion_transforms"] == [
        transform.model_dump(mode="json") for transform in transforms
    ]


def test_transition_anchor_plan_binds_method_and_all_exact_corrections_to_digest() -> (
    None
):
    """Catches method substitution, sparse correction plans, or an unbound curve."""
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=2.0 + index / 24.0,
            translation_x=-2.0 if index % 2 else 2.0,
            translation_y=1.0,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            semantics="frame_correction",
        )
        for index in range(96)
    )
    measured_config = StabilizationConfig(
        frame_width=1280,
        frame_height=720,
        accepted_ranges=((2.0, 6.0),),
    )

    def planned(values: tuple[MotionTransform, ...]) -> RescuePlan:
        return build_rescue_plan(
            metadata=video_metadata(),
            damage_map=damage_map(interval(DamageKind.SHAKE, 2.0, 6.0)),
            strategy=RescueStrategy.BALANCED,
            config=RescueEffectiveConfig(),
            stabilization_assessment=StabilizationAssessment(
                recommended=True,
                reason="measured_transition_anchor_motion",
                crop_ratio=0.02,
                parameters={
                    "affected_ranges": [[2.0, 6.0]],
                    "method": "transition_anchor_v1",
                    "algorithm_version": "1",
                    "estimator_algorithm_version": "transition_anchor_v1",
                    "transition_range": [2.0, 3.0],
                    "following_anchor_range": [3.0, 6.0],
                    "transition_correction_count": 96,
                    "config": measured_config.model_dump(mode="json"),
                },
                transforms=values,
            ),
        )

    first = planned(transforms)
    changed = list(transforms)
    changed[0] = changed[0].model_copy(update={"translation_x": 1.75})
    second = planned(tuple(changed))
    action = next(
        item for item in first.actions if item.kind is RescueActionKind.STABILIZE
    )

    assert action.parameters["method"] == "transition_anchor_v1"
    assert action.parameters["algorithm_version"] == "1"
    assert action.parameters["estimator_algorithm_version"] == ("transition_anchor_v1")
    assert action.parameters["motion_transforms"] == [
        transform.model_dump(mode="json") for transform in transforms
    ]
    assert len(action.parameters["motion_transforms"]) == 96
    assert first.plan_digest != second.plan_digest


def test_anchor_config_is_rebound_to_exact_unlocked_action_ranges() -> None:
    """Catches an assessment-time accepted range surviving a planning lock."""
    locked_ranges = ((2.4, 2.6),)
    measured_config = StabilizationConfig(
        frame_width=1280,
        frame_height=720,
        accepted_ranges=((2.0, 3.0),),
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.SHAKE, 2.0, 3.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(locked_ranges=locked_ranges),
        locked_ranges=locked_ranges,
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="measured_anchor_correction",
            crop_ratio=0.04,
            parameters={
                "affected_ranges": [[2.0, 3.0]],
                "config": measured_config.model_dump(mode="json"),
            },
            transforms=(
                MotionTransform(
                    timestamp_seconds=2.2,
                    translation_x=-4.0,
                    translation_y=1.0,
                    rotation_degrees=0.0,
                    scale=1.0,
                    inlier_ratio=0.95,
                    residual_pixels=0.2,
                    scene_boundary=False,
                    semantics="frame_correction",
                ),
                MotionTransform(
                    timestamp_seconds=2.8,
                    translation_x=-3.0,
                    translation_y=1.0,
                    rotation_degrees=0.0,
                    scale=1.0,
                    inlier_ratio=0.95,
                    residual_pixels=0.2,
                    scene_boundary=False,
                    semantics="frame_correction",
                ),
            ),
        ),
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.STABILIZE
    )
    assert action.source_ranges == ((2.0, 2.4), (2.6, 3.0))
    action_config = _json_object(action.parameters["config"])
    assert action_config["accepted_ranges"] == [
        [start, end] for start, end in action.source_ranges
    ]


def test_conservative_plan_never_contains_subjective_enhancement() -> None:
    """Catches an enhancement action accidentally leaking into Conservative mode."""
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.DARK, 2.0, 5.0),
            interval(DamageKind.VIDEO_NOISE, 6.0, 8.0),
        ),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(),
    )

    assert {action.kind for action in plan.actions}.isdisjoint(
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


def test_locked_damaged_edge_is_not_trimmed_and_content_changes_need_confirmation() -> (
    None
):
    """Catches any destructive removal being planned through a locked range."""
    locked = interval(DamageKind.UNDECODABLE, 0.0, 2.0)
    removable = interval(DamageKind.UNDECODABLE, 10.0, 12.0)
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(locked, removable),
        strategy=RescueStrategy.BALANCED,
        locked_ranges=((0.0, 2.0),),
        config=RescueEffectiveConfig(locked_ranges=((0.0, 2.0),)),
    )

    assert RescueActionKind.TRIM_DAMAGED_EDGES not in {
        action.kind for action in plan.actions
    }
    salvage = next(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.SALVAGE_SEGMENTS
    )
    assert salvage.parameters["damage_ids"] == [removable.id]
    assert locked.id not in salvage.parameters["damage_ids"]
    assert all(
        action.requires_confirmation
        for action in plan.actions
        if action.changes_content
    )


def test_balanced_actions_exclude_locked_spans_and_bind_strength_limit() -> None:
    """Catches global enhancement filters and ignored strength configuration."""
    config = RescueEffectiveConfig(
        balanced_strength_limit=0.5,
        locked_ranges=((3.0, 4.0),),
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.DARK, 2.0, 5.0)),
        strategy=RescueStrategy.BALANCED,
        locked_ranges=config.locked_ranges,
        config=config,
        visual_assessment=measured_dark_assessment(),
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    assert action.source_ranges == ((2.0, 3.0), (4.0, 5.0))
    assert action.parameters["strength_limit"] == 0.5
    brightness = action.parameters["brightness"]
    gamma = action.parameters["gamma"]
    assert isinstance(brightness, (int, float)) and not isinstance(brightness, bool)
    assert isinstance(gamma, (int, float)) and not isinstance(gamma, bool)
    assert 0 < float(brightness) <= 0.09
    assert 1 < float(gamma) <= 1.6
    assert action.parameters["derivation_version"] == "5"
    evidence = _json_array(action.parameters["assessment_evidence"])
    assert [_json_object(item)["timestamp_seconds"] for item in evidence] == [2.5, 4.5]


def test_adjust_luma_planner_rejects_missing_range_bound_evidence() -> None:
    with pytest.raises(RescuePlanError) as exc_info:
        build_rescue_plan(
            metadata=video_metadata(),
            damage_map=damage_map(interval(DamageKind.DARK, 2.0, 5.0)),
            strategy=RescueStrategy.BALANCED,
            config=RescueEffectiveConfig(),
            visual_assessment=measured_dark_assessment().model_copy(
                update={"evidence": ()}
            ),
        )
    assert (
        exc_info.value.internal_message == "ADJUST_LUMA assessment evidence is invalid"
    )


def test_default_assessor_reserves_luma_evidence_for_each_planned_range() -> None:
    config = VisualAssessmentConfig()
    samples = tuple(
        VisualSample(timestamp_seconds=timestamp, luma=((level,),))
        for timestamp, level in (
            (1.1, 0.05),
            (1.4, 0.06),
            (1.7, 0.07),
            (4.5, 0.10),
        )
    )
    assessment = assess_visual_samples(samples, (), config)

    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.DARK, 1.0, 2.0),
            interval(DamageKind.DARK, 4.0, 5.0),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=assessment,
        assessment_parameters={"visual_config": config.model_dump(mode="json")},
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    evidence = tuple(
        VisualEvidence.model_validate_json(json.dumps(item))
        for item in _json_array(action.parameters["assessment_evidence"])
    )
    assert action.source_ranges == ((1.0, 2.0), (4.0, 5.0))
    assert tuple(item.timestamp_seconds for item in evidence) == (1.1, 1.4, 4.5)
    assert all(
        sum(
            start <= item.timestamp_seconds < end for start, end in action.source_ranges
        )
        == 1
        for item in evidence
    )
    assert all(
        any(start <= item.timestamp_seconds < end for item in evidence)
        for start, end in action.source_ranges
    )


def test_duplicate_pts_luma_ties_are_byte_deterministic_through_plan_identity() -> None:
    config = VisualAssessmentConfig(max_evidence_samples=2)

    def tied_sample(context: float) -> VisualSample:
        return VisualSample(
            timestamp_seconds=1.0,
            luma=((0.05, 0.05, *(context for _ in range(8))),),
        )

    first_tied = tied_sample(0.10)
    second_tied = tied_sample(0.15)
    third_tied = tied_sample(0.12)
    samples = (
        first_tied,
        second_tied,
        third_tied,
        second_tied.model_copy(),
        VisualSample(timestamp_seconds=1.5, luma=((0.5,) * 10,)),
        VisualSample(
            timestamp_seconds=4.0,
            luma=((0.08, 0.08, *(0.12 for _ in range(8))),),
        ),
        VisualSample(timestamp_seconds=4.5, luma=((0.5,) * 10,)),
    )

    first_assessment = assess_visual_samples(samples, (), config)
    reversed_assessment = assess_visual_samples(tuple(reversed(samples)), (), config)

    def make_plan(assessment: VisualAssessment) -> RescuePlan:
        return build_rescue_plan(
            metadata=video_metadata(),
            damage_map=damage_map(
                interval(DamageKind.DARK, 0.8, 1.2),
                interval(DamageKind.DARK, 3.8, 4.2),
            ),
            strategy=RescueStrategy.BALANCED,
            config=RescueEffectiveConfig(),
            visual_assessment=assessment,
            assessment_parameters={"visual_config": config.model_dump(mode="json")},
        )

    first_plan = make_plan(first_assessment)
    reversed_plan = make_plan(reversed_assessment)
    first_action = next(
        item for item in first_plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    reversed_action = next(
        item
        for item in reversed_plan.actions
        if item.kind is RescueActionKind.ADJUST_LUMA
    )

    assert first_assessment.model_dump_json() == reversed_assessment.model_dump_json()
    assert first_assessment.evidence[0].context_luma_p50 == pytest.approx(0.10)
    assert first_assessment.limitations == reversed_assessment.limitations
    assert (
        json.dumps(
            first_action.parameters, sort_keys=True, separators=(",", ":")
        ).encode()
        == json.dumps(
            reversed_action.parameters, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    assert first_action.id == reversed_action.id
    assert first_plan.plan_digest == reversed_plan.plan_digest
    assert first_plan.model_dump_json() == reversed_plan.model_dump_json()


def test_signed_zero_luma_ties_are_byte_deterministic_through_plan_identity() -> None:
    config = VisualAssessmentConfig(max_evidence_samples=2)
    negative_zero = VisualSample(
        timestamp_seconds=-0.0,
        luma=((-0.0,) * 10,),
    )
    positive_zero = VisualSample(
        timestamp_seconds=0.0,
        luma=((0.0,) * 10,),
    )
    samples = (
        negative_zero,
        positive_zero,
        VisualSample(timestamp_seconds=0.5, luma=((0.5,) * 10,)),
        VisualSample(timestamp_seconds=4.0, luma=((0.08,) * 10,)),
    )

    first_assessment = assess_visual_samples(samples, (), config)
    reversed_assessment = assess_visual_samples(tuple(reversed(samples)), (), config)

    def make_plan(assessment: VisualAssessment) -> RescuePlan:
        return build_rescue_plan(
            metadata=video_metadata(),
            damage_map=damage_map(
                interval(DamageKind.DARK, 0.0, 1.0),
                interval(DamageKind.DARK, 3.0, 5.0),
            ),
            strategy=RescueStrategy.BALANCED,
            config=RescueEffectiveConfig(),
            visual_assessment=assessment,
            assessment_parameters={"visual_config": config.model_dump(mode="json")},
        )

    first_plan = make_plan(first_assessment)
    reversed_plan = make_plan(reversed_assessment)
    first_action = next(
        item for item in first_plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    reversed_action = next(
        item
        for item in reversed_plan.actions
        if item.kind is RescueActionKind.ADJUST_LUMA
    )

    assert first_assessment.model_dump_json() == reversed_assessment.model_dump_json()
    assert math.copysign(1.0, first_assessment.evidence[0].observed) == 1.0
    assert math.copysign(1.0, first_assessment.evidence[0].timestamp_seconds) == 1.0
    assert first_assessment.limitations == reversed_assessment.limitations
    assert (
        json.dumps(
            first_action.parameters, sort_keys=True, separators=(",", ":")
        ).encode()
        == json.dumps(
            reversed_action.parameters, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    assert first_action.id == reversed_action.id
    assert first_plan.plan_digest == reversed_plan.plan_digest
    assert first_plan.model_dump_json() == reversed_plan.model_dump_json()


def test_luma_ranges_are_disjoint_covered_and_half_open_after_exclusions() -> None:
    evidence = tuple(
        VisualEvidence(
            action=RescueActionKind.ADJUST_LUMA,
            timestamp_seconds=timestamp,
            metric="luma_p10",
            observed=0.05,
            threshold=0.18,
            context_luma_p50=0.08,
        )
        for timestamp in (1.5, 2.0, 4.0)
    )
    assessment = measured_dark_assessment().model_copy(update={"evidence": evidence})

    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.DARK, 1.0, 3.0),
            interval(DamageKind.DARK, 2.0, 4.0),
            interval(DamageKind.DARK, 4.0, 5.0),
            interval(DamageKind.DARK, 5.0, 6.0),
            interval(DamageKind.UNDECODABLE, 2.5, 3.5),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        locked_ranges=((1.8, 2.0),),
        visual_assessment=assessment,
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    assert action.source_ranges == ((1.0, 1.8), (2.0, 4.0), (4.0, 5.0))
    assert all(
        sum(
            start <= item.timestamp_seconds < end for start, end in action.source_ranges
        )
        == 1
        for item in evidence
    )
    limitations = _json_array(action.parameters["assessment_limitations"])
    assert any(
        isinstance(item, str) and "without persisted" in item for item in limitations
    )


def test_darker_measurements_produce_stronger_plan_bound_luma_actions() -> None:
    """Catches assessment metrics being recorded but ignored by the planner."""
    dark = measured_dark_assessment()
    less_dark = dark.model_copy(
        update={
            "metrics": dark.metrics.model_copy(
                update={"luma_p10": 0.13, "luma_p50": 0.17, "luma_p90": 0.45}
            ),
            "evidence": tuple(
                item.model_copy(update={"observed": 0.13, "context_luma_p50": 0.17})
                for item in dark.evidence
            ),
        }
    )
    common: dict[str, Any] = dict(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.DARK, 2.0, 5.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
    )

    strong = build_rescue_plan(**common, visual_assessment=dark)
    mild = build_rescue_plan(**common, visual_assessment=less_dark)
    strong_action = next(
        item for item in strong.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    mild_action = next(
        item for item in mild.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )

    strong_brightness = strong_action.parameters["brightness"]
    mild_brightness = mild_action.parameters["brightness"]
    strong_gamma = strong_action.parameters["gamma"]
    mild_gamma = mild_action.parameters["gamma"]
    assert isinstance(strong_brightness, (int, float))
    assert isinstance(mild_brightness, (int, float))
    assert isinstance(strong_gamma, (int, float))
    assert isinstance(mild_gamma, (int, float))
    assert float(strong_brightness) > float(mild_brightness)
    assert float(strong_gamma) > float(mild_gamma)
    assert strong.plan_digest != mild.plan_digest


def test_soft_detail_action_uses_its_local_evidence_not_global_sharpness() -> None:
    assessment = VisualAssessment(
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
        evidence=(
            VisualEvidence(
                action=RescueActionKind.SHARPEN,
                timestamp_seconds=3.0,
                metric="scene_relative_sharpness",
                observed=0.0007,
                threshold=0.003,
            ),
        ),
        preview_required=True,
        public_explanation="Local soft detail supports a private preview.",
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.SOFT_DETAIL, 2.0, 5.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=assessment,
    )
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.SHARPEN
    )

    assert action.parameters["observed_sharpness"] == pytest.approx(0.0007)
    assert action.parameters["amount"] == pytest.approx(
        0.8 + 0.7 * math.sqrt(2.3 / 3.0)
    )
    assert "improved-viewing.mp4" in plan.public_artifacts
    without_improved = plan.model_dump(mode="json")
    without_improved["public_artifacts"] = [
        item for item in plan.public_artifacts if item != "improved-viewing.mp4"
    ]
    assert make_rescue_plan_digest(without_improved) != plan.plan_digest


def test_sharpen_action_uses_digest_bound_assessment_config() -> None:
    assessment = VisualAssessment(
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
        evidence=(
            VisualEvidence(
                action=RescueActionKind.SHARPEN,
                timestamp_seconds=3.0,
                metric="scene_relative_sharpness",
                observed=0.0007,
                threshold=0.003,
                scene_baseline_sharpness=0.04,
            ),
            VisualEvidence(
                action=RescueActionKind.SHARPEN,
                timestamp_seconds=9.0,
                metric="scene_relative_sharpness",
                observed=0.0008,
                threshold=0.003,
                scene_baseline_sharpness=0.04,
            ),
        ),
        preview_required=True,
        public_explanation="Measured local softness requires a private preview.",
    )
    custom = SharpenConfig(
        minimum_recovered_baseline_ratio=0.91,
        minimum_improved_frame_fraction=0.95,
        maximum_noise_increase=0.007,
        edge_neighborhood_radius=5,
        maximum_edge_overshoot_ratio=0.02,
        maximum_ringing_ratio=0.03,
    )
    common: dict[str, Any] = {
        "metadata": video_metadata(),
        "damage_map": damage_map(
            interval(DamageKind.SOFT_DETAIL, 2.0, 5.0),
            interval(DamageKind.SOFT_DETAIL, 8.0, 10.0),
        ),
        "strategy": RescueStrategy.BALANCED,
        "config": RescueEffectiveConfig(),
        "visual_assessment": assessment,
    }
    custom_parameters: dict[str, JsonValue] = {
        "visual_config": VisualAssessmentConfig(sharpen=custom).model_dump(mode="json")
    }

    first = build_rescue_plan(**common, assessment_parameters=custom_parameters)
    second = build_rescue_plan(**common, assessment_parameters=custom_parameters)
    default = build_rescue_plan(**common)
    action = next(
        item for item in first.actions if item.kind is RescueActionKind.SHARPEN
    )
    default_action = next(
        item for item in default.actions if item.kind is RescueActionKind.SHARPEN
    )

    assert action.source_ranges == ((2.0, 5.0), (8.0, 10.0))
    assert all(
        any(
            source_start < preview_end and preview_start < source_end
            for preview_start, preview_end in first.preview_ranges
        )
        for source_start, source_end in action.source_ranges
    )
    assert action.parameters["minimum_recovered_baseline_ratio"] == 0.91
    assert action.parameters["minimum_improved_frame_fraction"] == 0.95
    assert action.parameters["maximum_noise_increase"] == 0.007
    assert action.parameters["edge_neighborhood_radius"] == 5
    assert action.parameters["maximum_edge_overshoot_ratio"] == 0.02
    assert action.parameters["maximum_ringing_ratio"] == 0.03
    assert action.parameters["derivation_version"] == "2"
    assert action.version == "1"
    assert action.id == next(
        item.id for item in second.actions if item.kind is RescueActionKind.SHARPEN
    )
    assert first.plan_digest == second.plan_digest
    assert action.id != default_action.id
    assert first.plan_digest != default.plan_digest
    assert default_action.parameters["minimum_recovered_baseline_ratio"] == 0.8
    assert default_action.parameters["minimum_improved_frame_fraction"] == 0.8
    assert default_action.parameters["maximum_noise_increase"] == 0.02
    assert default_action.parameters["maximum_edge_overshoot_ratio"] == 0.05
    assert default_action.parameters["maximum_ringing_ratio"] == 0.08


@pytest.mark.parametrize("invalid_shape", ["missing", "extra"])
def test_supplied_sharpen_config_fails_closed_if_not_exact(
    invalid_shape: str,
) -> None:
    assessment = VisualAssessment(
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
        public_explanation="Measured local softness supports a private preview.",
    )
    raw_visual = VisualAssessmentConfig().model_dump(mode="json")
    raw_sharpen = _json_object(raw_visual["sharpen"])
    if invalid_shape == "missing":
        del raw_sharpen["maximum_ringing_ratio"]
    else:
        raw_sharpen["unknown_threshold"] = 0.01

    with pytest.raises(RescuePlanError):
        build_rescue_plan(
            metadata=video_metadata(),
            damage_map=damage_map(interval(DamageKind.SOFT_DETAIL, 2.0, 5.0)),
            strategy=RescueStrategy.BALANCED,
            config=RescueEffectiveConfig(),
            assessment_parameters={"visual_config": raw_visual},
            visual_assessment=assessment,
        )


def test_preview_ranges_cover_actions_and_ignore_sub_microsecond_noise() -> None:
    """A rounded-zero scan interval cannot displace the action being reviewed."""
    tiny = interval(DamageKind.TIMESTAMP_DISCONTINUITY, 4.2 - 1e-15, 4.2)
    dark = interval(DamageKind.DARK, 7.0, 8.0)
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(tiny, dark),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(max_preview_ranges=1),
        visual_assessment=measured_dark_assessment(),
    )
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )

    assert all(end - start >= 1e-6 for start, end in plan.preview_ranges)
    assert any(
        action_start < preview_end and preview_start < action_end
        for action_start, action_end in action.source_ranges
        for preview_start, preview_end in plan.preview_ranges
    )


def test_stabilization_is_planned_when_native_preview_is_available() -> None:
    """Native preview support keeps measured stabilization in the plan."""
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.SHAKE, 2.0, 4.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="Measured motion supports bounded stabilization review.",
            crop_ratio=0.05,
            parameters={"crop_ratio": 0.05, "max_crop_ratio": 0.12},
        ),
    )

    assert RescueActionKind.STABILIZE in {action.kind for action in plan.actions}
    assert "preview_renderer_unavailable" not in " ".join(plan.assessment_warnings)


def test_preview_cap_review_gates_every_uncovered_action() -> None:
    """Catches confirming a fourth disjoint action beyond the preview-window cap."""
    visual = VisualAssessment(
        metrics=measured_dark_assessment().metrics,
        recommended_actions=(
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
        ),
        evidence=(
            VisualEvidence(
                action=RescueActionKind.ADJUST_LUMA,
                timestamp_seconds=1.5,
                metric="luma_p10",
                observed=0.05,
                threshold=0.18,
                context_luma_p50=0.08,
            ),
        ),
        preview_required=True,
        public_explanation="Measured samples support bounded previews.",
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.DARK, 1.0, 2.0),
            interval(DamageKind.VIDEO_NOISE, 5.0, 6.0),
            interval(DamageKind.SOFT_DETAIL, 9.0, 10.0),
            interval(DamageKind.FLICKER, 13.0, 14.0),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(max_preview_ranges=3),
        visual_assessment=visual,
        flicker_correction=FlickerCorrectionPlan(
            intervals=((13.0, 14.0),),
            gains=((13.0, 1.0), (14.0, 1.01)),
        ),
    )

    confirmable = {action.id for action in plan.actions if action.requires_confirmation}
    assert len(confirmable) == 3
    assert all(
        any(
            start < preview_end and preview_start < end
            for start, end in action.source_ranges
            for preview_start, preview_end in plan.preview_ranges
        )
        for action in plan.actions
        if action.id in confirmable
    )
    assert "preview_range_uncovered" in " ".join(plan.assessment_warnings)


def test_preview_budget_fairly_retains_three_measured_capabilities() -> None:
    """A ten-second budget must not starve a later review-gated deblur action."""
    metadata, map_, measurements, stabilization = _v15_preview_case()
    config = RescueEffectiveConfig(
        max_preview_ranges=3,
        max_preview_total_seconds=10.0,
    )

    first = build_rescue_plan(
        metadata=metadata,
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
        requested_symptoms=(
            RescueSymptom.SOFT_DETAIL,
            RescueSymptom.AUDIO_NOISE,
            RescueSymptom.SHAKE,
        ),
        assessment_parameters=measurements,
        stabilization_assessment=stabilization,
    )
    second = build_rescue_plan(
        metadata=metadata,
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
        requested_symptoms=(
            RescueSymptom.SOFT_DETAIL,
            RescueSymptom.AUDIO_NOISE,
            RescueSymptom.SHAKE,
        ),
        assessment_parameters=measurements,
        stabilization_assessment=stabilization,
    )

    required = {
        RescueActionKind.DEBLUR,
        RescueActionKind.DENOISE_AUDIO,
        RescueActionKind.STABILIZE,
    }
    actions = tuple(action for action in first.actions if action.kind in required)
    assert {action.kind for action in actions} == required
    assert [action.kind for action in first.actions] == sorted(
        (action.kind for action in first.actions),
        key=lambda kind: list(RescueActionKind).index(kind),
    )
    assert first.preview_ranges == second.preview_ranges
    assert first.plan_digest == second.plan_digest
    assert len(first.preview_ranges) <= config.max_preview_ranges
    assert sum(end - start for start, end in first.preview_ranges) <= (
        config.max_preview_total_seconds
    )
    assert all(
        previous_end <= current_start
        for (_previous_start, previous_end), (current_start, _current_end) in zip(
            first.preview_ranges, first.preview_ranges[1:], strict=False
        )
    )
    assert all(
        _range_has_private_preview(source_range, first.preview_ranges)
        for action in actions
        for source_range in action.source_ranges
    )


def test_anchor_preview_capability_keeps_legal_pts_in_left_window() -> None:
    boundary_pts = 35.333333
    computed_boundary = 35.333333333333336
    transform = MotionTransform(
        timestamp_seconds=boundary_pts,
        translation_x=-2.0,
        translation_y=0.5,
        rotation_degrees=0.0,
        scale=1.0,
        inlier_ratio=0.95,
        residual_pixels=0.2,
        semantics="frame_correction",
    )
    action = RescueAction(
        id="rescue_action_quantized_boundary",
        version="1",
        kind=RescueActionKind.STABILIZE,
        description="Apply exact measured corrections.",
        source_ranges=((32.0, 36.0),),
        parameters={
            "method": "transition_anchor_v1",
            "motion_transforms": [transform.model_dump(mode="json")],
            "config": StabilizationConfig(accepted_ranges=((32.0, 36.0),)).model_dump(
                mode="json"
            ),
        },
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )

    left = evaluate_action_capabilities(
        (action,),
        ((32.0, computed_boundary),),
        duration_seconds=42.0,
        locked_ranges=(),
    )[0]
    right = evaluate_action_capabilities(
        (action,),
        ((computed_boundary, 36.0),),
        duration_seconds=42.0,
        locked_ranges=(),
    )[0]

    assert left.preview_covered is True
    assert right.preview_covered is False


def _direct_stabilization_preview_plan(
    timestamps: tuple[float, ...],
    *,
    action_range: tuple[float, float],
    preview_budget: float,
    locked_ranges: tuple[tuple[float, float], ...] = (),
) -> RescuePlan:
    duration = action_range[1] + 1.0
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            translation_x=-2.0 if index % 2 else 2.0,
            translation_y=0.5,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            semantics="frame_correction",
        )
        for index, timestamp in enumerate(timestamps)
    )
    config = StabilizationConfig(
        frame_width=640,
        frame_height=360,
        accepted_ranges=(action_range,),
    )
    return build_rescue_plan(
        metadata=video_metadata().model_copy(
            update={
                "duration_seconds": duration,
                "estimated_frame_count": math.ceil(duration * 24.0),
                "average_frame_rate": 24.0,
            }
        ),
        damage_map=MediaDamageMap(
            input_hash="a" * 64,
            duration_seconds=duration,
            scan_coverage=((0.0, duration),),
            intervals=(interval(DamageKind.SHAKE, *action_range),),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(
            max_preview_ranges=3,
            max_preview_total_seconds=preview_budget,
            locked_ranges=locked_ranges,
        ),
        locked_ranges=locked_ranges,
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="Measured direct stabilization corrections.",
            crop_ratio=0.02,
            transforms=transforms,
            parameters={
                "method": "transition_anchor_v1",
                "config": config.model_dump(mode="json"),
                "affected_ranges": [list(action_range)],
            },
        ),
    )


def test_direct_stabilization_budget_end_snaps_deterministically() -> None:
    timestamps = tuple(32.0 + round(index / 24.0, 6) for index in range(96))

    first = _direct_stabilization_preview_plan(
        timestamps,
        action_range=(32.0, 36.0),
        preview_budget=10.0 / 3.0,
    )
    second = _direct_stabilization_preview_plan(
        timestamps,
        action_range=(32.0, 36.0),
        preview_budget=10.0 / 3.0,
    )
    action = next(
        item for item in first.actions if item.kind is RescueActionKind.STABILIZE
    )

    assert first.preview_ranges == ((32.0, 35.333333),)
    assert first.preview_ranges == second.preview_ranges
    assert first.plan_digest == second.plan_digest
    assert first.preview_ranges[0][1] - first.preview_ranges[0][0] <= 10.0 / 3.0
    assert action.source_ranges == ((32.0, 36.0),)
    assert _json_object(action.parameters["config"])["accepted_ranges"] == [
        [32.0, 36.0]
    ]


def _shared_stabilization_audio_preview_plan(
    audio_range: tuple[float, float],
    timestamps: tuple[float, ...],
) -> RescuePlan:
    action_range = (0.0, 2.0)
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            translation_x=-2.0 if index % 2 else 2.0,
            translation_y=0.5,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            semantics="frame_correction",
        )
        for index, timestamp in enumerate(timestamps)
    )
    stabilization_config = StabilizationConfig(
        frame_width=640,
        frame_height=360,
        accepted_ranges=(action_range,),
    )
    measured = _perceptual_measurements(radius=3, frequency_hz=880.0)
    tonal_profile = dict(measured["tonal_interference_measurements"][0])
    raw_tone = dict(tonal_profile["interference_profiles"][0])
    raw_tone["channel_indices"] = tuple(raw_tone["channel_indices"])
    tone = InterferenceTone.model_validate(raw_tone).model_copy(
        update={
            "start_seconds": audio_range[0],
            "end_seconds": audio_range[1],
            "render_qualification": _passing_tonal_qualification(
                audio_range[1] - audio_range[0]
            ),
        }
    )
    tonal_profile["source_ranges"] = [list(audio_range)]
    tonal_profile["interference_profiles"] = [tone.model_dump(mode="json")]
    metadata = video_metadata().model_copy(
        update={
            "duration_seconds": 3.0,
            "estimated_frame_count": 72,
            "average_frame_rate": 24.0,
        }
    )
    map_ = MediaDamageMap(
        input_hash="a" * 64,
        duration_seconds=3.0,
        scan_coverage=((0.0, 3.0),),
        intervals=(
            interval(DamageKind.SHAKE, *action_range),
            interval(DamageKind.AUDIO_NOISE, *audio_range),
        ),
    )
    return build_rescue_plan(
        metadata=metadata,
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(
            max_preview_ranges=1,
            max_preview_total_seconds=10.0,
        ),
        assessment_parameters={"tonal_interference_measurements": [tonal_profile]},
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="Measured direct stabilization corrections.",
            crop_ratio=0.02,
            transforms=transforms,
            parameters={
                "method": "transition_anchor_v1",
                "config": stabilization_config.model_dump(mode="json"),
                "affected_ranges": [list(action_range)],
            },
        ),
    )


def test_shared_audio_preview_snaps_internal_stabilization_end() -> None:
    """A shared-cluster cut cannot map one more correction than FFmpeg frames."""
    shared_end = 1.3333333333333333
    timestamps = tuple(round(index / 24.0, 6) for index in range(48))

    first = _shared_stabilization_audio_preview_plan((0.0, shared_end), timestamps)
    second = _shared_stabilization_audio_preview_plan((0.0, shared_end), timestamps)
    stabilize = next(
        action for action in first.actions if action.kind is RescueActionKind.STABILIZE
    )
    assert RescueActionKind.DENOISE_AUDIO in {action.kind for action in first.actions}

    # The old long endpoint admitted the rounded 1.333333 correction while the
    # six-decimal FFmpeg endpoint excluded its corresponding frame: 33 != 32.
    serialized_unsnapped_end = float(f"{shared_end:.6f}")
    assert sum(timestamp < shared_end for timestamp in timestamps) == 33
    assert sum(timestamp < serialized_unsnapped_end for timestamp in timestamps) == 32

    assert first.preview_ranges == ((0.0, 1.333333),)
    assert first.preview_ranges == second.preview_ranges
    assert first.plan_digest == second.plan_digest
    preview_start, preview_end = first.preview_ranges[0]
    mapped_timestamps = tuple(
        float(timestamp)
        for transform in _json_array(stabilize.parameters["motion_transforms"])
        if isinstance(transform, dict)
        for timestamp in (transform.get("timestamp_seconds"),)
        if isinstance(timestamp, (int, float))
        and not isinstance(timestamp, bool)
        and preview_start <= float(timestamp) < preview_end
    )
    assert len(mapped_timestamps) == 32
    assert mapped_timestamps[-1] == 1.291667


def test_shared_audio_preview_snaps_internal_stabilization_start_forward() -> None:
    timestamps = tuple(round(index / 24.0, 6) for index in range(48))
    computed_start = 0.3333333333333333

    plan = _shared_stabilization_audio_preview_plan((computed_start, 2.0), timestamps)

    assert {action.kind for action in plan.actions} >= {
        RescueActionKind.STABILIZE,
        RescueActionKind.DENOISE_AUDIO,
    }
    serialized_unsnapped_start = float(f"{computed_start:.6f}")
    assert sum(timestamp >= serialized_unsnapped_start for timestamp in timestamps) == (
        sum(timestamp >= computed_start for timestamp in timestamps) + 1
    )
    assert plan.preview_ranges == ((0.375, 2.0),)


def test_direct_preview_snap_fails_closed_without_common_multi_action_boundary() -> (
    None
):
    def direct_action(action_id: str, timestamps: tuple[float, ...]) -> RescueAction:
        transforms = tuple(
            MotionTransform(
                timestamp_seconds=timestamp,
                translation_x=1.0,
                translation_y=0.0,
                rotation_degrees=0.0,
                scale=1.0,
                inlier_ratio=0.95,
                residual_pixels=0.2,
                semantics="frame_correction",
            )
            for timestamp in timestamps
        )
        return RescueAction(
            id=action_id,
            version="1",
            kind=RescueActionKind.STABILIZE,
            description="Apply separately bound direct corrections.",
            source_ranges=((0.0, 2.0),),
            parameters={
                "method": "transition_anchor_v1",
                "motion_transforms": [
                    transform.model_dump(mode="json") for transform in transforms
                ],
            },
            changes_content=True,
            requires_confirmation=True,
            strategy=RescueStrategy.BALANCED,
        )

    first = direct_action("rescue_action_direct_inventory_a", (0.0, 0.5, 1.0))
    second = direct_action("rescue_action_direct_inventory_b", (0.0, 0.6, 1.2))

    assert rescue_planner._snap_direct_stabilization_preview_range(  # noqa: SLF001
        (first, second),
        (0.25, 1.5),
        removed_ranges=(),
    ) == (0.25, 0.25)


def test_direct_stabilization_snap_handles_vfr_nonzero_and_preserves_full_end() -> None:
    timestamps = (5.0, 5.07, 5.19, 5.41, 5.8)

    truncated = _direct_stabilization_preview_plan(
        timestamps,
        action_range=(5.0, 6.0),
        preview_budget=0.5,
    )
    full = _direct_stabilization_preview_plan(
        timestamps,
        action_range=(5.0, 6.0),
        preview_budget=2.0,
    )

    assert truncated.preview_ranges == ((5.0, 5.41),)
    assert full.preview_ranges == ((5.0, 6.0),)


@pytest.mark.parametrize(
    ("timestamps", "budget"),
    (
        ((5.0,), 0.02),
        ((5.0, 5.0000005), 0.000001),
    ),
)
def test_direct_stabilization_snap_fails_closed_without_usable_boundary(
    timestamps: tuple[float, ...],
    budget: float,
) -> None:
    plan = _direct_stabilization_preview_plan(
        timestamps,
        action_range=(5.0, 6.0),
        preview_budget=budget,
    )

    assert RescueActionKind.STABILIZE not in {action.kind for action in plan.actions}
    assert plan.preview_ranges == ()
    assert "preview_range_uncovered" in " ".join(plan.assessment_warnings)


def test_direct_stabilization_lock_split_keeps_legal_last_pts_on_left() -> None:
    plan = _direct_stabilization_preview_plan(
        (0.5, 0.9995, 1.2, 1.5),
        action_range=(0.0, 2.0),
        preview_budget=3.0,
        locked_ranges=((1.0, 1.2),),
    )
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.STABILIZE
    )

    assert action.source_ranges == ((0.0, 1.0), (1.2, 2.0))
    assert plan.preview_ranges == ((0.5, 1.0), (1.2, 2.0))


def test_multirange_action_is_not_covered_by_one_intersection() -> None:
    """Every operation range, including the range containing 31s, needs review."""
    action = RescueAction(
        id="rescue_action_multirange_audio_coverage",
        version="1",
        kind=RescueActionKind.DENOISE_AUDIO,
        description="Reduce two measured local interference ranges.",
        source_ranges=((5.0, 10.0), (25.0, 32.0)),
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )

    decision = evaluate_action_capabilities(
        (action,),
        ((5.0, 8.0),),
        duration_seconds=42.0,
        locked_ranges=(),
    )[0]

    assert decision.preview_covered is False
    assert decision.automatic is False
    assert decision.reason is ActionCapabilityReason.PREVIEW_RANGE_UNCOVERED


def test_unrelated_damage_cannot_consume_an_action_preview_budget() -> None:
    """Damage evidence must match action semantics and be clipped to action ranges."""
    metadata, map_, measurements, _stabilization = _v15_preview_case()
    _, broad_map, broad_measurements, _stabilization = _v15_preview_case(
        include_broad_unrelated_damage=True
    )
    tonal_only = {
        "tonal_interference_measurements": measurements[
            "tonal_interference_measurements"
        ]
    }
    broad_tonal_only = {
        "tonal_interference_measurements": broad_measurements[
            "tonal_interference_measurements"
        ]
    }
    config = RescueEffectiveConfig(
        max_preview_ranges=3,
        max_preview_total_seconds=10.0,
    )
    baseline = build_rescue_plan(
        metadata=metadata,
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
        requested_symptoms=(RescueSymptom.AUDIO_NOISE,),
        assessment_parameters=tonal_only,
    )
    with_unrelated = build_rescue_plan(
        metadata=metadata,
        damage_map=broad_map,
        strategy=RescueStrategy.BALANCED,
        config=config,
        requested_symptoms=(RescueSymptom.AUDIO_NOISE,),
        assessment_parameters=broad_tonal_only,
    )
    action = next(
        item
        for item in with_unrelated.actions
        if item.kind is RescueActionKind.DENOISE_AUDIO
    )

    assert with_unrelated.preview_ranges == baseline.preview_ranges
    assert all(
        _range_has_private_preview(source_range, with_unrelated.preview_ranges)
        for source_range in action.source_ranges
    )
    assert all(
        any(
            action_start <= preview_start < preview_end <= action_end
            for action_start, action_end in action.source_ranges
        )
        for preview_start, preview_end in with_unrelated.preview_ranges
    )


def test_preview_allocation_respects_caps_locks_half_open_and_determinism() -> None:
    """Allocated previews are unique, bounded, lock-safe, and half-open."""
    metadata, map_, measurements, _stabilization = _v15_preview_case()
    locked = ((6.0, 7.0),)
    config = RescueEffectiveConfig(
        max_preview_ranges=3,
        max_preview_total_seconds=10.0,
        locked_ranges=locked,
    )
    assessment_parameters = {
        "tonal_interference_measurements": measurements[
            "tonal_interference_measurements"
        ]
    }

    first = build_rescue_plan(
        metadata=metadata,
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
        locked_ranges=locked,
        requested_symptoms=(RescueSymptom.AUDIO_NOISE,),
        assessment_parameters=assessment_parameters,
    )
    second = build_rescue_plan(
        metadata=metadata,
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
        locked_ranges=locked,
        requested_symptoms=(RescueSymptom.AUDIO_NOISE,),
        assessment_parameters=assessment_parameters,
    )
    action = next(
        item for item in first.actions if item.kind is RescueActionKind.DENOISE_AUDIO
    )

    assert first.preview_ranges == second.preview_ranges
    assert first.plan_digest == second.plan_digest
    assert len(first.preview_ranges) == len(set(first.preview_ranges))
    assert len(first.preview_ranges) <= config.max_preview_ranges
    assert sum(end - start for start, end in first.preview_ranges) <= (
        config.max_preview_total_seconds
    )
    assert all(start < end for start, end in first.preview_ranges)
    assert all(
        preview_end <= lock_start or lock_end <= preview_start
        for preview_start, preview_end in first.preview_ranges
        for lock_start, lock_end in locked
    )
    assert all(
        _range_has_private_preview(source_range, first.preview_ranges)
        for source_range in action.source_ranges
    )

    touching = evaluate_action_capabilities(
        (action,),
        ((32.0, 36.0),),
        duration_seconds=42.0,
        locked_ranges=(),
    )[0]
    assert touching.preview_covered is False


def test_preview_slots_round_robin_across_distinct_actions() -> None:
    """An earlier multi-range action cannot starve a later action of every slot."""
    metadata = video_metadata().model_copy(
        update={"duration_seconds": 10.0, "estimated_frame_count": 300}
    )
    shake_ranges = ((0.0, 1.0), (2.0, 3.0), (4.0, 5.0))
    tone = InterferenceTone(
        start_seconds=6.0,
        end_seconds=7.0,
        center_frequency_hz=880.0,
        confidence=0.94,
        baseline_before_dbfs=-52.0,
        baseline_after_dbfs=-51.0,
        peak_dbfs=-14.0,
        local_peak_over_baseline_db=37.0,
        persistence_window_count=40,
        frequency_standard_deviation_hz=1.5,
        channel_indices=(0,),
        attenuation_target_db=24.0,
        render_qualification=_passing_tonal_qualification(1.0),
    )
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=start + 0.5,
            translation_x=-2.0,
            translation_y=0.5,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            scene_boundary=False,
            semantics="frame_correction",
        )
        for start, _end in shake_ranges
    )
    intervals = tuple(
        interval(DamageKind.SHAKE, start, end) for start, end in shake_ranges
    ) + (interval(DamageKind.AUDIO_NOISE, 6.0, 7.0),)
    plan = build_rescue_plan(
        metadata=metadata,
        damage_map=MediaDamageMap(
            input_hash="a" * 64,
            duration_seconds=10.0,
            scan_coverage=((0.0, 10.0),),
            intervals=intervals,
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(
            max_preview_ranges=3,
            max_preview_total_seconds=3.0,
        ),
        requested_symptoms=(RescueSymptom.SHAKE, RescueSymptom.AUDIO_NOISE),
        assessment_parameters={
            "tonal_interference_measurements": [
                {
                    "algorithm_version": "1",
                    "source_ranges": [[6.0, 7.0]],
                    "interference_profiles": [tone.model_dump(mode="json")],
                    "config": TonalInterferenceConfig().model_dump(mode="json"),
                }
            ]
        },
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="measured_anchor_correction",
            crop_ratio=0.04,
            parameters={
                "affected_ranges": [list(item) for item in shake_ranges],
                "config": StabilizationConfig(
                    frame_width=1280,
                    frame_height=720,
                    accepted_ranges=shake_ranges,
                ).model_dump(mode="json"),
            },
            transforms=transforms,
        ),
    )

    assert RescueActionKind.DENOISE_AUDIO in {action.kind for action in plan.actions}
    assert _range_has_private_preview((6.0, 7.0), plan.preview_ranges)


def test_structural_preview_reserves_retained_lock_safe_context() -> None:
    """A long removal cannot be represented only by media it deletes."""
    damage = interval(DamageKind.UNDECODABLE, 2.0, 8.0)
    locked = ((1.0, 2.0),)
    plan = build_rescue_plan(
        metadata=video_metadata().model_copy(
            update={"duration_seconds": 10.0, "estimated_frame_count": 300}
        ),
        damage_map=MediaDamageMap(
            input_hash="a" * 64,
            duration_seconds=10.0,
            scan_coverage=((0.0, 10.0),),
            intervals=(damage,),
        ),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(
            max_preview_ranges=1,
            max_preview_total_seconds=3.0,
            locked_ranges=locked,
        ),
        locked_ranges=locked,
    )

    salvage = next(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.SALVAGE_SEGMENTS
    )
    mappings = tuple(
        mapping
        for index, window in enumerate(plan.preview_ranges)
        for mapping in preview_source_mappings(plan, window, f"faithful-{index}.mp4")
    )
    assert salvage.requires_confirmation is True
    assert mappings
    assert all(
        preview_end <= lock_start or lock_end <= preview_start
        for preview_start, preview_end in plan.preview_ranges
        for lock_start, lock_end in locked
    )


def test_structural_preview_honors_locks_supplied_only_by_planner_argument() -> None:
    """Merged explicit locks bind the plan and every retained-context preview."""
    damage = interval(DamageKind.UNDECODABLE, 4.0, 5.0)
    locked = ((5.2, 5.4),)
    plan = build_rescue_plan(
        metadata=video_metadata().model_copy(
            update={"duration_seconds": 10.0, "estimated_frame_count": 300}
        ),
        damage_map=MediaDamageMap(
            input_hash="a" * 64,
            duration_seconds=10.0,
            scan_coverage=((0.0, 10.0),),
            intervals=(damage,),
        ),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(
            max_preview_ranges=1,
            max_preview_total_seconds=10.0,
        ),
        locked_ranges=locked,
    )

    mappings = tuple(
        mapping
        for index, window in enumerate(plan.preview_ranges)
        for mapping in preview_source_mappings(plan, window, f"faithful-{index}.mp4")
    )
    assert plan.effective_config.locked_ranges == locked
    assert mappings
    assert all(mapping.source_end > mapping.source_start for mapping in mappings)
    assert all(
        preview_end <= lock_start or lock_end <= preview_start
        for preview_start, preview_end in plan.preview_ranges
        for lock_start, lock_end in locked
    )


def test_global_actions_conflicting_with_locks_are_review_gated() -> None:
    """Catches whole-stream rotation or offset changes crossing a user lock."""
    metadata = video_metadata().model_copy(update={"raw_probe": {"rotation": 90.0}})
    plan = build_rescue_plan(
        metadata=metadata,
        damage_map=damage_map(),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(locked_ranges=((1.0, 2.0),)),
        locked_ranges=((1.0, 2.0),),
        fixed_offset_assessment=FixedOffsetAssessment(
            offset_seconds=0.1,
            shift_seconds=-0.1,
            correlation=0.95,
            matched_event_count=3,
            agreement_seconds=0.01,
        ),
    )

    assert {action.kind for action in plan.actions}.isdisjoint(
        {
            RescueActionKind.NORMALIZE_ROTATION,
            RescueActionKind.CORRECT_FIXED_AV_OFFSET,
        }
    )
    warnings_text = " ".join(plan.assessment_warnings)
    assert warnings_text.count("locked_range_conflict") == 2


def test_capability_policy_rejects_a_local_range_that_still_overlaps_a_lock() -> None:
    """Catches bypassing upstream lock subtraction before plan issuance."""
    action = RescueAction(
        id="rescue_action_local_lock_regression",
        version="1",
        kind=RescueActionKind.ADJUST_LUMA,
        description="Adjust one measured local range.",
        source_ranges=((1.0, 3.0),),
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )

    decision = evaluate_action_capabilities(
        (action,),
        ((1.0, 3.0),),
        duration_seconds=4.0,
        locked_ranges=((2.0, 2.5),),
    )[0]

    assert decision.range_exact is False
    assert decision.automatic is False
    assert decision.reason is ActionCapabilityReason.LOCKED_RANGE_CONFLICT


def test_action_capability_profiles_cover_every_action_kind() -> None:
    """Catches adding an action kind without an explicit capability decision."""
    profiles = getattr(rescue_capabilities, "_ACTION_CAPABILITY_PROFILES", {})

    assert set(profiles) == set(RescueActionKind)


def test_deblur_capability_is_local_previewable_and_review_gated() -> None:
    """Catches DEBLUR becoming automatic before Task 7 outcome verification."""
    profiles = getattr(rescue_capabilities, "_ACTION_CAPABILITY_PROFILES", {})
    profile = profiles[RescueActionKind.DEBLUR]
    assert profile.range_mode == "local"
    assert profile.preview_supported is True
    assert profile.verification_mode == "needs_review"

    action = RescueAction(
        id="rescue_action_deblur_preview_regression",
        version="1",
        kind=RescueActionKind.DEBLUR,
        description="Restore locally measured soft detail.",
        source_ranges=((1.0, 2.0),),
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )

    decision = evaluate_action_capabilities(
        (action,),
        ((1.0, 2.0),),
        duration_seconds=4.0,
        locked_ranges=(),
    )[0]

    assert decision.preview_supported is True
    assert decision.range_exact is True
    assert decision.verification_mode == "needs_review"
    assert decision.automatic is False
    assert decision.reason is ActionCapabilityReason.RANGE_MAPPING_UNAVAILABLE


def test_missing_action_capability_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an unprofiled content action defaulting to automatic eligibility."""
    action = RescueAction(
        id="rescue_action_missing_profile_regression",
        version="1",
        kind=RescueActionKind.ADJUST_LUMA,
        description="Adjust one measured local range.",
        source_ranges=((1.0, 2.0),),
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    monkeypatch.setattr(
        rescue_capabilities,
        "_ACTION_CAPABILITY_PROFILES",
        {},
        raising=False,
    )

    decision = evaluate_action_capabilities(
        (action,),
        ((1.0, 2.0),),
        duration_seconds=4.0,
        locked_ranges=(),
    )[0]

    assert decision.preview_supported is False
    assert decision.verification_mode == "needs_review"
    assert decision.automatic is False
    assert decision.reason is ActionCapabilityReason.PREVIEW_RENDERER_UNAVAILABLE


def test_plan_has_stable_action_order_digest_and_bounded_non_overlapping_previews() -> (
    None
):
    """Catches nondeterministic planning or preview ranges exceeding their cap."""
    map_ = damage_map(
        interval(DamageKind.VIDEO_NOISE, 5.0, 14.0),
        interval(DamageKind.UNDECODABLE, 0.0, 5.0),
        interval(DamageKind.FLICKER, 14.0, 18.0),
    )
    config = RescueEffectiveConfig(max_preview_total_seconds=10.0)

    first = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
    )
    second = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
    )

    assert [action.kind for action in first.actions] == sorted(
        (action.kind for action in first.actions),
        key=lambda kind: list(RescueActionKind).index(kind),
    )
    assert first.plan_digest == second.plan_digest
    assert first.preview_ranges == second.preview_ranges
    assert sum(end - start for start, end in first.preview_ranges) <= (
        config.max_preview_total_seconds
    )
    assert all(
        previous_end <= following_start
        for (_previous_start, previous_end), (following_start, _following_end) in zip(
            first.preview_ranges, first.preview_ranges[1:], strict=False
        )
    )
    salvage = next(
        action
        for action in first.actions
        if action.kind is RescueActionKind.SALVAGE_SEGMENTS
    )
    assert any(
        action_start < preview_end and preview_start < action_end
        for action_start, action_end in salvage.source_ranges
        for preview_start, preview_end in first.preview_ranges
    )
    assert sum(end - start for start, end in first.preview_ranges) <= 10.0
    assert all(
        previous[1] <= current[0]
        for previous, current in zip(first.preview_ranges, first.preview_ranges[1:])
    )


def test_planner_declares_the_complete_faithful_public_bundle() -> None:
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(),
    )
    assert plan.public_artifacts == (
        "rescue-plan.json",
        "damaged-segments.json",
        "changes.json",
        "verification-report.json",
        "technical-report.json",
        "report.html",
        "faithful-rescue.mp4",
    )


def test_list_parameters_are_immutable_and_serialize_without_warnings() -> None:
    """Catches frozen JSON lists becoming serializer-invalid tuples in a Rescue plan."""
    map_ = damage_map(interval(DamageKind.DARK, 2.0, 5.0))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        first = build_rescue_plan(
            metadata=video_metadata(),
            damage_map=map_,
            strategy=RescueStrategy.BALANCED,
            config=RescueEffectiveConfig(),
            visual_assessment=measured_dark_assessment(),
        )
        serialized = first.model_dump(mode="json")
    second = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=measured_dark_assessment(),
    )
    damage_ids = next(
        action.parameters["damage_ids"]
        for action in first.actions
        if action.kind is RescueActionKind.ADJUST_LUMA
    )

    assert serialized["actions"]
    assert damage_ids == [map_.intervals[0].id]
    assert first.plan_digest == second.plan_digest
    with pytest.raises(TypeError):
        damage_ids.append("not-allowed")


def test_planner_omits_unmeasured_flicker_and_stabilization_actions() -> None:
    """Catches generic filters standing in for a reviewed correction curve."""
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.FLICKER, 2.0, 4.0),
            interval(DamageKind.SHAKE, 5.0, 7.0),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
    )

    assert {action.kind for action in plan.actions}.isdisjoint(
        {RescueActionKind.DEFLICKER, RescueActionKind.STABILIZE}
    )
    assert "improved-viewing.mp4" not in plan.public_artifacts


def test_symptom_hint_is_digest_bound_but_cannot_invent_an_action() -> None:
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        requested_symptoms=(RescueSymptom.DARK,),
    )

    assert plan.requested_symptoms == (RescueSymptom.DARK,)
    assert RescueActionKind.ADJUST_LUMA not in {action.kind for action in plan.actions}


def test_explicit_symptoms_limit_balanced_repairs_to_requested_problem_types() -> None:
    visual = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.05,
            luma_p50=0.08,
            luma_p90=0.3,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.01,
            sharpness=0.001,
        ),
        recommended_actions=(
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.SHARPEN,
        ),
        evidence=(
            VisualEvidence(
                action=RescueActionKind.ADJUST_LUMA,
                timestamp_seconds=3.0,
                metric="luma_p10",
                observed=0.05,
                threshold=0.18,
            ),
            VisualEvidence(
                action=RescueActionKind.SHARPEN,
                timestamp_seconds=8.0,
                metric="scene_relative_sharpness",
                observed=0.001,
                threshold=0.003,
            ),
        ),
        preview_required=True,
        public_explanation="Measured dark and soft samples support previews.",
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.DARK, 0.0, 20.0),
            interval(DamageKind.SOFT_DETAIL, 7.0, 10.0),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        requested_symptoms=(RescueSymptom.SOFT_DETAIL,),
        visual_assessment=visual,
    )

    kinds = {action.kind for action in plan.actions}
    assert RescueActionKind.SHARPEN in kinds
    assert RescueActionKind.ADJUST_LUMA not in kinds
    assert plan.preview_ranges == ((7.0, 10.0),)


def test_no_explicit_symptoms_preserves_all_measured_balanced_suggestions() -> None:
    visual = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.05,
            luma_p50=0.08,
            luma_p90=0.3,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.01,
            sharpness=0.001,
        ),
        recommended_actions=(
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.SHARPEN,
        ),
        evidence=(
            VisualEvidence(
                action=RescueActionKind.ADJUST_LUMA,
                timestamp_seconds=3.0,
                metric="luma_p10",
                observed=0.05,
                threshold=0.18,
                context_luma_p50=0.08,
            ),
        ),
        preview_required=True,
        public_explanation="Measured dark and soft samples support previews.",
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.DARK, 2.0, 5.0),
            interval(DamageKind.SOFT_DETAIL, 7.0, 10.0),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=visual,
    )

    kinds = {action.kind for action in plan.actions}
    assert {RescueActionKind.ADJUST_LUMA, RescueActionKind.SHARPEN} <= kinds
