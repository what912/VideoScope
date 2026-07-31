"""Unified VideoScope report domain."""

from videoscope.domain.models import (
    SCHEMA_VERSION,
    AnalysisReport,
    DetectorExecution,
    DetectorStatus,
    Evidence,
    Finding,
    Severity,
    TimeRange,
    VideoMetadata,
    make_finding_id,
)
from videoscope.domain.serialization import (
    analysis_report_json_schema,
    read_report_json,
    report_from_json,
    report_to_json,
    write_report_json,
)

__all__ = [
    "SCHEMA_VERSION",
    "AnalysisReport",
    "DetectorExecution",
    "DetectorStatus",
    "Evidence",
    "Finding",
    "Severity",
    "TimeRange",
    "VideoMetadata",
    "analysis_report_json_schema",
    "make_finding_id",
    "read_report_json",
    "report_from_json",
    "report_to_json",
    "write_report_json",
]
