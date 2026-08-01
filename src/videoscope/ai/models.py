"""Vendor-neutral models for optional AI providers and shared embeddings."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

np: Any = importlib.import_module("numpy")
FloatArray: TypeAlias = Any


class AIModel(BaseModel):
    """Strict base model for public AI runtime configuration and records."""

    model_config = ConfigDict(extra="forbid")


class Device(StrEnum):
    """Resolved execution device exposed to providers."""

    CPU = "cpu"
    CUDA = "cuda"


class DevicePreference(StrEnum):
    """User-selected device policy."""

    AUTO = "auto"
    CPU = "cpu"
    CUDA = "cuda"


class Precision(StrEnum):
    """Requested model arithmetic precision."""

    FLOAT32 = "float32"
    FLOAT16 = "float16"
    BFLOAT16 = "bfloat16"


class ModelHealthStatus(StrEnum):
    """Provider lifecycle health reported without forcing a load."""

    UNLOADED = "unloaded"
    READY = "ready"
    ERROR = "error"


class ModelRunStatus(StrEnum):
    """Outcome of one batched runtime call."""

    OK = "ok"
    ERROR = "error"


class ModelRuntimeConfig(AIModel):
    """Shared runtime policy independent of any concrete model package."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    device: DevicePreference = DevicePreference.AUTO
    precision: Precision = Precision.FLOAT32
    batch_size: int = Field(default=16, ge=1, le=4096)
    memory_budget_bytes: int = Field(
        default=256 * 1024 * 1024,
        ge=0,
    )
    disk_cache_directory: Path | None = None
    allow_model_download: bool = False
    interactive: bool = False


class ModelHealth(AIModel):
    """Provider state and local-weight availability."""

    status: ModelHealthStatus
    local_files_available: bool
    message: str


class ModelSpec(AIModel):
    """Lazy provider registration visible to ``videoscope models list``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str
    model_id: str
    capabilities: tuple[str, ...] = ("image_embedding", "text_embedding")
    required_extra: str = "ai"
    preprocessing_version: str = "1"

    @field_validator(
        "provider_id",
        "model_id",
        "required_extra",
        "preprocessing_version",
    )
    @classmethod
    def require_nonblank(cls, value: str) -> str:
        """Normalize stable identifiers and reject blank values."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("model identifiers must not be blank")
        return normalized

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """Store a deterministic, non-empty capability declaration."""
        normalized = tuple(sorted({value.strip() for value in values if value.strip()}))
        if not normalized:
            raise ValueError("at least one model capability is required")
        return normalized


class ImageEmbeddingInput(AIModel):
    """One sampled frame plus the identity needed for deterministic caching."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    video_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    preprocessing_version: str

    @field_validator("preprocessing_version")
    @classmethod
    def require_preprocessing_version(cls, value: str) -> str:
        """Require an explicit preprocessing contract version."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("preprocessing_version must not be blank")
        return normalized


class OCRImageInput(AIModel):
    """One local sampled frame submitted to an OCR provider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)


class NormalizedBoundingBox(AIModel):
    """Axis-aligned text box normalized to the inclusive image unit square."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x_min: float = Field(ge=0, le=1, allow_inf_nan=False)
    y_min: float = Field(ge=0, le=1, allow_inf_nan=False)
    x_max: float = Field(ge=0, le=1, allow_inf_nan=False)
    y_max: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_area(self) -> NormalizedBoundingBox:
        """Require a non-empty normalized rectangle."""
        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise ValueError("normalized bounding box must have positive area")
        return self


class OCRObservation(AIModel):
    """Recognized text anchored to one sampled-frame timestamp and box."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    bounding_box: NormalizedBoundingBox
    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)


@dataclass(frozen=True, slots=True)
class OCRBatch:
    """Normalized OCR observations plus JSON-compatible provider metadata."""

    observations: tuple[OCRObservation, ...]
    metadata: dict[str, Any]


class EmbeddingCacheKey(AIModel):
    """Stable identity of one frame embedding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    provider_id: str
    model_id: str
    preprocessing_version: str

    @field_validator("provider_id", "model_id", "preprocessing_version")
    @classmethod
    def require_key_component(cls, value: str) -> str:
        """Reject ambiguous empty cache-key components."""
        normalized = value.strip()
        if not normalized:
            raise ValueError("cache key components must not be blank")
        return normalized

    def canonical_payload(self) -> bytes:
        """Return deterministic UTF-8 bytes without locale-sensitive floats."""
        document = {
            "model_id": self.model_id,
            "preprocessing_version": self.preprocessing_version,
            "provider_id": self.provider_id,
            "timestamp_hex": self.timestamp_seconds.hex(),
            "video_hash": self.video_hash,
        }
        return json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        """Return the cache filename digest."""
        return hashlib.sha256(self.canonical_payload()).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddingBatch:
    """Standard NumPy embeddings plus JSON-compatible provider metadata."""

    embeddings: FloatArray
    metadata: dict[str, Any]

    def __post_init__(self) -> None:
        """Validate the cross-provider array contract."""
        if self.embeddings.ndim != 2:
            raise ValueError("embeddings must be a two-dimensional array")
        if not np.issubdtype(self.embeddings.dtype, np.floating):
            raise ValueError("embeddings must use a floating-point dtype")
        if not np.isfinite(self.embeddings).all():
            raise ValueError("embeddings must contain only finite values")


class ModelRunRecord(AIModel):
    """One model call recorded for reports and performance inspection."""

    provider_id: str
    model_id: str
    operation: str
    status: ModelRunStatus
    device: str
    precision: str
    batch_size: int = Field(ge=1)
    requested_items: int = Field(ge=0)
    encoded_items: int = Field(ge=0)
    inference_seconds: float = Field(ge=0, allow_inf_nan=False)
    cache_hits: int = Field(ge=0)
    cache_misses: int = Field(ge=0)
    cache_hit_rate: float = Field(ge=0, le=1, allow_inf_nan=False)
    error_type: str | None = None

    @classmethod
    def from_counts(
        cls,
        *,
        provider_id: str,
        model_id: str,
        operation: str,
        status: ModelRunStatus,
        device: str,
        precision: str,
        batch_size: int,
        requested_items: int,
        encoded_items: int,
        inference_seconds: float,
        cache_hits: int,
        cache_misses: int,
        error_type: str | None = None,
    ) -> ModelRunRecord:
        """Create a record with a deterministic zero-safe hit rate."""
        denominator = cache_hits + cache_misses
        hit_rate = 0.0 if denominator == 0 else cache_hits / denominator
        if not math.isfinite(hit_rate):
            hit_rate = 0.0
        return cls(
            provider_id=provider_id,
            model_id=model_id,
            operation=operation,
            status=status,
            device=device,
            precision=precision,
            batch_size=batch_size,
            requested_items=requested_items,
            encoded_items=encoded_items,
            inference_seconds=inference_seconds,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            cache_hit_rate=hit_rate,
            error_type=error_type,
        )
