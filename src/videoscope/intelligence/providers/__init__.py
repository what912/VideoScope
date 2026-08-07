"""Lazy optional providers for Advanced AI."""

from videoscope.intelligence.providers.faster_whisper import (
    FasterWhisperASRProvider,
    FasterWhisperUnavailableError,
)
from videoscope.intelligence.providers.ollama import (
    OllamaContentIntelligenceProvider,
    OllamaUnavailableError,
)

__all__ = [
    "FasterWhisperASRProvider",
    "FasterWhisperUnavailableError",
    "OllamaContentIntelligenceProvider",
    "OllamaUnavailableError",
]
