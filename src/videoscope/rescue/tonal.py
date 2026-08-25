"""CPU-only local narrowband-interference measurement and restoration."""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, cast

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueInputError,
    RescueMediaError,
)
from videoscope.rescue.tonal_metrics import (
    complete_tonal_window_metrics,
    source_relative_tonal_boundary_metrics,
)

if TYPE_CHECKING:
    from videoscope.rescue.executor import CommandResult, ExternalCommandRunner
    from videoscope.rescue.models import RescuePlan

_ALGORITHM_VERSION: Final = "tonal-interference-v1"
_NOTCH_SETTLING_SAFETY_DB: Final = 3.0
_RENDER_QUALIFICATION_WINDOW_SECONDS: Final = 0.05
_TONAL_PROBE_ATTEMPTS: Final = 2


class _TonalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TonalInterferenceConfig(_TonalModel):
    """Complete measurement and restoration bounds for local tonal noise."""

    algorithm_version: Literal["tonal-interference-v1"] = _ALGORITHM_VERSION
    window_seconds: float = Field(default=0.05, gt=0, le=0.2, allow_inf_nan=False)
    hop_seconds: float = Field(default=0.025, gt=0, le=0.1, allow_inf_nan=False)
    local_baseline_seconds: float = Field(
        default=0.5, gt=0, le=5.0, allow_inf_nan=False
    )
    minimum_baseline_windows: int = Field(default=6, ge=2, le=1000)
    minimum_persistence_windows: int = Field(default=8, ge=2, le=1000)
    minimum_frequency_hz: float = Field(
        default=80.0, gt=0, le=4000, allow_inf_nan=False
    )
    maximum_frequency_hz: float = Field(
        default=8000.0, gt=100, le=24000, allow_inf_nan=False
    )
    minimum_peak_dbfs: float = Field(default=-35.0, ge=-120, le=0, allow_inf_nan=False)
    minimum_peak_prominence_db: float = Field(
        default=14.0, gt=0, le=80, allow_inf_nan=False
    )
    minimum_local_gain_db: float = Field(default=12.0, gt=0, le=80, allow_inf_nan=False)
    maximum_frequency_deviation_hz: float = Field(
        default=25.0, gt=0, le=500, allow_inf_nan=False
    )
    maximum_simultaneous_peaks: int = Field(default=2, ge=1, le=16)
    minimum_confidence: float = Field(default=0.75, ge=0, le=1, allow_inf_nan=False)
    confidence_gain_span_db: float = Field(
        default=18.0, gt=0, le=80, allow_inf_nan=False
    )
    attenuation_db: float = Field(default=24.0, gt=0, le=60, allow_inf_nan=False)
    render_attenuation_headroom_db: float = Field(
        default=3.0, ge=3.0, le=12.0, allow_inf_nan=False
    )
    notch_q: float = Field(default=18.0, gt=1, le=100, allow_inf_nan=False)
    render_qualification_notch_q_values: tuple[float, ...] = (
        18.0,
        12.0,
        8.0,
        6.0,
        5.0,
        4.0,
        3.0,
        2.0,
    )
    render_qualification_notch_pass_counts: tuple[int, ...] = (1,)
    boundary_transition_seconds: float = Field(
        default=0.05, gt=0, le=1.0, allow_inf_nan=False
    )
    max_boundary_energy_jump_db: float = Field(
        default=0.5, gt=0, le=24.0, allow_inf_nan=False
    )
    max_boundary_crest_jump_db: float = Field(
        default=3.0, gt=0, le=24.0, allow_inf_nan=False
    )
    max_boundary_adjacent_delta: float = Field(
        default=0.08, gt=0, le=2.0, allow_inf_nan=False
    )
    max_non_target_band_attenuation_db: float = Field(
        default=0.25, ge=0, le=6.0, allow_inf_nan=False
    )
    maximum_measurement_windows: int = Field(default=1_000_000, ge=10, le=10_000_000)
    minimum_sample_rate_hz: int = Field(default=8000, ge=4000, le=48000)
    maximum_sample_rate_hz: int = Field(default=192000, ge=48000, le=384000)
    maximum_channels: int = Field(default=2, ge=1, le=8)
    stream_block_samples: int = Field(default=4096, ge=128, le=1_048_576)
    render_timeout_seconds: float = Field(
        default=3600.0, gt=0, le=86_400, allow_inf_nan=False
    )
    duration_tolerance_seconds: float = Field(
        default=0.05, gt=0, le=1.0, allow_inf_nan=False
    )
    audio_bitrate_kbps: int = Field(default=192, ge=64, le=512)

    @model_validator(mode="after")
    def _validate_relationships(self) -> TonalInterferenceConfig:
        if self.hop_seconds > self.window_seconds:
            raise ValueError("tonal hop must not exceed its window")
        if self.minimum_frequency_hz >= self.maximum_frequency_hz:
            raise ValueError("tonal frequency bounds are reversed")
        if self.minimum_sample_rate_hz > self.maximum_sample_rate_hz:
            raise ValueError("tonal sample-rate bounds are reversed")
        candidates = self.render_qualification_notch_q_values
        if (
            not candidates
            or len(candidates) > 16
            or any(
                not math.isfinite(value) or not 1.0 < value <= 100.0
                for value in candidates
            )
            or any(left <= right for left, right in zip(candidates, candidates[1:]))
        ):
            raise ValueError(
                "tonal render qualification Q values must be strictly descending"
            )
        pass_counts = self.render_qualification_notch_pass_counts
        if (
            not pass_counts
            or len(pass_counts) > 4
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 2
                for value in pass_counts
            )
            or len(set(pass_counts)) != len(pass_counts)
        ):
            raise ValueError("tonal render qualification pass counts are invalid")
        baseline_inventory = max(
            self.minimum_baseline_windows,
            math.ceil(self.local_baseline_seconds / self.hop_seconds),
        )
        minimum_inventory = 2 * baseline_inventory + self.minimum_persistence_windows
        if self.maximum_measurement_windows < minimum_inventory:
            raise ValueError(
                "tonal measurement inventory cannot hold bilateral baselines "
                "and the minimum persistent event"
            )
        return self


class TonalRenderQualification(_TonalModel):
    """Path-free evidence for the exact renderer selected for one tone."""

    algorithm_version: Literal["tonal-render-qualification-v1"] = (
        "tonal-render-qualification-v1"
    )
    boundary_mode: Literal["full_interval_v1"]
    notch_q: float = Field(gt=1.0, le=100.0, allow_inf_nan=False)
    notch_pass_count: int = Field(default=1, ge=1, le=2, strict=True)
    complete_window_count: int = Field(ge=1)
    minimum_target_reduction_db: float = Field(allow_inf_nan=False)
    maximum_non_target_attenuation_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_energy_jump_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_crest_jump_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_adjacent_delta: float = Field(ge=0.0, allow_inf_nan=False)


class InterferenceTone(_TonalModel):
    """Measured local interference with path-free before/after evidence."""

    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(gt=0, allow_inf_nan=False)
    center_frequency_hz: float = Field(gt=0, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    baseline_before_dbfs: float = Field(le=0, allow_inf_nan=False)
    baseline_after_dbfs: float = Field(le=0, allow_inf_nan=False)
    peak_dbfs: float = Field(le=0, allow_inf_nan=False)
    local_peak_over_baseline_db: float = Field(ge=0, allow_inf_nan=False)
    persistence_window_count: int = Field(ge=1)
    frequency_standard_deviation_hz: float = Field(ge=0, allow_inf_nan=False)
    channel_indices: tuple[int, ...] = Field(min_length=1)
    attenuation_target_db: float = Field(gt=0, allow_inf_nan=False)
    algorithm_version: Literal["tonal-interference-v1"] = _ALGORITHM_VERSION
    render_qualification: TonalRenderQualification | None = None

    @model_validator(mode="after")
    def _validate_interval(self) -> InterferenceTone:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("interference interval must be positive and half-open")
        if (
            any(index < 0 for index in self.channel_indices)
            or tuple(sorted(set(self.channel_indices))) != self.channel_indices
        ):
            raise ValueError("interference channel indices must be sorted and unique")
        return self


def detect_local_tonal_interference(
    samples: NDArray[np.generic],
    sample_rate_hz: int,
    config: TonalInterferenceConfig,
) -> tuple[InterferenceTone, ...]:
    """Return locally introduced stable spectral peaks with bilateral baselines."""
    pcm_view = _validated_pcm_view(samples, sample_rate_hz, config)
    window_size = max(2, round(config.window_seconds * sample_rate_hz))
    hop_size = max(1, round(config.hop_seconds * sample_rate_hz))
    if pcm_view.shape[0] < window_size:
        _validated_pcm(pcm_view, sample_rate_hz, config)
        return ()
    measurement_window_count = 1 + (pcm_view.shape[0] - window_size) // hop_size
    if measurement_window_count > config.maximum_measurement_windows:
        raise ValueError("tonal measurement window inventory exceeds configured limit")
    pcm = _validated_pcm(pcm_view, sample_rate_hz, config)
    frequencies = np.fft.rfftfreq(window_size, d=1.0 / sample_rate_hz)
    selected_bins = np.flatnonzero(
        (frequencies >= config.minimum_frequency_hz)
        & (frequencies <= min(config.maximum_frequency_hz, sample_rate_hz / 2.0))
    )
    if selected_bins.size == 0:
        return ()
    hann = np.hanning(window_size)
    coherent_gain = max(float(np.sum(hann)) / 2.0, np.finfo(np.float64).tiny)
    starts = range(0, pcm.shape[0] - window_size + 1, hop_size)
    epsilon = np.finfo(np.float64).tiny
    baseline_count = max(
        config.minimum_baseline_windows,
        math.ceil(config.local_baseline_seconds / config.hop_seconds),
    )
    raw_tones: list[InterferenceTone] = []
    for channel_index in range(pcm.shape[1]):
        spectra_db: NDArray[np.float64] = np.empty(
            (len(starts), selected_bins.size), dtype=np.float64
        )
        peak_matrix: NDArray[np.bool_] = np.zeros(
            (len(starts), selected_bins.size), dtype=np.bool_
        )
        for index, start in enumerate(starts):
            spectrum = np.abs(
                np.fft.rfft(pcm[start : start + window_size, channel_index] * hann)
            )
            db = 20.0 * np.log10(
                np.maximum(spectrum[selected_bins] / coherent_gain, epsilon)
            )
            spectra_db[index] = db
            noise_floor = float(np.median(db))
            local_maximum = np.ones(db.size, dtype=np.bool_)
            local_maximum[1:] &= db[1:] > db[:-1]
            local_maximum[:-1] &= db[:-1] >= db[1:]
            peak_matrix[index] = (
                local_maximum
                & (db >= config.minimum_peak_dbfs)
                & (db >= noise_floor + config.minimum_peak_prominence_db)
            )
            if np.count_nonzero(peak_matrix[index]) > config.maximum_simultaneous_peaks:
                peak_matrix[index] = False
        candidates: list[tuple[int, int, int]] = []
        for frequency_index in range(selected_bins.size):
            run_start: int | None = None
            for window_index, is_active in enumerate(peak_matrix[:, frequency_index]):
                if is_active and run_start is None:
                    run_start = window_index
                elif not is_active and run_start is not None:
                    candidates.append((frequency_index, run_start, window_index))
                    run_start = None
            if run_start is not None:
                candidates.append((frequency_index, run_start, len(starts)))
        for frequency_index, first, last_exclusive in candidates:
            candidate = _tone_from_candidate(
                spectra_db,
                frequencies,
                selected_bins,
                starts,
                pcm.shape[0],
                window_size,
                sample_rate_hz,
                channel_index,
                frequency_index,
                first,
                last_exclusive,
                baseline_count,
                config,
            )
            if candidate is not None:
                raw_tones.append(candidate)
    tones: list[InterferenceTone] = []
    for tone in sorted(
        raw_tones,
        key=lambda value: (
            value.start_seconds,
            value.center_frequency_hz,
            value.channel_indices,
        ),
    ):
        if tones and _same_tone_event(tones[-1], tone, config):
            previous = tones.pop()
            tones.append(
                previous.model_copy(
                    update={
                        "channel_indices": tuple(
                            sorted(set(previous.channel_indices + tone.channel_indices))
                        ),
                        "confidence": min(previous.confidence, tone.confidence),
                    }
                )
            )
        else:
            tones.append(tone)
    return tuple(tones)


def _tone_from_candidate(
    spectra_db: NDArray[np.float64],
    frequencies: NDArray[np.float64],
    selected_bins: NDArray[np.int64],
    starts: Sequence[int],
    sample_count: int,
    window_size: int,
    sample_rate_hz: int,
    channel_index: int,
    frequency_index: int,
    first: int,
    last_exclusive: int,
    baseline_count: int,
    config: TonalInterferenceConfig,
) -> InterferenceTone | None:
    if last_exclusive - first < config.minimum_persistence_windows:
        return None
    before_first = max(0, first - baseline_count)
    after_last = min(len(starts), last_exclusive + baseline_count)
    if (
        first - before_first < config.minimum_baseline_windows
        or after_last - last_exclusive < config.minimum_baseline_windows
    ):
        return None
    candidate_rows = spectra_db[first:last_exclusive]
    center_bin = int(selected_bins[frequency_index])
    bin_width_hz = float(frequencies[1] - frequencies[0])
    peak_frequencies: list[float] = []
    for row in candidate_rows:
        offset = 0.0
        if 0 < frequency_index < row.size - 1:
            left = float(row[frequency_index - 1])
            center = float(row[frequency_index])
            right = float(row[frequency_index + 1])
            denominator = left - 2.0 * center + right
            if abs(denominator) > np.finfo(np.float64).eps:
                offset = float(np.clip(0.5 * (left - right) / denominator, -0.5, 0.5))
        peak_frequencies.append(float(frequencies[center_bin]) + offset * bin_width_hz)
    center_frequency = float(np.median(peak_frequencies))
    frequency_deviation = float(np.std(peak_frequencies))
    if frequency_deviation > config.maximum_frequency_deviation_hz:
        return None
    before = float(np.median(spectra_db[before_first:first, frequency_index]))
    after = float(np.median(spectra_db[last_exclusive:after_last, frequency_index]))
    peak = float(np.median(spectra_db[first:last_exclusive, frequency_index]))
    local_gain = peak - max(before, after)
    if local_gain < config.minimum_local_gain_db:
        return None
    persistence = min(
        1.0,
        (last_exclusive - first) / config.minimum_persistence_windows,
    )
    gain_confidence = min(
        1.0,
        (local_gain - config.minimum_local_gain_db) / config.confidence_gain_span_db,
    )
    stability = max(
        0.0,
        1.0 - frequency_deviation / config.maximum_frequency_deviation_hz,
    )
    confidence = float((persistence + gain_confidence + stability) / 3.0)
    if confidence < config.minimum_confidence:
        return None
    start_sample = starts[first] + window_size // 2
    end_sample = starts[last_exclusive - 1] + window_size // 2
    return InterferenceTone(
        start_seconds=max(0.0, start_sample / sample_rate_hz),
        end_seconds=min(sample_count / sample_rate_hz, end_sample / sample_rate_hz),
        center_frequency_hz=center_frequency,
        confidence=confidence,
        baseline_before_dbfs=before,
        baseline_after_dbfs=after,
        peak_dbfs=peak,
        local_peak_over_baseline_db=local_gain,
        persistence_window_count=last_exclusive - first,
        frequency_standard_deviation_hz=frequency_deviation,
        channel_indices=(channel_index,),
        attenuation_target_db=config.attenuation_db,
        algorithm_version=config.algorithm_version,
    )


def _same_tone_event(
    left: InterferenceTone, right: InterferenceTone, config: TonalInterferenceConfig
) -> bool:
    return (
        left.start_seconds == right.start_seconds
        and left.end_seconds == right.end_seconds
        and abs(left.center_frequency_hz - right.center_frequency_hz)
        <= config.maximum_frequency_deviation_hz
    )


def apply_tonal_reduction_to_pcm(
    samples: NDArray[np.generic],
    sample_rate_hz: int,
    tones: Sequence[InterferenceTone],
    config: TonalInterferenceConfig,
) -> NDArray[np.float64]:
    """Apply stateful biquad notches with raised-cosine half-open boundaries."""
    pcm = _validated_pcm(samples, sample_rate_hz, config)
    selected = _validated_tones(
        tones,
        sample_rate_hz,
        pcm.shape[0],
        pcm.shape[1],
        config,
    )
    rendered = pcm.copy()
    for tone in selected:
        wet = rendered
        pass_count = (
            tone.render_qualification.notch_pass_count
            if tone.render_qualification is not None
            else 1
        )
        for _ in range(pass_count):
            wet = _notch_filter(
                wet,
                sample_rate_hz,
                tone.center_frequency_hz,
                config,
                notch_q=_tone_render_notch_q(tone, config),
            )
        weights = _interval_weights(
            rendered.shape[0],
            sample_rate_hz,
            tone.start_seconds,
            tone.end_seconds,
            config,
            boundary_mode=(
                tone.render_qualification.boundary_mode
                if tone.render_qualification is not None
                else "raised_cosine_v1"
            ),
        )
        maximum_mix = _maximum_notch_mix(tone, config)
        mix = weights[:, np.newaxis] * maximum_mix
        for channel_index in tone.channel_indices:
            rendered[:, channel_index] = (
                rendered[:, channel_index] * (1.0 - mix[:, 0])
                + wet[:, channel_index] * mix[:, 0]
            )
    return rendered


def qualify_tonal_render_profiles(
    samples: NDArray[np.generic],
    sample_rate_hz: int,
    tones: Sequence[InterferenceTone],
    config: TonalInterferenceConfig,
) -> tuple[InterferenceTone, ...]:
    """Keep only profiles with one deterministic renderer passing every gate."""
    pcm = _validated_pcm(samples, sample_rate_hz, config)
    measured = _validated_tones(
        tones,
        sample_rate_hz,
        pcm.shape[0],
        pcm.shape[1],
        config,
    )
    qualified: list[InterferenceTone] = []
    window_size = max(8, round(_RENDER_QUALIFICATION_WINDOW_SECONDS * sample_rate_hz))
    for tone in measured:
        global_start = _required_exclusive_sample_count(
            tone.start_seconds, sample_rate_hz
        )
        global_end = _required_exclusive_sample_count(tone.end_seconds, sample_rate_hz)
        baseline_margin = max(
            window_size, round(config.local_baseline_seconds * sample_rate_hz)
        )
        local_start = max(0, global_start - baseline_margin)
        local_end = min(pcm.shape[0], global_end + baseline_margin)
        local_pcm = pcm[local_start:local_end]
        start = global_start - local_start
        end = global_end - local_start
        global_times = (
            local_start + np.arange(local_pcm.shape[0], dtype=np.float64)
        ) / sample_rate_hz
        selected: TonalRenderQualification | None = None
        for notch_q in config.render_qualification_notch_q_values:
            for notch_pass_count in config.render_qualification_notch_pass_counts:
                wet = local_pcm
                for _ in range(notch_pass_count):
                    wet = _notch_filter(
                        wet,
                        sample_rate_hz,
                        tone.center_frequency_hz,
                        config,
                        notch_q=notch_q,
                    )
                candidate = local_pcm.copy()
                weights = _weights_for_times(
                    global_times,
                    tone.start_seconds,
                    tone.end_seconds,
                    config,
                    boundary_mode="full_interval_v1",
                )
                mix = weights * _maximum_notch_mix(tone, config)
                for channel_index in tone.channel_indices:
                    candidate[:, channel_index] = (
                        local_pcm[:, channel_index] * (1.0 - mix)
                        + wet[:, channel_index] * mix
                    )
                target_reductions: list[float] = []
                non_target_losses: list[float] = []
                boundary_energy: list[float] = []
                boundary_crest: list[float] = []
                boundary_adjacent: list[float] = []
                complete_windows = 0
                try:
                    for channel_index in tone.channel_indices:
                        spectral = complete_tonal_window_metrics(
                            local_pcm[start:end, channel_index],
                            candidate[start:end, channel_index],
                            sample_rate_hz,
                            target_frequency_hz=tone.center_frequency_hz,
                            window_seconds=_RENDER_QUALIFICATION_WINDOW_SECONDS,
                        )
                        target_reductions.append(
                            spectral["minimum_target_reduction_db"]
                        )
                        non_target_losses.append(
                            spectral["maximum_non_target_attenuation_db"]
                        )
                        complete_windows += round(spectral["complete_window_count"])
                        boundaries: tuple[tuple[int, Literal["start", "end"]], ...] = (
                            (start, "start"),
                            (end, "end"),
                        )
                        for boundary, side in boundaries:
                            boundary_metrics = source_relative_tonal_boundary_metrics(
                                local_pcm[:, channel_index],
                                candidate[:, channel_index],
                                boundary,
                                window_size,
                                sample_rate_hz,
                                tone.center_frequency_hz,
                                boundary_side=side,
                                boundary_mode="full_interval_v1",
                                boundary_transition_seconds=(
                                    config.boundary_transition_seconds
                                ),
                                derivative_numerical_floor=(
                                    config.max_boundary_adjacent_delta
                                ),
                            )
                            boundary_energy.append(boundary_metrics["energy_jump_db"])
                            boundary_crest.append(boundary_metrics["crest_jump_db"])
                            boundary_adjacent.append(boundary_metrics["adjacent_delta"])
                except ValueError:
                    continue
                evidence = TonalRenderQualification(
                    boundary_mode="full_interval_v1",
                    notch_q=notch_q,
                    notch_pass_count=notch_pass_count,
                    complete_window_count=complete_windows,
                    minimum_target_reduction_db=min(target_reductions),
                    maximum_non_target_attenuation_db=max(non_target_losses),
                    maximum_boundary_energy_jump_db=max(boundary_energy),
                    maximum_boundary_crest_jump_db=max(boundary_crest),
                    maximum_boundary_adjacent_delta=max(boundary_adjacent),
                )
                if _render_qualification_passes(evidence, tone, config):
                    selected = evidence
                    break
            if selected is not None:
                break
        if selected is not None:
            qualified.append(tone.model_copy(update={"render_qualification": selected}))
    return tuple(qualified)


def _render_qualification_passes(
    qualification: TonalRenderQualification,
    tone: InterferenceTone,
    config: TonalInterferenceConfig,
) -> bool:
    return (
        qualification.notch_q in config.render_qualification_notch_q_values
        and qualification.notch_pass_count
        in config.render_qualification_notch_pass_counts
        and qualification.minimum_target_reduction_db >= tone.attenuation_target_db
        and qualification.maximum_non_target_attenuation_db
        <= config.max_non_target_band_attenuation_db
        and qualification.maximum_boundary_energy_jump_db
        <= config.max_boundary_energy_jump_db
        and qualification.maximum_boundary_crest_jump_db
        <= config.max_boundary_crest_jump_db
        and qualification.maximum_boundary_adjacent_delta
        <= config.max_boundary_adjacent_delta
    )


def validate_tonal_render_qualification(
    tone: InterferenceTone, config: TonalInterferenceConfig
) -> TonalRenderQualification:
    """Return one strict passing qualification or reject the serialized profile."""
    qualification = tone.render_qualification
    if qualification is None or not _render_qualification_passes(
        qualification, tone, config
    ):
        raise ValueError("tonal render qualification is missing or does not pass")
    expected_windows = math.floor(
        (tone.end_seconds - tone.start_seconds) / _RENDER_QUALIFICATION_WINDOW_SECONDS
        + 1e-9
    ) * len(tone.channel_indices)
    if qualification.complete_window_count != expected_windows:
        raise ValueError("tonal render qualification window inventory is invalid")
    return qualification


def validate_tonal_profile_contracts(
    tones: Sequence[InterferenceTone], config: TonalInterferenceConfig
) -> tuple[tuple[float, float], ...]:
    """Validate serialized measurement semantics and return exact range union."""
    profiles = tuple(tones)
    if not profiles:
        raise ValueError("tonal profile inventory is empty")
    ordered = tuple(
        sorted(
            profiles,
            key=lambda tone: (
                tone.start_seconds,
                tone.center_frequency_hz,
                tone.channel_indices,
                tone.end_seconds,
            ),
        )
    )
    if profiles != ordered:
        raise ValueError("tonal profiles are not in canonical measurement order")
    previous_end_by_channel = [-math.inf] * config.maximum_channels
    for tone in profiles:
        validate_tonal_render_qualification(tone, config)
        expected_local_gain = tone.peak_dbfs - max(
            tone.baseline_before_dbfs, tone.baseline_after_dbfs
        )
        if (
            tone.algorithm_version != config.algorithm_version
            or tone.attenuation_target_db != config.attenuation_db
            or not config.minimum_frequency_hz
            <= tone.center_frequency_hz
            <= config.maximum_frequency_hz
            or tone.confidence < config.minimum_confidence
            or tone.peak_dbfs < config.minimum_peak_dbfs
            or tone.local_peak_over_baseline_db < config.minimum_local_gain_db
            or tone.persistence_window_count < config.minimum_persistence_windows
            or tone.frequency_standard_deviation_hz
            > config.maximum_frequency_deviation_hz
            or any(index >= config.maximum_channels for index in tone.channel_indices)
            or not math.isclose(
                tone.local_peak_over_baseline_db,
                expected_local_gain,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
        ):
            raise ValueError("tonal profile does not match its measurement config")
        for channel_index in tone.channel_indices:
            if tone.start_seconds < previous_end_by_channel[channel_index]:
                raise ValueError("tonal profiles overlap on one decoded channel")
            previous_end_by_channel[channel_index] = tone.end_seconds
    return tuple(
        sorted(set((tone.start_seconds, tone.end_seconds) for tone in profiles))
    )


def validate_plan_tonal_action_contracts(
    plan: RescuePlan, *, allow_unqualified_draft: bool = False
) -> None:
    """Reject unqualified or range-drifted tonal action wires."""
    from videoscope.rescue.models import RescueActionKind

    for action in plan.actions:
        if (
            action.kind is not RescueActionKind.DENOISE_AUDIO
            or not action.parameters.get("interference_profiles")
        ):
            continue
        try:
            if (
                action.parameters["algorithm_version"]
                != plan.effective_config.tonal_algorithm_version
            ):
                raise ValueError
            config = TonalInterferenceConfig.model_validate_json(
                json.dumps(action.parameters["config"], ensure_ascii=False)
            )
            raw_profiles = action.parameters["interference_profiles"]
            if not isinstance(raw_profiles, (list, tuple)) or not raw_profiles:
                raise ValueError
            profiles = tuple(
                InterferenceTone.model_validate_json(
                    json.dumps(profile, ensure_ascii=False)
                )
                for profile in raw_profiles
            )
            profile_ranges = validate_tonal_profile_contracts(profiles, config)
            if profile_ranges != action.source_ranges:
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("tonal action qualification contract is invalid") from exc
        if action.parameters.get("encoded_candidate_qualification") is None:
            if allow_unqualified_draft:
                continue
            raise ValueError("encoded candidate qualification is missing")
        from videoscope.rescue.tonal_qualification import (
            validate_encoded_tonal_qualification,
        )

        validate_encoded_tonal_qualification(plan, action, config, profiles)


def render_tonal_interference_reduced_audio(
    source: Path,
    output: Path,
    tones: Sequence[InterferenceTone],
    config: TonalInterferenceConfig,
    *,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    runner: ExternalCommandRunner,
    cancellation_callback: Callable[[], bool] | None = None,
) -> None:
    """Stream PCM through measured notches and publish a verified media candidate."""
    _render_tonal_audio_generation(
        source,
        output,
        tones,
        config,
        identity=False,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        runner=runner,
        cancellation_callback=cancellation_callback,
    )


def render_tonal_identity_audio(
    source: Path,
    output: Path,
    config: TonalInterferenceConfig,
    *,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    runner: ExternalCommandRunner,
    cancellation_callback: Callable[[], bool] | None = None,
) -> None:
    """Encode one untouched PCM sibling through the tonal candidate path."""
    _render_tonal_audio_generation(
        source,
        output,
        (),
        config,
        identity=True,
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        runner=runner,
        cancellation_callback=cancellation_callback,
    )


def _render_tonal_audio_generation(
    source: Path,
    output: Path,
    tones: Sequence[InterferenceTone],
    config: TonalInterferenceConfig,
    *,
    identity: bool,
    ffmpeg_path: Path,
    ffprobe_path: Path,
    runner: ExternalCommandRunner,
    cancellation_callback: Callable[[], bool] | None = None,
) -> None:
    """Render either a processed candidate or its same-generation identity."""
    source = Path(source)
    output = Path(output)
    ffmpeg_path = Path(ffmpeg_path)
    ffprobe_path = Path(ffprobe_path)
    cancelled = cancellation_callback or (lambda: False)
    callback_failure: list[Exception] = []

    def safe_cancelled() -> bool:
        if callback_failure:
            return True
        try:
            return bool(cancelled())
        except Exception as exc:
            callback_failure.append(exc)
            return True

    def raise_callback_failure() -> None:
        if callback_failure:
            raise callback_failure[0]

    def safe_runner(arguments: tuple[str, ...]) -> CommandResult:
        try:
            result = runner(
                arguments,
                timeout_seconds=config.render_timeout_seconds,
                sensitive_paths=(source, output),
                cancellation_callback=safe_cancelled,
            )
        except RescueCancelledError:
            raise_callback_failure()
            raise
        except RescueMediaError:
            raise_callback_failure()
            raise
        except Exception as exc:
            raise_callback_failure()
            raise RescueMediaError("tonal media command failed") from exc
        raise_callback_failure()
        return result

    _validate_render_paths(source, output, ffmpeg_path, ffprobe_path)
    if safe_cancelled():
        raise_callback_failure()
        raise RescueCancelledError("tonal rendering cancelled before probe")
    source_probe = _probe_audio(source, ffprobe_path, safe_runner)
    duration, sample_rate_hz, channel_count, channel_layout = _audio_properties(
        source_probe, config
    )
    try:
        selected = _validated_tones(
            tones,
            sample_rate_hz,
            round(duration * sample_rate_hz),
            channel_count,
            config,
            end_tolerance_seconds=config.duration_tolerance_seconds,
        )
    except ValueError as exc:
        raise RescueMediaError("tonal measurements do not match source audio") from exc
    if not selected and not identity:
        raise RescueInputError("at least one measured interference tone is required")

    output.parent.mkdir(parents=True, exist_ok=True)
    candidate_file = tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.tonal-",
        suffix=".mp4",
        dir=output.parent,
        delete=False,
    )
    candidate = Path(candidate_file.name)
    candidate_file.close()
    candidate.unlink(missing_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="videoscope-tonal-", dir=output.parent
        ) as temporary_name:
            temporary = Path(temporary_name)
            decoded = temporary / "decoded.f32le"
            processed = temporary / "processed.f32le"
            decode = safe_runner(
                (
                    str(ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-n",
                    "-i",
                    str(source),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    str(channel_count),
                    "-ar",
                    str(sample_rate_hz),
                    "-c:a",
                    "pcm_f32le",
                    "-f",
                    "f32le",
                    str(decoded),
                )
            )
            if decode.returncode != 0 or not decoded.is_file():
                raise RescueMediaError("tonal source audio decode failed")
            try:
                selected = _tones_for_decoded_pcm_inventory(
                    selected,
                    sample_rate_hz,
                    _decoded_pcm_sample_count(decoded, channel_count),
                    channel_count,
                    config,
                )
            except ValueError as exc:
                raise RescueMediaError(
                    "tonal measurements do not match decoded audio"
                ) from exc
            try:
                if not selected and not identity:
                    raise RescueInputError(
                        "at least one measured interference tone is required"
                    )
                if identity:
                    _stream_copy_pcm(
                        decoded,
                        processed,
                        channel_count,
                        config,
                        safe_cancelled,
                    )
                else:
                    _stream_filter_pcm(
                        decoded,
                        processed,
                        sample_rate_hz,
                        channel_count,
                        selected,
                        config,
                        safe_cancelled,
                    )
            except RescueCancelledError:
                raise_callback_failure()
                raise
            raise_callback_failure()
            mux = safe_runner(
                (
                    str(ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-n",
                    "-i",
                    str(source),
                    "-f",
                    "f32le",
                    "-ar",
                    str(sample_rate_hz),
                    "-ac",
                    str(channel_count),
                    "-channel_layout",
                    channel_layout,
                    "-i",
                    str(processed),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-b:a",
                    f"{config.audio_bitrate_kbps}k",
                    "-map_metadata",
                    "-1",
                    "-movflags",
                    "+faststart",
                    "-shortest",
                    str(candidate),
                )
            )
            if mux.returncode != 0 or not candidate.is_file():
                raise RescueMediaError("tonal audio mux failed")
            candidate_probe = _probe_audio(candidate, ffprobe_path, safe_runner)
            _validate_candidate_audio(
                source_probe,
                candidate_probe,
                duration=duration,
                sample_rate_hz=sample_rate_hz,
                channel_count=channel_count,
                channel_layout=channel_layout,
                config=config,
            )
            verified = safe_runner(
                (
                    str(ffmpeg_path),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-xerror",
                    "-i",
                    str(candidate),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-f",
                    "null",
                    "-",
                )
            )
            if verified.returncode != 0:
                raise RescueMediaError("tonal candidate did not fully decode")
            if safe_cancelled():
                raise_callback_failure()
                raise RescueCancelledError(
                    "tonal rendering cancelled before publication"
                )
            try:
                os.link(candidate, output)
            except OSError as exc:
                raise RescueArtifactError(
                    "tonal output could not be published without overwrite"
                ) from exc
            candidate.unlink()
    finally:
        candidate.unlink(missing_ok=True)


def _validate_render_paths(
    source: Path, output: Path, ffmpeg_path: Path, ffprobe_path: Path
) -> None:
    if not source.is_file():
        raise RescueInputError("tonal source must be an existing regular file")
    if output.exists() or output.is_symlink():
        raise RescueArtifactError("tonal output must not already exist")
    if os.path.normcase(str(source.resolve(strict=False))) == os.path.normcase(
        str(output.resolve(strict=False))
    ):
        raise RescueArtifactError("tonal output must not alias the source")
    for executable in (ffmpeg_path, ffprobe_path):
        if str(executable) in ("", ".", "..") or (
            executable.exists() and not executable.is_file()
        ):
            raise RescueInputError("media executable path is not safe")


def _probe_audio(
    path: Path,
    ffprobe_path: Path,
    runner: Callable[[tuple[str, ...]], CommandResult],
) -> dict[str, object]:
    command = (
        str(ffprobe_path),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    )
    for attempt in range(1, _TONAL_PROBE_ATTEMPTS + 1):
        result = runner(command)
        if result.returncode != 0:
            raise RescueMediaError("tonal media probe failed")
        decode_position: tuple[int, int] | None = None
        non_text_stdout = False
        payload: object = None
        try:
            payload = json.loads(result.stdout_summary)
        except json.JSONDecodeError as exc:
            decode_position = (exc.lineno, exc.colno)
        except TypeError:
            non_text_stdout = True
        if decode_position is not None:
            if attempt < _TONAL_PROBE_ATTEMPTS:
                continue
            line, column = decode_position
            raise RescueMediaError(
                "tonal media probe returned invalid JSON after "
                f"{attempt} attempts (line {line}, column {column})"
            )
        if non_text_stdout:
            if attempt < _TONAL_PROBE_ATTEMPTS:
                continue
            raise RescueMediaError(
                f"tonal media probe returned non-text JSON after {attempt} attempts"
            )
        if _is_usable_tonal_probe_payload(payload):
            return cast(dict[str, object], payload)
        if attempt >= _TONAL_PROBE_ATTEMPTS:
            raise RescueMediaError(
                f"tonal media probe returned incomplete data after {attempt} attempts"
            )

    raise AssertionError("tonal media probe retry loop exhausted unexpectedly")


def _is_usable_tonal_probe_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    streams = payload.get("streams")
    raw_format = payload.get("format")
    if (
        not isinstance(streams, list)
        or not all(isinstance(stream, dict) for stream in streams)
        or not isinstance(raw_format, dict)
    ):
        return False
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = [stream for stream in streams if stream.get("codec_type") == "video"]
    if len(audio) != 1 or len(video) != 1:
        return False
    try:
        duration = float(raw_format["duration"])
        int(audio[0]["sample_rate"])
        int(audio[0]["channels"])
    except (KeyError, TypeError, ValueError):
        return False
    channel_layout = audio[0].get("channel_layout")
    return (
        math.isfinite(duration)
        and duration > 0
        and isinstance(channel_layout, str)
        and bool(channel_layout)
    )


def _audio_properties(
    payload: dict[str, object], config: TonalInterferenceConfig
) -> tuple[float, int, int, str]:
    streams = payload.get("streams")
    raw_format = payload.get("format")
    if not isinstance(streams, list) or not isinstance(raw_format, dict):
        raise RescueMediaError("tonal media metadata is incomplete")
    audio = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "audio"
    ]
    video = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if len(audio) != 1 or len(video) != 1:
        raise RescueMediaError(
            "tonal rendering requires one video and one audio stream"
        )
    try:
        duration = float(raw_format["duration"])
        sample_rate_hz = int(audio[0]["sample_rate"])
        channel_count = int(audio[0]["channels"])
        channel_layout = str(audio[0]["channel_layout"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RescueMediaError("tonal audio metadata is incomplete") from exc
    if (
        not math.isfinite(duration)
        or duration <= 0
        or not config.minimum_sample_rate_hz
        <= sample_rate_hz
        <= config.maximum_sample_rate_hz
        or not 1 <= channel_count <= config.maximum_channels
        or channel_layout not in {"mono", "stereo"}
        or (channel_layout == "mono") != (channel_count == 1)
    ):
        raise RescueMediaError("tonal audio layout is unsupported")
    return duration, sample_rate_hz, channel_count, channel_layout


def _stream_filter_pcm(
    decoded: Path,
    processed: Path,
    sample_rate_hz: int,
    channel_count: int,
    tones: tuple[InterferenceTone, ...],
    config: TonalInterferenceConfig,
    cancellation_callback: Callable[[], bool],
) -> None:
    states = tuple(
        tuple(
            _StreamingNotch(
                sample_rate_hz,
                channel_count,
                tone.center_frequency_hz,
                config,
                notch_q=_tone_render_notch_q(tone, config),
            )
            for _ in range(
                tone.render_qualification.notch_pass_count
                if tone.render_qualification is not None
                else 1
            )
        )
        for tone in tones
    )
    frame_bytes = channel_count * np.dtype("<f4").itemsize
    sample_offset = 0
    try:
        with decoded.open("rb") as input_file, processed.open("xb") as output_file:
            while True:
                if cancellation_callback():
                    raise RescueCancelledError(
                        "tonal rendering cancelled during PCM processing"
                    )
                raw = input_file.read(config.stream_block_samples * frame_bytes)
                if not raw:
                    break
                if len(raw) % frame_bytes:
                    raise RescueMediaError("tonal PCM decode was truncated")
                block = (
                    np.frombuffer(raw, dtype="<f4")
                    .reshape((-1, channel_count))
                    .astype(np.float64)
                )
                if not np.all(np.isfinite(block)) or float(np.max(np.abs(block))) > 1.1:
                    raise RescueMediaError("tonal PCM decode is malformed")
                rendered = block
                for tone, tone_states in zip(tones, states, strict=True):
                    wet = rendered
                    for state in tone_states:
                        wet = state.process(wet)
                    weights = _block_weights(
                        sample_offset,
                        rendered.shape[0],
                        sample_rate_hz,
                        tone,
                        config,
                    )
                    maximum_mix = _maximum_notch_mix(tone, config)
                    mix = weights[:, np.newaxis] * maximum_mix
                    for channel_index in tone.channel_indices:
                        rendered[:, channel_index] = (
                            rendered[:, channel_index] * (1.0 - mix[:, 0])
                            + wet[:, channel_index] * mix[:, 0]
                        )
                output_file.write(rendered.astype("<f4").tobytes())
                sample_offset += rendered.shape[0]
    except (RescueCancelledError, RescueMediaError):
        raise
    except OSError as exc:
        raise RescueArtifactError("tonal PCM workspace failed") from exc


def _stream_copy_pcm(
    decoded: Path,
    processed: Path,
    channel_count: int,
    config: TonalInterferenceConfig,
    cancellation_callback: Callable[[], bool],
) -> None:
    """Copy decoded PCM exactly while preserving bounded cancellation checks."""
    frame_bytes = channel_count * np.dtype("<f4").itemsize
    try:
        with decoded.open("rb") as input_file, processed.open("xb") as output_file:
            while True:
                if cancellation_callback():
                    raise RescueCancelledError(
                        "tonal rendering cancelled during PCM processing"
                    )
                raw = input_file.read(config.stream_block_samples * frame_bytes)
                if not raw:
                    break
                if len(raw) % frame_bytes:
                    raise RescueMediaError("tonal PCM decode was truncated")
                block = np.frombuffer(raw, dtype="<f4")
                if not np.all(np.isfinite(block)) or float(np.max(np.abs(block))) > 1.1:
                    raise RescueMediaError("tonal PCM decode is malformed")
                output_file.write(raw)
    except (RescueCancelledError, RescueMediaError):
        raise
    except OSError as exc:
        raise RescueArtifactError("tonal PCM workspace failed") from exc


def _maximum_notch_mix(
    tone: InterferenceTone, config: TonalInterferenceConfig
) -> float:
    """Return bounded render strength with margin above the public 24 dB gate."""
    render_attenuation_db = (
        tone.attenuation_target_db + config.render_attenuation_headroom_db
    )
    return 1.0 - math.pow(10.0, -render_attenuation_db / 20.0)


def _decoded_pcm_sample_count(decoded: Path, channel_count: int) -> int:
    if (
        isinstance(channel_count, bool)
        or not isinstance(channel_count, int)
        or channel_count <= 0
    ):
        raise RescueMediaError("tonal PCM channel inventory is invalid")
    frame_bytes = channel_count * np.dtype("<f4").itemsize
    byte_count = decoded.stat().st_size
    if byte_count <= 0 or byte_count % frame_bytes:
        raise RescueMediaError("tonal PCM decode was truncated")
    return byte_count // frame_bytes


def _tones_for_decoded_pcm_inventory(
    tones: Sequence[InterferenceTone],
    sample_rate_hz: int,
    sample_count: int,
    channel_count: int,
    config: TonalInterferenceConfig,
) -> tuple[InterferenceTone, ...]:
    """Bind tones strictly to the decoded integer PCM sample inventory."""
    return _validated_tones(
        tones,
        sample_rate_hz,
        sample_count,
        channel_count,
        config,
    )


class _StreamingNotch:
    def __init__(
        self,
        sample_rate_hz: int,
        channels: int,
        frequency_hz: float,
        config: TonalInterferenceConfig,
        *,
        notch_q: float,
    ) -> None:
        frequency_hz = _render_center_frequency_hz(frequency_hz, config)
        omega = 2.0 * math.pi * frequency_hz / sample_rate_hz
        alpha = math.sin(omega) / (2.0 * notch_q)
        a0 = 1.0 + alpha
        self.coefficients = (
            1.0 / a0,
            -2.0 * math.cos(omega) / a0,
            1.0 / a0,
            -2.0 * math.cos(omega) / a0,
            (1.0 - alpha) / a0,
        )
        self.x1: NDArray[np.float64] = np.zeros(channels, dtype=np.float64)
        self.x2: NDArray[np.float64] = np.zeros(channels, dtype=np.float64)
        self.y1: NDArray[np.float64] = np.zeros(channels, dtype=np.float64)
        self.y2: NDArray[np.float64] = np.zeros(channels, dtype=np.float64)

    def process(self, samples: NDArray[np.float64]) -> NDArray[np.float64]:
        b0, b1, b2, a1, a2 = self.coefficients
        output = np.empty_like(samples)
        for index, current in enumerate(samples):
            value = (
                b0 * current + b1 * self.x1 + b2 * self.x2 - a1 * self.y1 - a2 * self.y2
            )
            output[index] = value
            self.x2, self.x1 = self.x1, current.copy()
            self.y2, self.y1 = self.y1, value.copy()
        return output


def _block_weights(
    sample_offset: int,
    sample_count: int,
    sample_rate_hz: int,
    tone: InterferenceTone,
    config: TonalInterferenceConfig,
) -> NDArray[np.float64]:
    times = (sample_offset + np.arange(sample_count, dtype=np.float64)) / sample_rate_hz
    qualification = tone.render_qualification
    return _weights_for_times(
        times,
        tone.start_seconds,
        tone.end_seconds,
        config,
        boundary_mode=(
            qualification.boundary_mode
            if qualification is not None
            else "raised_cosine_v1"
        ),
    )


def _validate_candidate_audio(
    source_probe: dict[str, object],
    candidate_probe: dict[str, object],
    *,
    duration: float,
    sample_rate_hz: int,
    channel_count: int,
    channel_layout: str,
    config: TonalInterferenceConfig,
) -> None:
    candidate_duration, candidate_rate, candidate_channels, candidate_layout = (
        _audio_properties(candidate_probe, config)
    )
    if (
        abs(candidate_duration - duration) > config.duration_tolerance_seconds
        or candidate_rate != sample_rate_hz
        or candidate_channels != channel_count
        or candidate_layout != channel_layout
    ):
        raise RescueMediaError("tonal candidate audio contract changed")
    source_streams = source_probe.get("streams")
    candidate_streams = candidate_probe.get("streams")
    if not isinstance(source_streams, list) or not isinstance(candidate_streams, list):
        raise RescueMediaError("tonal candidate stream inventory is invalid")
    source_video = [
        item.get("codec_name")
        for item in source_streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    candidate_video = [
        item.get("codec_name")
        for item in candidate_streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if source_video != candidate_video:
        raise RescueMediaError("tonal candidate video stream changed")


def _validated_pcm(
    samples: NDArray[np.generic],
    sample_rate_hz: int,
    config: TonalInterferenceConfig,
) -> NDArray[np.float64]:
    array = _validated_pcm_view(samples, sample_rate_hz, config)
    result = array.astype(np.float64)
    if not np.all(np.isfinite(result)):
        raise ValueError("PCM values must be finite")
    if float(np.max(np.abs(result))) > 1.0:
        raise ValueError("PCM values must be normalized to [-1, 1]")
    return result


def _validated_pcm_view(
    samples: NDArray[np.generic],
    sample_rate_hz: int,
    config: TonalInterferenceConfig,
) -> NDArray[np.generic]:
    if isinstance(sample_rate_hz, bool) or not isinstance(sample_rate_hz, int):
        raise ValueError("sample rate must be an integer")
    if (
        not config.minimum_sample_rate_hz
        <= sample_rate_hz
        <= config.maximum_sample_rate_hz
    ):
        raise ValueError("sample rate is outside configured bounds")
    array = np.asarray(samples)
    if array.ndim == 1:
        array = array[:, np.newaxis]
    if array.ndim != 2 or array.shape[0] == 0:
        raise ValueError("PCM must contain mono or interleaved channel samples")
    if array.shape[1] < 1 or array.shape[1] > config.maximum_channels:
        raise ValueError("PCM channel layout is unsupported")
    if not np.issubdtype(array.dtype, np.number):
        raise ValueError("PCM values must be numeric")
    return array


def _validated_tones(
    tones: Sequence[InterferenceTone],
    sample_rate_hz: int,
    sample_count: int,
    channel_count: int,
    config: TonalInterferenceConfig,
    *,
    end_tolerance_seconds: float = 0.0,
) -> tuple[InterferenceTone, ...]:
    if not math.isfinite(end_tolerance_seconds) or end_tolerance_seconds < 0:
        raise ValueError("tone end tolerance must be finite and non-negative")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count <= 0
        or isinstance(channel_count, bool)
        or not isinstance(channel_count, int)
        or channel_count <= 0
    ):
        raise ValueError("PCM inventory must contain complete sample frames")
    values = tuple(tones)
    previous_end_by_channel = [-math.inf] * channel_count
    for tone in values:
        if tone.algorithm_version != config.algorithm_version:
            raise ValueError("tone algorithm version does not match config")
        if tone.attenuation_target_db != config.attenuation_db:
            raise ValueError("tone attenuation target does not match config")
        if tone.center_frequency_hz >= sample_rate_hz / 2.0:
            raise ValueError("tone frequency exceeds Nyquist")
        if any(index >= channel_count for index in tone.channel_indices):
            raise ValueError("tone channel index exceeds actual PCM channels")
        start_index = _required_exclusive_sample_count(
            tone.start_seconds, sample_rate_hz
        )
        end_index = _required_exclusive_sample_count(tone.end_seconds, sample_rate_hz)
        if start_index >= end_index:
            raise ValueError("tone interval contains no PCM sample")
        if end_tolerance_seconds > 0:
            exceeds_inventory = tone.end_seconds > (
                sample_count / sample_rate_hz + end_tolerance_seconds
            )
        else:
            exceeds_inventory = end_index > sample_count
        if exceeds_inventory:
            raise ValueError("tone interval exceeds PCM duration")
        for channel_index in tone.channel_indices:
            if tone.start_seconds < previous_end_by_channel[channel_index]:
                raise ValueError(
                    "tone intervals must be sorted and non-overlapping per channel"
                )
            previous_end_by_channel[channel_index] = tone.end_seconds
    return values


def _required_exclusive_sample_count(
    timestamp_seconds: float, sample_rate_hz: int
) -> int:
    """Return ceil(t * rate), normalizing only one scaled-float ULP."""
    if (
        isinstance(sample_rate_hz, bool)
        or not isinstance(sample_rate_hz, int)
        or sample_rate_hz <= 0
        or not math.isfinite(timestamp_seconds)
        or timestamp_seconds < 0
    ):
        raise ValueError("sample timestamp and rate must be finite and non-negative")
    scaled = timestamp_seconds * sample_rate_hz
    if not math.isfinite(scaled):
        raise ValueError("sample timestamp exceeds supported inventory")
    nearest_integer = round(scaled)
    if abs(scaled - nearest_integer) <= math.ulp(scaled):
        scaled = float(nearest_integer)
    return math.ceil(scaled)


def _notch_filter(
    samples: NDArray[np.float64],
    sample_rate_hz: int,
    frequency_hz: float,
    config: TonalInterferenceConfig,
    *,
    notch_q: float | None = None,
) -> NDArray[np.float64]:
    frequency_hz = _render_center_frequency_hz(frequency_hz, config)
    omega = 2.0 * math.pi * frequency_hz / sample_rate_hz
    effective_q = _render_notch_q(frequency_hz, config) if notch_q is None else notch_q
    alpha = math.sin(omega) / (2.0 * effective_q)
    a0 = 1.0 + alpha
    b0 = 1.0 / a0
    b1 = -2.0 * math.cos(omega) / a0
    b2 = 1.0 / a0
    a1 = -2.0 * math.cos(omega) / a0
    a2 = (1.0 - alpha) / a0
    output = np.empty_like(samples)
    x1 = np.zeros(samples.shape[1], dtype=np.float64)
    x2 = np.zeros(samples.shape[1], dtype=np.float64)
    y1 = np.zeros(samples.shape[1], dtype=np.float64)
    y2 = np.zeros(samples.shape[1], dtype=np.float64)
    for index, current in enumerate(samples):
        value = b0 * current + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        output[index] = value
        x2, x1 = x1, current.copy()
        y2, y1 = y1, value.copy()
    return output


def _render_center_frequency_hz(
    measured_frequency_hz: float, config: TonalInterferenceConfig
) -> float:
    """Keep the continuous measured tone center for the renderer."""
    del config
    return measured_frequency_hz


def _render_notch_q(
    render_frequency_hz: float, config: TonalInterferenceConfig
) -> float:
    """Bound pole settling so the first full-weight window meets its target."""
    required_settling_db = (
        config.attenuation_db
        + config.render_attenuation_headroom_db
        + _NOTCH_SETTLING_SAFETY_DB
    )
    required_exponent = required_settling_db * math.log(10.0) / 20.0
    settling_q = (
        math.pi
        * render_frequency_hz
        * config.boundary_transition_seconds
        / required_exponent
    )
    return min(config.notch_q, max(1.000_001, settling_q))


def _interval_weights(
    sample_count: int,
    sample_rate_hz: int,
    start_seconds: float,
    end_seconds: float,
    config: TonalInterferenceConfig,
    *,
    boundary_mode: Literal["raised_cosine_v1", "full_interval_v1"] = (
        "raised_cosine_v1"
    ),
) -> NDArray[np.float64]:
    times = np.arange(sample_count, dtype=np.float64) / sample_rate_hz
    return _weights_for_times(
        times,
        start_seconds,
        end_seconds,
        config,
        boundary_mode=boundary_mode,
    )


def _weights_for_times(
    times: NDArray[np.float64],
    start_seconds: float,
    end_seconds: float,
    config: TonalInterferenceConfig,
    *,
    boundary_mode: Literal["raised_cosine_v1", "full_interval_v1"] = (
        "raised_cosine_v1"
    ),
) -> NDArray[np.float64]:
    weights = np.zeros(times.size, dtype=np.float64)
    inside = (times >= start_seconds) & (times < end_seconds)
    weights[inside] = 1.0
    if boundary_mode == "full_interval_v1":
        return weights
    transition = min(
        config.boundary_transition_seconds,
        (end_seconds - start_seconds) / 2.0,
    )
    fade_in = inside & (times < start_seconds + transition)
    fade_out = inside & (times >= end_seconds - transition)
    weights[fade_in] = 0.5 - 0.5 * np.cos(
        np.pi * (times[fade_in] - start_seconds) / transition
    )
    weights[fade_out] = 0.5 - 0.5 * np.cos(
        np.pi * (end_seconds - times[fade_out]) / transition
    )
    return weights


def _tone_render_notch_q(
    tone: InterferenceTone, config: TonalInterferenceConfig
) -> float:
    qualification = tone.render_qualification
    if qualification is None:
        return _render_notch_q(tone.center_frequency_hz, config)
    return qualification.notch_q


__all__ = [
    "InterferenceTone",
    "TonalInterferenceConfig",
    "TonalRenderQualification",
    "apply_tonal_reduction_to_pcm",
    "detect_local_tonal_interference",
    "qualify_tonal_render_profiles",
    "render_tonal_interference_reduced_audio",
    "render_tonal_identity_audio",
    "validate_tonal_profile_contracts",
    "validate_tonal_render_qualification",
    "validate_plan_tonal_action_contracts",
]
