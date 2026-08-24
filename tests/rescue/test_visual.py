"""Tests for deterministic, bounded CPU visual assessment."""

from __future__ import annotations

import math
import random
from itertools import permutations

import pytest
from pydantic import JsonValue, ValidationError

import videoscope.rescue.visual as visual_module
from videoscope.rescue.models import RescueActionKind, RescueVerificationStatus
from videoscope.rescue.visual import (
    LumaAdjustmentConfig,
    SharpenConfig,
    VideoDenoiseConfig,
    VisualAssessmentConfig,
    VisualEvidence,
    VisualMetrics,
    VisualSample,
    assess_visual_samples,
    compare_visual_metrics,
    denoise_filter_fragment,
    derive_visual_action_parameters,
    filter_fragment_from_action,
    luma_filter_fragment,
    sharpen_filter_fragment,
)
from videoscope.scenes.models import VideoScene


def _json_float(value: JsonValue) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _sample(timestamp: float, rows: tuple[tuple[float, ...], ...]) -> VisualSample:
    return VisualSample(timestamp_seconds=timestamp, luma=rows)


def _edge_rows(level: float = 1.0) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(0.3 * level if column < 4 else 0.7 * level for column in range(8))
        for _ in range(8)
    )


def _noisy_dark_rows(offset: int) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(
            0.035 if (row + column + offset) % 2 == 0 else 0.125 for column in range(8)
        )
        for row in range(8)
    )


def _random_dark_rows(
    seed: int, standard_deviation: float
) -> tuple[tuple[float, ...], ...]:
    generator = random.Random(seed)
    return tuple(
        tuple(
            min(1.0, max(0.0, generator.gauss(0.09, standard_deviation)))
            for _column in range(32)
        )
        for _row in range(32)
    )


def _one_scene() -> tuple[VideoScene, ...]:
    return (
        VideoScene(
            scene_index=0,
            start_seconds=0.0,
            end_seconds=2.0,
            duration_seconds=2.0,
            representative_timestamp=1.0,
        ),
    )


def _visual_config() -> VisualAssessmentConfig:
    return VisualAssessmentConfig(
        luma=LumaAdjustmentConfig(
            dark_percentile_threshold=0.18,
            shadow_percentile=10.0,
            low_clip_level=0.01,
            high_clip_level=0.99,
            maximum_clip_ratio=0.02,
            brightness=0.04,
            contrast=1.02,
        ),
        denoise=VideoDenoiseConfig(
            residual_threshold=0.04,
            luma_spatial=1.5,
            chroma_spatial=1.0,
            luma_temporal=2.0,
            chroma_temporal=1.5,
        ),
        sharpen=SharpenConfig(
            relative_sharpness_threshold=0.45,
            absolute_sharpness_floor=0.003,
            radius=2,
            amount=0.4,
        ),
    )


def test_clean_video_does_not_receive_balanced_filters() -> None:
    """Catches applying subjective filters when bounded visual metrics are clean."""
    samples = tuple(_sample(index * 0.5, _edge_rows()) for index in range(4))

    assessment = assess_visual_samples(samples, _one_scene(), _visual_config())

    assert assessment.recommended_actions == ()
    assert assessment.preview_required is False


def test_dark_noisy_video_gets_bounded_luma_and_denoise_without_false_claim() -> None:
    """Catches missing measured actions or claiming unavailable detail was recovered."""
    samples = tuple(_sample(index * 0.5, _noisy_dark_rows(index)) for index in range(4))

    assessment = assess_visual_samples(samples, _one_scene(), _visual_config())

    assert assessment.recommended_actions == (
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
    )
    assert assessment.preview_required is True


def test_severely_soft_detail_uses_multistage_recovery_bound_to_scene_baseline() -> (
    None
):
    """Catches a weak single sharpen pass being called a useful restoration."""
    parameters = derive_visual_action_parameters(
        RescueActionKind.SHARPEN,
        VisualMetrics(
            luma_p10=0.0,
            luma_p50=0.0,
            luma_p90=0.08,
            low_clip_ratio=0.8,
            high_clip_ratio=0.0,
            noise_residual=0.001,
            sharpness=0.002,
        ),
        sharpen_config=SharpenConfig(
            absolute_sharpness_floor=0.04,
            minimum_recovered_baseline_ratio=0.8,
        ),
    )

    assert parameters["minimum_recovered_baseline_ratio"] == pytest.approx(0.8)
    detail_passes = parameters["detail_passes"]
    assert isinstance(detail_passes, int)
    assert detail_passes >= 2
    fragment = filter_fragment_from_action(RescueActionKind.SHARPEN, parameters)
    assert fragment is not None
    assert fragment.count("unsharp=") >= 2
    assert "cas=strength=" in fragment


def test_scene_relative_soft_sample_gets_bounded_sharpening_evidence() -> None:
    """Catches sharpening a whole soft scene or omitting a locally soft sample."""
    samples = (
        _sample(0.0, _edge_rows()),
        _sample(0.5, _edge_rows()),
        _sample(1.0, _edge_rows()),
        _sample(1.5, tuple(tuple(0.5 for _ in range(8)) for _ in range(8))),
    )

    assessment = assess_visual_samples(samples, _one_scene(), _visual_config())

    assert assessment.recommended_actions == (RescueActionKind.SHARPEN,)
    assert assessment.evidence == (
        VisualEvidence(
            action=RescueActionKind.SHARPEN,
            timestamp_seconds=1.5,
            metric="scene_relative_sharpness",
            observed=0.0,
            threshold=0.003,
            context_luma_p50=0.5,
            scene_baseline_sharpness=0.05333333333333331,
        ),
    )
    assert tuple(
        (item.start_seconds, item.end_seconds) for item in assessment.action_intervals
    ) == ((1.25, 1.75),)
    assert "preview-only" in assessment.public_explanation


def test_contiguous_soft_samples_keep_the_full_measured_interval() -> None:
    """Catches shrinking repair scope to the few thumbnails shown as evidence."""
    samples = tuple(
        _sample(timestamp, _edge_rows() if timestamp < 2.0 else ((0.5,) * 8,) * 8)
        for timestamp in (0.25, 0.75, 1.25, 1.75, 2.25, 2.75, 3.25, 3.75)
    )
    scenes = (
        VideoScene(
            scene_index=0,
            start_seconds=0.0,
            end_seconds=4.0,
            duration_seconds=4.0,
            representative_timestamp=2.0,
        ),
    )

    assessment = assess_visual_samples(samples, scenes, _visual_config())

    assert len(assessment.evidence) <= _visual_config().max_evidence_samples
    assert tuple(
        (item.start_seconds, item.end_seconds) for item in assessment.action_intervals
    ) == ((2.0, 4.0),)


def test_luma_evidence_budget_keeps_only_strongest_covered_ranges() -> None:
    config = VisualAssessmentConfig(max_evidence_samples=2)
    samples = tuple(
        _sample(timestamp, ((level,),))
        for timestamp, level in (
            (1.1, 0.05),
            (1.2, 0.05),
            (1.4, 0.50),
            (4.5, 0.08),
            (4.6, 0.08),
            (4.8, 0.50),
            (8.0, 0.10),
            (8.1, 0.10),
        )
    )

    first = assess_visual_samples(samples, (), config)
    reordered = assess_visual_samples(tuple(reversed(samples)), (), config)

    assert first == reordered
    assert tuple(
        item.timestamp_seconds
        for item in first.evidence
        if item.action is RescueActionKind.ADJUST_LUMA
    ) == (1.1, 4.5)
    luma_ranges = tuple(
        (item.start_seconds, item.end_seconds)
        for item in first.action_intervals
        if item.action is RescueActionKind.ADJUST_LUMA
    )
    assert len(luma_ranges) == config.max_evidence_samples
    assert all(
        any(start <= evidence.timestamp_seconds < end for start, end in luma_ranges)
        for evidence in first.evidence
        if evidence.action is RescueActionKind.ADJUST_LUMA
    )
    assert any("evidence budget" in item for item in first.limitations)


def test_finite_float_order_is_ieee_stable_for_signed_zero_and_extremes() -> None:
    minimum_subnormal = float.fromhex("0x0.0000000000001p-1022")
    maximum_finite = float.fromhex("0x1.fffffffffffffp+1023")
    expected_zero_keys = [
        visual_module._finite_float_order_key(value)
        for value in (0.0, -0.0, minimum_subnormal)
    ]

    for values in permutations((-0.0, 0.0, minimum_subnormal)):
        assert [
            visual_module._finite_float_order_key(value)
            for value in sorted(values, key=visual_module._finite_float_order_key)
        ] == expected_zero_keys

    extremes = (
        maximum_finite,
        -0.0,
        -minimum_subnormal,
        0.0,
        -maximum_finite,
        minimum_subnormal,
    )
    assert [
        visual_module._finite_float_order_key(value)
        for value in sorted(extremes, key=visual_module._finite_float_order_key)
    ] == [
        visual_module._finite_float_order_key(value)
        for value in (
            -maximum_finite,
            -minimum_subnormal,
            0.0,
            -0.0,
            minimum_subnormal,
            maximum_finite,
        )
    ]

    for value in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError, match="requires a finite value"):
            visual_module._finite_float_order_key(value)
        with pytest.raises(ValidationError):
            VisualSample(timestamp_seconds=value, luma=((0.0,),))
        with pytest.raises(ValidationError):
            VisualSample(timestamp_seconds=0.0, luma=((value,),))


def test_bounded_filter_fragments_are_exact_and_reject_unvalidated_parameters() -> None:
    """Catches a filter fragment changing silently or accepting action text."""
    assert (
        luma_filter_fragment(LumaAdjustmentConfig())
        == "eq=brightness=0.04:contrast=1.02:gamma=1.5:gamma_weight=0.85"
    )
    assert denoise_filter_fragment(VideoDenoiseConfig()) == "hqdn3d=1.5:1:2:1.5"
    assert (
        sharpen_filter_fragment(SharpenConfig())
        == "cas=strength=0.32,unsharp=5:5:1:5:5:0"
    )


def test_luma_parameters_are_derived_from_measured_darkness_and_are_bounded() -> None:
    """Catches reverting to one invisible fixed lift for every dark video."""
    config = LumaAdjustmentConfig()
    extremely_dark = VisualMetrics(
        luma_p10=0.035,
        luma_p50=0.035,
        luma_p90=0.14,
        low_clip_ratio=0.005,
        high_clip_ratio=0.0,
        noise_residual=0.01,
        sharpness=0.03,
    )
    moderately_dark = extremely_dark.model_copy(
        update={"luma_p10": 0.12, "luma_p50": 0.16, "luma_p90": 0.42}
    )

    extreme = derive_visual_action_parameters(
        RescueActionKind.ADJUST_LUMA, extremely_dark, luma_config=config
    )
    moderate = derive_visual_action_parameters(
        RescueActionKind.ADJUST_LUMA, moderately_dark, luma_config=config
    )

    assert _json_float(extreme["brightness"]) > _json_float(moderate["brightness"])
    assert _json_float(extreme["gamma"]) > _json_float(moderate["gamma"])
    assert (
        config.minimum_brightness
        <= _json_float(extreme["brightness"])
        <= config.maximum_brightness
    )
    assert config.minimum_gamma <= _json_float(extreme["gamma"]) <= config.maximum_gamma
    assert extreme["derivation_version"] == "5"
    assert extreme["observed_luma_p10"] == pytest.approx(0.035)
    assert extreme["target_shadow_luma"] == pytest.approx(config.target_shadow_luma)


def test_visible_noise_below_denoise_gate_neutralizes_luma_contrast() -> None:
    """Catches a luma-only action amplifying visible residual below denoise scope."""
    config = VisualAssessmentConfig(
        luma=LumaAdjustmentConfig(contrast_noise_guard_threshold=0.01),
        denoise=VideoDenoiseConfig(residual_threshold=0.04),
    )
    samples = tuple(
        _sample(index * 0.5, _random_dark_rows(index, 0.03)) for index in range(4)
    )

    assessment = assess_visual_samples(samples, _one_scene(), config)
    parameters = derive_visual_action_parameters(
        RescueActionKind.ADJUST_LUMA,
        assessment.metrics,
        luma_config=config.luma,
    )

    assert assessment.recommended_actions == (RescueActionKind.ADJUST_LUMA,)
    assert (
        config.luma.contrast_noise_guard_threshold
        <= assessment.metrics.noise_residual
        < config.denoise.residual_threshold
    )
    assert parameters["contrast"] == pytest.approx(1.0)
    assert parameters["observed_noise_residual"] == pytest.approx(
        assessment.metrics.noise_residual
    )
    assert parameters["contrast_noise_guard_threshold"] == pytest.approx(
        config.luma.contrast_noise_guard_threshold
    )
    assert parameters["contrast_derivation"] == "noise_guarded"
    assert parameters["filter_mode"] == "noise_guarded_y_offset"
    assert parameters["gamma"] == pytest.approx(1.0)
    expected_lift_steps = round(
        max(
            _json_float(parameters["brightness"]),
            config.luma.minimum_perceptible_luma_delta,
        )
        * 255
        * 2
    )
    assert parameters["luma_lift_steps"] == expected_lift_steps
    assert parameters["noise_guard_video_crf"] == 23
    assert parameters["noise_guard_chroma_qp_offset"] == -6
    assert parameters["maximum_residual_increase"] == pytest.approx(0.0)
    assert parameters["maximum_chroma_shift"] == pytest.approx(0.01)
    assert parameters["maximum_luma_improvement_delta"] == pytest.approx(0.08)
    fragment = filter_fragment_from_action(RescueActionKind.ADJUST_LUMA, parameters)
    assert fragment is not None
    assert fragment.startswith(f"lutyuv=y='val+{expected_lift_steps}'")
    assert fragment == f"lutyuv=y='val+{expected_lift_steps}'"
    assert "hqdn3d" not in fragment
    assert "lutrgb" not in fragment
    assert "eq=" not in fragment


def test_noise_guarded_luma_filter_fails_closed_on_mode_or_lift_tamper() -> None:
    luma_config = LumaAdjustmentConfig(
        contrast_noise_guard_threshold=0.015,
        minimum_perceptible_luma_delta=0.05,
        maximum_luma_improvement_delta=0.09,
        maximum_chroma_shift=0.008,
        noise_guard_video_crf=23,
    )
    parameters = derive_visual_action_parameters(
        RescueActionKind.ADJUST_LUMA,
        VisualMetrics(
            luma_p10=0.08,
            luma_p50=0.24,
            luma_p90=0.50,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.03,
            sharpness=0.02,
        ),
        luma_config=luma_config,
    )

    assert parameters["filter_mode"] == "noise_guarded_y_offset"
    assert parameters["luma_config"] == luma_config.model_dump(mode="json")
    fragment = filter_fragment_from_action(RescueActionKind.ADJUST_LUMA, parameters)
    assert fragment is not None
    assert fragment.startswith("lutyuv=y=")
    assert (
        filter_fragment_from_action(
            RescueActionKind.ADJUST_LUMA,
            {**parameters, "filter_mode": "eq"},
        )
        is None
    )
    assert (
        filter_fragment_from_action(
            RescueActionKind.ADJUST_LUMA,
            {**parameters, "luma_lift_steps": 0},
        )
        is None
    )
    for update in (
        {"derivation_version": "999"},
        {"observed_noise_residual": 0.0},
        {"contrast_noise_guard_threshold": 0.2},
        {"target_shadow_luma": 0.2},
        {"noise_guard_video_crf": 22},
        {"noise_guard_chroma_qp_offset": -8},
    ):
        assert (
            filter_fragment_from_action(
                RescueActionKind.ADJUST_LUMA,
                {**parameters, **update},
            )
            is None
        )


def test_noise_guarded_chroma_qp_offset_is_strict_and_fail_closed() -> None:
    metrics = VisualMetrics(
        luma_p10=0.08,
        luma_p50=0.24,
        luma_p90=0.50,
        low_clip_ratio=0.0,
        high_clip_ratio=0.0,
        noise_residual=0.03,
        sharpness=0.02,
    )
    parameters = derive_visual_action_parameters(
        RescueActionKind.ADJUST_LUMA,
        metrics,
        luma_config=LumaAdjustmentConfig(noise_guard_chroma_qp_offset=-6),
    )

    assert parameters["noise_guard_chroma_qp_offset"] == -6
    luma_config = parameters["luma_config"]
    assert isinstance(luma_config, dict)
    assert luma_config["noise_guard_chroma_qp_offset"] == -6

    missing = dict(parameters)
    missing.pop("noise_guard_chroma_qp_offset")
    invalid = (
        missing,
        {**parameters, "noise_guard_chroma_qp_offset": "-6"},
        {**parameters, "noise_guard_chroma_qp_offset": -6.0},
        {**parameters, "noise_guard_chroma_qp_offset": True},
        {**parameters, "noise_guard_chroma_qp_offset": 1},
        {**parameters, "noise_guard_chroma_qp_offset": -13},
    )
    for candidate in invalid:
        assert (
            filter_fragment_from_action(RescueActionKind.ADJUST_LUMA, candidate) is None
        )


@pytest.mark.parametrize(
    "value",
    ("-6", -6.0, True, 1, -13),
)
def test_luma_config_rejects_invalid_chroma_qp_offset(value: object) -> None:
    with pytest.raises(ValueError):
        LumaAdjustmentConfig(noise_guard_chroma_qp_offset=value)  # type: ignore[arg-type]


def test_dark_clean_measurement_keeps_configured_contrast() -> None:
    """Catches disabling bounded contrast for every dark input unconditionally."""
    config = VisualAssessmentConfig(
        luma=LumaAdjustmentConfig(
            contrast=1.02,
            contrast_noise_guard_threshold=0.01,
        ),
        denoise=VideoDenoiseConfig(residual_threshold=0.04),
    )
    samples = tuple(
        _sample(index * 0.5, _random_dark_rows(index, 0.001)) for index in range(4)
    )

    assessment = assess_visual_samples(samples, _one_scene(), config)
    parameters = derive_visual_action_parameters(
        RescueActionKind.ADJUST_LUMA,
        assessment.metrics,
        luma_config=config.luma,
    )

    assert (
        assessment.metrics.noise_residual < config.luma.contrast_noise_guard_threshold
    )
    assert parameters["contrast"] == pytest.approx(config.luma.contrast)
    assert parameters["contrast_derivation"] == "configured"
    assert parameters["filter_mode"] == "eq"
    assert parameters["luma_lift_steps"] is None
    assert parameters["noise_guard_video_crf"] is None
    assert parameters["noise_guard_chroma_qp_offset"] is None
    fragment = filter_fragment_from_action(RescueActionKind.ADJUST_LUMA, parameters)
    assert fragment is not None
    assert fragment.startswith("eq=")


def test_high_noise_keeps_explicit_denoise_and_derives_strength_from_measurement() -> (
    None
):
    """Catches reverting measured high-noise actions to one fixed filter strength."""
    config = VisualAssessmentConfig(
        denoise=VideoDenoiseConfig(
            residual_threshold=0.04,
            minimum_strength_ratio=0.5,
            full_strength_residual=0.08,
        )
    )
    moderate = assess_visual_samples(
        tuple(
            _sample(index * 0.5, _random_dark_rows(index, 0.10)) for index in range(4)
        ),
        _one_scene(),
        config,
    )
    severe = assess_visual_samples(
        tuple(
            _sample(index * 0.5, _random_dark_rows(index + 20, 0.18))
            for index in range(4)
        ),
        _one_scene(),
        config,
    )

    moderate_parameters = derive_visual_action_parameters(
        RescueActionKind.DENOISE_VIDEO,
        moderate.metrics,
        denoise_config=config.denoise,
    )
    severe_parameters = derive_visual_action_parameters(
        RescueActionKind.DENOISE_VIDEO,
        severe.metrics,
        denoise_config=config.denoise,
    )

    assert RescueActionKind.DENOISE_VIDEO in moderate.recommended_actions
    assert RescueActionKind.DENOISE_VIDEO in severe.recommended_actions
    assert moderate.metrics.noise_residual < severe.metrics.noise_residual
    assert _json_float(moderate_parameters["strength_ratio"]) < _json_float(
        severe_parameters["strength_ratio"]
    )
    assert _json_float(moderate_parameters["luma_temporal"]) < _json_float(
        severe_parameters["luma_temporal"]
    )
    assert moderate_parameters["observed_noise_residual"] == pytest.approx(
        moderate.metrics.noise_residual
    )


def test_confirmed_luma_filter_revalidates_gamma_and_gamma_weight() -> None:
    parameters = derive_visual_action_parameters(
        RescueActionKind.ADJUST_LUMA,
        VisualMetrics(
            luma_p10=0.035,
            luma_p50=0.035,
            luma_p90=0.14,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.0,
            sharpness=0.03,
        ),
    )

    fragment = filter_fragment_from_action(RescueActionKind.ADJUST_LUMA, parameters)

    assert fragment is not None
    assert ":gamma=" in fragment
    assert ":gamma_weight=" in fragment
    assert (
        filter_fragment_from_action(
            RescueActionKind.ADJUST_LUMA,
            {**parameters, "gamma": 99.0},
        )
        is None
    )
    assert (
        filter_fragment_from_action(
            RescueActionKind.DENOISE_VIDEO,
            {"luma_spatial": "1.5"},
        )
        is None
    )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_visual_configs_reject_non_finite_values(value: float) -> None:
    """Catches non-finite thresholds reaching deterministic filters or JSON."""
    with pytest.raises(ValidationError):
        LumaAdjustmentConfig(brightness=value)
    with pytest.raises(ValidationError):
        LumaAdjustmentConfig(contrast_noise_guard_threshold=value)
    with pytest.raises(ValidationError):
        VideoDenoiseConfig(residual_threshold=value)
    with pytest.raises(ValidationError):
        VideoDenoiseConfig(minimum_strength_ratio=value)
    with pytest.raises(ValidationError):
        VideoDenoiseConfig(full_strength_residual=value)
    with pytest.raises(ValidationError):
        SharpenConfig(amount=value)


def test_visual_configs_are_strict_and_filter_strengths_are_bounded() -> None:
    """Catches silent coercion, unknown keys, or excessive filter strengths."""
    with pytest.raises(ValidationError):
        LumaAdjustmentConfig(brightness="0.04")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        VideoDenoiseConfig(luma_spatial=4.01)
    with pytest.raises(ValidationError):
        VideoDenoiseConfig(
            residual_threshold=0.04,
            full_strength_residual=0.04,
        )
    with pytest.raises(ValidationError):
        SharpenConfig(radius=4)
    with pytest.raises(ValidationError):
        SharpenConfig(unknown_threshold=1.0)  # type: ignore[call-arg]


def test_worsened_objective_side_effects_always_need_review() -> None:
    """Catches a subjective score overruling new clipping, noise, or softness."""
    before = VisualMetrics(
        luma_p10=0.2,
        luma_p50=0.5,
        luma_p90=0.8,
        low_clip_ratio=0.0,
        high_clip_ratio=0.0,
        noise_residual=0.02,
        sharpness=0.08,
    )
    worsening = (
        before.model_copy(update={"high_clip_ratio": 0.04}),
        before.model_copy(update={"noise_residual": 0.08}),
        before.model_copy(update={"sharpness": 0.01}),
    )

    for after in worsening:
        comparison = compare_visual_metrics(before, after, _visual_config())
        assert comparison.status is RescueVerificationStatus.NEEDS_REVIEW
        assert comparison.reasons


def test_side_effect_comparison_records_the_exact_trigger_after_revalidation() -> None:
    """Catches a clipped improved sample being marked passed after comparison."""
    before = VisualMetrics(
        luma_p10=0.2,
        luma_p50=0.5,
        luma_p90=0.8,
        low_clip_ratio=0.0,
        high_clip_ratio=0.0,
        noise_residual=0.02,
        sharpness=0.08,
    )
    after = VisualMetrics(
        luma_p10=0.2,
        luma_p50=0.5,
        luma_p90=0.8,
        low_clip_ratio=0.0,
        high_clip_ratio=0.04,
        noise_residual=0.02,
        sharpness=0.08,
    )

    comparison = compare_visual_metrics(before, after, _visual_config())

    assert comparison.status is RescueVerificationStatus.NEEDS_REVIEW
    assert comparison.reasons == ("high_clip_ratio_increased",)
