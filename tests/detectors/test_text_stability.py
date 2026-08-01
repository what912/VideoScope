"""Offline FakeOCR tests for scene-local temporal text stability."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias

from videoscope.ai import (
    MODEL_RUNTIME_CACHE_KEY,
    Device,
    DevicePreference,
    FakeOCRProvider,
    ModelRuntimeConfig,
    ModelRuntimeManager,
    ModelSpec,
    Precision,
)
from videoscope.detectors import (
    DETECTOR_DIAGNOSTICS_CACHE_KEY,
    AnalysisContext,
    DetectorRegistry,
    DetectorRunner,
    DetectorRunResult,
    TextStabilityDetector,
)
from videoscope.domain import DetectorStatus
from videoscope.scenes import VideoScene

from .helpers import make_image_context

PROVIDER_ID = "fake_ocr"
MODEL_ID = "fake-ocr-v1"
BOX = (0.1, 0.65, 0.9, 0.9)
OCRResult: TypeAlias = tuple[
    str,
    float,
    tuple[float, float, float, float],
]
OCRResults: TypeAlias = Mapping[float, tuple[OCRResult, ...]]


def _scene(index: int, start: float, end: float) -> VideoScene:
    return VideoScene(
        scene_index=index,
        start_seconds=start,
        end_seconds=end,
        duration_seconds=end - start,
        representative_timestamp=start + (end - start) / 2,
    )


def _context(
    tmp_path: Path,
    labels: OCRResults,
    *,
    scenes: tuple[VideoScene, ...] | None = None,
    fail_detect: bool = False,
) -> tuple[AnalysisContext, list[FakeOCRProvider]]:
    context = make_image_context(
        tmp_path,
        [20] * 12,
        duration_seconds=6.0,
        scenes=scenes or (_scene(0, 0.0, 6.0),),
    )
    providers: list[FakeOCRProvider] = []
    runtime = ModelRuntimeManager(
        ModelRuntimeConfig(
            device=DevicePreference.CPU,
            batch_size=4,
            disk_cache_directory=tmp_path / "ocr cache",
        ),
        cuda_available=lambda: False,
    )

    def factory(device: Device, precision: Precision) -> FakeOCRProvider:
        provider = FakeOCRProvider(
            device,
            precision,
            model_id=MODEL_ID,
            results_by_timestamp=labels,
            fail_detect=fail_detect,
        )
        providers.append(provider)
        return provider

    runtime.register(
        ModelSpec(
            provider_id=PROVIDER_ID,
            model_id=MODEL_ID,
            capabilities=("ocr",),
            required_extra="ocr",
        ),
        factory,
    )
    return (
        context.model_copy(update={"shared_cache": {MODEL_RUNTIME_CACHE_KEY: runtime}}),
        providers,
    )


def _configuration() -> dict[str, object]:
    return {
        "provider_id": PROVIDER_ID,
        "model_id": MODEL_ID,
        "language": "en",
        "minimum_ocr_confidence": 0.65,
    }


def _run(context: AnalysisContext) -> DetectorRunResult:
    return DetectorRunner(DetectorRegistry([TextStabilityDetector()])).run(
        context,
        configurations={"text_stability": _configuration()},
    )


def _stable_labels(text: str = "VIDEO SCOPE") -> dict[float, tuple[OCRResult, ...]]:
    return {index / 2: ((text, 0.95, BOX),) for index in range(12)}


def test_stable_text_has_no_finding_and_provider_is_batched(
    tmp_path: Path,
) -> None:
    context, providers = _context(tmp_path, _stable_labels())

    result = _run(context)

    assert result.findings == ()
    assert result.executions[0].status is DetectorStatus.OK
    assert len(providers) == 1
    assert providers[0].load_count == 1
    assert providers[0].detect_calls == 3


def test_frequent_same_region_changes_produce_timestamped_ocr_evidence(
    tmp_path: Path,
) -> None:
    labels = _stable_labels()
    for index, text in zip(
        range(4, 9),
        ("VIDE0 SCOPE", "VIDEO SCOPE", "V1DEO SCOPE", "VIDEO SCOPE", "VIDEO SC0PE"),
        strict=True,
    ):
        labels[index / 2] = ((text, 0.9, BOX),)
    context, _ = _context(tmp_path, labels)

    result = _run(context)

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.title == "Potential temporal text instability"
    assert 1.5 <= finding.time_range.start_seconds <= 2.0
    assert 4.0 <= finding.time_range.end_seconds <= 4.5
    assert len(finding.evidence) == 2
    assert all("bounding_box" in item.metadata for item in finding.evidence)
    assert all("text_edit_distance" in item.metadata for item in finding.evidence)
    assert any("OCR recognition errors" in item for item in finding.limitations)
    diagnostics = context.shared_cache[DETECTOR_DIAGNOSTICS_CACHE_KEY]
    assert diagnostics["text_stability"]["candidate_count"] == 1


def test_brief_disappearance_and_text_flash_are_detected(
    tmp_path: Path,
) -> None:
    disappearance = _stable_labels()
    disappearance.pop(2.5)
    context, _ = _context(tmp_path, disappearance)
    disappearance_result = _run(context)

    flash_context, _ = _context(
        tmp_path / "flash",
        {2.5: (("FLASH", 0.96, BOX),)},
    )
    flash_result = _run(flash_context)

    assert disappearance_result.findings[0].tags[-1] == "brief_disappearance"
    assert flash_result.findings[0].tags[-1] == "brief_text_flash"
    assert len(flash_result.findings[0].evidence) == 3


def test_normal_subtitle_switch_scene_cut_and_low_confidence_are_excluded(
    tmp_path: Path,
) -> None:
    subtitle_switch = {
        index / 2: (("FIRST SUBTITLE" if index < 6 else "SECOND SUBTITLE", 0.95, BOX),)
        for index in range(12)
    }
    switch_context, _ = _context(tmp_path, subtitle_switch)
    cut_context, _ = _context(
        tmp_path / "cut",
        subtitle_switch,
        scenes=(_scene(0, 0.0, 3.0), _scene(1, 3.0, 6.0)),
    )
    low_context, _ = _context(
        tmp_path / "low",
        {2.5: (("NOISY", 0.2, BOX),)},
    )

    assert _run(switch_context).findings == ()
    assert _run(cut_context).findings == ()
    assert _run(low_context).findings == ()


def test_reasonable_monotonic_scrolling_and_provider_failure_are_isolated(
    tmp_path: Path,
) -> None:
    scrolling = {
        index / 2: (
            (
                "ROLLING CREDIT",
                0.95,
                (0.1, 0.05 + index * 0.04, 0.9, 0.35 + index * 0.04),
            ),
        )
        for index in range(12)
    }
    scrolling_context, _ = _context(tmp_path, scrolling)
    failed_context, _ = _context(
        tmp_path / "failed",
        _stable_labels(),
        fail_detect=True,
    )

    assert _run(scrolling_context).findings == ()
    failed = _run(failed_context)
    assert failed.findings == ()
    assert failed.executions[0].status is DetectorStatus.DETECTOR_ERROR
    assert "fake OCR provider detect failure" not in (
        failed.executions[0].error_message or ""
    )
