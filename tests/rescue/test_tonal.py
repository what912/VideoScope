from __future__ import annotations

import io
import json
import math
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import ValidationError

import videoscope.rescue.tonal as tonal_module
import videoscope.rescue.verification as verification_module
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueMediaError,
)
from videoscope.rescue.executor import CommandResult, run_external_command
from videoscope.rescue.models import RescueActionKind, RescuePlan
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
    apply_tonal_reduction_to_pcm,
    detect_local_tonal_interference,
    render_tonal_identity_audio,
    render_tonal_interference_reduced_audio,
    validate_plan_tonal_action_contracts,
)
from videoscope.rescue.tonal_metrics import (
    complete_tonal_window_metrics,
    source_relative_tonal_boundary_metrics,
)


def _tone(
    frequency_hz: float,
    *,
    seconds: float,
    sample_rate_hz: int = 48_000,
    amplitude: float = 0.1,
) -> NDArray[np.float64]:
    timeline: NDArray[np.float64] = np.arange(
        round(seconds * sample_rate_hz), dtype=np.float64
    )
    return amplitude * np.sin(2.0 * np.pi * frequency_hz * timeline / sample_rate_hz)


def _local_interference_signal() -> NDArray[np.float64]:
    sample_rate = 48_000
    duration = 3.0
    signal = _tone(220.0, seconds=duration, amplitude=0.08)
    start = round(1.0 * sample_rate)
    end = round(2.0 * sample_rate)
    signal[start:end] += _tone(880.0, seconds=1.0, amplitude=0.16)
    return np.column_stack((signal, signal * 0.9))


def test_detector_selects_only_locally_added_tone() -> None:
    tones = detect_local_tonal_interference(
        _local_interference_signal(), 48_000, TonalInterferenceConfig()
    )

    assert len(tones) == 1
    detected = tones[0]
    assert detected.center_frequency_hz == pytest.approx(880.0, abs=20.0)
    assert detected.start_seconds == pytest.approx(1.0, abs=0.08)
    assert detected.end_seconds == pytest.approx(2.0, abs=0.08)
    assert detected.local_peak_over_baseline_db >= 12.0
    assert detected.attenuation_target_db == 24.0
    assert detected.channel_indices == (0, 1)


def test_persistent_control_tone_is_not_actionable() -> None:
    persistent = np.column_stack((_tone(440.0, seconds=3.0, amplitude=0.2),) * 2)
    assert (
        detect_local_tonal_interference(persistent, 48_000, TonalInterferenceConfig())
        == ()
    )


def test_pcm_filter_attenuates_target_and_preserves_control() -> None:
    sample_rate = 48_000
    signal = _local_interference_signal()
    tone = detect_local_tonal_interference(
        signal, sample_rate, TonalInterferenceConfig()
    )[0]

    rendered = apply_tonal_reduction_to_pcm(
        signal, sample_rate, (tone,), TonalInterferenceConfig()
    )

    def level_db(samples: NDArray[np.float64], frequency: float) -> float:
        timeline = np.arange(samples.shape[0], dtype=np.float64) / sample_rate
        carrier = np.exp(-2j * np.pi * frequency * timeline)
        magnitude = abs(np.sum(samples[:, 0] * carrier)) / samples.shape[0]
        return 20.0 * math.log10(max(float(magnitude), 1e-12))

    target_slice = slice(sample_rate, 2 * sample_rate)
    before_target = level_db(signal[target_slice], 880.0)
    after_target = level_db(rendered[target_slice], 880.0)
    before_control = level_db(signal[target_slice], 220.0)
    after_control = level_db(rendered[target_slice], 220.0)
    assert before_target - after_target >= 18.0
    assert before_control - after_control <= 1.0
    assert np.array_equal(rendered[: sample_rate - 1], signal[: sample_rate - 1])
    assert np.array_equal(
        rendered[2 * sample_rate + 1 :], signal[2 * sample_rate + 1 :]
    )


@pytest.mark.parametrize(
    ("actual_frequency_hz", "measured_frequency_hz"),
    ((880.0, 880.0), (117.84618303542793, 117.84618303542793)),
)
def test_pcm_renderer_has_full_weight_headroom_for_on_and_off_bin_tones(
    actual_frequency_hz: float,
    measured_frequency_hz: float,
) -> None:
    sample_rate = 48_000
    config = TonalInterferenceConfig()
    assert (
        tonal_module._render_center_frequency_hz(measured_frequency_hz, config)
        == measured_frequency_hz
    )
    timeline = np.arange(3 * sample_rate, dtype=np.float64) / sample_rate
    source = 0.08 * np.sin(2.0 * np.pi * 220.0 * timeline)
    event = (timeline >= 1.0) & (timeline < 2.0)
    source[event] += 0.16 * np.sin(2.0 * np.pi * actual_frequency_hz * timeline[event])
    profile = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=measured_frequency_hz,
        confidence=0.95,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-12.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.05,
        channel_indices=(0,),
        attenuation_target_db=config.attenuation_db,
    )

    rendered = apply_tonal_reduction_to_pcm(source, sample_rate, (profile,), config)
    measured = verification_module._independent_tonal_window_metrics(
        source[event],
        rendered[event, 0],
        sample_rate,
        target_frequency_hz=measured_frequency_hz,
        window_seconds=0.05,
        boundary_transition_seconds=config.boundary_transition_seconds,
    )

    assert measured["target_reduction_db"] >= config.attenuation_db
    assert (
        measured["non_target_attenuation_db"]
        <= config.max_non_target_band_attenuation_db
    )


def test_render_qualification_selects_first_profile_passing_every_complete_window() -> (
    None
):
    sample_rate = 48_000
    config = TonalInterferenceConfig()
    timeline = np.arange(3 * sample_rate, dtype=np.float64) / sample_rate
    source = 0.08 * np.sin(2.0 * np.pi * 220.0 * timeline)
    event = (timeline >= 1.0) & (timeline < 2.0)
    source[event] += 0.16 * np.sin(2.0 * np.pi * 880.0 * timeline[event])
    measured = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=880.0,
        confidence=0.95,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-12.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.05,
        channel_indices=(0,),
        attenuation_target_db=config.attenuation_db,
    )

    qualified = tonal_module.qualify_tonal_render_profiles(
        source,
        sample_rate,
        (measured,),
        config,
    )

    assert len(qualified) == 1
    profile = qualified[0]
    assert profile.render_qualification is not None
    assert profile.render_qualification.boundary_mode == "full_interval_v1"
    assert profile.render_qualification.notch_q == 8.0
    assert profile.render_qualification.complete_window_count == 20
    assert (
        profile.render_qualification.minimum_target_reduction_db
        >= config.attenuation_db
    )
    assert (
        profile.render_qualification.maximum_non_target_attenuation_db
        <= config.max_non_target_band_attenuation_db
    )
    rendered = apply_tonal_reduction_to_pcm(
        source,
        sample_rate,
        qualified,
        config,
    )
    all_windows = verification_module._independent_tonal_window_metrics(
        source[event],
        rendered[event, 0],
        sample_rate,
        target_frequency_hz=880.0,
        window_seconds=0.05,
        boundary_transition_seconds=0.0,
    )
    assert all_windows["window_count"] == 20.0
    assert all_windows["excluded_transition_window_count"] == 0.0
    assert all_windows["target_reduction_db"] >= config.attenuation_db


def test_render_qualification_omits_profile_without_one_safe_candidate() -> None:
    sample_rate = 48_000
    config = TonalInterferenceConfig()
    frequency_hz = 117.84618303542793
    timeline = np.arange(3 * sample_rate, dtype=np.float64) / sample_rate
    source = 0.08 * np.sin(2.0 * np.pi * 220.0 * timeline)
    event = (timeline >= 1.0) & (timeline < 2.0)
    source[event] += 0.16 * np.sin(2.0 * np.pi * frequency_hz * timeline[event])
    measured = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=frequency_hz,
        confidence=0.95,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-12.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.05,
        channel_indices=(0,),
        attenuation_target_db=config.attenuation_db,
    )

    assert (
        tonal_module.qualify_tonal_render_profiles(
            source,
            sample_rate,
            (measured,),
            config,
        )
        == ()
    )


def test_complete_window_preservation_reports_single_worst_window() -> None:
    sample_rate = 48_000
    window_size = round(0.05 * sample_rate)
    window_count = 21
    timeline = np.arange(window_count * window_size, dtype=np.float64) / sample_rate
    target = 0.16 * np.sin(2.0 * np.pi * 880.0 * timeline)
    control = 0.08 * np.sin(2.0 * np.pi * 220.0 * timeline)
    source = target + control
    candidate = target * math.pow(10.0, -25.0 / 20.0) + control
    last_window = slice((window_count - 1) * window_size, window_count * window_size)
    candidate[last_window] = (
        target[last_window] * math.pow(10.0, -25.0 / 20.0) + control[last_window] * 0.1
    )

    measured = complete_tonal_window_metrics(
        source,
        candidate,
        sample_rate,
        target_frequency_hz=880.0,
        window_seconds=0.05,
    )

    assert measured["complete_window_count"] == float(window_count)
    assert measured["minimum_target_reduction_db"] >= 24.0
    assert measured["maximum_non_target_attenuation_db"] >= 19.0


def test_streaming_renderer_consumes_the_qualified_mode_and_q(tmp_path: Path) -> None:
    sample_rate = 48_000
    config = TonalInterferenceConfig(stream_block_samples=777)
    timeline = np.arange(3 * sample_rate, dtype=np.float64) / sample_rate
    source = 0.08 * np.sin(2.0 * np.pi * 220.0 * timeline)
    event = (timeline >= 1.0) & (timeline < 2.0)
    source[event] += 0.16 * np.sin(2.0 * np.pi * 880.0 * timeline[event])
    measured = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=880.0,
        confidence=0.95,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-12.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.05,
        channel_indices=(0,),
        attenuation_target_db=config.attenuation_db,
    )
    profiles = tonal_module.qualify_tonal_render_profiles(
        source, sample_rate, (measured,), config
    )
    decoded = tmp_path / "qualified decoded.f32le"
    processed = tmp_path / "qualified processed.f32le"
    source.astype("<f4").tofile(decoded)

    tonal_module._stream_filter_pcm(
        decoded,
        processed,
        sample_rate,
        1,
        profiles,
        config,
        lambda: False,
    )

    streamed = np.fromfile(processed, dtype="<f4").astype(np.float64)
    direct = apply_tonal_reduction_to_pcm(source, sample_rate, profiles, config)[:, 0]
    assert np.allclose(streamed, direct, rtol=0.0, atol=2e-7)


def test_frequency_drift_is_not_actionable() -> None:
    sample_rate = 48_000
    timeline = np.arange(3 * sample_rate, dtype=np.float64) / sample_rate
    phase = 2.0 * np.pi * (700.0 * timeline + 100.0 * timeline**2)
    drift = np.zeros_like(timeline)
    selected = (timeline >= 1.0) & (timeline < 2.0)
    drift[selected] = 0.18 * np.sin(phase[selected])
    signal = _tone(220.0, seconds=3.0, amplitude=0.08) + drift
    assert (
        detect_local_tonal_interference(
            np.column_stack((signal, signal)),
            sample_rate,
            TonalInterferenceConfig(),
        )
        == ()
    )


def test_frequency_deviation_gate_rejects_sub_bin_local_chirp() -> None:
    sample_rate = 48_000
    timeline = np.arange(3 * sample_rate, dtype=np.float64) / sample_rate
    signal = _tone(220.0, seconds=3.0, amplitude=0.08)
    selected = (timeline >= 1.0) & (timeline < 2.0)
    local_time = timeline[selected] - 1.0
    phase = 2.0 * np.pi * (875.0 * local_time + 5.0 * local_time**2)
    signal[selected] += 0.18 * np.sin(phase)

    assert (
        detect_local_tonal_interference(
            np.column_stack((signal, signal)),
            sample_rate,
            TonalInterferenceConfig(maximum_frequency_deviation_hz=1.0),
        )
        == ()
    )
    measured = detect_local_tonal_interference(
        np.column_stack((signal, signal)),
        sample_rate,
        TonalInterferenceConfig(
            maximum_frequency_deviation_hz=20.0,
            minimum_confidence=0.7,
        ),
    )
    assert len(measured) == 1
    assert measured[0].frequency_standard_deviation_hz > 1.0
    assert measured[0].frequency_standard_deviation_hz <= 20.0


def test_harmonically_rich_local_content_is_not_actionable() -> None:
    sample_rate = 48_000
    signal = _tone(220.0, seconds=3.0, amplitude=0.08)
    local = slice(sample_rate, 2 * sample_rate)
    for frequency in (440.0, 660.0, 880.0, 1100.0):
        signal[local] += _tone(frequency, seconds=1.0, amplitude=0.09)
    assert (
        detect_local_tonal_interference(
            np.column_stack((signal, signal)),
            sample_rate,
            TonalInterferenceConfig(maximum_simultaneous_peaks=2),
        )
        == ()
    )


def test_broadband_noise_weak_and_transient_peaks_are_not_actionable() -> None:
    sample_rate = 48_000
    generator = np.random.default_rng(7)
    base = _tone(220.0, seconds=3.0, amplitude=0.08)
    broadband = base.copy()
    broadband[sample_rate : 2 * sample_rate] += generator.normal(0.0, 0.06, sample_rate)
    weak = base.copy()
    weak[sample_rate : 2 * sample_rate] += _tone(880.0, seconds=1.0, amplitude=0.002)
    transient = base.copy()
    transient[sample_rate : sample_rate + 2_000] += _tone(
        880.0, seconds=2_000 / sample_rate, amplitude=0.2
    )
    config = TonalInterferenceConfig()
    for samples in (broadband, weak, transient):
        assert (
            detect_local_tonal_interference(
                np.column_stack((samples, samples)), sample_rate, config
            )
            == ()
        )


def test_missing_bilateral_baseline_fails_closed() -> None:
    sample_rate = 48_000
    signal = _tone(220.0, seconds=1.5, amplitude=0.08)
    signal[:sample_rate] += _tone(880.0, seconds=1.0, amplitude=0.16)
    assert (
        detect_local_tonal_interference(signal, sample_rate, TonalInterferenceConfig())
        == ()
    )


def test_boundaries_have_no_click_and_exact_half_open_processing() -> None:
    sample_rate = 48_000
    signal = _local_interference_signal()
    config = TonalInterferenceConfig()
    tone = detect_local_tonal_interference(signal, sample_rate, config)[0]
    rendered = apply_tonal_reduction_to_pcm(signal, sample_rate, (tone,), config)
    difference = rendered - signal
    start = round(tone.start_seconds * sample_rate)
    end = round(tone.end_seconds * sample_rate)
    transition = round(config.boundary_transition_seconds * sample_rate)
    assert np.array_equal(difference[:start], np.zeros_like(difference[:start]))
    assert np.array_equal(difference[end:], np.zeros_like(difference[end:]))
    window = round(0.05 * sample_rate)
    for boundary in (start, end):
        before = rendered[boundary - window : boundary, 0]
        after = rendered[boundary : boundary + window, 0]
        source_before = signal[boundary - window : boundary, 0]
        source_after = signal[boundary : boundary + window, 0]
        rendered_rms = tuple(
            float(np.sqrt(np.mean(value**2))) for value in (before, after)
        )
        source_rms = tuple(
            float(np.sqrt(np.mean(value**2))) for value in (source_before, source_after)
        )
        rendered_energy_jump_db = abs(
            20.0 * math.log10(rendered_rms[1] / rendered_rms[0])
        )
        source_energy_jump_db = abs(20.0 * math.log10(source_rms[1] / source_rms[0]))
        rendered_crest = tuple(
            float(np.max(np.abs(value))) / rms
            for value, rms in zip((before, after), rendered_rms, strict=True)
        )
        source_crest = tuple(
            float(np.max(np.abs(value))) / rms
            for value, rms in zip(
                (source_before, source_after), source_rms, strict=True
            )
        )
        rendered_crest_jump_db = abs(
            20.0 * math.log10(rendered_crest[1] / rendered_crest[0])
        )
        source_crest_jump_db = abs(20.0 * math.log10(source_crest[1] / source_crest[0]))
        assert (
            rendered_energy_jump_db
            <= source_energy_jump_db + config.max_boundary_energy_jump_db
        )
        assert (
            rendered_crest_jump_db
            <= source_crest_jump_db + config.max_boundary_crest_jump_db
        )
        around_boundary = rendered[boundary - window : boundary + window, 0]
        assert (
            np.max(np.abs(np.diff(around_boundary)))
            < config.max_boundary_adjacent_delta
        )
    assert transition == window


def test_pcm_filter_preserves_speech_like_and_broadband_non_target_energy() -> None:
    sample_rate = 48_000
    config = TonalInterferenceConfig()
    generator = np.random.default_rng(912)
    duration = 3.0
    speech_like = (
        _tone(180.0, seconds=duration, amplitude=0.035)
        + _tone(260.0, seconds=duration, amplitude=0.025)
        + _tone(410.0, seconds=duration, amplitude=0.018)
    )
    broadband = generator.normal(0.0, 0.012, round(duration * sample_rate))
    signal = speech_like + broadband
    local = slice(sample_rate, 2 * sample_rate)
    signal[local] += _tone(880.0, seconds=1.0, amplitude=0.16)
    stereo = np.column_stack((signal, signal * 0.93))
    measured = detect_local_tonal_interference(
        _local_interference_signal(), sample_rate, config
    )[0]

    rendered = apply_tonal_reduction_to_pcm(stereo, sample_rate, (measured,), config)

    source_local = stereo[local, 0]
    rendered_local = rendered[local, 0]
    frequencies = np.fft.rfftfreq(source_local.size, d=1.0 / sample_rate)
    source_power = np.abs(np.fft.rfft(source_local)) ** 2
    rendered_power = np.abs(np.fft.rfft(rendered_local)) ** 2
    non_target = (frequencies < 800.0) | (frequencies > 960.0)
    preservation_db = 10.0 * math.log10(
        float(np.sum(rendered_power[non_target]))
        / float(np.sum(source_power[non_target]))
    )
    assert abs(preservation_db) <= config.max_non_target_band_attenuation_db
    for frequency_hz in (180.0, 260.0, 410.0):
        source_level = _frequency_level_db(
            source_local[:, np.newaxis], frequency_hz, sample_rate
        )
        rendered_level = _frequency_level_db(
            rendered_local[:, np.newaxis], frequency_hz, sample_rate
        )
        assert (
            abs(rendered_level - source_level)
            <= config.max_non_target_band_attenuation_db
        )
    assert np.array_equal(rendered[:sample_rate], stereo[:sample_rate])
    assert np.array_equal(rendered[2 * sample_rate :], stereo[2 * sample_rate :])


def test_pcm_validation_and_tone_order_fail_closed() -> None:
    config = TonalInterferenceConfig()
    with pytest.raises(ValueError):
        detect_local_tonal_interference(np.array([], dtype=float), 48_000, config)
    with pytest.raises(ValueError):
        detect_local_tonal_interference(np.array([0.0, np.nan]), 48_000, config)
    with pytest.raises(ValueError):
        detect_local_tonal_interference(np.zeros(100), 4_000, config)
    with pytest.raises(ValueError):
        detect_local_tonal_interference(np.zeros((100, 3)), 48_000, config)

    signal = _local_interference_signal()
    tone = detect_local_tonal_interference(signal, 48_000, config)[0]
    overlap = tone.model_copy(update={"start_seconds": tone.start_seconds + 0.1})
    with pytest.raises(ValueError):
        apply_tonal_reduction_to_pcm(signal, 48_000, (tone, overlap), config)
    assert np.array_equal(
        apply_tonal_reduction_to_pcm(signal, 48_000, (), config), signal
    )


def test_channel_isolated_tone_changes_only_target_channels() -> None:
    sample_rate = 48_000
    base = _tone(220.0, seconds=3.0, amplitude=0.08)
    local = _tone(880.0, seconds=1.0, amplitude=0.16)
    left = base.copy()
    right = base.copy()
    left[sample_rate : 2 * sample_rate] += local
    right[sample_rate : 2 * sample_rate] -= local
    signal = np.column_stack((left, right))
    config = TonalInterferenceConfig()

    tones = detect_local_tonal_interference(signal, sample_rate, config)
    assert len(tones) == 1
    assert tones[0].channel_indices == (0, 1)

    right_only = tones[0].model_copy(update={"channel_indices": (1,)})
    rendered = apply_tonal_reduction_to_pcm(signal, sample_rate, (right_only,), config)
    assert np.array_equal(rendered[:, 0], signal[:, 0])
    assert not np.array_equal(rendered[:, 1], signal[:, 1])


def test_stereo_tone_with_one_hop_boundary_skew_is_one_renderable_event() -> None:
    sample_rate = 48_000
    base = _tone(220.0, seconds=3.0, amplitude=0.08)
    left = base.copy()
    right = base.copy()
    left[sample_rate : 2 * sample_rate] += _tone(880.0, seconds=1.0, amplitude=0.16)
    right[round(1.05 * sample_rate) : round(1.95 * sample_rate)] += _tone(
        880.0, seconds=0.9, amplitude=0.16
    )
    signal = np.column_stack((left, right))
    config = TonalInterferenceConfig()

    tones = detect_local_tonal_interference(signal, sample_rate, config)

    assert len(tones) == 2
    assert tones[0].channel_indices == (0,)
    assert tones[1].channel_indices == (1,)
    rendered = apply_tonal_reduction_to_pcm(signal, sample_rate, tones, config)
    assert rendered.shape == signal.shape
    assert not np.array_equal(rendered[:, 0], signal[:, 0])
    assert not np.array_equal(rendered[:, 1], signal[:, 1])


def test_pcm_rejects_tone_channel_outside_actual_layout() -> None:
    signal = _local_interference_signal()
    config = TonalInterferenceConfig()
    tone = detect_local_tonal_interference(signal, 48_000, config)[0].model_copy(
        update={"channel_indices": (1,)}
    )
    with pytest.raises(ValueError, match="actual PCM channels"):
        apply_tonal_reduction_to_pcm(signal[:, 0], 48_000, (tone,), config)


@pytest.mark.parametrize("channels", [1, 2])
@pytest.mark.parametrize("sample_rate", [8_000, 48_000, 192_000])
def test_pcm_mono_stereo_and_sample_rate_boundaries_are_deterministic(
    channels: int, sample_rate: int
) -> None:
    seconds = 3.0
    base = _tone(220.0, seconds=seconds, sample_rate_hz=sample_rate, amplitude=0.08)
    start, end = sample_rate, 2 * sample_rate
    base[start:end] += _tone(
        880.0,
        seconds=1.0,
        sample_rate_hz=sample_rate,
        amplitude=0.16,
    )
    signal = base if channels == 1 else np.column_stack((base, base * 0.9))
    config = TonalInterferenceConfig()
    first = detect_local_tonal_interference(signal, sample_rate, config)
    second = detect_local_tonal_interference(signal.copy(), sample_rate, config)
    assert first == second
    assert len(first) == 1
    assert np.array_equal(
        apply_tonal_reduction_to_pcm(signal, sample_rate, first, config),
        apply_tonal_reduction_to_pcm(signal.copy(), sample_rate, second, config),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("window_seconds", float("nan")),
        ("hop_seconds", 0.0),
        ("notch_q", float("inf")),
        ("attenuation_db", 0.0),
    ],
)
def test_config_is_strict_and_finite(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        TonalInterferenceConfig.model_validate({field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_boundary_energy_jump_db", float("nan")),
        ("max_boundary_crest_jump_db", float("inf")),
        ("max_boundary_adjacent_delta", 0.0),
        ("max_non_target_band_attenuation_db", -0.01),
        ("maximum_measurement_windows", 1),
    ],
)
def test_acceptance_and_inventory_config_is_strict(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        TonalInterferenceConfig.model_validate({field: value})


@pytest.mark.parametrize(
    "candidates",
    ((), (8.0, 8.0), (6.0, 8.0), (18.0, 1.0), (101.0, 8.0)),
)
def test_render_qualification_q_inventory_is_strict_and_ordered(
    candidates: tuple[float, ...],
) -> None:
    with pytest.raises(ValidationError, match="strictly descending"):
        TonalInterferenceConfig(render_qualification_notch_q_values=candidates)


def test_render_qualification_model_is_strict_and_path_free() -> None:
    qualification = TonalRenderQualification(
        boundary_mode="full_interval_v1",
        notch_q=8.0,
        complete_window_count=20,
        minimum_target_reduction_db=24.0,
        maximum_non_target_attenuation_db=0.25,
        maximum_boundary_energy_jump_db=0.5,
        maximum_boundary_crest_jump_db=3.0,
        maximum_boundary_adjacent_delta=0.08,
    )
    assert (
        qualification.model_validate_json(qualification.model_dump_json())
        == qualification
    )
    with pytest.raises(ValidationError):
        TonalRenderQualification.model_validate(
            {**qualification.model_dump(mode="python"), "notch_q": "8.0"}
        )
    with pytest.raises(ValidationError):
        TonalRenderQualification.model_validate(
            {**qualification.model_dump(mode="python"), "artifact_path": "hidden"}
        )


def test_final_tonal_action_rejects_raw_pcm_only_qualification() -> None:
    config = TonalInterferenceConfig()
    profile = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=880.0,
        confidence=0.99,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-30.0,
        local_peak_over_baseline_db=30.0,
        persistence_window_count=40,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0, 1),
        attenuation_target_db=24.0,
        render_qualification=TonalRenderQualification(
            boundary_mode="full_interval_v1",
            notch_q=8.0,
            complete_window_count=40,
            minimum_target_reduction_db=25.0,
            maximum_non_target_attenuation_db=0.1,
            maximum_boundary_energy_jump_db=0.0,
            maximum_boundary_crest_jump_db=0.0,
            maximum_boundary_adjacent_delta=0.0,
        ),
    )
    action = SimpleNamespace(
        kind=RescueActionKind.DENOISE_AUDIO,
        source_ranges=((1.0, 2.0),),
        parameters={
            "algorithm_version": "1",
            "config": config.model_dump(mode="json"),
            "interference_profiles": [profile.model_dump(mode="json")],
        },
    )
    plan = SimpleNamespace(
        actions=(action,),
        effective_config=SimpleNamespace(tonal_algorithm_version="1"),
    )

    with pytest.raises(ValueError, match="encoded candidate qualification"):
        validate_plan_tonal_action_contracts(cast(RescuePlan, plan))


def test_renderer_headroom_is_strict_and_cannot_be_disabled() -> None:
    config = TonalInterferenceConfig()
    assert config.render_attenuation_headroom_db >= 3.0
    with pytest.raises(ValidationError):
        TonalInterferenceConfig(render_attenuation_headroom_db=2.999)


def test_acceptance_threshold_boundaries_and_inventory_relationship() -> None:
    config = TonalInterferenceConfig(
        max_boundary_energy_jump_db=0.01,
        max_boundary_crest_jump_db=0.01,
        max_boundary_adjacent_delta=0.000_001,
        max_non_target_band_attenuation_db=0.0,
        minimum_baseline_windows=2,
        minimum_persistence_windows=2,
        local_baseline_seconds=0.1,
        hop_seconds=0.025,
        maximum_measurement_windows=10,
    )
    assert config.maximum_measurement_windows == 10
    with pytest.raises(ValidationError, match="measurement inventory"):
        TonalInterferenceConfig(
            local_baseline_seconds=0.1,
            hop_seconds=0.025,
            minimum_baseline_windows=2,
            maximum_measurement_windows=10,
            minimum_persistence_windows=3,
        )


def test_detector_rejects_measurement_inventory_before_matrix_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_rate = 8_000
    config = TonalInterferenceConfig(
        window_seconds=0.05,
        hop_seconds=0.025,
        local_baseline_seconds=0.1,
        minimum_baseline_windows=2,
        minimum_persistence_windows=2,
        maximum_measurement_windows=10,
    )
    window = round(config.window_seconds * sample_rate)
    hop = round(config.hop_seconds * sample_rate)
    samples = np.ones(window + hop * config.maximum_measurement_windows)
    original_empty = np.empty
    original_zeros = np.zeros

    def guarded_empty(shape: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(shape, tuple):
            assert shape[0] <= config.maximum_measurement_windows
        return original_empty(shape, *args, **kwargs)

    def guarded_zeros(shape: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(shape, tuple):
            assert shape[0] <= config.maximum_measurement_windows
        return original_zeros(shape, *args, **kwargs)

    monkeypatch.setattr(np, "empty", guarded_empty)
    monkeypatch.setattr(np, "zeros", guarded_zeros)

    with pytest.raises(ValueError, match="measurement window inventory"):
        detect_local_tonal_interference(samples, sample_rate, config)


def test_detector_rejects_measurement_inventory_before_full_buffer_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample_rate = 8_000
    config = TonalInterferenceConfig(
        window_seconds=0.05,
        hop_seconds=0.025,
        local_baseline_seconds=0.1,
        minimum_baseline_windows=2,
        minimum_persistence_windows=2,
        maximum_measurement_windows=10,
    )
    window = round(config.window_seconds * sample_rate)
    hop = round(config.hop_seconds * sample_rate)
    samples = np.ones((window + hop * config.maximum_measurement_windows, 1))
    original_asarray = np.asarray

    class RejectFullBufferWork(np.ndarray):
        def astype(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError(
                "measurement budget must precede full-buffer conversion"
            )

        def copy(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("measurement budget must precede full-buffer copy")

        def __array_ufunc__(
            self,
            ufunc: np.ufunc,
            method: Literal[
                "__call__", "reduce", "reduceat", "accumulate", "outer", "at"
            ],
            *inputs: Any,
            **kwargs: Any,
        ) -> Any:
            raise AssertionError("measurement budget must precede full-buffer scan")

    def guarded_asarray(value: Any, *args: Any, **kwargs: Any) -> Any:
        return original_asarray(value, *args, **kwargs).view(RejectFullBufferWork)

    monkeypatch.setattr(np, "asarray", guarded_asarray)

    with pytest.raises(ValueError, match="measurement window inventory"):
        detect_local_tonal_interference(samples, sample_rate, config)


def test_detector_accepts_measurement_inventory_exactly_at_limit() -> None:
    sample_rate = 8_000
    config = TonalInterferenceConfig(
        window_seconds=0.05,
        hop_seconds=0.025,
        local_baseline_seconds=0.1,
        minimum_baseline_windows=2,
        minimum_persistence_windows=2,
        maximum_measurement_windows=10,
    )
    window = round(config.window_seconds * sample_rate)
    hop = round(config.hop_seconds * sample_rate)
    samples = np.zeros(window + hop * (config.maximum_measurement_windows - 1))

    assert detect_local_tonal_interference(samples, sample_rate, config) == ()


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        TonalInterferenceConfig.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):
        InterferenceTone.model_validate(
            {
                "start_seconds": 1.0,
                "end_seconds": 2.0,
                "center_frequency_hz": 880.0,
                "confidence": 0.9,
                "baseline_before_dbfs": -80.0,
                "baseline_after_dbfs": -80.0,
                "peak_dbfs": -20.0,
                "local_peak_over_baseline_db": 60.0,
                "attenuation_target_db": 24.0,
                "algorithm_version": "tonal-interference-v1",
                "channel_indices": (0,),
                "unknown": 1,
            }
        )


def _renderer_probe() -> dict[str, object]:
    return {
        "format": {"duration": "3.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
            },
        ],
    }


@pytest.mark.parametrize(
    "first_stdout",
    (
        '{"streams":[',
        json.dumps({"streams": []}),
    ),
)
def test_probe_audio_retries_once_after_zero_exit_unusable_payload(
    first_stdout: str,
) -> None:
    expected = _renderer_probe()
    attempts = 0

    def runner(arguments: tuple[str, ...]) -> CommandResult:
        nonlocal attempts
        attempts += 1
        stdout = first_stdout if attempts == 1 else json.dumps(expected)
        return CommandResult(0, "", stdout)

    payload = tonal_module._probe_audio(
        Path("private candidate.mp4"), Path("ffprobe"), runner
    )

    assert payload == expected
    assert attempts == 2


def test_probe_audio_fails_closed_after_two_invalid_json_results() -> None:
    attempts = 0
    private_marker = "C:/x.mp4"
    malformed_stdout = '{"format":{"filename":"C:/x.mp4"}'

    def runner(arguments: tuple[str, ...]) -> CommandResult:
        nonlocal attempts
        attempts += 1
        return CommandResult(0, "", malformed_stdout)

    with pytest.raises(RescueMediaError) as caught:
        tonal_module._probe_audio(
            Path("private candidate.mp4"), Path("ffprobe"), runner
        )

    assert attempts == 2
    assert caught.value.internal_message == (
        "tonal media probe returned invalid JSON after 2 attempts (line 1, column 34)"
    )
    assert private_marker not in caught.value.internal_message
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_probe_audio_fails_closed_after_two_incomplete_payloads() -> None:
    attempts = 0

    def runner(arguments: tuple[str, ...]) -> CommandResult:
        nonlocal attempts
        attempts += 1
        return CommandResult(0, "", json.dumps({"streams": []}))

    with pytest.raises(RescueMediaError) as caught:
        tonal_module._probe_audio(
            Path("private candidate.mp4"), Path("ffprobe"), runner
        )

    assert attempts == 2
    assert caught.value.internal_message == (
        "tonal media probe returned incomplete data after 2 attempts"
    )


def test_probe_audio_retries_once_after_zero_exit_non_text_stdout() -> None:
    expected = _renderer_probe()
    attempts = 0

    def runner(arguments: tuple[str, ...]) -> CommandResult:
        nonlocal attempts
        attempts += 1
        stdout: Any = None if attempts == 1 else json.dumps(expected)
        return CommandResult(0, "", stdout)

    payload = tonal_module._probe_audio(
        Path("private candidate.mp4"), Path("ffprobe"), runner
    )

    assert payload == expected
    assert attempts == 2


def test_probe_audio_fails_closed_after_two_non_text_stdout_results() -> None:
    attempts = 0

    def runner(arguments: tuple[str, ...]) -> CommandResult:
        nonlocal attempts
        attempts += 1
        stdout: Any = None
        return CommandResult(0, "", stdout)

    with pytest.raises(RescueMediaError) as caught:
        tonal_module._probe_audio(
            Path("private candidate.mp4"), Path("ffprobe"), runner
        )

    assert attempts == 2
    assert caught.value.internal_message == (
        "tonal media probe returned non-text JSON after 2 attempts"
    )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_probe_audio_does_not_retry_nonzero_result() -> None:
    attempts = 0

    def runner(arguments: tuple[str, ...]) -> CommandResult:
        nonlocal attempts
        attempts += 1
        return CommandResult(9, "private diagnostic", "{")

    with pytest.raises(RescueMediaError) as caught:
        tonal_module._probe_audio(Path("candidate.mp4"), Path("ffprobe"), runner)

    assert attempts == 1
    assert caught.value.internal_message == "tonal media probe failed"


def test_renderer_uses_argv_streams_blocks_and_publishes_no_clobber(
    tmp_path: Path,
) -> None:
    source = tmp_path / "中文 source.mp4"
    output = tmp_path / "fixed output.mp4"
    source.write_bytes(b"source")
    signal = _local_interference_signal().astype("<f4")
    tone = detect_local_tonal_interference(
        signal.astype(np.float64), 48_000, TonalInterferenceConfig()
    )[0]
    commands: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(arguments)
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        if "pcm_f32le" in arguments and arguments[-1] != "-":
            Path(arguments[-1]).write_bytes(signal.tobytes())
        elif "null" not in arguments:
            Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    render_tonal_interference_reduced_audio(
        source,
        output,
        (tone,),
        TonalInterferenceConfig(stream_block_samples=1024),
        ffmpeg_path=Path("ffmpeg"),
        ffprobe_path=Path("ffprobe"),
        runner=runner,
    )

    assert output.read_bytes() == b"candidate"
    assert source.read_bytes() == b"source"
    assert all(isinstance(command, tuple) for command in commands)
    assert not any("tonal" in path.name for path in tmp_path.iterdir())


def test_renderer_never_overwrites_existing_destination(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    output.write_bytes(b"keep")
    with pytest.raises(RescueArtifactError):
        render_tonal_interference_reduced_audio(
            source,
            output,
            (),
            TonalInterferenceConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=lambda *_args, **_kwargs: CommandResult(0, "", ""),
        )
    assert output.read_bytes() == b"keep"


def test_renderer_cancellation_and_callback_failure_clean_owned_partials(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    for callback, expected in (
        (lambda: True, RescueCancelledError),
        (lambda: (_ for _ in ()).throw(RuntimeError("private path")), RuntimeError),
    ):
        output = tmp_path / f"{expected.__name__}.mp4"
        with pytest.raises(expected):
            render_tonal_interference_reduced_audio(
                source,
                output,
                (),
                TonalInterferenceConfig(),
                ffmpeg_path=Path("ffmpeg"),
                ffprobe_path=Path("ffprobe"),
                runner=lambda *_args, **_kwargs: CommandResult(0, "", ""),
                cancellation_callback=callback,
            )
        assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_renderer_rethrows_callback_failure_during_pcm_streaming(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    signal = _local_interference_signal().astype("<f4")
    config = TonalInterferenceConfig(stream_block_samples=128)
    tone = detect_local_tonal_interference(signal.astype(np.float64), 48_000, config)[0]
    callback_calls = 0

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls >= 4:
            raise RuntimeError("application callback failed")
        return False

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        if "pcm_f32le" in arguments:
            Path(arguments[-1]).write_bytes(signal.tobytes())
        return CommandResult(0, "", "")

    with pytest.raises(RuntimeError, match="application callback failed"):
        render_tonal_interference_reduced_audio(
            source,
            output,
            (tone,),
            config,
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=runner,
            cancellation_callback=callback,
        )
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_identity_renderer_checks_cancellation_during_pcm_copy(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "identity.mp4"
    source.write_bytes(b"source")
    signal: NDArray[np.float32] = np.zeros((4096, 2), dtype="<f4")
    callback_calls = 0
    commands: list[tuple[str, ...]] = []

    def callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        return callback_calls >= 2

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(arguments)
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        if "pcm_f32le" in arguments:
            Path(arguments[-1]).write_bytes(signal.tobytes())
        elif "null" not in arguments:
            Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    with pytest.raises(RescueCancelledError):
        render_tonal_identity_audio(
            source,
            output,
            TonalInterferenceConfig(stream_block_samples=128),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=runner,
            cancellation_callback=callback,
        )

    assert not output.exists()
    assert not any("-b:a" in command for command in commands)
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_renderer_rejects_tone_channel_outside_actual_mono_layout(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    tone = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=880.0,
        confidence=0.9,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-20.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=8,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(1,),
        attenuation_target_db=24.0,
    )
    mono_probe = {
        "format": {"duration": "3.0"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 1,
                "channel_layout": "mono",
            },
        ],
    }

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(mono_probe))
        raise AssertionError("renderer must reject before decode")

    commands: list[tuple[str, ...]] = []

    def tracked_runner(arguments: tuple[str, ...], **kwargs: object) -> CommandResult:
        commands.append(arguments)
        return runner(arguments, **kwargs)

    with pytest.raises(RescueMediaError):
        render_tonal_interference_reduced_audio(
            source,
            output,
            (tone,),
            TonalInterferenceConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=tracked_runner,
        )
    assert len(commands) == 1
    assert not output.exists()


def test_renderer_accepts_one_ulp_tail_at_actual_noninteger_pcm_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample_rate_hz = 48_000
    sample_count = 160_001
    actual_end = sample_count / sample_rate_hz
    requested_end = math.nextafter(actual_end, math.inf)
    config = TonalInterferenceConfig()
    tone = InterferenceTone(
        start_seconds=0.25,
        end_seconds=requested_end,
        center_frequency_hz=880.0,
        confidence=0.9,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-20.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=8,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0, 1),
        attenuation_target_db=config.attenuation_db,
    )
    probe = {
        "format": {"duration": f"{requested_end:.6f}"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": str(sample_rate_hz),
                "channels": 2,
                "channel_layout": "stereo",
            },
        ],
    }
    captured: list[tuple[InterferenceTone, ...]] = []

    def capture_stream(
        decoded: Path,
        processed: Path,
        _sample_rate_hz: int,
        _channel_count: int,
        tones: tuple[InterferenceTone, ...],
        _config: TonalInterferenceConfig,
        _cancellation_callback: Any,
    ) -> None:
        captured.append(tones)
        processed.write_bytes(decoded.read_bytes())

    monkeypatch.setattr(tonal_module, "_stream_filter_pcm", capture_stream)

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(probe))
        if "pcm_f32le" in arguments:
            Path(arguments[-1]).write_bytes(b"\0" * (sample_count * 2 * 4))
        elif "null" not in arguments:
            Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    for index in range(2):
        render_tonal_interference_reduced_audio(
            source,
            tmp_path / f"output-{index}.mp4",
            (tone,),
            config,
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=runner,
        )

    assert len(captured) == 2
    assert captured[0] == captured[1]
    assert captured[0][0].start_seconds == 0.25
    assert captured[0][0].end_seconds == requested_end


def _inventory_tone(
    start_seconds: float,
    end_seconds: float,
    config: TonalInterferenceConfig,
) -> InterferenceTone:
    return InterferenceTone(
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        center_frequency_hz=880.0,
        confidence=0.9,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-20.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=8,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0, 1),
        attenuation_target_db=config.attenuation_db,
    )


def test_half_open_sample_inventory_uses_explicit_ceil_with_one_ulp_normalization() -> (
    None
):
    sample_rate_hz = 48_000
    r5_end = 3.333333666666661
    noninteger_end = 160_001 / sample_rate_hz

    assert (
        tonal_module._required_exclusive_sample_count(r5_end, sample_rate_hz) == 160_001
    )
    assert (
        tonal_module._required_exclusive_sample_count(noninteger_end, sample_rate_hz)
        == 160_001
    )
    assert (
        tonal_module._required_exclusive_sample_count(
            math.nextafter(noninteger_end, math.inf), sample_rate_hz
        )
        == 160_001
    )


@pytest.mark.parametrize(
    "timestamp_seconds",
    (-1.0, math.nan, math.inf, -math.inf, float.fromhex("0x1.fffffffffffffp1023")),
)
def test_half_open_sample_inventory_rejects_invalid_or_overflowing_timestamp(
    timestamp_seconds: float,
) -> None:
    with pytest.raises(ValueError):
        tonal_module._required_exclusive_sample_count(timestamp_seconds, 48_000)


def test_strict_decoded_inventory_rejects_one_sample_shortage_and_empty_interval() -> (
    None
):
    config = TonalInterferenceConfig()
    exact_end = 160_001 / 48_000
    tone = _inventory_tone(0.25, exact_end, config)

    with pytest.raises(ValueError, match="tone interval exceeds PCM duration"):
        tonal_module._validated_tones((tone,), 48_000, 160_000, 2, config)

    sub_sample = _inventory_tone(0.000001, 0.000002, config)
    with pytest.raises(ValueError, match="no PCM sample"):
        tonal_module._validated_tones((sub_sample,), 48_000, 1, 2, config)


def test_decoded_pcm_inventory_requires_complete_multichannel_float_frames(
    tmp_path: Path,
) -> None:
    exact = tmp_path / "exact.f32le"
    exact.write_bytes(b"\0" * (3 * 2 * 4))
    assert tonal_module._decoded_pcm_sample_count(exact, 2) == 3

    for name, byte_count in (("partial-float", 3), ("partial-stereo-frame", 4)):
        malformed = tmp_path / f"{name}.f32le"
        malformed.write_bytes(b"\0" * byte_count)
        with pytest.raises(RescueMediaError, match="could not be processed"):
            tonal_module._decoded_pcm_sample_count(malformed, 2)


def test_renderer_rejects_tail_beyond_explicit_tolerance_and_cleans(
    tmp_path: Path,
) -> None:
    config = TonalInterferenceConfig(duration_tolerance_seconds=0.05)
    tone = InterferenceTone(
        start_seconds=0.25,
        end_seconds=3.051,
        center_frequency_hz=880.0,
        confidence=0.9,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-20.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=8,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0, 1),
        attenuation_target_db=config.attenuation_db,
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        raise AssertionError("out-of-tolerance tone must fail before decode")

    with pytest.raises(RescueMediaError):
        render_tonal_interference_reduced_audio(
            source,
            tmp_path / "output.mp4",
            (tone,),
            config,
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=runner,
        )

    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_renderer_wraps_decoded_inventory_mismatch_and_cleans(
    tmp_path: Path,
) -> None:
    config = TonalInterferenceConfig(duration_tolerance_seconds=0.05)
    tone = InterferenceTone(
        start_seconds=0.25,
        end_seconds=3.0,
        center_frequency_hz=880.0,
        confidence=0.9,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-20.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=8,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0, 1),
        attenuation_target_db=config.attenuation_db,
    )
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    published = False

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal published
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        if "pcm_f32le" in arguments:
            sample_count = round(2.96 * 48_000)
            Path(arguments[-1]).write_bytes(b"\0" * (sample_count * 2 * 4))
        elif "null" not in arguments:
            published = True
            Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError) as caught:
        render_tonal_interference_reduced_audio(
            source,
            output,
            (tone,),
            config,
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=runner,
        )

    assert caught.value.internal_message == (
        "tonal measurements do not match decoded audio"
    )
    assert not published
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


@pytest.mark.parametrize("failure", ["probe", "decode", "mux", "verify"])
def test_renderer_media_failures_remove_every_partial(
    tmp_path: Path, failure: str
) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    signal = _local_interference_signal().astype("<f4")
    tone = detect_local_tonal_interference(
        signal.astype(np.float64), 48_000, TonalInterferenceConfig()
    )[0]
    probe_count = 0

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal probe_count
        if arguments[0] == "ffprobe":
            probe_count += 1
            if failure == "probe" and probe_count == 1:
                return CommandResult(1, "failed", "")
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        if "pcm_f32le" in arguments:
            if failure != "decode":
                Path(arguments[-1]).write_bytes(signal.tobytes())
            return CommandResult(1 if failure == "decode" else 0, "", "")
        if "null" in arguments:
            return CommandResult(1 if failure == "verify" else 0, "", "")
        if failure != "mux":
            Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(1 if failure == "mux" else 0, "", "")

    with pytest.raises(RescueMediaError):
        render_tonal_interference_reduced_audio(
            source,
            output,
            (tone,),
            TonalInterferenceConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=runner,
        )
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


@pytest.mark.parametrize("exception", [FileNotFoundError(), TimeoutError()])
def test_renderer_missing_tool_or_timeout_is_sanitized_and_cleans(
    tmp_path: Path, exception: Exception
) -> None:
    source = tmp_path / "private 中文 source.mp4"
    output = tmp_path / "private output.mp4"
    source.write_bytes(b"source")

    def failed_runner(*_args: object, **_kwargs: object) -> CommandResult:
        raise exception

    with pytest.raises(RescueMediaError) as caught:
        render_tonal_interference_reduced_audio(
            source,
            output,
            (),
            TonalInterferenceConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=failed_runner,
        )
    assert str(source) not in str(caught.value)
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_renderer_rejects_truncated_pcm_and_destination_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    signal = _local_interference_signal().astype("<f4")
    tone = detect_local_tonal_interference(
        signal.astype(np.float64), 48_000, TonalInterferenceConfig()
    )[0]

    def truncated(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        if "pcm_f32le" in arguments:
            Path(arguments[-1]).write_bytes(b"bad")
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError):
        render_tonal_interference_reduced_audio(
            source,
            tmp_path / "truncated.mp4",
            (tone,),
            TonalInterferenceConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=truncated,
        )

    output = tmp_path / "race.mp4"
    original_link = __import__("os").link

    def race_link(candidate: Path, destination: Path) -> None:
        Path(destination).write_bytes(b"racer")
        original_link(candidate, destination)

    monkeypatch.setattr("videoscope.rescue.tonal.os.link", race_link)

    def success(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        if "pcm_f32le" in arguments:
            Path(arguments[-1]).write_bytes(signal.tobytes())
        elif "null" not in arguments:
            Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    with pytest.raises(RescueArtifactError):
        render_tonal_interference_reduced_audio(
            source,
            output,
            (tone,),
            TonalInterferenceConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=success,
        )
    assert output.read_bytes() == b"racer"
    assert not any("tonal" in item.name for item in tmp_path.iterdir())


def test_streaming_filter_read_size_is_bounded_independent_of_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    output = tmp_path / "output.mp4"
    source.write_bytes(b"source")
    signal = _local_interference_signal().astype("<f4")
    config = TonalInterferenceConfig(stream_block_samples=257)
    tone = detect_local_tonal_interference(signal.astype(np.float64), 48_000, config)[0]
    observed_reads: list[int] = []
    original_open = io.open

    class ReadProbe:
        def __init__(self, wrapped: object) -> None:
            self.wrapped = wrapped

        def __enter__(self) -> ReadProbe:
            self.wrapped.__enter__()  # type: ignore[attr-defined]
            return self

        def __exit__(self, *args: object) -> object:
            return self.wrapped.__exit__(*args)  # type: ignore[attr-defined]

        def read(self, size: int = -1) -> bytes:
            observed_reads.append(size)
            return self.wrapped.read(size)  # type: ignore[attr-defined,no-any-return]

        def __getattr__(self, name: str) -> object:
            return getattr(self.wrapped, name)

    def probing_open(path: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        opened = original_open(path, mode, *args, **kwargs)
        if str(path).endswith("decoded.f32le") and "rb" in mode:
            return ReadProbe(opened)
        return opened

    monkeypatch.setattr(io, "open", probing_open)

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", json.dumps(_renderer_probe()))
        if "pcm_f32le" in arguments:
            Path(arguments[-1]).write_bytes(signal.tobytes())
        elif "null" not in arguments:
            Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    render_tonal_interference_reduced_audio(
        source,
        output,
        (tone,),
        config,
        ffmpeg_path=Path("ffmpeg"),
        ffprobe_path=Path("ffprobe"),
        runner=runner,
    )
    assert observed_reads
    assert set(observed_reads) == {257 * 2 * 4}


def _local_audio_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        local_bin = (
            Path.home()
            / "AppData/Local/VideoScope/tools/ffmpeg-8.1.2"
            / "ffmpeg-8.1.2-essentials_build/bin"
        )
        local_ffmpeg = local_bin / "ffmpeg.exe"
        local_ffprobe = local_bin / "ffprobe.exe"
        if local_ffmpeg.is_file() and local_ffprobe.is_file():
            ffmpeg, ffprobe = str(local_ffmpeg), str(local_ffprobe)
    if ffmpeg is None or ffprobe is None:
        pytest.fail("existing local FFmpeg 8.1.2 tools are required")
    assert ffmpeg is not None and ffprobe is not None
    return ffmpeg, ffprobe


def _run_media(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        arguments, shell=False, check=True, capture_output=True, timeout=60
    )


def _decode_native_pcm(path: Path, ffmpeg: str) -> NDArray[np.float64]:
    raw = _run_media(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-",
        ]
    ).stdout
    return np.frombuffer(raw, dtype="<f4").reshape((-1, 2)).astype(np.float64)


def _frequency_level_db(
    samples: NDArray[np.float64], frequency_hz: float, sample_rate_hz: int
) -> float:
    timeline = np.arange(samples.shape[0], dtype=np.float64) / sample_rate_hz
    carrier = np.exp(-2j * np.pi * frequency_hz * timeline)
    magnitude = abs(np.sum(samples[:, 0] * carrier)) / samples.shape[0]
    return 20.0 * math.log10(max(float(magnitude), 1e-12))


def test_native_ffmpeg_8_1_2_renderer_executes_and_attenuates_tone(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = _local_audio_tools()
    version = _run_media([ffmpeg, "-version"]).stdout.decode("utf-8", "replace")
    assert "ffmpeg version 8.1.2" in version
    source = tmp_path / "中文 native source.mp4"
    output = tmp_path / "中文 native output.mp4"
    _run_media(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x48:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.08*sin(2*PI*220*t)+if(between(t\\,1\\,2)\\,0.16*sin(2*PI*880*t)\\,0):s=48000:d=3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-shortest",
            str(source),
        ]
    )
    decoded = _decode_native_pcm(source, ffmpeg)
    tone = detect_local_tonal_interference(decoded, 48_000, TonalInterferenceConfig())[
        0
    ]
    render_tonal_interference_reduced_audio(
        source,
        output,
        (tone,),
        TonalInterferenceConfig(),
        ffmpeg_path=Path(ffmpeg),
        ffprobe_path=Path(ffprobe),
        runner=run_external_command,
    )
    assert source.exists() and output.exists()
    rendered = _decode_native_pcm(output, ffmpeg)
    start = 48_000
    end = min(2 * 48_000, decoded.shape[0], rendered.shape[0])
    source_target = _frequency_level_db(decoded[start:end], 880.0, 48_000)
    output_target = _frequency_level_db(rendered[start:end], 880.0, 48_000)
    source_control = _frequency_level_db(decoded[start:end], 220.0, 48_000)
    output_control = _frequency_level_db(rendered[start:end], 220.0, 48_000)
    assert source_target - output_target >= 18.0
    assert source_control - output_control <= 1.0
    probe = json.loads(
        _run_media(
            [
                ffprobe,
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(output),
            ]
        ).stdout
    )
    audio = next(item for item in probe["streams"] if item["codec_type"] == "audio")
    assert audio["sample_rate"] == "48000"
    assert audio["channels"] == 2
    assert audio["channel_layout"] == "stereo"
    assert float(probe["format"]["duration"]) == pytest.approx(3.0, abs=0.05)


def test_native_aac_qualified_renderer_meets_every_complete_window_and_boundary(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = _local_audio_tools()
    actual_frequency_hz = 880.0
    measured_frequency_hz = 880.0
    source = tmp_path / "qualified native source.mp4"
    output = tmp_path / "qualified native output.mp4"
    _run_media(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x48:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.08*sin(2*PI*220*t)+if(between(t\\,1\\,2)\\,"
            f"0.16*sin(2*PI*{actual_frequency_hz}*t)\\,0):s=48000:d=3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-shortest",
            str(source),
        ]
    )
    config = TonalInterferenceConfig()
    profile = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=measured_frequency_hz,
        confidence=0.95,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-12.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.05,
        channel_indices=(0, 1),
        attenuation_target_db=config.attenuation_db,
    )
    decoded_source = _decode_native_pcm(source, ffmpeg)
    qualified = tonal_module.qualify_tonal_render_profiles(
        decoded_source,
        48_000,
        (profile,),
        config,
    )
    assert len(qualified) == 1
    selected = qualified[0]
    assert selected.render_qualification is not None
    render_tonal_interference_reduced_audio(
        source,
        output,
        qualified,
        config,
        ffmpeg_path=Path(ffmpeg),
        ffprobe_path=Path(ffprobe),
        runner=run_external_command,
    )
    decoded_output = _decode_native_pcm(output, ffmpeg)
    start = 48_000
    end = min(2 * 48_000, decoded_source.shape[0], decoded_output.shape[0])
    measured = verification_module._independent_tonal_window_metrics(
        decoded_source[start:end, 0],
        decoded_output[start:end, 0],
        48_000,
        target_frequency_hz=measured_frequency_hz,
        window_seconds=0.05,
        boundary_transition_seconds=0.0,
    )
    assert measured["window_count"] == 20.0
    assert measured["excluded_transition_window_count"] == 0.0
    assert measured["target_reduction_db"] >= config.attenuation_db
    assert (
        measured["non_target_attenuation_db"]
        <= config.max_non_target_band_attenuation_db
    )
    window_size = round(0.05 * 48_000)
    boundaries: tuple[tuple[float, Literal["start", "end"]], ...] = (
        (selected.start_seconds, "start"),
        (selected.end_seconds, "end"),
    )
    for boundary_seconds, boundary_side in boundaries:
        boundary = round(boundary_seconds * 48_000)
        source_pair = decoded_source[boundary - window_size : boundary + window_size, 0]
        candidate_pair = decoded_output[
            boundary - window_size : boundary + window_size, 0
        ]
        boundary_metrics = source_relative_tonal_boundary_metrics(
            source_pair,
            candidate_pair,
            window_size,
            window_size,
            48_000,
            selected.center_frequency_hz,
            boundary_side=boundary_side,
            boundary_mode=selected.render_qualification.boundary_mode,
            boundary_transition_seconds=config.boundary_transition_seconds,
            derivative_numerical_floor=config.max_boundary_adjacent_delta,
        )
        assert boundary_metrics["energy_jump_db"] <= config.max_boundary_energy_jump_db
        assert boundary_metrics["crest_jump_db"] <= config.max_boundary_crest_jump_db
        assert boundary_metrics["adjacent_delta"] <= config.max_boundary_adjacent_delta


@pytest.mark.parametrize("frequency_hz", (117.84618303542793, 880.0))
def test_native_aac_smooth_tonal_boundaries_do_not_report_transients(
    tmp_path: Path,
    frequency_hz: float,
) -> None:
    ffmpeg, ffprobe = _local_audio_tools()
    source = tmp_path / f"smooth boundary source {frequency_hz}.mp4"
    output = tmp_path / f"smooth boundary output {frequency_hz}.mp4"
    _run_media(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x48:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc=0.08*sin(2*PI*220*t)+0.7*sin(2*PI*{frequency_hz}*t):s=48000:d=3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-shortest",
            str(source),
        ]
    )
    config = TonalInterferenceConfig()
    profile = InterferenceTone(
        start_seconds=1.0,
        end_seconds=2.0,
        center_frequency_hz=frequency_hz,
        confidence=0.95,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-3.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.05,
        channel_indices=(0, 1),
        attenuation_target_db=config.attenuation_db,
    )
    render_tonal_interference_reduced_audio(
        source,
        output,
        (profile,),
        config,
        ffmpeg_path=Path(ffmpeg),
        ffprobe_path=Path(ffprobe),
        runner=run_external_command,
    )
    decoded_source = _decode_native_pcm(source, ffmpeg)[:, 0]
    decoded_output = _decode_native_pcm(output, ffmpeg)[:, 0]
    window_size = round(0.05 * 48_000)
    boundaries: tuple[tuple[float, Literal["start", "end"]], ...] = (
        (profile.start_seconds, "start"),
        (profile.end_seconds, "end"),
    )
    for boundary_seconds, boundary_side in boundaries:
        boundary = round(boundary_seconds * 48_000)
        source_pair = decoded_source[boundary - window_size : boundary + window_size]
        output_pair = decoded_output[boundary - window_size : boundary + window_size]
        measured = verification_module._source_relative_tonal_boundary_metrics(
            source_pair,
            output_pair,
            window_size,
            window_size,
            48_000,
            profile.center_frequency_hz,
            boundary_side=boundary_side,
            boundary_transition_seconds=config.boundary_transition_seconds,
            derivative_numerical_floor=config.max_boundary_adjacent_delta,
        )
        assert measured["energy_jump_db"] <= config.max_boundary_energy_jump_db
        assert measured["crest_jump_db"] <= config.max_boundary_crest_jump_db
        assert measured["adjacent_delta"] <= config.max_boundary_adjacent_delta


def test_native_renderer_repeated_outputs_have_identical_decoded_pcm(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = _local_audio_tools()
    source = tmp_path / "repeat source.mp4"
    first = tmp_path / "repeat output 1.mp4"
    second = tmp_path / "repeat output 2.mp4"
    _run_media(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x48:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.08*sin(2*PI*220*t)+if(between(t\\,1\\,2)\\,0.16*sin(2*PI*880*t)\\,0):s=48000:d=3",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ac",
            "2",
            "-shortest",
            str(source),
        ]
    )
    decoded = _decode_native_pcm(source, ffmpeg)
    config = TonalInterferenceConfig()
    tone = detect_local_tonal_interference(decoded, 48_000, config)[0]
    for output in (first, second):
        render_tonal_interference_reduced_audio(
            source,
            output,
            (tone,),
            config,
            ffmpeg_path=Path(ffmpeg),
            ffprobe_path=Path(ffprobe),
            runner=run_external_command,
        )
    assert np.array_equal(
        _decode_native_pcm(first, ffmpeg), _decode_native_pcm(second, ffmpeg)
    )
