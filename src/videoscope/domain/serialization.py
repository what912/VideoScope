"""Stable UTF-8 JSON serialization for VideoScope reports."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from videoscope.domain.models import AnalysisReport


def report_to_json(report: AnalysisReport, *, indent: int = 2) -> str:
    """Serialize a report with stable keys and unescaped Unicode."""
    validated_report = AnalysisReport.model_validate(report.model_dump(mode="python"))
    data = validated_report.model_dump(mode="json")
    return json.dumps(
        data,
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
        allow_nan=False,
    )


def report_from_json(content: str | bytes) -> AnalysisReport:
    """Validate and deserialize a UTF-8 report."""
    return AnalysisReport.model_validate_json(content)


def write_report_json(report: AnalysisReport, path: Path) -> None:
    """Atomically write a stable report as UTF-8 with Unix newlines."""
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
            stream.write(f"{report_to_json(report)}\n")
            stream.flush()
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_report_json(path: Path) -> AnalysisReport:
    """Read and validate a UTF-8 report from disk."""
    return report_from_json(path.read_bytes())


def analysis_report_json_schema() -> dict[str, Any]:
    """Return the versioned JSON Schema for AnalysisReport."""
    return AnalysisReport.model_json_schema()
