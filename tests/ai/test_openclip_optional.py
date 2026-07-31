"""Opt-in integration test for an already cached real OpenCLIP model."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

from videoscope.ai import Device, ModelHealthStatus, Precision
from videoscope.ai.providers import OpenCLIPEmbeddingProvider

pytestmark = pytest.mark.optional


def test_real_openclip_provider_with_local_cached_weights(tmp_path: Path) -> None:
    if os.environ.get("VIDEOSCOPE_RUN_OPENCLIP_TESTS") != "1":
        pytest.skip(
            "set VIDEOSCOPE_RUN_OPENCLIP_TESTS=1 for local OpenCLIP integration"
        )
    provider = OpenCLIPEmbeddingProvider(Device.CPU, Precision.FLOAT32)
    health = provider.health()
    if health.status is ModelHealthStatus.ERROR:
        pytest.skip(health.message)
    if not health.local_files_available:
        pytest.skip("default OpenCLIP weights are not present in the local cache")
    image_path = tmp_path / "sample.png"
    Image.new("RGB", (32, 32), color=(200, 20, 20)).save(image_path)

    provider.load()
    image = provider.encode_images([str(image_path)])
    text = provider.encode_text(["a red square"])

    assert image.embeddings.shape[0] == 1
    assert text.embeddings.shape == image.embeddings.shape
