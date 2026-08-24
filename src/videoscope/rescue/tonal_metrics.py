"""Independent, path-free tonal verification metrics shared by qualification."""

from __future__ import annotations

import math
from typing import Literal

import numpy as np


def complete_tonal_window_metrics(
    source_samples: np.ndarray,
    candidate_samples: np.ndarray,
    sample_rate_hz: int,
    *,
    target_frequency_hz: float,
    window_seconds: float,
) -> dict[str, float]:
    """Measure every complete, non-overlapping window in one exact event."""
    source = np.asarray(source_samples, dtype=np.float64).reshape(-1)
    candidate = np.asarray(candidate_samples, dtype=np.float64).reshape(-1)
    if source.shape != candidate.shape or source.size == 0:
        raise ValueError("tonal comparison samples are not aligned")
    if (
        sample_rate_hz <= 0
        or not math.isfinite(target_frequency_hz)
        or not math.isclose(window_seconds, 0.05, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise ValueError("tonal comparison parameters are invalid")
    window_size = max(8, round(window_seconds * sample_rate_hz))
    if source.size < window_size:
        raise ValueError("tonal comparison contains no complete window")
    window = np.hanning(window_size)
    frequencies = np.fft.rfftfreq(window_size, d=1.0 / sample_rate_hz)
    target_index = int(np.argmin(np.abs(frequencies - target_frequency_hz)))
    exclusion = np.abs(frequencies - target_frequency_hz) <= max(
        2.0 * sample_rate_hz / window_size, 40.0
    )
    epsilon = float(np.finfo(np.float64).tiny)
    reductions: list[float] = []
    preservation: list[float] = []
    for start in range(0, source.size - window_size + 1, window_size):
        source_spectrum = np.abs(
            np.fft.rfft(source[start : start + window_size] * window)
        )
        candidate_spectrum = np.abs(
            np.fft.rfft(candidate[start : start + window_size] * window)
        )
        reductions.append(
            20.0
            * math.log10(
                max(float(source_spectrum[target_index]), epsilon)
                / max(float(candidate_spectrum[target_index]), epsilon)
            )
        )
        source_non_target = float(np.linalg.norm(source_spectrum[~exclusion]))
        candidate_non_target = float(np.linalg.norm(candidate_spectrum[~exclusion]))
        preservation.append(
            max(
                0.0,
                20.0
                * math.log10(
                    max(source_non_target, epsilon) / max(candidate_non_target, epsilon)
                ),
            )
        )
    return {
        "minimum_target_reduction_db": float(min(reductions)),
        "maximum_non_target_attenuation_db": float(max(preservation)),
        "complete_window_count": float(len(reductions)),
    }


def source_relative_tonal_boundary_metrics(
    source_samples: np.ndarray,
    candidate_samples: np.ndarray,
    boundary_index: int,
    window_size: int,
    sample_rate_hz: int,
    target_frequency_hz: float,
    *,
    boundary_side: Literal["start", "end"],
    boundary_mode: Literal["raised_cosine_v1", "full_interval_v1"],
    boundary_transition_seconds: float,
    derivative_numerical_floor: float,
) -> dict[str, float]:
    """Measure non-tonal derivative defects around one declared boundary mode."""
    source = np.asarray(source_samples, dtype=np.float64).reshape(-1)
    candidate = np.asarray(candidate_samples, dtype=np.float64).reshape(-1)
    if (
        source.shape != candidate.shape
        or not np.all(np.isfinite(source))
        or not np.all(np.isfinite(candidate))
        or sample_rate_hz <= 0
        or not math.isfinite(target_frequency_hz)
        or not 0.0 < target_frequency_hz < sample_rate_hz / 2.0
        or boundary_side not in {"start", "end"}
        or boundary_mode not in {"raised_cosine_v1", "full_interval_v1"}
        or not math.isfinite(boundary_transition_seconds)
        or boundary_transition_seconds <= 0.0
        or not math.isfinite(derivative_numerical_floor)
        or derivative_numerical_floor <= 0.0
    ):
        raise ValueError("tonal boundary comparison parameters are invalid")
    if boundary_index < window_size or boundary_index + window_size > source.size:
        raise ValueError("tonal boundary lacks aligned bilateral 50 ms windows")

    pair_start = boundary_index - window_size
    pair_end = boundary_index + window_size
    source_pair = source[pair_start:pair_end]
    candidate_pair = candidate[pair_start:pair_end]
    pair_boundary = window_size
    defect_window_size = max(8, window_size // 10)
    defect_start = pair_boundary - defect_window_size
    defect_end = pair_boundary + defect_window_size
    residual = candidate_pair - source_pair
    relative_times = (
        np.arange(-window_size, window_size, dtype=np.float64) / sample_rate_hz
    )
    envelope = np.zeros(relative_times.size, dtype=np.float64)
    if boundary_side == "start":
        active = relative_times >= 0.0
        distance = relative_times[active]
    else:
        active = relative_times < 0.0
        distance = -relative_times[active]
    if boundary_mode == "full_interval_v1":
        envelope[active] = 1.0
    else:
        envelope[active] = 0.5 - 0.5 * np.cos(
            np.pi
            * np.minimum(distance, boundary_transition_seconds)
            / boundary_transition_seconds
        )
    phase = 2.0 * np.pi * target_frequency_hz * relative_times
    tonal_basis = np.column_stack((envelope * np.sin(phase), envelope * np.cos(phase)))
    normalized_time = relative_times / float(np.max(np.abs(relative_times)))
    design = np.column_stack(
        (np.ones(relative_times.size, dtype=np.float64), normalized_time, tonal_basis)
    )
    singular_values = np.linalg.svd(design, compute_uv=False)
    if singular_values.size != 4 or not np.all(np.isfinite(singular_values)):
        raise ValueError("tonal boundary projection basis is invalid")
    basis_tolerance = np.finfo(np.float64).eps * max(design.shape) * singular_values[0]
    if singular_values[0] <= 0.0 or singular_values[-1] <= basis_tolerance:
        raise ValueError("tonal boundary projection basis is ill-conditioned")
    coefficients, _residuals, rank, _singular = np.linalg.lstsq(
        design, residual, rcond=None
    )
    if rank != 4 or not np.all(np.isfinite(coefficients)):
        raise ValueError("tonal boundary projection basis is ill-conditioned")
    observed_peak = max(
        float(np.max(np.abs(source_pair))), float(np.max(np.abs(candidate_pair)))
    )
    coefficient_limit = 2.0 * observed_peak
    tonal_coefficients = coefficients[2:].copy()
    coefficient_norm = float(np.linalg.norm(tonal_coefficients))
    if coefficient_norm > coefficient_limit and coefficient_norm > 0.0:
        tonal_coefficients *= coefficient_limit / coefficient_norm
    remaining_residual = residual - tonal_basis @ tonal_coefficients
    source_difference = np.diff(source_pair)
    residual_difference = np.diff(remaining_residual)
    crossing_difference_index = pair_boundary - 1
    derivative_windows = (
        slice(
            crossing_difference_index - defect_window_size, crossing_difference_index
        ),
        slice(pair_boundary, pair_boundary + defect_window_size),
    )

    def derivative_rms(values: np.ndarray) -> float:
        return math.sqrt(float(np.mean(np.square(values), dtype=np.float64)))

    corrected_difference = source_difference + residual_difference
    energy_excesses: list[float] = []
    crest_excesses: list[float] = []
    for derivative_window in derivative_windows:
        source_values = source_difference[derivative_window]
        candidate_values = corrected_difference[derivative_window]
        source_rms = max(derivative_rms(source_values), derivative_numerical_floor)
        candidate_rms = max(
            derivative_rms(candidate_values), derivative_numerical_floor
        )
        source_peak = max(
            float(np.max(np.abs(source_values))), derivative_numerical_floor
        )
        candidate_peak = max(
            float(np.max(np.abs(candidate_values))), derivative_numerical_floor
        )
        energy_excesses.append(max(0.0, 20.0 * math.log10(candidate_rms / source_rms)))
        source_crest = source_peak / source_rms
        candidate_crest = candidate_peak / candidate_rms
        crest_excesses.append(
            max(0.0, 20.0 * math.log10(candidate_crest / source_crest))
        )
    cleaned_defect = remaining_residual[defect_start:defect_end]
    exact_residual_delta = abs(
        float(remaining_residual[pair_boundary])
        - float(remaining_residual[pair_boundary - 1])
    )
    maximum_residual_first_difference = float(np.max(np.abs(np.diff(cleaned_defect))))
    return {
        "energy_jump_db": max(energy_excesses),
        "crest_jump_db": max(crest_excesses),
        "adjacent_delta": max(exact_residual_delta, maximum_residual_first_difference),
    }
