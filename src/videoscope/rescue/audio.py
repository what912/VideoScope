"""Measured, bounded audio improvements for local Video Rescue.

The module deliberately accepts compact numeric observations instead of media
buffers.  FFmpeg remains the media boundary; this module validates the two-pass
``loudnorm`` measurements and creates only deterministic filter fragments.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from statistics import median
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from videoscope.rescue.models import RescueActionKind


class _AudioModel(BaseModel):
    """Strict immutable audio values suitable for confirmation binding."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _FrozenAudioDict(dict[str, float | int]):
    """A JSON-shaped parameter map that cannot change after assessment."""

    def _reject(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("frozen audio parameters do not support mutation")

    __setitem__ = _reject
    __delitem__ = _reject
    clear = _reject
    pop = _reject  # type: ignore[assignment]
    popitem = _reject  # type: ignore[assignment]
    setdefault = _reject  # type: ignore[assignment]
    update = _reject
    __ior__ = _reject  # type: ignore[assignment]


class LoudnessConfig(_AudioModel):
    """All bounds used for deterministic two-pass loudness normalization."""

    target_integrated_lufs: float = Field(
        default=-16.0, ge=-30.0, le=-10.0, allow_inf_nan=False
    )
    target_loudness_range: float = Field(
        default=11.0, gt=0.0, le=20.0, allow_inf_nan=False
    )
    true_peak_limit_dbtp: float = Field(
        default=-1.5, ge=-9.0, le=-0.1, allow_inf_nan=False
    )
    minimum_loudness_deviation_lu: float = Field(
        default=1.0, gt=0.0, le=12.0, allow_inf_nan=False
    )
    clipping_peak_threshold_dbtp: float = Field(
        default=-0.1, ge=-1.0, le=0.0, allow_inf_nan=False
    )
    clipping_ratio_threshold: float = Field(
        default=0.001, gt=0.0, le=0.1, allow_inf_nan=False
    )


class AudioDenoiseConfig(_AudioModel):
    """Noise evidence requirements and the cap applied by ``afftdn``."""

    noise_floor_threshold_dbfs: float = Field(
        default=-45.0, ge=-90.0, le=-10.0, allow_inf_nan=False
    )
    minimum_confidence: float = Field(default=0.8, ge=0.0, le=1.0, allow_inf_nan=False)
    minimum_event_count: int = Field(default=3, ge=2, le=100)
    maximum_reduction_db: float = Field(
        default=12.0, gt=0.0, le=18.0, allow_inf_nan=False
    )


class FixedOffsetConfig(_AudioModel):
    """Evidence threshold for a single, constant A/V timing correction."""

    minimum_correlation: float = Field(
        default=0.85, ge=0.0, le=1.0, allow_inf_nan=False
    )
    minimum_event_count: int = Field(default=3, ge=3, le=100)
    maximum_agreement_seconds: float = Field(
        default=0.04, gt=0.0, le=0.5, allow_inf_nan=False
    )
    maximum_absolute_offset_seconds: float = Field(
        default=2.0, gt=0.0, le=10.0, allow_inf_nan=False
    )


class LoudnessMeasurement(_AudioModel):
    """Finite FFmpeg ``loudnorm`` first-pass values used by the second pass."""

    input_i: float = Field(allow_inf_nan=False)
    input_tp: float = Field(allow_inf_nan=False)
    input_lra: float = Field(ge=0.0, allow_inf_nan=False)
    input_thresh: float = Field(allow_inf_nan=False)
    target_offset: float = Field(allow_inf_nan=False)
    clipping_ratio: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    noise_floor_dbfs: float | None = Field(default=None, allow_inf_nan=False)
    noise_confidence: float = Field(default=0.0, ge=0.0, le=1.0, allow_inf_nan=False)
    noise_event_count: int = Field(default=0, ge=0, le=100000)


class AudioAssessment(_AudioModel):
    """Neutral summary of observed audio values and evidence-led actions."""

    measurement: LoudnessMeasurement
    clipping_detected: bool
    recommended_actions: tuple[RescueActionKind, ...] = ()
    parameters: dict[str, float | int] = Field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    preview_required: bool = False
    public_explanation: str = Field(min_length=1)

    @model_validator(mode="after")
    def freeze_parameters(self) -> Self:
        object.__setattr__(self, "parameters", _FrozenAudioDict(self.parameters))
        return self


class FixedOffsetAssessment(_AudioModel):
    """A single measured A/V offset, or a neutral manual-review reason."""

    offset_seconds: float | None = Field(default=None, allow_inf_nan=False)
    shift_seconds: float | None = Field(default=None, allow_inf_nan=False)
    correlation: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    matched_event_count: int = Field(ge=0)
    agreement_seconds: float | None = Field(default=None, allow_inf_nan=False)
    reason: str | None = None
    config: FixedOffsetConfig = Field(default_factory=FixedOffsetConfig)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.offset_seconds is None:
            if self.shift_seconds is not None or self.reason is None:
                raise ValueError("unreliable offset requires one manual-review reason")
        elif self.shift_seconds != -self.offset_seconds or self.reason is not None:
            raise ValueError("fixed offset must record exactly one inverse audio shift")
        return self


def parse_loudnorm_measurement(
    payload: str | bytes | Mapping[str, object],
) -> LoudnessMeasurement:
    """Parse only the finite JSON object printed by FFmpeg's first loudnorm pass."""
    raw: object
    if isinstance(payload, Mapping):
        raw = dict(payload)
    else:
        try:
            text = (
                payload.decode("utf-8", errors="replace")
                if isinstance(payload, bytes)
                else payload
            )
            start = text.find("{")
            end = text.rfind("}")
            raw = json.loads(
                text if start < 0 or end < start else text[start : end + 1]
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("loudnorm measurement was not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("loudnorm measurement must be a JSON object")
    values: dict[str, object] = {}
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ValueError(f"loudnorm measurement is missing {key}")
        try:
            converted = float(value)
        except ValueError as exc:
            raise ValueError(f"loudnorm measurement has invalid {key}") from exc
        if not math.isfinite(converted):
            raise ValueError(f"loudnorm measurement has non-finite {key}")
        values[key] = converted
    for key in (
        "clipping_ratio",
        "noise_floor_dbfs",
        "noise_confidence",
        "noise_event_count",
    ):
        if key in raw:
            values[key] = raw[key]
    try:
        return LoudnessMeasurement.model_validate(values)
    except (TypeError, ValueError) as exc:
        raise ValueError("loudnorm measurement contains invalid finite values") from exc


def assess_audio(
    measurements: str | bytes | Mapping[str, object],
    loudness_config: LoudnessConfig,
    denoise_config: AudioDenoiseConfig | None = None,
) -> AudioAssessment:
    """Recommend only measured normalizing or repeated confident denoise actions."""
    denoise = denoise_config or AudioDenoiseConfig()
    measurement = parse_loudnorm_measurement(measurements)
    deviation = measurement.input_i - loudness_config.target_integrated_lufs
    clipping = bool(
        measurement.input_tp >= loudness_config.clipping_peak_threshold_dbtp
        or measurement.clipping_ratio >= loudness_config.clipping_ratio_threshold
    )
    actions: list[RescueActionKind] = []
    parameters: dict[str, float | int] = {
        "target_integrated_lufs": loudness_config.target_integrated_lufs,
        "target_loudness_range": loudness_config.target_loudness_range,
        "true_peak_limit_dbtp": loudness_config.true_peak_limit_dbtp,
        "loudness_deviation_lu": deviation,
        "minimum_loudness_deviation_lu": loudness_config.minimum_loudness_deviation_lu,
        "clipping_peak_threshold_dbtp": loudness_config.clipping_peak_threshold_dbtp,
        "clipping_ratio_threshold": loudness_config.clipping_ratio_threshold,
        "noise_floor_threshold_dbfs": denoise.noise_floor_threshold_dbfs,
        "minimum_noise_confidence": denoise.minimum_confidence,
        "minimum_noise_event_count": denoise.minimum_event_count,
    }
    limitations: list[str] = []
    if abs(deviation) >= loudness_config.minimum_loudness_deviation_lu or clipping:
        actions.append(RescueActionKind.NORMALIZE_AUDIO)
        parameters.update(loudness_measurement_parameters(measurement))
    noise_is_reliable = (
        measurement.noise_floor_dbfs is not None
        and measurement.noise_floor_dbfs >= denoise.noise_floor_threshold_dbfs
        and measurement.noise_confidence >= denoise.minimum_confidence
        and measurement.noise_event_count >= denoise.minimum_event_count
    )
    if noise_is_reliable:
        noise_floor = measurement.noise_floor_dbfs
        assert noise_floor is not None
        actions.append(RescueActionKind.DENOISE_AUDIO)
        parameters.update(
            {
                "noise_floor_dbfs": noise_floor,
                "maximum_reduction_db": denoise.maximum_reduction_db,
            }
        )
    elif measurement.noise_floor_dbfs is not None and (
        measurement.noise_floor_dbfs >= denoise.noise_floor_threshold_dbfs
    ):
        limitations.append("noise_evidence_is_not_reliable")
    return AudioAssessment(
        measurement=measurement,
        clipping_detected=clipping,
        recommended_actions=tuple(actions),
        parameters=parameters,
        limitations=tuple(limitations),
        preview_required=bool(actions),
        public_explanation=(
            "Measured audio values support a preview-only adjustment."
            if actions
            else "Measured audio values do not support an automatic adjustment."
        ),
    )


def loudness_measurement_parameters(
    measurement: LoudnessMeasurement,
) -> dict[str, float]:
    """Return the exact finite first-pass values bound into a confirmed action."""
    return {
        "measured_I": measurement.input_i,
        "measured_TP": measurement.input_tp,
        "measured_LRA": measurement.input_lra,
        "measured_thresh": measurement.input_thresh,
        "offset": measurement.target_offset,
    }


def loudnorm_measurement_filter(config: LoudnessConfig) -> str:
    """Return the deterministic first-pass FFmpeg loudnorm filter."""
    return (
        "loudnorm=I="
        + _number(config.target_integrated_lufs)
        + ":LRA="
        + _number(config.target_loudness_range)
        + ":TP="
        + _number(config.true_peak_limit_dbtp)
        + ":print_format=json"
    )


def loudnorm_apply_filter(
    measurement: LoudnessMeasurement, config: LoudnessConfig
) -> str:
    """Return the bounded second-pass loudnorm filter using only measured values."""
    values = loudness_measurement_parameters(measurement)
    return (
        "loudnorm=I="
        + _number(config.target_integrated_lufs)
        + ":LRA="
        + _number(config.target_loudness_range)
        + ":TP="
        + _number(config.true_peak_limit_dbtp)
        + ":measured_I="
        + _number(values["measured_I"])
        + ":measured_TP="
        + _number(values["measured_TP"])
        + ":measured_LRA="
        + _number(values["measured_LRA"])
        + ":measured_thresh="
        + _number(values["measured_thresh"])
        + ":offset="
        + _number(values["offset"])
        + ":linear=true:print_format=summary"
    )


def denoise_filter_fragment(
    noise_floor_dbfs: float, maximum_reduction_db: float
) -> str:
    """Return a finite, capped ``afftdn`` filter fragment."""
    config = AudioDenoiseConfig(maximum_reduction_db=maximum_reduction_db)
    if not math.isfinite(noise_floor_dbfs) or not -90.0 <= noise_floor_dbfs <= -10.0:
        raise ValueError("noise floor must be finite and within configured bounds")
    return (
        "afftdn=nr="
        + _number(config.maximum_reduction_db)
        + ":nf="
        + _number(noise_floor_dbfs)
    )


def fixed_offset_filter_fragment(
    shift_seconds: float, config: FixedOffsetConfig
) -> str:
    """Shift audio once; a positive observed offset means audio lags."""
    if (
        not math.isfinite(shift_seconds)
        or abs(shift_seconds) > config.maximum_absolute_offset_seconds
    ):
        raise ValueError("fixed offset shift is outside the configured bound")
    return (
        "asetpts=PTS"
        + ("+" if shift_seconds >= 0 else "")
        + _number(shift_seconds)
        + "/TB"
    )


def audio_filter_fragment_from_actions(
    actions: Sequence[RescueActionKind], parameters: Mapping[str, object]
) -> str | None:
    """Revalidate confirmed values before turning them into one FFmpeg argument."""
    fragments: list[str] = []
    try:
        if RescueActionKind.NORMALIZE_AUDIO in actions:
            measurement = LoudnessMeasurement.model_validate(
                {
                    "input_i": parameters["measured_I"],
                    "input_tp": parameters["measured_TP"],
                    "input_lra": parameters["measured_LRA"],
                    "input_thresh": parameters["measured_thresh"],
                    "target_offset": parameters["offset"],
                }
            )
            config = LoudnessConfig(
                target_integrated_lufs=_float_parameter(
                    parameters, "target_integrated_lufs"
                ),
                target_loudness_range=_float_parameter(
                    parameters, "target_loudness_range"
                ),
                true_peak_limit_dbtp=_float_parameter(
                    parameters, "true_peak_limit_dbtp"
                ),
            )
            fragments.append(loudnorm_apply_filter(measurement, config))
        if RescueActionKind.DENOISE_AUDIO in actions:
            fragments.append(
                denoise_filter_fragment(
                    _float_parameter(parameters, "noise_floor_dbfs"),
                    _float_parameter(parameters, "maximum_reduction_db"),
                )
            )
        if RescueActionKind.CORRECT_FIXED_AV_OFFSET in actions:
            fixed_offset_config = FixedOffsetConfig(
                minimum_correlation=_float_parameter(parameters, "minimum_correlation"),
                minimum_event_count=_int_parameter(parameters, "minimum_event_count"),
                maximum_agreement_seconds=_float_parameter(
                    parameters, "maximum_agreement_seconds"
                ),
                maximum_absolute_offset_seconds=_float_parameter(
                    parameters, "maximum_absolute_offset_seconds"
                ),
            )
            offset_seconds = _float_parameter(parameters, "offset_seconds")
            shift_seconds = _float_parameter(parameters, "audio_shift_seconds")
            correlation = _float_parameter(parameters, "correlation")
            agreement_seconds = _float_parameter(parameters, "agreement_seconds")
            matched_event_count = _int_parameter(parameters, "matched_event_count")
            if (
                not math.isclose(
                    shift_seconds, -offset_seconds, rel_tol=0.0, abs_tol=1e-12
                )
                or abs(offset_seconds)
                > fixed_offset_config.maximum_absolute_offset_seconds
                or correlation < fixed_offset_config.minimum_correlation
                or matched_event_count < fixed_offset_config.minimum_event_count
                or agreement_seconds > fixed_offset_config.maximum_agreement_seconds
            ):
                return None
            fragments.append(
                fixed_offset_filter_fragment(
                    shift_seconds,
                    fixed_offset_config,
                )
            )
    except (KeyError, TypeError, ValueError, ValidationError):
        return None
    return ",".join(fragments) or None


def measure_fixed_av_offset(
    audio_events: Sequence[tuple[float, float]],
    video_events: Sequence[tuple[float, float]],
    config: FixedOffsetConfig,
) -> FixedOffsetAssessment:
    """Accept only one constant offset from repeated high-confidence paired events."""
    paired = tuple(zip(audio_events, video_events))
    valid = tuple(
        (
            float(audio_time) - float(video_time),
            min(float(audio_conf), float(video_conf)),
        )
        for (audio_time, audio_conf), (video_time, video_conf) in paired
        if _finite_event(audio_time, audio_conf)
        and _finite_event(video_time, video_conf)
    )
    high_confidence = tuple(
        offset
        for offset, confidence in valid
        if confidence >= config.minimum_correlation
    )
    correlation = (
        sum(confidence for _offset, confidence in valid) / len(valid) if valid else 0.0
    )
    if len(valid) < config.minimum_event_count:
        return _no_offset(
            correlation,
            len(high_confidence),
            "insufficient_event_count",
            config=config,
        )
    if correlation < config.minimum_correlation:
        return _no_offset(
            correlation,
            len(high_confidence),
            "insufficient_correlation",
            config=config,
        )
    if len(high_confidence) < config.minimum_event_count:
        return _no_offset(
            correlation,
            len(high_confidence),
            "insufficient_event_count",
            config=config,
        )
    offset = float(median(high_confidence))
    agreement = max(abs(value - offset) for value in high_confidence)
    if agreement > config.maximum_agreement_seconds:
        return _no_offset(
            correlation,
            len(high_confidence),
            "offset_not_constant",
            agreement,
            config,
        )
    if abs(offset) > config.maximum_absolute_offset_seconds:
        return _no_offset(
            correlation,
            len(high_confidence),
            "offset_outside_configured_bound",
            agreement,
            config,
        )
    return FixedOffsetAssessment(
        offset_seconds=offset,
        shift_seconds=-offset,
        correlation=correlation,
        matched_event_count=len(high_confidence),
        agreement_seconds=agreement,
        config=config,
    )


def _no_offset(
    correlation: float,
    count: int,
    reason: str,
    agreement: float | None = None,
    config: FixedOffsetConfig | None = None,
) -> FixedOffsetAssessment:
    return FixedOffsetAssessment(
        correlation=max(0.0, min(1.0, correlation)),
        matched_event_count=count,
        agreement_seconds=agreement,
        reason=reason,
        config=config or FixedOffsetConfig(),
    )


def _finite_event(timestamp: float, confidence: float) -> bool:
    return (
        not isinstance(timestamp, bool)
        and not isinstance(confidence, bool)
        and math.isfinite(float(timestamp))
        and math.isfinite(float(confidence))
        and timestamp >= 0
        and 0 <= confidence <= 1
    )


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("audio filter values must be finite")
    return format(value, ".6f").rstrip("0").rstrip(".") or "0"


def _float_parameter(parameters: Mapping[str, object], key: str) -> float:
    value = parameters[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"audio action parameter {key} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"audio action parameter {key} must be finite")
    return result


def _int_parameter(parameters: Mapping[str, object], key: str) -> int:
    value = parameters[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"audio action parameter {key} must be an integer")
    return value


__all__ = [
    "AudioAssessment",
    "AudioDenoiseConfig",
    "FixedOffsetAssessment",
    "FixedOffsetConfig",
    "LoudnessConfig",
    "LoudnessMeasurement",
    "assess_audio",
    "audio_filter_fragment_from_actions",
    "denoise_filter_fragment",
    "fixed_offset_filter_fragment",
    "loudnorm_apply_filter",
    "loudnorm_measurement_filter",
    "measure_fixed_av_offset",
    "parse_loudnorm_measurement",
]
