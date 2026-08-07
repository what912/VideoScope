"""Offline tests for optional suspicious-text privacy proposals."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel, ConfigDict

from videoscope.ai import (
    Device,
    DevicePreference,
    FakeOCRProvider,
    ModelRuntimeConfig,
    ModelRuntimeManager,
    ModelSpec,
    Precision,
)
from videoscope.domain import Severity
from videoscope.privacy.models import (
    PrivacyRisk,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_risk_id,
)
from videoscope.privacy.profiles import PUBLIC
from videoscope.privacy.scanners import (
    PrivacyScanContext,
    PrivacyScanner,
    PrivacyScannerRequirements,
    PrivacyScannerRunner,
    PrivacyScannerStatus,
)
from videoscope.privacy.text import (
    SuspiciousTextConfig,
    SuspiciousTextKind,
    SuspiciousTextScanner,
    classify_private_text,
)
from videoscope.scenes import VideoScene
from videoscope.video import FrameSample

_PROVIDER_ID = "fake_ocr"
_MODEL_ID = "fake-ocr-v1"
_OCR_SPEC = ModelSpec(
    provider_id=_PROVIDER_ID,
    model_id=_MODEL_ID,
    capabilities=("ocr",),
    required_extra="ocr",
)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("text", "expected"),
    [
        ("Call 13800138000", SuspiciousTextKind.PHONE),
        ("１３８００１３８０００", SuspiciousTextKind.PHONE),
        ("person@example.com", SuspiciousTextKind.EMAIL),
        ("寄往上海市浦东新区世纪大道123号", SuspiciousTextKind.ADDRESS),
        ("账号: 6222021234567890", SuspiciousTextKind.ACCOUNT),
        ("验证码 482913", SuspiciousTextKind.CODE),
        (r"C:\Users\alice\private\clip.mp4", SuspiciousTextKind.PATH),
        ("https://example.com/private?id=42", SuspiciousTextKind.URL),
        ("Order total 128.00", None),
        ("Frame code H264", None),
    ],
)
def test_private_text_classifier(
    text: str,
    expected: SuspiciousTextKind | None,
) -> None:
    assert classify_private_text(text, locale="zh-CN") is expected


def test_classifier_applies_explicit_locale_rules() -> None:
    assert classify_private_text("寄往上海市浦东新区世纪大道123号", locale="en") is None
    assert (
        classify_private_text("123 Market Street", locale="en")
        is SuspiciousTextKind.ADDRESS
    )
    with pytest.raises(ValueError, match="locale"):
        classify_private_text("person@example.com", locale="fr-FR")
    with pytest.raises(ValueError):
        SuspiciousTextConfig(locale="fr-FR")  # type: ignore[arg-type]


def _runtime(
    tmp_path: Path,
    results_by_timestamp: dict[
        float,
        tuple[tuple[str, float, tuple[float, float, float, float]], ...],
    ],
    *,
    fail_detect: bool = False,
) -> ModelRuntimeManager:
    runtime = ModelRuntimeManager(
        ModelRuntimeConfig(
            device=DevicePreference.CPU,
            batch_size=2,
            disk_cache_directory=tmp_path / "ocr cache",
        ),
        cuda_available=lambda: False,
    )

    def factory(device: Device, precision: Precision) -> FakeOCRProvider:
        return FakeOCRProvider(
            device,
            precision,
            results_by_timestamp=results_by_timestamp,
            fail_detect=fail_detect,
        )

    runtime.register(_OCR_SPEC, factory)
    return runtime


def _context(tmp_path: Path) -> PrivacyScanContext:
    source = tmp_path / "来源 视频.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "私有 审查"
    frames = workspace / "帧 序列"
    frames.mkdir(parents=True, exist_ok=True)
    samples: list[FrameSample] = []
    for index, timestamp in enumerate((0.2, 0.4, 0.6, 0.8)):
        relative_path = f"帧 序列/帧_{index:02d}.png"
        (workspace / relative_path).write_bytes(b"frame")
        samples.append(
            FrameSample(
                timestamp_seconds=timestamp,
                sample_index=index,
                relative_path=relative_path,
                width=80,
                height=60,
            )
        )
    return PrivacyScanContext(
        input_path=source,
        input_hash="e" * 64,
        duration_seconds=1.0,
        profile=PUBLIC,
        workspace=workspace,
        frame_samples=tuple(samples),
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=0.5,
                duration_seconds=0.5,
                representative_timestamp=0.25,
            ),
            VideoScene(
                scene_index=1,
                start_seconds=0.5,
                end_seconds=1.0,
                duration_seconds=0.5,
                representative_timestamp=0.75,
            ),
        ),
    )


def _config(**updates: object) -> SuspiciousTextConfig:
    return SuspiciousTextConfig.model_validate(
        {
            "provider_id": _PROVIDER_ID,
            "model_id": _MODEL_ID,
            **updates,
        }
    )


def test_scanner_keeps_raw_ocr_text_private(tmp_path: Path) -> None:
    raw_text = "person@example.com"
    runtime = _runtime(
        tmp_path,
        {0.2: ((raw_text, 0.96, (0.1, 0.2, 0.7, 0.35)),)},
    )
    scanner = SuspiciousTextScanner(runtime)

    risk = scanner.scan(_context(tmp_path), _config())[0]

    assert risk.private_evidence[0]["ocr_text"] == raw_text
    assert raw_text not in risk.public_description
    assert raw_text not in str(risk.evidence)
    assert risk.public_description == (
        "OCR proposed a possible email-like text region for manual review."
    )


def test_scanner_filters_isolated_low_confidence_text(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        {0.2: (("person@example.com", 0.42, (0.1, 0.2, 0.7, 0.35)),)},
    )

    risks = SuspiciousTextScanner(runtime).scan(_context(tmp_path), _config())

    assert risks == []


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "text",
    ["person@example.com", "13800138000"],
)
def test_scanner_retains_stable_repeated_low_confidence_text(
    tmp_path: Path,
    text: str,
) -> None:
    box = (0.1, 0.2, 0.7, 0.35)
    runtime = _runtime(
        tmp_path,
        {
            0.2: ((text, 0.55, box),),
            0.4: ((text, 0.58, box),),
        },
    )

    risks = SuspiciousTextScanner(runtime).scan(_context(tmp_path), _config())

    assert len(risks) == 1
    assert risks[0].start_seconds == 0.1
    assert risks[0].end_seconds == 0.5


def test_repeated_low_confidence_requires_adjacent_samples(tmp_path: Path) -> None:
    box = (0.1, 0.2, 0.7, 0.35)
    runtime = _runtime(
        tmp_path,
        {
            0.2: (("person@example.com", 0.55, box),),
            0.4: (("person@example.com", 0.58, box),),
        },
    )
    context = _context(tmp_path)
    samples = list(context.frame_samples)
    samples[1] = samples[1].model_copy(update={"sample_index": 2})
    context = context.model_copy(update={"frame_samples": tuple(samples)})

    risks = SuspiciousTextScanner(runtime).scan(context, _config())

    assert risks == []


def test_same_frame_duplicate_observations_are_aggregated(tmp_path: Path) -> None:
    box = (0.1, 0.2, 0.7, 0.35)
    raw_values = {
        "person@example.com",
        "person@example.com ",
        "Call 13800138000",
    }
    runtime = _runtime(
        tmp_path,
        {
            0.2: (
                ("person@example.com", 0.91, box),
                ("person@example.com ", 0.96, box),
                ("Call 13800138000", 0.95, box),
            )
        },
    )

    risks = SuspiciousTextScanner(runtime).scan(_context(tmp_path), _config())

    assert len(risks) == 1
    private_values = {str(item["ocr_text"]) for item in risks[0].private_evidence}
    assert private_values == raw_values
    public_payload = risks[0].public_description + str(risks[0].evidence)
    assert all(value not in public_payload for value in raw_values)


def test_tracking_is_scene_local_and_uses_text_similarity(tmp_path: Path) -> None:
    box = (0.1, 0.2, 0.7, 0.35)
    runtime = _runtime(
        tmp_path,
        {
            0.2: (("person@example.com", 0.96, box),),
            0.4: (("person@example.con", 0.93, box),),
            0.6: (("person@example.com", 0.95, box),),
            0.8: (("person@example.com", 0.94, box),),
        },
    )

    risks = SuspiciousTextScanner(runtime).scan(_context(tmp_path), _config())

    assert len(risks) == 2
    assert [risk.track_id for risk in risks] == ["text_track_01", "text_track_02"]
    assert [(risk.start_seconds, risk.end_seconds) for risk in risks] == [
        (0.1, 0.5),
        (0.5, 0.9),
    ]
    assert all(risk.risk_type is PrivacyRiskType.SUSPICIOUS_TEXT for risk in risks)


def test_missing_ocr_is_skipped_with_manual_fallback(tmp_path: Path) -> None:
    context = _context(tmp_path)
    for sample in context.frame_samples:
        (context.workspace / sample.relative_path).unlink()
    result = PrivacyScannerRunner((SuspiciousTextScanner(None),)).run(
        context,
        {},
    )

    assert result.executions[0].status is PrivacyScannerStatus.SKIPPED
    assert result.executions[0].fallback == "manual_visual_region"
    assert result.risks == ()


def test_empty_runtime_is_skipped_with_manual_fallback(tmp_path: Path) -> None:
    runtime = ModelRuntimeManager(
        ModelRuntimeConfig(
            device=DevicePreference.CPU,
            disk_cache_directory=tmp_path / "empty ocr cache",
        ),
        cuda_available=lambda: False,
    )

    result = PrivacyScannerRunner((SuspiciousTextScanner(runtime),)).run(
        _context(tmp_path),
        {},
    )

    assert result.executions[0].status is PrivacyScannerStatus.SKIPPED
    assert result.executions[0].fallback == "manual_visual_region"
    assert result.risks == ()


def test_registered_non_ocr_model_is_skipped_with_manual_fallback(
    tmp_path: Path,
) -> None:
    runtime = ModelRuntimeManager(
        ModelRuntimeConfig(
            device=DevicePreference.CPU,
            disk_cache_directory=tmp_path / "non ocr cache",
        ),
        cuda_available=lambda: False,
    )
    runtime.register(
        ModelSpec(
            provider_id=_PROVIDER_ID,
            model_id=_MODEL_ID,
            capabilities=("image_embedding",),
            required_extra="ai",
        ),
        lambda device, precision: FakeOCRProvider(device, precision),
    )

    result = PrivacyScannerRunner((SuspiciousTextScanner(runtime),)).run(
        _context(tmp_path),
        {},
    )

    assert result.executions[0].status is PrivacyScannerStatus.SKIPPED
    assert result.executions[0].fallback == "manual_visual_region"


class _EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _PreservedRiskScanner:
    display_name = "Preserved test risk"
    version = "1.0.0"
    description = "Returns one non-OCR privacy risk."
    requirements = PrivacyScannerRequirements()
    config_model = _EmptyConfig

    def __init__(self, scanner_id: str, risk_type: PrivacyRiskType) -> None:
        self.id = scanner_id
        self._risk_type = risk_type

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        del config
        recommended_style = (
            RedactionStyle.REMOVE_METADATA
            if self._risk_type is PrivacyRiskType.METADATA
            else RedactionStyle.BLUR
        )
        return [
            PrivacyRisk(
                id=make_privacy_risk_id(
                    context.input_hash,
                    self.id,
                    self._risk_type,
                    0.0,
                    context.duration_seconds,
                    None,
                ),
                scanner_id=self.id,
                scanner_version=self.version,
                risk_type=self._risk_type,
                title="Preserved observation",
                public_description="A non-OCR privacy observation was preserved.",
                severity=Severity.MEDIUM,
                confidence=1.0,
                start_seconds=0.0,
                end_seconds=context.duration_seconds,
                recommended_style=recommended_style,
                limitations=("Test-only deterministic observation.",),
                evidence=({"selection": self.id},),
            )
        ]


def test_ocr_failure_does_not_remove_metadata_or_manual_risks(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, {}, fail_detect=True)
    scanners = cast(
        tuple[PrivacyScanner, ...],
        (
            _PreservedRiskScanner("metadata", PrivacyRiskType.METADATA),
            SuspiciousTextScanner(runtime),
            _PreservedRiskScanner("manual", PrivacyRiskType.MANUAL_VISUAL),
        ),
    )
    result = PrivacyScannerRunner(scanners).run(
        _context(tmp_path), {"suspicious_text": _config()}
    )

    assert any(item.risk_type is PrivacyRiskType.METADATA for item in result.risks)
    assert any(item.risk_type is PrivacyRiskType.MANUAL_VISUAL for item in result.risks)
    executions = {item.scanner_id: item for item in result.executions}
    assert executions["suspicious_text"].status is PrivacyScannerStatus.SCANNER_ERROR
    assert "detect failure" not in (executions["suspicious_text"].error_message or "")


def test_scanner_uses_shared_runtime_batching(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        {0.2: (("person@example.com", 0.96, (0.1, 0.2, 0.7, 0.35)),)},
    )
    scanner = SuspiciousTextScanner(runtime)

    scanner.scan(_context(tmp_path), _config())
    scanner.scan(_context(tmp_path), _config())

    records = runtime.records()
    assert len(records) == 2
    assert all(record.operation == "detect_and_recognize" for record in records)
    assert all(record.requested_items == 4 for record in records)
