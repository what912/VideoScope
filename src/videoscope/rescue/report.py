"""Offline, escaped HTML rendering for validated Video Rescue documents."""

from __future__ import annotations

import math
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from videoscope.rescue.executor import SourceMapping
from videoscope.rescue.models import (
    RescuePlan,
    RescueTechnicalReport,
    RescueVerificationStatus,
)


def render_rescue_report(
    plan: RescuePlan,
    report: RescueTechnicalReport,
    mappings: tuple[SourceMapping, ...] = (),
) -> str:
    """Render only validated public models; no source path or remote asset is used."""
    validated_plan = RescuePlan.model_validate(plan.model_dump(mode="python"))
    validated_report = RescueTechnicalReport.model_validate(
        report.model_dump(mode="python", exclude_unset=True)
    )
    validated_mappings = _validate_mappings(mappings)
    downloadable_artifacts = tuple(
        artifact
        for artifact in validated_report.artifacts
        if _artifact_is_downloadable(artifact.relative_path, validated_report)
    )
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "reporting" / "templates"),
        autoescape=select_autoescape(default_for_string=True, default=True),
        undefined=StrictUndefined,
    )
    rendered = cast(
        str,
        environment.get_template("rescue_report.html.j2").render(
            plan=validated_plan,
            report=validated_report,
            mappings=validated_mappings,
            downloadable_artifacts=downloadable_artifacts,
        ),
    )
    if not validated_report.action_execution_state_known:
        rendered = rendered.replace(
            "No action execution record is available.",
            "Execution state unknown for this legacy record; regenerate the "
            "Rescue operation to obtain an explicit action ledger.",
        )
    if "http://" in rendered.casefold() or "https://" in rendered.casefold():
        raise ValueError("Rescue report must not contain remote resources or URLs")
    return rendered


def _validate_mappings(
    mappings: tuple[SourceMapping, ...],
) -> tuple[SourceMapping, ...]:
    previous_output_end = 0.0
    validated: list[SourceMapping] = []
    for mapping in mappings:
        if not isinstance(mapping, SourceMapping):
            raise ValueError("Rescue report source mapping has an invalid type")
        seconds = (
            mapping.source_start,
            mapping.source_end,
            mapping.output_start,
            mapping.output_end,
        )
        if any(not math.isfinite(value) or value < 0 for value in seconds):
            raise ValueError("Rescue report source mapping has invalid seconds")
        if (
            mapping.source_end <= mapping.source_start
            or mapping.output_end <= mapping.output_start
            or not math.isclose(mapping.output_start, previous_output_end, abs_tol=1e-9)
        ):
            raise ValueError("Rescue report source mapping is not ordered")
        posix = PurePosixPath(mapping.output_relative_path)
        windows = PureWindowsPath(mapping.output_relative_path)
        if (
            not mapping.output_relative_path
            or mapping.output_relative_path != posix.as_posix()
            or "\\" in mapping.output_relative_path
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise ValueError("Rescue report source mapping path is not safely relative")
        validated.append(mapping)
        previous_output_end = mapping.output_end
    return tuple(validated)


def _artifact_is_downloadable(
    relative_path: str,
    report: RescueTechnicalReport,
) -> bool:
    if relative_path == "faithful-rescue.mp4":
        return report.verification.faithful_status is RescueVerificationStatus.PASSED
    if relative_path == "improved-viewing.mp4":
        return report.verification.improved_status in {
            RescueVerificationStatus.PASSED,
            RescueVerificationStatus.NEEDS_REVIEW,
        }
    return False


__all__ = ["render_rescue_report"]
