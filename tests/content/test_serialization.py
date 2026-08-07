"""Canonical JSON serialization for Long Video to Useful Content."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.content.test_models import (
    INPUT_HASH,
    make_content_map,
    make_plan,
    make_verification,
)
from videoscope.content.models import (
    CONTENT_SCHEMA_VERSION,
    ContentArtifact,
    ContentArtifactRole,
    ContentGoal,
    ContentOutcome,
    ContentTechnicalReport,
)
from videoscope.content.serialization import (
    content_map_to_json,
    content_plan_to_json,
    content_technical_report_from_json,
    content_technical_report_to_json,
    read_content_map_json,
    read_content_plan_json,
    read_content_technical_report_json,
    write_content_map_json,
    write_content_plan_json,
    write_content_technical_report_json,
)


def make_report() -> ContentTechnicalReport:
    verification = make_verification()
    return ContentTechnicalReport(
        input_hash=INPUT_HASH,
        goal=ContentGoal.CHAPTERED_FULL,
        outcome=ContentOutcome.COMPLETED,
        plan_digest=verification.plan_digest,
        artifacts=(
            ContentArtifact(
                role=ContentArtifactRole.MEDIA,
                relative_path="content-output/useful-content.mp4",
                sha256="b" * 64,
                description="验证后的成品视频",
            ),
        ),
        chapters=(),
        source_mappings=(),
        change_log=None,
        verification=verification,
        warnings=("中文内容保持 UTF-8。",),
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("writer", "reader", "factory"),
    [
        (write_content_map_json, read_content_map_json, make_content_map),
        (write_content_plan_json, read_content_plan_json, make_plan),
        (
            write_content_technical_report_json,
            read_content_technical_report_json,
            make_report,
        ),
    ],
)
def test_atomic_round_trip_in_unicode_directory(
    tmp_path: Path,
    writer: Callable[[object, Path], None],
    reader: Callable[[Path], object],
    factory: Callable[[], object],
) -> None:
    destination = tmp_path / "中文 目录" / "result.json"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    value = factory()

    writer(value, destination)

    assert reader(destination) == value
    assert list(destination.parent.glob("*.tmp")) == []
    assert destination.read_bytes().endswith(b"\n")


def test_json_is_stable_sorted_and_preserves_chinese() -> None:
    first = content_technical_report_to_json(make_report())
    second = content_technical_report_to_json(make_report())

    assert first == second
    assert "验证后的成品视频" in first
    assert "\\u9a8c" not in first
    assert list(json.loads(first)) == sorted(json.loads(first))


def test_all_primary_documents_use_current_schema() -> None:
    assert CONTENT_SCHEMA_VERSION == "0.1"
    assert (
        json.loads(content_map_to_json(make_content_map()))["schema_version"] == "0.1"
    )
    assert json.loads(content_plan_to_json(make_plan()))["schema_version"] == "0.1"


def test_unknown_schema_and_unknown_fields_fail_closed() -> None:
    payload = json.loads(content_technical_report_to_json(make_report()))
    payload["schema_version"] = "9.9"

    with pytest.raises(ValidationError):
        content_technical_report_from_json(json.dumps(payload))

    payload = json.loads(content_technical_report_to_json(make_report()))
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        content_technical_report_from_json(json.dumps(payload))
