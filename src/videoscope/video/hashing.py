"""Streaming hashes for local video inputs."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from videoscope.video.errors import (
    VideoHashError,
    VideoNotFoundError,
)

DEFAULT_HASH_CHUNK_SIZE = 1024 * 1024


def compute_file_sha256(
    path: Path,
    *,
    chunk_size: int = DEFAULT_HASH_CHUNK_SIZE,
) -> str:
    """Compute a SHA-256 digest without loading the entire file into memory."""
    input_path = Path(path)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if not input_path.is_file():
        raise VideoNotFoundError(f"Input file not found: {input_path.name}")

    digest = sha256()
    try:
        with input_path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
    except OSError as exc:
        raise VideoHashError(
            f"Could not read input while hashing: {input_path.name}"
        ) from exc
    return digest.hexdigest()
