"""Tests for deterministic, bounded CPU visual assessment."""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

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
    filter_fragment_from_action,
    luma_filter_fragment,
    sharpen_filter_fragment,
)
from videoscope.scenes.models import VideoScene


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
    explanation = assessment.public_explanation.lower()
    assert "recover" not in explanation
    assert "restore" not in explanation


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
        ),
    )
    assert "preview-only" in assessment.public_explanation


def test_bounded_filter_fragments_are_exact_and_reject_unvalidated_parameters() -> None:
    """Catches a filter fragment changing silently or accepting action text."""
    assert (
        luma_filter_fragment(LumaAdjustmentConfig())
        == "eq=brightness=0.04:contrast=1.02"
    )
    assert denoise_filter_fragment(VideoDenoiseConfig()) == "hqdn3d=1.5:1:2:1.5"
    assert sharpen_filter_fragment(SharpenConfig()) == "unsharp=5:5:0.4:5:5:0"
    assert (
        filter_fragment_from_action(
            RescueActionKind.ADJUST_LUMA,
            {"brightness": 0.04, "contrast": 1.02},
        )
        == "eq=brightness=0.04:contrast=1.02"
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
        VideoDenoiseConfig(residual_threshold=value)
    with pytest.raises(ValidationError):
        SharpenConfig(amount=value)


def test_visual_configs_are_strict_and_filter_strengths_are_bounded() -> None:
    """Catches silent coercion, unknown keys, or excessive filter strengths."""
    with pytest.raises(ValidationError):
        LumaAdjustmentConfig(brightness="0.04")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        VideoDenoiseConfig(luma_spatial=4.01)
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
