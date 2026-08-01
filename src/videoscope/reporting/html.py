"""Offline, self-contained HTML rendering for analysis reports."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from jinja2 import Environment, PackageLoader, StrictUndefined

from videoscope.domain import AnalysisReport, DetectorStatus, Severity

REPORT_FILENAME = "report.html"
_MINIMUM_MARKER_PERCENT = 0.35


def _format_seconds(value: float) -> str:
    """Format seconds as a copyable HH:MM:SS.mmm timestamp."""
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _safe_relative_url(value: str | None) -> str | None:
    """Return a URL-encoded report-local path or reject unsafe paths."""
    if value is None:
        return None
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.parts[0].endswith(":")
    ):
        return None
    return quote(path.as_posix(), safe="/")


def _json_display(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )


def _ocr_box_overlays(metadata: dict[str, Any]) -> list[dict[str, str]]:
    """Validate OCR evidence boxes and convert them to CSS percentages."""
    raw_boxes = metadata.get("ocr_boxes")
    if not isinstance(raw_boxes, list):
        return []
    overlays: list[dict[str, str]] = []
    for raw_item in raw_boxes:
        if not isinstance(raw_item, dict):
            continue
        raw_box = raw_item.get("bounding_box")
        if not isinstance(raw_box, dict):
            continue
        try:
            x_min = float(raw_box["x_min"])
            y_min = float(raw_box["y_min"])
            x_max = float(raw_box["x_max"])
            y_max = float(raw_box["y_max"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= x_min < x_max <= 1 and 0 <= y_min < y_max <= 1):
            continue
        text = str(raw_item.get("text", "")).strip()
        confidence = raw_item.get("confidence")
        label = text or "Detected text"
        if isinstance(confidence, (int, float)):
            label = f"{label} ({float(confidence):.2f})"
        overlays.append(
            {
                "left": f"{x_min * 100:.6f}",
                "top": f"{y_min * 100:.6f}",
                "width": f"{(x_max - x_min) * 100:.6f}",
                "height": f"{(y_max - y_min) * 100:.6f}",
                "label": label,
            }
        )
    return overlays


class HTMLReportRenderer:
    """Render a validated report as an offline HTML document."""

    def __init__(self) -> None:
        self._environment = Environment(
            loader=PackageLoader("videoscope.reporting", "templates"),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(
        self,
        report: AnalysisReport,
        output_directory: Path,
        *,
        bundled_video_relative_path: str | None = None,
    ) -> Path:
        """Atomically write report.html into the selected output directory."""
        validated = AnalysisReport.model_validate(report.model_dump(mode="python"))
        destination_directory = Path(output_directory)
        destination_directory.mkdir(parents=True, exist_ok=True)
        destination = destination_directory / REPORT_FILENAME
        template = self._environment.get_template("report.html.j2")
        duration = validated.metadata.duration_seconds
        severity_counts = Counter(finding.severity for finding in validated.findings)
        detector_ids = sorted(
            {execution.detector_id for execution in validated.detector_executions}
            | {finding.detector_id for finding in validated.findings}
        )
        findings: list[dict[str, Any]] = []
        for finding in validated.findings:
            start = finding.time_range.start_seconds
            end = finding.time_range.end_seconds
            if duration > 0:
                left_percent = min(100.0, max(0.0, start / duration * 100.0))
                natural_width = max(0.0, (end - start) / duration * 100.0)
                width_percent = min(
                    100.0 - left_percent,
                    max(_MINIMUM_MARKER_PERCENT, natural_width),
                )
            else:
                left_percent = 0.0
                width_percent = _MINIMUM_MARKER_PERCENT
            findings.append(
                {
                    "model": finding,
                    "start_label": _format_seconds(start),
                    "end_label": _format_seconds(end),
                    "left_percent": f"{left_percent:.6f}",
                    "width_percent": f"{width_percent:.6f}",
                    "parameters_json": _json_display(finding.parameters),
                    "evidence": [
                        {
                            "model": evidence,
                            "url": _safe_relative_url(evidence.relative_path),
                            "timestamp_label": _format_seconds(
                                evidence.timestamp_seconds
                            ),
                            "metadata_json": _json_display(evidence.metadata),
                            "ocr_boxes": _ocr_box_overlays(evidence.metadata),
                        }
                        for evidence in finding.evidence
                    ],
                }
            )
        failed_executions = [
            execution
            for execution in validated.detector_executions
            if execution.status is DetectorStatus.DETECTOR_ERROR
        ]
        locale = str(validated.configuration.get("locale", "en")).strip() or "en"
        html = template.render(
            report=validated,
            findings=findings,
            detector_ids=detector_ids,
            failed_executions=failed_executions,
            severity_values=[severity.value for severity in Severity],
            severity_counts={
                severity.value: severity_counts[severity] for severity in Severity
            },
            duration_label=_format_seconds(duration),
            bundled_video_url=_safe_relative_url(bundled_video_relative_path),
            locale=locale,
            format_seconds=_format_seconds,
            json_display=_json_display,
        )
        self._atomic_write(destination, f"{html.rstrip()}\n")
        return destination

    @staticmethod
    def _atomic_write(destination: Path, content: str) -> None:
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
                stream.write(content)
                stream.flush()
            temporary_path.replace(destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
