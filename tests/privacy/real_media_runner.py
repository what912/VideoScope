"""Safe Sharing fixture pipeline runner and public-package assertions."""

from __future__ import annotations

import json
from collections.abc import Sequence
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast

from tests.privacy.real_media_adapters import (
    GENERATED_ROOT,
    REVIEWED_AT,
    _media_probe,
    _NoRiskScannerRunner,
    _privacy_manifest,
    _RealAudioAnalyzer,
    _RealCoverageChecker,
    _RealRegressionAnalyzer,
)
from videoscope.privacy.executor import NativePrivacyExecutor
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
    build_manual_audio_risk,
    build_manual_visual_risk,
)
from videoscope.privacy.models import (
    PrivacyDecision,
    PrivacyReviewDecision,
    PrivacyRisk,
    PrivacyRiskType,
    RedactionStyle,
    VerificationStatus,
)
from videoscope.privacy.pipeline import (
    PrivacyResult,
    SafeSharingConfig,
    SafeSharingPipeline,
)
from videoscope.privacy.verification import PrivacyVerifier
from videoscope.video import sample_frames
from videoscope.video.probe import probe_video_with_private_summary


def _pipeline(
    output: Path,
    ffmpeg: str,
    ffprobe: str,
    *,
    clean_scan: bool = False,
    no_visual_scan: bool = False,
    scanner_runner: object | None = None,
    qr_rescanner: object | None = None,
    text_rescanner: object | None = None,
) -> SafeSharingPipeline:
    verifier = PrivacyVerifier(
        media_probe=partial(_media_probe, ffprobe),
        coverage_checker=_RealCoverageChecker(),
        qr_rescanner=cast(Any, qr_rescanner),
        text_rescanner=cast(Any, text_rescanner),
        audio_analyzer=_RealAudioAnalyzer(ffmpeg),
        regression_analyzer=_RealRegressionAnalyzer(),
    )
    return SafeSharingPipeline(
        output,
        probe=partial(probe_video_with_private_summary, ffprobe=ffprobe),
        sampler=partial(sample_frames, ffmpeg=ffmpeg),
        scanner_runner=(
            _NoRiskScannerRunner() if clean_scan or no_visual_scan else scanner_runner
        ),
        metadata_scanner=(lambda *_: []) if clean_scan else None,
        executor=NativePrivacyExecutor(ffmpeg=ffmpeg, ffprobe=ffprobe),
        verifier=verifier,
    )


def _manual_inputs(
    fixture_name: str,
) -> tuple[tuple[ManualVisualRegionInput, ...], tuple[ManualAudioIntervalInput, ...]]:
    entry = _privacy_manifest()[fixture_name]
    visual = tuple(
        ManualVisualRegionInput.model_validate(value)
        for value in cast(list[dict[str, object]], entry["manual_visual_regions"])
    )
    audio = tuple(
        ManualAudioIntervalInput.model_validate(value)
        for value in cast(list[dict[str, object]], entry["manual_audio_intervals"])
    )
    return visual, audio


def _review_decision(risk: PrivacyRisk) -> PrivacyReviewDecision:
    style: RedactionStyle | None
    if risk.risk_type is PrivacyRiskType.METADATA:
        decision = PrivacyDecision.REDACT
        style = RedactionStyle.REMOVE_METADATA
    elif risk.risk_type in {
        PrivacyRiskType.MANUAL_VISUAL,
        PrivacyRiskType.MANUAL_AUDIO,
        PrivacyRiskType.QR_CODE,
        PrivacyRiskType.BARCODE,
        PrivacyRiskType.SUSPICIOUS_TEXT,
    }:
        decision = PrivacyDecision.REDACT
        style = risk.style or RedactionStyle.SOLID_FILL
    else:
        decision = PrivacyDecision.ALLOW
        style = None
    return PrivacyReviewDecision(
        risk_id=risk.id,
        decision=decision,
        style=style,
        reviewed_at=REVIEWED_AT,
    )


def _run_real_safe_sharing(
    source: Path,
    output: Path,
    fixture_name: str,
    ffmpeg: str,
    ffprobe: str,
    *,
    clean_scan: bool = False,
    no_visual_scan: bool = False,
    preview: bool = False,
    scanner_runner: object | None = None,
    qr_rescanner: object | None = None,
    text_rescanner: object | None = None,
    audience: str = "family",
) -> PrivacyResult:
    pipeline = _pipeline(
        output,
        ffmpeg,
        ffprobe,
        clean_scan=clean_scan,
        no_visual_scan=no_visual_scan,
        scanner_runner=scanner_runner,
        qr_rescanner=qr_rescanner,
        text_rescanner=text_rescanner,
    )
    scan = pipeline.scan(
        source=source,
        config=SafeSharingConfig(
            audience=audience,
            sample_fps=10.0,
            enable_ocr=text_rescanner is not None,
        ),
    )
    visual, audio = _manual_inputs(fixture_name)
    manual_risks = tuple(
        build_manual_visual_risk(scan.risk_map.input_hash, value) for value in visual
    ) + tuple(
        build_manual_audio_risk(scan.risk_map.input_hash, value) for value in audio
    )
    reviews = tuple(
        _review_decision(risk) for risk in (*scan.risk_map.risks, *manual_risks)
    )
    reviewed = pipeline.review(
        scan.scan_id,
        reviews,
        manual_visual_regions=visual,
        manual_audio_intervals=audio,
    )
    preparation = pipeline.prepare(reviewed.review_id)
    if preview:
        preview_path = pipeline.preview(preparation.preparation_id)
        assert preview_path.is_file()
        assert "privacy-review-private" in preview_path.parts
    return pipeline.confirm(preparation.preparation_id, preparation.plan.digest)


def _assert_public_package_is_separate_and_path_free(
    output: Path,
    *,
    forbidden_strings: Sequence[str] = (),
) -> None:
    public_root = output / "share-package"
    private_root = output / "privacy-review-private"
    assert set(path.name for path in public_root.iterdir()) == {
        "changes.json",
        "manifest.json",
        "privacy-summary.json",
        "share-safe.mp4",
        "technical-report.json",
        "verification.json",
    }
    assert (private_root / "risk-map.json").is_file()
    forbidden = {
        str(output.resolve()).casefold(),
        output.resolve().as_posix().casefold(),
        str(GENERATED_ROOT.resolve()).casefold(),
        GENERATED_ROOT.resolve().as_posix().casefold(),
        "private_evidence",
    }
    for json_path in public_root.glob("*.json"):
        content = json_path.read_text(encoding="utf-8").casefold()
        assert not any(value in content for value in forbidden)
        assert not any(value.casefold() in content for value in forbidden_strings)
        payload = cast(dict[str, object], json.loads(content))
        for value in _relative_paths(payload):
            posix = PurePosixPath(value)
            windows = PureWindowsPath(value)
            assert value == posix.as_posix()
            assert not posix.is_absolute()
            assert not windows.is_absolute()
            assert not windows.drive
            assert ".." not in posix.parts


def _relative_paths(value: object) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        if isinstance(mapping.get("relative_path"), str):
            found.append(cast(str, mapping["relative_path"]))
        for item in mapping.values():
            found.extend(_relative_paths(item))
    elif isinstance(value, list):
        for item in cast(list[object], value):
            found.extend(_relative_paths(item))
    return tuple(found)


def _nonpassing_checks(result: object) -> list[tuple[str, str, object]]:
    verification = cast(object, getattr(result, "verification"))
    checks = cast(Sequence[object], getattr(verification, "checks"))
    return [
        (
            cast(str, getattr(check, "check_id")),
            cast(VerificationStatus, getattr(check, "status")).value,
            getattr(check, "measured"),
        )
        for check in checks
        if getattr(check, "status") is not VerificationStatus.PASSED
    ]
