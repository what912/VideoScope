"""Sequential, failure-isolated detector execution."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from time import perf_counter
from typing import Any

from pydantic import BaseModel

from videoscope.detectors.interface import Detector
from videoscope.detectors.models import (
    AnalysisContext,
    DetectorRunResult,
)
from videoscope.detectors.registry import DetectorRegistry
from videoscope.domain import (
    DetectorExecution,
    DetectorStatus,
    Finding,
)
from videoscope.video.errors import sanitize_diagnostic

DetectorConfigInput = BaseModel | Mapping[str, Any] | None
Clock = Callable[[], float]

_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s,;]+"
)
_WINDOWS_PATH_PATTERN = re.compile(r"(?<![\w])(?:[A-Za-z]:[\\/])(?:[^\s,;]+)")
_UNC_PATH_PATTERN = re.compile(r"(?<![\w])\\\\[^\s\\]+\\[^\s,;]+")
_POSIX_PATH_PATTERN = re.compile(r"(?<![\w])/(?:[^\s,;]+)")


def _sanitize_exception(
    exc: Exception,
    *,
    context: AnalysisContext,
) -> str:
    message = sanitize_diagnostic(
        str(exc),
        sensitive_paths=(context.input_path, context.workspace),
    )
    if context.prompt:
        message = message.replace(context.prompt, "<prompt>")
    message = _CREDENTIAL_PATTERN.sub(
        lambda match: f"{match.group(1)}=<redacted>",
        message,
    )
    message = _WINDOWS_PATH_PATTERN.sub("<path>", message)
    message = _UNC_PATH_PATTERN.sub("<path>", message)
    message = _POSIX_PATH_PATTERN.sub("<path>", message)
    message = " ".join(message.split())
    return message or f"{type(exc).__name__} without diagnostic details"


def _validate_config(
    detector: Detector,
    raw_config: DetectorConfigInput,
) -> BaseModel:
    if raw_config is None:
        return detector.config_model.model_validate({})
    if isinstance(raw_config, BaseModel):
        return detector.config_model.model_validate(raw_config.model_dump())
    return detector.config_model.model_validate(dict(raw_config))


def _validate_findings(
    detector: Detector,
    raw_findings: object,
) -> list[Finding]:
    if not isinstance(raw_findings, list):
        raise TypeError("detector analyze() must return a list of Finding")
    findings: list[Finding] = []
    seen_ids: set[str] = set()
    for item in raw_findings:
        if not isinstance(item, Finding):
            raise TypeError("detector analyze() returned a non-Finding value")
        if item.detector_id != detector.id:
            raise ValueError("Finding detector_id does not match detector id")
        if item.detector_version != detector.version:
            raise ValueError("Finding detector_version does not match detector version")
        if item.id in seen_ids:
            raise ValueError("detector returned duplicate Finding IDs")
        seen_ids.add(item.id)
        findings.append(item)
    return sorted(findings, key=Finding.sort_key)


class DetectorRunner:
    """Run selected detectors sequentially while isolating ordinary failures."""

    def __init__(
        self,
        registry: DetectorRegistry,
        *,
        clock: Clock = perf_counter,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.registry = registry
        self._clock = clock
        self._progress = progress

    def run(
        self,
        context: AnalysisContext,
        *,
        detector_ids: Sequence[str] | None = None,
        configurations: Mapping[str, DetectorConfigInput] | None = None,
    ) -> DetectorRunResult:
        """Run detectors in explicit or stable registry order."""
        selected = (
            tuple(self.registry.get(detector_id) for detector_id in detector_ids)
            if detector_ids is not None
            else self.registry.list_default_enabled()
        )
        effective_configurations = configurations or {}
        executions: list[DetectorExecution] = []
        findings: list[Finding] = []

        for detector in selected:
            if self._progress is not None:
                self._progress(f"Running detector: {detector.id}")
            if detector.requirements.requires_prompt and not (
                context.prompt and context.prompt.strip()
            ):
                executions.append(
                    DetectorExecution(
                        detector_id=detector.id,
                        status=DetectorStatus.SKIPPED,
                        elapsed_seconds=0.0,
                        findings_count=0,
                    )
                )
                continue
            started_at = self._clock()
            try:
                config = _validate_config(
                    detector,
                    effective_configurations.get(detector.id),
                )
                detector_findings = _validate_findings(
                    detector,
                    detector.analyze(context, config),
                )
            except Exception as exc:
                elapsed = max(0.0, self._clock() - started_at)
                executions.append(
                    DetectorExecution(
                        detector_id=detector.id,
                        status=DetectorStatus.DETECTOR_ERROR,
                        elapsed_seconds=elapsed,
                        findings_count=0,
                        error_type=type(exc).__name__,
                        error_message=_sanitize_exception(exc, context=context),
                    )
                )
                continue

            elapsed = max(0.0, self._clock() - started_at)
            findings.extend(detector_findings)
            executions.append(
                DetectorExecution(
                    detector_id=detector.id,
                    status=DetectorStatus.OK,
                    elapsed_seconds=elapsed,
                    findings_count=len(detector_findings),
                )
            )

        return DetectorRunResult(
            executions=tuple(executions),
            findings=tuple(sorted(findings, key=Finding.sort_key)),
        )
