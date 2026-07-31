"""Opt-in integration test for an already cached real DINOv2 model."""

from __future__ import annotations

import os

import pytest

from videoscope.ai import Device, ModelHealthStatus, Precision
from videoscope.ai.providers import DINOv2EmbeddingProvider

pytestmark = pytest.mark.optional


def test_cached_dinov2_provider_can_load_and_unload() -> None:
    if os.environ.get("VIDEOSCOPE_RUN_DINOV2_TESTS") != "1":
        pytest.skip("set VIDEOSCOPE_RUN_DINOV2_TESTS=1 for local DINOv2 integration")
    provider = DINOv2EmbeddingProvider(Device.CPU, Precision.FLOAT32)
    health = provider.health()
    if health.status is ModelHealthStatus.ERROR or not health.local_files_available:
        pytest.skip("default DINOv2 model is not present in the local cache")

    provider.load()

    assert provider.health().status is ModelHealthStatus.READY
    provider.unload()
