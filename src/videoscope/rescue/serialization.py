"""Canonical UTF-8 JSON codecs for versioned Video Rescue documents."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from videoscope.rescue.models import (
    MediaDamageMap,
    RescueChangeLog,
    RescuePlan,
    RescueTechnicalReport,
)

RescueJsonModel = TypeVar("RescueJsonModel", bound=BaseModel)


def damage_map_to_json(damage_map: MediaDamageMap) -> str:
    """Serialize a validated damage map deterministically."""
    return _to_json(damage_map, MediaDamageMap)


def damage_map_from_json(content: str | bytes) -> MediaDamageMap:
    """Deserialize and validate a versioned damage map."""
    return MediaDamageMap.model_validate_json(content)


def write_damage_map_json(damage_map: MediaDamageMap, path: Path) -> None:
    """Atomically write a damage map as UTF-8 JSON with one final newline."""
    _write_json(damage_map_to_json(damage_map), path)


def read_damage_map_json(path: Path) -> MediaDamageMap:
    """Read and validate a versioned damage map."""
    return damage_map_from_json(Path(path).read_bytes())


def rescue_plan_to_json(plan: RescuePlan) -> str:
    """Serialize a confirmation-bound Rescue plan deterministically."""
    return _to_json(plan, RescuePlan)


def rescue_plan_from_json(content: str | bytes) -> RescuePlan:
    """Deserialize and validate a confirmation-bound Rescue plan."""
    return RescuePlan.model_validate_json(content)


def write_rescue_plan_json(plan: RescuePlan, path: Path) -> None:
    """Atomically write a Rescue plan as UTF-8 JSON with one final newline."""
    _write_json(rescue_plan_to_json(plan), path)


def read_rescue_plan_json(path: Path) -> RescuePlan:
    """Read and validate a confirmation-bound Rescue plan."""
    return rescue_plan_from_json(Path(path).read_bytes())


def rescue_change_log_to_json(change_log: RescueChangeLog) -> str:
    """Serialize a Rescue execution change log deterministically."""
    return _to_json(change_log, RescueChangeLog)


def rescue_change_log_from_json(content: str | bytes) -> RescueChangeLog:
    """Deserialize and validate a Rescue execution change log."""
    return RescueChangeLog.model_validate_json(content)


def write_rescue_change_log_json(change_log: RescueChangeLog, path: Path) -> None:
    """Atomically write a Rescue change log as UTF-8 JSON."""
    _write_json(rescue_change_log_to_json(change_log), path)


def read_rescue_change_log_json(path: Path) -> RescueChangeLog:
    """Read and validate a Rescue execution change log."""
    return rescue_change_log_from_json(Path(path).read_bytes())


def rescue_technical_report_to_json(report: RescueTechnicalReport) -> str:
    """Serialize a public Rescue technical report deterministically."""
    return _to_json(report, RescueTechnicalReport)


def rescue_technical_report_from_json(content: str | bytes) -> RescueTechnicalReport:
    """Deserialize and validate a public Rescue technical report."""
    return RescueTechnicalReport.model_validate_json(content)


def write_rescue_technical_report_json(
    report: RescueTechnicalReport,
    path: Path,
) -> None:
    """Atomically write a Rescue technical report as UTF-8 JSON."""
    _write_json(rescue_technical_report_to_json(report), path)


def read_rescue_technical_report_json(path: Path) -> RescueTechnicalReport:
    """Read and validate a public Rescue technical report."""
    return rescue_technical_report_from_json(Path(path).read_bytes())


def _to_json(value: RescueJsonModel, model_type: type[RescueJsonModel]) -> str:
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
    """Replace a destination through a same-directory temporary file."""
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
