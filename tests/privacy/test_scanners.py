"""Behavior tests for deterministic privacy-scanner orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from pydantic import BaseModel, ConfigDict

from videoscope.domain import Severity
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyRisk,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_risk_id,
)
from videoscope.privacy.profiles import PUBLIC
from videoscope.privacy.scanners import (
    PrivacyScanContext,
    PrivacyScannerRegistry,
    PrivacyScannerRequirements,
    PrivacyScannerRunner,
    PrivacyScannerStatus,
)


class ScannerConfig(BaseModel):
    """Strict fake scanner configuration."""

    model_config = ConfigDict(extra="forbid")

    confidence: float = 0.75


class OneRiskScanner:
    id = "one_risk"
    display_name = "One risk"
    version = "1.0.0"
    description = "Returns one deterministic review proposal."
    requirements = PrivacyScannerRequirements()
    config_model: type[BaseModel] = ScannerConfig

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        validated = ScannerConfig.model_validate(config.model_dump())
        box = NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.3, y_max=0.4)
        return [
            PrivacyRisk(
                id=make_privacy_risk_id(
                    context.input_hash,
                    self.id,
                    PrivacyRiskType.MANUAL_VISUAL,
                    1.0,
                    2.0,
                    box,
                ),
                scanner_id=self.id,
                scanner_version=self.version,
                risk_type=PrivacyRiskType.MANUAL_VISUAL,
                title="Region proposed for review",
                public_description="A visual region was proposed for review.",
                severity=Severity.MEDIUM,
                confidence=validated.confidence,
                start_seconds=1.0,
                end_seconds=2.0,
                box=box,
                recommended_style=RedactionStyle.BLUR,
                limitations=("Human review is required.",),
                evidence=({"timestamp_seconds": 1.0},),
            )
        ]


class FailingScanner(OneRiskScanner):
    id = "failing"
    display_name = "Failing scanner"

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        del config
        raise RuntimeError(
            f"failed at {context.input_path} with {context.private_text_values[0]}"
        )


class InterruptingScanner(OneRiskScanner):
    id = "interrupting"
    display_name = "Interrupting scanner"
    signal: ClassVar[type[BaseException]] = KeyboardInterrupt

    def __init__(self, signal: type[BaseException] = KeyboardInterrupt) -> None:
        self._signal = signal

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        del context, config
        raise self._signal()


class WrongScannerRisk(OneRiskScanner):
    id = "wrong_risk"
    display_name = "Wrong risk scanner"

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        risk = super().scan(context, config)[0]
        assert risk.box is not None
        return [
            risk.model_copy(
                update={
                    "id": make_privacy_risk_id(
                        context.input_hash,
                        "one_risk",
                        risk.risk_type,
                        risk.start_seconds,
                        risk.end_seconds,
                        risk.box,
                    ),
                    "scanner_id": "one_risk",
                }
            )
        ]


class SeverityRiskScanner(OneRiskScanner):
    severity: ClassVar[Severity] = Severity.MEDIUM

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        risk = super().scan(context, config)[0]
        return [risk.model_copy(update={"severity": self.severity})]


class HighRiskScanner(SeverityRiskScanner):
    id = "a_high_risk"
    display_name = "High risk"
    severity = Severity.HIGH


class LowRiskScanner(SeverityRiskScanner):
    id = "z_low_risk"
    display_name = "Low risk"
    severity = Severity.LOW


class EmptyEvidenceScanner(OneRiskScanner):
    id = "empty_evidence"
    display_name = "Empty evidence"

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        risk = super().scan(context, config)[0]
        return [risk.model_copy(update={"evidence": ()})]


class EmptyLimitationsScanner(OneRiskScanner):
    id = "empty_limitations"
    display_name = "Empty limitations"

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        risk = super().scan(context, config)[0]
        return [risk.model_copy(update={"limitations": ()})]


def make_scan_context(tmp_path: Path) -> PrivacyScanContext:
    source = tmp_path / "private person" / "源 视频.mp4"
    workspace = tmp_path / "review workspace"
    source.parent.mkdir()
    workspace.mkdir()
    source.write_bytes(b"not decoded in this unit test")
    return PrivacyScanContext(
        input_path=source,
        input_hash="a" * 64,
        duration_seconds=4.0,
        profile=PUBLIC,
        workspace=workspace,
        private_text_values=("person@example.com",),
    )


def test_one_scanner_failure_does_not_hide_other_risks(tmp_path: Path) -> None:
    """An ordinary scanner error cannot erase another scanner's result."""
    runner = PrivacyScannerRunner((FailingScanner(), OneRiskScanner()))

    result = runner.run(make_scan_context(tmp_path), {})

    assert [execution.status for execution in result.executions] == [
        PrivacyScannerStatus.SCANNER_ERROR,
        PrivacyScannerStatus.OK,
    ]
    assert [risk.scanner_id for risk in result.risks] == ["one_risk"]


def test_scanner_error_does_not_expose_paths_or_private_text(tmp_path: Path) -> None:
    """Failure diagnostics cannot disclose source paths or private OCR-like text."""
    context = make_scan_context(tmp_path)

    execution = PrivacyScannerRunner((FailingScanner(),)).run(context, {}).executions[0]

    assert execution.error_type == "RuntimeError"
    assert execution.error_message is not None
    assert str(context.input_path) not in execution.error_message
    assert str(context.workspace) not in execution.error_message
    assert "person@example.com" not in execution.error_message


def test_registry_rejects_duplicate_scanner_id() -> None:
    """Duplicate IDs cannot make scanner selection order ambiguous."""
    registry = PrivacyScannerRegistry()
    registry.register(OneRiskScanner())

    with pytest.raises(ValueError, match="duplicate"):
        registry.register(OneRiskScanner())


def test_registry_lists_scanners_in_stable_id_order() -> None:
    """Registration order cannot change the default run order."""
    registry = PrivacyScannerRegistry((OneRiskScanner(), FailingScanner()))

    assert [scanner.id for scanner in registry.list_available()] == [
        "failing",
        "one_risk",
    ]


def test_runner_sorts_equal_time_risks_by_canonical_severity_order(
    tmp_path: Path,
) -> None:
    """Risk ordering must match the PrivacyRiskMap contract across scanners."""
    result = PrivacyScannerRunner((HighRiskScanner(), LowRiskScanner())).run(
        make_scan_context(tmp_path),
        {},
    )

    assert [(risk.severity, risk.scanner_id) for risk in result.risks] == [
        (Severity.LOW, "z_low_risk"),
        (Severity.HIGH, "a_high_risk"),
    ]


def test_runner_rejects_duplicate_explicit_scanner_ids(tmp_path: Path) -> None:
    """One scanner cannot execute twice through an ambiguous explicit selection."""
    runner = PrivacyScannerRunner((OneRiskScanner(),))

    with pytest.raises(ValueError, match="duplicate"):
        runner.run(
            make_scan_context(tmp_path),
            {},
            scanner_ids=("one_risk", "one_risk"),
        )


def test_runner_rejects_unknown_configuration_ids(tmp_path: Path) -> None:
    """A misspelled scanner configuration cannot be silently ignored."""
    runner = PrivacyScannerRunner((OneRiskScanner(),))

    with pytest.raises(ValueError, match="unknown.*configuration"):
        runner.run(make_scan_context(tmp_path), {"misspelled": {}})


def test_runner_validates_config_and_isolates_invalid_values(tmp_path: Path) -> None:
    """Invalid per-scanner config is recorded without invoking unrelated scanners."""
    result = PrivacyScannerRunner((OneRiskScanner(),)).run(
        make_scan_context(tmp_path),
        {"one_risk": {"unknown": True}},
    )

    assert result.executions[0].status is PrivacyScannerStatus.SCANNER_ERROR
    assert result.risks == ()


def test_runner_rejects_risk_owned_by_another_scanner(tmp_path: Path) -> None:
    """A scanner cannot publish a risk under another scanner's identity."""
    result = PrivacyScannerRunner((WrongScannerRisk(),)).run(
        make_scan_context(tmp_path),
        {},
    )

    assert result.executions[0].status is PrivacyScannerStatus.SCANNER_ERROR
    assert result.risks == ()


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "scanner",
    [EmptyEvidenceScanner(), EmptyLimitationsScanner()],
)
def test_runner_rejects_proposals_without_review_support(
    tmp_path: Path,
    scanner: OneRiskScanner,
) -> None:
    """Every proposal needs observable evidence and an explicit limitation."""
    result = PrivacyScannerRunner((scanner,)).run(make_scan_context(tmp_path), {})

    assert result.executions[0].status is PrivacyScannerStatus.SCANNER_ERROR
    assert result.risks == ()


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "signal", [KeyboardInterrupt, SystemExit]
)
def test_runner_does_not_catch_process_control_signals(
    tmp_path: Path,
    signal: type[BaseException],
) -> None:
    """User cancellation and interpreter exit must escape the scanner runner."""
    scanner = InterruptingScanner(signal)

    with pytest.raises(signal):
        PrivacyScannerRunner((scanner,)).run(make_scan_context(tmp_path), {})
