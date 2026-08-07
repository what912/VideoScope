"""Canonical UTF-8 JSON for Long Video to Useful Content documents."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from videoscope.content.models import (
    ContentChangeLog,
    ContentMap,
    ContentPlan,
    ContentTechnicalReport,
    Storyboard,
)

ContentJsonModel = TypeVar("ContentJsonModel", bound=BaseModel)


def content_map_to_json(value: ContentMap) -> str:
    return _to_json(value, ContentMap)


def content_map_from_json(content: str | bytes) -> ContentMap:
    return ContentMap.model_validate_json(content)


def write_content_map_json(value: ContentMap, path: Path) -> None:
    _write_json(content_map_to_json(value), path)


def read_content_map_json(path: Path) -> ContentMap:
    return content_map_from_json(Path(path).read_bytes())


def storyboard_to_json(value: Storyboard) -> str:
    return _to_json(value, Storyboard)


def storyboard_from_json(content: str | bytes) -> Storyboard:
    return Storyboard.model_validate_json(content)


def write_storyboard_json(value: Storyboard, path: Path) -> None:
    _write_json(storyboard_to_json(value), path)


def read_storyboard_json(path: Path) -> Storyboard:
    return storyboard_from_json(Path(path).read_bytes())


def content_plan_to_json(value: ContentPlan) -> str:
    return _to_json(value, ContentPlan)


def content_plan_from_json(content: str | bytes) -> ContentPlan:
    return ContentPlan.model_validate_json(content)


def write_content_plan_json(value: ContentPlan, path: Path) -> None:
    _write_json(content_plan_to_json(value), path)


def read_content_plan_json(path: Path) -> ContentPlan:
    return content_plan_from_json(Path(path).read_bytes())


def content_change_log_to_json(value: ContentChangeLog) -> str:
    return _to_json(value, ContentChangeLog)


def content_change_log_from_json(content: str | bytes) -> ContentChangeLog:
    return ContentChangeLog.model_validate_json(content)


def write_content_change_log_json(value: ContentChangeLog, path: Path) -> None:
    _write_json(content_change_log_to_json(value), path)


def read_content_change_log_json(path: Path) -> ContentChangeLog:
    return content_change_log_from_json(Path(path).read_bytes())


def content_technical_report_to_json(value: ContentTechnicalReport) -> str:
    return _to_json(value, ContentTechnicalReport)


def content_technical_report_from_json(
    content: str | bytes,
) -> ContentTechnicalReport:
    return ContentTechnicalReport.model_validate_json(content)


def write_content_technical_report_json(
    value: ContentTechnicalReport, path: Path
) -> None:
    _write_json(content_technical_report_to_json(value), path)


def read_content_technical_report_json(path: Path) -> ContentTechnicalReport:
    return content_technical_report_from_json(Path(path).read_bytes())


def _to_json(value: ContentJsonModel, model_type: type[ContentJsonModel]) -> str:
    validated = model_type.model_validate(value.model_dump(mode="python"))
    return json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    )


def _write_json(content: str, path: Path) -> None:
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
