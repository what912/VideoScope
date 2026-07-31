"""Opt-in integration test for already cached real PaddleOCR models."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from videoscope.ai import (
    Device,
    ModelHealthStatus,
    OCRImageInput,
    Precision,
)
from videoscope.ai.providers import (
    PADDLEOCR_ENGLISH_MODEL_ID,
    PADDLEOCR_ENGLISH_RECOGNITION_MODEL,
    PaddleOCRProvider,
)

pytestmark = pytest.mark.optional


def test_real_paddleocr_provider_with_local_cached_models(
    tmp_path: Path,
) -> None:
    if os.environ.get("VIDEOSCOPE_RUN_PADDLEOCR_TESTS") != "1":
        pytest.skip(
            "set VIDEOSCOPE_RUN_PADDLEOCR_TESTS=1 for local PaddleOCR integration"
        )
    provider = PaddleOCRProvider(
        Device.CPU,
        Precision.FLOAT32,
        language="en",
        model_id=PADDLEOCR_ENGLISH_MODEL_ID,
        recognition_model_name=PADDLEOCR_ENGLISH_RECOGNITION_MODEL,
    )
    health = provider.health()
    if health.status is ModelHealthStatus.ERROR:
        pytest.skip(health.message)
    if not health.local_files_available:
        pytest.skip("PaddleOCR models are not present in the local PaddleX cache")
    image_path = tmp_path / "text.png"
    image = Image.new("RGB", (240, 80), "white")
    ImageDraw.Draw(image).text((20, 25), "VideoScope", fill="black")
    image.save(image_path)

    provider.load()
    result = provider.detect_and_recognize(
        [OCRImageInput(path=image_path, timestamp_seconds=0.0)]
    )

    assert all(item.timestamp_seconds == 0.0 for item in result.observations)
