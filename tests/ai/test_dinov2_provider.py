"""Offline contract tests for the lazy optional DINOv2 provider."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from videoscope.ai import Device, ModelHealthStatus, Precision
from videoscope.ai.providers import (
    DEFAULT_DINOV2_MODEL_ID,
    DEFAULT_DINOV2_MODEL_NAME,
    DINOv2EmbeddingProvider,
)


def test_provider_construction_does_not_import_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []

    def record_import(name: str, package: str | None = None) -> object:
        del package
        imported.append(name)
        raise AssertionError("construction must not import optional packages")

    monkeypatch.setattr(
        "videoscope.ai.providers.dinov2.importlib.import_module",
        record_import,
    )

    provider = DINOv2EmbeddingProvider(Device.CPU, Precision.FLOAT32)

    assert provider.model_id == DEFAULT_DINOV2_MODEL_ID
    assert imported == []


def test_missing_dinov2_runtime_reports_actionable_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "videoscope.ai.providers.dinov2.importlib.util.find_spec",
        lambda name: None,
    )
    provider = DINOv2EmbeddingProvider(Device.CPU, Precision.FLOAT32)

    health = provider.health()

    assert health.status is ModelHealthStatus.ERROR
    assert health.local_files_available is False
    assert 'pip install "genvideoscope[ai]"' in health.message
    assert "--enable-ai" in health.message


def test_health_requires_both_cached_repository_and_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub_directory = tmp_path / "torch hub"
    repository = hub_directory / "facebookresearch_dinov2_main"
    repository.mkdir(parents=True)
    (repository / "hubconf.py").write_text("# local", encoding="utf-8")
    checkpoint = (
        hub_directory / "checkpoints" / f"{DEFAULT_DINOV2_MODEL_NAME}_pretrain.pth"
    )
    checkpoint.parent.mkdir()
    checkpoint.write_bytes(b"cached")
    fake_torch = SimpleNamespace(
        hub=SimpleNamespace(get_dir=lambda: str(hub_directory))
    )
    provider = DINOv2EmbeddingProvider(
        Device.CPU,
        Precision.FLOAT32,
        hub_directory=hub_directory,
    )
    monkeypatch.setattr(provider, "_packages_available", lambda: True)
    monkeypatch.setattr(
        provider,
        "_import_runtime",
        lambda: (fake_torch, object()),
    )

    health = provider.health()

    assert health.status is ModelHealthStatus.UNLOADED
    assert health.local_files_available is True

    checkpoint.unlink()
    missing_health = provider.health()
    assert missing_health.local_files_available is False
