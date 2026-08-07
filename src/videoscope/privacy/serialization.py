"""Canonical UTF-8 JSON codecs for versioned Safe Sharing documents."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from videoscope.privacy.models import (
    PrivacyChangeLog,
    PrivacyPlan,
    PrivacyRiskMap,
    PrivacyTechnicalReport,
)

PrivacyJsonModel = TypeVar("PrivacyJsonModel", bound=BaseModel)


def privacy_risk_map_to_json(risk_map: PrivacyRiskMap) -> str:
    """Serialize a private or public risk map deterministically."""
    return _to_json(risk_map, PrivacyRiskMap)


def privacy_risk_map_from_json(content: str | bytes) -> PrivacyRiskMap:
    """Deserialize and validate a versioned privacy risk map."""
    return PrivacyRiskMap.model_validate_json(content)


def write_privacy_risk_map_json(risk_map: PrivacyRiskMap, path: Path) -> None:
    """Atomically write a risk map as UTF-8 JSON with one final newline."""
    _write_json(privacy_risk_map_to_json(risk_map), path)


def read_privacy_risk_map_json(path: Path) -> PrivacyRiskMap:
    """Read and validate a versioned privacy risk map."""
    return privacy_risk_map_from_json(Path(path).read_bytes())


def privacy_plan_to_json(plan: PrivacyPlan) -> str:
    """Serialize a confirmation-bound privacy plan deterministically."""
    return _to_json(plan, PrivacyPlan)


def privacy_plan_from_json(content: str | bytes) -> PrivacyPlan:
    """Deserialize and validate a confirmation-bound privacy plan."""
    return PrivacyPlan.model_validate_json(content)


def write_privacy_plan_json(plan: PrivacyPlan, path: Path) -> None:
    """Atomically write a privacy plan as UTF-8 JSON with one final newline."""
    _write_json(privacy_plan_to_json(plan), path)


def read_privacy_plan_json(path: Path) -> PrivacyPlan:
    """Read and validate a confirmation-bound privacy plan."""
    return privacy_plan_from_json(Path(path).read_bytes())


def privacy_change_log_to_json(change_log: PrivacyChangeLog) -> str:
    """Serialize a Safe Sharing change log deterministically."""
    return _to_json(change_log, PrivacyChangeLog)


def privacy_change_log_from_json(content: str | bytes) -> PrivacyChangeLog:
    """Deserialize and validate a Safe Sharing change log."""
    return PrivacyChangeLog.model_validate_json(content)


def write_privacy_change_log_json(change_log: PrivacyChangeLog, path: Path) -> None:
    """Atomically write a Safe Sharing change log with one final newline."""
    _write_json(privacy_change_log_to_json(change_log), path)


def read_privacy_change_log_json(path: Path) -> PrivacyChangeLog:
    """Read and validate a Safe Sharing change log."""
    return privacy_change_log_from_json(Path(path).read_bytes())


def privacy_technical_report_to_json(report: PrivacyTechnicalReport) -> str:
    """Serialize a public Safe Sharing technical report deterministically."""
    return _to_json(report, PrivacyTechnicalReport)


def privacy_technical_report_from_json(content: str | bytes) -> PrivacyTechnicalReport:
    """Deserialize and validate a public Safe Sharing technical report."""
    return PrivacyTechnicalReport.model_validate_json(content)


def write_privacy_technical_report_json(
    report: PrivacyTechnicalReport,
    path: Path,
) -> None:
    """Atomically write a technical report with one final UTF-8 newline."""
    _write_json(privacy_technical_report_to_json(report), path)


def read_privacy_technical_report_json(path: Path) -> PrivacyTechnicalReport:
    """Read and validate a public Safe Sharing technical report."""
    return privacy_technical_report_from_json(Path(path).read_bytes())


def _to_json(value: PrivacyJsonModel, model_type: type[PrivacyJsonModel]) -> str:
    """Revalidate and serialize one document with fixed canonical formatting."""
    validated = model_type.model_validate(value.model_dump(mode="python"))
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )


def _write_json(content: str, path: Path) -> None:
    """Atomically replace a destination using a same-directory temporary file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(f"{content}\n")
            stream.flush()
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
