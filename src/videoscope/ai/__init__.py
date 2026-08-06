"""Optional AI runtime with no eager heavy-package imports."""

from videoscope.ai.cache import (
    CachedEmbedding,
    CacheLookup,
    CacheSource,
    EmbeddingCache,
    EmbeddingCacheStats,
)
from videoscope.ai.fake import FakeEmbeddingProvider, FakeOCRProvider
from videoscope.ai.models import (
    Device,
    DevicePreference,
    EmbeddingBatch,
    EmbeddingCacheKey,
    ImageEmbeddingInput,
    ModelHealth,
    ModelHealthStatus,
    ModelRunRecord,
    ModelRunStatus,
    ModelRuntimeConfig,
    ModelSpec,
    NormalizedBoundingBox,
    OCRBatch,
    OCRImageInput,
    OCRObservation,
    Precision,
)
from videoscope.ai.ocr import OCRRuntimeUnavailableError, detect_with_optional_ocr
from videoscope.ai.protocols import EmbeddingProvider, ModelProvider, OCRProvider
from videoscope.ai.runtime import (
    DuplicateModelRegistrationError,
    ModelDeviceUnavailableError,
    ModelDownloadPermissionError,
    ModelProviderExecutionError,
    ModelRuntimeError,
    ModelRuntimeManager,
    UnknownModelError,
    create_model_runtime,
    default_embedding_cache_directory,
)

MODEL_RUNTIME_CACHE_KEY = "model_runtime"

__all__ = [
    "MODEL_RUNTIME_CACHE_KEY",
    "CacheLookup",
    "CacheSource",
    "CachedEmbedding",
    "Device",
    "DevicePreference",
    "DuplicateModelRegistrationError",
    "EmbeddingBatch",
    "EmbeddingCache",
    "EmbeddingCacheKey",
    "EmbeddingCacheStats",
    "EmbeddingProvider",
    "FakeEmbeddingProvider",
    "FakeOCRProvider",
    "ImageEmbeddingInput",
    "ModelDeviceUnavailableError",
    "ModelDownloadPermissionError",
    "ModelHealth",
    "ModelHealthStatus",
    "ModelProvider",
    "ModelProviderExecutionError",
    "ModelRunRecord",
    "ModelRunStatus",
    "ModelRuntimeConfig",
    "ModelRuntimeError",
    "ModelRuntimeManager",
    "ModelSpec",
    "NormalizedBoundingBox",
    "OCRBatch",
    "OCRImageInput",
    "OCRObservation",
    "OCRProvider",
    "OCRRuntimeUnavailableError",
    "Precision",
    "UnknownModelError",
    "create_model_runtime",
    "default_embedding_cache_directory",
    "detect_with_optional_ocr",
]
