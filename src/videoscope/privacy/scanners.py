"""Deterministic, failure-isolated privacy scanner orchestration."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from videoscope.privacy.metadata import PrivateProbeSummary
from videoscope.privacy.models import (
    PrivacyRisk,
    make_privacy_risk_id,
    privacy_risk_sort_key,
)
from videoscope.privacy.profiles import ShareAudienceProfile
from videoscope.scenes import VideoScene
from videoscope.video import FrameSample

ScannerConfigInput = BaseModel | Mapping[str, Any] | None
ScannerClock = Callable[[], float]
CancellationCallback = Callable[[], bool]


class _ScannerModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrivacyScannerRequirements(_ScannerModel):
    """Capabilities and optional packages declared by a privacy scanner."""

    requires_gpu: bool = False
    requires_network: bool = False
    optional_packages: tuple[str, ...] = ()
    estimated_cost: Literal["low", "medium", "high"] = "low"

    @field_validator("optional_packages")
    @classmethod
    def normalize_packages(cls, packages: tuple[str, ...]) -> tuple[str, ...]:
        if any(not package.strip() for package in packages):
            raise ValueError("optional package names must not be blank")
        return tuple(sorted(set(packages)))


class PrivacyScanContext(BaseModel):
    """Prepared local-only inputs shared with privacy scanners."""

    model_config = ConfigDict(
        extra="forbid",
        arbitrary_types_allowed=True,
        frozen=True,
    )

    input_path: Path
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    profile: ShareAudienceProfile
    workspace: Path
    frame_samples: tuple[FrameSample, ...] = ()
    scenes: tuple[VideoScene, ...] = ()
    private_probe_summary: PrivateProbeSummary | None = None
    private_text_values: tuple[str, ...] = Field(default=(), exclude=True)
    shared_cache: dict[str, Any] = Field(default_factory=dict, exclude=True)
    cancellation_callback: CancellationCallback | None = Field(
        default=None,
        exclude=True,
    )

    def is_cancelled(self) -> bool:
        """Query cancellation without converting it into a scanner failure."""
        return (
            self.cancellation_callback()
            if self.cancellation_callback is not None
            else False
        )

    def resolve_frame_path(self, relative_path: str) -> Path:
        """Resolve one sampled frame without allowing workspace escape."""
        candidate_path = Path(relative_path)
        if candidate_path.is_absolute():
            raise ValueError("sampled frame path must stay inside the workspace")
        workspace = self.workspace.resolve()
        candidate = (workspace / candidate_path).resolve()
        if not candidate.is_relative_to(workspace):
            raise ValueError("sampled frame path must stay inside the workspace")
        if not candidate.is_file():
            raise ValueError("sampled frame is unavailable inside the workspace")
        return candidate


class PrivacyScannerStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    SCANNER_ERROR = "scanner_error"


class PrivacyScannerSkipped(RuntimeError):
    """Signal an unavailable optional scanner with a safe manual fallback."""

    def __init__(self, *, fallback: str) -> None:
        normalized = fallback.strip()
        if not normalized:
            raise ValueError("scanner fallback must not be blank")
        self.fallback = normalized
        super().__init__("optional privacy scanner is unavailable")


class PrivacyScannerExecution(_ScannerModel):
    """Public-safe execution record for one privacy scanner."""

    scanner_id: str = Field(min_length=1)
    status: PrivacyScannerStatus
    elapsed_seconds: float = Field(ge=0, allow_inf_nan=False)
    risks_count: int = Field(ge=0)
    fallback: str | None = None
    error_type: str | None = None
    error_message: str | None = None

    @field_validator("fallback")
    @classmethod
    def normalize_fallback(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("scanner fallback must not be blank")
        return normalized


class PrivacyScannerRunResult(_ScannerModel):
    """Ordered executions and deterministically sorted risk proposals."""

    executions: tuple[PrivacyScannerExecution, ...] = ()
    risks: tuple[PrivacyRisk, ...] = ()


class PrivacyScanner(Protocol):
    """Scanner metadata and execution contract independent of implementations."""

    id: str
    display_name: str
    version: str
    description: str
    requirements: PrivacyScannerRequirements
    config_model: type[BaseModel]

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        """Return reviewable privacy risks without changing media."""
        ...


class PrivacyScannerRegistry:
    """Register privacy scanners under unique stable IDs."""

    def __init__(self, scanners: Iterable[PrivacyScanner] = ()) -> None:
        self._scanners: dict[str, PrivacyScanner] = {}
        for scanner in scanners:
            self.register(scanner)

    def register(self, scanner: PrivacyScanner) -> None:
        scanner_id = getattr(scanner, "id", "")
        if not isinstance(scanner_id, str) or not scanner_id.strip():
            raise ValueError("scanner id must be a non-empty string")
        if scanner_id != scanner_id.strip():
            raise ValueError("scanner id must not have surrounding whitespace")
        if scanner_id in self._scanners:
            raise ValueError(f"duplicate privacy scanner id: {scanner_id}")
        for attribute in ("display_name", "version", "description"):
            value = getattr(scanner, attribute, None)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"scanner {scanner_id!r} has invalid {attribute}")
        if not isinstance(scanner.requirements, PrivacyScannerRequirements):
            raise ValueError(f"scanner {scanner_id!r} has invalid requirements")
        if not isinstance(scanner.config_model, type) or not issubclass(
            scanner.config_model, BaseModel
        ):
            raise ValueError(
                f"scanner {scanner_id!r} config_model must be a Pydantic model"
            )
        self._scanners[scanner_id] = scanner

    def get(self, scanner_id: str) -> PrivacyScanner:
        try:
            return self._scanners[scanner_id]
        except KeyError as exc:
            raise KeyError(f"unknown privacy scanner: {scanner_id}") from exc

    def list_available(self) -> tuple[PrivacyScanner, ...]:
        return tuple(self._scanners[key] for key in sorted(self._scanners))


class PrivacyScannerRunner:
    """Run scanners sequentially while isolating ordinary exceptions."""

    def __init__(
        self,
        scanners: Iterable[PrivacyScanner] | PrivacyScannerRegistry,
        *,
        clock: ScannerClock = perf_counter,
    ) -> None:
        self.registry = (
            scanners
            if isinstance(scanners, PrivacyScannerRegistry)
            else PrivacyScannerRegistry(scanners)
        )
        self._clock = clock

    def run(
        self,
        context: PrivacyScanContext,
        configurations: Mapping[str, ScannerConfigInput],
        *,
        scanner_ids: Sequence[str] | None = None,
    ) -> PrivacyScannerRunResult:
        """Run selected scanners in explicit or stable registry order."""
        available_ids = {scanner.id for scanner in self.registry.list_available()}
        unknown_configurations = sorted(set(configurations) - available_ids)
        if unknown_configurations:
            raise ValueError(
                "unknown privacy scanner configuration IDs: "
                + ", ".join(unknown_configurations)
            )
        if scanner_ids is not None and len(scanner_ids) != len(set(scanner_ids)):
            raise ValueError("duplicate explicit privacy scanner ID")
        selected = (
            tuple(self.registry.get(scanner_id) for scanner_id in scanner_ids)
            if scanner_ids is not None
            else self.registry.list_available()
        )
        executions: list[PrivacyScannerExecution] = []
        risks: list[PrivacyRisk] = []
        for scanner in selected:
            started_at = self._clock()
            try:
                config = _validate_config(scanner, configurations.get(scanner.id))
                scanner_risks = _validate_risks(
                    scanner,
                    context,
                    scanner.scan(context, config),
                )
            except PrivacyScannerSkipped as exc:
                executions.append(
                    PrivacyScannerExecution(
                        scanner_id=scanner.id,
                        status=PrivacyScannerStatus.SKIPPED,
                        elapsed_seconds=max(0.0, self._clock() - started_at),
                        risks_count=0,
                        fallback=exc.fallback,
                    )
                )
                continue
            except Exception as exc:
                executions.append(
                    PrivacyScannerExecution(
                        scanner_id=scanner.id,
                        status=PrivacyScannerStatus.SCANNER_ERROR,
                        elapsed_seconds=max(0.0, self._clock() - started_at),
                        risks_count=0,
                        error_type=type(exc).__name__,
                        error_message=_public_error_message(exc),
                    )
                )
                continue
            risks.extend(scanner_risks)
            executions.append(
                PrivacyScannerExecution(
                    scanner_id=scanner.id,
                    status=PrivacyScannerStatus.OK,
                    elapsed_seconds=max(0.0, self._clock() - started_at),
                    risks_count=len(scanner_risks),
                )
            )
        return PrivacyScannerRunResult(
            executions=tuple(executions),
            risks=tuple(sorted(risks, key=privacy_risk_sort_key)),
        )


def _validate_config(
    scanner: PrivacyScanner,
    raw_config: ScannerConfigInput,
) -> BaseModel:
    if raw_config is None:
        return scanner.config_model.model_validate({})
    if isinstance(raw_config, BaseModel):
        return scanner.config_model.model_validate(raw_config.model_dump())
    return scanner.config_model.model_validate(dict(raw_config))


def _validate_risks(
    scanner: PrivacyScanner,
    context: PrivacyScanContext,
    raw_risks: object,
) -> list[PrivacyRisk]:
    if not isinstance(raw_risks, list):
        raise TypeError("privacy scanner must return a list of PrivacyRisk")
    risks: list[PrivacyRisk] = []
    seen: set[str] = set()
    for risk in raw_risks:
        if not isinstance(risk, PrivacyRisk):
            raise TypeError("privacy scanner returned a non-PrivacyRisk value")
        if risk.scanner_id != scanner.id:
            raise ValueError("risk scanner_id does not match privacy scanner id")
        if risk.scanner_version != scanner.version:
            raise ValueError("risk scanner_version does not match scanner version")
        if not risk.evidence:
            raise ValueError("privacy risk proposal requires observable evidence")
        if not risk.limitations:
            raise ValueError("privacy risk proposal requires explicit limitations")
        if risk.end_seconds > context.duration_seconds:
            raise ValueError("privacy risk interval exceeds media duration")
        expected_id = make_privacy_risk_id(
            context.input_hash,
            risk.scanner_id,
            risk.risk_type,
            risk.start_seconds,
            risk.end_seconds,
            risk.box,
        )
        if risk.id != expected_id:
            raise ValueError("privacy risk ID does not match scanner observation")
        if risk.id in seen:
            raise ValueError("privacy scanner returned duplicate risk IDs")
        seen.add(risk.id)
        risks.append(risk)
    return sorted(risks, key=privacy_risk_sort_key)


def _public_error_message(exc: Exception) -> str:
    """Return a useful error class without retaining private exception text."""
    return f"{type(exc).__name__} while running privacy scanner; details redacted"


__all__ = [
    "PrivacyScanContext",
    "PrivacyScanner",
    "PrivacyScannerExecution",
    "PrivacyScannerRegistry",
    "PrivacyScannerRequirements",
    "PrivacyScannerRunResult",
    "PrivacyScannerRunner",
    "PrivacyScannerSkipped",
    "PrivacyScannerStatus",
]
