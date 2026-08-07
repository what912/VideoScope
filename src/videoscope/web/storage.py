"""Safe local filesystem storage shared by Web job managers."""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePath, PureWindowsPath
from secrets import token_hex

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_SAFE_SUFFIX = re.compile(r"^\.[a-z0-9]{1,10}$")
_RESERVATION_ATTEMPTS = 16


@dataclass(frozen=True, slots=True)
class LocalJobPaths:
    """Filesystem locations allocated for one local job."""

    job_id: str
    directory: Path
    input_path: Path
    output_directory: Path


class LocalJobStore:
    """Own only validated per-job directories below one application root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve(strict=False)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=False)

    def reserve(self, original_filename: str) -> LocalJobPaths:
        """Atomically create a random job directory and normalized input path."""
        suffix = _normalized_suffix(original_filename)
        for _ in range(_RESERVATION_ATTEMPTS):
            job_id = token_hex(16)
            directory = self._job_candidate(job_id)
            try:
                directory.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                continue
            return LocalJobPaths(
                job_id=job_id,
                directory=self.require_directory(job_id),
                input_path=directory / f"input{suffix}",
                output_directory=directory / "artifacts",
            )
        raise RuntimeError("Could not allocate a local job directory")

    def require_directory(self, job_id: str) -> Path:
        """Return an existing ordinary job directory for one canonical ID."""
        directory = self._job_candidate(job_id)
        if _is_link_like(directory) or not directory.is_dir():
            raise FileNotFoundError("Job directory not found")
        return self._require_within_root(directory)

    def resolve_artifact(
        self,
        job_id: str,
        requested_path: str,
        *,
        artifact_root: Path | None = None,
    ) -> Path:
        """Return one existing file strictly below a contained artifact root."""
        directory = self.require_directory(job_id)
        artifact_path = artifact_root or directory / "artifacts"
        if _is_link_like(artifact_path) or not _is_safe_artifact_path(requested_path):
            raise FileNotFoundError("Artifact not found")
        contained_root = self._require_within(artifact_path, directory)
        if not contained_root.is_dir():
            raise FileNotFoundError("Artifact not found")
        candidate = self._require_within(
            contained_root / requested_path,
            contained_root,
        )
        if not candidate.is_file():
            raise FileNotFoundError("Artifact not found")
        return candidate

    def discard(self, job_id: str) -> None:
        """Remove one local job directory without following a symlink."""
        try:
            directory = self._job_candidate(job_id)
        except FileNotFoundError:
            return
        if _is_link_like(directory):
            _unlink_link_like(directory)
            return
        try:
            contained = self.require_directory(job_id)
        except FileNotFoundError:
            return
        shutil.rmtree(contained, ignore_errors=True)

    def cleanup_orphans(
        self,
        *,
        cutoff: datetime,
        active_job_ids: Collection[str],
    ) -> tuple[str, ...]:
        """Delete expired untracked job directories while retaining other data."""
        try:
            children = tuple(self.root.iterdir())
        except OSError:
            return ()
        active = set(active_job_ids)
        removed: list[str] = []
        cutoff_timestamp = cutoff.timestamp()
        for child in children:
            if _JOB_ID.fullmatch(child.name) is None:
                continue
            if child.name in active:
                continue
            if _is_link_like(child):
                _unlink_link_like(child)
                if not os.path.lexists(child):
                    removed.append(child.name)
                continue
            try:
                directory = self.require_directory(child.name)
                if directory.stat().st_mtime > cutoff_timestamp:
                    continue
            except OSError:
                continue
            except FileNotFoundError:
                continue
            self.discard(child.name)
            if not os.path.lexists(directory):
                removed.append(child.name)
        return tuple(sorted(removed))

    def _job_candidate(self, job_id: str) -> Path:
        if _JOB_ID.fullmatch(job_id) is None:
            raise FileNotFoundError("Job directory not found")
        return self.root / job_id

    def _require_within_root(self, path: Path) -> Path:
        return self._require_within(path, self.root)

    @staticmethod
    def _require_within(path: Path, root: Path) -> Path:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError("Local job path not found") from exc
        return resolved


def _normalized_suffix(original_filename: str) -> str:
    suffix = PurePath(original_filename.replace("\\", "/")).suffix.casefold()
    return suffix if _SAFE_SUFFIX.fullmatch(suffix) is not None else ".bin"


def _is_link_like(path: Path) -> bool:
    """Detect symlinks and Windows junction/reparse-point directory entries."""
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _unlink_link_like(path: Path) -> None:
    """Remove only a link entry, never the directory tree it targets."""
    try:
        path.unlink()
        return
    except (IsADirectoryError, PermissionError):
        pass
    except OSError:
        return
    try:
        path.rmdir()
    except OSError:
        pass


def _is_safe_artifact_path(requested_path: str) -> bool:
    if not requested_path:
        return False
    normalized = requested_path.replace("\\", "/")
    if any(not part or part in {".", ".."} for part in normalized.split("/")):
        return False
    local_path = PurePath(requested_path)
    windows_path = PureWindowsPath(requested_path)
    return not local_path.is_absolute() and not windows_path.is_absolute()
