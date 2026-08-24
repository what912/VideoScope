"""Strict path-free contracts for private pre-preview candidate qualification."""

from __future__ import annotations

import json
import math
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, Protocol, Self

from pydantic import ConfigDict, Field, JsonValue, field_validator, model_validator

from videoscope.rescue.models import (
    CanonicalVideoEncodeContract,
    RescueModel,
    SharpenQualificationProfile,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
SHARPEN_QUALIFICATION_VERSION = "1"
SHARPEN_QUALIFICATION_LIMITATION = (
    "SHARPEN was omitted because no bounded full-range candidate profile passed "
    "all unchanged sharpness verification gates."
)
SHARPEN_QUALIFICATION_UNAVAILABLE_LIMITATION = (
    "SHARPEN was omitted because bounded full-range candidate qualification was "
    "unavailable."
)

V15_QUALIFICATION_INVENTORY_VERSION: Final = "v15-bounded-inventory-v1"
V15_QUALIFICATION_TRACK_ORDER: Final = (
    "sharpen",
    "denoise_audio",
    "stabilize",
)
_PATH_SEMANTIC_KEY = re.compile(
    r"(?:path|uri|url|filename|file_name|filepath|file_path|location)", re.IGNORECASE
)
_URI_VALUE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def validate_v15_qualification_inventories(
    inventories: Mapping[str, Sequence[str]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Validate the finite, ordered, independent V15 track inventories."""

    if tuple(inventories) != V15_QUALIFICATION_TRACK_ORDER:
        raise ValueError("V15 qualification track inventory order is not canonical")
    normalized: list[tuple[str, tuple[str, ...]]] = []
    seen: set[str] = set()
    for track_id in V15_QUALIFICATION_TRACK_ORDER:
        profiles = inventories[track_id]
        if isinstance(profiles, (str, bytes)) or not isinstance(profiles, Sequence):
            raise ValueError("V15 qualification profile inventory must be finite")
        profile_ids = tuple(profiles)
        if not profile_ids:
            raise ValueError("V15 qualification profile inventory cannot be empty")
        if any(
            not isinstance(profile_id, str) or not profile_id
            for profile_id in profile_ids
        ):
            raise ValueError("V15 qualification profile IDs must be non-empty strings")
        if len(set(profile_ids)) != len(profile_ids):
            raise ValueError("duplicate V15 qualification profile ID")
        overlap = seen.intersection(profile_ids)
        if overlap:
            raise ValueError("V15 qualification profile inventories overlap")
        seen.update(profile_ids)
        normalized.append((track_id, profile_ids))
    return tuple(normalized)


class RescueCandidateQualifier(Protocol):
    """Injectable private boundary between a draft and confirmable final plan."""

    def qualify(
        self,
        draft_plan: Any,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> SharpenQualificationEvidenceV1: ...


class UnavailableRescueCandidateQualifier:
    """Safe default until a local native qualifier can produce complete evidence."""

    def qualify(
        self,
        draft_plan: Any,
        source: Path,
        work_root: Path,
        cancellation_callback: Callable[[], bool],
    ) -> SharpenQualificationEvidenceV1:
        del draft_plan, source, work_root
        if cancellation_callback():
            from videoscope.rescue.errors import RescueCancelledError

            raise RescueCancelledError("candidate qualification was cancelled")
        raise RuntimeError("native candidate qualification is unavailable")


def _json_tuple(value: object) -> object:
    if isinstance(value, list):
        return tuple(tuple(item) if isinstance(item, list) else item for item in value)
    return value


def validate_path_free_canonical_json(value: JsonValue, *, field_name: str) -> None:
    """Reject private filesystem references and non-canonical numbers in evidence."""

    def visit(item: object) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{field_name} keys must be strings")
                if _PATH_SEMANTIC_KEY.search(key):
                    raise ValueError(f"{field_name} must be path-free")
                visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)
        elif isinstance(item, float):
            if not math.isfinite(item) or (
                item == 0.0 and math.copysign(1.0, item) < 0
            ):
                raise ValueError(f"{field_name} must use finite canonical JSON numbers")
        elif isinstance(item, str):
            value_text = item.strip()
            is_windows_absolute = (
                len(value_text) >= 3
                and value_text[0].isalpha()
                and (value_text[1:3] in (":/", ":\\"))
            )
            is_relative_path = value_text.startswith(
                ("../", "..\\", "./", ".\\", "~/", "~\\")
            )
            is_uri = bool(_URI_VALUE.match(value_text)) or (
                value_text.lower().startswith("file:")
            )
            has_media_suffix = bool(
                re.search(r"[\\/][^\\/]+\.[A-Za-z0-9]{1,8}$", value_text)
            )
            has_relative_separator = "/" in value_text or "\\" in value_text
            if (
                value_text.startswith(("/", "\\\\"))
                or is_windows_absolute
                or is_relative_path
                or is_uri
                or has_media_suffix
                or has_relative_separator
            ):
                raise ValueError(f"{field_name} must be path-free")

    visit(value)


def _validate_ranges(
    ranges: Sequence[tuple[float, float]], *, field_name: str
) -> tuple[tuple[float, float], ...]:
    normalized: list[tuple[float, float]] = []
    for start, end in ranges:
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(float(start))
            or not math.isfinite(float(end))
            or float(start) < 0.0
            or float(end) <= float(start)
        ):
            raise ValueError(f"{field_name} must contain finite positive intervals")
        normalized.append((float(start), float(end)))
    if not normalized:
        raise ValueError(f"{field_name} cannot be empty")
    ordered = tuple(sorted(normalized))
    if ordered != tuple(normalized):
        raise ValueError(f"{field_name} must use canonical order")
    if any(current[0] < previous[1] for previous, current in zip(ordered, ordered[1:])):
        raise ValueError(f"{field_name} cannot overlap")
    return ordered


class VerificationControlRecipeV1(RescueModel):
    """Digest-bound private identity sibling used by final verification."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    control_kind: Literal["identity_stabilization"] = "identity_stabilization"
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    action_id: str = Field(min_length=1)
    parent_sha256: str = Field(pattern=_SHA256_PATTERN)
    control_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    encode_contract: CanonicalVideoEncodeContract
    normalized_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    parent_normalized_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    parent_stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    candidate_normalized_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    candidate_stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    source_ranges: tuple[tuple[float, float], ...]
    frame_count: int = Field(ge=1, strict=True)
    parent_frame_count: int = Field(ge=1, strict=True)
    candidate_frame_count: int = Field(ge=1, strict=True)

    @field_validator("source_ranges", mode="before")
    @classmethod
    def accept_json_ranges(cls, value: object) -> object:
        return _json_tuple(value)

    @model_validator(mode="after")
    def validate_ranges(self) -> Self:
        object.__setattr__(
            self,
            "source_ranges",
            _validate_ranges(self.source_ranges, field_name="control source ranges"),
        )
        if self.parent_sha256 == self.control_sha256:
            raise ValueError("identity control must be a newly encoded generation")
        return self


@dataclass(frozen=True, slots=True)
class VerificationControlHandle:
    """Runtime-only media handle; the path never enters a public model."""

    path: Path
    parent_path: Path
    recipe: VerificationControlRecipeV1

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path):
            object.__setattr__(self, "path", Path(self.path))
        if not isinstance(self.parent_path, Path):
            object.__setattr__(self, "parent_path", Path(self.parent_path))

    @property
    def cleanup_paths(self) -> tuple[Path, ...]:
        return (self.parent_path, self.path)


class SharpenVerificationControlRecipeV1(RescueModel):
    """Digest-bound same-generation controls for final SHARPEN verification."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    control_kind: Literal["sharpen_same_generation"] = "sharpen_same_generation"
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    action_id: str = Field(min_length=1)
    baseline_sha256: str = Field(pattern=_SHA256_PATTERN)
    visibility_control_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    encode_contract: CanonicalVideoEncodeContract
    normalized_pts_digest: str = Field(pattern=_SHA256_PATTERN)
    stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    source_ranges: tuple[tuple[float, float], ...]
    output_ranges: tuple[tuple[float, float], ...]
    inventory_frame_count: int = Field(ge=1, strict=True)

    @field_validator("source_ranges", "output_ranges", mode="before")
    @classmethod
    def accept_json_ranges(cls, value: object) -> object:
        return _json_tuple(value)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        object.__setattr__(
            self,
            "source_ranges",
            _validate_ranges(self.source_ranges, field_name="SHARPEN source ranges"),
        )
        object.__setattr__(
            self,
            "output_ranges",
            _validate_ranges(self.output_ranges, field_name="SHARPEN output ranges"),
        )
        if len(self.source_ranges) != len(self.output_ranges):
            raise ValueError("SHARPEN verification ranges differ")
        if (
            len(
                {
                    self.baseline_sha256,
                    self.visibility_control_sha256,
                    self.candidate_sha256,
                }
            )
            != 3
        ):
            raise ValueError("SHARPEN runtime generations must be distinct")
        return self


@dataclass(frozen=True, slots=True)
class SharpenVerificationControlHandle:
    baseline_path: Path
    visibility_path: Path
    recipe: SharpenVerificationControlRecipeV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "baseline_path", Path(self.baseline_path))
        object.__setattr__(self, "visibility_path", Path(self.visibility_path))

    @property
    def cleanup_paths(self) -> tuple[Path, ...]:
        return (self.baseline_path, self.visibility_path)


class TonalVerificationControlRecipeV1(RescueModel):
    """Digest-bound same-parent identity control for tonal boundary checks."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    control_kind: Literal["tonal_same_generation"] = "tonal_same_generation"
    plan_digest: str = Field(pattern=_SHA256_PATTERN)
    action_id: str = Field(min_length=1)
    parent_sha256: str = Field(pattern=_SHA256_PATTERN)
    control_sha256: str = Field(pattern=_SHA256_PATTERN)
    qualified_candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_ranges: tuple[tuple[float, float], ...]
    output_ranges: tuple[tuple[float, float], ...]
    encode_contract: dict[str, JsonValue]
    control_audio_topology: dict[str, JsonValue]
    candidate_audio_topology: dict[str, JsonValue]
    control_audio_timeline: dict[str, JsonValue]
    candidate_audio_timeline: dict[str, JsonValue]

    @field_validator("source_ranges", "output_ranges", mode="before")
    @classmethod
    def accept_json_ranges(cls, value: object) -> object:
        return _json_tuple(value)

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        object.__setattr__(
            self,
            "source_ranges",
            _validate_ranges(self.source_ranges, field_name="tonal source ranges"),
        )
        object.__setattr__(
            self,
            "output_ranges",
            _validate_ranges(self.output_ranges, field_name="tonal output ranges"),
        )
        if len(self.source_ranges) != len(self.output_ranges):
            raise ValueError("tonal verification ranges differ")
        if self.control_sha256 in {
            self.parent_sha256,
            self.qualified_candidate_sha256,
        }:
            raise ValueError("tonal runtime generations must be distinct")
        return self


@dataclass(frozen=True, slots=True)
class TonalVerificationControlHandle:
    path: Path
    recipe: TonalVerificationControlRecipeV1

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))

    @property
    def cleanup_paths(self) -> tuple[Path, ...]:
        return (self.path,)


RuntimeVerificationControlHandle = (
    VerificationControlHandle
    | SharpenVerificationControlHandle
    | TonalVerificationControlHandle
)


class SharpenQualificationThresholdsV1(RescueModel):
    """The unchanged final SHARPEN gates used during qualification."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    minimum_aggregate_gain_ratio: float = Field(ge=0.0, allow_inf_nan=False)
    minimum_recovered_baseline_ratio: float = Field(ge=0.0, allow_inf_nan=False)
    minimum_improved_frame_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    maximum_noise_increase: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_edge_overshoot_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    maximum_edge_overshoot_amplitude: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_ringing_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class SharpenQualificationMetricsV1(RescueModel):
    """Exact retained-range frame coverage and unchanged clarity gates."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    range_coverage_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    expected_frames: int = Field(ge=1, strict=True)
    compared_frames: int = Field(ge=0, strict=True)
    range_count: int = Field(ge=1, strict=True)
    passing_range_count: int = Field(ge=0, strict=True)
    minimum_aggregate_gain_ratio: float = Field(ge=0.0, allow_inf_nan=False)
    minimum_recovered_baseline_ratio: float = Field(ge=0.0, allow_inf_nan=False)
    minimum_improved_frame_fraction: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    maximum_noise_increase: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_edge_overshoot_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    maximum_edge_overshoot_amplitude: float = Field(ge=0.0, allow_inf_nan=False)
    maximum_ringing_ratio: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)

    @property
    def has_full_coverage(self) -> bool:
        return (
            math.isclose(self.range_coverage_ratio, 1.0, rel_tol=0.0, abs_tol=1e-9)
            and self.compared_frames == self.expected_frames
            and self.passing_range_count == self.range_count
        )


class SharpenProfileMeasurementV1(RescueModel):
    """Whole-generation provenance plus bounded retained-range metrics."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: SharpenQualificationProfile
    baseline_sha256: str = Field(pattern=_SHA256_PATTERN)
    visibility_control_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    normalized_pts_digest: str = Field(
        pattern=_SHA256_PATTERN,
        description="Normalized PTS digest of the complete generated artifact.",
    )
    stream_topology_digest: str = Field(pattern=_SHA256_PATTERN)
    decoded_width: int = Field(ge=1, strict=True)
    decoded_height: int = Field(ge=1, strict=True)
    generation_count: Literal[1] = 1
    inventory_frame_count: int = Field(
        ge=1,
        strict=True,
        description="Decoded frame count of the complete generated artifact.",
    )
    metrics: SharpenQualificationMetricsV1
    thresholds: SharpenQualificationThresholdsV1

    @model_validator(mode="after")
    def validate_distinct_candidates(self) -> Self:
        if (
            len(
                {
                    self.baseline_sha256,
                    self.visibility_control_sha256,
                    self.candidate_sha256,
                }
            )
            != 3
        ):
            raise ValueError("qualification artifacts must be distinct generations")
        if (
            self.metrics.expected_frames > self.inventory_frame_count
            or self.metrics.compared_frames > self.metrics.expected_frames
        ):
            raise ValueError("qualification bounded frame coverage is invalid")
        return self

    @property
    def passed(self) -> bool:
        measured = self.metrics
        threshold = self.thresholds
        return bool(
            measured.has_full_coverage
            and measured.minimum_aggregate_gain_ratio
            >= threshold.minimum_aggregate_gain_ratio
            and measured.minimum_recovered_baseline_ratio
            >= threshold.minimum_recovered_baseline_ratio
            and measured.minimum_improved_frame_fraction
            >= threshold.minimum_improved_frame_fraction
            and measured.maximum_noise_increase <= threshold.maximum_noise_increase
            and measured.maximum_edge_overshoot_ratio
            <= threshold.maximum_edge_overshoot_ratio
            and measured.maximum_edge_overshoot_amplitude
            <= threshold.maximum_edge_overshoot_amplitude
            and measured.maximum_ringing_ratio <= threshold.maximum_ringing_ratio
        )


class SharpenQualificationEvidenceV1(RescueModel):
    """Complete deterministic qualification wire consumed by the final planner."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["1"] = "1"
    measurement_algorithm_version: Literal["sharpen_final_v1"] = "sharpen_final_v1"
    input_hash: str = Field(pattern=_SHA256_PATTERN)
    draft_action_id: str = Field(min_length=1)
    draft_parameters: dict[str, JsonValue]
    source_ranges: tuple[tuple[float, float], ...]
    output_ranges: tuple[tuple[float, float], ...]
    encode_contract: CanonicalVideoEncodeContract
    profile_measurements: tuple[SharpenProfileMeasurementV1, ...]
    selected_profile_id: str | None = Field(default=None, min_length=1)
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
            value, field_name="qualification draft parameters"
        )
        return value

    @field_validator("profile_measurements", mode="before")
    @classmethod
    def accept_json_measurements(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        object.__setattr__(
            self,
            "source_ranges",
            _validate_ranges(
                self.source_ranges, field_name="qualification source ranges"
            ),
        )
        object.__setattr__(
            self,
            "output_ranges",
            _validate_ranges(
                self.output_ranges, field_name="qualification output ranges"
            ),
        )
        if len(self.source_ranges) != len(self.output_ranges) or any(
            not math.isclose(
                source_end - source_start,
                output_end - output_start,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            for (source_start, source_end), (output_start, output_end) in zip(
                self.source_ranges, self.output_ranges, strict=True
            )
        ):
            raise ValueError("qualification output range inventory differs")
        if not self.profile_measurements:
            raise ValueError("qualification profile measurements cannot be empty")
        if any(
            item.metrics.range_count != len(self.source_ranges)
            for item in self.profile_measurements
        ):
            raise ValueError("qualification profile retained range inventory differs")
        profile_ids = tuple(
            item.profile.profile_id for item in self.profile_measurements
        )
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("duplicate qualification profile measurement")
        parent_inventories = {
            (
                item.baseline_sha256,
                item.normalized_pts_digest,
                item.stream_topology_digest,
                item.decoded_width,
                item.decoded_height,
                item.inventory_frame_count,
            )
            for item in self.profile_measurements
        }
        if len(parent_inventories) != 1:
            raise ValueError(
                "qualification profiles do not share one common parent inventory"
            )
        passing = tuple(item for item in self.profile_measurements if item.passed)
        expected = passing[0].profile.profile_id if passing else None
        if self.selected_profile_id != expected:
            raise ValueError("selected profile is not the first passing profile")
        if expected is None and self.limitation != SHARPEN_QUALIFICATION_LIMITATION:
            raise ValueError("failed qualification requires the canonical limitation")
        if expected is not None and self.limitation is not None:
            raise ValueError("passing qualification cannot carry a limitation")
        return self

    @property
    def selected(self) -> SharpenProfileMeasurementV1 | None:
        return next(
            (
                item
                for item in self.profile_measurements
                if item.profile.profile_id == self.selected_profile_id
            ),
            None,
        )


def build_sharpen_qualification_evidence(
    *,
    input_hash: str,
    draft_action_id: str,
    draft_parameters: Mapping[str, JsonValue],
    source_ranges: tuple[tuple[float, float], ...],
    output_ranges: tuple[tuple[float, float], ...],
    encode_contract: CanonicalVideoEncodeContract,
    configured_profiles: Sequence[SharpenQualificationProfile],
    measurements: Sequence[SharpenProfileMeasurementV1],
) -> SharpenQualificationEvidenceV1:
    """Validate configured order and select the first fully passing profile."""
    configured = tuple(configured_profiles)
    observed = tuple(measurements)
    if tuple(item.profile for item in observed) != configured:
        raise ValueError("qualification measurements do not match configured order")
    selected = next((item for item in observed if item.passed), None)
    return SharpenQualificationEvidenceV1(
        input_hash=input_hash,
        draft_action_id=draft_action_id,
        draft_parameters=dict(draft_parameters),
        source_ranges=source_ranges,
        output_ranges=output_ranges,
        encode_contract=encode_contract,
        profile_measurements=observed,
        selected_profile_id=(selected.profile.profile_id if selected else None),
        limitation=(None if selected else SHARPEN_QUALIFICATION_LIMITATION),
    )


def qualification_action_parameters(
    evidence: SharpenQualificationEvidenceV1,
) -> dict[str, JsonValue]:
    """Return the complete path-free wire that binds final action ID and digest."""
    selected = evidence.selected
    if selected is None:
        raise ValueError("failed qualification has no action parameters")
    return {
        "qualification": evidence.model_dump(mode="json"),
        "qualification_version": SHARPEN_QUALIFICATION_VERSION,
        "qualification_profile_id": selected.profile.profile_id,
        "qualification_metrics": selected.metrics.model_dump(mode="json"),
        "qualification_provenance": {
            "baseline_sha256": selected.baseline_sha256,
            "visibility_control_sha256": selected.visibility_control_sha256,
            "candidate_sha256": selected.candidate_sha256,
            "normalized_pts_digest": selected.normalized_pts_digest,
            "stream_topology_digest": selected.stream_topology_digest,
            "decoded_width": selected.decoded_width,
            "decoded_height": selected.decoded_height,
            "generation_count": selected.generation_count,
        },
    }


def apply_qualified_sharpen_profile(
    parameters: Mapping[str, Any],
    profile: SharpenQualificationProfile,
) -> dict[str, JsonValue]:
    """Apply only the configured finite profile axis to derived SHARPEN values."""
    updated: dict[str, Any] = dict(parameters)
    cas = updated.get("adaptive_strength")
    amount = updated.get("amount")
    passes = updated.get("detail_passes")
    if (
        isinstance(cas, bool)
        or not isinstance(cas, (int, float))
        or not math.isfinite(float(cas))
        or isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(float(amount))
        or isinstance(passes, bool)
        or not isinstance(passes, int)
        or passes < 1
    ):
        raise ValueError("derived SHARPEN parameters are invalid")
    updated["adaptive_strength"] = float(cas) * profile.cas_strength_scale
    updated["amount"] = float(amount) * profile.unsharp_amount_scale
    updated["detail_passes"] = min(passes, profile.pass_count)
    if profile.radius != 2 or "radius" in updated:
        updated["radius"] = profile.radius
    return updated


def validate_plan_sharpen_qualification_contracts(
    plan: Any, *, allow_unqualified_draft: bool = False
) -> None:
    """Re-derive every executable SHARPEN value from its strict evidence wire."""
    from videoscope.rescue.models import (
        RescueActionKind,
        canonical_video_encode_contract,
        make_rescue_action_id,
    )

    actions = tuple(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )
    if not actions:
        return
    if len(actions) != 1:
        raise ValueError("SHARPEN qualification action inventory is ambiguous")
    action = actions[0]
    if action.parameters.get("qualification") is None:
        if allow_unqualified_draft:
            return
        raise ValueError("final SHARPEN action qualification is missing")
    try:
        serialized = json.dumps(
            action.parameters.get("qualification"),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        evidence = SharpenQualificationEvidenceV1.model_validate_json(serialized)
    except (TypeError, ValueError) as exc:
        raise ValueError("SHARPEN qualification wire is invalid") from exc
    selected = evidence.selected
    if selected is None:
        raise ValueError("final SHARPEN action has no passing qualification profile")
    if (
        evidence.input_hash != plan.input_hash
        or evidence.source_ranges != action.source_ranges
        or evidence.encode_contract
        != canonical_video_encode_contract(plan.effective_config)
        or tuple(item.profile for item in evidence.profile_measurements)
        != plan.effective_config.sharpen_qualification_profiles
    ):
        raise ValueError("SHARPEN qualification binding differs from the plan")
    draft_parameters = dict(evidence.draft_parameters)
    if any(str(key).startswith("qualification") for key in draft_parameters):
        raise ValueError("SHARPEN qualification draft parameters are recursive")
    expected_draft_id = make_rescue_action_id(
        kind=action.kind,
        parameters=draft_parameters,
        source_ranges=action.source_ranges,
        strategy=action.strategy,
        version=action.version,
    )
    if evidence.draft_action_id != expected_draft_id:
        raise ValueError("SHARPEN qualification draft action ID is invalid")
    expected_thresholds = _thresholds_from_parameters(draft_parameters)
    if any(
        measurement.thresholds != expected_thresholds
        for measurement in evidence.profile_measurements
    ):
        raise ValueError("SHARPEN qualification thresholds differ from the draft")
    expected_parameters = apply_qualified_sharpen_profile(
        draft_parameters, selected.profile
    )
    expected_parameters.update(qualification_action_parameters(evidence))
    if action.parameters != expected_parameters:
        raise ValueError("SHARPEN action parameters differ from qualification")
    if action.id != make_rescue_action_id(
        kind=action.kind,
        parameters=expected_parameters,
        source_ranges=action.source_ranges,
        strategy=action.strategy,
        version=action.version,
    ):
        raise ValueError("SHARPEN action ID differs from qualification")


def validate_plan_sharpen_output_range_contracts(
    plan: Any,
    mappings: Sequence[Any],
    *,
    allow_unqualified_draft: bool = False,
) -> None:
    """Re-derive qualified SHARPEN output ranges from the current timeline."""
    from videoscope.rescue.models import RescueActionKind

    validate_plan_sharpen_qualification_contracts(
        plan, allow_unqualified_draft=allow_unqualified_draft
    )
    actions = tuple(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )
    if not actions or actions[0].parameters.get("qualification") is None:
        return
    evidence = SharpenQualificationEvidenceV1.model_validate(
        actions[0].parameters["qualification"]
    )
    expected_output_ranges = _map_exact_qualification_ranges(
        actions[0].source_ranges, mappings
    )
    if evidence.output_ranges != expected_output_ranges:
        raise ValueError("SHARPEN qualification output ranges differ")


class NativeRescueCandidateQualifier:
    """Render bounded private candidates and consume the final measurement core."""

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
    ) -> SharpenQualificationEvidenceV1:
        from videoscope.rescue.errors import RescueCancelledError, RescueMediaError
        from videoscope.rescue.models import (
            RescueActionKind,
            canonical_video_encode_contract,
        )

        source = Path(source)
        work_root = Path(work_root)
        if work_root.exists():
            raise RescueMediaError("candidate qualification root already exists")
        work_root.mkdir(parents=True, exist_ok=False)
        try:
            if cancellation_callback():
                raise RescueCancelledError("candidate qualification was cancelled")
            sharpen_actions = tuple(
                action
                for action in draft_plan.actions
                if action.kind is RescueActionKind.SHARPEN
            )
            if len(sharpen_actions) != 1:
                raise RescueMediaError("candidate qualification action is ambiguous")
            action = sharpen_actions[0]
            execution = self._executor.execute_faithful(
                draft_plan,
                source,
                work_root,
                cancellation_callback,
                _allow_unqualified_sharpen_draft=True,
            )
            restoration = getattr(self._executor, "execute_faithful_restoration", None)
            if callable(restoration):
                execution = restoration(
                    draft_plan,
                    execution,
                    work_root,
                    cancellation_callback,
                    _allow_unqualified_sharpen_draft=True,
                )
            output_ranges = _map_exact_qualification_ranges(
                action.source_ranges, execution.source_mappings
            )
            controls_root = work_root / "controls"
            controls_root.mkdir(parents=True, exist_ok=False)
            baseline = controls_root / "baseline.mp4"
            self._executor.render_sharpen_qualification_candidate(
                plan=draft_plan,
                faithful_parent=execution.output_path,
                output=baseline,
                source_ranges=action.source_ranges,
                parameters=action.parameters,
                mode="baseline",
                source_mappings=execution.source_mappings,
                cancellation_callback=cancellation_callback,
            )
            measurements: list[SharpenProfileMeasurementV1] = []
            for profile in draft_plan.effective_config.sharpen_qualification_profiles:
                if cancellation_callback():
                    raise RescueCancelledError("candidate qualification was cancelled")
                parameters = apply_qualified_sharpen_profile(action.parameters, profile)
                visibility = controls_root / f"visibility-{profile.profile_id}.mp4"
                candidate = controls_root / f"candidate-{profile.profile_id}.mp4"
                self._executor.render_sharpen_qualification_candidate(
                    plan=draft_plan,
                    faithful_parent=execution.output_path,
                    output=visibility,
                    source_ranges=action.source_ranges,
                    parameters=parameters,
                    mode="visibility",
                    source_mappings=execution.source_mappings,
                    cancellation_callback=cancellation_callback,
                )
                self._executor.render_sharpen_qualification_candidate(
                    plan=draft_plan,
                    faithful_parent=execution.output_path,
                    output=candidate,
                    source_ranges=action.source_ranges,
                    parameters=parameters,
                    mode="candidate",
                    source_mappings=execution.source_mappings,
                    cancellation_callback=cancellation_callback,
                )
                raw = self._measurement_provider.measure_sharpen_qualification(
                    baseline,
                    visibility,
                    candidate,
                    output_ranges,
                    parameters,
                    cancellation_callback,
                )
                measurements.append(
                    _profile_measurement_from_raw(profile, parameters, raw)
                )
            return build_sharpen_qualification_evidence(
                input_hash=draft_plan.input_hash,
                draft_action_id=action.id,
                draft_parameters=action.parameters,
                source_ranges=action.source_ranges,
                output_ranges=output_ranges,
                encode_contract=canonical_video_encode_contract(
                    draft_plan.effective_config
                ),
                configured_profiles=(
                    draft_plan.effective_config.sharpen_qualification_profiles
                ),
                measurements=tuple(measurements),
            )
        finally:
            shutil.rmtree(work_root, ignore_errors=False)


def _map_exact_qualification_ranges(
    source_ranges: tuple[tuple[float, float], ...],
    mappings: Sequence[Any],
) -> tuple[tuple[float, float], ...]:
    ordered_mappings = tuple(mappings)
    if not ordered_mappings:
        raise ValueError("qualification source mappings cannot be empty")
    previous_source_end = -math.inf
    previous_output_end = -math.inf
    for mapping in ordered_mappings:
        source_duration = float(mapping.source_end) - float(mapping.source_start)
        output_duration = float(mapping.output_end) - float(mapping.output_start)
        if (
            not all(
                math.isfinite(float(value))
                for value in (
                    mapping.source_start,
                    mapping.source_end,
                    mapping.output_start,
                    mapping.output_end,
                )
            )
            or source_duration <= 0.0
            or output_duration <= 0.0
            or not math.isclose(
                source_duration, output_duration, rel_tol=0.0, abs_tol=1e-9
            )
            or float(mapping.source_start) < previous_source_end
            or float(mapping.output_start) < previous_output_end
        ):
            raise ValueError("qualification source mappings are not exact")
        previous_source_end = float(mapping.source_end)
        previous_output_end = float(mapping.output_end)
    output: list[tuple[float, float]] = []
    for start, end in source_ranges:
        matches = tuple(
            mapping
            for mapping in ordered_mappings
            if mapping.source_start <= start and end <= mapping.source_end
        )
        if len(matches) != 1:
            raise ValueError("qualification range is not exactly retained")
        mapping = matches[0]
        if not math.isclose(
            float(mapping.source_end) - float(mapping.source_start),
            float(mapping.output_end) - float(mapping.output_start),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("qualification range mapping changes duration")
        output.append(
            (
                mapping.output_start + start - mapping.source_start,
                mapping.output_start + end - mapping.source_start,
            )
        )
    return _validate_ranges(output, field_name="qualification output ranges")


def _raw_finite(raw: Mapping[str, JsonValue], key: str) -> float:
    value = raw.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"qualification metric {key} is invalid")
    return float(value)


def _raw_int(raw: Mapping[str, JsonValue], key: str, *, minimum: int) -> int:
    value = _raw_finite(raw, key)
    if not value.is_integer() or value < minimum:
        raise ValueError(f"qualification metric {key} is invalid")
    return int(value)


def _raw_digest(raw: Mapping[str, JsonValue], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"qualification provenance {key} is invalid")
    return value


def _parameter_finite(parameters: Mapping[str, Any], key: str) -> float:
    value = parameters.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"qualification threshold {key} is invalid")
    return float(value)


def _thresholds_from_parameters(
    parameters: Mapping[str, Any],
) -> SharpenQualificationThresholdsV1:
    return SharpenQualificationThresholdsV1(
        minimum_aggregate_gain_ratio=_parameter_finite(
            parameters, "minimum_perceptible_sharpness_gain_ratio"
        ),
        minimum_recovered_baseline_ratio=_parameter_finite(
            parameters, "minimum_recovered_baseline_ratio"
        ),
        minimum_improved_frame_fraction=_parameter_finite(
            parameters, "minimum_improved_frame_fraction"
        ),
        maximum_noise_increase=_parameter_finite(parameters, "maximum_noise_increase"),
        maximum_edge_overshoot_ratio=_parameter_finite(
            parameters, "maximum_edge_overshoot_ratio"
        ),
        maximum_edge_overshoot_amplitude=_parameter_finite(
            parameters, "maximum_edge_overshoot_amplitude"
        ),
        maximum_ringing_ratio=_parameter_finite(parameters, "maximum_ringing_ratio"),
    )


def _profile_measurement_from_raw(
    profile: SharpenQualificationProfile,
    parameters: Mapping[str, Any],
    raw: Mapping[str, JsonValue],
) -> SharpenProfileMeasurementV1:
    pts_digests = tuple(
        _raw_digest(raw, key)
        for key in (
            "baseline_normalized_pts_digest",
            "control_normalized_pts_digest",
            "candidate_normalized_pts_digest",
        )
    )
    if len(set(pts_digests)) != 1:
        raise ValueError("qualification PTS inventory differs")
    topology_digests = tuple(
        _raw_digest(raw, key)
        for key in (
            "baseline_topology_sha256",
            "control_topology_sha256",
            "candidate_topology_sha256",
        )
    )
    if len(set(topology_digests)) != 1:
        raise ValueError("qualification topology inventory differs")
    frame_counts = tuple(
        _raw_int(raw, key, minimum=1)
        for key in (
            "baseline_frame_count",
            "control_frame_count",
            "candidate_frame_count",
        )
    )
    if len(set(frame_counts)) != 1:
        raise ValueError("qualification frame inventory differs")
    return SharpenProfileMeasurementV1(
        profile=profile,
        baseline_sha256=_raw_digest(raw, "baseline_sha256"),
        visibility_control_sha256=_raw_digest(raw, "control_sha256"),
        candidate_sha256=_raw_digest(raw, "candidate_sha256"),
        normalized_pts_digest=pts_digests[0],
        stream_topology_digest=topology_digests[0],
        decoded_width=_raw_int(raw, "decoded_width", minimum=1),
        decoded_height=_raw_int(raw, "decoded_height", minimum=1),
        generation_count=1,
        inventory_frame_count=frame_counts[0],
        metrics=SharpenQualificationMetricsV1(
            range_coverage_ratio=_raw_finite(raw, "range_coverage_ratio"),
            expected_frames=_raw_int(raw, "expected_frames", minimum=1),
            compared_frames=_raw_int(raw, "compared_frames", minimum=0),
            range_count=_raw_int(raw, "range_count", minimum=1),
            passing_range_count=_raw_int(raw, "passing_range_count", minimum=0),
            minimum_aggregate_gain_ratio=_raw_finite(
                raw, "minimum_aggregate_gain_ratio"
            ),
            minimum_recovered_baseline_ratio=_raw_finite(
                raw, "minimum_recovered_baseline_ratio"
            ),
            minimum_improved_frame_fraction=_raw_finite(
                raw, "minimum_improved_frame_fraction"
            ),
            maximum_noise_increase=_raw_finite(raw, "maximum_noise_increase"),
            maximum_edge_overshoot_ratio=_raw_finite(
                raw, "maximum_edge_overshoot_ratio"
            ),
            maximum_edge_overshoot_amplitude=_raw_finite(
                raw, "maximum_edge_overshoot_amplitude"
            ),
            maximum_ringing_ratio=_raw_finite(raw, "maximum_ringing_ratio"),
        ),
        thresholds=_thresholds_from_parameters(parameters),
    )
