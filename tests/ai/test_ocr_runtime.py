"""Offline tests for shared, lazy, batched OCR execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoscope.ai import (
    Device,
    DevicePreference,
    FakeOCRProvider,
    ModelProviderExecutionError,
    ModelRuntimeConfig,
    ModelRuntimeManager,
    ModelSpec,
    OCRImageInput,
    Precision,
)

OCR_SPEC = ModelSpec(
    provider_id="fake_ocr",
    model_id="fake-ocr-v1",
    capabilities=("ocr",),
    required_extra="ocr",
)


def _runtime(
    tmp_path: Path,
    *,
    fail_detect: bool = False,
) -> tuple[ModelRuntimeManager, list[FakeOCRProvider]]:
    providers: list[FakeOCRProvider] = []
    runtime = ModelRuntimeManager(
        ModelRuntimeConfig(
            device=DevicePreference.CPU,
            batch_size=2,
            disk_cache_directory=tmp_path / "ocr cache",
        ),
        cuda_available=lambda: False,
    )

    def factory(device: Device, precision: Precision) -> FakeOCRProvider:
        provider = FakeOCRProvider(
            device,
            precision,
            results_by_timestamp={
                0.0: (("Hello", 0.95, (0.1, 0.6, 0.8, 0.9)),),
                1.0: (("World", 0.91, (0.1, 0.6, 0.8, 0.9)),),
            },
            fail_detect=fail_detect,
        )
        providers.append(provider)
        return provider

    runtime.register(OCR_SPEC, factory)
    return runtime, providers


def _inputs(tmp_path: Path) -> tuple[OCRImageInput, ...]:
    inputs: list[OCRImageInput] = []
    for index in range(3):
        path = tmp_path / "中文 frames" / f"{index}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"frame")
        inputs.append(OCRImageInput(path=path, timestamp_seconds=float(index)))
    return tuple(inputs)


def test_ocr_provider_is_lazy_singleton_and_runtime_batches(tmp_path: Path) -> None:
    runtime, providers = _runtime(tmp_path)

    assert providers == []
    result = runtime.detect_and_recognize(
        "fake_ocr",
        "fake-ocr-v1",
        _inputs(tmp_path),
    )
    runtime.detect_and_recognize(
        "fake_ocr",
        "fake-ocr-v1",
        _inputs(tmp_path),
    )

    assert [item.text for item in result.observations] == ["Hello", "World"]
    assert len(providers) == 1
    assert providers[0].load_count == 1
    assert providers[0].detect_calls == 4
    assert runtime.records()[0].batch_size == 2
    assert runtime.records()[0].encoded_items == 3


def test_ocr_provider_failure_is_recorded_and_sanitized(tmp_path: Path) -> None:
    runtime, _ = _runtime(tmp_path, fail_detect=True)

    with pytest.raises(ModelProviderExecutionError) as captured:
        runtime.detect_and_recognize(
            "fake_ocr",
            "fake-ocr-v1",
            _inputs(tmp_path),
        )

    assert str(tmp_path) not in str(captured.value)
    assert runtime.records()[0].status.value == "error"
    assert runtime.records()[0].error_type == "RuntimeError"
