"""Strict full-range encoded-candidate qualification for tonal restoration."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from videoscope.rescue.errors import RescueCancelledError, RescueMediaError
from videoscope.rescue.models import RescueModel
from videoscope.rescue.qualification import validate_path_free_canonical_json
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_TIMELINE_PROBE_BYTES = 64 * 1024
TONAL_ENCODED_QUALIFICATION_VERSION = "3"
TONAL_ENCODED_QUALIFICATION_LIMITATION = (
    "Tonal interference reduction was omitted because no full-range encoded "
    "candidate passed every unchanged tonal verification gate."
)
TONAL_ENCODED_QUALIFICATION_UNAVAILABLE_LIMITATION = (
    "Tonal interference reduction was omitted because full-range encoded "
    "candidate qualification was unavailable."
)


def _json_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(tuple(item) if isinstance(item, list) else item for item in value)
    return value


def _validated_ranges(
    ranges: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    normalized = tuple((float(start), float(end)) for start, end in ranges)
    if (
        not normalized
        or tuple(sorted(normalized)) != normalized
        or any(
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0.0
            or end <= start
            for start, end in normalized
        )
        or any(
            current[0] < previous[1]
            for previous, current in zip(normalized, normalized[1:])
        )
    ):
        raise ValueError("tonal qualification ranges are invalid")
    return normalized


class TonalEncodedMetricsV2(RescueModel):
    """One final-provider measurement over an exact encoded candidate range."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    range_coverage_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    measured_windows: int = Field(ge=1, strict=True)
    excluded_transition_windows: int = Field(ge=0, strict=True)
    minimum_target_reduction_db: float = Field(allow_inf_nan=False)
    minimum_target_margin_db: float = Field(allow_inf_nan=False)
    maximum_non_target_attenuation_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_energy_jump_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_crest_jump_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_adjacent_delta: float = Field(ge=0.0, allow_inf_nan=False)

    def passes(self, thresholds: TonalEncodedThresholdsV2) -> bool:
        return bool(
            math.isclose(self.range_coverage_ratio, 1.0, rel_tol=0.0, abs_tol=1e-9)
            and self.minimum_target_reduction_db
            >= thresholds.minimum_target_reduction_db
            and self.minimum_target_margin_db >= 0.0
            and self.maximum_non_target_attenuation_db
            <= thresholds.maximum_non_target_attenuation_db
            and self.maximum_boundary_energy_jump_db
            <= thresholds.maximum_boundary_energy_jump_db
            and self.maximum_boundary_crest_jump_db
            <= thresholds.maximum_boundary_crest_jump_db
            and self.maximum_boundary_adjacent_delta
            <= thresholds.maximum_boundary_adjacent_delta
        )


class TonalEncodedThresholdsV2(RescueModel):
    """Every unchanged final tonal gate bound beside one encoded result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    minimum_target_reduction_db: float = Field(gt=0.0, allow_inf_nan=False)
    maximum_non_target_attenuation_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_energy_jump_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_crest_jump_db: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_boundary_adjacent_delta: float = Field(ge=0.0, allow_inf_nan=False)


class TonalAudioEncodeContractV2(RescueModel):
    """Requested AAC generations used by qualification and final execution."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2"] = "2"
    parent_codec: Literal["aac"] = "aac"
    parent_bitrate_kbps: int = Field(ge=1, strict=True)
    candidate_codec: Literal["aac"] = "aac"
    candidate_bitrate_kbps: int = Field(ge=1, strict=True)
    sample_rate_policy: Literal["preserve_parent"] = "preserve_parent"
    channel_layout_policy: Literal["preserve_parent"] = "preserve_parent"


class TonalAudioTopologyV2(RescueModel):
    """Canonical decoded ffprobe audio-stream topology plus its digest."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    codec_name: Literal["aac"] = "aac"
    codec_tag_string: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    sample_fmt: str = Field(min_length=1)
    sample_rate_hz: int = Field(ge=8000, le=384000, strict=True)
    channels: int = Field(ge=1, le=32, strict=True)
    channel_layout: str = Field(min_length=1)
    time_base: str = Field(pattern=r"^[1-9][0-9]*/[1-9][0-9]*$")
    topology_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        values = self.model_dump(mode="json", exclude={"topology_sha256"})
        encoded = json.dumps(
            values,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.topology_sha256:
            raise ValueError("tonal audio topology digest differs")
        return self


class TonalAudioTimelineV1(RescueModel):
    """Path-free normalized AAC packet inventory used for exact alignment."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    packet_count: int = Field(ge=1, strict=True)
    first_normalized_pts_seconds: float = Field(allow_inf_nan=False)
    last_normalized_pts_seconds: float = Field(allow_inf_nan=False)
    normalized_pts_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.last_normalized_pts_seconds < self.first_normalized_pts_seconds:
            raise ValueError("tonal audio timeline is reversed")
        return self


class TonalRangeMappingV2(RescueModel):
    """Path-free exact source/output mapping used by the final provider."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_start: float = Field(ge=0.0, allow_inf_nan=False)
    source_end: float = Field(gt=0.0, allow_inf_nan=False)
    output_start: float = Field(ge=0.0, allow_inf_nan=False)
    output_end: float = Field(gt=0.0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_exact_duration(self) -> Self:
        source_duration = self.source_end - self.source_start
        output_duration = self.output_end - self.output_start
        if source_duration <= 0.0 or not math.isclose(
            source_duration, output_duration, rel_tol=0.0, abs_tol=1e-9
        ):
            raise ValueError("tonal qualification mapping is not exact")
        return self


class TonalEncodedCandidateAttemptV2(RescueModel):
    """Path-free result for one configured Q value and one measured profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    notch_q: float = Field(gt=1.0, le=100.0, allow_inf_nan=False)
    notch_pass_count: int = Field(default=1, ge=1, le=2, strict=True)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_audio_topology: TonalAudioTopologyV2
    metrics: TonalEncodedMetricsV2
    thresholds: TonalEncodedThresholdsV2

    @property
    def passed(self) -> bool:
        return _metrics_match_thresholds(self.metrics, self.thresholds) and (
            self.metrics.passes(self.thresholds)
        )


class TonalEncodedProfileQualificationV2(RescueModel):
    """Ordered attempts proving the first encoded Q that passes one profile."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile_index: int = Field(ge=0, strict=True)
    attempts: tuple[TonalEncodedCandidateAttemptV2, ...] = Field(min_length=1)
    selected_notch_q: float | None = Field(
        default=None, gt=1.0, le=100.0, allow_inf_nan=False
    )
    selected_notch_pass_count: int | None = Field(default=None, ge=1, le=2, strict=True)

    @field_validator("attempts", mode="before")
    @classmethod
    def accept_json_attempts(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_attempt_inventory(self) -> Self:
        values = tuple((item.notch_q, item.notch_pass_count) for item in self.attempts)
        if len(values) != len(set(values)):
            raise ValueError("duplicate tonal encoded candidate profile")
        hashes = tuple(item.candidate_sha256 for item in self.attempts)
        if len(hashes) != len(set(hashes)):
            raise ValueError("duplicate tonal encoded candidate artifact")
        if self.selected_notch_q is not None and self.selected_notch_pass_count is None:
            object.__setattr__(self, "selected_notch_pass_count", 1)
        if self.selected_notch_q is None and self.selected_notch_pass_count is not None:
            object.__setattr__(self, "selected_notch_pass_count", None)
        if (self.selected_notch_q is None) != (self.selected_notch_pass_count is None):
            raise ValueError("selected tonal profile is incomplete")
        if self.selected_notch_q is not None:
            selected = (self.selected_notch_q, self.selected_notch_pass_count)
            if selected not in values:
                raise ValueError(
                    "selected tonal profile has no encoded candidate evidence"
                )
        return self


class _TonalEncodedQualificationEvidenceBase(RescueModel):
    """Fields shared by versioned encoded qualification evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    measurement_algorithm_version: Literal["tonal-final-v1"] = "tonal-final-v1"
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    draft_action_id: str = Field(min_length=1)
    draft_parameters: dict[str, JsonValue]
    source_ranges: tuple[tuple[float, float], ...]
    output_ranges: tuple[tuple[float, float], ...]
    range_mappings: tuple[TonalRangeMappingV2, ...] = Field(min_length=1)
    audio_encode_contract: TonalAudioEncodeContractV2
    parent_sha256: str = Field(pattern=_SHA256_PATTERN)
    parent_audio_topology: TonalAudioTopologyV2
    profile_qualifications: tuple[TonalEncodedProfileQualificationV2, ...] = Field(
        min_length=1
    )
    combined_candidate_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    combined_audio_topology: TonalAudioTopologyV2 | None = None
    combined_metrics: tuple[TonalEncodedMetricsV2, ...] = ()
    combined_thresholds: tuple[TonalEncodedThresholdsV2, ...] = ()
    selected_profiles: tuple[InterferenceTone, ...] = ()
    limitation: str | None = Field(default=None, min_length=1)

    @field_validator("source_ranges", "output_ranges", mode="before")
    @classmethod
    def accept_json_ranges(cls, value: object) -> object:
        return _json_tuple(value)

    @field_validator("draft_parameters")
    @classmethod
    def validate_path_free_draft_parameters(
        cls, value: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        validate_path_free_canonical_json(
            value, field_name="tonal qualification draft parameters"
        )
        return value

    @field_validator(
        "profile_qualifications",
        "combined_metrics",
        "combined_thresholds",
        "range_mappings",
        mode="before",
    )
    @classmethod
    def accept_json_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @field_validator("selected_profiles", mode="before")
    @classmethod
    def accept_json_profiles(cls, value: object) -> object:
        if not isinstance(value, (list, tuple)):
            return value
        return tuple(
            item
            if isinstance(item, InterferenceTone)
            else InterferenceTone.model_validate_json(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
            for item in value
        )

    @model_validator(mode="after")
    def validate_selection_shape(self) -> Self:
        object.__setattr__(self, "source_ranges", _validated_ranges(self.source_ranges))
        object.__setattr__(self, "output_ranges", _validated_ranges(self.output_ranges))
        mappings = tuple(self.range_mappings)
        if tuple(sorted(mappings, key=lambda item: item.source_start)) != mappings:
            raise ValueError("tonal qualification mappings are not ordered")
        if any(
            current.source_start < previous.source_end
            or current.output_start < previous.output_end
            for previous, current in zip(mappings, mappings[1:])
        ):
            raise ValueError("tonal qualification mappings overlap")
        indices = tuple(item.profile_index for item in self.profile_qualifications)
        if indices != tuple(range(len(indices))):
            raise ValueError("tonal profile qualification order is invalid")
        if any(
            attempt.candidate_sha256 == self.parent_sha256
            for qualification in self.profile_qualifications
            for attempt in qualification.attempts
        ):
            raise ValueError("tonal candidate must be a newly encoded artifact")
        passing = bool(self.selected_profiles)
        if passing:
            if (
                self.combined_candidate_sha256 is None
                or self.combined_audio_topology is None
                or self.combined_candidate_sha256 == self.parent_sha256
                or len(self.combined_metrics) != len(self.selected_profiles)
                or len(self.combined_thresholds) != len(self.selected_profiles)
                or len(self.profile_qualifications) != len(self.selected_profiles)
                or self.limitation is not None
            ):
                raise ValueError("passing tonal qualification evidence is incomplete")
        elif (
            self.limitation != TONAL_ENCODED_QUALIFICATION_LIMITATION
            or self.combined_candidate_sha256 is not None
            or self.combined_audio_topology is not None
            or self.combined_metrics
            or self.combined_thresholds
        ):
            raise ValueError("failed tonal qualification requires its limitation")
        return self

    @property
    def passed(self) -> bool:
        return bool(self.selected_profiles)


class TonalEncodedQualificationEvidenceV2(_TonalEncodedQualificationEvidenceBase):
    """Legacy qualification evidence without a boundary identity control."""

    schema_version: Literal["2"] = "2"


class TonalEncodedQualificationEvidenceV3(_TonalEncodedQualificationEvidenceBase):
    """V3 binds the same-generation identity used by boundary measurements."""

    schema_version: Literal["3"] = "3"
    boundary_control_sha256: str = Field(pattern=_SHA256_PATTERN)
    boundary_control_audio_topology: TonalAudioTopologyV2
    boundary_control_audio_timeline: TonalAudioTimelineV1
    profile_candidate_audio_timelines: tuple[tuple[TonalAudioTimelineV1, ...], ...]
    combined_audio_timeline: TonalAudioTimelineV1 | None = None

    @field_validator("profile_candidate_audio_timelines", mode="before")
    @classmethod
    def accept_json_timeline_inventory(cls, value: object) -> object:
        if isinstance(value, list):
            return tuple(tuple(items) for items in value)
        return value

    @model_validator(mode="after")
    def validate_boundary_control(self) -> Self:
        if self.boundary_control_sha256 == self.parent_sha256:
            raise ValueError("tonal boundary control must be a new encoded artifact")
        if self.boundary_control_audio_topology != self.parent_audio_topology:
            raise ValueError("tonal boundary control topology differs from parent")
        if (
            any(
                attempt.candidate_sha256 == self.boundary_control_sha256
                for qualification in self.profile_qualifications
                for attempt in qualification.attempts
            )
            or self.combined_candidate_sha256 == self.boundary_control_sha256
        ):
            raise ValueError("tonal boundary control must differ from candidates")
        if len(self.profile_candidate_audio_timelines) != len(
            self.profile_qualifications
        ) or any(
            len(timelines) != len(qualification.attempts)
            for timelines, qualification in zip(
                self.profile_candidate_audio_timelines,
                self.profile_qualifications,
                strict=True,
            )
        ):
            raise ValueError("tonal candidate timeline inventory differs")
        if any(
            timeline != self.boundary_control_audio_timeline
            for timelines in self.profile_candidate_audio_timelines
            for timeline in timelines
        ):
            raise ValueError("tonal candidate timeline differs from control")
        if self.passed:
            if self.combined_audio_timeline != self.boundary_control_audio_timeline:
                raise ValueError("combined tonal timeline differs from control")
        elif self.combined_audio_timeline is not None:
            raise ValueError("failed tonal qualification has a combined timeline")
        return self


def _finite(raw: Mapping[str, JsonValue], key: str) -> float:
    value = raw.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError("tonal encoded measurement is incomplete")
    return float(value)


def _metrics_match_thresholds(
    metrics: TonalEncodedMetricsV2, thresholds: TonalEncodedThresholdsV2
) -> bool:
    return math.isclose(
        metrics.minimum_target_margin_db,
        metrics.minimum_target_reduction_db - thresholds.minimum_target_reduction_db,
        rel_tol=0.0,
        abs_tol=1e-9,
    )


def _has_complete_window_inventory(
    profile: InterferenceTone, metrics: TonalEncodedMetricsV2
) -> bool:
    qualification = profile.render_qualification
    return bool(
        qualification is not None
        and metrics.measured_windows == qualification.complete_window_count
        and metrics.excluded_transition_windows == 0
    )


def audio_topology_from_ffprobe_stdout(stdout: str) -> TonalAudioTopologyV2:
    """Parse exactly one selected AAC stream without accepting loose coercions."""
    try:
        payload = json.loads(stdout)
        streams = payload.get("streams") if isinstance(payload, dict) else None
        if not isinstance(streams, list) or len(streams) != 1:
            raise ValueError
        stream = streams[0]
        if not isinstance(stream, dict):
            raise ValueError
        raw_sample_rate = stream.get("sample_rate")
        if (
            isinstance(raw_sample_rate, bool)
            or not isinstance(raw_sample_rate, (str, int))
            or not str(raw_sample_rate).isdigit()
        ):
            raise ValueError
        channels = stream.get("channels")
        if isinstance(channels, bool) or not isinstance(channels, int):
            raise ValueError
        topology: dict[str, JsonValue] = {
            "codec_name": stream.get("codec_name"),
            "codec_tag_string": stream.get("codec_tag_string"),
            "profile": stream.get("profile"),
            "sample_fmt": stream.get("sample_fmt"),
            "sample_rate_hz": int(raw_sample_rate),
            "channels": channels,
            "channel_layout": stream.get("channel_layout"),
            "time_base": stream.get("time_base"),
        }
        encoded = json.dumps(
            topology,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        topology["topology_sha256"] = hashlib.sha256(encoded).hexdigest()
        return TonalAudioTopologyV2.model_validate(topology)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("tonal audio topology probe is incomplete") from exc


def tonal_audio_topology_probe_arguments(
    path: Path, *, ffprobe: str = "ffprobe"
) -> tuple[str, ...]:
    return (
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        (
            "stream=codec_name,codec_tag_string,profile,sample_fmt,"
            "sample_rate,channels,channel_layout,time_base"
        ),
        "-of",
        "json",
        str(Path(path)),
    )


def audio_timeline_from_ffprobe_stdout(stdout: str) -> TonalAudioTimelineV1:
    """Parse one exact packet inventory normalized only by stream start time."""
    try:
        packet_values: list[object]
        if stdout.startswith("{"):
            payload = json.loads(stdout)
            streams = payload.get("streams") if isinstance(payload, dict) else None
            packets = payload.get("packets") if isinstance(payload, dict) else None
            if (
                not isinstance(streams, list)
                or len(streams) != 1
                or not isinstance(streams[0], dict)
                or not isinstance(packets, list)
                or not packets
            ):
                raise ValueError
            start_raw = streams[0].get("start_time")
            packet_values = []
            for packet in packets:
                if not isinstance(packet, dict):
                    raise ValueError
                packet_values.append(packet.get("pts_time"))
        else:
            if (
                not stdout.endswith(("\n", "\r"))
                or len(stdout.encode("utf-8")) > _MAX_TIMELINE_PROBE_BYTES
            ):
                raise ValueError
            lines = stdout.splitlines()
            if len(lines) < 2:
                raise ValueError
            stream_fields = lines[-1].split("|")
            if len(stream_fields) != 2 or stream_fields[0] != "stream":
                raise ValueError
            start_raw = stream_fields[1]
            packet_values = []
            for line in lines[:-1]:
                fields = line.split("|")
                if (
                    len(fields) not in {2, 3}
                    or fields[0] != "packet"
                    or (len(fields) == 3 and fields[2] != "")
                ):
                    raise ValueError
                packet_values.append(fields[1])
        if isinstance(start_raw, bool) or not isinstance(start_raw, (str, int, float)):
            raise ValueError
        start = Decimal(str(start_raw))
        if not start.is_finite():
            raise ValueError
        normalized: list[Decimal] = []
        tokens: list[str] = []
        for raw in packet_values:
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                raise ValueError
            value = Decimal(str(raw)) - start
            if not value.is_finite():
                raise ValueError
            normalized.append(value)
            tokens.append(format(value.normalize(), "f"))
        if any(
            current <= previous for previous, current in zip(normalized, normalized[1:])
        ):
            raise ValueError
        encoded = json.dumps(tokens, separators=(",", ":")).encode("ascii")
        return TonalAudioTimelineV1(
            packet_count=len(normalized),
            first_normalized_pts_seconds=float(normalized[0]),
            last_normalized_pts_seconds=float(normalized[-1]),
            normalized_pts_sha256=hashlib.sha256(encoded).hexdigest(),
        )
    except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("tonal audio timeline probe is incomplete") from exc


def tonal_audio_timeline_probe_arguments(
    path: Path, *, ffprobe: str = "ffprobe"
) -> tuple[str, ...]:
    return (
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=start_time:packet=pts_time",
        "-of",
        "compact=p=1:nk=1",
        str(Path(path)),
    )


def metrics_from_provider(
    raw: Mapping[str, JsonValue],
    *,
    target_db: float,
    profile_index: int | None = None,
) -> TonalEncodedMetricsV2:
    prefix = "" if profile_index is None else f"profile_{profile_index}_"
    reduction = _finite(raw, f"{prefix}minimum_target_reduction_db")
    windows = _finite(raw, f"{prefix}measured_windows")
    excluded = _finite(raw, f"{prefix}excluded_transition_windows")
    if not windows.is_integer() or not excluded.is_integer():
        raise ValueError("tonal encoded window inventory is invalid")
    return TonalEncodedMetricsV2(
        range_coverage_ratio=_finite(raw, "range_coverage_ratio"),
        measured_windows=int(windows),
        excluded_transition_windows=int(excluded),
        minimum_target_reduction_db=reduction,
        minimum_target_margin_db=reduction - target_db,
        maximum_non_target_attenuation_db=_finite(
            raw, f"{prefix}maximum_non_target_attenuation_db"
        ),
        maximum_boundary_energy_jump_db=_finite(
            raw, f"{prefix}maximum_boundary_energy_jump_db"
        ),
        maximum_boundary_crest_jump_db=_finite(
            raw, f"{prefix}maximum_boundary_crest_jump_db"
        ),
        maximum_boundary_adjacent_delta=_finite(
            raw, f"{prefix}maximum_boundary_adjacent_delta"
        ),
    )


def _thresholds_for_profile(
    profile: InterferenceTone, config: TonalInterferenceConfig
) -> TonalEncodedThresholdsV2:
    return TonalEncodedThresholdsV2(
        minimum_target_reduction_db=profile.attenuation_target_db,
        maximum_non_target_attenuation_db=(config.max_non_target_band_attenuation_db),
        maximum_boundary_energy_jump_db=config.max_boundary_energy_jump_db,
        maximum_boundary_crest_jump_db=config.max_boundary_crest_jump_db,
        maximum_boundary_adjacent_delta=config.max_boundary_adjacent_delta,
    )


def _audio_encode_contract(
    plan: Any, config: TonalInterferenceConfig
) -> TonalAudioEncodeContractV2:
    return TonalAudioEncodeContractV2(
        parent_bitrate_kbps=plan.effective_config.improved_audio_bitrate_kbps,
        candidate_bitrate_kbps=config.audio_bitrate_kbps,
    )


def _path_free_mappings(mappings: Sequence[Any]) -> tuple[TonalRangeMappingV2, ...]:
    return tuple(
        TonalRangeMappingV2(
            source_start=mapping.source_start,
            source_end=mapping.source_end,
            output_start=mapping.output_start,
            output_end=mapping.output_end,
        )
        for mapping in mappings
    )


def _map_ranges_from_evidence(
    source_ranges: tuple[tuple[float, float], ...],
    mappings: tuple[TonalRangeMappingV2, ...],
) -> tuple[tuple[float, float], ...]:
    output: list[tuple[float, float]] = []
    for start, end in source_ranges:
        matching = tuple(
            mapping
            for mapping in mappings
            if mapping.source_start <= start and end <= mapping.source_end
        )
        if len(matching) != 1:
            raise ValueError("tonal qualification range is not exactly retained")
        mapping = matching[0]
        output.append(
            (
                mapping.output_start + start - mapping.source_start,
                mapping.output_start + end - mapping.source_start,
            )
        )
    return _validated_ranges(output)


def validate_tonal_runtime_parent(
    evidence: TonalEncodedQualificationEvidenceV3,
    mappings: Sequence[Any],
    *,
    parent_sha256: str,
    parent_audio_topology: TonalAudioTopologyV2,
) -> None:
    observed_mappings = _path_free_mappings(mappings)
    if (
        observed_mappings != evidence.range_mappings
        or _map_ranges_from_evidence(evidence.source_ranges, observed_mappings)
        != evidence.output_ranges
        or parent_sha256 != evidence.parent_sha256
        or parent_audio_topology != evidence.parent_audio_topology
    ):
        raise ValueError("tonal encoded qualification parent differs from execution")


def validate_tonal_runtime_candidate(
    evidence: TonalEncodedQualificationEvidenceV3,
    *,
    candidate_sha256: str,
    candidate_audio_topology: TonalAudioTopologyV2,
) -> None:
    if (
        evidence.combined_candidate_sha256 is None
        or evidence.combined_audio_topology is None
        or candidate_sha256 != evidence.combined_candidate_sha256
        or candidate_audio_topology != evidence.combined_audio_topology
    ):
        raise ValueError("tonal encoded candidate differs from qualification")


def _qualification_for_q(
    tone: InterferenceTone,
    notch_q: float,
    metrics: TonalEncodedMetricsV2 | None = None,
    notch_pass_count: int = 1,
) -> InterferenceTone:
    raw = tone.render_qualification
    if raw is None:
        raise ValueError("raw tonal profile qualification is missing")
    measured = metrics or TonalEncodedMetricsV2(
        range_coverage_ratio=1.0,
        measured_windows=raw.complete_window_count,
        excluded_transition_windows=0,
        minimum_target_reduction_db=tone.attenuation_target_db,
        minimum_target_margin_db=0.0,
        maximum_non_target_attenuation_db=0.0,
        maximum_boundary_energy_jump_db=0.0,
        maximum_boundary_crest_jump_db=0.0,
        maximum_boundary_adjacent_delta=0.0,
    )
    return tone.model_copy(
        update={
            "render_qualification": TonalRenderQualification(
                boundary_mode="full_interval_v1",
                notch_q=notch_q,
                notch_pass_count=notch_pass_count,
                complete_window_count=measured.measured_windows,
                minimum_target_reduction_db=measured.minimum_target_reduction_db,
                maximum_non_target_attenuation_db=(
                    measured.maximum_non_target_attenuation_db
                ),
                maximum_boundary_energy_jump_db=(
                    measured.maximum_boundary_energy_jump_db
                ),
                maximum_boundary_crest_jump_db=(
                    measured.maximum_boundary_crest_jump_db
                ),
                maximum_boundary_adjacent_delta=(
                    measured.maximum_boundary_adjacent_delta
                ),
            )
        }
    )


def qualified_tonal_action_parameters(
    evidence: TonalEncodedQualificationEvidenceV3,
) -> dict[str, JsonValue]:
    if not evidence.passed:
        raise ValueError("failed tonal qualification has no action parameters")
    parameters = dict(evidence.draft_parameters)
    if "encoded_candidate_qualification" in parameters:
        raise ValueError("tonal qualification draft parameters are recursive")
    parameters["interference_profiles"] = [
        profile.model_dump(mode="json") for profile in evidence.selected_profiles
    ]
    parameters["encoded_qualification_version"] = TONAL_ENCODED_QUALIFICATION_VERSION
    parameters["encoded_candidate_qualification"] = evidence.model_dump(mode="json")
    return parameters


def validate_encoded_tonal_qualification(
    plan: Any,
    action: Any,
    config: TonalInterferenceConfig,
    profiles: tuple[InterferenceTone, ...],
) -> TonalEncodedQualificationEvidenceV3:
    from videoscope.rescue.models import make_rescue_action_id

    try:
        evidence = TonalEncodedQualificationEvidenceV3.model_validate_json(
            json.dumps(
                action.parameters.get("encoded_candidate_qualification"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "encoded candidate qualification is missing or invalid"
        ) from exc
    if not evidence.passed:
        raise ValueError("final tonal action has no passing encoded candidate")
    if (
        evidence.input_hash != plan.input_hash
        or evidence.source_ranges != action.source_ranges
        or evidence.output_ranges
        != _map_ranges_from_evidence(action.source_ranges, evidence.range_mappings)
        or evidence.audio_encode_contract != _audio_encode_contract(plan, config)
        or tuple(evidence.selected_profiles) != profiles
        or action.parameters.get("encoded_qualification_version")
        != TONAL_ENCODED_QUALIFICATION_VERSION
    ):
        raise ValueError("encoded candidate qualification differs from the plan")
    draft_parameters = dict(evidence.draft_parameters)
    if any(str(key).startswith("encoded_qualification") for key in draft_parameters):
        raise ValueError("encoded tonal qualification draft is recursive")
    raw_draft_profiles = draft_parameters.get("interference_profiles")
    if not isinstance(raw_draft_profiles, (list, tuple)):
        raise ValueError("encoded tonal qualification profile inventory differs")
    draft_profiles = tuple(
        InterferenceTone.model_validate_json(json.dumps(item, ensure_ascii=False))
        for item in raw_draft_profiles
    )
    if len(draft_profiles) != len(profiles):
        raise ValueError("encoded tonal qualification profile inventory differs")
    expected_profiles = tuple(
        (notch_q, pass_count)
        for notch_q in config.render_qualification_notch_q_values
        for pass_count in config.render_qualification_notch_pass_counts
    )
    for index, (raw_profile, final_profile, qualified) in enumerate(
        zip(draft_profiles, profiles, evidence.profile_qualifications, strict=True)
    ):
        if qualified.profile_index != index:
            raise ValueError("encoded tonal qualification order differs")
        attempts = qualified.attempts
        attempt_profiles = tuple(
            (item.notch_q, item.notch_pass_count) for item in attempts
        )
        expected_thresholds = _thresholds_for_profile(raw_profile, config)
        if attempt_profiles != expected_profiles[: len(attempt_profiles)]:
            raise ValueError("encoded tonal qualification profile/Q order differs")
        first_passing = next((item for item in attempts if item.passed), None)
        if (
            first_passing is None
            or qualified.selected_notch_q != first_passing.notch_q
            or qualified.selected_notch_pass_count != first_passing.notch_pass_count
            or attempts[-1] is not first_passing
        ):
            raise ValueError("encoded tonal qualification did not select first pass")
        if any(
            not _has_complete_window_inventory(final_profile, attempt.metrics)
            for attempt in attempts
        ):
            raise ValueError("encoded tonal qualification window inventory differs")
        if any(
            attempt.thresholds != expected_thresholds
            or attempt.candidate_audio_topology != evidence.parent_audio_topology
            for attempt in attempts
        ):
            raise ValueError("encoded tonal qualification provenance differs")
        if raw_profile.model_copy(update={"render_qualification": None}) != (
            final_profile.model_copy(update={"render_qualification": None})
        ):
            raise ValueError("encoded tonal qualification changed measured profile")
    if (
        len(evidence.combined_metrics) != len(profiles)
        or len(evidence.combined_thresholds) != len(profiles)
        or evidence.combined_audio_topology != evidence.parent_audio_topology
    ):
        raise ValueError("encoded combined tonal metrics are incomplete")
    for profile, qualified, metrics, thresholds in zip(
        profiles,
        evidence.profile_qualifications,
        evidence.combined_metrics,
        evidence.combined_thresholds,
        strict=True,
    ):
        selected_notch_q = qualified.selected_notch_q
        selected_notch_pass_count = qualified.selected_notch_pass_count
        if selected_notch_q is None or selected_notch_pass_count is None:
            raise ValueError("encoded combined tonal candidate does not pass")
        if not _has_complete_window_inventory(profile, metrics):
            raise ValueError("encoded tonal qualification window inventory differs")
        if (
            thresholds != _thresholds_for_profile(profile, config)
            or not _metrics_match_thresholds(metrics, thresholds)
            or not metrics.passes(thresholds)
            or profile
            != _qualification_for_q(
                profile,
                selected_notch_q,
                metrics,
                selected_notch_pass_count,
            )
        ):
            raise ValueError("encoded combined tonal candidate does not pass")
    if action.parameters != qualified_tonal_action_parameters(evidence):
        raise ValueError("tonal action parameters differ from encoded qualification")
    expected_draft_id = make_rescue_action_id(
        kind=action.kind,
        parameters=draft_parameters,
        source_ranges=action.source_ranges,
        strategy=action.strategy,
        version=action.version,
    )
    if evidence.draft_action_id != expected_draft_id:
        raise ValueError("encoded tonal qualification draft ID differs")
    if action.id != make_rescue_action_id(
        kind=action.kind,
        parameters=action.parameters,
        source_ranges=action.source_ranges,
        strategy=action.strategy,
        version=action.version,
    ):
        raise ValueError("tonal action ID differs from encoded qualification")
    return evidence


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class NativeTonalCandidateQualifier:
    """Render full-stream encoded candidates before final plan confirmation."""

    def __init__(
        self, *, executor: Any = None, measurement_provider: Any = None
    ) -> None:
        if executor is None:
            from videoscope.rescue.executor import NativeRescueExecutor

            executor = NativeRescueExecutor()
        if measurement_provider is None:
            from videoscope.rescue.verification import NativeMediaMeasurementProvider

            measurement_provider = NativeMediaMeasurementProvider()
        self._executor = executor
        self._measurement_provider = measurement_provider

    def qualify(
        self,
        draft_plan: Any,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> TonalEncodedQualificationEvidenceV3:
        from videoscope.rescue.action_roles import faithful_restoration_action_ids
        from videoscope.rescue.models import RescueActionKind
        from videoscope.rescue.qualification import _map_exact_qualification_ranges

        source = Path(source)
        work_root = Path(work_root)
        if work_root.exists():
            raise RescueMediaError("tonal qualification root already exists")
        work_root.mkdir(parents=True, exist_ok=False)
        try:
            actions = tuple(
                action
                for action in draft_plan.actions
                if action.kind is RescueActionKind.DENOISE_AUDIO
                and action.parameters.get("interference_profiles")
            )
            if len(actions) != 1:
                raise RescueMediaError("tonal qualification action is ambiguous")
            action = actions[0]
            unsupported = faithful_restoration_action_ids(draft_plan) - {
                action.id,
                *(
                    item.id
                    for item in draft_plan.actions
                    if item.kind is RescueActionKind.STABILIZE
                ),
            }
            if unsupported:
                raise RescueMediaError(
                    "tonal qualification parent contains unsupported prior actions"
                )
            config = TonalInterferenceConfig.model_validate_json(
                json.dumps(action.parameters.get("config"), ensure_ascii=False)
            )
            profiles = tuple(
                InterferenceTone.model_validate_json(
                    json.dumps(item, ensure_ascii=False)
                )
                for item in action.parameters.get("interference_profiles", ())
            )
            execution = self._executor.execute_faithful(
                draft_plan,
                source,
                work_root,
                cancellation_callback,
                _allow_unqualified_sharpen_draft=True,
                _allow_unqualified_tonal_draft=True,
            )
            parent = Path(execution.output_path)
            parent_sha256 = _sha256_file(parent)
            inspect_topology = getattr(
                self._measurement_provider, "inspect_tonal_audio_topology", None
            )
            if not callable(inspect_topology):
                raise RescueMediaError("tonal audio topology inspection is unavailable")
            parent_topology = TonalAudioTopologyV2.model_validate(
                inspect_topology(parent, cancellation_callback)
            )
            inspect_timeline = getattr(
                self._measurement_provider, "inspect_tonal_audio_timeline", None
            )
            if not callable(inspect_timeline):
                raise RescueMediaError("tonal audio timeline inspection is unavailable")
            mappings = _path_free_mappings(execution.source_mappings)
            output_ranges = _map_ranges_from_evidence(action.source_ranges, mappings)
            encode_contract = _audio_encode_contract(draft_plan, config)
            qualifications: list[TonalEncodedProfileQualificationV2] = []
            selected: list[InterferenceTone] = []
            candidates_root = work_root / "tonal-candidates"
            candidates_root.mkdir(parents=True, exist_ok=False)
            boundary_control = candidates_root / "identity-control.mp4"
            execute_identity = getattr(self._executor, "execute_tonal_identity", None)
            if not callable(execute_identity):
                raise RescueMediaError(
                    "tonal identity control rendering is unavailable"
                )
            execute_identity(
                source=parent,
                output=boundary_control,
                config=config,
                cancellation_callback=cancellation_callback,
            )
            boundary_control_sha256 = _sha256_file(boundary_control)
            boundary_control_topology = TonalAudioTopologyV2.model_validate(
                inspect_topology(boundary_control, cancellation_callback)
            )
            if boundary_control_topology != parent_topology:
                raise RescueMediaError("tonal identity control audio topology differs")
            boundary_control_timeline = TonalAudioTimelineV1.model_validate(
                inspect_timeline(boundary_control, cancellation_callback)
            )
            candidate_timelines: list[tuple[TonalAudioTimelineV1, ...]] = []
            for profile_index, profile in enumerate(profiles):
                attempts: list[TonalEncodedCandidateAttemptV2] = []
                attempt_timelines: list[TonalAudioTimelineV1] = []
                selected_q: float | None = None
                selected_pass_count: int | None = None
                attempt_index = 0
                for notch_q in config.render_qualification_notch_q_values:
                    for (
                        notch_pass_count
                    ) in config.render_qualification_notch_pass_counts:
                        if cancellation_callback():
                            raise RescueCancelledError(
                                "tonal qualification was cancelled"
                            )
                        candidate = candidates_root / (
                            f"profile-{profile_index:03d}-q-{attempt_index:02d}.mp4"
                        )
                        trial_profile = _qualification_for_q(
                            profile, notch_q, notch_pass_count=notch_pass_count
                        )
                        self._executor.execute_tonal_reduced(
                            source=parent,
                            output=candidate,
                            tones=(trial_profile,),
                            config=config,
                            cancellation_callback=cancellation_callback,
                        )
                        trial_parameters = dict(action.parameters)
                        trial_parameters["interference_profiles"] = [
                            trial_profile.model_dump(mode="json")
                        ]
                        profile_output_ranges = _map_exact_qualification_ranges(
                            ((profile.start_seconds, profile.end_seconds),),
                            execution.source_mappings,
                        )
                        raw = self._measurement_provider.measure_perceptual_restoration(
                            RescueActionKind.DENOISE_AUDIO,
                            source,
                            candidate,
                            ((profile.start_seconds, profile.end_seconds),),
                            profile_output_ranges,
                            trial_parameters,
                            cancellation_callback,
                            boundary_reference=boundary_control,
                        )
                        metrics = metrics_from_provider(
                            raw, target_db=profile.attenuation_target_db
                        )
                        if not _has_complete_window_inventory(profile, metrics):
                            raise RescueMediaError(
                                "tonal encoded window inventory differs"
                            )
                        candidate_topology = TonalAudioTopologyV2.model_validate(
                            inspect_topology(candidate, cancellation_callback)
                        )
                        if candidate_topology != parent_topology:
                            raise RescueMediaError(
                                "tonal encoded candidate audio topology differs"
                            )
                        candidate_timeline = TonalAudioTimelineV1.model_validate(
                            inspect_timeline(candidate, cancellation_callback)
                        )
                        if candidate_timeline != boundary_control_timeline:
                            raise RescueMediaError(
                                "tonal encoded candidate audio timeline differs"
                            )
                        attempt_timelines.append(candidate_timeline)
                        thresholds = _thresholds_for_profile(profile, config)
                        attempts.append(
                            TonalEncodedCandidateAttemptV2(
                                notch_q=notch_q,
                                notch_pass_count=notch_pass_count,
                                candidate_sha256=_sha256_file(candidate),
                                candidate_audio_topology=candidate_topology,
                                metrics=metrics,
                                thresholds=thresholds,
                            )
                        )
                        candidate.unlink(missing_ok=True)
                        if attempts[-1].passed:
                            selected_q = notch_q
                            selected_pass_count = notch_pass_count
                            selected.append(
                                _qualification_for_q(
                                    profile,
                                    notch_q,
                                    metrics,
                                    notch_pass_count,
                                )
                            )
                            break
                        attempt_index += 1
                    if selected_q is not None:
                        break
                qualifications.append(
                    TonalEncodedProfileQualificationV2(
                        profile_index=profile_index,
                        attempts=tuple(attempts),
                        selected_notch_q=selected_q,
                        selected_notch_pass_count=selected_pass_count,
                    )
                )
                candidate_timelines.append(tuple(attempt_timelines))
            combined_sha256: str | None = None
            combined_topology: TonalAudioTopologyV2 | None = None
            combined_metrics: tuple[TonalEncodedMetricsV2, ...] = ()
            combined_thresholds: tuple[TonalEncodedThresholdsV2, ...] = ()
            combined_timeline: TonalAudioTimelineV1 | None = None
            final_profiles: tuple[InterferenceTone, ...] = ()
            if len(selected) == len(profiles):
                combined = candidates_root / "combined.mp4"
                self._executor.execute_tonal_reduced(
                    source=parent,
                    output=combined,
                    tones=tuple(selected),
                    config=config,
                    cancellation_callback=cancellation_callback,
                )
                combined_parameters = dict(action.parameters)
                combined_parameters["interference_profiles"] = [
                    profile.model_dump(mode="json") for profile in selected
                ]
                raw = self._measurement_provider.measure_perceptual_restoration(
                    RescueActionKind.DENOISE_AUDIO,
                    source,
                    combined,
                    action.source_ranges,
                    _map_exact_qualification_ranges(
                        action.source_ranges, execution.source_mappings
                    ),
                    combined_parameters,
                    cancellation_callback,
                    boundary_reference=boundary_control,
                )
                measured_combined = tuple(
                    metrics_from_provider(
                        raw,
                        target_db=profile.attenuation_target_db,
                        profile_index=index,
                    )
                    for index, profile in enumerate(selected)
                )
                measured_thresholds = tuple(
                    _thresholds_for_profile(profile, config) for profile in selected
                )
                if any(
                    not _has_complete_window_inventory(profile, metrics)
                    for profile, metrics in zip(
                        selected, measured_combined, strict=True
                    )
                ):
                    raise RescueMediaError("tonal encoded window inventory differs")
                measured_topology = TonalAudioTopologyV2.model_validate(
                    inspect_topology(combined, cancellation_callback)
                )
                if measured_topology != parent_topology:
                    raise RescueMediaError(
                        "combined tonal candidate audio topology differs"
                    )
                measured_timeline = TonalAudioTimelineV1.model_validate(
                    inspect_timeline(combined, cancellation_callback)
                )
                if measured_timeline != boundary_control_timeline:
                    raise RescueMediaError(
                        "combined tonal candidate audio timeline differs"
                    )
                if all(
                    metrics.passes(thresholds)
                    for metrics, thresholds in zip(
                        measured_combined, measured_thresholds, strict=True
                    )
                ):
                    raw_selected_profiles = tuple(
                        qualification.selected_notch_q
                        for qualification in qualifications
                    )
                    raw_selected_pass_counts = tuple(
                        qualification.selected_notch_pass_count
                        for qualification in qualifications
                    )
                    if any(
                        value is None
                        for value in (*raw_selected_profiles, *raw_selected_pass_counts)
                    ):
                        raise RescueMediaError(
                            "combined tonal candidate selection is incomplete"
                        )
                    selected_q_values = tuple(
                        float(value)
                        for value in raw_selected_profiles
                        if value is not None
                    )
                    selected_pass_values = tuple(
                        int(value)
                        for value in raw_selected_pass_counts
                        if value is not None
                    )
                    combined_sha256 = _sha256_file(combined)
                    combined_topology = measured_topology
                    combined_metrics = measured_combined
                    combined_thresholds = measured_thresholds
                    combined_timeline = measured_timeline
                    final_profiles = tuple(
                        _qualification_for_q(
                            profile,
                            selected_q,
                            measured,
                            selected_pass,
                        )
                        for profile, selected_q, selected_pass, measured in zip(
                            profiles,
                            selected_q_values,
                            selected_pass_values,
                            combined_metrics,
                            strict=True,
                        )
                    )
            return TonalEncodedQualificationEvidenceV3(
                input_hash=draft_plan.input_hash,
                draft_action_id=action.id,
                draft_parameters=dict(action.parameters),
                source_ranges=action.source_ranges,
                output_ranges=output_ranges,
                range_mappings=mappings,
                audio_encode_contract=encode_contract,
                parent_sha256=parent_sha256,
                parent_audio_topology=parent_topology,
                boundary_control_sha256=boundary_control_sha256,
                boundary_control_audio_topology=boundary_control_topology,
                boundary_control_audio_timeline=boundary_control_timeline,
                profile_candidate_audio_timelines=tuple(candidate_timelines),
                combined_audio_timeline=combined_timeline,
                profile_qualifications=tuple(qualifications),
                combined_candidate_sha256=combined_sha256,
                combined_audio_topology=combined_topology,
                combined_metrics=combined_metrics,
                combined_thresholds=combined_thresholds,
                selected_profiles=final_profiles,
                limitation=(
                    None if final_profiles else TONAL_ENCODED_QUALIFICATION_LIMITATION
                ),
            )
        finally:
            shutil.rmtree(work_root, ignore_errors=False)


__all__ = [
    "NativeTonalCandidateQualifier",
    "TONAL_ENCODED_QUALIFICATION_LIMITATION",
    "TONAL_ENCODED_QUALIFICATION_UNAVAILABLE_LIMITATION",
    "TONAL_ENCODED_QUALIFICATION_VERSION",
    "TonalAudioEncodeContractV2",
    "TonalAudioTimelineV1",
    "TonalAudioTopologyV2",
    "TonalEncodedCandidateAttemptV2",
    "TonalEncodedMetricsV2",
    "TonalEncodedProfileQualificationV2",
    "TonalEncodedQualificationEvidenceV2",
    "TonalEncodedQualificationEvidenceV3",
    "TonalEncodedThresholdsV2",
    "TonalRangeMappingV2",
    "audio_topology_from_ffprobe_stdout",
    "audio_timeline_from_ffprobe_stdout",
    "metrics_from_provider",
    "qualified_tonal_action_parameters",
    "tonal_audio_topology_probe_arguments",
    "tonal_audio_timeline_probe_arguments",
    "validate_encoded_tonal_qualification",
    "validate_tonal_runtime_candidate",
    "validate_tonal_runtime_parent",
]
