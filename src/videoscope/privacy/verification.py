"""Independent, conservative verification of Safe Sharing outputs."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from pydantic import JsonValue

from videoscope.privacy.artifacts import (
    PrivacyArtifactLayout,
    validate_public_manifest,
)
from videoscope.privacy.errors import PrivacyArtifactError, PrivacyConfirmationError
from videoscope.privacy.models import (
    PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS,
    PrivacyAction,
    PrivacyActionKind,
    PrivacyDecision,
    PrivacyJobOutcome,
    PrivacyPlan,
    PrivacyRisk,
    PrivacyRiskType,
    PrivacyVerificationCheck,
    PrivacyVerificationReport,
    VerificationStatus,
)
from videoscope.privacy.profiles import (
    ManualReviewCategory,
    ShareAudienceProfile,
    get_share_audience_profile,
)
from videoscope.video.probe import probe_video_with_private_summary

_DURATION_TOLERANCE_SECONDS = 0.5
_DURATION_TOLERANCE_FRAMES = 2.0
_MIN_FPS = 1.0
_REGRESSION_IDS = (
    ("near_black", "black_regression"),
    ("possible_freeze", "freeze_regression"),
)


@dataclass(frozen=True, slots=True)
class MediaVerificationSnapshot:
    """Path-free media facts used by independent verification checks."""

    duration_seconds: float
    average_frame_rate: float
    video_stream_count: int
    audio_stream_count: int
    metadata_categories: tuple[str, ...] = ()
    has_embedded_metadata: bool = False

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.duration_seconds)
            or self.duration_seconds < 0
            or not math.isfinite(self.average_frame_rate)
            or self.average_frame_rate < 0
        ):
            raise ValueError(
                "verification media timing must be finite and non-negative"
            )
        if self.video_stream_count < 0 or self.audio_stream_count < 0:
            raise ValueError("verification stream counts must be non-negative")


@dataclass(frozen=True, slots=True)
class TemporalCoverageResult:
    """Coverage observations for every requested visual timestamp."""

    checked_timestamps: tuple[float, ...] = ()
    uncovered_timestamps: tuple[float, ...] = ()
    available: bool = True


@dataclass(frozen=True, slots=True)
class RescanVerificationResult:
    """Sensitive-content rescan outcome without retaining decoded content."""

    available: bool
    checked_timestamps: tuple[float, ...] = ()
    detected_timestamps: tuple[float, ...] = ()


@dataclass(frozen=True, slots=True)
class AudioVerificationResult:
    """Mute-energy observations without retaining source or output samples."""

    available: bool
    checked_timestamps: tuple[float, ...] = ()
    excessive_energy_timestamps: tuple[float, ...] = ()
    outside_signal_retained: bool | None = None


@dataclass(frozen=True, slots=True)
class RegressionVerificationResult:
    """Detector-local source/output event summary."""

    available: bool
    before_event_count: int = 0
    before_duration_seconds: float = 0.0
    after_event_count: int = 0
    after_duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ScannerVerificationIssue:
    """Public-safe record that an optional proposal scanner was unavailable."""

    scanner_id: str
    category: ManualReviewCategory

    def __post_init__(self) -> None:
        if (
            not self.scanner_id
            or self.scanner_id[0] not in "abcdefghijklmnopqrstuvwxyz"
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_.-"
                for character in self.scanner_id
            )
        ):
            raise ValueError("scanner issue ID must be a normalized non-empty value")


@dataclass(frozen=True, slots=True)
class PrivacyVerificationContext:
    """Private runtime inputs that never enter the public verification report."""

    public_root: Path
    expected_candidate_sha256: str
    expected_artifacts: tuple[str, ...]
    scanner_issues: tuple[ScannerVerificationIssue, ...] = ()
    sample_fps: float = 2.0
    mute_rms_threshold: float = 0.01

    def __post_init__(self) -> None:
        if not _is_sha256(self.expected_candidate_sha256):
            raise ValueError("expected candidate hash must be a lowercase SHA-256")
        required_stage = {"share-safe.mp4", "changes.json"}
        if (
            len(self.expected_artifacts) != len(set(self.expected_artifacts))
            or not required_stage.issubset(self.expected_artifacts)
            or any(
                not artifact
                or "\\" in artifact
                or Path(artifact).is_absolute()
                or ".." in Path(artifact).parts
                for artifact in self.expected_artifacts
            )
        ):
            raise ValueError(
                "expected artifacts must include the current Safe Sharing stage"
            )
        if not math.isfinite(self.sample_fps) or self.sample_fps <= 0:
            raise ValueError("verification sample_fps must be finite and positive")
        if not math.isfinite(self.mute_rms_threshold) or self.mute_rms_threshold < 0:
            raise ValueError("mute RMS threshold must be finite and non-negative")


class MediaProbe(Protocol):
    def __call__(self, path: Path) -> MediaVerificationSnapshot: ...


class CoverageChecker(Protocol):
    def verify(
        self,
        source: Path,
        candidate: Path,
        action: PrivacyAction,
        timestamps: tuple[float, ...],
    ) -> TemporalCoverageResult: ...


class PrivacyRescanner(Protocol):
    def rescan(
        self,
        candidate: Path,
        risks: tuple[PrivacyRisk, ...],
        timestamps: tuple[float, ...],
    ) -> RescanVerificationResult: ...


class AudioAnalyzer(Protocol):
    def verify(
        self,
        source: Path,
        candidate: Path,
        actions: tuple[PrivacyAction, ...],
        timestamps: tuple[float, ...],
    ) -> AudioVerificationResult: ...


class RegressionAnalyzer(Protocol):
    def compare(
        self,
        source: Path,
        candidate: Path,
        detector_id: str,
    ) -> RegressionVerificationResult: ...


class _UnavailableCoverageChecker:
    def verify(
        self,
        source: Path,
        candidate: Path,
        action: PrivacyAction,
        timestamps: tuple[float, ...],
    ) -> TemporalCoverageResult:
        del source, candidate, action, timestamps
        return TemporalCoverageResult(available=False)


class _UnavailableRescanner:
    def rescan(
        self,
        candidate: Path,
        risks: tuple[PrivacyRisk, ...],
        timestamps: tuple[float, ...],
    ) -> RescanVerificationResult:
        del candidate, risks, timestamps
        return RescanVerificationResult(available=False)


class _UnavailableAudioAnalyzer:
    def verify(
        self,
        source: Path,
        candidate: Path,
        actions: tuple[PrivacyAction, ...],
        timestamps: tuple[float, ...],
    ) -> AudioVerificationResult:
        del source, candidate, actions, timestamps
        return AudioVerificationResult(available=False)


class _UnavailableRegressionAnalyzer:
    def compare(
        self,
        source: Path,
        candidate: Path,
        detector_id: str,
    ) -> RegressionVerificationResult:
        del source, candidate, detector_id
        return RegressionVerificationResult(available=False)


_MP4_MAJOR_BRANDS = frozenset({"isom"})
_MP4_COMPATIBLE_BRANDS = frozenset(
    {
        "3gp4",
        "3gp5",
        "3gp6",
        "M4A ",
        "M4V ",
        "avc1",
        "dash",
        "hev1",
        "hvc1",
        "iso2",
        "iso3",
        "iso4",
        "iso5",
        "iso6",
        "isom",
        "mp41",
        "mp42",
        "qt  ",
    }
)
_MP4_MINOR_VERSIONS = frozenset({"512"})
_FORMAT_ENCODER_VALUE = re.compile(r"Lavf\d+(?:\.\d+){1,3}")
_STREAM_ENCODER_VALUE = re.compile(r"Lavc(?:\d+(?:\.\d+){1,3})? (?:aac|libx264|mpeg4)")


def _is_expected_compatible_brands(value: str) -> bool:
    if not value or len(value) % 4:
        return False
    return all(
        value[index : index + 4] in _MP4_COMPATIBLE_BRANDS
        for index in range(0, len(value), 4)
    )


def _is_expected_global_tag(key: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    if key == "major_brand":
        return value in _MP4_MAJOR_BRANDS
    if key == "minor_version":
        return value in _MP4_MINOR_VERSIONS
    if key == "compatible_brands":
        return _is_expected_compatible_brands(value)
    if key == "encoder":
        return bool(_FORMAT_ENCODER_VALUE.fullmatch(value))
    return False


def _is_expected_stream_tag(key: str, value: object) -> bool:
    if not isinstance(value, str):
        return False
    if key == "language":
        return value == "und"
    if key == "handler_name":
        return value in {"VideoHandler", "SoundHandler"}
    if key == "vendor_id":
        return value == "[0][0][0][0]"
    if key == "encoder":
        return bool(_STREAM_ENCODER_VALUE.fullmatch(value))
    return False


def _default_media_probe(path: Path) -> MediaVerificationSnapshot:
    metadata, private = probe_video_with_private_summary(path)
    embedded = bool(
        any(
            not _is_expected_global_tag(key, value)
            for key, value in private.global_tags.items()
        )
        or any(
            any(
                not _is_expected_stream_tag(key, value)
                for key, value in tag_set.tags.items()
            )
            for tag_set in private.stream_tags
        )
        or private.chapter_tags
        or private.attachment_tags
    )
    return MediaVerificationSnapshot(
        duration_seconds=metadata.duration_seconds,
        average_frame_rate=metadata.average_frame_rate,
        video_stream_count=1,
        audio_stream_count=1 if metadata.has_audio else 0,
        has_embedded_metadata=embedded,
    )


class PrivacyVerifier:
    """Run independent checks and allow only conservative aggregate outcomes."""

    def __init__(
        self,
        *,
        media_probe: MediaProbe = _default_media_probe,
        coverage_checker: CoverageChecker | None = None,
        qr_rescanner: PrivacyRescanner | None = None,
        text_rescanner: PrivacyRescanner | None = None,
        audio_analyzer: AudioAnalyzer | None = None,
        regression_analyzer: RegressionAnalyzer | None = None,
    ) -> None:
        self._media_probe = media_probe
        self._coverage_checker = coverage_checker or _UnavailableCoverageChecker()
        self._qr_rescanner = qr_rescanner or _UnavailableRescanner()
        self._text_rescanner = text_rescanner or _UnavailableRescanner()
        self._audio_analyzer = audio_analyzer or _UnavailableAudioAnalyzer()
        self._regression_analyzer = (
            regression_analyzer or _UnavailableRegressionAnalyzer()
        )

    def verify(
        self,
        source: Path,
        candidate: Path,
        plan: PrivacyPlan,
        private_context: PrivacyVerificationContext,
    ) -> PrivacyVerificationReport:
        """Verify one candidate without retaining private probe or rescan content."""
        source = Path(source)
        candidate = Path(candidate)
        if _safe_sha256(source) != plan.input_hash:
            raise PrivacyConfirmationError(
                "confirmed plan does not match source content"
            )
        public_root = _resolved_directory(private_context.public_root)
        try:
            resolved_candidate = candidate.resolve(strict=True)
            expected_candidate = (public_root / "share-safe.mp4").resolve(strict=True)
        except OSError as exc:
            raise PrivacyArtifactError(
                "Safe Sharing candidate share-safe.mp4 is unavailable"
            ) from exc
        if resolved_candidate != expected_candidate:
            raise PrivacyArtifactError(
                "Safe Sharing candidate must be public share-safe.mp4"
            )
        candidate_sha256 = _safe_sha256(resolved_candidate)
        if candidate_sha256 is None:
            raise PrivacyArtifactError("Safe Sharing candidate could not be read")

        source_snapshot = _safe_probe(self._media_probe, source)
        candidate_snapshot = _safe_probe(self._media_probe, resolved_candidate)
        profile = _safe_profile(plan.profile)
        checks: list[PrivacyVerificationCheck] = [
            _decodable_check(candidate_snapshot),
            _duration_check(source_snapshot, candidate_snapshot),
            _streams_check(source_snapshot, candidate_snapshot),
            _profile_check(plan, profile),
            _metadata_check(candidate_snapshot, profile),
            self._visual_coverage_check(
                source, resolved_candidate, plan, private_context
            ),
            self._rescan_check(
                "qr_redaction",
                resolved_candidate,
                plan,
                private_context,
                self._qr_rescanner,
                {PrivacyRiskType.QR_CODE, PrivacyRiskType.BARCODE},
            ),
            self._rescan_check(
                "text_redaction",
                resolved_candidate,
                plan,
                private_context,
                self._text_rescanner,
                {PrivacyRiskType.SUSPICIOUS_TEXT},
            ),
            self._audio_check(source, resolved_candidate, plan, private_context),
        ]
        checks.extend(
            self._regression_check(source, resolved_candidate, detector, check_id)
            for detector, check_id in _REGRESSION_IDS
        )
        checks.append(
            _public_artifact_check(
                public_root,
                plan,
                private_context,
                candidate_sha256,
            )
        )
        checks = list(
            _apply_scanner_issues(
                tuple(checks),
                private_context.scanner_issues,
                profile,
            )
        )
        provisional = PrivacyVerificationReport(
            plan_digest=plan.digest,
            status=_aggregate_outcome(tuple(checks)),
            checks=tuple(checks),
        )
        return provisional

    def _visual_coverage_check(
        self,
        source: Path,
        candidate: Path,
        plan: PrivacyPlan,
        context: PrivacyVerificationContext,
    ) -> PrivacyVerificationCheck:
        actions = tuple(
            action
            for action in plan.actions
            if action.kind
            in {PrivacyActionKind.VISUAL_REDACTION, PrivacyActionKind.CROP}
        )
        if not actions:
            return _passed("visual_coverage", "No visual redaction required.")
        missing = 0
        uncovered = 0
        checked = 0
        unavailable = False
        for action in actions:
            requested = _sample_timestamps(
                action.start_seconds,
                action.end_seconds,
                context.sample_fps,
            )
            try:
                result = self._coverage_checker.verify(
                    source, candidate, action, requested
                )
            except Exception:
                result = TemporalCoverageResult(available=False)
            if not result.available:
                unavailable = True
                continue
            observed = _timestamp_set(result.checked_timestamps)
            requested_set = _timestamp_set(requested)
            missing += len(requested_set - observed)
            uncovered += len(_timestamp_set(result.uncovered_timestamps))
            checked += len(observed & requested_set)
        measured: dict[str, JsonValue] = {
            "actions": len(actions),
            "checked_samples": checked,
            "missing_samples": missing,
            "uncovered_samples": uncovered,
        }
        if uncovered:
            return _check(
                "visual_coverage",
                VerificationStatus.FAILED,
                "At least one confirmed visual region was not covered.",
                measured,
            )
        if unavailable or missing:
            return _check(
                "visual_coverage",
                VerificationStatus.NEEDS_REVIEW,
                "Continuous visual coverage could not be verified at every sample.",
                measured,
            )
        return _check(
            "visual_coverage",
            VerificationStatus.PASSED,
            "Every sampled point in each visual interval was covered.",
            measured,
        )

    def _rescan_check(
        self,
        check_id: str,
        candidate: Path,
        plan: PrivacyPlan,
        context: PrivacyVerificationContext,
        rescanner: PrivacyRescanner,
        risk_types: set[PrivacyRiskType],
    ) -> PrivacyVerificationCheck:
        risks = tuple(
            risk
            for risk in plan.risks
            if risk.risk_type in risk_types and risk.decision is PrivacyDecision.REDACT
        )
        if not risks:
            return _passed(
                check_id, "No applicable redacted observation required rescan."
            )
        requested = _risk_timestamps(risks, context.sample_fps)
        try:
            result = rescanner.rescan(candidate, risks, requested)
        except Exception:
            result = RescanVerificationResult(available=False)
        measured: dict[str, JsonValue] = {
            "requested_samples": len(requested),
            "checked_samples": len(
                _timestamp_set(result.checked_timestamps) & _timestamp_set(requested)
            ),
            "detections": len(result.detected_timestamps),
        }
        if result.detected_timestamps:
            return _check(
                check_id,
                VerificationStatus.FAILED,
                "Sensitive visual content remained detectable in a redacted region.",
                measured,
            )
        if not result.available or not _timestamps_cover(
            result.checked_timestamps, requested
        ):
            return _check(
                check_id,
                VerificationStatus.NEEDS_REVIEW,
                "The redacted region could not be fully rescanned.",
                measured,
            )
        return _check(
            check_id,
            VerificationStatus.PASSED,
            "No selected sensitive content was recovered during local rescan.",
            measured,
        )

    def _audio_check(
        self,
        source: Path,
        candidate: Path,
        plan: PrivacyPlan,
        context: PrivacyVerificationContext,
    ) -> PrivacyVerificationCheck:
        actions = tuple(
            action
            for action in plan.actions
            if action.kind is PrivacyActionKind.AUDIO_MUTE
        )
        if not actions:
            return _passed(
                "audio_mute", "No reviewed audio mute interval required verification."
            )
        requested = tuple(
            sorted(
                {
                    timestamp
                    for action in actions
                    for timestamp in _sample_timestamps(
                        action.start_seconds,
                        action.end_seconds,
                        context.sample_fps,
                    )
                }
            )
        )
        try:
            result = self._audio_analyzer.verify(source, candidate, actions, requested)
        except Exception:
            result = AudioVerificationResult(available=False)
        measured: dict[str, JsonValue] = {
            "requested_samples": len(requested),
            "checked_samples": len(
                _timestamp_set(result.checked_timestamps) & _timestamp_set(requested)
            ),
            "samples_above_threshold": len(result.excessive_energy_timestamps),
            "outside_signal_retained": result.outside_signal_retained,
            "rms_threshold": context.mute_rms_threshold,
        }
        if (
            result.excessive_energy_timestamps
            or result.outside_signal_retained is False
        ):
            return _check(
                "audio_mute",
                VerificationStatus.FAILED,
                "Mute energy or retained outside audio did not match the "
                "reviewed plan.",
                measured,
            )
        if (
            not result.available
            or result.outside_signal_retained is None
            or not _timestamps_cover(result.checked_timestamps, requested)
        ):
            return _check(
                "audio_mute",
                VerificationStatus.NEEDS_REVIEW,
                "The reviewed audio mute interval could not be fully verified.",
                measured,
            )
        return _check(
            "audio_mute",
            VerificationStatus.PASSED,
            "Mute energy and retained outside audio matched the reviewed plan.",
            measured,
        )

    def _regression_check(
        self,
        source: Path,
        candidate: Path,
        detector_id: str,
        check_id: str,
    ) -> PrivacyVerificationCheck:
        try:
            result = self._regression_analyzer.compare(source, candidate, detector_id)
        except Exception:
            result = RegressionVerificationResult(available=False)
        measured: dict[str, JsonValue] = {
            "before_high_critical_count": result.before_event_count,
            "before_high_critical_duration_seconds": result.before_duration_seconds,
            "after_high_critical_count": result.after_event_count,
            "after_high_critical_duration_seconds": result.after_duration_seconds,
        }
        if not result.available:
            return _check(
                check_id,
                VerificationStatus.NEEDS_REVIEW,
                "The detector-local quality comparison was unavailable.",
                measured,
            )
        if (
            result.after_event_count > result.before_event_count
            or result.after_duration_seconds > result.before_duration_seconds
        ):
            return _check(
                check_id,
                VerificationStatus.NEEDS_REVIEW,
                "A detector-local high-severity event summary increased after "
                "redaction.",
                measured,
            )
        return _check(
            check_id,
            VerificationStatus.PASSED,
            "The detector-local high-severity event summary did not increase.",
            measured,
        )


def verify_public_manifest(
    manifest: Mapping[str, JsonValue],
    *,
    plan_digest: str = "0" * 64,
) -> PrivacyVerificationReport:
    """Verify one in-memory public manifest without retaining rejected content."""
    try:
        validate_public_manifest(manifest)
    except Exception:
        check = _check(
            "public_artifact_privacy",
            VerificationStatus.FAILED,
            "The public manifest contains a disallowed private field or identifier.",
            {"valid": False},
        )
    else:
        check = _check(
            "public_artifact_privacy",
            VerificationStatus.PASSED,
            "The public manifest contains only approved public values.",
            {"valid": True},
        )
    checks = tuple(
        check
        if check_id == "public_artifact_privacy"
        else _check(
            check_id,
            VerificationStatus.NEEDS_REVIEW,
            "This check was not run by the manifest-only verifier.",
            {},
        )
        for check_id in PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS
    )
    return PrivacyVerificationReport(
        plan_digest=plan_digest,
        status=_aggregate_outcome(checks),
        checks=checks,
    )


def _safe_probe(
    probe: MediaProbe,
    path: Path,
) -> MediaVerificationSnapshot | None:
    try:
        return probe(path)
    except Exception:
        return None


def _safe_profile(profile_id: str) -> ShareAudienceProfile | None:
    try:
        return get_share_audience_profile(profile_id)
    except KeyError:
        return None


def _decodable_check(
    candidate: MediaVerificationSnapshot | None,
) -> PrivacyVerificationCheck:
    return _boolean_check(
        "decodable",
        candidate is not None and candidate.video_stream_count > 0,
        "The candidate was independently probed as decodable video.",
        "The candidate could not be independently probed as decodable video.",
        {"candidate_probe_available": candidate is not None},
    )


def _duration_check(
    source: MediaVerificationSnapshot | None,
    candidate: MediaVerificationSnapshot | None,
) -> PrivacyVerificationCheck:
    tolerance = (
        max(
            _DURATION_TOLERANCE_SECONDS,
            _DURATION_TOLERANCE_FRAMES / max(source.average_frame_rate, _MIN_FPS),
        )
        if source is not None
        else _DURATION_TOLERANCE_SECONDS
    )
    drift = (
        abs(source.duration_seconds - candidate.duration_seconds)
        if source is not None and candidate is not None
        else None
    )
    return _boolean_check(
        "duration",
        drift is not None and drift <= tolerance,
        "Candidate duration is within the source-relative tolerance.",
        "Candidate duration is unavailable or exceeds the allowed drift.",
        {"drift_seconds": drift, "tolerance_seconds": tolerance},
    )


def _streams_check(
    source: MediaVerificationSnapshot | None,
    candidate: MediaVerificationSnapshot | None,
) -> PrivacyVerificationCheck:
    passed = (
        source is not None
        and candidate is not None
        and source.video_stream_count == candidate.video_stream_count
        and source.audio_stream_count == candidate.audio_stream_count
    )
    return _boolean_check(
        "streams",
        passed,
        "Candidate stream presence matches the source.",
        "Candidate video or audio stream presence does not match the source.",
        {
            "source_video_streams": source.video_stream_count if source else None,
            "source_audio_streams": source.audio_stream_count if source else None,
            "candidate_video_streams": (
                candidate.video_stream_count if candidate else None
            ),
            "candidate_audio_streams": (
                candidate.audio_stream_count if candidate else None
            ),
        },
    )


def _profile_check(
    plan: PrivacyPlan,
    profile: ShareAudienceProfile | None,
) -> PrivacyVerificationCheck:
    passed = (
        profile is not None
        and plan.effective_config.profile_version == profile.version
        and plan.effective_config.qr_handling == profile.qr_handling
        and plan.effective_config.default_visual_style is profile.default_visual_style
        and plan.effective_config.verification_policy
        == PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS
    )
    return _boolean_check(
        "profile",
        passed,
        "The candidate was prepared under the selected audience profile.",
        "The selected audience profile is unknown or version-mismatched.",
        {
            "profile_id": plan.profile,
            "profile_version": plan.effective_config.profile_version,
            "profile_contract_matches": passed,
        },
    )


def _metadata_check(
    candidate: MediaVerificationSnapshot | None,
    profile: ShareAudienceProfile | None,
) -> PrivacyVerificationCheck:
    forbidden = set(profile.forbidden_metadata_categories) if profile else set()
    present = set(candidate.metadata_categories) if candidate else set()
    residual = present & forbidden
    embedded = candidate.has_embedded_metadata if candidate else False
    return _boolean_check(
        "metadata",
        candidate is not None and not residual and not embedded,
        "Forbidden embedded metadata was absent from the candidate.",
        "Forbidden or unclassified embedded metadata remained in the candidate.",
        {
            "residual_categories_count": len(residual),
            "unclassified_embedded_metadata": embedded,
        },
    )


def _public_artifact_check(
    public_root: Path,
    plan: PrivacyPlan,
    context: PrivacyVerificationContext,
    candidate_sha256: str,
) -> PrivacyVerificationCheck:
    expected = set(context.expected_artifacts)
    configured = set(plan.effective_config.expected_artifacts)
    try:
        layout = PrivacyArtifactLayout(
            job_root=Path(public_root).parent,
            private_root=Path(public_root).parent / "privacy-review-private",
            public_root=Path(public_root),
        )
        files = layout.validate_public_tree()
        file_set = set(files)
        if not expected <= configured or not expected <= file_set:
            raise PrivacyArtifactError("expected public artifact is missing")
        if candidate_sha256 != context.expected_candidate_sha256:
            raise PrivacyArtifactError("candidate digest does not match execution")
        if not _json_artifact_binds_candidate(
            public_root / "changes.json",
            candidate_sha256,
        ):
            raise PrivacyArtifactError("change log does not bind candidate digest")
        if "manifest.json" in expected and not _json_artifact_binds_candidate(
            public_root / "manifest.json",
            candidate_sha256,
        ):
            raise PrivacyArtifactError("manifest does not bind candidate digest")
    except Exception:
        return _check(
            "public_artifact_privacy",
            VerificationStatus.FAILED,
            "The public package contains a disallowed field, value, path, or file.",
            {"valid": False},
        )
    return _check(
        "public_artifact_privacy",
        VerificationStatus.PASSED,
        "The public package passed local privacy-boundary validation.",
        {
            "valid": True,
            "files_count": len(files),
            "expected_files_count": len(expected),
            "candidate_digest_matches": True,
        },
    )


def _apply_scanner_issues(
    checks: tuple[PrivacyVerificationCheck, ...],
    issues: tuple[ScannerVerificationIssue, ...],
    profile: ShareAudienceProfile | None,
) -> tuple[PrivacyVerificationCheck, ...]:
    category_checks = {
        "audio": "audio_mute",
        "metadata": "metadata",
        "qr_barcode": "qr_redaction",
        "text": "text_redaction",
        "visual": "visual_coverage",
    }
    required_categories = (
        set(profile.required_manual_review_categories)
        if profile is not None
        else set(category_checks)
    )
    required_issues = tuple(
        issue for issue in issues if issue.category in required_categories
    )
    affected = {category_checks[issue.category] for issue in required_issues}
    required_checks = tuple(
        check
        if check.check_id not in affected or check.status is VerificationStatus.FAILED
        else _check(
            check.check_id,
            VerificationStatus.NEEDS_REVIEW,
            "An applicable privacy scanner was unavailable; manual review is required.",
            {
                **check.measured,
                "scanner_errors_count": sum(
                    category_checks[issue.category] == check.check_id
                    for issue in required_issues
                ),
            },
        )
        for check in checks
    )
    optional_issues = tuple(
        sorted(
            (issue for issue in issues if issue.category not in required_categories),
            key=lambda issue: issue.scanner_id,
        )
    )
    optional_checks = tuple(
        _check(
            f"scanner_issue:{issue.scanner_id}",
            VerificationStatus.NEEDS_REVIEW,
            "An optional privacy scanner was unavailable.",
            {"category": issue.category},
            required=False,
        )
        for issue in optional_issues
    )
    return (*required_checks, *optional_checks)


def _resolved_directory(path: Path) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise PrivacyArtifactError("Safe Sharing public root is unavailable") from exc
    if not resolved.is_dir():
        raise PrivacyArtifactError("Safe Sharing public root is not a directory")
    return resolved


def _safe_sha256(path: Path) -> str | None:
    digest = sha256()
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _is_sha256(value: str) -> bool:
    if len(value) != 64 or value != value.lower():
        return False
    try:
        return len(bytes.fromhex(value)) == 32
    except ValueError:
        return False


def _json_artifact_binds_candidate(path: Path, expected_sha256: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, Mapping):
        return False
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        return False
    return any(
        isinstance(artifact, Mapping)
        and artifact.get("relative_path") == "share-safe.mp4"
        and artifact.get("sha256") == expected_sha256
        for artifact in artifacts
    )


def _sample_timestamps(
    start: float, end: float, sample_fps: float
) -> tuple[float, ...]:
    if end <= start:
        return ()
    step = 1.0 / sample_fps
    values: list[float] = []
    index = 0
    while True:
        timestamp = round(start + index * step, 6)
        if timestamp >= end:
            break
        values.append(timestamp)
        index += 1
    return tuple(values)


def _risk_timestamps(
    risks: tuple[PrivacyRisk, ...],
    sample_fps: float,
) -> tuple[float, ...]:
    return tuple(
        sorted(
            {
                timestamp
                for risk in risks
                for timestamp in _sample_timestamps(
                    risk.start_seconds, risk.end_seconds, sample_fps
                )
            }
        )
    )


def _timestamp_set(values: tuple[float, ...]) -> set[float]:
    return {round(float(value), 6) for value in values}


def _timestamps_cover(
    observed: tuple[float, ...], requested: tuple[float, ...]
) -> bool:
    return _timestamp_set(requested) <= _timestamp_set(observed)


def _passed(check_id: str, message: str) -> PrivacyVerificationCheck:
    return _check(check_id, VerificationStatus.PASSED, message, {})


def _boolean_check(
    check_id: str,
    passed: bool,
    passed_message: str,
    failed_message: str,
    measured: dict[str, JsonValue],
) -> PrivacyVerificationCheck:
    return _check(
        check_id,
        VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        passed_message if passed else failed_message,
        measured,
    )


def _check(
    check_id: str,
    status: VerificationStatus,
    message: str,
    measured: dict[str, JsonValue],
    *,
    required: bool = True,
) -> PrivacyVerificationCheck:
    return PrivacyVerificationCheck(
        check_id=check_id,
        status=status,
        message=message,
        measured=measured,
        required=required,
    )


def _aggregate_outcome(
    checks: tuple[PrivacyVerificationCheck, ...],
) -> PrivacyJobOutcome:
    required = tuple(check for check in checks if check.required)
    if any(check.status is VerificationStatus.FAILED for check in required):
        return PrivacyJobOutcome.FAILED
    if any(check.status is VerificationStatus.NEEDS_REVIEW for check in required):
        return PrivacyJobOutcome.NEEDS_REVIEW
    if any(
        check.status is not VerificationStatus.PASSED
        for check in checks
        if not check.required
    ):
        return PrivacyJobOutcome.PARTIAL
    return PrivacyJobOutcome.COMPLETED


__all__ = [
    "AudioVerificationResult",
    "MediaVerificationSnapshot",
    "PrivacyVerificationContext",
    "PrivacyVerifier",
    "RegressionVerificationResult",
    "RescanVerificationResult",
    "ScannerVerificationIssue",
    "TemporalCoverageResult",
    "verify_public_manifest",
]
