"""Conservative, independently injectable Safe Sharing verification tests."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from videoscope.domain import Severity
from videoscope.privacy import verification as verification_module
from videoscope.privacy.artifacts import PrivacyArtifactLayout
from videoscope.privacy.errors import PrivacyArtifactError, PrivacyConfirmationError
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
    build_manual_audio_risk,
    build_manual_visual_risk,
)
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyDecision,
    PrivacyEffectiveConfig,
    PrivacyJobOutcome,
    PrivacyPlan,
    PrivacyRisk,
    PrivacyRiskMap,
    PrivacyRiskType,
    PrivacyVerificationCheck,
    PrivacyVerificationReport,
    RedactionStyle,
    VerificationStatus,
    make_privacy_plan_digest,
    make_privacy_risk_id,
)
from videoscope.privacy.planner import build_privacy_plan
from videoscope.privacy.profiles import get_share_audience_profile
from videoscope.privacy.verification import (
    AudioVerificationResult,
    MediaVerificationSnapshot,
    PrivacyVerificationContext,
    PrivacyVerifier,
    RegressionVerificationResult,
    RescanVerificationResult,
    ScannerVerificationIssue,
    TemporalCoverageResult,
    verify_public_manifest,
)

_SOURCE_BYTES = b"source"
_INPUT_HASH = sha256(_SOURCE_BYTES).hexdigest()


def test_default_media_probe_ignores_structural_mp4_tags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ordinary MP4 bookkeeping must not make every native candidate fail."""
    metadata = SimpleNamespace(
        duration_seconds=4.0,
        average_frame_rate=10.0,
        has_audio=False,
    )
    private = SimpleNamespace(
        global_tags={
            "major_brand": "isom",
            "minor_version": "512",
            "compatible_brands": "isomiso2mp41",
            "encoder": "Lavf62.3.100",
        },
        stream_tags=(
            SimpleNamespace(
                tags={
                    "language": "und",
                    "handler_name": "VideoHandler",
                    "vendor_id": "[0][0][0][0]",
                    "encoder": "Lavc62.11.100 libx264",
                }
            ),
        ),
        chapter_tags=(),
        attachment_tags=(),
    )
    monkeypatch.setattr(
        verification_module,
        "probe_video_with_private_summary",
        lambda _path: (metadata, private),
    )

    snapshot = verification_module._default_media_probe(tmp_path / "candidate.mp4")

    assert snapshot.has_embedded_metadata is False


def test_default_media_probe_retains_nonstructural_metadata_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Removing false positives must not hide author or device-style metadata."""
    metadata = SimpleNamespace(
        duration_seconds=4.0,
        average_frame_rate=10.0,
        has_audio=False,
    )
    private = SimpleNamespace(
        global_tags={"major_brand": "isom", "artist": "private author"},
        stream_tags=(),
        chapter_tags=(),
        attachment_tags=(),
    )
    monkeypatch.setattr(
        verification_module,
        "probe_video_with_private_summary",
        lambda _path: (metadata, private),
    )

    snapshot = verification_module._default_media_probe(tmp_path / "candidate.mp4")

    assert snapshot.has_embedded_metadata is True


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("global_tags", "stream_tags"),
    [
        (
            {"major_brand": "isom", "encoder": "Private Studio Export"},
            {},
        ),
        (
            {"major_brand": "isom"},
            {"handler_name": "Alice private client"},
        ),
        (
            {"major_brand": "isom"},
            {"encoder": "Internal workstation Alice"},
        ),
        (
            {"major_brand": "isom"},
            {"language": "Alice-private"},
        ),
        (
            {"major_brand": "Alice"},
            {},
        ),
        (
            {"major_brand": "isom", "minor_version": "9876543210"},
            {},
        ),
        (
            {"major_brand": "isom", "compatible_brands": "AlicePrivate"},
            {},
        ),
        (
            {"major_brand": "isom"},
            {"encoder": "Lavc62.11.100 AlicePrivate"},
        ),
    ],
)
def test_default_media_probe_rejects_custom_values_for_structural_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    global_tags: dict[str, str],
    stream_tags: dict[str, str],
) -> None:
    """A familiar metadata key must not hide a custom potentially private value."""
    metadata = SimpleNamespace(
        duration_seconds=4.0,
        average_frame_rate=10.0,
        has_audio=False,
    )
    private = SimpleNamespace(
        global_tags=global_tags,
        stream_tags=(SimpleNamespace(tags=stream_tags),),
        chapter_tags=(),
        attachment_tags=(),
    )
    monkeypatch.setattr(
        verification_module,
        "probe_video_with_private_summary",
        lambda _path: (metadata, private),
    )

    snapshot = verification_module._default_media_probe(tmp_path / "candidate.mp4")

    assert snapshot.has_embedded_metadata is True


def test_sample_timestamps_cover_half_open_interval_at_actual_frame_rate() -> None:
    assert verification_module._sample_timestamps(1.0, 1.4, 10.0) == (
        1.0,
        1.1,
        1.2,
        1.3,
    )


def _risk(
    risk_type: PrivacyRiskType,
    *,
    start: float,
    end: float,
    box: NormalizedBox,
    input_hash: str = _INPUT_HASH,
) -> PrivacyRisk:
    scanner_id = f"test_{risk_type.value}"
    return PrivacyRisk(
        id=make_privacy_risk_id(
            input_hash,
            scanner_id,
            risk_type,
            start,
            end,
            box,
        ),
        scanner_id=scanner_id,
        scanner_version="1.0.0",
        risk_type=risk_type,
        title="Reviewable privacy observation",
        public_description="A local heuristic proposed this region for review.",
        severity=Severity.HIGH,
        confidence=0.9,
        start_seconds=start,
        end_seconds=end,
        box=box,
        recommended_style=RedactionStyle.BLUR,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.BLUR,
        limitations=("Heuristic proposals require human review.",),
        evidence=({"timestamp_seconds": start, "box": box.model_dump(mode="json")},),
    )


def _plan(
    input_hash: str = _INPUT_HASH,
    profile_id: str = "public",
) -> PrivacyPlan:
    visual_box = NormalizedBox(x_min=0.05, y_min=0.1, x_max=0.35, y_max=0.45)
    qr_box = NormalizedBox(x_min=0.55, y_min=0.1, x_max=0.9, y_max=0.45)
    text_box = NormalizedBox(x_min=0.2, y_min=0.6, x_max=0.8, y_max=0.9)
    risks = (
        build_manual_visual_risk(
            input_hash,
            ManualVisualRegionInput(
                start_seconds=0.5,
                end_seconds=1.5,
                box=visual_box,
                style=RedactionStyle.BLUR,
                source_duration_seconds=4.0,
            ),
        ),
        build_manual_audio_risk(
            input_hash,
            ManualAudioIntervalInput(
                start_seconds=1.0,
                end_seconds=2.0,
                source_duration_seconds=4.0,
            ),
        ),
        _risk(
            PrivacyRiskType.QR_CODE,
            start=2.0,
            end=3.0,
            box=qr_box,
            input_hash=input_hash,
        ),
        _risk(
            PrivacyRiskType.SUSPICIOUS_TEXT,
            start=3.0,
            end=4.0,
            box=text_box,
            input_hash=input_hash,
        ),
    )
    profile = get_share_audience_profile(profile_id)
    return build_privacy_plan(
        PrivacyRiskMap(
            input_hash=input_hash,
            profile=profile.id,
            duration_seconds=4.0,
            risks=risks,
        ),
        (),
        profile,
        PrivacyEffectiveConfig(),
    )


class StaticProbe:
    def __init__(
        self, source: MediaVerificationSnapshot, candidate: MediaVerificationSnapshot
    ) -> None:
        self.source = source
        self.candidate = candidate

    def __call__(self, path: Path) -> MediaVerificationSnapshot:
        return self.source if path.name == "source.mp4" else self.candidate


class CoverageChecker:
    def __init__(self, *, missing_timestamp: float | None = None) -> None:
        self.missing_timestamp = missing_timestamp

    def verify(
        self,
        source: Path,
        candidate: Path,
        action: object,
        timestamps: tuple[float, ...],
    ) -> TemporalCoverageResult:
        del source, candidate, action
        checked = tuple(
            timestamp for timestamp in timestamps if timestamp != self.missing_timestamp
        )
        return TemporalCoverageResult(
            checked_timestamps=checked,
            uncovered_timestamps=(),
        )


class Rescanner:
    def __init__(
        self, *, detected: tuple[float, ...] = (), available: bool = True
    ) -> None:
        self.detected = detected
        self.available = available

    def rescan(
        self,
        candidate: Path,
        risks: tuple[PrivacyRisk, ...],
        timestamps: tuple[float, ...],
    ) -> RescanVerificationResult:
        del candidate, risks
        return RescanVerificationResult(
            available=self.available,
            checked_timestamps=timestamps if self.available else (),
            detected_timestamps=self.detected,
        )


class AudioAnalyzer:
    def __init__(
        self,
        *,
        excessive: tuple[float, ...] = (),
        outside_signal_retained: bool | None = True,
    ) -> None:
        self.excessive = excessive
        self.outside_signal_retained = outside_signal_retained

    def verify(
        self,
        source: Path,
        candidate: Path,
        actions: tuple[object, ...],
        timestamps: tuple[float, ...],
    ) -> AudioVerificationResult:
        del source, candidate, actions
        return AudioVerificationResult(
            available=True,
            checked_timestamps=timestamps,
            excessive_energy_timestamps=self.excessive,
            outside_signal_retained=self.outside_signal_retained,
        )


class RegressionAnalyzer:
    def __init__(
        self, *, after_black: int = 0, after_freeze: int = 0, available: bool = True
    ) -> None:
        self.after_black = after_black
        self.after_freeze = after_freeze
        self.available = available

    def compare(
        self, source: Path, candidate: Path, detector_id: str
    ) -> RegressionVerificationResult:
        del source, candidate
        after = self.after_black if detector_id == "near_black" else self.after_freeze
        return RegressionVerificationResult(
            available=self.available,
            before_event_count=0,
            before_duration_seconds=0.0,
            after_event_count=after,
            after_duration_seconds=float(after),
        )


def _snapshot(
    *, duration: float = 4.0, metadata: tuple[str, ...] = ()
) -> MediaVerificationSnapshot:
    return MediaVerificationSnapshot(
        duration_seconds=duration,
        average_frame_rate=10.0,
        video_stream_count=1,
        audio_stream_count=1,
        metadata_categories=metadata,
    )


def _context(
    tmp_path: Path, *, scanner_issues: tuple[ScannerVerificationIssue, ...] = ()
) -> PrivacyVerificationContext:
    layout = PrivacyArtifactLayout.create(tmp_path / "job")
    candidate = layout.public_root / "share-safe.mp4"
    candidate.write_bytes(b"candidate")
    candidate_hash = _sha256_bytes(b"candidate")
    (layout.public_root / "changes.json").write_text(
        '{"artifacts":[{"relative_path":"share-safe.mp4","sha256":"'
        + candidate_hash
        + '"}]}',
        encoding="utf-8",
    )
    return PrivacyVerificationContext(
        public_root=layout.public_root,
        expected_candidate_sha256=candidate_hash,
        expected_artifacts=("share-safe.mp4", "changes.json"),
        scanner_issues=scanner_issues,
        sample_fps=2.0,
        mute_rms_threshold=0.01,
    )


def _verifier(
    *,
    source: MediaVerificationSnapshot | None = None,
    candidate: MediaVerificationSnapshot | None = None,
    coverage: CoverageChecker | None = None,
    qr: Rescanner | None = None,
    text: Rescanner | None = None,
    audio: AudioAnalyzer | None = None,
    regression: RegressionAnalyzer | None = None,
) -> PrivacyVerifier:
    return PrivacyVerifier(
        media_probe=StaticProbe(source or _snapshot(), candidate or _snapshot()),
        coverage_checker=coverage or CoverageChecker(),
        qr_rescanner=qr or Rescanner(),
        text_rescanner=text or Rescanner(),
        audio_analyzer=audio or AudioAnalyzer(),
        regression_analyzer=regression or RegressionAnalyzer(),
    )


def _run(
    tmp_path: Path,
    verifier: PrivacyVerifier | None = None,
    *,
    context: PrivacyVerificationContext | None = None,
    plan: PrivacyPlan | None = None,
) -> PrivacyVerificationReport:
    private_context = context or _context(tmp_path)
    source = tmp_path / "source.mp4"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(_SOURCE_BYTES)
    return (verifier or _verifier()).verify(
        source,
        private_context.public_root / "share-safe.mp4",
        plan or _plan(_INPUT_HASH),
        private_context,
    )


def _check(
    report: PrivacyVerificationReport,
    check_id: str,
) -> PrivacyVerificationCheck:
    return next(item for item in report.checks if item.check_id == check_id)


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _plan_with_config(plan: PrivacyPlan, config: PrivacyEffectiveConfig) -> PrivacyPlan:
    digest = make_privacy_plan_digest(
        plan.input_hash,
        plan.profile,
        config,
        plan.risks,
        plan.actions,
        plan.artifacts,
        duration_seconds=plan.duration_seconds,
    )
    return PrivacyPlan(
        input_hash=plan.input_hash,
        profile=plan.profile,
        duration_seconds=plan.duration_seconds,
        effective_config=config,
        risks=plan.risks,
        actions=plan.actions,
        artifacts=plan.artifacts,
        digest=digest,
    )


def test_verification_report_rejects_missing_or_nonrequired_contract_checks() -> None:
    with pytest.raises(ValidationError, match="verification check"):
        PrivacyVerificationReport(
            plan_digest="a" * 64,
            status=PrivacyJobOutcome.COMPLETED,
            checks=(),
        )

    report = _run_contract_report()
    forged = report.model_dump(mode="python")
    forged["checks"][0]["required"] = False
    with pytest.raises(ValidationError, match="required"):
        PrivacyVerificationReport.model_validate(forged)


def test_verification_report_rejects_deserialized_missing_check() -> None:
    report = _run_contract_report()
    forged = report.model_dump(mode="python")
    forged["checks"] = forged["checks"][:-1]

    with pytest.raises(ValidationError, match="verification check"):
        PrivacyVerificationReport.model_validate(forged)


def test_verification_report_allows_only_namespaced_nonrequired_scanner_issues() -> (
    None
):
    report = _run_contract_report()
    payload = report.model_dump(mode="python")
    payload["status"] = PrivacyJobOutcome.PARTIAL
    payload["checks"] = list(payload["checks"])
    payload["checks"].append(
        {
            "check_id": "scanner_issue:optional_text",
            "status": VerificationStatus.NEEDS_REVIEW,
            "message": "Optional text scanner unavailable.",
            "measured": {"category": "text"},
            "required": False,
        }
    )
    partial = PrivacyVerificationReport.model_validate(payload)
    assert partial.status is PrivacyJobOutcome.PARTIAL

    forged_id = partial.model_dump(mode="python")
    forged_id["checks"][-1]["check_id"] = "unapproved_extra"
    with pytest.raises(ValidationError, match="optional verification check"):
        PrivacyVerificationReport.model_validate(forged_id)

    forged_required = partial.model_dump(mode="python")
    forged_required["checks"][-1]["required"] = True
    with pytest.raises(ValidationError, match="optional verification check"):
        PrivacyVerificationReport.model_validate(forged_required)


def _run_contract_report() -> PrivacyVerificationReport:
    checks = tuple(
        PrivacyVerificationCheck(
            check_id=check_id,
            status=VerificationStatus.PASSED,
            message="Verified.",
        )
        for check_id in (
            "decodable",
            "duration",
            "streams",
            "profile",
            "metadata",
            "visual_coverage",
            "qr_redaction",
            "text_redaction",
            "audio_mute",
            "black_regression",
            "freeze_regression",
            "public_artifact_privacy",
        )
    )
    return PrivacyVerificationReport(
        plan_digest="a" * 64,
        status=PrivacyJobOutcome.COMPLETED,
        checks=checks,
    )


def test_source_hash_must_match_confirmed_plan_before_checks(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"different source")
    context = _context(tmp_path)

    with pytest.raises(PrivacyConfirmationError) as captured:
        _verifier().verify(
            source,
            context.public_root / "share-safe.mp4",
            _plan(),
            context,
        )
    assert "plan does not match source" in str(captured.value.internal_message)


def test_candidate_must_be_exact_share_safe_file_inside_public_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    context = _context(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"candidate")

    with pytest.raises(PrivacyArtifactError) as captured:
        _verifier().verify(source, outside, _plan(), context)
    assert "share-safe" in str(captured.value.internal_message)


def test_candidate_hash_must_match_private_execution_expectation(
    tmp_path: Path,
) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path / "job")
    candidate = layout.public_root / "share-safe.mp4"
    candidate.write_bytes(b"candidate")
    (layout.public_root / "changes.json").write_text("{}", encoding="utf-8")
    context = PrivacyVerificationContext(
        public_root=layout.public_root,
        expected_candidate_sha256="f" * 64,
        expected_artifacts=("share-safe.mp4", "changes.json"),
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    plan = _plan(_sha256_bytes(b"source"))

    report = _verifier().verify(source, candidate, plan, context)

    assert report.status is PrivacyJobOutcome.FAILED
    assert _check(report, "public_artifact_privacy").status is VerificationStatus.FAILED


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "config",
    [
        PrivacyEffectiveConfig(profile_version="99"),
        PrivacyEffectiveConfig(qr_handling="review"),
        PrivacyEffectiveConfig(default_visual_style=RedactionStyle.PIXELATE),
        PrivacyEffectiveConfig(verification_policy=("decodable",)),
    ],
)
def test_profile_check_rejects_effective_policy_forgery(
    tmp_path: Path,
    config: PrivacyEffectiveConfig,
) -> None:
    plan = _plan_with_config(_plan(), config)
    report = _run(tmp_path, plan=plan)

    assert _check(report, "profile").status is VerificationStatus.FAILED
    assert report.status is PrivacyJobOutcome.FAILED


def test_public_artifact_check_fails_empty_or_missing_current_stage_files(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    (context.public_root / "share-safe.mp4").unlink()
    with pytest.raises(PrivacyArtifactError):
        _run(tmp_path / "empty", context=context)

    missing_changes = _context(tmp_path / "missing-changes")
    (missing_changes.public_root / "changes.json").unlink()
    report = _run(tmp_path / "missing-changes", context=missing_changes)
    assert _check(report, "public_artifact_privacy").status is VerificationStatus.FAILED


def test_all_independent_required_checks_complete(tmp_path: Path) -> None:
    report = _run(tmp_path)

    assert report.status is PrivacyJobOutcome.COMPLETED
    assert tuple(check.check_id for check in report.checks) == (
        "decodable",
        "duration",
        "streams",
        "profile",
        "metadata",
        "visual_coverage",
        "qr_redaction",
        "text_redaction",
        "audio_mute",
        "black_regression",
        "freeze_regression",
        "public_artifact_privacy",
    )
    assert all(check.status is VerificationStatus.PASSED for check in report.checks)


def test_unverified_required_qr_check_never_completes(tmp_path: Path) -> None:
    report = _run(tmp_path, _verifier(qr=Rescanner(available=False)))

    assert report.status is PrivacyJobOutcome.NEEDS_REVIEW
    assert _check(report, "qr_redaction").status is VerificationStatus.NEEDS_REVIEW


def test_visual_coverage_requires_every_sampling_timestamp(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        _verifier(coverage=CoverageChecker(missing_timestamp=1.0)),
    )

    check = _check(report, "visual_coverage")
    assert check.status is VerificationStatus.NEEDS_REVIEW
    assert check.measured["missing_samples"] == 1
    assert report.status is PrivacyJobOutcome.NEEDS_REVIEW


def test_decodable_qr_or_recovered_text_fails_redaction(tmp_path: Path) -> None:
    qr_report = _run(tmp_path / "qr", _verifier(qr=Rescanner(detected=(2.5,))))
    text_report = _run(
        tmp_path / "text",
        _verifier(text=Rescanner(detected=(3.5,))),
    )

    assert _check(qr_report, "qr_redaction").status is VerificationStatus.FAILED
    assert _check(text_report, "text_redaction").status is VerificationStatus.FAILED
    assert qr_report.status is PrivacyJobOutcome.FAILED
    assert text_report.status is PrivacyJobOutcome.FAILED


def test_unexpected_audio_energy_fails_mute_check(tmp_path: Path) -> None:
    report = _run(
        tmp_path,
        _verifier(audio=AudioAnalyzer(excessive=(1.5,))),
    )

    assert _check(report, "audio_mute").status is VerificationStatus.FAILED
    assert report.status is PrivacyJobOutcome.FAILED


def test_black_or_freeze_regression_needs_review_without_erasing_other_checks(
    tmp_path: Path,
) -> None:
    report = _run(
        tmp_path,
        _verifier(regression=RegressionAnalyzer(after_black=1)),
    )

    assert _check(report, "black_regression").status is VerificationStatus.NEEDS_REVIEW
    assert _check(report, "freeze_regression").status is VerificationStatus.PASSED
    assert _check(report, "metadata").status is VerificationStatus.PASSED
    assert report.status is PrivacyJobOutcome.NEEDS_REVIEW


def test_profile_duration_stream_and_metadata_mismatches_fail(tmp_path: Path) -> None:
    candidate = _snapshot(duration=5.0, metadata=("location",))
    candidate = replace(candidate, audio_stream_count=0)
    report = _run(tmp_path, _verifier(candidate=candidate))

    assert _check(report, "duration").status is VerificationStatus.FAILED
    assert _check(report, "streams").status is VerificationStatus.FAILED
    assert _check(report, "metadata").status is VerificationStatus.FAILED
    assert report.status is PrivacyJobOutcome.FAILED


def test_optional_scanner_error_is_partial_for_profile_that_does_not_require_it(
    tmp_path: Path,
) -> None:
    plan = _plan(profile_id="family")
    issue = ScannerVerificationIssue(scanner_id="optional_text", category="text")
    context = _context(tmp_path, scanner_issues=(issue,))
    report = _run(tmp_path, context=context, plan=plan)

    assert report.status is PrivacyJobOutcome.PARTIAL
    optional = _check(report, "scanner_issue:optional_text")
    assert optional.required is False
    assert optional.status is VerificationStatus.NEEDS_REVIEW


def test_required_scanner_error_needs_review_for_public_profile(tmp_path: Path) -> None:
    issue = ScannerVerificationIssue(scanner_id="optional_text", category="text")
    context = _context(tmp_path, scanner_issues=(issue,))
    report = _run(tmp_path, context=context)

    assert report.status is PrivacyJobOutcome.NEEDS_REVIEW
    assert _check(report, "text_redaction").required is True
    assert all(
        check.check_id != "scanner_issue:optional_text" for check in report.checks
    )


def test_share_manifest_with_private_text_fails() -> None:
    report = verify_public_manifest({"ocr_text": "person@example.com"})

    assert report.status is PrivacyJobOutcome.FAILED
    assert _check(report, "public_artifact_privacy").status is VerificationStatus.FAILED


def test_public_tree_private_field_failure_has_no_private_value_in_message(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    secret = "person@example.com"
    (context.public_root / "verification.json").write_text(
        '{"ocr_text":"' + secret + '"}',
        encoding="utf-8",
    )

    report = _run(tmp_path, context=context)
    check = _check(report, "public_artifact_privacy")

    assert check.status is VerificationStatus.FAILED
    assert secret not in check.message
    assert secret not in str(check.measured)
