"""Tests for streaming video input hashing."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from videoscope.video import (
    VideoNotFoundError,
    compute_file_sha256,
)


def test_hash_is_consistent_for_unicode_path_and_small_chunks(tmp_path: Path) -> None:
    video_path = tmp_path / "含 空格的视频.mp4"
    content = bytes(range(256)) * 32
    video_path.write_bytes(content)

    first = compute_file_sha256(video_path, chunk_size=17)
    second = compute_file_sha256(video_path, chunk_size=1024)

    assert first == sha256(content).hexdigest()
    assert first == second


def test_hash_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(VideoNotFoundError) as error:
        compute_file_sha256(tmp_path / "不存在.mp4")

    assert error.value.code == "video_not_found"
    assert str(tmp_path) not in str(error.value)


def test_hash_rejects_invalid_chunk_size(tmp_path: Path) -> None:
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"video")

    with pytest.raises(ValueError, match="chunk_size"):
        compute_file_sha256(video_path, chunk_size=0)
