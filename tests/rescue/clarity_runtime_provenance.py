"""Private runtime provenance contracts for the V15 clarity native gate."""

from __future__ import annotations

import dis
import inspect
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import CodeType, FrameType
from typing import Annotated, BinaryIO, Literal, Self, TypeVar, cast

import pytest
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from videoscope.rescue.errors import (
    RescueCancelledError,  # type: ignore[import-untyped]
)
from videoscope.rescue.executor import (  # type: ignore[import-untyped]
    NativeRescueExecutor,
    RescueExecutionResult,
    RescueImprovedExecutionResult,
)
from videoscope.rescue.models import (  # type: ignore[import-untyped]
    RescueAction,
    RescueActionKind,
    RescueOutcome,
    RescuePlan,
    RescueVerificationReport,
    RescueVerificationStatus,
    canonical_video_encode_contract,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.pipeline import (  # type: ignore[import-untyped]
    _cleanup_verification_controls,
)
from videoscope.rescue.planner import build_rescue_plan  # type: ignore[import-untyped]
from videoscope.rescue.qualification import (  # type: ignore[import-untyped]
    NativeRescueCandidateQualifier,
    SharpenQualificationEvidenceV1,
    SharpenVerificationControlHandle,
    SharpenVerificationControlRecipeV1,
    validate_plan_sharpen_output_range_contracts,
    validate_plan_sharpen_qualification_contracts,
)
from videoscope.rescue.timeline import SourceMapping  # type: ignore[import-untyped]
from videoscope.rescue.verification import (  # type: ignore[import-untyped]
    NativeMediaMeasurementProvider,
    RescueVerifier,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
_STABLE_TOKEN_PATTERN = r"^[a-z0-9_]+$"
_RESCUE_ACTION_ID_PATTERN = r"^rescue_action_[0-9a-f]{64}$"
_RESCUE_MODULE_PATTERN = r"^videoscope\.rescue\.[a-z_][a-z0-9_]*$"
_PYTHON_QUALNAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
_TOOL_VERSION_LINE_PATTERN = (
    r"^(?:ffmpeg|ffprobe) version 8\.1\.2"
    r"(?:-[A-Za-z0-9][A-Za-z0-9._+-]*)?"
    r"(?: Copyright \(c\) [0-9]{4}-[0-9]{4} the FFmpeg developers)?$"
)
_FINAL_NAME = "clarity-runtime-provenance.json"
_PARTIAL_NAME = f"{_FINAL_NAME}.partial"
_TOOL_TIMEOUT_SECONDS = 5.0
_TOOL_STDOUT_LIMIT_BYTES = 64 * 1024
_TOOL_STDERR_LIMIT_BYTES = 64 * 1024
_TOOL_STREAM_CHUNK_BYTES = 8 * 1024

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

ClarityPhase = Literal[
    "tool_identity_verified",
    "draft_bound",
    "qualification_returned",
    "qualification_cleanup_verified",
    "final_plan_bound",
    "faithful_returned",
    "improved_returned",
    "verification_returned",
    "controls_cleanup_returned",
    "source_integrity_verified",
    "publication_absence_verified",
]
ClarityEventOutcome = Literal["returned", "verified"]
ClarityEventComponent = Literal[
    "tool_identity_verifier",
    "build_rescue_plan",
    "NativeRescueCandidateQualifier.qualify",
    "qualification_cleanup",
    "NativeRescueExecutor.execute_faithful",
    "NativeRescueExecutor.execute_improved_with_controls",
    "RescueVerifier.verify",
    "_cleanup_verification_controls",
    "source_integrity",
    "publication_absence",
]
ClarityProfileId = Literal["full", "moderate", "gentle"]

_CANONICAL_PHASES: tuple[ClarityPhase, ...] = (
    "tool_identity_verified",
    "draft_bound",
    "qualification_returned",
    "qualification_cleanup_verified",
    "final_plan_bound",
    "faithful_returned",
    "improved_returned",
    "verification_returned",
    "controls_cleanup_returned",
    "source_integrity_verified",
    "publication_absence_verified",
)
_PHASE_INDEX = {phase: index for index, phase in enumerate(_CANONICAL_PHASES)}
_PHASE_OUTCOME: dict[ClarityPhase, ClarityEventOutcome] = {
    "tool_identity_verified": "verified",
    "draft_bound": "verified",
    "qualification_returned": "returned",
    "qualification_cleanup_verified": "verified",
    "final_plan_bound": "verified",
    "faithful_returned": "returned",
    "improved_returned": "returned",
    "verification_returned": "returned",
    "controls_cleanup_returned": "returned",
    "source_integrity_verified": "verified",
    "publication_absence_verified": "verified",
}
_PHASE_COMPONENT: dict[ClarityPhase, ClarityEventComponent] = {
    "tool_identity_verified": "tool_identity_verifier",
    "draft_bound": "build_rescue_plan",
    "qualification_returned": "NativeRescueCandidateQualifier.qualify",
    "qualification_cleanup_verified": "qualification_cleanup",
    "final_plan_bound": "build_rescue_plan",
    "faithful_returned": "NativeRescueExecutor.execute_faithful",
    "improved_returned": "NativeRescueExecutor.execute_improved_with_controls",
    "verification_returned": "RescueVerifier.verify",
    "controls_cleanup_returned": "_cleanup_verification_controls",
    "source_integrity_verified": "source_integrity",
    "publication_absence_verified": "publication_absence",
}
_ALLOWED_COMPONENT_IDENTITIES = frozenset(
    {
        ("videoscope.rescue.planner", "build_rescue_plan"),
        (
            "videoscope.rescue.qualification",
            "NativeRescueCandidateQualifier.qualify",
        ),
        (
            "videoscope.rescue.executor",
            "NativeRescueExecutor.execute_faithful",
        ),
        (
            "videoscope.rescue.executor",
            "NativeRescueExecutor.execute_improved_with_controls",
        ),
        ("videoscope.rescue.verification", "RescueVerifier.verify"),
        ("videoscope.rescue.pipeline", "_cleanup_verification_controls"),
    }
)

_NO_PROFILE_PHASES: tuple[ClarityPhase, ...] = (
    "tool_identity_verified",
    "draft_bound",
    "qualification_returned",
    "qualification_cleanup_verified",
    "source_integrity_verified",
    "publication_absence_verified",
)


def _strict_finite_float(value: object) -> float:
    if type(value) is not float or not math.isfinite(value):
        raise ValueError("value must be a finite strict float")
    return value


FiniteFloat = Annotated[float, BeforeValidator(_strict_finite_float)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(ge=1, strict=True)]
SourceRange = tuple[FiniteFloat, FiniteFloat]


def _assert_path_free_string(value: str) -> None:
    lowered = value.casefold()
    if (
        "/" in value
        or "\\" in value
        or lowered.startswith("file:")
        or lowered.startswith("https:")
    ):
        raise ValueError("clarity provenance content must be path-free")


def _json_value(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical provenance object keys must be strings")
            _assert_path_free_string(key)
            result[key] = _json_value(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, str):
        _assert_path_free_string(value)
        return value
    if value is None or isinstance(value, bool) or type(value) is int:
        return cast(JsonValue, value)
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("canonical provenance floats must be finite")
        return value
    raise ValueError(f"unsupported canonical provenance value: {type(value).__name__}")


def _canonical_bytes(value: object) -> bytes:
    normalized = _json_value(value)
    text = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
        separators=(",", ":"),
    )
    return f"{text}\n".encode()


def canonical_provenance_bytes(
    value: BaseModel | Mapping[str, object],
) -> bytes:
    """Return path-free canonical UTF-8 JSON with exactly one final LF."""

    return _canonical_bytes(value)


def provenance_digest(value: BaseModel | Mapping[str, object]) -> str:
    """Return the SHA-256 of the canonical provenance bytes."""

    return sha256(canonical_provenance_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_regular_file(path: Path, *, label: str) -> os.stat_result:
    if not isinstance(path, Path):
        raise TypeError(f"{label} must be a Path")
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    try:
        metadata = path.lstat()
    except OSError:
        raise ValueError(f"{label} must be an available regular file") from None
    if stat.S_ISLNK(metadata.st_mode) or _is_link_like(path):
        raise ValueError(f"{label} must be a non-link regular file")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} must be an available regular file")
    return metadata


def _drain_bounded_tool_stream(
    stream: BinaryIO,
    buffer: bytearray,
    limit: int,
) -> None:
    while True:
        chunk = stream.read(_TOOL_STREAM_CHUNK_BYTES)
        if not chunk:
            return
        remaining = limit + 1 - len(buffer)
        if remaining > 0:
            buffer.extend(chunk[:remaining])


def _run_bounded_tool_version(
    arguments: list[str],
    **options: object,
) -> subprocess.CompletedProcess[str]:
    """Drain both child streams while retaining at most each limit plus one."""

    timeout = options.get("timeout")
    if type(timeout) is not float or timeout <= 0.0:
        raise ValueError("clarity tool timeout is invalid")
    process = subprocess.Popen(
        arguments,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise OSError("clarity tool streams are unavailable")
    stdout = bytearray()
    stderr = bytearray()
    stdout_thread = threading.Thread(
        target=_drain_bounded_tool_stream,
        args=(process.stdout, stdout, _TOOL_STDOUT_LIMIT_BYTES),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_bounded_tool_stream,
        args=(process.stderr, stderr, _TOOL_STDERR_LIMIT_BYTES),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        stdout_thread.join()
        stderr_thread.join()
    return subprocess.CompletedProcess(
        arguments,
        returncode,
        stdout=bytes(stdout).decode("utf-8", errors="replace"),
        stderr=bytes(stderr).decode("utf-8", errors="replace"),
    )


def verify_clarity_tool_identity(
    path: Path,
    role: Literal["ffmpeg", "ffprobe"],
    *,
    runner: CommandRunner | None = None,
) -> ClarityToolIdentityV1:
    """Verify one fixed 8.1.2 tool without persisting its path or stderr."""

    if role not in {"ffmpeg", "ffprobe"}:
        raise ValueError("clarity tool role is invalid")
    _require_regular_file(path, label=f"{role} executable")
    binary_sha256 = _sha256_file(path)
    try:
        selected_runner = runner or _run_bounded_tool_version
        completed = selected_runner(
            [str(path), "-version"],
            capture_output=True,
            check=False,
            encoding="utf-8",
            errors="replace",
            shell=False,
            text=True,
            timeout=_TOOL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise ValueError(f"{role} version check timed out") from None
    except FileNotFoundError:
        raise ValueError(f"{role} executable is unavailable") from None
    except OSError:
        raise ValueError(f"{role} version check failed") from None
    if not isinstance(completed, subprocess.CompletedProcess):
        raise TypeError("clarity tool runner returned an invalid result")
    if type(completed.returncode) is not int or completed.returncode != 0:
        raise ValueError(f"{role} version check failed")
    if not isinstance(completed.stdout, str):
        raise TypeError("clarity tool stdout must be text")
    stdout_bytes = completed.stdout.encode("utf-8")
    if len(stdout_bytes) > _TOOL_STDOUT_LIMIT_BYTES:
        raise ValueError("clarity tool stdout exceeds the bounded limit")
    lines = completed.stdout.splitlines()
    if not lines:
        raise ValueError(f"{role} version line is malformed")
    first_line = " ".join(lines[0].split())
    prefix = f"{role} version "
    if not first_line.startswith(prefix):
        raise ValueError(f"{role} version line has the wrong role")
    token = first_line[len(prefix) :].split(" ", 1)[0]
    parsed = re.fullmatch(
        r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
        r"(?:-[A-Za-z0-9][A-Za-z0-9._+-]*)?",
        token,
    )
    if parsed is None or parsed.group("version") != "8.1.2":
        raise ValueError(f"{role} semantic version must be 8.1.2")
    try:
        identity = ClarityToolIdentityV1(
            role=role,
            binary_sha256=binary_sha256,
            reported_version_line=first_line,
            version_stdout_sha256=sha256(stdout_bytes).hexdigest(),
            semantic_version="8.1.2",
        )
    except ValueError:
        raise ValueError(f"{role} version line is malformed") from None
    if _sha256_file(path) != binary_sha256:
        raise ValueError(f"{role} binary changed during verification")
    return identity


def _validate_ranges(ranges: tuple[SourceRange, ...], *, field_name: str) -> None:
    if not ranges:
        raise ValueError(f"{field_name} must not be empty")
    previous_end: float | None = None
    for start, end in ranges:
        if start < 0.0 or end <= start:
            raise ValueError(f"{field_name} must contain positive ordered ranges")
        if previous_end is not None and start < previous_end:
            raise ValueError(f"{field_name} must not overlap")
        previous_end = end


class _ClarityStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="after")
    def validate_path_free(self) -> Self:
        _json_value(self.model_dump(mode="json"))
        return self


class ClarityComponentIdentityV1(_ClarityStrictModel):
    module: str = Field(
        min_length=1,
        max_length=128,
        pattern=_RESCUE_MODULE_PATTERN,
    )
    qualname: str = Field(
        min_length=1,
        max_length=128,
        pattern=_PYTHON_QUALNAME_PATTERN,
    )
    source_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_production_identity(self) -> Self:
        if (self.module, self.qualname) not in _ALLOWED_COMPONENT_IDENTITIES:
            raise ValueError("clarity component is not an approved production identity")
        return self


class ClarityToolIdentityV1(_ClarityStrictModel):
    role: Literal["ffmpeg", "ffprobe"]
    binary_sha256: str = Field(pattern=SHA256_PATTERN)
    reported_version_line: str = Field(
        min_length=1,
        max_length=192,
        pattern=_TOOL_VERSION_LINE_PATTERN,
    )
    version_stdout_sha256: str = Field(pattern=SHA256_PATTERN)
    semantic_version: Literal["8.1.2"]

    @model_validator(mode="after")
    def validate_version_line_role(self) -> Self:
        if not self.reported_version_line.startswith(f"{self.role} version 8.1.2"):
            raise ValueError("clarity tool version line does not match its role")
        return self


class ClaritySourceProjectionV1(_ClarityStrictModel):
    sha256_before: str = Field(pattern=SHA256_PATTERN)
    sha256_after: str = Field(pattern=SHA256_PATTERN)
    size_bytes: NonNegativeInt


class ClarityPlanProjectionV1(_ClarityStrictModel):
    input_hash: str = Field(pattern=SHA256_PATTERN)
    plan_digest: str = Field(pattern=SHA256_PATTERN)
    action_id: str = Field(pattern=_RESCUE_ACTION_ID_PATTERN)
    config_digest: str = Field(pattern=SHA256_PATTERN)
    encode_contract_digest: str = Field(pattern=SHA256_PATTERN)
    source_ranges: tuple[SourceRange, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_source_ranges(self) -> Self:
        _validate_ranges(self.source_ranges, field_name="plan source_ranges")
        return self


class ClarityQualificationProjectionV1(_ClarityStrictModel):
    evidence_digest: str = Field(pattern=SHA256_PATTERN)
    profile_order: tuple[ClarityProfileId, ...] = Field(min_length=1)
    selected_profile_id: ClarityProfileId | None
    selected_identity_digest: str | None = Field(pattern=SHA256_PATTERN)
    selected_metrics_digest: str | None = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_selection(self) -> Self:
        if len(set(self.profile_order)) != len(self.profile_order):
            raise ValueError("qualification profile_order must be unique")
        selected_values = (
            self.selected_profile_id,
            self.selected_identity_digest,
            self.selected_metrics_digest,
        )
        if all(item is None for item in selected_values):
            return self
        if any(item is None for item in selected_values):
            raise ValueError("qualification selection must be complete or absent")
        if self.selected_profile_id not in self.profile_order:
            raise ValueError("selected qualification profile is not configured")
        return self


class ClarityFinalProjectionV1(_ClarityStrictModel):
    plan_digest: str = Field(pattern=SHA256_PATTERN)
    action_id: str = Field(pattern=_RESCUE_ACTION_ID_PATTERN)
    source_mappings_digest: str = Field(pattern=SHA256_PATTERN)
    output_ranges_digest: str = Field(pattern=SHA256_PATTERN)
    faithful_sha256: str = Field(pattern=SHA256_PATTERN)
    improved_sha256: str = Field(pattern=SHA256_PATTERN)


class ClarityRuntimeRecipeProjectionV1(_ClarityStrictModel):
    recipe_digest: str = Field(pattern=SHA256_PATTERN)
    baseline_sha256: str = Field(pattern=SHA256_PATTERN)
    visibility_control_sha256: str = Field(pattern=SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=SHA256_PATTERN)
    normalized_pts_digest: str = Field(pattern=SHA256_PATTERN)
    stream_topology_digest: str = Field(pattern=SHA256_PATTERN)
    inventory_frame_count: PositiveInt
    source_ranges_digest: str = Field(pattern=SHA256_PATTERN)
    output_ranges_digest: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_distinct_generations(self) -> Self:
        generations = {
            self.baseline_sha256,
            self.visibility_control_sha256,
            self.candidate_sha256,
        }
        if len(generations) != 3:
            raise ValueError("clarity runtime generations must be distinct")
        return self


class ClarityVerificationProjectionV1(_ClarityStrictModel):
    report_digest: str = Field(pattern=SHA256_PATTERN)
    required_check_id: Literal["perceptible_sharpness_improvement"]
    required_check_status: Literal["passed", "needs_review", "failed"]
    runtime_control_recipe_valid: Literal[True]
    selected_qualification_binding_valid: Literal[True]
    expected_frames: NonNegativeInt
    compared_frames: NonNegativeInt
    range_count: NonNegativeInt
    passing_range_count: NonNegativeInt
    range_coverage_ratio: FiniteFloat
    minimum_aggregate_gain_ratio: FiniteFloat
    minimum_recovered_baseline_ratio: FiniteFloat
    minimum_improved_frame_fraction: FiniteFloat
    maximum_noise_increase: FiniteFloat
    maximum_edge_overshoot_ratio: FiniteFloat
    maximum_edge_overshoot_amplitude: FiniteFloat
    maximum_ringing_ratio: FiniteFloat
    metrics_digest: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_metric_counts(self) -> Self:
        if self.compared_frames > self.expected_frames:
            raise ValueError("compared_frames exceeds expected_frames")
        if self.passing_range_count > self.range_count:
            raise ValueError("passing_range_count exceeds range_count")
        return self


class ClarityCleanupProjectionV1(_ClarityStrictModel):
    qualification_root_absent: bool
    control_count: NonNegativeInt
    controls_absent: bool
    source_unchanged: bool
    public_outputs_absent: bool


class ClarityErrorV1(_ClarityStrictModel):
    phase: str = Field(min_length=1, max_length=64, pattern=_STABLE_TOKEN_PATTERN)
    code: str = Field(min_length=1, max_length=64, pattern=_STABLE_TOKEN_PATTERN)


class ClarityRuntimeEventV1(_ClarityStrictModel):
    sequence: NonNegativeInt
    phase: ClarityPhase
    component: ClarityEventComponent
    outcome: ClarityEventOutcome
    stable_input_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    stable_output_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    previous_event_sha256: str | None = Field(default=None, pattern=SHA256_PATTERN)
    event_sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_event_hash(self) -> Self:
        if self.component != _PHASE_COMPONENT[self.phase]:
            raise ValueError("clarity event component does not match its phase")
        payload = self.model_dump(mode="python", exclude={"event_sha256"})
        if self.event_sha256 != provenance_digest(payload):
            raise ValueError("clarity event digest mismatch")
        return self


@dataclass(frozen=True, slots=True)
class ProductionComponentSpec:
    """Frozen exact code identity for one observed production component."""

    name: str
    module: str
    qualname: str
    source_sha256: str
    code: CodeType
    receiver_type: type[object] | None
    argument_names: tuple[str, ...]
    expected_return_count: int = 1


def production_component(
    name: str,
    value: Callable[..., object],
    *,
    receiver_type: type[object] | None = None,
    expected_return_count: int = 1,
) -> ProductionComponentSpec:
    """Capture a callable's exact code object and path-free source identity."""

    if not isinstance(name, str) or not name:
        raise ValueError("production component name must not be empty")
    if type(expected_return_count) is not int or expected_return_count < 1:
        raise ValueError("expected return count must be a positive integer")
    code = getattr(value, "__code__", None)
    module = getattr(value, "__module__", None)
    qualname = getattr(value, "__qualname__", None)
    if not isinstance(code, CodeType):
        raise TypeError("production component must expose a Python code object")
    if not isinstance(module, str) or not module:
        raise ValueError("production component module must not be empty")
    if not isinstance(qualname, str) or not qualname:
        raise ValueError("production component qualname must not be empty")
    try:
        source = inspect.getsource(value)
    except (OSError, TypeError) as error:
        raise ValueError("production component source is unavailable") from error
    if receiver_type is not None and not isinstance(receiver_type, type):
        raise TypeError("production component receiver_type must be a type")
    argument_names = tuple(
        name
        for name, parameter in inspect.signature(value).parameters.items()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        and name not in {"self", "cls"}
    )
    return ProductionComponentSpec(
        name=name,
        module=module,
        qualname=qualname,
        source_sha256=sha256(source.encode()).hexdigest(),
        code=code,
        receiver_type=receiver_type,
        argument_names=argument_names,
        expected_return_count=expected_return_count,
    )


DEFAULT_COMPONENTS = (
    production_component(
        "build_rescue_plan",
        build_rescue_plan,
        expected_return_count=2,
    ),
    production_component(
        "qualify",
        NativeRescueCandidateQualifier.qualify,
        receiver_type=NativeRescueCandidateQualifier,
    ),
    production_component(
        "execute_faithful",
        NativeRescueExecutor.execute_faithful,
        receiver_type=NativeRescueExecutor,
    ),
    production_component(
        "execute_improved_with_controls",
        NativeRescueExecutor.execute_improved_with_controls,
        receiver_type=NativeRescueExecutor,
    ),
    production_component(
        "verify",
        RescueVerifier.verify,
        receiver_type=RescueVerifier,
    ),
    production_component("cleanup_controls", _cleanup_verification_controls),
)

_DEFAULT_RETURN_SEQUENCE = (
    "build_rescue_plan",
    "qualify",
    "build_rescue_plan",
    "execute_faithful",
    "execute_improved_with_controls",
    "verify",
    "cleanup_controls",
)


@dataclass(frozen=True, slots=True)
class ClarityObservedReturn:
    """One in-memory return from an exact registered code object."""

    sequence: int
    component: str
    value: object
    object_id: int
    thread_id: int
    receiver: object | None = None
    arguments: tuple[tuple[str, object], ...] = ()


class ClarityRuntimeObserver:
    """Observe exact Python code-object returns on one owning thread."""

    def __init__(
        self,
        components: Sequence[ProductionComponentSpec],
        *,
        expected_sequence: Sequence[str] | None = None,
    ) -> None:
        if not components:
            raise ValueError("clarity observer requires production components")
        normalized = tuple(components)
        if not all(isinstance(item, ProductionComponentSpec) for item in normalized):
            raise TypeError("clarity observer requires ProductionComponentSpec values")
        names = tuple(item.name for item in normalized)
        if len(set(names)) != len(names):
            raise ValueError("clarity observer has a duplicate component name")
        codes = tuple(item.code for item in normalized)
        if len({id(code) for code in codes}) != len(codes):
            raise ValueError("clarity observer has a duplicate code object")
        if expected_sequence is None:
            sequence = tuple(
                name
                for item in normalized
                for name in (item.name,) * item.expected_return_count
            )
        else:
            sequence = tuple(expected_sequence)
        if not sequence or any(name not in names for name in sequence):
            raise ValueError("clarity observer expected sequence is invalid")
        expected_counts = {item.name: item.expected_return_count for item in normalized}
        actual_counts = {name: sequence.count(name) for name in names}
        if actual_counts != expected_counts:
            raise ValueError("clarity observer expected return counts differ")

        self._components = normalized
        self._by_code_id = {id(item.code): item for item in normalized}
        self._expected_sequence = sequence
        self._returns: list[ClarityObservedReturn] = []
        self._active_frames: set[int] = set()
        self._active_calls: dict[
            int, tuple[object | None, tuple[tuple[str, object], ...]]
        ] = {}
        self._rejected_frames: set[int] = set()
        self._active_qualifier_frames: set[int] = set()
        self._owner_thread_id: int | None = None
        self._previous_hook: Callable[[FrameType, str, object], object] | None = None
        self._started = False
        self._integrity_error: str | None = None

        def dispatcher(frame: FrameType, event: str, arg: object) -> None:
            previous = self._previous_hook
            if previous is not None:
                previous(frame, event, arg)
            component = self._by_code_id.get(id(frame.f_code))
            if (
                component is None
                or frame.f_code is not component.code
                or event not in {"call", "return"}
            ):
                return
            current_thread = threading.get_ident()
            if current_thread != self._owner_thread_id:
                self._integrity_error = "component returned from a cross-thread call"
                return
            frame_id = id(frame)
            if self._active_qualifier_frames and component.name != "qualify":
                return
            if event == "call":
                receiver = frame.f_locals.get("self")
                if (
                    component.receiver_type is not None
                    and type(receiver) is not component.receiver_type
                ):
                    self._integrity_error = (
                        f"{component.name} receiver type is not exact"
                    )
                    self._rejected_frames.add(frame_id)
                    return
                arguments = tuple(
                    (name, frame.f_locals[name])
                    for name in component.argument_names
                    if name in frame.f_locals
                )
                self._active_frames.add(frame_id)
                self._active_calls[frame_id] = (receiver, arguments)
                if component.name == "qualify":
                    self._active_qualifier_frames.add(frame_id)
                return
            if frame_id in self._rejected_frames:
                self._rejected_frames.remove(frame_id)
                return
            if frame_id not in self._active_frames:
                self._integrity_error = "component return has no matching call"
                return
            self._active_frames.remove(frame_id)
            receiver, arguments = self._active_calls.pop(frame_id)
            if component.name == "qualify":
                self._active_qualifier_frames.discard(frame_id)
            if not self._is_successful_return(frame):
                return
            self._returns.append(
                ClarityObservedReturn(
                    sequence=len(self._returns),
                    component=component.name,
                    value=arg,
                    object_id=id(arg),
                    thread_id=current_thread,
                    receiver=receiver,
                    arguments=arguments,
                )
            )

        self._dispatcher = dispatcher

    @staticmethod
    def _is_successful_return(frame: FrameType) -> bool:
        if frame.f_lasti < 0 or frame.f_lasti >= len(frame.f_code.co_code):
            return False
        opcode = frame.f_code.co_code[frame.f_lasti]
        return dis.opname[opcode] in {"RETURN_CONST", "RETURN_VALUE"}

    @property
    def components(self) -> tuple[ProductionComponentSpec, ...]:
        return self._components

    @property
    def observed_returns(self) -> tuple[ClarityObservedReturn, ...]:
        return tuple(self._returns)

    def start(self) -> None:
        if self._started:
            raise ValueError("clarity runtime observer is already started")
        self._previous_hook = cast(
            Callable[[FrameType, str, object], object] | None,
            sys.getprofile(),
        )
        self._owner_thread_id = threading.get_ident()
        self._started = True
        sys.setprofile(self._dispatcher)

    def stop(self) -> None:
        if not self._started:
            return
        previous = self._previous_hook
        self._started = False
        sys.setprofile(previous)

    def require_intact(self) -> None:
        if self._started and sys.getprofile() is not self._dispatcher:
            raise ValueError("clarity runtime observer was replaced")
        if self._integrity_error is not None:
            raise ValueError(self._integrity_error)
        actual = tuple(item.component for item in self._returns)
        expected_prefix = self._expected_sequence[: len(actual)]
        if actual != expected_prefix:
            raise ValueError("clarity runtime return sequence is invalid")

    def require_return(self, component: str, value: object) -> ClarityObservedReturn:
        matching = tuple(item for item in self._returns if item.component == component)
        if not matching:
            raise ValueError(f"{component} return is missing")
        for observed in matching:
            if observed.value is value:
                return observed
        raise ValueError(f"{component} return identity mismatch")

    def _require_complete(self) -> None:
        self.require_intact()
        actual = tuple(item.component for item in self._returns)
        if actual != self._expected_sequence:
            raise ValueError("clarity runtime return sequence is incomplete")


@dataclass(frozen=True, slots=True)
class ClarityEventInput:
    phase: ClarityPhase
    component: ClarityEventComponent
    outcome: ClarityEventOutcome
    stable_input_digest: str | None
    stable_output_digest: str | None


def build_event_chain(
    events: Sequence[ClarityEventInput],
) -> tuple[ClarityRuntimeEventV1, ...]:
    """Build a contiguous hash chain in canonical semantic phase order."""

    result: list[ClarityRuntimeEventV1] = []
    previous_index = -1
    previous_hash: str | None = None
    for sequence, event in enumerate(events):
        if not isinstance(event, ClarityEventInput):
            raise ValueError("clarity event chain requires ClarityEventInput values")
        phase_index = _PHASE_INDEX.get(event.phase)
        if phase_index is None or phase_index <= previous_index:
            raise ValueError("clarity event phase order is invalid")
        expected_outcome = _PHASE_OUTCOME[event.phase]
        if event.outcome != expected_outcome:
            raise ValueError("clarity event outcome does not match its phase")
        payload: dict[str, object] = {
            "sequence": sequence,
            "phase": event.phase,
            "component": event.component,
            "outcome": event.outcome,
            "stable_input_digest": event.stable_input_digest,
            "stable_output_digest": event.stable_output_digest,
            "previous_event_sha256": previous_hash,
        }
        event_hash = provenance_digest(payload)
        result.append(
            ClarityRuntimeEventV1.model_validate(
                {**payload, "event_sha256": event_hash}
            )
        )
        previous_index = phase_index
        previous_hash = event_hash
    return tuple(result)


class ClarityRuntimeProvenanceV1(_ClarityStrictModel):
    schema_version: Literal["1"] = "1"
    track: Literal["sharpen_clarity"] = "sharpen_clarity"
    producer_version: Literal["clarity_runtime_provenance_v1"]
    selector_id: Literal["clarity_exact_native_v1"]
    outcome: Literal["passed", "no_profile_passed", "cancelled", "error"]
    component_manifest: tuple[ClarityComponentIdentityV1, ...] = Field(min_length=1)
    tools: tuple[ClarityToolIdentityV1, ...]
    source: ClaritySourceProjectionV1 | None
    draft: ClarityPlanProjectionV1 | None
    qualification: ClarityQualificationProjectionV1 | None
    final: ClarityFinalProjectionV1 | None
    runtime_recipe: ClarityRuntimeRecipeProjectionV1 | None
    verification: ClarityVerificationProjectionV1 | None
    cleanup: ClarityCleanupProjectionV1
    events: tuple[ClarityRuntimeEventV1, ...]
    events_digest: str = Field(pattern=SHA256_PATTERN)
    error: ClarityErrorV1 | None
    envelope_digest: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_envelope(self) -> Self:
        self._validate_unique_identities()
        phases = self._validate_event_chain()
        self._validate_outcome(phases)
        expected_events_digest = provenance_digest({"events": self.events})
        if self.events_digest != expected_events_digest:
            raise ValueError("clarity events digest mismatch")
        envelope = self.model_dump(mode="python", exclude={"envelope_digest"})
        if self.envelope_digest != provenance_digest(envelope):
            raise ValueError("clarity envelope digest mismatch")
        return self

    def _validate_unique_identities(self) -> None:
        component_keys = tuple(
            (item.module, item.qualname) for item in self.component_manifest
        )
        if len(set(component_keys)) != len(component_keys):
            raise ValueError("clarity component manifest contains duplicates")
        if (
            len(component_keys) != len(_ALLOWED_COMPONENT_IDENTITIES)
            or set(component_keys) != _ALLOWED_COMPONENT_IDENTITIES
        ):
            raise ValueError(
                "clarity component manifest must contain the exact mandatory components"
            )
        tool_roles = tuple(item.role for item in self.tools)
        if len(set(tool_roles)) != len(tool_roles):
            raise ValueError("clarity tool identities contain duplicate roles")

    def _validate_event_chain(self) -> tuple[ClarityPhase, ...]:
        phases: list[ClarityPhase] = []
        previous_index = -1
        previous_hash: str | None = None
        for expected_sequence, event in enumerate(self.events):
            if event.sequence != expected_sequence:
                raise ValueError("clarity event sequence is not contiguous")
            phase_index = _PHASE_INDEX[event.phase]
            if phase_index <= previous_index:
                raise ValueError("clarity event phase order is invalid")
            if event.outcome != _PHASE_OUTCOME[event.phase]:
                raise ValueError("clarity event outcome does not match its phase")
            if event.previous_event_sha256 != previous_hash:
                raise ValueError("clarity previous event digest mismatch")
            phases.append(event.phase)
            previous_index = phase_index
            previous_hash = event.event_sha256
        return tuple(phases)

    def _validate_outcome(self, phases: tuple[ClarityPhase, ...]) -> None:
        if self.outcome == "passed":
            required = (
                self.source,
                self.draft,
                self.qualification,
                self.final,
                self.runtime_recipe,
                self.verification,
            )
            if any(item is None for item in required) or self.error is not None:
                raise ValueError("passed clarity provenance requires every section")
            if phases != _CANONICAL_PHASES:
                raise ValueError("passed clarity provenance requires every event")
            if {item.role for item in self.tools} != {"ffmpeg", "ffprobe"}:
                raise ValueError("passed clarity provenance requires both fixed tools")
            verification = self.verification
            if verification is None or verification.required_check_status != "passed":
                raise ValueError("passed clarity provenance requires a passed check")
            if not self._successful_cleanup():
                raise ValueError("passed clarity provenance requires complete cleanup")
            return

        if self.outcome == "no_profile_passed":
            if any(
                item is None for item in (self.source, self.draft, self.qualification)
            ):
                raise ValueError("no-profile provenance requires completed sections")
            if any(
                item is not None
                for item in (self.final, self.runtime_recipe, self.verification)
            ):
                raise ValueError("no-profile provenance forbids final sections")
            if self.error is not None:
                raise ValueError("no-profile provenance forbids an error")
            qualification = self.qualification
            if qualification is None or qualification.selected_profile_id is not None:
                raise ValueError("no-profile provenance cannot contain a selection")
            if phases != _NO_PROFILE_PHASES:
                raise ValueError("no-profile provenance has invalid events")
            if {item.role for item in self.tools} != {"ffmpeg", "ffprobe"}:
                raise ValueError("no-profile provenance requires both fixed tools")
            if not self._successful_cleanup() or self.cleanup.control_count != 0:
                raise ValueError("no-profile provenance requires complete cleanup")
            return

        if self.error is None:
            raise ValueError("cancelled and error provenance require a stable error")
        self._validate_completed_sections(phases)

    def _successful_cleanup(self) -> bool:
        source_matches = bool(
            self.source is not None
            and self.source.sha256_before == self.source.sha256_after
        )
        return bool(
            self.cleanup.qualification_root_absent
            and self.cleanup.controls_absent
            and self.cleanup.source_unchanged
            and source_matches
            and self.cleanup.public_outputs_absent
        )

    def _validate_completed_sections(self, phases: tuple[ClarityPhase, ...]) -> None:
        required_by_phase: dict[ClarityPhase, object | None] = {
            "draft_bound": self.draft,
            "qualification_returned": self.qualification,
            "final_plan_bound": self.final,
            "faithful_returned": self.final,
            "improved_returned": self.runtime_recipe,
            "verification_returned": self.verification,
        }
        for phase, section in required_by_phase.items():
            if phase in phases and section is None:
                raise ValueError(f"successful {phase} event requires its section")
        section_prefix = (
            self.source,
            self.draft,
            self.qualification,
            self.final,
            self.runtime_recipe,
            self.verification,
        )
        found_null = False
        for section in section_prefix:
            if section is None:
                found_null = True
            elif found_null:
                raise ValueError("completed clarity sections must form a prefix")
        if "tool_identity_verified" in phases and {
            item.role for item in self.tools
        } != {"ffmpeg", "ffprobe"}:
            raise ValueError("verified tool event requires both fixed tools")
        if (
            "qualification_cleanup_verified" in phases
            and not self.cleanup.qualification_root_absent
        ):
            raise ValueError("qualification cleanup event requires absent root")
        if "controls_cleanup_returned" in phases and not self.cleanup.controls_absent:
            raise ValueError("controls cleanup event requires absent controls")
        if "source_integrity_verified" in phases:
            source_matches = bool(
                self.source is not None
                and self.source.sha256_before == self.source.sha256_after
            )
            if not self.cleanup.source_unchanged or not source_matches:
                raise ValueError(
                    "source integrity event requires a matching source projection"
                )
        if (
            "publication_absence_verified" in phases
            and not self.cleanup.public_outputs_absent
        ):
            raise ValueError("publication event requires absent public outputs")


def _path_exists_no_follow(path: Path) -> bool:
    return os.path.lexists(path)


_WINDOWS_RENAME_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08, 0.16)


def _retry_windows_no_replace_rename(
    source: Path,
    target: Path,
    *,
    rename: Callable[[Path, Path], None] = os.rename,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    for delay in (*_WINDOWS_RENAME_RETRY_DELAYS_SECONDS, None):
        try:
            rename(source, target)
        except OSError as error:
            if (
                delay is None
                or getattr(error, "winerror", None) != 5
                or not _path_exists_no_follow(source)
                or _path_exists_no_follow(target)
            ):
                raise
            sleep(delay)
        else:
            return


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction is not None and is_junction())


def _validate_root(root: Path) -> None:
    if not isinstance(root, Path):
        raise TypeError("clarity provenance root must be a Path")
    if not root.is_absolute():
        raise ValueError("clarity provenance root must be absolute")
    if ".." in root.parts:
        raise ValueError("clarity provenance root contains a lexical path escape")
    parent = root.parent
    while parent != parent.parent:
        if _path_exists_no_follow(parent) and _is_link_like(parent):
            raise ValueError("clarity provenance root traverses a link-like path")
        parent = parent.parent
    if not root.parent.is_dir():
        raise FileNotFoundError("clarity provenance parent directory does not exist")


def _write_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _atomic_promote(partial: Path, final: Path) -> None:
    if os.name == "nt":
        _retry_windows_no_replace_rename(partial, final)
        return
    descriptor = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    os.replace(partial, final)


def _remove_owned_root(root: Path) -> None:
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or _is_link_like(root):
        root.unlink()
    elif stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(root)
    else:
        root.unlink()


def write_clarity_runtime_provenance(
    root: Path,
    envelope: ClarityRuntimeProvenanceV1,
) -> Path:
    """Write one strict envelope into a newly acquired private audit root."""

    if not isinstance(envelope, ClarityRuntimeProvenanceV1):
        raise TypeError("clarity provenance writer requires a strict envelope")
    payload = canonical_provenance_bytes(envelope)
    _validate_root(root)
    if _path_exists_no_follow(root):
        raise FileExistsError(f"clarity provenance root already exists: {root.name}")

    owned = False
    promotion_failed = False
    try:
        root.mkdir(exist_ok=False)
        owned = True
        partial = root / _PARTIAL_NAME
        final = root / _FINAL_NAME
        _write_exclusive(partial, payload)
        try:
            _atomic_promote(partial, final)
        except BaseException:
            promotion_failed = True
            raise
        loaded = read_clarity_runtime_provenance(final)
        if canonical_provenance_bytes(loaded) != payload:
            raise ValueError("clarity provenance readback bytes differ")
        return final
    except BaseException:
        if owned and not promotion_failed:
            try:
                _remove_owned_root(root)
            except Exception as cleanup_error:
                raise RuntimeError(
                    "clarity provenance cleanup failed for an owned root"
                ) from cleanup_error
        raise


def read_clarity_runtime_provenance(path: Path) -> ClarityRuntimeProvenanceV1:
    """Read, validate, and require canonical bytes for a private envelope."""

    if not isinstance(path, Path):
        raise TypeError("clarity provenance path must be a Path")
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or _is_link_like(path):
        raise ValueError("clarity provenance file cannot be link-like")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("clarity provenance path must be a regular file")
    payload = path.read_bytes()
    envelope = ClarityRuntimeProvenanceV1.model_validate_json(payload)
    if canonical_provenance_bytes(envelope) != payload:
        raise ValueError("clarity provenance file is not canonical")
    return envelope


EXACT_CLARITY_NODE_ID = (
    "tests/rescue/test_fixture_rescue.py::"
    "test_native_fixed_8_1_2_soft_detail_qualification_matches_final_verifier"
)

PytestCallOutcome = Literal["passed", "failed", "skipped"]


@dataclass(frozen=True, slots=True)
class ClarityPytestCallReport:
    """The only pytest call facts retained for partial failure projection."""

    outcome: PytestCallOutcome
    exception_type: type[BaseException] | None

    def __post_init__(self) -> None:
        if self.outcome not in {"passed", "failed", "skipped"}:
            raise ValueError("pytest call outcome is invalid")
        if self.exception_type is not None and (
            not isinstance(self.exception_type, type)
            or not issubclass(self.exception_type, BaseException)
        ):
            raise ValueError("pytest exception type must be an exception class")


CLARITY_CALL_REPORT_KEY = pytest.StashKey[ClarityPytestCallReport]()


def _component_manifest() -> tuple[ClarityComponentIdentityV1, ...]:
    return tuple(
        ClarityComponentIdentityV1(
            module=item.module,
            qualname=item.qualname,
            source_sha256=item.source_sha256,
        )
        for item in DEFAULT_COMPONENTS
    )


def _partial_error_envelope(
    *,
    outcome: Literal["cancelled", "error"],
    phase: str,
    code: str,
) -> ClarityRuntimeProvenanceV1:
    events: tuple[ClarityRuntimeEventV1, ...] = ()
    payload: dict[str, object] = {
        "schema_version": "1",
        "track": "sharpen_clarity",
        "producer_version": "clarity_runtime_provenance_v1",
        "selector_id": "clarity_exact_native_v1",
        "outcome": outcome,
        "component_manifest": _component_manifest(),
        "tools": (),
        "source": None,
        "draft": None,
        "qualification": None,
        "final": None,
        "runtime_recipe": None,
        "verification": None,
        "cleanup": ClarityCleanupProjectionV1(
            qualification_root_absent=False,
            control_count=0,
            controls_absent=False,
            source_unchanged=False,
            public_outputs_absent=False,
        ),
        "events": events,
        "events_digest": provenance_digest({"events": events}),
        "error": ClarityErrorV1(phase=phase, code=code),
    }
    return ClarityRuntimeProvenanceV1.model_validate(
        {**payload, "envelope_digest": provenance_digest(payload)}
    )


_ModelT = TypeVar("_ModelT", bound=BaseModel)
_INTEGER_METRICS = (
    "expected_frames",
    "compared_frames",
    "range_count",
    "passing_range_count",
)
# Integral binary64 values above this boundary cannot represent every adjacent
# integer exactly, so the audit boundary does not accept them as count evidence.
_MAX_EXACT_JSON_INTEGER = 9_007_199_254_740_991
_FLOAT_METRICS = (
    "range_coverage_ratio",
    "minimum_aggregate_gain_ratio",
    "minimum_recovered_baseline_ratio",
    "minimum_improved_frame_fraction",
    "maximum_noise_increase",
    "maximum_edge_overshoot_ratio",
    "maximum_edge_overshoot_amplitude",
    "maximum_ringing_ratio",
)


def _canonicalize_clarity_count_metric(
    actual: object,
    selected: object,
    *,
    name: str,
) -> int:
    """Return the selected strict count after exact numeric comparison."""

    if type(selected) is not int or selected < 0:
        raise ValueError(f"clarity integer metric {name} differs")
    if type(actual) is int:
        observed = actual
    elif (
        type(actual) is float
        and math.isfinite(actual)
        and actual.is_integer()
        and 0.0 <= actual <= _MAX_EXACT_JSON_INTEGER
    ):
        observed = int(actual)
    else:
        raise ValueError(f"clarity integer metric {name} differs")
    if observed != selected:
        raise ValueError(f"clarity integer metric {name} differs")
    return selected


def _require_exact_model(
    value: object,
    expected: type[_ModelT],
    *,
    label: str,
) -> _ModelT:
    if type(value) is not expected:
        raise TypeError(f"{label} must be an exact {expected.__name__}")
    current = value
    rebuilt = expected.model_validate(current.model_dump(mode="python"))
    if rebuilt != current:
        raise ValueError(f"{label} differs after strict validation")
    return current


def _require_plan_identity(
    value: object,
    *,
    label: str,
    allow_unqualified_draft: bool,
) -> tuple[RescuePlan, RescueAction, ClarityPlanProjectionV1]:
    if type(value) is not RescuePlan:
        raise TypeError(f"{label} must be an exact RescuePlan")
    plan = value
    if not allow_unqualified_draft:
        rebuilt = RescuePlan.model_validate(plan.model_dump(mode="python"))
        if rebuilt != plan:
            raise ValueError(f"{label} differs after strict validation")
    payload = cast(
        Mapping[str, JsonValue],
        plan.model_dump(mode="json", exclude={"plan_digest"}),
    )
    if plan.plan_digest != make_rescue_plan_digest(payload):
        raise ValueError(f"{label} digest is invalid")
    for action in plan.actions:
        if type(action) is not RescueAction:
            raise TypeError(f"{label} contains a non-exact RescueAction")
        expected_id = make_rescue_action_id(
            kind=action.kind,
            parameters=action.parameters,
            source_ranges=action.source_ranges,
            strategy=action.strategy,
            version=action.version,
        )
        if action.id != expected_id:
            raise ValueError(f"{label} action identity is invalid")
    sharpen_actions = tuple(
        action for action in plan.actions if action.kind is RescueActionKind.SHARPEN
    )
    if len(sharpen_actions) != 1:
        raise ValueError(f"{label} requires exactly one SHARPEN action")
    validate_plan_sharpen_qualification_contracts(
        plan,
        allow_unqualified_draft=allow_unqualified_draft,
    )
    action = sharpen_actions[0]
    projection = ClarityPlanProjectionV1(
        input_hash=plan.input_hash,
        plan_digest=plan.plan_digest,
        action_id=action.id,
        config_digest=provenance_digest(plan.effective_config),
        encode_contract_digest=provenance_digest(
            canonical_video_encode_contract(plan.effective_config)
        ),
        source_ranges=action.source_ranges,
    )
    return plan, action, projection


def _require_qualification_binding(
    value: object,
    *,
    draft: RescuePlan,
    draft_action: RescueAction,
) -> tuple[SharpenQualificationEvidenceV1, ClarityQualificationProjectionV1]:
    evidence = _require_exact_model(
        value,
        SharpenQualificationEvidenceV1,
        label="clarity qualification evidence",
    )
    profile_order = tuple(
        item.profile.profile_id for item in evidence.profile_measurements
    )
    configured_order = tuple(
        item.profile_id
        for item in draft.effective_config.sharpen_qualification_profiles
    )
    if (
        evidence.input_hash != draft.input_hash
        or evidence.draft_action_id != draft_action.id
        or evidence.draft_parameters != draft_action.parameters
        or evidence.source_ranges != draft_action.source_ranges
        or evidence.encode_contract
        != canonical_video_encode_contract(draft.effective_config)
        or profile_order != configured_order
    ):
        raise ValueError("clarity qualification differs from the live draft")
    selected = evidence.selected
    selected_identity_digest = None
    selected_metrics_digest = None
    if selected is not None:
        selected_identity_digest = provenance_digest(
            {
                "profile": selected.profile,
                "baseline_sha256": selected.baseline_sha256,
                "visibility_control_sha256": selected.visibility_control_sha256,
                "candidate_sha256": selected.candidate_sha256,
                "normalized_pts_digest": selected.normalized_pts_digest,
                "stream_topology_digest": selected.stream_topology_digest,
                "decoded_width": selected.decoded_width,
                "decoded_height": selected.decoded_height,
                "generation_count": selected.generation_count,
                "inventory_frame_count": selected.inventory_frame_count,
            }
        )
        selected_metrics_digest = provenance_digest(selected.metrics)
    projection = ClarityQualificationProjectionV1(
        evidence_digest=provenance_digest(evidence),
        profile_order=cast(tuple[ClarityProfileId, ...], profile_order),
        selected_profile_id=cast(ClarityProfileId | None, evidence.selected_profile_id),
        selected_identity_digest=selected_identity_digest,
        selected_metrics_digest=selected_metrics_digest,
    )
    return evidence, projection


def _require_source_after(
    *,
    source: Path,
    source_sha256_after: str,
    bound_path: Path,
    sha256_before: str,
    size_before: int,
) -> ClaritySourceProjectionV1:
    if source != bound_path:
        raise ValueError("clarity source path differs from its binding")
    metadata = _require_regular_file(source, label="clarity source")
    if not re.fullmatch(SHA256_PATTERN, source_sha256_after):
        raise ValueError("clarity source after digest is invalid")
    actual = _sha256_file(source)
    if (
        actual != source_sha256_after
        or actual != sha256_before
        or metadata.st_size != size_before
    ):
        raise ValueError("clarity source changed after binding")
    return ClaritySourceProjectionV1(
        sha256_before=sha256_before,
        sha256_after=actual,
        size_bytes=size_before,
    )


def _require_execution_file(path: Path, root: Path, *, label: str) -> str:
    _require_regular_file(path, label=label)
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValueError("clarity execution root must be an absolute Path")
    try:
        root_metadata = root.lstat()
    except OSError:
        raise ValueError(
            "clarity execution root must be an available directory"
        ) from None
    if (
        stat.S_ISLNK(root_metadata.st_mode)
        or _is_link_like(root)
        or not stat.S_ISDIR(root_metadata.st_mode)
    ):
        raise ValueError("clarity execution root must be a non-link directory")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        raise ValueError(f"{label} escapes the execution root") from None
    return _sha256_file(path)


def _public_outputs_absent(execution_root: Path) -> bool:
    return all(
        not _path_exists_no_follow(execution_root / relative)
        for relative in ("rescue-output", "report.json", "report.html")
    )


def _strict_envelope(payload: dict[str, object]) -> ClarityRuntimeProvenanceV1:
    events = cast(tuple[ClarityRuntimeEventV1, ...], payload["events"])
    payload["events_digest"] = provenance_digest({"events": events})
    return ClarityRuntimeProvenanceV1.model_validate(
        {**payload, "envelope_digest": provenance_digest(payload)}
    )


def _require_public_verifier_mapping_projection(
    faithful_mappings: tuple[SourceMapping, ...],
    verifier_mappings: object,
) -> tuple[SourceMapping, ...]:
    """Bind a distinct public verifier projection to exact private intervals."""

    if (
        type(faithful_mappings) is not tuple
        or not faithful_mappings
        or any(type(mapping) is not SourceMapping for mapping in faithful_mappings)
    ):
        raise ValueError("clarity public verifier mappings lack a private source")
    if (
        type(verifier_mappings) is not tuple
        or verifier_mappings is faithful_mappings
        or len(verifier_mappings) != len(faithful_mappings)
        or any(type(mapping) is not SourceMapping for mapping in verifier_mappings)
    ):
        raise ValueError("clarity public verifier mappings have invalid cardinality")
    public = cast(tuple[SourceMapping, ...], verifier_mappings)
    output_cursor = 0.0
    for private, projected in zip(faithful_mappings, public, strict=True):
        private_values = (
            private.source_start,
            private.source_end,
            private.output_start,
            private.output_end,
        )
        public_values = (
            projected.source_start,
            projected.source_end,
            projected.output_start,
            projected.output_end,
        )
        if (
            projected is private
            or any(
                not math.isfinite(value) for value in (*private_values, *public_values)
            )
            or private.source_end <= private.source_start
            or private.output_end <= private.output_start
            or projected.source_end <= projected.source_start
            or projected.output_end <= projected.output_start
            or private.output_relative_path == "faithful-rescue.mp4"
            or projected.output_relative_path != "faithful-rescue.mp4"
            or public_values != private_values
            or projected.output_start != output_cursor
        ):
            raise ValueError(
                "clarity public verifier mappings differ from private intervals"
            )
        output_cursor = projected.output_end
    return public


class ClarityRuntimeGuard:
    """Own the exact selector observer and fail-closed pytest finalization."""

    def __init__(self, audit_root: Path) -> None:
        if not isinstance(audit_root, Path):
            raise TypeError("clarity runtime audit root must be a Path")
        self._audit_root = audit_root
        self._observer = ClarityRuntimeObserver(
            DEFAULT_COMPONENTS,
            expected_sequence=_DEFAULT_RETURN_SEQUENCE,
        )
        self._terminal_path: Path | None = None
        self._terminal_envelope: ClarityRuntimeProvenanceV1 | None = None
        self._terminal_digest: str | None = None
        self._started = False
        self._finalized = False
        self._tools: tuple[ClarityToolIdentityV1, ...] | None = None
        self._ffmpeg_path: Path | None = None
        self._ffprobe_path: Path | None = None
        self._source_path: Path | None = None
        self._source_sha256_before: str | None = None
        self._source_size_before: int | None = None

    @property
    def observer(self) -> ClarityRuntimeObserver:
        return self._observer

    def start(self) -> None:
        if self._started:
            raise ValueError("clarity runtime guard is already started")
        if self._finalized:
            raise ValueError("clarity runtime guard is already finalized")
        _validate_root(self._audit_root)
        if _path_exists_no_follow(self._audit_root):
            raise FileExistsError(
                f"clarity provenance root already exists: {self._audit_root.name}"
            )
        self._observer.start()
        self._started = True

    def _require_active_unsealed(self) -> None:
        if not self._started or self._finalized:
            raise ValueError("clarity runtime guard is not active")
        if any(
            item is not None
            for item in (
                self._terminal_path,
                self._terminal_envelope,
                self._terminal_digest,
            )
        ):
            raise ValueError("clarity runtime terminal seal is already recorded")

    def bind_tools(self, ffmpeg: Path, ffprobe: Path) -> None:
        """Verify and bind both fixed tools before production returns begin."""

        self._require_active_unsealed()
        if self._tools is not None:
            raise ValueError("clarity runtime tools are already bound")
        if self._observer.observed_returns:
            raise ValueError("clarity runtime tools must be bound before milestones")
        tools = (
            verify_clarity_tool_identity(ffmpeg, "ffmpeg"),
            verify_clarity_tool_identity(ffprobe, "ffprobe"),
        )
        if any(type(tool) is not ClarityToolIdentityV1 for tool in tools):
            raise TypeError("clarity tool verifier returned a non-exact identity")
        roles = tuple(tool.role for tool in tools)
        if roles != ("ffmpeg", "ffprobe") or len(set(roles)) != 2:
            raise ValueError("clarity tool identities contain a duplicate role")
        self._tools = tools
        self._ffmpeg_path = ffmpeg
        self._ffprobe_path = ffprobe

    def bind_source_before(self, source: Path, sha256: str) -> None:
        """Bind one immutable regular source before the draft planner returns."""

        self._require_active_unsealed()
        if self._source_path is not None:
            raise ValueError("clarity source is already bound")
        if self._observer.observed_returns:
            raise ValueError("clarity source must be bound before the draft return")
        if self._tools is None:
            raise ValueError("clarity tools must be bound before the source")
        metadata = _require_regular_file(source, label="clarity source")
        if not isinstance(sha256, str) or re.fullmatch(SHA256_PATTERN, sha256) is None:
            raise ValueError("clarity source digest is invalid")
        if _sha256_file(source) != sha256:
            raise ValueError("clarity source digest differs from the file")
        self._source_path = source
        self._source_sha256_before = sha256
        self._source_size_before = metadata.st_size

    def _require_bindings(
        self,
    ) -> tuple[tuple[ClarityToolIdentityV1, ...], Path, str, int]:
        self._require_active_unsealed()
        if self._tools is None:
            raise ValueError("clarity tools are not bound")
        if (
            self._source_path is None
            or self._source_sha256_before is None
            or self._source_size_before is None
        ):
            raise ValueError("clarity source is not bound")
        return (
            self._tools,
            self._source_path,
            self._source_sha256_before,
            self._source_size_before,
        )

    def _require_bound_tool_paths(self) -> tuple[Path, Path]:
        if self._ffmpeg_path is None or self._ffprobe_path is None:
            raise ValueError("clarity fixed tool paths are not bound")
        return self._ffmpeg_path, self._ffprobe_path

    def _require_qualifier_live_call(
        self,
        observed: ClarityObservedReturn,
        *,
        draft: RescuePlan,
        evidence: SharpenQualificationEvidenceV1,
        source: Path,
        qualification_root: Path,
    ) -> tuple[NativeRescueExecutor, NativeMediaMeasurementProvider]:
        if observed.component != "qualify" or observed.value is not evidence:
            raise ValueError("clarity qualifier live return identity differs")
        if type(observed.receiver) is not NativeRescueCandidateQualifier:
            raise ValueError("clarity qualifier live receiver type differs")
        qualifier = observed.receiver
        arguments = dict(observed.arguments)
        if (
            arguments.get("draft_plan") is not draft
            or arguments.get("source") != source
            or arguments.get("work_root") != qualification_root
        ):
            raise ValueError("clarity qualifier live call arguments differ")
        executor = qualifier._executor
        provider = qualifier._measurement_provider
        if type(executor) is not NativeRescueExecutor:
            raise ValueError("clarity qualifier executor receiver type differs")
        if type(provider) is not NativeMediaMeasurementProvider:
            raise ValueError("clarity qualifier provider receiver type differs")
        ffmpeg, ffprobe = self._require_bound_tool_paths()
        expected_paths = (str(ffmpeg), str(ffprobe))
        if (executor._ffmpeg, executor._ffprobe) != expected_paths:
            raise ValueError("clarity executor fixed tool paths differ")
        if (provider._ffmpeg, provider._ffprobe) != expected_paths:
            raise ValueError("clarity provider fixed tool paths differ")
        return executor, provider

    def _require_success_return_identities(
        self,
        *,
        draft: RescuePlan,
        evidence: SharpenQualificationEvidenceV1,
        final: RescuePlan,
        faithful: RescueExecutionResult,
        improved: RescueImprovedExecutionResult,
        report: RescueVerificationReport,
        source: Path,
        qualification_root: Path,
        execution_root: Path,
        controls: tuple[SharpenVerificationControlHandle, ...],
    ) -> tuple[SourceMapping, ...]:
        self._observer._require_complete()
        expected = (
            ("build_rescue_plan", draft),
            ("qualify", evidence),
            ("build_rescue_plan", final),
            ("execute_faithful", faithful),
            ("execute_improved_with_controls", improved),
            ("verify", report),
            ("cleanup_controls", None),
        )
        observed = self._observer.observed_returns
        if len(observed) != len(expected):
            raise ValueError("clarity live return sequence differs")
        for item, (component, value) in zip(observed, expected, strict=True):
            if item.component != component or item.value is not value:
                raise ValueError("clarity live return identity differs")
        executor, provider = self._require_qualifier_live_call(
            observed[1],
            draft=draft,
            evidence=evidence,
            source=source,
            qualification_root=qualification_root,
        )
        faithful_call = observed[3]
        improved_call = observed[4]
        verifier_call = observed[5]
        cleanup_call = observed[6]
        if (
            type(faithful_call.receiver) is not NativeRescueExecutor
            or faithful_call.receiver is not executor
            or type(improved_call.receiver) is not NativeRescueExecutor
            or improved_call.receiver is not executor
        ):
            raise ValueError("clarity executor live receiver identity differs")
        faithful_arguments = dict(faithful_call.arguments)
        improved_arguments = dict(improved_call.arguments)
        if (
            faithful_arguments.get("plan") is not final
            or faithful_arguments.get("source") != source
            or faithful_arguments.get("work_root") != execution_root
            or improved_arguments.get("plan") is not final
            or improved_arguments.get("faithful") != faithful.output_path
            or improved_arguments.get("work_root") != execution_root
            or improved_arguments.get("source_mappings") is not faithful.source_mappings
            or improved_arguments.get("inherited_action_ids")
            is not faithful.applied_action_ids
        ):
            raise ValueError("clarity executor live call arguments differ")
        if type(verifier_call.receiver) is not RescueVerifier:
            raise ValueError("clarity verifier live receiver type differs")
        verifier = verifier_call.receiver
        if verifier._measurement_provider is not provider:
            raise ValueError("clarity verifier provider live identity differs")
        verifier_arguments = dict(verifier_call.arguments)
        if (
            verifier_arguments.get("source") != source
            or verifier_arguments.get("faithful") != faithful.output_path
            or verifier_arguments.get("improved") != improved.output_path
            or verifier_arguments.get("plan") is not final
            or verifier_arguments.get("verification_controls") is not controls
        ):
            raise ValueError("clarity verifier live call arguments differ")
        verifier_mappings = _require_public_verifier_mapping_projection(
            faithful.source_mappings,
            verifier_arguments.get("mappings"),
        )
        cleanup_arguments = dict(cleanup_call.arguments)
        if (
            cleanup_call.receiver is not None
            or cleanup_arguments.get("private_root") != execution_root
            or cleanup_arguments.get("handles") is not controls
        ):
            raise ValueError("clarity cleanup live call arguments differ")
        return verifier_mappings

    def _persist_terminal(
        self,
        envelope: ClarityRuntimeProvenanceV1,
    ) -> ClarityRuntimeProvenanceV1:
        path = write_clarity_runtime_provenance(self._audit_root, envelope)
        retained = read_clarity_runtime_provenance(path)
        if retained != envelope:
            raise ValueError("clarity retained envelope differs after readback")
        if retained.envelope_digest != envelope.envelope_digest:
            raise ValueError("clarity retained envelope digest differs after readback")
        self._terminal_path = path
        self._terminal_envelope = retained
        self._terminal_digest = retained.envelope_digest
        return retained

    def _partial_live_error_envelope(
        self,
        *,
        outcome: Literal["cancelled", "error"],
        phase: str,
        code: str,
        include_observed_prefix: bool,
    ) -> ClarityRuntimeProvenanceV1:
        tools: tuple[ClarityToolIdentityV1, ...] = ()
        source_projection: ClaritySourceProjectionV1 | None = None
        draft_projection: ClarityPlanProjectionV1 | None = None
        qualification_projection: ClarityQualificationProjectionV1 | None = None
        qualification_absent = False
        event_inputs: list[ClarityEventInput] = []
        if self._tools is not None and {item.role for item in self._tools} == {
            "ffmpeg",
            "ffprobe",
        }:
            tools = self._tools
            event_inputs.append(
                ClarityEventInput(
                    "tool_identity_verified",
                    "tool_identity_verifier",
                    "verified",
                    None,
                    provenance_digest({"tools": tools}),
                )
            )
        source_path = self._source_path
        if (
            source_path is not None
            and self._source_sha256_before is not None
            and self._source_size_before is not None
        ):
            try:
                source_projection = _require_source_after(
                    source=source_path,
                    source_sha256_after=_sha256_file(source_path),
                    bound_path=source_path,
                    sha256_before=self._source_sha256_before,
                    size_before=self._source_size_before,
                )
            except (OSError, TypeError, ValueError):
                source_projection = None
        observed = self._observer.observed_returns if include_observed_prefix else ()
        if source_projection is not None and observed:
            first = observed[0]
            if first.component == "build_rescue_plan":
                try:
                    draft, draft_action, draft_projection = _require_plan_identity(
                        first.value,
                        label="clarity partial draft plan",
                        allow_unqualified_draft=True,
                    )
                    if draft.input_hash != source_projection.sha256_before:
                        raise ValueError(
                            "clarity partial draft differs from source binding"
                        )
                except (TypeError, ValueError):
                    draft_projection = None
                else:
                    event_inputs.append(
                        ClarityEventInput(
                            "draft_bound",
                            "build_rescue_plan",
                            "verified",
                            source_projection.sha256_before,
                            provenance_digest(draft_projection),
                        )
                    )
                    if len(observed) >= 2 and observed[1].component == "qualify":
                        try:
                            evidence, qualification_projection = (
                                _require_qualification_binding(
                                    observed[1].value,
                                    draft=draft,
                                    draft_action=draft_action,
                                )
                            )
                            arguments = dict(observed[1].arguments)
                            qualification_root = arguments.get("work_root")
                            if not isinstance(qualification_root, Path):
                                raise ValueError(
                                    "clarity partial qualification root is invalid"
                                )
                            self._require_qualifier_live_call(
                                observed[1],
                                draft=draft,
                                evidence=evidence,
                                source=cast(Path, source_path),
                                qualification_root=qualification_root,
                            )
                            qualification_absent = not _path_exists_no_follow(
                                qualification_root
                            )
                            if not qualification_absent:
                                raise ValueError(
                                    "clarity partial qualification cleanup "
                                    "is incomplete"
                                )
                        except (TypeError, ValueError):
                            qualification_projection = None
                            qualification_absent = False
                        else:
                            event_inputs.extend(
                                (
                                    ClarityEventInput(
                                        "qualification_returned",
                                        "NativeRescueCandidateQualifier.qualify",
                                        "returned",
                                        provenance_digest(draft_projection),
                                        provenance_digest(qualification_projection),
                                    ),
                                    ClarityEventInput(
                                        "qualification_cleanup_verified",
                                        "qualification_cleanup",
                                        "verified",
                                        provenance_digest(qualification_projection),
                                        provenance_digest(
                                            {"qualification_root_absent": True}
                                        ),
                                    ),
                                )
                            )
        if source_projection is not None:
            event_inputs.append(
                ClarityEventInput(
                    "source_integrity_verified",
                    "source_integrity",
                    "verified",
                    source_projection.sha256_before,
                    source_projection.sha256_after,
                )
            )
        events = build_event_chain(tuple(event_inputs))
        return _strict_envelope(
            {
                "schema_version": "1",
                "track": "sharpen_clarity",
                "producer_version": "clarity_runtime_provenance_v1",
                "selector_id": "clarity_exact_native_v1",
                "outcome": outcome,
                "component_manifest": _component_manifest(),
                "tools": tools,
                "source": source_projection,
                "draft": draft_projection,
                "qualification": qualification_projection,
                "final": None,
                "runtime_recipe": None,
                "verification": None,
                "cleanup": ClarityCleanupProjectionV1(
                    qualification_root_absent=qualification_absent,
                    control_count=0,
                    controls_absent=False,
                    source_unchanged=source_projection is not None,
                    public_outputs_absent=False,
                ),
                "events": events,
                "error": ClarityErrorV1(phase=phase, code=code),
            }
        )

    def seal_success(
        self,
        *,
        source: Path,
        source_sha256_after: str,
        draft: RescuePlan,
        evidence: SharpenQualificationEvidenceV1,
        final: RescuePlan,
        faithful: RescueExecutionResult,
        improved: RescueImprovedExecutionResult,
        controls: tuple[SharpenVerificationControlHandle, ...],
        report: RescueVerificationReport,
        qualification_root: Path,
        execution_root: Path,
    ) -> ClarityRuntimeProvenanceV1:
        """Validate and persist one successful live clarity chain."""

        tools, bound_source, source_before, source_size = self._require_bindings()
        draft, draft_action, draft_projection = _require_plan_identity(
            draft,
            label="clarity draft plan",
            allow_unqualified_draft=True,
        )
        if draft.input_hash != source_before:
            raise ValueError("clarity draft hash differs from the source binding")
        evidence, qualification_projection = _require_qualification_binding(
            evidence,
            draft=draft,
            draft_action=draft_action,
        )
        if evidence.selected is None:
            raise ValueError("successful clarity seal requires a selected profile")
        final, final_action, _ = _require_plan_identity(
            final,
            label="clarity final plan",
            allow_unqualified_draft=False,
        )
        if (
            final.input_hash != draft.input_hash
            or final.effective_config != draft.effective_config
            or final_action.parameters.get("qualification")
            != evidence.model_dump(mode="json")
        ):
            raise ValueError("clarity final plan differs from the live qualification")
        if type(faithful) is not RescueExecutionResult:
            raise TypeError("faithful result must be an exact RescueExecutionResult")
        if type(improved) is not RescueImprovedExecutionResult:
            raise TypeError(
                "improved result must be an exact RescueImprovedExecutionResult"
            )
        if faithful.failed_source_ranges:
            raise ValueError("successful clarity faithful result cannot be partial")
        if not faithful.source_mappings or any(
            type(mapping) is not SourceMapping for mapping in faithful.source_mappings
        ):
            raise TypeError(
                "clarity source mappings must be exact SourceMapping values"
            )
        validate_plan_sharpen_output_range_contracts(
            final,
            faithful.source_mappings,
        )
        faithful_sha256 = _require_execution_file(
            faithful.output_path,
            execution_root,
            label="faithful clarity output",
        )
        improved_sha256 = _require_execution_file(
            improved.output_path,
            execution_root,
            label="improved clarity output",
        )
        if type(controls) is not tuple or len(controls) != 1:
            raise TypeError("clarity controls must contain one exact handle")
        if type(controls[0]) is not SharpenVerificationControlHandle:
            raise TypeError("clarity control must be an exact SHARPEN handle")
        if (
            type(improved.verification_controls) is not tuple
            or len(improved.verification_controls) != 1
            or improved.verification_controls[0] is not controls[0]
        ):
            raise ValueError("clarity live control identity differs")
        recipe = _require_exact_model(
            controls[0].recipe,
            SharpenVerificationControlRecipeV1,
            label="clarity runtime recipe",
        )
        selected = evidence.selected
        assert selected is not None
        if (
            recipe.plan_digest != final.plan_digest
            or recipe.action_id != final_action.id
            or recipe.source_ranges != final_action.source_ranges
            or recipe.output_ranges != evidence.output_ranges
            or recipe.encode_contract
            != canonical_video_encode_contract(final.effective_config)
            or recipe.baseline_sha256 != selected.baseline_sha256
            or recipe.visibility_control_sha256 != selected.visibility_control_sha256
            or recipe.candidate_sha256 != selected.candidate_sha256
            or recipe.candidate_sha256 != improved_sha256
            or recipe.normalized_pts_digest != selected.normalized_pts_digest
            or recipe.stream_topology_digest != selected.stream_topology_digest
            or recipe.inventory_frame_count != selected.inventory_frame_count
        ):
            raise ValueError("clarity runtime recipe differs from qualification")
        report = _require_exact_model(
            report,
            RescueVerificationReport,
            label="clarity verification report",
        )
        checks = tuple(
            check
            for check in report.checks
            if check.artifact == "improved"
            and check.check_id == "perceptible_sharpness_improvement"
        )
        if (
            report.plan_digest != final.plan_digest
            or report.improved_status is not RescueVerificationStatus.PASSED
            or report.outcome is not RescueOutcome.COMPLETED
            or len(checks) != 1
            or checks[0].status is not RescueVerificationStatus.PASSED
        ):
            raise ValueError("clarity verification report is not a passed live report")
        check = checks[0]
        measured = check.measured
        if (
            measured.get("valid") is not True
            or measured.get("reference") != "runtime_same_generation_visibility_control"
            or measured.get("source_ranges")
            != [list(value) for value in final_action.source_ranges]
            or measured.get("output_ranges")
            != [list(value) for value in evidence.output_ranges]
            or measured.get("runtime_control_recipe_valid") is not True
            or measured.get("selected_qualification_binding_valid") is not True
        ):
            raise ValueError("clarity verification binding flags or ranges differ")
        metric_payload: dict[str, int | float] = {}
        for name in _INTEGER_METRICS:
            actual = measured.get(name)
            expected_value = getattr(selected.metrics, name)
            metric_payload[name] = _canonicalize_clarity_count_metric(
                actual,
                expected_value,
                name=name,
            )
        for name in _FLOAT_METRICS:
            actual = measured.get(name)
            expected_value = getattr(selected.metrics, name)
            if type(actual) is not float or not math.isclose(
                actual,
                expected_value,
                rel_tol=0.0,
                abs_tol=1e-9,
            ):
                raise ValueError(f"clarity float metric {name} differs")
            metric_payload[name] = actual
        if (
            not isinstance(qualification_root, Path)
            or not qualification_root.is_absolute()
        ):
            raise ValueError("clarity qualification root must be an absolute Path")
        qualification_absent = not _path_exists_no_follow(qualification_root)
        controls_absent = all(
            not _path_exists_no_follow(path)
            for handle in controls
            for path in handle.cleanup_paths
        )
        public_absent = _public_outputs_absent(execution_root)
        if not qualification_absent:
            raise ValueError("clarity qualification cleanup is incomplete")
        if not controls_absent:
            raise ValueError("clarity runtime control cleanup is incomplete")
        if not public_absent:
            raise ValueError("clarity public output is unexpectedly present")
        source_projection = _require_source_after(
            source=source,
            source_sha256_after=source_sha256_after,
            bound_path=bound_source,
            sha256_before=source_before,
            size_before=source_size,
        )
        verifier_mappings = self._require_success_return_identities(
            draft=draft,
            evidence=evidence,
            final=final,
            faithful=faithful,
            improved=improved,
            report=report,
            source=source,
            qualification_root=qualification_root,
            execution_root=execution_root,
            controls=controls,
        )
        mappings_payload = tuple(
            {
                "source_start": mapping.source_start,
                "source_end": mapping.source_end,
                "output_start": mapping.output_start,
                "output_end": mapping.output_end,
                "output_relative_path": mapping.output_relative_path,
            }
            for mapping in verifier_mappings
        )
        final_projection = ClarityFinalProjectionV1(
            plan_digest=final.plan_digest,
            action_id=final_action.id,
            source_mappings_digest=provenance_digest(
                {"source_mappings": mappings_payload}
            ),
            output_ranges_digest=provenance_digest(
                {"output_ranges": evidence.output_ranges}
            ),
            faithful_sha256=faithful_sha256,
            improved_sha256=improved_sha256,
        )
        recipe_projection = ClarityRuntimeRecipeProjectionV1(
            recipe_digest=provenance_digest(recipe),
            baseline_sha256=recipe.baseline_sha256,
            visibility_control_sha256=recipe.visibility_control_sha256,
            candidate_sha256=recipe.candidate_sha256,
            normalized_pts_digest=recipe.normalized_pts_digest,
            stream_topology_digest=recipe.stream_topology_digest,
            inventory_frame_count=recipe.inventory_frame_count,
            source_ranges_digest=provenance_digest(
                {"source_ranges": recipe.source_ranges}
            ),
            output_ranges_digest=provenance_digest(
                {"output_ranges": recipe.output_ranges}
            ),
        )
        verification_projection = ClarityVerificationProjectionV1(
            report_digest=provenance_digest(report),
            required_check_id="perceptible_sharpness_improvement",
            required_check_status="passed",
            runtime_control_recipe_valid=True,
            selected_qualification_binding_valid=True,
            expected_frames=cast(int, metric_payload["expected_frames"]),
            compared_frames=cast(int, metric_payload["compared_frames"]),
            range_count=cast(int, metric_payload["range_count"]),
            passing_range_count=cast(int, metric_payload["passing_range_count"]),
            range_coverage_ratio=cast(float, metric_payload["range_coverage_ratio"]),
            minimum_aggregate_gain_ratio=cast(
                float, metric_payload["minimum_aggregate_gain_ratio"]
            ),
            minimum_recovered_baseline_ratio=cast(
                float, metric_payload["minimum_recovered_baseline_ratio"]
            ),
            minimum_improved_frame_fraction=cast(
                float, metric_payload["minimum_improved_frame_fraction"]
            ),
            maximum_noise_increase=cast(
                float, metric_payload["maximum_noise_increase"]
            ),
            maximum_edge_overshoot_ratio=cast(
                float, metric_payload["maximum_edge_overshoot_ratio"]
            ),
            maximum_edge_overshoot_amplitude=cast(
                float, metric_payload["maximum_edge_overshoot_amplitude"]
            ),
            maximum_ringing_ratio=cast(float, metric_payload["maximum_ringing_ratio"]),
            metrics_digest=provenance_digest(metric_payload),
        )
        cleanup = ClarityCleanupProjectionV1(
            qualification_root_absent=True,
            control_count=len(controls),
            controls_absent=True,
            source_unchanged=True,
            public_outputs_absent=True,
        )
        events = build_event_chain(
            (
                ClarityEventInput(
                    "tool_identity_verified",
                    "tool_identity_verifier",
                    "verified",
                    None,
                    provenance_digest({"tools": tools}),
                ),
                ClarityEventInput(
                    "draft_bound",
                    "build_rescue_plan",
                    "verified",
                    source_before,
                    provenance_digest(draft_projection),
                ),
                ClarityEventInput(
                    "qualification_returned",
                    "NativeRescueCandidateQualifier.qualify",
                    "returned",
                    provenance_digest(draft_projection),
                    provenance_digest(qualification_projection),
                ),
                ClarityEventInput(
                    "qualification_cleanup_verified",
                    "qualification_cleanup",
                    "verified",
                    provenance_digest(qualification_projection),
                    provenance_digest({"qualification_root_absent": True}),
                ),
                ClarityEventInput(
                    "final_plan_bound",
                    "build_rescue_plan",
                    "verified",
                    provenance_digest(qualification_projection),
                    final.plan_digest,
                ),
                ClarityEventInput(
                    "faithful_returned",
                    "NativeRescueExecutor.execute_faithful",
                    "returned",
                    final.plan_digest,
                    faithful_sha256,
                ),
                ClarityEventInput(
                    "improved_returned",
                    "NativeRescueExecutor.execute_improved_with_controls",
                    "returned",
                    faithful_sha256,
                    provenance_digest(recipe_projection),
                ),
                ClarityEventInput(
                    "verification_returned",
                    "RescueVerifier.verify",
                    "returned",
                    provenance_digest(recipe_projection),
                    provenance_digest(verification_projection),
                ),
                ClarityEventInput(
                    "controls_cleanup_returned",
                    "_cleanup_verification_controls",
                    "returned",
                    provenance_digest(recipe_projection),
                    provenance_digest({"controls_absent": True}),
                ),
                ClarityEventInput(
                    "source_integrity_verified",
                    "source_integrity",
                    "verified",
                    source_before,
                    source_projection.sha256_after,
                ),
                ClarityEventInput(
                    "publication_absence_verified",
                    "publication_absence",
                    "verified",
                    None,
                    provenance_digest({"public_outputs_absent": True}),
                ),
            )
        )
        envelope = _strict_envelope(
            {
                "schema_version": "1",
                "track": "sharpen_clarity",
                "producer_version": "clarity_runtime_provenance_v1",
                "selector_id": "clarity_exact_native_v1",
                "outcome": "passed",
                "component_manifest": _component_manifest(),
                "tools": tools,
                "source": source_projection,
                "draft": draft_projection,
                "qualification": qualification_projection,
                "final": final_projection,
                "runtime_recipe": recipe_projection,
                "verification": verification_projection,
                "cleanup": cleanup,
                "events": events,
                "error": None,
            }
        )
        return self._persist_terminal(envelope)

    def seal_no_profile(
        self,
        *,
        source: Path,
        source_sha256_after: str,
        draft: RescuePlan,
        evidence: SharpenQualificationEvidenceV1,
        qualification_root: Path,
        execution_root: Path,
    ) -> ClarityRuntimeProvenanceV1:
        """Persist an honest no-profile outcome without executing final media."""

        tools, bound_source, source_before, source_size = self._require_bindings()
        draft, draft_action, draft_projection = _require_plan_identity(
            draft,
            label="clarity draft plan",
            allow_unqualified_draft=True,
        )
        if draft.input_hash != source_before:
            raise ValueError("clarity draft hash differs from the source binding")
        evidence, qualification_projection = _require_qualification_binding(
            evidence,
            draft=draft,
            draft_action=draft_action,
        )
        if evidence.selected is not None:
            raise ValueError(
                "no-profile clarity seal cannot contain a selected profile"
            )
        self._observer.require_intact()
        observed = self._observer.observed_returns
        if (
            len(observed) != 2
            or observed[0].component != "build_rescue_plan"
            or observed[0].value is not draft
            or observed[1].component != "qualify"
            or observed[1].value is not evidence
        ):
            raise ValueError(
                "no-profile clarity runtime sequence contains later returns"
            )
        self._require_qualifier_live_call(
            observed[1],
            draft=draft,
            evidence=evidence,
            source=source,
            qualification_root=qualification_root,
        )
        if (
            not isinstance(qualification_root, Path)
            or not qualification_root.is_absolute()
        ):
            raise ValueError("clarity qualification root must be an absolute Path")
        if _path_exists_no_follow(qualification_root):
            raise ValueError("clarity qualification cleanup is incomplete")
        if not isinstance(execution_root, Path) or not execution_root.is_absolute():
            raise ValueError("clarity execution root must be an absolute Path")
        if _path_exists_no_follow(execution_root):
            raise ValueError("no-profile clarity seal forbids final execution media")
        public_absent = _public_outputs_absent(execution_root)
        if not public_absent:
            raise ValueError("clarity public output is unexpectedly present")
        source_projection = _require_source_after(
            source=source,
            source_sha256_after=source_sha256_after,
            bound_path=bound_source,
            sha256_before=source_before,
            size_before=source_size,
        )
        cleanup = ClarityCleanupProjectionV1(
            qualification_root_absent=True,
            control_count=0,
            controls_absent=True,
            source_unchanged=True,
            public_outputs_absent=True,
        )
        events = build_event_chain(
            (
                ClarityEventInput(
                    "tool_identity_verified",
                    "tool_identity_verifier",
                    "verified",
                    None,
                    provenance_digest({"tools": tools}),
                ),
                ClarityEventInput(
                    "draft_bound",
                    "build_rescue_plan",
                    "verified",
                    source_before,
                    provenance_digest(draft_projection),
                ),
                ClarityEventInput(
                    "qualification_returned",
                    "NativeRescueCandidateQualifier.qualify",
                    "returned",
                    provenance_digest(draft_projection),
                    provenance_digest(qualification_projection),
                ),
                ClarityEventInput(
                    "qualification_cleanup_verified",
                    "qualification_cleanup",
                    "verified",
                    provenance_digest(qualification_projection),
                    provenance_digest({"qualification_root_absent": True}),
                ),
                ClarityEventInput(
                    "source_integrity_verified",
                    "source_integrity",
                    "verified",
                    source_before,
                    source_projection.sha256_after,
                ),
                ClarityEventInput(
                    "publication_absence_verified",
                    "publication_absence",
                    "verified",
                    None,
                    provenance_digest({"public_outputs_absent": True}),
                ),
            )
        )
        envelope = _strict_envelope(
            {
                "schema_version": "1",
                "track": "sharpen_clarity",
                "producer_version": "clarity_runtime_provenance_v1",
                "selector_id": "clarity_exact_native_v1",
                "outcome": "no_profile_passed",
                "component_manifest": _component_manifest(),
                "tools": tools,
                "source": source_projection,
                "draft": draft_projection,
                "qualification": qualification_projection,
                "final": None,
                "runtime_recipe": None,
                "verification": None,
                "cleanup": cleanup,
                "events": events,
                "error": None,
            }
        )
        return self._persist_terminal(envelope)

    def _read_stored_terminal_seal(self) -> ClarityRuntimeProvenanceV1 | None:
        stored = (
            self._terminal_path,
            self._terminal_envelope,
            self._terminal_digest,
        )
        if all(item is None for item in stored):
            return None
        if any(item is None for item in stored):
            raise ValueError("clarity runtime terminal seal is incomplete")
        path = cast(Path, self._terminal_path)
        envelope = cast(ClarityRuntimeProvenanceV1, self._terminal_envelope)
        digest = cast(str, self._terminal_digest)
        if path != self._audit_root / _FINAL_NAME:
            raise ValueError("clarity runtime terminal seal path differs")
        retained = read_clarity_runtime_provenance(path)
        if retained != envelope or retained.envelope_digest != digest:
            raise ValueError("clarity runtime terminal seal readback differs")
        return retained

    def finalize_from_pytest_item(self, item: pytest.Item) -> None:
        """Finalize once from sanitized pytest call facts and restore profiling."""

        if self._finalized:
            raise ValueError("clarity runtime guard is already finalized")
        report = item.stash.get(CLARITY_CALL_REPORT_KEY, None)
        observer_error: ValueError | None = None
        try:
            self._observer.require_intact()
        except ValueError as error:
            observer_error = error
        finally:
            self._observer.stop()
            self._started = False
            self._finalized = True

        partial_outcome: Literal["cancelled", "error"] | None = None
        error_phase: str | None = None
        error_code: str | None = None
        failure_message: str | None = None

        terminal: ClarityRuntimeProvenanceV1 | None = None
        terminal_error: ValueError | None = None
        try:
            terminal = self._read_stored_terminal_seal()
        except (OSError, ValueError):
            terminal_error = ValueError("clarity runtime terminal seal readback failed")

        if observer_error is not None:
            partial_outcome = "error"
            error_phase = "observer"
            error_code = "observer_replaced"
            failure_message = str(observer_error)
        elif terminal_error is not None:
            partial_outcome = "error"
            error_phase = "selector_finalize"
            error_code = "terminal_seal_invalid"
            failure_message = str(terminal_error)
        elif report is None:
            partial_outcome = "error"
            error_phase = "pytest_lifecycle"
            error_code = "pytest_call_report_missing"
        elif report.outcome != "passed":
            if terminal is None:
                cancelled = report.exception_type is RescueCancelledError
                partial_outcome = "cancelled" if cancelled else "error"
                error_phase = "selector_call"
                error_code = (
                    "pytest_call_cancelled" if cancelled else "pytest_call_failed"
                )
        elif terminal is None:
            partial_outcome = "error"
            error_phase = "selector_finalize"
            error_code = "missing_terminal_seal"
            failure_message = "clarity runtime terminal seal is missing"
        elif terminal.outcome == "no_profile_passed":
            failure_message = (
                "clarity no-profile terminal requires a failed pytest call"
            )
        elif terminal.outcome == "passed":
            try:
                self._observer._require_complete()
            except ValueError as error:
                partial_outcome = "error"
                error_phase = "observer"
                error_code = "runtime_sequence_invalid"
                failure_message = str(error)

        if partial_outcome is not None:
            assert error_phase is not None
            assert error_code is not None
            if not _path_exists_no_follow(self._audit_root):
                write_clarity_runtime_provenance(
                    self._audit_root,
                    self._partial_live_error_envelope(
                        outcome=partial_outcome,
                        phase=error_phase,
                        code=error_code,
                        include_observed_prefix=observer_error is None,
                    ),
                )
        if failure_message is not None and report is not None:
            if report.outcome == "passed":
                raise ValueError(failure_message)


__all__ = [
    "CLARITY_CALL_REPORT_KEY",
    "CommandRunner",
    "DEFAULT_COMPONENTS",
    "EXACT_CLARITY_NODE_ID",
    "ClarityCleanupProjectionV1",
    "ClarityComponentIdentityV1",
    "ClarityErrorV1",
    "ClarityEventInput",
    "ClarityFinalProjectionV1",
    "ClarityPlanProjectionV1",
    "ClarityPytestCallReport",
    "ClarityQualificationProjectionV1",
    "ClarityRuntimeEventV1",
    "ClarityRuntimeGuard",
    "ClarityRuntimeObserver",
    "ClarityRuntimeProvenanceV1",
    "ClarityRuntimeRecipeProjectionV1",
    "ClaritySourceProjectionV1",
    "ClarityToolIdentityV1",
    "ClarityVerificationProjectionV1",
    "ClarityObservedReturn",
    "ProductionComponentSpec",
    "build_event_chain",
    "canonical_provenance_bytes",
    "production_component",
    "provenance_digest",
    "read_clarity_runtime_provenance",
    "verify_clarity_tool_identity",
    "write_clarity_runtime_provenance",
]
