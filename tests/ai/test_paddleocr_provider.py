"""Offline contract tests for the optional lazy PaddleOCR provider."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from videoscope.ai import (
    Device,
    ModelHealthStatus,
    OCRImageInput,
    Precision,
)
from videoscope.ai.providers import (
    PADDLEOCR_CHINESE_MODEL_ID,
    PaddleOCRProvider,
)


def _provider(tmp_path: Path) -> PaddleOCRProvider:
    return PaddleOCRProvider(
        Device.CPU,
        Precision.FLOAT32,
        language="ch",
        model_id=PADDLEOCR_CHINESE_MODEL_ID,
        recognition_model_name="PP-OCRv5_mobile_rec",
        cache_home=tmp_path / "paddlex",
    )


def test_construction_and_health_do_not_import_optional_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []

    def record_import(name: str, package: str | None = None) -> object:
        del package
        imported.append(name)
        raise AssertionError("health must not import optional OCR packages")

    monkeypatch.setattr(importlib, "import_module", record_import)
    provider = _provider(tmp_path)
    monkeypatch.setattr(provider, "_packages_available", lambda: False)

    health = provider.health()

    assert imported == []
    assert health.status is ModelHealthStatus.ERROR
    assert 'pip install "genvideoscope[ocr]"' in health.message
    assert "--enable-ocr" in health.message


def test_health_requires_detection_and_language_model_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider(tmp_path)
    monkeypatch.setattr(provider, "_packages_available", lambda: True)
    model_root = tmp_path / "paddlex" / "official_models"
    for model_name in ("PP-OCRv5_mobile_det", "PP-OCRv5_mobile_rec"):
        model_directory = model_root / model_name
        model_directory.mkdir(parents=True)
        (model_directory / "model.pdparams").write_bytes(b"cached")

    health = provider.health()

    assert health.status is ModelHealthStatus.UNLOADED
    assert health.local_files_available is True
    (model_root / "PP-OCRv5_mobile_rec" / "model.pdparams").unlink()
    assert provider.health().local_files_available is False


def test_result_is_timestamped_and_box_is_normalized(
    tmp_path: Path,
) -> None:
    provider = _provider(tmp_path)
    image_path = tmp_path / "中文 frame.png"
    Image.new("RGB", (200, 100), "white").save(image_path)
    provider._pipeline = SimpleNamespace(
        predict=lambda paths: [
            SimpleNamespace(
                json={
                    "res": {
                        "rec_texts": ["测试"],
                        "rec_scores": [0.93],
                        "rec_boxes": [[20, 50, 180, 90]],
                    }
                }
            )
            for _ in paths
        ]
    )

    result = provider.detect_and_recognize(
        [OCRImageInput(path=image_path, timestamp_seconds=1.25)]
    )

    assert len(result.observations) == 1
    observation = result.observations[0]
    assert observation.text == "测试"
    assert observation.timestamp_seconds == 1.25
    assert observation.bounding_box.model_dump() == {
        "x_min": 0.1,
        "y_min": 0.5,
        "x_max": 0.9,
        "y_max": 0.9,
    }
