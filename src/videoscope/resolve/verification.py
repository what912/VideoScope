"""Profile-specific verification for freshly probed Publish Ready outputs."""

from __future__ import annotations

from pydantic import JsonValue

from videoscope.domain import (
    AnalysisReport,
    DetectorStatus,
    Severity,
    VideoMetadata,
)
from videoscope.resolve.models import (
    VerificationCheck,
    VerificationReport,
    VerificationStatus,
)
from videoscope.resolve.profiles import PublishProfile

_MIN_DURATION_TOLERANCE_SECONDS = 0.5
_DURATION_TOLERANCE_FRAMES = 2.0
_MIN_SOURCE_FPS_FOR_TOLERANCE = 1.0
_REGRESSION_DETECTOR_IDS = ("near_black", "possible_freeze")


class PublishVerifier:
    """Verify one freshly probed output against its selected profile."""

    def verify(
        self,
        *,
        source_metadata: VideoMetadata,
        output_metadata: VideoMetadata | None,
        profile: PublishProfile,
        before: AnalysisReport,
        after: AnalysisReport | None,
    ) -> VerificationReport:
        """Return technical and detector-local verification checks."""
        checks = (
            _decodable_check(output_metadata),
            _duration_check(source_metadata, output_metadata),
            _dimensions_check(source_metadata, output_metadata, profile),
            _container_check(output_metadata, profile),
            _video_codec_check(output_metadata, profile),
            _pixel_format_check(output_metadata, profile),
            _frame_rate_check(output_metadata, profile),
            _audio_stream_check(source_metadata, output_metadata),
            _audio_codec_check(source_metadata, output_metadata, profile),
            *(
                _detector_regression_check(before, after, detector_id)
                for detector_id in _REGRESSION_DETECTOR_IDS
            ),
        )
        status = _aggregate_status(checks)
        return VerificationReport(
            profile_id=profile.id,
            profile_version=profile.version,
            status=status,
            checks=checks,
            manual_review_reasons=tuple(
                check.message
                for check in checks
                if check.status is VerificationStatus.NEEDS_REVIEW
            ),
        )


def severe_summary(report: AnalysisReport, detector_id: str) -> tuple[int, float]:
    """Return detector-local high/critical event count and total duration."""
    matches = [
        item
        for item in report.findings
        if item.detector_id == detector_id
        and item.severity in {Severity.HIGH, Severity.CRITICAL}
    ]
    return len(matches), sum(
        item.time_range.end_seconds - item.time_range.start_seconds for item in matches
    )


def _decodable_check(output: VideoMetadata | None) -> VerificationCheck:
    available = output is not None
    return _technical_check(
        check_id="decodable",
        passed=available,
        passed_message="The output was independently probed as decodable media.",
        failed_message=(
            "The output could not be independently probed as decodable media."
        ),
        measured={"output_metadata_available": available},
    )


def _duration_check(
    source: VideoMetadata,
    output: VideoMetadata | None,
) -> VerificationCheck:
    tolerance = max(
        _MIN_DURATION_TOLERANCE_SECONDS,
        _DURATION_TOLERANCE_FRAMES
        / max(source.average_frame_rate, _MIN_SOURCE_FPS_FOR_TOLERANCE),
    )
    output_duration = output.duration_seconds if output is not None else None
    drift = (
        abs(output_duration - source.duration_seconds)
        if output_duration is not None
        else None
    )
    passed = drift is not None and drift <= tolerance
    return _technical_check(
        check_id="duration",
        passed=passed,
        passed_message="Output duration is within the source-relative tolerance.",
        failed_message="Output duration is unavailable or exceeds the allowed drift.",
        measured={
            "source_seconds": source.duration_seconds,
            "output_seconds": output_duration,
            "drift_seconds": drift,
            "tolerance_seconds": tolerance,
        },
    )


def _dimensions_check(
    source: VideoMetadata,
    output: VideoMetadata | None,
    profile: PublishProfile,
) -> VerificationCheck:
    expected_width = profile.width if profile.width is not None else source.width
    expected_height = profile.height if profile.height is not None else source.height
    output_width = output.width if output is not None else None
    output_height = output.height if output is not None else None
    passed = output_width == expected_width and output_height == expected_height
    return _technical_check(
        check_id="dimensions",
        passed=passed,
        passed_message="Output dimensions match the selected profile canvas.",
        failed_message="Output dimensions do not match the selected profile canvas.",
        measured={
            "expected_width": expected_width,
            "expected_height": expected_height,
            "output_width": output_width,
            "output_height": output_height,
        },
    )


def _container_check(
    output: VideoMetadata | None,
    profile: PublishProfile,
) -> VerificationCheck:
    output_container = output.container_format if output is not None else None
    container_names = (
        {
            name.strip().casefold()
            for name in output_container.split(",")
            if name.strip()
        }
        if output_container is not None
        else set()
    )
    passed = profile.container.casefold() in container_names
    return _technical_check(
        check_id="container",
        passed=passed,
        passed_message="Output container is compatible with the selected profile.",
        failed_message="Output container is unavailable or incompatible.",
        measured={
            "required_container": profile.container,
            "output_container": output_container,
        },
    )


def _video_codec_check(
    output: VideoMetadata | None,
    profile: PublishProfile,
) -> VerificationCheck:
    output_codec = output.codec if output is not None else None
    passed = (
        output_codec is not None
        and output_codec.casefold() == profile.video_codec.casefold()
    )
    return _technical_check(
        check_id="video_codec",
        passed=passed,
        passed_message="Output video codec matches the selected profile.",
        failed_message="Output video codec is unavailable or incompatible.",
        measured={
            "required_video_codec": profile.video_codec,
            "output_video_codec": output_codec,
        },
    )


def _pixel_format_check(
    output: VideoMetadata | None,
    profile: PublishProfile,
) -> VerificationCheck:
    output_pixel_format = _probe_string(output, "pixel_format")
    passed = (
        output_pixel_format is not None
        and output_pixel_format.casefold() == profile.pixel_format.casefold()
    )
    return _technical_check(
        check_id="pixel_format",
        passed=passed,
        passed_message="Output pixel format matches the selected profile.",
        failed_message="Output pixel format is unavailable or incompatible.",
        measured={
            "required_pixel_format": profile.pixel_format,
            "output_pixel_format": output_pixel_format,
        },
    )


def _frame_rate_check(
    output: VideoMetadata | None,
    profile: PublishProfile,
) -> VerificationCheck:
    output_fps = output.average_frame_rate if output is not None else None
    passed = output_fps is not None and 0 < output_fps <= profile.maximum_fps
    return _technical_check(
        check_id="frame_rate",
        passed=passed,
        passed_message="Output frame rate is within the selected profile limit.",
        failed_message=(
            "Output frame rate is unavailable, non-positive, or exceeds the "
            "profile limit."
        ),
        measured={
            "maximum_fps": profile.maximum_fps,
            "output_fps": output_fps,
        },
    )


def _audio_stream_check(
    source: VideoMetadata,
    output: VideoMetadata | None,
) -> VerificationCheck:
    output_has_audio = output.has_audio if output is not None else None
    passed = output is not None and source.has_audio == output.has_audio
    if output is None:
        failed_message = "Output audio-stream information is unavailable."
    elif source.has_audio:
        failed_message = "Expected source audio is missing from the output."
    else:
        failed_message = "The output unexpectedly contains an audio stream."
    return _technical_check(
        check_id="audio_stream",
        passed=passed,
        passed_message="Output preserves the source audio-stream requirement.",
        failed_message=failed_message,
        measured={
            "source_has_audio": source.has_audio,
            "output_has_audio": output_has_audio,
        },
    )


def _audio_codec_check(
    source: VideoMetadata,
    output: VideoMetadata | None,
    profile: PublishProfile,
) -> VerificationCheck:
    output_audio_codec = _probe_string(output, "audio_codec")
    if output is None:
        passed = False
    elif not output.has_audio:
        passed = not source.has_audio
    else:
        passed = (
            output_audio_codec is not None
            and output_audio_codec.casefold() == profile.audio_codec.casefold()
        )
    return _technical_check(
        check_id="audio_codec",
        passed=passed,
        passed_message="Output audio codec satisfies the selected profile.",
        failed_message="Output audio codec is unavailable or incompatible.",
        measured={
            "required_audio_codec": profile.audio_codec,
            "output_audio_codec": output_audio_codec,
            "audio_required": source.has_audio,
        },
    )


def _detector_regression_check(
    before: AnalysisReport,
    after: AnalysisReport | None,
    detector_id: str,
) -> VerificationCheck:
    before_count, before_duration = severe_summary(before, detector_id)
    after_count, after_duration = (
        severe_summary(after, detector_id) if after is not None else (0, 0.0)
    )
    before_execution = _detector_execution_state(before, detector_id)
    after_execution = _detector_execution_state(after, detector_id)
    measured: dict[str, JsonValue] = {
        "before_high_critical_count": before_count,
        "before_high_critical_duration_seconds": before_duration,
        "after_high_critical_count": after_count,
        "after_high_critical_duration_seconds": after_duration,
        "before_execution": before_execution,
        "after_execution": after_execution,
    }
    check_id = f"{detector_id}_regression"
    if before_execution != DetectorStatus.OK.value or after_execution != (
        DetectorStatus.OK.value
    ):
        return VerificationCheck(
            check_id=check_id,
            status=VerificationStatus.NEEDS_REVIEW,
            message=(
                f"The {detector_id} comparison needs review because detector "
                "execution was incomplete."
            ),
            measured=measured,
        )
    if after_count > before_count or after_duration > before_duration:
        return VerificationCheck(
            check_id=check_id,
            status=VerificationStatus.NEEDS_REVIEW,
            message=(
                f"The {detector_id} high/critical event summary increased "
                "after processing."
            ),
            measured=measured,
        )
    return VerificationCheck(
        check_id=check_id,
        status=VerificationStatus.PASSED,
        message=(f"The {detector_id} high/critical event summary did not increase."),
        measured=measured,
    )


def _technical_check(
    *,
    check_id: str,
    passed: bool,
    passed_message: str,
    failed_message: str,
    measured: dict[str, JsonValue],
) -> VerificationCheck:
    return VerificationCheck(
        check_id=check_id,
        status=(VerificationStatus.PASSED if passed else VerificationStatus.FAILED),
        message=passed_message if passed else failed_message,
        measured=measured,
    )


def _probe_string(output: VideoMetadata | None, key: str) -> str | None:
    if output is None:
        return None
    value = output.raw_probe.get(key)
    return value if isinstance(value, str) else None


def _detector_execution_state(
    report: AnalysisReport | None,
    detector_id: str,
) -> str:
    if report is None:
        return "missing"
    statuses = [
        execution.status
        for execution in report.detector_executions
        if execution.detector_id == detector_id
    ]
    if not statuses:
        return "missing"
    if DetectorStatus.DETECTOR_ERROR in statuses:
        return DetectorStatus.DETECTOR_ERROR.value
    if DetectorStatus.SKIPPED in statuses:
        return DetectorStatus.SKIPPED.value
    return DetectorStatus.OK.value


def _aggregate_status(
    checks: tuple[VerificationCheck, ...],
) -> VerificationStatus:
    statuses = {check.status for check in checks}
    if VerificationStatus.FAILED in statuses:
        return VerificationStatus.FAILED
    if VerificationStatus.NEEDS_REVIEW in statuses:
        return VerificationStatus.NEEDS_REVIEW
    return VerificationStatus.PASSED


__all__ = ["PublishVerifier", "severe_summary"]
