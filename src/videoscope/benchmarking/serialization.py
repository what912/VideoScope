"""Stable atomic JSON serialization for benchmark reports."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from videoscope.benchmarking.models import BenchmarkReport


def benchmark_to_json(report: BenchmarkReport, *, indent: int = 2) -> str:
    """Serialize a validated benchmark report with stable UTF-8 keys."""
    validated = BenchmarkReport.model_validate(report.model_dump(mode="python"))
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        indent=indent,
        sort_keys=True,
        allow_nan=False,
    )


def write_benchmark_json(report: BenchmarkReport, path: Path) -> Path:
    """Atomically write benchmark.json."""
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
            stream.write(f"{benchmark_to_json(report)}\n")
            stream.flush()
        temporary_path.replace(destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return destination
