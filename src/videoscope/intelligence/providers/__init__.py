"""Lazy optional providers for Advanced AI."""

from videoscope.intelligence.providers.faster_whisper import (
    FasterWhisperASRProvider,
    FasterWhisperUnavailableError,
)
from videoscope.intelligence.providers.ollama import (
    OllamaContentIntelligenceProvider,
    OllamaUnavailableError,
)
from videoscope.intelligence.providers.openai_compatible import (
    OpenAICompatibleContentIntelligenceProvider,
    OpenAICompatibleUnavailableError,
)

__all__ = [
    "FasterWhisperASRProvider",
    "FasterWhisperUnavailableError",
    "OllamaContentIntelligenceProvider",
    "OllamaUnavailableError",
    "OpenAICompatibleContentIntelligenceProvider",
    "OpenAICompatibleUnavailableError",
]
