"""Protocols implemented by optional local or remote model providers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from videoscope.ai.models import (
    Device,
    EmbeddingBatch,
    ModelHealth,
    OCRBatch,
    OCRImageInput,
    Precision,
)


@runtime_checkable
class ModelProvider(Protocol):
    """Lifecycle and identity contract shared by every AI provider."""

    @property
    def provider_id(self) -> str:
        """Stable provider implementation ID."""
        ...

    @property
    def model_id(self) -> str:
        """Stable model/checkpoint ID."""
        ...

    @property
    def device(self) -> Device:
        """Resolved execution device."""
        ...

    @property
    def precision(self) -> Precision:
        """Resolved inference precision."""
        ...

    def load(self) -> None:
        """Load local model resources after runtime policy approval."""
        ...

    def unload(self) -> None:
        """Release loaded resources."""
        ...

    def health(self) -> ModelHealth:
        """Report state without implicitly downloading or loading a model."""
        ...


@runtime_checkable
class EmbeddingProvider(ModelProvider, Protocol):
    """Batched image/text embedding provider with a NumPy output contract."""

    def encode_images(self, image_paths: Sequence[str]) -> EmbeddingBatch:
        """Encode a batch of local image paths."""
        ...

    def encode_text(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Encode a batch of text values."""
        ...


@runtime_checkable
class OCRProvider(ModelProvider, Protocol):
    """Batched local OCR provider with normalized, timestamped results."""

    def detect_and_recognize(
        self,
        images: Sequence[OCRImageInput],
    ) -> OCRBatch:
        """Detect and recognize text in a batch of local sampled frames."""
        ...
