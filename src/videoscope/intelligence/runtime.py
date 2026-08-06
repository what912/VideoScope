"""Lazy registrations that reuse the shared VideoScope model runtime."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from videoscope.ai import ModelProvider, ModelRuntimeManager, ModelSpec
from videoscope.intelligence.protocols import ASRProvider, ContentIntelligenceProvider
from videoscope.intelligence.providers import (
    FasterWhisperASRProvider,
    OllamaContentIntelligenceProvider,
)


def register_faster_whisper_provider(
    runtime: ModelRuntimeManager,
    *,
    model_id: str = "small",
    download_root: Path | None = None,
    language: str | None = None,
) -> ModelSpec:
    spec = ModelSpec(
        provider_id="faster_whisper",
        model_id=model_id,
        capabilities=("asr",),
        required_extra="asr",
        preprocessing_version="faster-whisper-segment-v1",
    )
    runtime.register(
        spec,
        lambda device, precision: cast(
            ModelProvider,
            FasterWhisperASRProvider(
                model_id=model_id,
                device=device,
                precision=precision,
                download_root=download_root,
                language=language,
            ),
        ),
    )
    return spec


def register_ollama_provider(
    runtime: ModelRuntimeManager,
    *,
    model_id: str,
    endpoint: str = "http://127.0.0.1:11434",
    timeout_seconds: float = 120.0,
) -> ModelSpec:
    spec = ModelSpec(
        provider_id="ollama",
        model_id=model_id,
        capabilities=("content_intelligence",),
        required_extra="local-ai",
        preprocessing_version="grounded-content-request-v1",
    )
    runtime.register(
        spec,
        lambda device, precision: cast(
            ModelProvider,
            OllamaContentIntelligenceProvider(
                model_id=model_id,
                endpoint=endpoint,
                device=device,
                precision=precision,
                timeout_seconds=timeout_seconds,
            ),
        ),
    )
    return spec


def get_asr_provider(
    runtime: ModelRuntimeManager, provider_id: str, model_id: str
) -> ASRProvider:
    provider = runtime.get_provider(provider_id, model_id)
    if not isinstance(provider, ASRProvider):
        raise TypeError("registered provider does not implement ASRProvider")
    return provider


def get_content_intelligence_provider(
    runtime: ModelRuntimeManager, provider_id: str, model_id: str
) -> ContentIntelligenceProvider:
    provider = runtime.get_provider(provider_id, model_id)
    if not isinstance(provider, ContentIntelligenceProvider):
        raise TypeError(
            "registered provider does not implement ContentIntelligenceProvider"
        )
    return provider


__all__ = [
    "get_asr_provider",
    "get_content_intelligence_provider",
    "register_faster_whisper_provider",
    "register_ollama_provider",
]
