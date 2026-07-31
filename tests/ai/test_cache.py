"""Tests for deterministic, memory-bounded frame embedding caching."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from videoscope.ai import (
    CacheSource,
    EmbeddingCache,
    EmbeddingCacheKey,
)

np: Any = importlib.import_module("numpy")
VIDEO_HASH = "12" * 32


def make_key(
    *,
    timestamp_seconds: float = 1.25,
    preprocessing_version: str = "rgb-v1",
) -> EmbeddingCacheKey:
    """Create a canonical test key."""
    return EmbeddingCacheKey(
        video_hash=VIDEO_HASH,
        timestamp_seconds=timestamp_seconds,
        provider_id="fake",
        model_id="fake-embedding-v1",
        preprocessing_version=preprocessing_version,
    )


def test_cache_key_is_deterministic_and_covers_required_components() -> None:
    first = make_key()
    repeated = make_key()
    different_time = make_key(timestamp_seconds=1.5)
    different_preprocessing = make_key(preprocessing_version="rgb-v2")

    assert first.digest == repeated.digest
    assert first.canonical_payload() == repeated.canonical_payload()
    assert first.digest != different_time.digest
    assert first.digest != different_preprocessing.digest


def test_memory_cache_hit_returns_float32_vector() -> None:
    cache = EmbeddingCache(memory_budget_bytes=1024, disk_directory=None)
    key = make_key()
    cache.put(key, np.asarray([1.0, 2.0]), {"source": "test"})

    lookup = cache.get(key)

    assert lookup.source is CacheSource.MEMORY
    assert lookup.value is not None
    assert lookup.value.embedding.dtype == np.float32
    assert lookup.value.embedding.flags.writeable is False
    assert cache.stats().hit_rate == 1.0


def test_disk_cache_survives_a_new_memory_cache_instance(tmp_path: Path) -> None:
    cache_directory = tmp_path / "嵌入 cache"
    key = make_key()
    first = EmbeddingCache(
        memory_budget_bytes=0,
        disk_directory=cache_directory,
    )
    first.put(key, np.asarray([0.25, 0.75]), {"中文": "保留"})

    second = EmbeddingCache(
        memory_budget_bytes=1024,
        disk_directory=cache_directory,
    )
    lookup = second.get(key)

    assert lookup.source is CacheSource.DISK
    assert lookup.value is not None
    np.testing.assert_allclose(lookup.value.embedding, [0.25, 0.75])
    assert lookup.value.metadata == {"中文": "保留"}


def test_memory_budget_evicts_least_recently_used_entry() -> None:
    cache = EmbeddingCache(memory_budget_bytes=12, disk_directory=None)
    first_key = make_key(timestamp_seconds=1.0)
    second_key = make_key(timestamp_seconds=2.0)
    cache.put(first_key, np.asarray([1.0, 2.0]), {})
    cache.put(second_key, np.asarray([3.0, 4.0]), {})

    first_lookup = cache.get(first_key)
    second_lookup = cache.get(second_key)

    assert first_lookup.source is CacheSource.MISS
    assert second_lookup.source is CacheSource.MEMORY
