"""Deterministic UTF-8 JSON codecs for Publish Ready public artifacts."""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from videoscope.resolve.models import (
    PublishChangeLog,
    PublishPlan,
    PublishTechnicalReport,
)

ResolveJsonModel = TypeVar("ResolveJsonModel", bound=BaseModel)

_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS = (0.01, 0.02, 0.04, 0.08, 0.16)


def publish_plan_to_json(plan: PublishPlan) -> str:
    """Serialize a validated plan using canonical, unescaped UTF-8 JSON."""
    return _to_json(plan, PublishPlan)


def publish_plan_from_json(content: str | bytes) -> PublishPlan:
    """Deserialize and validate a PublishPlan JSON document."""
    return PublishPlan.model_validate_json(content)


def write_publish_plan_json(plan: PublishPlan, path: Path) -> None:
    """Atomically write a plan document as UTF-8 with a trailing newline."""
    _write_json(publish_plan_to_json(plan), path)


def read_publish_plan_json(path: Path) -> PublishPlan:
    """Read and validate a UTF-8 plan document."""
    return publish_plan_from_json(Path(path).read_bytes())


def publish_change_log_to_json(change_log: PublishChangeLog) -> str:
    """Serialize an executed-action change log deterministically."""
    return _to_json(change_log, PublishChangeLog)


def publish_change_log_from_json(content: str | bytes) -> PublishChangeLog:
    """Deserialize and validate a PublishChangeLog JSON document."""
    return PublishChangeLog.model_validate_json(content)


def write_publish_change_log_json(change_log: PublishChangeLog, path: Path) -> None:
    """Atomically write a UTF-8 change log with a trailing newline."""
    _write_json(publish_change_log_to_json(change_log), path)


def read_publish_change_log_json(path: Path) -> PublishChangeLog:
    """Read and validate a UTF-8 change log."""
    return publish_change_log_from_json(Path(path).read_bytes())


def publish_technical_report_to_json(
    technical_report: PublishTechnicalReport,
) -> str:
    """Serialize a technical report deterministically."""
    return _to_json(technical_report, PublishTechnicalReport)


def publish_technical_report_from_json(
    content: str | bytes,
) -> PublishTechnicalReport:
    """Deserialize and validate a PublishTechnicalReport JSON document."""
    return PublishTechnicalReport.model_validate_json(content)


def write_publish_technical_report_json(
    technical_report: PublishTechnicalReport, path: Path
) -> None:
    """Atomically write a UTF-8 technical report with a trailing newline."""
    _write_json(publish_technical_report_to_json(technical_report), path)


def read_publish_technical_report_json(path: Path) -> PublishTechnicalReport:
    """Read and validate a UTF-8 technical report."""
    return publish_technical_report_from_json(Path(path).read_bytes())


def _to_json(
    value: ResolveJsonModel,
    model_type: type[ResolveJsonModel],
) -> str:
    """Revalidate then canonically serialize one public Resolve model."""
    validated = model_type.model_validate(value.model_dump(mode="python"))
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )


def _retry_windows_replace(
    source: Path,
    destination: Path,
    *,
    replace: Callable[[Path, Path], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    replace_file = replace or os.replace
    sleep_for = sleep or time.sleep
    for delay in (*_WINDOWS_REPLACE_RETRY_DELAYS_SECONDS, None):
        try:
            replace_file(source, destination)
        except OSError as error:
            if (
                delay is None
                or getattr(error, "winerror", None) != 5
                or not os.path.lexists(source)
            ):
                raise
            sleep_for(delay)
        else:
            return


def _write_json(content: str, path: Path) -> None:
    """Atomically replace one destination with a same-directory temporary file."""
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
        _retry_windows_replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
