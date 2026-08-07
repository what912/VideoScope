"""Tests for canonical Safe Sharing privacy JSON serialization."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from videoscope.domain import Severity
from videoscope.privacy.models import (
    PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS,
    NormalizedBox,
    PrivacyAction,
    PrivacyActionKind,
    PrivacyArtifact,
    PrivacyDecision,
    PrivacyEffectiveConfig,
    PrivacyJobOutcome,
    PrivacyPlan,
    PrivacyRisk,
    PrivacyRiskMap,
    PrivacyRiskType,
    PrivacyTechnicalReport,
    PrivacyVerificationCheck,
    PrivacyVerificationReport,
    RedactionStyle,
    VerificationStatus,
    make_privacy_plan_digest,
    make_privacy_risk_id,
)
from videoscope.privacy.serialization import (
    privacy_plan_to_json,
    privacy_risk_map_to_json,
    privacy_technical_report_to_json,
    read_privacy_plan_json,
    read_privacy_risk_map_json,
    read_privacy_technical_report_json,
    write_privacy_plan_json,
    write_privacy_risk_map_json,
    write_privacy_technical_report_json,
)


def make_risk_map() -> PrivacyRiskMap:
    """Build a real private review document with public Chinese text."""
    box = NormalizedBox(x_min=0.1, y_min=0.2, x_max=0.4, y_max=0.5)
    risk = PrivacyRisk(
        id=make_privacy_risk_id(
            "a" * 64,
            "qr_barcode_region",
            PrivacyRiskType.QR_CODE,
            1.0,
            2.0,
            box,
        ),
        scanner_id="qr_barcode_region",
        scanner_version="1.0.0",
        risk_type=PrivacyRiskType.QR_CODE,
        title="二维码区域",
        public_description="检测到需要人工复核的二维码区域。",
        severity=Severity.HIGH,
        confidence=0.8,
        start_seconds=1.0,
        end_seconds=2.0,
        box=box,
        track_id=None,
        metadata_scope=None,
        metadata_key=None,
        recommended_style=RedactionStyle.PIXELATE,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.PIXELATE,
        limitations=("局部 CPU 启发式可能遗漏区域。",),
        evidence=({"timestamp_seconds": 1.0},),
        private_evidence=({"ocr_text": "仅限私有复核"},),
    )
    return PrivacyRiskMap(
        input_hash="a" * 64,
        profile="public",
        duration_seconds=4.0,
        risks=(risk,),
    )


def make_plan() -> PrivacyPlan:
    """Build a digest-bound plan with hand-specified action and artifact values."""
    risk_map = make_risk_map().public_summary()
    config = PrivacyEffectiveConfig(preview_seconds=3.0, guard_pixels=12)
    action = PrivacyAction(
        id="redact-qr",
        version="1.0.0",
        kind=PrivacyActionKind.VISUAL_REDACTION,
        start_seconds=1.0,
        end_seconds=2.0,
        box=risk_map.risks[0].box,
        parameters={"style": "pixelate"},
        changes_semantics=True,
        requires_confirmation=True,
    )
    artifact = PrivacyArtifact(
        relative_path="share-safe.mp4",
        sha256="b" * 64,
        description="可安全分享的视频副本",
    )
    return PrivacyPlan(
        input_hash=risk_map.input_hash,
        profile=risk_map.profile,
        effective_config=config,
        risks=risk_map.risks,
        actions=(action,),
        artifacts=(artifact,),
        digest=make_privacy_plan_digest(
            risk_map.input_hash,
            risk_map.profile,
            config,
            risk_map.risks,
            (action,),
            (artifact,),
        ),
    )


def make_report() -> PrivacyTechnicalReport:
    """Build the technical report tied to a complete, stable plan."""
    plan = make_plan()
    checks = tuple(
        PrivacyVerificationCheck(
            check_id=check_id,
            status=VerificationStatus.PASSED,
            message="输出已完成本地验证。",
        )
        for check_id in PRIVACY_REQUIRED_VERIFICATION_CHECK_IDS
    )
    return PrivacyTechnicalReport(
        plan_digest=plan.digest,
        verification=PrivacyVerificationReport(
            plan_digest=plan.digest,
            status=PrivacyJobOutcome.COMPLETED,
            checks=checks,
        ),
        artifacts=plan.artifacts,
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("writer", "reader", "value_factory"),
    [
        (write_privacy_risk_map_json, read_privacy_risk_map_json, make_risk_map),
        (write_privacy_plan_json, read_privacy_plan_json, make_plan),
        (
            write_privacy_technical_report_json,
            read_privacy_technical_report_json,
            make_report,
        ),
    ],
)
def test_atomic_privacy_writers_replace_in_unicode_directory(
    tmp_path: Path,
    writer: Callable[[object, Path], None],
    reader: Callable[[Path], object],
    value_factory: Callable[[], object],
) -> None:
    """Writers must replace stale JSON without leaving a temporary artifact behind."""
    destination = tmp_path / "中文 目录" / "result.json"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    value = value_factory()

    writer(value, destination)

    assert reader(destination) == value
    assert list(destination.parent.glob("*.tmp")) == []


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("serializer", "value_factory"),
    [
        (privacy_risk_map_to_json, make_risk_map),
        (privacy_plan_to_json, make_plan),
        (privacy_technical_report_to_json, make_report),
    ],
)
def test_privacy_json_is_canonical_and_preserves_chinese_text(
    serializer: Callable[[object], str],
    value_factory: Callable[[], object],
) -> None:
    """Public JSON must be stable, sorted, and UTF-8 rather than ASCII escaped."""
    value = value_factory()

    first = serializer(value)
    second = serializer(value)

    assert first == second
    assert "\\u" not in first
    assert list(json.loads(first)) == sorted(json.loads(first))
    assert first.endswith("\n") is False
