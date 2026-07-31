"""Stable JSON serialization tests for analysis reports."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from videoscope.domain import (
    read_report_json,
    report_from_json,
    report_to_json,
    write_report_json,
)

from .test_models import make_report


def test_json_preserves_chinese_and_round_trips() -> None:
    report = make_report()

    content = report_to_json(report)
    restored = report_from_json(content)

    assert "一只猫在窗边" in content
    assert "示例 视频.mp4" in content
    assert "\\u4e00" not in content
    assert restored == report


def test_json_output_is_stable() -> None:
    report = make_report()

    first = report_to_json(report)
    second = report_to_json(report)

    assert first == second
    top_level_keys = [
        line.strip().split('"', maxsplit=2)[1]
        for line in first.splitlines()
        if line.startswith('  "') and '":' in line
    ]
    assert top_level_keys == sorted(top_level_keys)


def test_serialization_revalidates_copied_models() -> None:
    report = make_report()
    invalid_finding = report.findings[0].model_copy(
        update={"id": f"finding_{'0' * 64}"}
    )
    invalid_report = report.model_copy(update={"findings": [invalid_finding]})

    with pytest.raises(ValidationError, match="deterministic ID"):
        report_to_json(invalid_report)


def test_file_round_trip_supports_spaces_and_chinese(tmp_path: Path) -> None:
    report = make_report()
    output_path = tmp_path / "中文 报告.json"

    write_report_json(report, output_path)
    restored = read_report_json(output_path)

    assert output_path.read_bytes().endswith(b"\n")
    assert restored == report


def test_atomic_write_preserves_existing_report_on_serialization_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_path = tmp_path / "report.json"
    output_path.write_text("previous-report", encoding="utf-8")

    def fail_serialization(report: object) -> str:
        del report
        raise RuntimeError("injected serialization failure")

    monkeypatch.setattr(
        "videoscope.domain.serialization.report_to_json",
        fail_serialization,
    )

    with pytest.raises(RuntimeError, match="injected"):
        write_report_json(make_report(), output_path)

    assert output_path.read_text(encoding="utf-8") == "previous-report"
    assert not list(tmp_path.glob(".report.json.*.tmp"))
