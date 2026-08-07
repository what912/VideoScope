"""Safety and lifecycle coverage for local Web job storage."""

from __future__ import annotations

import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from videoscope.web.storage import LocalJobStore


def _store(tmp_path: Path) -> LocalJobStore:
    return LocalJobStore(tmp_path / "应用 数据" / "web jobs")


def test_reserve_uses_random_contained_directories_and_normalized_suffix(
    tmp_path: Path,
) -> None:
    """A changed upload name must never control a job path."""
    store = _store(tmp_path)

    first = store.reserve("../../外部/Camera.MP4")
    second = store.reserve("Camera.MP4")

    assert re.fullmatch(r"[0-9a-f]{32}", first.job_id)
    assert first.job_id != second.job_id
    assert first.directory.parent == store.root
    assert first.input_path == first.directory / "input.mp4"
    assert first.output_directory == first.directory / "artifacts"
    assert first.directory.is_dir()
    assert not (store.root.parent / "外部").exists()


def test_reserve_falls_back_to_bin_for_unsafe_upload_suffix(tmp_path: Path) -> None:
    """An invalid extension must not become part of the on-disk filename."""
    job = _store(tmp_path).reserve("video.verylongsuffix")

    assert job.input_path.name == "input.bin"


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "job_id", ["", "../outside", "A" * 32, "0" * 31]
)
def test_require_directory_rejects_noncanonical_job_identifiers(
    tmp_path: Path,
    job_id: str,
) -> None:
    """Relaxing identifier validation would permit directory traversal."""
    store = _store(tmp_path)

    with pytest.raises(FileNotFoundError):
        store.require_directory(job_id)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "requested_path",
    [
        "",
        ".",
        "..",
        "nested/../report.json",
        "report//copy.json",
        "/tmp/report.json",
        r"C:\\outside\\report.json",
    ],
)
def test_resolve_artifact_rejects_non_relative_artifact_paths(
    tmp_path: Path,
    requested_path: str,
) -> None:
    """An unchecked artifact path could expose files outside job artifacts."""
    store = _store(tmp_path)
    job = store.reserve("video.mp4")
    job.output_directory.mkdir()
    (job.output_directory / "report.json").write_text("{}", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        store.resolve_artifact(job.job_id, requested_path)


def test_resolve_artifact_returns_existing_file_under_job_artifacts(
    tmp_path: Path,
) -> None:
    """Changing normal relative resolution would break completed-job downloads."""
    store = _store(tmp_path)
    job = store.reserve("video.mp4")
    evidence = job.output_directory / "evidence"
    evidence.mkdir(parents=True)
    expected = evidence / "frame.jpg"
    expected.write_bytes(b"evidence")

    assert store.resolve_artifact(job.job_id, "evidence/frame.jpg") == expected


def test_resolve_artifact_rejects_symlink_escape_when_supported(tmp_path: Path) -> None:
    """Following an artifact symlink outside its job would disclose local data."""
    store = _store(tmp_path)
    job = store.reserve("video.mp4")
    job.output_directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    try:
        (job.output_directory / "escape").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    with pytest.raises(FileNotFoundError):
        store.resolve_artifact(job.job_id, "escape/secret.txt")


def test_custom_private_artifact_root_cannot_be_a_symlink(
    tmp_path: Path,
) -> None:
    """A linked private evidence root could expose files outside the job."""
    store = _store(tmp_path)
    job = store.reserve("video.mp4")
    outside = tmp_path / "outside private evidence"
    outside.mkdir()
    (outside / "raw.png").write_bytes(b"private")
    linked_root = job.directory / "artifacts" / "privacy-review-private"
    linked_root.parent.mkdir()
    try:
        linked_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    with pytest.raises(FileNotFoundError):
        store.resolve_artifact(
            job.job_id,
            "raw.png",
            artifact_root=linked_root,
        )


def test_discard_removes_reserved_job_directory(tmp_path: Path) -> None:
    """A failed upload must not leave a retained local upload directory."""
    store = _store(tmp_path)
    job = store.reserve("video.mp4")
    job.input_path.write_bytes(b"partial upload")

    store.discard(job.job_id)

    assert not job.directory.exists()
    with pytest.raises(FileNotFoundError):
        store.require_directory(job.job_id)


def test_discard_unlinks_job_shaped_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    """Discard must remove a hostile link entry while preserving outside data."""
    store = _store(tmp_path)
    outside = tmp_path / "outside sentinel"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    job_id = "d" * 32
    link = store.root / job_id
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    store.discard(job_id)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not os.path.lexists(link)


def test_cleanup_unlinks_job_shaped_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    """Orphan cleanup must not retain or traverse an invalid job link."""
    store = _store(tmp_path)
    outside = tmp_path / "outside cleanup sentinel"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    job_id = "e" * 32
    link = store.root / job_id
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    removed = store.cleanup_orphans(
        cutoff=datetime.now(UTC),
        active_job_ids=set(),
    )

    assert removed == (job_id,)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not os.path.lexists(link)


@pytest.mark.skipif(  # type: ignore[untyped-decorator]
    os.name != "nt", reason="Windows junction semantics"
)
def test_discard_and_cleanup_unlink_junctions_without_touching_target(
    tmp_path: Path,
) -> None:
    """Windows reparse-point cleanup must never recurse into outside targets."""
    store = _store(tmp_path)
    outside = tmp_path / "junction sentinel"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    discard_id = "a" * 32
    cleanup_id = "b" * 32
    for job_id in (discard_id, cleanup_id):
        completed = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "mklink",
                "/J",
                str(store.root / job_id),
                str(outside),
            ],
            shell=False,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if completed.returncode != 0:
            pytest.skip("creating a Windows junction is unavailable")

    store.discard(discard_id)
    removed = store.cleanup_orphans(
        cutoff=datetime.now(UTC),
        active_job_ids=set(),
    )

    assert removed == (cleanup_id,)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not os.path.lexists(store.root / discard_id)
    assert not os.path.lexists(store.root / cleanup_id)


def test_cleanup_orphans_removes_only_expired_untracked_job_directories(
    tmp_path: Path,
) -> None:
    """A cleanup bug could erase a live job or unrelated application data."""
    store = _store(tmp_path)
    active = store.reserve("active.mp4")
    orphan = store.reserve("orphan.mp4")
    recent = store.reserve("recent.mp4")
    unrelated = store.root / "notes"
    unrelated.mkdir()
    cutoff = datetime.now(UTC) - timedelta(minutes=5)
    old_timestamp = (cutoff - timedelta(seconds=1)).timestamp()
    os.utime(active.directory, (old_timestamp, old_timestamp))
    os.utime(orphan.directory, (old_timestamp, old_timestamp))

    removed = store.cleanup_orphans(cutoff=cutoff, active_job_ids={active.job_id})

    assert removed == (orphan.job_id,)
    assert active.directory.exists()
    assert not orphan.directory.exists()
    assert recent.directory.exists()
    assert unrelated.exists()


def test_concurrent_reservations_create_unique_directories(tmp_path: Path) -> None:
    """A collision or non-atomic reserve could merge two local uploads."""
    store = _store(tmp_path)

    with ThreadPoolExecutor(max_workers=8) as executor:
        jobs = list(executor.map(lambda _: store.reserve("clip.mp4"), range(32)))

    assert len({job.job_id for job in jobs}) == 32
    assert all(
        job.directory.is_dir() and job.directory.parent == store.root for job in jobs
    )
