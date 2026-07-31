"""Deterministic, offline embedding provider used only by tests."""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from videoscope.ai.models import (
    Device,
    EmbeddingBatch,
    FloatArray,
    ModelHealth,
    ModelHealthStatus,
    NormalizedBoundingBox,
    OCRBatch,
    OCRImageInput,
    OCRObservation,
    Precision,
)

np: Any = importlib.import_module("numpy")


class FakeEmbeddingProvider:
    """Small provider with no network, GPU, model, or random dependency."""

    provider_id = "fake"

    def __init__(
        self,
        device: Device,
        precision: Precision,
        *,
        model_id: str = "fake-embedding-v1",
        dimension: int = 8,
        local_files_available: bool = True,
        fail_load: bool = False,
        fail_encode: bool = False,
        payload_vectors: Mapping[bytes, Sequence[float]] | None = None,
    ) -> None:
        if dimension < 1:
            raise ValueError("dimension must be positive")
        self.model_id = model_id
        self.device = device
        self.precision = precision
        self.dimension = dimension
        self.local_files_available = local_files_available
        self.fail_load = fail_load
        self.fail_encode = fail_encode
        self.payload_vectors = dict(payload_vectors or {})
        self.loaded = False
        self.load_count = 0
        self.unload_count = 0
        self.image_encode_calls = 0
        self.text_encode_calls = 0

    def load(self) -> None:
        """Simulate local load, optionally injecting a failure."""
        self.load_count += 1
        if self.fail_load:
            raise RuntimeError("fake provider load failure")
        self.local_files_available = True
        self.loaded = True

    def unload(self) -> None:
        """Release the fake loaded marker."""
        self.unload_count += 1
        self.loaded = False

    def health(self) -> ModelHealth:
        """Return state without performing work."""
        return ModelHealth(
            status=(
                ModelHealthStatus.READY if self.loaded else ModelHealthStatus.UNLOADED
            ),
            local_files_available=self.local_files_available,
            message=(
                "Fake provider is ready."
                if self.loaded
                else "Fake provider is not loaded."
            ),
        )

    def _vector(self, payload: bytes) -> FloatArray:
        override = self.payload_vectors.get(payload)
        if override is not None:
            values = np.asarray(override, dtype=np.float32)
            if values.shape != (self.dimension,):
                raise ValueError(
                    "fake embedding override does not match provider dimension"
                )
            norm = float(np.linalg.norm(values))
            if norm <= 0:
                raise ValueError("fake embedding override must have a non-zero norm")
            return values / np.float32(norm)
        digest = np.frombuffer(hashlib.sha256(payload).digest(), dtype=np.uint8)
        values = np.resize(digest, self.dimension).astype(np.float32)
        values = (values - np.float32(127.5)) / np.float32(127.5)
        norm = float(np.linalg.norm(values))
        if norm > 0:
            values /= np.float32(norm)
        return values

    def _require_ready(self) -> None:
        if not self.loaded:
            raise RuntimeError("fake provider is not loaded")
        if self.fail_encode:
            raise RuntimeError("fake provider encode failure")

    def encode_images(self, image_paths: Sequence[str]) -> EmbeddingBatch:
        """Encode file bytes deterministically without decoding images."""
        self._require_ready()
        self.image_encode_calls += 1
        embeddings = np.stack(
            [self._vector(Path(path).read_bytes()) for path in image_paths]
        )
        return EmbeddingBatch(
            embeddings.astype(np.float32, copy=False),
            {
                "fake": True,
                "operation": "encode_images",
                "provider_batch_size": len(image_paths),
            },
        )

    def encode_text(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Encode UTF-8 text deterministically."""
        self._require_ready()
        self.text_encode_calls += 1
        embeddings = np.stack([self._vector(text.encode("utf-8")) for text in texts])
        return EmbeddingBatch(
            embeddings.astype(np.float32, copy=False),
            {
                "fake": True,
                "operation": "encode_text",
                "provider_batch_size": len(texts),
            },
        )


FakeOCRResult = tuple[str, float, tuple[float, float, float, float]]


class FakeOCRProvider:
    """Controllable local OCR provider used by offline detector tests."""

    provider_id = "fake_ocr"

    def __init__(
        self,
        device: Device,
        precision: Precision,
        *,
        model_id: str = "fake-ocr-v1",
        results_by_timestamp: Mapping[float, Sequence[FakeOCRResult]] | None = None,
        local_files_available: bool = True,
        fail_load: bool = False,
        fail_detect: bool = False,
    ) -> None:
        self.model_id = model_id
        self.device = device
        self.precision = precision
        self.results_by_timestamp = {
            float(timestamp): tuple(results)
            for timestamp, results in (results_by_timestamp or {}).items()
        }
        self.local_files_available = local_files_available
        self.fail_load = fail_load
        self.fail_detect = fail_detect
        self.loaded = False
        self.load_count = 0
        self.unload_count = 0
        self.detect_calls = 0
        self.detected_images = 0

    def load(self) -> None:
        """Simulate provider loading without a model or network."""
        self.load_count += 1
        if self.fail_load:
            raise RuntimeError("fake OCR provider load failure")
        self.local_files_available = True
        self.loaded = True

    def unload(self) -> None:
        """Release the fake provider state."""
        self.unload_count += 1
        self.loaded = False

    def health(self) -> ModelHealth:
        """Return deterministic lifecycle state."""
        return ModelHealth(
            status=(
                ModelHealthStatus.READY if self.loaded else ModelHealthStatus.UNLOADED
            ),
            local_files_available=self.local_files_available,
            message=(
                "Fake OCR provider is ready."
                if self.loaded
                else "Fake OCR provider is not loaded."
            ),
        )

    def detect_and_recognize(
        self,
        images: Sequence[OCRImageInput],
    ) -> OCRBatch:
        """Return configured observations for exact frame timestamps."""
        if not self.loaded:
            raise RuntimeError("fake OCR provider is not loaded")
        if self.fail_detect:
            raise RuntimeError("fake OCR provider detect failure")
        self.detect_calls += 1
        self.detected_images += len(images)
        observations = tuple(
            OCRObservation(
                text=text,
                confidence=confidence,
                bounding_box=NormalizedBoundingBox(
                    x_min=box[0],
                    y_min=box[1],
                    x_max=box[2],
                    y_max=box[3],
                ),
                timestamp_seconds=image.timestamp_seconds,
            )
            for image in images
            for text, confidence, box in self.results_by_timestamp.get(
                image.timestamp_seconds,
                (),
            )
        )
        return OCRBatch(
            observations=observations,
            metadata={
                "fake": True,
                "operation": "detect_and_recognize",
                "provider_batch_size": len(images),
            },
        )
