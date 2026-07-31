"""Offline contract tests for the lazy optional OpenCLIP provider."""

from __future__ import annotations

import pytest

from videoscope.ai import Device, ModelHealthStatus, Precision
from videoscope.ai.providers import (
    DEFAULT_OPENCLIP_MODEL_ID,
    OpenCLIPEmbeddingProvider,
)


def test_provider_construction_does_not_import_openclip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []

    def record_import(name: str, package: str | None = None) -> object:
        del package
        imported.append(name)
        raise AssertionError("construction must not import optional packages")

    monkeypatch.setattr(
        "videoscope.ai.providers.openclip.importlib.import_module",
        record_import,
    )

    provider = OpenCLIPEmbeddingProvider(Device.CPU, Precision.FLOAT32)

    assert provider.model_id == DEFAULT_OPENCLIP_MODEL_ID
    assert imported == []


def test_missing_openclip_reports_actionable_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "videoscope.ai.providers.openclip.importlib.util.find_spec",
        lambda name: None,
    )
    provider = OpenCLIPEmbeddingProvider(Device.CPU, Precision.FLOAT32)

    health = provider.health()

    assert health.status is ModelHealthStatus.ERROR
    assert health.local_files_available is False
    assert 'pip install "genvideoscope[ai]"' in health.message
    assert "--enable-ai" in health.message
