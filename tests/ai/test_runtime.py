"""Tests for lazy shared providers, batching, policy, and failure isolation."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from pydantic import BaseModel, ConfigDict

from tests.detectors.dummy import DummyDetector
from tests.detectors.test_runner import make_context
from videoscope.ai import (
    MODEL_RUNTIME_CACHE_KEY,
    Device,
    DevicePreference,
    EmbeddingCache,
    FakeEmbeddingProvider,
    ImageEmbeddingInput,
    ModelDeviceUnavailableError,
    ModelDownloadPermissionError,
    ModelHealth,
    ModelProviderExecutionError,
    ModelRuntimeConfig,
    ModelRuntimeManager,
    ModelSpec,
    Precision,
)
from videoscope.detectors import (
    AnalysisContext,
    DetectorRegistry,
    DetectorRequirements,
    DetectorRunner,
    EstimatedCost,
)
from videoscope.domain import DetectorStatus, Finding
from videoscope.video import FrameSample

np: Any = importlib.import_module("numpy")
VIDEO_HASH = "ab" * 32
MODEL_SPEC = ModelSpec(
    provider_id="fake",
    model_id="fake-embedding-v1",
    preprocessing_version="bytes-v1",
)


class FakeProviderOptions(TypedDict, total=False):
    """Typed optional constructor controls for the fake provider."""

    model_id: str
    dimension: int
    local_files_available: bool
    fail_load: bool
    fail_encode: bool


def make_runtime(
    tmp_path: Path,
    *,
    device: DevicePreference = DevicePreference.CPU,
    batch_size: int = 2,
    allow_model_download: bool = False,
    interactive: bool = False,
    provider_options: FakeProviderOptions | None = None,
    cuda_available: bool = False,
    confirm_download: Callable[[ModelSpec, ModelHealth], bool] | None = None,
) -> tuple[ModelRuntimeManager, list[FakeEmbeddingProvider]]:
    """Create a registered runtime and expose factory instances for assertions."""
    providers: list[FakeEmbeddingProvider] = []
    options = provider_options or {}
    config = ModelRuntimeConfig(
        device=device,
        batch_size=batch_size,
        disk_cache_directory=tmp_path / "模型 cache",
        allow_model_download=allow_model_download,
        interactive=interactive,
    )
    runtime = ModelRuntimeManager(
        config,
        cache=EmbeddingCache(
            memory_budget_bytes=config.memory_budget_bytes,
            disk_directory=config.disk_cache_directory,
        ),
        cuda_available=lambda: cuda_available,
        confirm_download=confirm_download,
    )

    def factory(device: Device, precision: Precision) -> FakeEmbeddingProvider:
        provider = FakeEmbeddingProvider(
            device,
            precision,
            model_id=options.get("model_id", "fake-embedding-v1"),
            dimension=options.get("dimension", 8),
            local_files_available=options.get("local_files_available", True),
            fail_load=options.get("fail_load", False),
            fail_encode=options.get("fail_encode", False),
        )
        providers.append(provider)
        return provider

    runtime.register(MODEL_SPEC, factory)
    return runtime, providers


def make_inputs(tmp_path: Path, count: int = 3) -> tuple[ImageEmbeddingInput, ...]:
    """Create Unicode-path image-like byte inputs."""
    inputs: list[ImageEmbeddingInput] = []
    for index in range(count):
        path = tmp_path / "帧 文件" / f"{index}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"frame-{index}".encode())
        inputs.append(
            ImageEmbeddingInput(
                path=path,
                video_hash=VIDEO_HASH,
                timestamp_seconds=index * 0.5,
                preprocessing_version="bytes-v1",
            )
        )
    return tuple(inputs)


def test_registration_and_listing_do_not_construct_or_load_provider(
    tmp_path: Path,
) -> None:
    runtime, providers = make_runtime(tmp_path)

    assert runtime.list_models() == (MODEL_SPEC,)
    assert providers == []


def test_same_model_is_a_loaded_singleton(tmp_path: Path) -> None:
    runtime, providers = make_runtime(tmp_path)

    first = runtime.get_provider("fake", "fake-embedding-v1")
    second = runtime.get_provider("fake", "fake-embedding-v1")

    assert first is second
    assert len(providers) == 1
    assert providers[0].load_count == 1


def test_images_are_batched_and_second_consumer_hits_cache(tmp_path: Path) -> None:
    runtime, providers = make_runtime(tmp_path, batch_size=2)
    inputs = make_inputs(tmp_path, count=5)

    first = runtime.encode_images("fake", "fake-embedding-v1", inputs)
    second = runtime.encode_images("fake", "fake-embedding-v1", inputs)

    np.testing.assert_allclose(first.embeddings, second.embeddings)
    assert providers[0].image_encode_calls == 3
    assert providers[0].load_count == 1
    first_record, second_record = runtime.records()
    assert first_record.encoded_items == 5
    assert first_record.cache_hits == 0
    assert second_record.encoded_items == 0
    assert second_record.cache_hits == 5
    assert second_record.cache_hit_rate == 1.0


def test_text_encoding_uses_configured_batch_size(tmp_path: Path) -> None:
    runtime, providers = make_runtime(tmp_path, batch_size=2)

    result = runtime.encode_text(
        "fake",
        "fake-embedding-v1",
        ["一", "two", "三"],
    )

    assert result.embeddings.shape == (3, 8)
    assert providers[0].text_encode_calls == 2
    assert runtime.records()[0].batch_size == 2


def test_provider_exception_is_recorded_and_sanitized(tmp_path: Path) -> None:
    runtime, _ = make_runtime(
        tmp_path,
        provider_options={"fail_encode": True},
    )

    with pytest.raises(ModelProviderExecutionError) as captured:
        runtime.encode_images(
            "fake",
            "fake-embedding-v1",
            make_inputs(tmp_path, count=1),
        )

    assert str(tmp_path) not in str(captured.value)
    record = runtime.records()[0]
    assert record.status.value == "error"
    assert record.error_type == "RuntimeError"


def test_explicit_cuda_fails_cleanly_without_gpu(tmp_path: Path) -> None:
    runtime, providers = make_runtime(
        tmp_path,
        device=DevicePreference.CUDA,
        cuda_available=False,
    )

    with pytest.raises(ModelDeviceUnavailableError):
        runtime.get_provider("fake", "fake-embedding-v1")

    assert providers == []


def test_noninteractive_runtime_blocks_implicit_download(tmp_path: Path) -> None:
    runtime, providers = make_runtime(
        tmp_path,
        provider_options={"local_files_available": False},
    )

    with pytest.raises(ModelDownloadPermissionError) as captured:
        runtime.get_provider("fake", "fake-embedding-v1")

    assert "--allow-model-download" in str(captured.value)
    assert providers[0].load_count == 0


def test_explicit_download_permission_reaches_provider_load(tmp_path: Path) -> None:
    runtime, providers = make_runtime(
        tmp_path,
        allow_model_download=True,
        provider_options={"local_files_available": False},
    )

    provider = runtime.get_provider("fake", "fake-embedding-v1")

    assert provider is providers[0]
    assert providers[0].load_count == 1


def test_interactive_runtime_uses_explicit_confirmation_callback(
    tmp_path: Path,
) -> None:
    prompted: list[tuple[str, str]] = []

    def confirm(spec: ModelSpec, health: ModelHealth) -> bool:
        prompted.append((spec.model_id, health.message))
        return True

    runtime, providers = make_runtime(
        tmp_path,
        interactive=True,
        provider_options={"local_files_available": False},
        confirm_download=confirm,
    )

    runtime.get_provider("fake", "fake-embedding-v1")

    assert prompted == [("fake-embedding-v1", "Fake provider is not loaded.")]
    assert providers[0].load_count == 1


class EmptyConfig(BaseModel):
    """No-op detector configuration."""

    model_config = ConfigDict(extra="forbid")


class SharedEmbeddingDetector:
    """Test detector that consumes the injected shared model runtime."""

    display_name = "Shared embedding consumer"
    version = "1.0.0"
    description = "Exercises the shared runtime without emitting Findings."
    requirements = DetectorRequirements(
        requires_gpu=False,
        requires_network=False,
        optional_packages=("fake",),
        estimated_cost=EstimatedCost.HIGH,
    )
    default_enabled = True
    config_model = EmptyConfig

    def __init__(self, detector_id: str) -> None:
        self.id = detector_id

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        del config
        runtime = context.shared_cache[MODEL_RUNTIME_CACHE_KEY]
        if not isinstance(runtime, ModelRuntimeManager):
            raise TypeError("shared runtime missing")
        inputs = tuple(
            ImageEmbeddingInput(
                path=context.workspace / sample.relative_path,
                video_hash=context.input_hash,
                timestamp_seconds=sample.timestamp_seconds,
                preprocessing_version="bytes-v1",
            )
            for sample in context.frame_samples
        )
        runtime.encode_images("fake", "fake-embedding-v1", inputs)
        return []


def shared_context(
    tmp_path: Path,
    runtime: ModelRuntimeManager,
) -> AnalysisContext:
    """Create a detector context whose sample exists in the workspace."""
    context = make_context(tmp_path)
    relative_path = "frames/共享 帧.jpg"
    frame_path = context.workspace / relative_path
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    frame_path.write_bytes(b"shared-frame")
    return cast(
        AnalysisContext,
        context.model_copy(
            update={
                "frame_samples": (
                    FrameSample(
                        timestamp_seconds=1.0,
                        sample_index=0,
                        relative_path=relative_path,
                        width=32,
                        height=32,
                    ),
                ),
                "shared_cache": {MODEL_RUNTIME_CACHE_KEY: runtime},
            }
        ),
    )


def test_two_detectors_share_one_model_and_one_frame_embedding(
    tmp_path: Path,
) -> None:
    runtime, providers = make_runtime(tmp_path)
    registry = DetectorRegistry(
        [
            SharedEmbeddingDetector("test.ai_a"),
            SharedEmbeddingDetector("test.ai_b"),
        ]
    )

    result = DetectorRunner(registry).run(shared_context(tmp_path, runtime))

    assert [item.status for item in result.executions] == [
        DetectorStatus.OK,
        DetectorStatus.OK,
    ]
    assert len(providers) == 1
    assert providers[0].load_count == 1
    assert providers[0].image_encode_calls == 1
    assert runtime.records()[1].cache_hit_rate == 1.0


def test_ai_provider_failure_does_not_remove_cpu_detector_result(
    tmp_path: Path,
) -> None:
    runtime, _ = make_runtime(
        tmp_path,
        provider_options={"fail_encode": True},
    )
    registry = DetectorRegistry(
        [
            SharedEmbeddingDetector("test.ai_failure"),
            DummyDetector("test.cpu_success"),
        ]
    )

    result = DetectorRunner(registry).run(shared_context(tmp_path, runtime))

    assert [item.status for item in result.executions] == [
        DetectorStatus.DETECTOR_ERROR,
        DetectorStatus.OK,
    ]
    assert len(result.findings) == 1
    assert result.findings[0].detector_id == "test.cpu_success"
