"""Independent fail-closed verification for useful-content pending artifacts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath

from pydantic import JsonValue

from videoscope.content.models import (
    CONTENT_REQUIRED_VERIFICATION_CHECK_IDS,
    ContentGoal,
    ContentOutcome,
    ContentPlan,
    ContentSourceMapping,
    ContentTimeRange,
    ContentUserRangeKind,
    ContentVerificationCheck,
    ContentVerificationReport,
    ContentVerificationStatus,
)
from videoscope.content.timeline import intersect_ranges, subtract_ranges, union_ranges

_CHECK_VERSION = "1"


@dataclass(frozen=True, slots=True)
class ContentVerificationEvidence:
    """Measurements produced independently of the native executor success flags."""

    decodable: bool | None
    output_duration_seconds: float | None
    has_video: bool | None
    has_audio: bool | None
    expected_has_audio: bool
    black_interval_regression: bool | None
    repeated_frame_regression: bool | None
    audio_continuity_ok: bool | None
    av_sync_residual_seconds: float | None
    chapter_timing_ok: bool | None
    subtitle_timing_ok: bool | None
    public_relative_paths: tuple[str, ...]
    source_hash_after: str | None
    source_modified: bool | None
    missing_source_ranges: tuple[ContentTimeRange, ...] = ()


def verify_content_result(
    *,
    plan: ContentPlan,
    mappings: tuple[ContentSourceMapping, ...],
    evidence: ContentVerificationEvidence,
) -> ContentVerificationReport:
    """Evaluate every mandatory policy check in canonical order."""
    checks = (
        _boolean_check(
            "decodable",
            evidence.decodable,
            passed="The pending media decoded successfully.",
            failed="The pending media did not decode successfully.",
        ),
        _duration_check(plan, mappings, evidence),
        _streams_check(evidence),
        _source_map_check(plan, mappings, evidence.missing_source_ranges),
        _locked_ranges_check(plan, mappings),
        _source_order_check(plan, mappings),
        _join_regression_check(evidence),
        _boolean_check(
            "audio_continuity",
            evidence.audio_continuity_ok if evidence.expected_has_audio else True,
            passed="Audio continuity matched the retained timeline.",
            failed="Audio continuity did not match the retained timeline.",
        ),
        _av_sync_check(plan, evidence),
        _chapters_subtitles_check(plan, evidence),
        _public_artifacts_check(plan, evidence.public_relative_paths),
        _source_read_only_check(plan, evidence),
    )
    return ContentVerificationReport(
        plan_digest=plan.plan_digest,
        checks=checks,
        missing_source_ranges=evidence.missing_source_ranges,
        required_check_ids=CONTENT_REQUIRED_VERIFICATION_CHECK_IDS,
        outcome=ContentOutcome.COMPLETED,
    )


def _duration_check(
    plan: ContentPlan,
    mappings: tuple[ContentSourceMapping, ...],
    evidence: ContentVerificationEvidence,
) -> ContentVerificationCheck:
    expected = mappings[-1].output_range.end_seconds if mappings else 0.0
    actual = evidence.output_duration_seconds
    if actual is None or not math.isfinite(actual):
        return _review(
            "duration",
            "Output duration could not be measured independently.",
        )
    delta = abs(actual - expected)
    passed = delta <= plan.effective_config.verification_duration_tolerance_seconds
    return _check(
        "duration",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        (
            "Measured duration matches the source map."
            if passed
            else "Measured duration does not match the source map."
        ),
        measured={
            "actual_seconds": actual,
            "expected_seconds": expected,
            "absolute_error_seconds": delta,
            "tolerance_seconds": (
                plan.effective_config.verification_duration_tolerance_seconds
            ),
        },
    )


def _streams_check(evidence: ContentVerificationEvidence) -> ContentVerificationCheck:
    if evidence.has_video is None or evidence.has_audio is None:
        return _review(
            "streams", "Output streams could not be inspected independently."
        )
    passed = evidence.has_video and evidence.has_audio is evidence.expected_has_audio
    return _check(
        "streams",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "Output stream inventory matches the confirmed source requirements."
        if passed
        else "Output stream inventory does not match the confirmed requirements.",
        measured={
            "has_audio": evidence.has_audio,
            "has_video": evidence.has_video,
        },
    )


def _source_map_check(
    plan: ContentPlan,
    mappings: tuple[ContentSourceMapping, ...],
    missing: tuple[ContentTimeRange, ...],
) -> ContentVerificationCheck:
    expected = tuple(
        item.source_range
        for item in sorted(
            (
                item
                for item in plan.storyboard.items
                if item.output_order_index is not None
            ),
            key=lambda item: (
                item.output_order_index if item.output_order_index is not None else -1
            ),
        )
    )
    actual = tuple(item.source_range for item in mappings)
    expected_after_missing = tuple(
        piece for item in expected for piece in subtract_ranges(item, missing)
    )
    output_contiguous = (
        bool(mappings)
        and math.isclose(
            mappings[0].output_range.start_seconds,
            0.0,
            abs_tol=1e-9,
        )
        and all(
            math.isclose(
                left.output_range.end_seconds,
                right.output_range.start_seconds,
                abs_tol=1e-6,
            )
            for left, right in zip(mappings, mappings[1:])
        )
    )
    missing_allowed = not missing or plan.goal is ContentGoal.SELECTED_CLIPS
    passed = actual == expected_after_missing and output_contiguous and missing_allowed
    return _check(
        "source_map",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "Every output interval maps to the exact retained source timeline."
        if passed
        else "The source map does not exactly represent the confirmed timeline.",
        measured={
            "mapping_count": len(mappings),
            "missing_range_count": len(missing),
            "output_contiguous": output_contiguous,
        },
    )


def _locked_ranges_check(
    plan: ContentPlan,
    mappings: tuple[ContentSourceMapping, ...],
) -> ContentVerificationCheck:
    source_ranges = tuple(item.source_range for item in mappings)
    locked_keep = tuple(
        item.source_range
        for item in plan.locked_ranges
        if item.kind is ContentUserRangeKind.LOCKED_KEEP
    )
    locked_exclude = tuple(
        item.source_range
        for item in plan.locked_ranges
        if item.kind is ContentUserRangeKind.LOCKED_EXCLUDE
    )
    keep_present = all(_range_is_covered(item, source_ranges) for item in locked_keep)
    excludes_absent = all(
        intersect_ranges(item, mapped) is None
        for item in locked_exclude
        for mapped in source_ranges
    )
    passed = keep_present and excludes_absent
    return _check(
        "locked_ranges",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "Locked keep and exclude ranges match the confirmed plan."
        if passed
        else "A locked keep or exclude range was violated.",
        measured={
            "locked_exclude_count": len(locked_exclude),
            "locked_keep_count": len(locked_keep),
        },
    )


def _source_order_check(
    plan: ContentPlan,
    mappings: tuple[ContentSourceMapping, ...],
) -> ContentVerificationCheck:
    indices = tuple(item.source_order_index for item in mappings)
    reordered = indices != tuple(sorted(indices))
    passed = reordered is plan.storyboard.reorder_acknowledged
    return _check(
        "source_order",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "Source order matches the explicit storyboard acknowledgement."
        if passed
        else "Source order changed without a matching acknowledgement.",
        measured={"reordered": reordered},
    )


def _join_regression_check(
    evidence: ContentVerificationEvidence,
) -> ContentVerificationCheck:
    values = (
        evidence.black_interval_regression,
        evidence.repeated_frame_regression,
    )
    if any(value is None for value in values):
        return _review(
            "join_regression",
            "Join regression measurements were inconclusive.",
        )
    passed = not any(values)
    return _check(
        "join_regression",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "No new long black or repeated-frame interval was measured at joins."
        if passed
        else "A new black or repeated-frame interval was measured at a join.",
        measured={
            "black_interval_regression": bool(evidence.black_interval_regression),
            "repeated_frame_regression": bool(evidence.repeated_frame_regression),
        },
    )


def _av_sync_check(
    plan: ContentPlan,
    evidence: ContentVerificationEvidence,
) -> ContentVerificationCheck:
    if not evidence.expected_has_audio:
        return _check(
            "av_sync",
            ContentVerificationStatus.PASSED,
            "A/V residual is not applicable because the source has no audio.",
        )
    residual = evidence.av_sync_residual_seconds
    if residual is None or not math.isfinite(residual):
        return _review("av_sync", "A/V residual could not be measured independently.")
    passed = (
        abs(residual) <= plan.effective_config.verification_av_sync_tolerance_seconds
    )
    return _check(
        "av_sync",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "Measured A/V residual is within the configured tolerance."
        if passed
        else "Measured A/V residual exceeds the configured tolerance.",
        measured={
            "residual_seconds": residual,
            "tolerance_seconds": (
                plan.effective_config.verification_av_sync_tolerance_seconds
            ),
        },
    )


def _chapters_subtitles_check(
    plan: ContentPlan,
    evidence: ContentVerificationEvidence,
) -> ContentVerificationCheck:
    values: list[bool | None] = []
    if plan.storyboard.chapters:
        values.append(evidence.chapter_timing_ok)
    if plan.effective_config.export_subtitles:
        values.append(evidence.subtitle_timing_ok)
    if not values:
        return _check(
            "chapters_subtitles",
            ContentVerificationStatus.PASSED,
            "No chapter or subtitle timing artifact was requested.",
        )
    if any(value is None for value in values):
        return _review(
            "chapters_subtitles",
            "Chapter or subtitle timing could not be verified independently.",
        )
    passed = all(values)
    return _check(
        "chapters_subtitles",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "Chapter and subtitle timing is valid."
        if passed
        else "Chapter or subtitle timing is invalid.",
    )


def _public_artifacts_check(
    plan: ContentPlan,
    paths: tuple[str, ...],
) -> ContentVerificationCheck:
    normalized = len(paths) == len(set(paths)) and all(
        _safe_public_path(item) for item in paths
    )
    allowlist = set(plan.public_artifacts)
    passed = normalized and set(paths) == allowlist
    return _check(
        "public_artifacts",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "Every reviewed public artifact is present in the exact allowlist."
        if passed
        else "A public artifact is missing, unsafe, or outside the allowlist.",
        measured={"artifact_count": len(paths)},
    )


def _source_read_only_check(
    plan: ContentPlan,
    evidence: ContentVerificationEvidence,
) -> ContentVerificationCheck:
    if evidence.source_hash_after is None or evidence.source_modified is None:
        return _review(
            "source_read_only",
            "Source byte identity could not be verified after execution.",
        )
    passed = (
        evidence.source_hash_after == plan.input_hash and not evidence.source_modified
    )
    return _check(
        "source_read_only",
        ContentVerificationStatus.PASSED
        if passed
        else ContentVerificationStatus.FAILED,
        "Source bytes remained unchanged."
        if passed
        else "Source byte identity changed during processing.",
    )


def _boolean_check(
    check_id: str,
    value: bool | None,
    *,
    passed: str,
    failed: str,
) -> ContentVerificationCheck:
    if value is None:
        return _review(check_id, f"{check_id} could not be measured independently.")
    return _check(
        check_id,
        ContentVerificationStatus.PASSED if value else ContentVerificationStatus.FAILED,
        passed if value else failed,
    )


def _review(check_id: str, message: str) -> ContentVerificationCheck:
    return _check(check_id, ContentVerificationStatus.NEEDS_REVIEW, message)


def _check(
    check_id: str,
    status: ContentVerificationStatus,
    message: str,
    *,
    measured: dict[str, JsonValue] | None = None,
) -> ContentVerificationCheck:
    return ContentVerificationCheck(
        check_id=check_id,
        version=_CHECK_VERSION,
        required=True,
        status=status,
        message=message,
        measured=measured or {},
    )


def _range_is_covered(
    expected: ContentTimeRange,
    actual: tuple[ContentTimeRange, ...],
) -> bool:
    overlap = tuple(
        item for mapped in actual if (item := intersect_ranges(expected, mapped))
    )
    combined = union_ranges(overlap)
    return len(combined) == 1 and combined[0] == expected


def _safe_public_path(value: str) -> bool:
    path = PurePosixPath(value)
    return (
        value.startswith("content-output/")
        and not path.is_absolute()
        and not PureWindowsPath(value).drive
        and ".." not in path.parts
        and "\\" not in value
        and value == path.as_posix()
    )


__all__ = ["ContentVerificationEvidence", "verify_content_result"]
