"""Memory-bounded LRU and privacy-conscious frame embedding disk cache."""

from __future__ import annotations

import importlib
import json
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from videoscope.ai.models import EmbeddingCacheKey, FloatArray

np: Any = importlib.import_module("numpy")


class CacheSource(StrEnum):
    """Source of one cache lookup."""

    MEMORY = "memory"
    DISK = "disk"
    MISS = "miss"


@dataclass(frozen=True, slots=True)
class CachedEmbedding:
    """One immutable vector and its provider metadata."""

    embedding: FloatArray
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """Result and provenance of one cache lookup."""

    value: CachedEmbedding | None
    source: CacheSource


@dataclass(frozen=True, slots=True)
class EmbeddingCacheStats:
    """Cumulative cache statistics."""

    requests: int
    memory_hits: int
    disk_hits: int
    misses: int
    writes: int

    @property
    def hit_rate(self) -> float:
        """Return the fraction of requests served from either cache tier."""
        if self.requests == 0:
            return 0.0
        return (self.memory_hits + self.disk_hits) / self.requests


class EmbeddingCache:
    """Two-tier cache keyed only by non-identifying content metadata."""

    def __init__(
        self,
        *,
        memory_budget_bytes: int,
        disk_directory: Path | None,
    ) -> None:
        if memory_budget_bytes < 0:
            raise ValueError("memory_budget_bytes must be non-negative")
        self.memory_budget_bytes = memory_budget_bytes
        self.disk_directory = disk_directory
        self._memory: OrderedDict[str, CachedEmbedding] = OrderedDict()
        self._memory_sizes: dict[str, int] = {}
        self._memory_bytes = 0
        self._requests = 0
        self._memory_hits = 0
        self._disk_hits = 0
        self._misses = 0
        self._writes = 0

    @staticmethod
    def _normalized(
        embedding: FloatArray,
        metadata: dict[str, Any],
    ) -> CachedEmbedding:
        vector = np.asarray(embedding, dtype=np.float32)
        if vector.ndim != 1:
            raise ValueError("cached embedding must be one-dimensional")
        if not np.isfinite(vector).all():
            raise ValueError("cached embedding must contain only finite values")
        frozen = np.array(vector, dtype=np.float32, copy=True)
        frozen.setflags(write=False)
        return CachedEmbedding(frozen, dict(metadata))

    @staticmethod
    def _entry_size(value: CachedEmbedding) -> int:
        metadata_size = len(
            json.dumps(
                value.metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        return int(value.embedding.nbytes) + metadata_size

    def _remember(self, digest: str, value: CachedEmbedding) -> None:
        if self.memory_budget_bytes == 0:
            return
        size = self._entry_size(value)
        if size > self.memory_budget_bytes:
            return
        existing_size = self._memory_sizes.pop(digest, 0)
        if digest in self._memory:
            del self._memory[digest]
        self._memory_bytes -= existing_size
        while self._memory and self._memory_bytes + size > self.memory_budget_bytes:
            old_digest, _ = self._memory.popitem(last=False)
            self._memory_bytes -= self._memory_sizes.pop(old_digest)
        self._memory[digest] = value
        self._memory_sizes[digest] = size
        self._memory_bytes += size

    def _disk_path(self, digest: str) -> Path | None:
        if self.disk_directory is None:
            return None
        return self.disk_directory / digest[:2] / f"{digest}.npz"

    def get(self, key: EmbeddingCacheKey) -> CacheLookup:
        """Return a cache entry without loading any model provider."""
        self._requests += 1
        digest = key.digest
        memory_value = self._memory.get(digest)
        if memory_value is not None:
            self._memory.move_to_end(digest)
            self._memory_hits += 1
            return CacheLookup(memory_value, CacheSource.MEMORY)

        disk_path = self._disk_path(digest)
        if disk_path is not None and disk_path.is_file():
            try:
                with np.load(disk_path, allow_pickle=False) as archive:
                    vector = archive["embedding"]
                    raw_metadata = str(archive["metadata"].item())
                decoded = json.loads(raw_metadata)
                if not isinstance(decoded, dict):
                    raise ValueError("cache metadata must be an object")
                value = self._normalized(
                    vector,
                    cast(dict[str, Any], decoded),
                )
            except (OSError, ValueError, KeyError, json.JSONDecodeError):
                value = None
            if value is not None:
                self._remember(digest, value)
                self._disk_hits += 1
                return CacheLookup(value, CacheSource.DISK)

        self._misses += 1
        return CacheLookup(None, CacheSource.MISS)

    def put(
        self,
        key: EmbeddingCacheKey,
        embedding: FloatArray,
        metadata: dict[str, Any],
    ) -> CachedEmbedding:
        """Store one vector in memory and, when configured, atomically on disk."""
        value = self._normalized(embedding, metadata)
        digest = key.digest
        self._remember(digest, value)

        disk_path = self._disk_path(digest)
        if disk_path is not None:
            disk_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_json = json.dumps(
                value.metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{digest}-",
                    suffix=".npz",
                    dir=disk_path.parent,
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    np.savez_compressed(
                        temporary,
                        embedding=value.embedding,
                        metadata=np.asarray(metadata_json),
                    )
                temporary_path.replace(disk_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        self._writes += 1
        return value

    def stats(self) -> EmbeddingCacheStats:
        """Return a stable snapshot of cumulative cache behavior."""
        return EmbeddingCacheStats(
            requests=self._requests,
            memory_hits=self._memory_hits,
            disk_hits=self._disk_hits,
            misses=self._misses,
            writes=self._writes,
        )
