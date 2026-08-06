"""Strict private/public layout and atomic useful-content publication."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from videoscope.content.errors import (
    ContentArtifactError,
    ContentCancelledError,
)
from videoscope.content.models import (
    ContentArtifact,
    ContentArtifactRole,
    ContentOutcome,
    ContentPlan,
    ContentVerificationReport,
)
from videoscope.video.hashing import compute_file_sha256

PRIVATE_ROOT_NAME = "content-review-private"
PENDING_ROOT_NAME = "content-pending"
PUBLIC_ROOT_NAME = "content-output"

_PRIVATE_FILES = frozenset(
    {
        "content-map.json",
        "storyboard.json",
        "plan.json",
        "transcript-normalized.json",
    }
)
_PRIVATE_DIRECTORIES = frozenset({"preview", "evidence"})
_PUBLIC_FIXED = frozenset(
    {
        "useful-content.mp4",
        "chapters.json",
        "source-map.json",
        "changes.json",
        "technical-report.json",
        "report.html",
        "subtitles.srt",
    }
)


@dataclass(frozen=True, slots=True)
class ContentArtifactLayout:
    job_root: Path
    private_root: Path
    pending_root: Path
    public_root: Path

    @classmethod
    def create(cls, job_root: Path) -> ContentArtifactLayout:
        root = Path(job_root)
        _reject_link(root)
        root.mkdir(parents=True, exist_ok=True)
        _reject_link(root)
        resolved = root.resolve(strict=True)
        private = resolved / PRIVATE_ROOT_NAME
        pending = resolved / PENDING_ROOT_NAME
        public = resolved / PUBLIC_ROOT_NAME
        if public.exists() or public.is_symlink():
            _reject_link(public)
            if not public.is_dir():
                raise ContentArtifactError("content public root is not a directory")
        private.mkdir(exist_ok=True)
        _reject_link(private)
        return cls(resolved, private, pending, public)

    def write_private_text(self, relative_path: str, content: str) -> Path:
        _require_private_name(relative_path)
        destination = self.private_root / PurePosixPath(relative_path)
        _require_within(self.private_root, destination)
        _reject_existing_links(destination.parent, stop=self.private_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic_text(destination, content)
        return destination

    def validate_private_tree(self) -> tuple[str, ...]:
        values: list[str] = []
        for entry in sorted(self.private_root.rglob("*")):
            _reject_link(entry)
            if entry.is_dir():
                continue
            relative = entry.relative_to(self.private_root).as_posix()
            _require_private_name(relative)
            values.append(relative)
        return tuple(values)

    def cleanup_private(self) -> None:
        for root in (self.private_root, self.pending_root):
            if root.exists() or root.is_symlink():
                _reject_link(root)
                shutil.rmtree(root)

    def expire_private(
        self, *, maximum_age_seconds: float, now: float | None = None
    ) -> bool:
        if maximum_age_seconds <= 0:
            raise ValueError("maximum age must be positive")
        if not self.private_root.exists():
            return False
        current = time.time() if now is None else now
        if current - self.private_root.stat().st_mtime < maximum_age_seconds:
            return False
        self.cleanup_private()
        return True


def publish_verified_content(
    layout: ContentArtifactLayout,
    *,
    plan: ContentPlan,
    verification: ContentVerificationReport,
    file_sources: Mapping[str, Path],
    text_documents: Mapping[str, str],
    cancellation_callback: Callable[[], bool] | None = None,
) -> tuple[ContentArtifact, ...]:
    """Publish one exact verified bundle through a single directory rename."""
    _validate_publication_inputs(
        layout,
        plan=plan,
        verification=verification,
        file_sources=file_sources,
        text_documents=text_documents,
    )
    cancelled = cancellation_callback or (lambda: False)
    if cancelled():
        raise ContentCancelledError("content publication cancelled")
    transaction = Path(
        tempfile.mkdtemp(prefix=".content-output-publish-", dir=layout.job_root)
    )
    transaction_for_cleanup: Path | None = transaction
    try:
        for declared in plan.public_artifacts:
            relative = _strip_public_prefix(declared)
            destination = transaction / PurePosixPath(relative)
            _require_within(transaction, destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if cancelled():
                raise ContentCancelledError("content publication cancelled")
            if declared in file_sources:
                source = Path(file_sources[declared])
                _require_regular_file(source)
                shutil.copyfile(source, destination)
            else:
                _write_atomic_text(destination, text_documents[declared])
            _require_regular_file(destination)
        _validate_transaction_tree(transaction, set(plan.public_artifacts))
        if layout.public_root.exists() or layout.public_root.is_symlink():
            raise ContentArtifactError("content public root already exists")
        transaction.replace(layout.public_root)
        transaction_for_cleanup = None
        return tuple(
            _artifact_for(layout.public_root, declared)
            for declared in plan.public_artifacts
        )
    finally:
        if transaction_for_cleanup is not None and transaction_for_cleanup.exists():
            shutil.rmtree(transaction_for_cleanup)


def validate_public_tree(
    layout: ContentArtifactLayout,
    plan: ContentPlan,
) -> tuple[str, ...]:
    if not layout.public_root.exists():
        return ()
    _reject_link(layout.public_root)
    expected = set(plan.public_artifacts)
    _validate_transaction_tree(layout.public_root, expected)
    return plan.public_artifacts


def _validate_publication_inputs(
    layout: ContentArtifactLayout,
    *,
    plan: ContentPlan,
    verification: ContentVerificationReport,
    file_sources: Mapping[str, Path],
    text_documents: Mapping[str, str],
) -> None:
    ContentPlan.model_validate(plan.model_dump(mode="python"))
    ContentVerificationReport.model_validate(verification.model_dump(mode="python"))
    if verification.plan_digest != plan.plan_digest:
        raise ContentArtifactError("verification belongs to another content plan")
    if verification.outcome not in {ContentOutcome.COMPLETED, ContentOutcome.PARTIAL}:
        raise ContentArtifactError("content verification does not permit publication")
    if layout.public_root.exists() or layout.public_root.is_symlink():
        raise ContentArtifactError("content public root already exists")
    supplied = set(file_sources) | set(text_documents)
    expected = set(plan.public_artifacts)
    if supplied != expected or set(file_sources) & set(text_documents):
        raise ContentArtifactError(
            "content public bundle does not match the exact plan"
        )
    for declared in expected:
        _require_public_name(declared)
    for declared, source in file_sources.items():
        del declared
        _require_regular_file(Path(source))
    for declared, content in text_documents.items():
        if declared.endswith((".json", ".html", ".srt")):
            _reject_private_leak(content)


def _validate_transaction_tree(root: Path, expected: set[str]) -> None:
    actual: set[str] = set()
    for entry in sorted(root.rglob("*")):
        _reject_link(entry)
        if entry.is_dir():
            continue
        _require_regular_file(entry)
        declared = f"{PUBLIC_ROOT_NAME}/{entry.relative_to(root).as_posix()}"
        _require_public_name(declared)
        actual.add(declared)
    if actual != expected:
        raise ContentArtifactError("content public tree is incomplete or unexpected")


def _artifact_for(root: Path, declared: str) -> ContentArtifact:
    relative = _strip_public_prefix(declared)
    path = root / PurePosixPath(relative)
    suffix = path.suffix.casefold()
    if suffix == ".mp4":
        role = (
            ContentArtifactRole.CLIP
            if relative.startswith("clips/")
            else ContentArtifactRole.MEDIA
        )
    elif suffix == ".srt":
        role = ContentArtifactRole.SUBTITLE
    else:
        role = ContentArtifactRole.DOCUMENT
    return ContentArtifact(
        role=role,
        relative_path=declared,
        sha256=compute_file_sha256(path),
        description=f"Verified useful-content {role.value} artifact.",
    )


def _require_private_name(relative: str) -> None:
    _require_safe_relative(relative)
    path = PurePosixPath(relative)
    if relative in _PRIVATE_FILES:
        return
    if path.parts and path.parts[0] in _PRIVATE_DIRECTORIES and len(path.parts) > 1:
        return
    raise ContentArtifactError("private content artifact is not allowlisted")


def _require_public_name(declared: str) -> None:
    if not declared.startswith(f"{PUBLIC_ROOT_NAME}/"):
        raise ContentArtifactError("public content artifact lacks the fixed root")
    relative = _strip_public_prefix(declared)
    _require_safe_relative(relative)
    if relative in _PUBLIC_FIXED:
        return
    path = PurePosixPath(relative)
    if (
        len(path.parts) == 2
        and path.parts[0] == "clips"
        and (
            path.name == "manifest.json"
            or path.name.startswith("clip-")
            and path.suffix.casefold() == ".mp4"
        )
    ):
        return
    raise ContentArtifactError("public content artifact is not allowlisted")


def _strip_public_prefix(declared: str) -> str:
    return declared.removeprefix(f"{PUBLIC_ROOT_NAME}/")


def _require_safe_relative(value: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or "\\" in value
        or value != path.as_posix()
    ):
        raise ContentArtifactError("content artifact path is not safely relative")


def _reject_private_leak(content: str) -> None:
    lowered = content.casefold()
    if PRIVATE_ROOT_NAME in lowered or PENDING_ROOT_NAME in lowered:
        raise ContentArtifactError("public content document references private data")
    if "http://" in lowered or "https://" in lowered:
        raise ContentArtifactError("public content report cannot load remote resources")
    for token in content.replace("\r", " ").replace("\n", " ").split():
        clean = token.strip("\"'<>(),[]{}")
        if PureWindowsPath(clean).drive or clean.startswith(("/home/", "/Users/")):
            raise ContentArtifactError(
                "public content document contains an absolute path"
            )


def _reject_existing_links(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop:
        if current.exists() or current.is_symlink():
            _reject_link(current)
        current = current.parent


def _reject_link(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    info = os.lstat(path)
    attributes = getattr(info, "st_file_attributes", 0)
    if stat.S_ISLNK(info.st_mode) or attributes & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    ):
        raise ContentArtifactError("content artifact path cannot use a link")


def _require_regular_file(path: Path) -> None:
    _reject_link(path)
    if not path.is_file() or path.stat().st_size <= 0:
        raise ContentArtifactError("content artifact must be a non-empty regular file")


def _require_within(root: Path, path: Path) -> None:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ContentArtifactError("content artifact escaped its fixed root") from exc


def _write_atomic_text(path: Path, content: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            if not content.endswith("\n"):
                stream.write("\n")
            stream.flush()
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


__all__ = [
    "ContentArtifactLayout",
    "PENDING_ROOT_NAME",
    "PRIVATE_ROOT_NAME",
    "PUBLIC_ROOT_NAME",
    "publish_verified_content",
    "validate_public_tree",
]
