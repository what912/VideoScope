"""Lazy, shared, failure-transparent model runtime and embedding batching."""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Callable, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

from platformdirs import user_cache_dir

from videoscope.ai.cache import CachedEmbedding, CacheSource, EmbeddingCache
from videoscope.ai.models import (
    Device,
    DevicePreference,
    EmbeddingBatch,
    EmbeddingCacheKey,
    FloatArray,
    ImageEmbeddingInput,
    ModelHealth,
    ModelHealthStatus,
    ModelRunRecord,
    ModelRunStatus,
    ModelRuntimeConfig,
    ModelSpec,
    OCRBatch,
    OCRImageInput,
    OCRObservation,
    Precision,
)
from videoscope.ai.protocols import EmbeddingProvider, ModelProvider, OCRProvider

np: Any = importlib.import_module("numpy")

ProviderFactory = Callable[[Device, Precision], ModelProvider]
DownloadConfirmation = Callable[[ModelSpec, ModelHealth], bool]
CudaAvailabilityCheck = Callable[[], bool]
Clock = Callable[[], float]


class ModelRuntimeError(RuntimeError):
    """Base error for optional model runtime operations."""


class DuplicateModelRegistrationError(ModelRuntimeError):
    """A provider/model pair was registered more than once."""


class UnknownModelError(ModelRuntimeError):
    """A requested provider/model pair is not registered."""


class ModelDeviceUnavailableError(ModelRuntimeError):
    """The requested execution device is unavailable."""


class ModelDownloadPermissionError(ModelRuntimeError):
    """A provider needs model files but download was not explicitly approved."""


class ModelProviderExecutionError(ModelRuntimeError):
    """A provider failed without leaking its local diagnostic details."""


def _default_cuda_available() -> bool:
    """Probe CUDA only when an optional provider is actually requested."""
    if importlib.util.find_spec("torch") is None:
        return False
    torch = importlib.import_module("torch")
    cuda = getattr(torch, "cuda", None)
    is_available = getattr(cuda, "is_available", None)
    return bool(is_available()) if callable(is_available) else False


def default_embedding_cache_directory() -> Path:
    """Return the platform-appropriate local embedding cache directory."""
    return Path(user_cache_dir("videoscope", "VideoScope")) / "embeddings"


class ModelRuntimeManager:
    """Share model instances and frame embeddings across all detectors."""

    def __init__(
        self,
        config: ModelRuntimeConfig | None = None,
        *,
        cache: EmbeddingCache | None = None,
        cuda_available: CudaAvailabilityCheck = _default_cuda_available,
        confirm_download: DownloadConfirmation | None = None,
        clock: Clock = perf_counter,
    ) -> None:
        self.config = config or ModelRuntimeConfig()
        disk_directory = (
            self.config.disk_cache_directory
            if self.config.disk_cache_directory is not None
            else default_embedding_cache_directory()
        )
        self.cache = cache or EmbeddingCache(
            memory_budget_bytes=self.config.memory_budget_bytes,
            disk_directory=disk_directory,
        )
        self._cuda_available = cuda_available
        self._confirm_download = confirm_download
        self._clock = clock
        self._registrations: dict[
            tuple[str, str], tuple[ModelSpec, ProviderFactory]
        ] = {}
        self._providers: dict[
            tuple[str, str, Device, Precision],
            ModelProvider,
        ] = {}
        self._records: list[ModelRunRecord] = []

    def register(self, spec: ModelSpec, factory: ProviderFactory) -> None:
        """Register a provider factory without constructing or loading it."""
        key = (spec.provider_id, spec.model_id)
        if key in self._registrations:
            raise DuplicateModelRegistrationError(
                f"Model {spec.provider_id}/{spec.model_id} is already registered."
            )
        self._registrations[key] = (spec, factory)

    def list_models(self) -> tuple[ModelSpec, ...]:
        """List registered providers in deterministic order."""
        return tuple(
            registration[0] for _, registration in sorted(self._registrations.items())
        )

    def _registration(
        self,
        provider_id: str,
        model_id: str,
    ) -> tuple[ModelSpec, ProviderFactory]:
        try:
            return self._registrations[(provider_id, model_id)]
        except KeyError as exc:
            raise UnknownModelError(
                f"Unknown model provider: {provider_id}/{model_id}"
            ) from exc

    def _resolve_device(self) -> Device:
        preference = self.config.device
        if preference is DevicePreference.CPU:
            return Device.CPU
        cuda_available = self._cuda_available()
        if preference is DevicePreference.CUDA:
            if not cuda_available:
                raise ModelDeviceUnavailableError(
                    "CUDA was requested but is not available; select cpu or auto."
                )
            return Device.CUDA
        return Device.CUDA if cuda_available else Device.CPU

    def _approve_download(self, spec: ModelSpec, health: ModelHealth) -> None:
        if health.local_files_available:
            return
        if self.config.allow_model_download:
            return
        if (
            self.config.interactive
            and self._confirm_download is not None
            and self._confirm_download(spec, health)
        ):
            return
        raise ModelDownloadPermissionError(
            f"Model {spec.provider_id}/{spec.model_id} is not available in the "
            "local cache. Download was not started. In non-interactive use, "
            "pass --allow-model-download explicitly."
        )

    @staticmethod
    def _validate_provider(
        provider: ModelProvider,
        *,
        spec: ModelSpec,
        device: Device,
        precision: Precision,
    ) -> None:
        expected = (
            spec.provider_id,
            spec.model_id,
            device,
            precision,
        )
        actual = (
            provider.provider_id,
            provider.model_id,
            provider.device,
            provider.precision,
        )
        if actual != expected:
            raise ModelProviderExecutionError(
                "Provider identity or runtime configuration does not match "
                "its registration."
            )

    def get_provider(
        self,
        provider_id: str,
        model_id: str,
    ) -> ModelProvider:
        """Return the lazily loaded singleton for one runtime configuration."""
        spec, factory = self._registration(provider_id, model_id)
        device = self._resolve_device()
        key = (provider_id, model_id, device, self.config.precision)
        existing = self._providers.get(key)
        if existing is not None:
            return existing

        try:
            provider = factory(device, self.config.precision)
            self._validate_provider(
                provider,
                spec=spec,
                device=device,
                precision=self.config.precision,
            )
            health = provider.health()
            if health.status is ModelHealthStatus.ERROR:
                raise ModelProviderExecutionError(
                    f"Provider {provider_id}/{model_id} is unavailable: "
                    f"{health.message}"
                )
            self._approve_download(spec, health)
            provider.load()
            loaded_health = provider.health()
            if loaded_health.status is not ModelHealthStatus.READY:
                raise ModelProviderExecutionError(
                    f"Provider {provider_id}/{model_id} did not become ready."
                )
        except ModelRuntimeError:
            raise
        except Exception as exc:
            raise ModelProviderExecutionError(
                f"Provider {provider_id}/{model_id} failed to load: "
                f"{type(exc).__name__}."
            ) from exc

        self._providers[key] = provider
        return provider

    @staticmethod
    def _normalized_batch(
        batch: EmbeddingBatch,
        *,
        expected_rows: int,
    ) -> EmbeddingBatch:
        embeddings = np.asarray(batch.embeddings, dtype=np.float32)
        if embeddings.ndim != 2 or embeddings.shape[0] != expected_rows:
            raise ModelProviderExecutionError(
                "Provider returned an invalid embedding batch shape."
            )
        if not np.isfinite(embeddings).all():
            raise ModelProviderExecutionError(
                "Provider returned non-finite embedding values."
            )
        normalized = np.array(embeddings, dtype=np.float32, copy=True)
        normalized.setflags(write=False)
        return EmbeddingBatch(normalized, dict(batch.metadata))

    @staticmethod
    def _stack(values: Sequence[CachedEmbedding]) -> FloatArray:
        dimensions = {value.embedding.shape for value in values}
        if len(dimensions) != 1:
            raise ModelProviderExecutionError(
                "Cached and computed embeddings have inconsistent dimensions."
            )
        stacked = np.stack([value.embedding for value in values]).astype(
            np.float32,
            copy=False,
        )
        stacked.setflags(write=False)
        return stacked

    def _record(
        self,
        *,
        provider_id: str,
        model_id: str,
        operation: str,
        status: ModelRunStatus,
        device: str,
        requested_items: int,
        encoded_items: int,
        inference_seconds: float,
        cache_hits: int,
        cache_misses: int,
        error_type: str | None = None,
    ) -> ModelRunRecord:
        record = ModelRunRecord.from_counts(
            provider_id=provider_id,
            model_id=model_id,
            operation=operation,
            status=status,
            device=device,
            precision=self.config.precision.value,
            batch_size=self.config.batch_size,
            requested_items=requested_items,
            encoded_items=encoded_items,
            inference_seconds=inference_seconds,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            error_type=error_type,
        )
        self._records.append(record)
        return record

    def encode_images(
        self,
        provider_id: str,
        model_id: str,
        items: Sequence[ImageEmbeddingInput],
    ) -> EmbeddingBatch:
        """Encode missing frames in batches and preserve caller order."""
        if not items:
            raise ValueError("at least one image embedding input is required")
        self._registration(provider_id, model_id)
        values: list[CachedEmbedding | None] = [None] * len(items)
        misses: list[tuple[int, ImageEmbeddingInput, EmbeddingCacheKey]] = []
        cache_hits = 0
        for index, item in enumerate(items):
            key = EmbeddingCacheKey(
                video_hash=item.video_hash,
                timestamp_seconds=item.timestamp_seconds,
                provider_id=provider_id,
                model_id=model_id,
                preprocessing_version=item.preprocessing_version,
            )
            lookup = self.cache.get(key)
            if lookup.source is CacheSource.MISS:
                misses.append((index, item, key))
            else:
                if lookup.value is None:
                    raise ModelProviderExecutionError(
                        "Cache reported a hit without an embedding."
                    )
                values[index] = lookup.value
                cache_hits += 1

        inference_seconds = 0.0
        encoded_items = 0
        device_name = self.config.device.value
        try:
            if misses:
                provider = self.get_provider(provider_id, model_id)
                if not isinstance(provider, EmbeddingProvider):
                    raise ModelProviderExecutionError(
                        f"Provider {provider_id}/{model_id} does not support "
                        "image embeddings."
                    )
                device_name = provider.device.value
                for offset in range(0, len(misses), self.config.batch_size):
                    chunk = misses[offset : offset + self.config.batch_size]
                    started_at = self._clock()
                    raw_batch = provider.encode_images(
                        [str(item.path) for _, item, _ in chunk]
                    )
                    inference_seconds += max(0.0, self._clock() - started_at)
                    batch = self._normalized_batch(
                        raw_batch,
                        expected_rows=len(chunk),
                    )
                    for row, (index, item, key) in enumerate(chunk):
                        cache_metadata: dict[str, Any] = {
                            "device": provider.device.value,
                            "model_id": provider.model_id,
                            "precision": provider.precision.value,
                            "preprocessing_version": item.preprocessing_version,
                            "provider_id": provider.provider_id,
                            "provider_metadata": batch.metadata,
                        }
                        values[index] = self.cache.put(
                            key,
                            batch.embeddings[row],
                            cache_metadata,
                        )
                    encoded_items += len(chunk)
            completed = [value for value in values if value is not None]
            if len(completed) != len(items):
                raise ModelProviderExecutionError(
                    "Runtime did not produce every requested image embedding."
                )
            record = self._record(
                provider_id=provider_id,
                model_id=model_id,
                operation="encode_images",
                status=ModelRunStatus.OK,
                device=device_name,
                requested_items=len(items),
                encoded_items=encoded_items,
                inference_seconds=inference_seconds,
                cache_hits=cache_hits,
                cache_misses=len(misses),
            )
            return EmbeddingBatch(
                self._stack(completed),
                {
                    "items": [value.metadata for value in completed],
                    "runtime": record.model_dump(mode="json"),
                },
            )
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._record(
                provider_id=provider_id,
                model_id=model_id,
                operation="encode_images",
                status=ModelRunStatus.ERROR,
                device=device_name,
                requested_items=len(items),
                encoded_items=encoded_items,
                inference_seconds=inference_seconds,
                cache_hits=cache_hits,
                cache_misses=len(misses),
                error_type=type(exc).__name__,
            )
            if isinstance(exc, ModelRuntimeError):
                raise
            raise ModelProviderExecutionError(
                f"Provider {provider_id}/{model_id} image encoding failed: "
                f"{type(exc).__name__}."
            ) from exc

    def encode_text(
        self,
        provider_id: str,
        model_id: str,
        texts: Sequence[str],
    ) -> EmbeddingBatch:
        """Encode text in deterministic batches without a frame cache."""
        if not texts:
            raise ValueError("at least one text value is required")
        inference_seconds = 0.0
        encoded_items = 0
        device_name = self.config.device.value
        batches: list[FloatArray] = []
        provider_metadata: list[dict[str, Any]] = []
        try:
            provider = self.get_provider(provider_id, model_id)
            if not isinstance(provider, EmbeddingProvider):
                raise ModelProviderExecutionError(
                    f"Provider {provider_id}/{model_id} does not support "
                    "text embeddings."
                )
            device_name = provider.device.value
            for offset in range(0, len(texts), self.config.batch_size):
                chunk = texts[offset : offset + self.config.batch_size]
                started_at = self._clock()
                raw_batch = provider.encode_text(chunk)
                inference_seconds += max(0.0, self._clock() - started_at)
                batch = self._normalized_batch(
                    raw_batch,
                    expected_rows=len(chunk),
                )
                batches.append(batch.embeddings)
                provider_metadata.append(batch.metadata)
                encoded_items += len(chunk)
            embeddings = np.concatenate(batches, axis=0)
            embeddings.setflags(write=False)
            record = self._record(
                provider_id=provider_id,
                model_id=model_id,
                operation="encode_text",
                status=ModelRunStatus.OK,
                device=device_name,
                requested_items=len(texts),
                encoded_items=encoded_items,
                inference_seconds=inference_seconds,
                cache_hits=0,
                cache_misses=0,
            )
            return EmbeddingBatch(
                embeddings,
                {
                    "provider_batches": provider_metadata,
                    "runtime": record.model_dump(mode="json"),
                },
            )
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._record(
                provider_id=provider_id,
                model_id=model_id,
                operation="encode_text",
                status=ModelRunStatus.ERROR,
                device=device_name,
                requested_items=len(texts),
                encoded_items=encoded_items,
                inference_seconds=inference_seconds,
                cache_hits=0,
                cache_misses=0,
                error_type=type(exc).__name__,
            )
            if isinstance(exc, ModelRuntimeError):
                raise
            raise ModelProviderExecutionError(
                f"Provider {provider_id}/{model_id} text encoding failed: "
                f"{type(exc).__name__}."
            ) from exc

    @staticmethod
    def _validated_ocr_batch(
        batch: OCRBatch,
        *,
        requested_timestamps: set[float],
    ) -> OCRBatch:
        observations: list[OCRObservation] = []
        for observation in batch.observations:
            validated = OCRObservation.model_validate(
                observation.model_dump(mode="python")
            )
            if validated.timestamp_seconds not in requested_timestamps:
                raise ModelProviderExecutionError(
                    "OCR provider returned an observation for an unrequested "
                    "frame timestamp."
                )
            observations.append(validated)
        return OCRBatch(
            observations=tuple(
                sorted(
                    observations,
                    key=lambda item: (
                        item.timestamp_seconds,
                        item.bounding_box.y_min,
                        item.bounding_box.x_min,
                        item.text,
                        -item.confidence,
                    ),
                )
            ),
            metadata=dict(batch.metadata),
        )

    def detect_and_recognize(
        self,
        provider_id: str,
        model_id: str,
        images: Sequence[OCRImageInput],
    ) -> OCRBatch:
        """Run OCR in deterministic batches through a shared lazy provider."""
        if not images:
            raise ValueError("at least one OCR image input is required")
        self._registration(provider_id, model_id)
        inference_seconds = 0.0
        processed_images = 0
        device_name = self.config.device.value
        observations: list[OCRObservation] = []
        provider_metadata: list[dict[str, Any]] = []
        try:
            provider = self.get_provider(provider_id, model_id)
            if not isinstance(provider, OCRProvider):
                raise ModelProviderExecutionError(
                    f"Provider {provider_id}/{model_id} does not support OCR."
                )
            device_name = provider.device.value
            for offset in range(0, len(images), self.config.batch_size):
                chunk = images[offset : offset + self.config.batch_size]
                started_at = self._clock()
                raw_batch = provider.detect_and_recognize(chunk)
                inference_seconds += max(0.0, self._clock() - started_at)
                batch = self._validated_ocr_batch(
                    raw_batch,
                    requested_timestamps={image.timestamp_seconds for image in chunk},
                )
                observations.extend(batch.observations)
                provider_metadata.append(batch.metadata)
                processed_images += len(chunk)
            record = self._record(
                provider_id=provider_id,
                model_id=model_id,
                operation="detect_and_recognize",
                status=ModelRunStatus.OK,
                device=device_name,
                requested_items=len(images),
                encoded_items=processed_images,
                inference_seconds=inference_seconds,
                cache_hits=0,
                cache_misses=0,
            )
            return OCRBatch(
                observations=tuple(
                    sorted(
                        observations,
                        key=lambda item: (
                            item.timestamp_seconds,
                            item.bounding_box.y_min,
                            item.bounding_box.x_min,
                            item.text,
                            -item.confidence,
                        ),
                    )
                ),
                metadata={
                    "provider_batches": provider_metadata,
                    "runtime": record.model_dump(mode="json"),
                },
            )
        except Exception as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self._record(
                provider_id=provider_id,
                model_id=model_id,
                operation="detect_and_recognize",
                status=ModelRunStatus.ERROR,
                device=device_name,
                requested_items=len(images),
                encoded_items=processed_images,
                inference_seconds=inference_seconds,
                cache_hits=0,
                cache_misses=0,
                error_type=type(exc).__name__,
            )
            if isinstance(exc, ModelRuntimeError):
                raise
            raise ModelProviderExecutionError(
                f"Provider {provider_id}/{model_id} OCR failed: {type(exc).__name__}."
            ) from exc

    def records(self) -> tuple[ModelRunRecord, ...]:
        """Return model run records in execution order."""
        return tuple(self._records)

    def unload_all(self) -> None:
        """Release every loaded singleton; cache entries remain reusable."""
        providers = tuple(self._providers.values())
        self._providers.clear()
        for provider in providers:
            try:
                provider.unload()
            except Exception:
                continue


def create_model_runtime(
    config: ModelRuntimeConfig | None = None,
    *,
    confirm_download: DownloadConfirmation | None = None,
) -> ModelRuntimeManager:
    """Create the built-in runtime and lazily register optional providers."""
    runtime = ModelRuntimeManager(
        config,
        confirm_download=confirm_download,
    )
    from videoscope.ai.providers.dinov2 import register_dinov2_provider
    from videoscope.ai.providers.openclip import register_openclip_provider
    from videoscope.ai.providers.paddleocr import register_paddleocr_providers

    register_dinov2_provider(runtime)
    register_openclip_provider(runtime)
    register_paddleocr_providers(runtime)
    return runtime
