from __future__ import annotations

import numpy as np
import pytest

from videoscope.rescue.models import RescueEffectiveConfig, SharpenQualificationProfile
from videoscope.rescue.qualification import (
    apply_qualified_sharpen_profile,
    validate_v15_qualification_inventories,
)
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
    apply_tonal_reduction_to_pcm,
)


def test_v15_track_profile_inventories_are_bounded_ordered_and_independent() -> None:
    rescue_config = RescueEffectiveConfig()
    tonal_config = TonalInterferenceConfig()
    sharpen_profiles = tuple(
        profile.profile_id for profile in rescue_config.sharpen_qualification_profiles
    )
    audio_profiles = tuple(
        f"q_{notch_q:g}_passes_{pass_count}"
        for notch_q in tonal_config.render_qualification_notch_q_values
        for pass_count in tonal_config.render_qualification_notch_pass_counts
    )
    inventories = validate_v15_qualification_inventories(
        {
            "sharpen": sharpen_profiles,
            "denoise_audio": audio_profiles,
            "stabilize": ("transition_anchor_v1",),
        }
    )
    assert tuple(track_id for track_id, _ in inventories) == (
        "sharpen",
        "denoise_audio",
        "stabilize",
    )
    assert all(profile_ids == tuple(profile_ids) for _, profile_ids in inventories)
    assert all(
        len(profile_ids) == len(set(profile_ids)) for _, profile_ids in inventories
    )
    all_profile_ids = [
        profile_id for _, profile_ids in inventories for profile_id in profile_ids
    ]
    assert len(all_profile_ids) == len(set(all_profile_ids))


def test_stabilization_qualification_profile_inventory_is_finite_and_ordered() -> None:
    """Catches a missing, duplicate, or implicit/unbounded estimator axis."""
    rescue_config = RescueEffectiveConfig()

    profile_ids = tuple(
        profile.profile_id
        for profile in rescue_config.stabilization_qualification_profiles
    )

    assert profile_ids == ("transition_anchor_v1",)
    assert len(profile_ids) == len(set(profile_ids))


@pytest.mark.parametrize(
    ("inventories", "message"),
    [
        (
            {
                "sharpen": ("full", "full"),
                "denoise_audio": ("q_18_passes_1",),
                "stabilize": ("transition_anchor_v1",),
            },
            "duplicate",
        ),
        (
            {
                "sharpen": ("full",),
                "denoise_audio": ("full",),
                "stabilize": ("transition_anchor_v1",),
            },
            "overlap",
        ),
        (
            {
                "denoise_audio": ("q_18_passes_1",),
                "sharpen": ("full",),
                "stabilize": ("transition_anchor_v1",),
            },
            "order",
        ),
        (
            {
                "sharpen": ("full",),
                "denoise_audio": ("q_18_passes_1",),
            },
            "order",
        ),
        (
            {
                "sharpen": ("full",),
                "denoise_audio": ("q_18_passes_1",),
                "stabilize": (profile_id for profile_id in ("transition_anchor_v1",)),
            },
            "finite",
        ),
    ],
)
def test_v15_track_inventory_rejects_invalid_provider_contracts(
    inventories: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_v15_qualification_inventories(inventories)  # type: ignore[arg-type]


def test_sharpen_profile_binds_a_bounded_radius_axis() -> None:
    profile = SharpenQualificationProfile(
        profile_id="radius_three",
        cas_strength_scale=0.75,
        unsharp_amount_scale=0.75,
        pass_count=2,
        radius=3,
    )
    parameters = apply_qualified_sharpen_profile(
        {
            "radius": 2,
            "adaptive_strength": 0.32,
            "amount": 1.0,
            "detail_passes": 3,
        },
        profile,
    )
    assert parameters["radius"] == 3
    assert parameters["detail_passes"] == 2


def test_denoise_audio_profile_can_repeat_a_bounded_notch() -> None:
    config = TonalInterferenceConfig(render_qualification_notch_pass_counts=(1, 2))
    tone = InterferenceTone(
        start_seconds=0.5,
        end_seconds=1.5,
        center_frequency_hz=440.0,
        confidence=1.0,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-20.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0,),
        attenuation_target_db=config.attenuation_db,
        render_qualification=TonalRenderQualification(
            boundary_mode="full_interval_v1",
            notch_q=2.0,
            notch_pass_count=2,
            complete_window_count=20,
            minimum_target_reduction_db=24.0,
            maximum_non_target_attenuation_db=0.0,
            maximum_boundary_energy_jump_db=0.0,
            maximum_boundary_crest_jump_db=0.0,
            maximum_boundary_adjacent_delta=0.0,
        ),
    )
    sample_rate = 8_000
    samples: np.ndarray = np.zeros((sample_rate * 2, 1), dtype=np.float64)
    samples[:, 0] = 0.2 * np.sin(
        2.0
        * np.pi
        * tone.center_frequency_hz
        * np.arange(samples.shape[0])
        / sample_rate
    )
    rendered = apply_tonal_reduction_to_pcm(samples, sample_rate, (tone,), config)
    assert rendered.shape == samples.shape
    assert np.isfinite(rendered).all()
