"""Validation and deterministic behavior tests for report domain models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from videoscope.domain import (
    AnalysisReport,
    DetectorExecution,
    DetectorStatus,
    Evidence,
    Finding,
    Severity,
    TimeRange,
    VideoMetadata,
    analysis_report_json_schema,
    make_finding_id,
)

INPUT_HASH = "ab" * 32


def make_finding(
    *,
    start_seconds: float = 1.0,
    end_seconds: float = 2.0,
    detector_id: str = "core.test",
    severity: Severity = Severity.MEDIUM,
    score: float = 0.5,
    confidence: float = 0.75,
    title: str = "可观察异常",
) -> Finding:
    time_range = TimeRange(
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        start_frame=30,
        end_frame=60,
    )
    return Finding(
        id=make_finding_id(
            input_hash=INPUT_HASH,
            detector_id=detector_id,
            time_range=time_range,
        ),
        detector_id=detector_id,
        detector_version="1.0.0",
        title=title,
        description="在该区间观察到相对变化。",
        severity=severity,
        score=score,
        confidence=confidence,
        time_range=time_range,
        evidence=[
            Evidence(
                evidence_type="frame",
                timestamp_seconds=start_seconds,
                relative_path="evidence/证据 frame.jpg",
                description="区间代表帧",
                metadata={"亮度": 0.25},
            )
        ],
        tags=["测试"],
        parameters={"threshold": 0.2},
        limitations=["这是启发式观察。"],
    )


def make_report(findings: list[Finding] | None = None) -> AnalysisReport:
    selected_findings = findings if findings is not None else [make_finding()]
    return AnalysisReport(
        tool_version="0.1.0",
        analysis_id="analysis-test",
        created_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
        input_hash=INPUT_HASH,
        prompt="一只猫在窗边",
        metadata=VideoMetadata(
            filename="示例 视频.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=5.0,
            average_frame_rate=30.0,
            estimated_frame_count=150,
            has_audio=True,
            file_size_bytes=1024,
            raw_probe={"format_name": "mov,mp4"},
        ),
        configuration={"profile": "cpu-default"},
        detector_executions=[
            DetectorExecution(
                detector_id="core.test",
                status=DetectorStatus.OK,
                elapsed_seconds=0.1,
                findings_count=len(selected_findings),
            )
        ],
        findings=selected_findings,
        warnings=["仅用于测试"],
        runtime={"python": "3.12.1", "platform": "Windows"},
    )


def test_time_range_rejects_reversed_interval() -> None:
    with pytest.raises(ValidationError, match="end_seconds"):
        TimeRange(start_seconds=2.0, end_seconds=1.0)


def test_time_range_allows_zero_length_interval() -> None:
    time_range = TimeRange(start_seconds=2.0, end_seconds=2.0)

    assert time_range.start_seconds == time_range.end_seconds


def test_time_range_rejects_reversed_frame_interval() -> None:
    with pytest.raises(ValidationError, match="end_frame"):
        TimeRange(
            start_seconds=1.0,
            end_seconds=2.0,
            start_frame=20,
            end_frame=10,
        )


def test_finding_rejects_scores_outside_unit_interval() -> None:
    invalid_values = [
        (-0.01, 0.5),
        (1.01, 0.5),
        (0.5, -0.01),
        (0.5, 1.01),
    ]
    for score, confidence in invalid_values:
        with pytest.raises(ValidationError):
            make_finding(score=score, confidence=confidence)


def test_finding_id_is_deterministic_and_interval_sensitive() -> None:
    first_range = TimeRange(
        start_seconds=1.25,
        end_seconds=2.5,
        start_frame=30,
        end_frame=60,
    )
    second_range = first_range.model_copy(update={"end_seconds": 2.75})

    first = make_finding_id(
        input_hash=INPUT_HASH,
        detector_id="core.freeze",
        time_range=first_range,
    )
    repeated = make_finding_id(
        input_hash=INPUT_HASH,
        detector_id="core.freeze",
        time_range=first_range,
    )
    changed = make_finding_id(
        input_hash=INPUT_HASH,
        detector_id="core.freeze",
        time_range=second_range,
    )
    changed_video = make_finding_id(
        input_hash="cd" * 32,
        detector_id="core.freeze",
        time_range=first_range,
    )
    changed_detector = make_finding_id(
        input_hash=INPUT_HASH,
        detector_id="core.black",
        time_range=first_range,
    )

    assert first == repeated
    assert first != changed
    assert first != changed_video
    assert first != changed_detector
    assert first.startswith("finding_")


def test_report_rejects_non_deterministic_finding_id() -> None:
    finding = make_finding().model_copy(
        update={"id": f"finding_{'0' * 64}"},
    )

    with pytest.raises(ValidationError, match="deterministic ID"):
        make_report([finding])


def test_findings_use_normative_sort_order() -> None:
    findings = [
        make_finding(
            start_seconds=1.0,
            end_seconds=2.0,
            detector_id="core.z",
            severity=Severity.MEDIUM,
        ),
        make_finding(
            start_seconds=0.0,
            end_seconds=0.5,
            detector_id="core.z",
            severity=Severity.CRITICAL,
        ),
        make_finding(
            start_seconds=1.0,
            end_seconds=2.1,
            detector_id="core.z",
            severity=Severity.INFO,
        ),
        make_finding(
            start_seconds=1.0,
            end_seconds=2.0,
            detector_id="core.a",
            severity=Severity.INFO,
        ),
    ]

    report = make_report(findings)

    assert [
        (
            finding.time_range.start_seconds,
            finding.severity,
            finding.detector_id,
        )
        for finding in report.findings
    ] == [
        (0.0, Severity.CRITICAL, "core.z"),
        (1.0, Severity.INFO, "core.a"),
        (1.0, Severity.INFO, "core.z"),
        (1.0, Severity.MEDIUM, "core.z"),
    ]


def test_detector_error_requires_error_details() -> None:
    with pytest.raises(ValidationError, match="requires both"):
        DetectorExecution(
            detector_id="core.test",
            status=DetectorStatus.DETECTOR_ERROR,
            elapsed_seconds=0.1,
            findings_count=0,
        )


def test_created_at_requires_timezone() -> None:
    report = make_report()

    with pytest.raises(ValidationError, match="timezone"):
        AnalysisReport.model_validate(
            report.model_dump() | {"created_at": datetime(2026, 7, 28, 12, 0)}
        )


def test_json_schema_contains_versioned_domain_models() -> None:
    schema = analysis_report_json_schema()

    assert "schema_version" in schema["properties"]
    assert "Finding" in schema["$defs"]
    assert "critical" in schema["$defs"]["Severity"]["enum"]
