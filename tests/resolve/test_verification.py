"""Tests for profile-specific Publish Ready output verification."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from videoscope.domain import (
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
from videoscope.resolve.models import VerificationStatus
from videoscope.resolve.profiles import COMPATIBLE_MP4, SOCIAL_VERTICAL
from videoscope.resolve.verification import PublishVerifier

_DETECTOR_IDS = ("near_black", "possible_freeze")


def _metadata(
    *,
    width: int = 1920,
    height: int = 1080,
    duration_seconds: float = 10.0,
    fps: float = 30.0,
    has_audio: bool = True,
    container: str = "mov,mp4,m4a,3gp,3g2,mj2",
    codec: str = "h264",
    pixel_format: str = "yuv420p",
    audio_codec: str | None = "aac",
    filename: str = "源 视频 ü.mp4",
) -> VideoMetadata:
    raw_probe: dict[str, JsonValue] = {"pixel_format": pixel_format}
    if audio_codec is not None:
        raw_probe["audio_codec"] = audio_codec
    return VideoMetadata(
        filename=filename,
        container_format=container,
        codec=codec,
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        average_frame_rate=fps,
        estimated_frame_count=round(duration_seconds * fps),
        has_audio=has_audio,
        file_size_bytes=4096,
        raw_probe=raw_probe,
    )


def _finding(
    *,
    input_hash: str,
    detector_id: str,
    start_seconds: float,
    end_seconds: float,
    severity: Severity = Severity.HIGH,
) -> Finding:
    time_range = TimeRange(
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )
    return Finding(
        id=make_finding_id(
            input_hash=input_hash,
            detector_id=detector_id,
            time_range=time_range,
        ),
        detector_id=detector_id,
        detector_version="1.0.0",
        title="Observable interval",
        description="Detector-local metrics crossed the configured threshold.",
        severity=severity,
        score=0.8,
        confidence=0.8,
        time_range=time_range,
        evidence=[
            Evidence(
                evidence_type="frame",
                timestamp_seconds=start_seconds,
                relative_path="evidence/frame.jpg",
                description="Representative frame.",
            )
        ],
    )


def _report(
    metadata: VideoMetadata,
    *,
    input_hash: str,
    findings: Sequence[Finding] = (),
    statuses: Mapping[str, DetectorStatus] | None = None,
    omitted_detectors: Sequence[str] = (),
) -> AnalysisReport:
    effective_statuses = statuses or {}
    executions: list[DetectorExecution] = []
    for detector_id in _DETECTOR_IDS:
        if detector_id in omitted_detectors:
            continue
        status = effective_statuses.get(detector_id, DetectorStatus.OK)
        count = sum(item.detector_id == detector_id for item in findings)
        executions.append(
            DetectorExecution(
                detector_id=detector_id,
                status=status,
                elapsed_seconds=0.1,
                findings_count=count if status is DetectorStatus.OK else 0,
                error_type=(
                    "InjectedDetectorError"
                    if status is DetectorStatus.DETECTOR_ERROR
                    else None
                ),
                error_message=(
                    "Detector execution did not complete."
                    if status is DetectorStatus.DETECTOR_ERROR
                    else None
                ),
            )
        )
    return AnalysisReport(
        tool_version="0.3.0.dev0",
        analysis_id=f"analysis-{input_hash[0]}",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        input_hash=input_hash,
        metadata=metadata,
        detector_executions=executions,
        findings=list(findings),
    )


def test_valid_output_passes_checks_in_deterministic_order() -> None:
    source = _metadata()
    output = _metadata(width=1080, height=1920, filename="发布 输出 日本語.mp4")

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64),
        after=_report(output, input_hash="b" * 64),
    )

    assert report.status is VerificationStatus.PASSED
    assert report.profile_id is SOCIAL_VERTICAL.id
    assert report.profile_version == SOCIAL_VERTICAL.version
    assert [check.check_id for check in report.checks] == [
        "decodable",
        "duration",
        "dimensions",
        "container",
        "video_codec",
        "pixel_format",
        "frame_rate",
        "audio_stream",
        "audio_codec",
        "near_black_regression",
        "possible_freeze_regression",
    ]
    assert all(check.status is VerificationStatus.PASSED for check in report.checks)
    assert report.manual_review_reasons == ()
    serialized = report.model_dump_json()
    assert source.filename not in serialized
    assert output.filename not in serialized


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("output", "failed_check"),
    [
        (_metadata(width=1079, height=1920), "dimensions"),
        (_metadata(width=1080, height=1920, codec="hevc"), "video_codec"),
        (_metadata(width=1080, height=1920, fps=60.01), "frame_rate"),
        (_metadata(width=1080, height=1920, fps=0.0), "frame_rate"),
        (
            _metadata(width=1080, height=1920, container="matroska,webm"),
            "container",
        ),
        (
            _metadata(width=1080, height=1920, pixel_format="yuv444p"),
            "pixel_format",
        ),
        (
            _metadata(width=1080, height=1920, audio_codec="opus"),
            "audio_codec",
        ),
        (
            _metadata(
                width=1080,
                height=1920,
                has_audio=False,
                audio_codec=None,
            ),
            "audio_stream",
        ),
    ],
)
def test_incompatible_technical_output_fails(
    output: VideoMetadata,
    failed_check: str,
) -> None:
    source = _metadata()

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64),
        after=_report(output, input_hash="b" * 64),
    )

    statuses = {check.check_id: check.status for check in report.checks}
    assert statuses[failed_check] is VerificationStatus.FAILED
    assert report.status is VerificationStatus.FAILED


def test_duration_drift_above_frame_based_tolerance_fails() -> None:
    source = _metadata(duration_seconds=10.0, fps=2.0)
    output = _metadata(width=1080, height=1920, duration_seconds=11.001)

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64),
        after=_report(output, input_hash="b" * 64),
    )

    duration = next(check for check in report.checks if check.check_id == "duration")
    assert duration.status is VerificationStatus.FAILED
    assert duration.measured["tolerance_seconds"] == 1.0
    assert report.status is VerificationStatus.FAILED


def test_undecodable_output_fails_without_path_disclosure() -> None:
    source = _metadata(filename="绝对路径不应出现.mp4")

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=None,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64),
        after=None,
    )

    decodable = report.checks[0]
    assert decodable.check_id == "decodable"
    assert decodable.status is VerificationStatus.FAILED
    assert report.status is VerificationStatus.FAILED
    assert source.filename not in report.model_dump_json()


def test_unavailable_output_uses_an_audio_probe_diagnostic_for_silent_source() -> None:
    """A missing output probe must not claim that silent source audio was lost."""
    source = _metadata(has_audio=False, audio_codec=None)

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=None,
        profile=COMPATIBLE_MP4,
        before=_report(source, input_hash="a" * 64),
        after=None,
    )

    audio = next(check for check in report.checks if check.check_id == "audio_stream")
    assert audio.status is VerificationStatus.FAILED
    assert "unavailable" in audio.message.lower()
    assert "expected source audio" not in audio.message.lower()


def test_silent_source_rejects_an_unexpected_output_audio_stream() -> None:
    """Publish Ready must not introduce audio that was absent from the source."""
    source = _metadata(has_audio=False, audio_codec=None)
    output = _metadata(has_audio=True, audio_codec="aac")

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=COMPATIBLE_MP4,
        before=_report(source, input_hash="a" * 64),
        after=_report(output, input_hash="b" * 64),
    )

    statuses = {check.check_id: check.status for check in report.checks}
    assert statuses["audio_stream"] is VerificationStatus.FAILED
    assert report.status is VerificationStatus.FAILED


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "detector_id",
    _DETECTOR_IDS,
)
def test_new_severe_detector_finding_needs_review(detector_id: str) -> None:
    source = _metadata()
    output = _metadata(width=1080, height=1920)
    regression = _finding(
        input_hash="b" * 64,
        detector_id=detector_id,
        start_seconds=1.0,
        end_seconds=2.5,
    )

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64),
        after=_report(output, input_hash="b" * 64, findings=(regression,)),
    )

    check = next(
        item for item in report.checks if item.check_id == f"{detector_id}_regression"
    )
    assert check.status is VerificationStatus.NEEDS_REVIEW
    assert check.measured == {
        "before_high_critical_count": 0,
        "before_high_critical_duration_seconds": 0.0,
        "after_high_critical_count": 1,
        "after_high_critical_duration_seconds": 1.5,
        "before_execution": "ok",
        "after_execution": "ok",
    }
    assert report.status is VerificationStatus.NEEDS_REVIEW
    assert report.manual_review_reasons == (check.message,)


def test_longer_preexisting_severe_interval_needs_review() -> None:
    source = _metadata()
    output = _metadata(width=1080, height=1920)
    before_finding = _finding(
        input_hash="a" * 64,
        detector_id="near_black",
        start_seconds=1.0,
        end_seconds=2.0,
    )
    after_finding = _finding(
        input_hash="b" * 64,
        detector_id="near_black",
        start_seconds=1.0,
        end_seconds=3.0,
    )

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64, findings=(before_finding,)),
        after=_report(output, input_hash="b" * 64, findings=(after_finding,)),
    )

    check = next(
        item for item in report.checks if item.check_id == "near_black_regression"
    )
    assert check.status is VerificationStatus.NEEDS_REVIEW
    assert check.measured["before_high_critical_count"] == 1
    assert check.measured["after_high_critical_count"] == 1
    assert check.measured["before_high_critical_duration_seconds"] == 1.0
    assert check.measured["after_high_critical_duration_seconds"] == 2.0


def test_unchanged_preexisting_severe_finding_passes() -> None:
    source = _metadata()
    output = _metadata(width=1080, height=1920)
    before_finding = _finding(
        input_hash="a" * 64,
        detector_id="possible_freeze",
        start_seconds=3.0,
        end_seconds=5.0,
    )
    after_finding = _finding(
        input_hash="b" * 64,
        detector_id="possible_freeze",
        start_seconds=3.0,
        end_seconds=5.0,
    )

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64, findings=(before_finding,)),
        after=_report(output, input_hash="b" * 64, findings=(after_finding,)),
    )

    check = next(
        item for item in report.checks if item.check_id == "possible_freeze_regression"
    )
    assert check.status is VerificationStatus.PASSED
    assert report.status is VerificationStatus.PASSED


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "status",
    [DetectorStatus.DETECTOR_ERROR, DetectorStatus.SKIPPED],
)
def test_incomplete_detector_execution_needs_review(
    status: DetectorStatus,
) -> None:
    source = _metadata()
    output = _metadata(width=1080, height=1920)

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64),
        after=_report(
            output,
            input_hash="b" * 64,
            statuses={"near_black": status},
        ),
    )

    check = next(
        item for item in report.checks if item.check_id == "near_black_regression"
    )
    assert check.status is VerificationStatus.NEEDS_REVIEW
    assert check.measured["after_execution"] == status.value
    assert report.status is VerificationStatus.NEEDS_REVIEW


def test_missing_detector_execution_needs_review() -> None:
    source = _metadata()
    output = _metadata(width=1080, height=1920)

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=SOCIAL_VERTICAL,
        before=_report(source, input_hash="a" * 64),
        after=_report(
            output,
            input_hash="b" * 64,
            omitted_detectors=("possible_freeze",),
        ),
    )

    check = next(
        item for item in report.checks if item.check_id == "possible_freeze_regression"
    )
    assert check.status is VerificationStatus.NEEDS_REVIEW
    assert check.measured["after_execution"] == "missing"


def test_compatible_profile_preserves_source_dimensions_and_silent_audio() -> None:
    source = _metadata(width=720, height=1280, has_audio=False, audio_codec=None)
    output = _metadata(
        width=720,
        height=1280,
        has_audio=False,
        audio_codec=None,
    )

    report = PublishVerifier().verify(
        source_metadata=source,
        output_metadata=output,
        profile=COMPATIBLE_MP4,
        before=_report(source, input_hash="a" * 64),
        after=_report(output, input_hash="b" * 64),
    )

    statuses = {check.check_id: check.status for check in report.checks}
    assert statuses["dimensions"] is VerificationStatus.PASSED
    assert statuses["audio_stream"] is VerificationStatus.PASSED
    assert statuses["audio_codec"] is VerificationStatus.PASSED
    assert report.status is VerificationStatus.PASSED
