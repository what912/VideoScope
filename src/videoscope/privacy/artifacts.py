"""Physical isolation and publication checks for Safe Sharing artifacts."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import unquote

from pydantic import JsonValue

from videoscope.privacy.errors import PrivacyArtifactError

PRIVATE_ROOT_NAME = "privacy-review-private"
PUBLIC_ROOT_NAME = "share-package"

_PUBLIC_TOP_LEVEL_FILES = frozenset(
    {
        "changes.json",
        "manifest.json",
        "privacy-summary.json",
        "share-safe.mp4",
        "technical-report.json",
        "verification.json",
    }
)
_PUBLIC_PREVIEW_FILES = frozenset(
    {
        "preview/after.jpg",
        "preview/after.png",
        "preview/before.jpg",
        "preview/before.png",
        "preview/privacy-preview.mp4",
    }
)
_TEXT_SUFFIXES = frozenset({".csv", ".html", ".json", ".md", ".txt"})
_SENSITIVE_KEYS = frozenset(
    {
        "absolute_path",
        "gps",
        "input_path",
        "latitude",
        "longitude",
        "ocr_text",
        "private_evidence",
        "raw_probe",
        "raw_text",
        "sanitized_metadata_key",
        "sanitized_metadata_value",
        "source_path",
        "user_name",
        "username",
    }
)
_WINDOWS_ABSOLUTE = re.compile(r"(?i)(?:^|[\s\"'])(?:[a-z]:[\\/]|\\\\)")
_POSIX_ABSOLUTE = re.compile(r"(?:^|[\s\"'])/(?!/)[^\s\"']+")
_PERSONAL_POSIX = re.compile(r"(?:^|[\s\"'])/(?:home|Users|private|tmp)/")
_FILE_URI = re.compile(r"(?i)(?:^|[\s\"'])file\s*:")
_MAX_PERCENT_DECODE_ROUNDS = 8
_GPS_PAIR = re.compile(
    r"(?<!\d)[+-]?(?:90(?:\.0+)?|[0-8]?\d(?:\.\d+)?)\s*[,;]\s*"
    r"[+-]?(?:180(?:\.0+)?|1[0-7]\d(?:\.\d+)?|\d?\d(?:\.\d+)?)(?!\d)"
)
_ISO_6709 = re.compile(r"[+-]\d{2,3}(?:\.\d+)?[+-]\d{3}(?:\.\d+)?/")


@dataclass(frozen=True, slots=True)
class PrivacyArtifactLayout:
    """Validated private/public roots for one Safe Sharing job."""

    job_root: Path
    private_root: Path
    public_root: Path

    @classmethod
    def create(cls, root: Path) -> PrivacyArtifactLayout:
        """Create exactly the two artifact roots below a non-link job root."""
        job_root = Path(root)
        _reject_link_components(job_root)
        job_root.mkdir(parents=True, exist_ok=True)
        _reject_link_components(job_root)
        resolved_root = job_root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise PrivacyArtifactError("Safe Sharing job root is not a directory")
        private_root = resolved_root / PRIVATE_ROOT_NAME
        public_root = resolved_root / PUBLIC_ROOT_NAME
        private_root.mkdir(exist_ok=True)
        public_root.mkdir(exist_ok=True)
        _reject_link_components(private_root)
        _reject_link_components(public_root)
        return cls(
            job_root=resolved_root,
            private_root=private_root.resolve(strict=True),
            public_root=public_root.resolve(strict=True),
        )

    def public_relative_path(self, path: Path) -> str:
        """Return an allowlisted public path or reject any private/linked escape."""
        candidate = Path(path)
        _reject_link_components(candidate)
        try:
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(self.public_root)
        except (OSError, ValueError) as exc:
            raise PrivacyArtifactError("artifact is outside the public root") from exc
        relative_path = relative.as_posix()
        if not _is_allowlisted_public_path(relative_path):
            raise PrivacyArtifactError("public artifact filename is not allowlisted")
        return relative_path

    def validate_share_manifest(self, manifest: Mapping[str, JsonValue]) -> None:
        """Reject public JSON values containing private fields or local identifiers."""
        validate_public_manifest(manifest)

    def validate_public_tree(self) -> tuple[str, ...]:
        """Validate every published file and return stable relative paths."""
        files: list[str] = []
        _reject_link_components(self.public_root)
        for candidate in sorted(self.public_root.rglob("*")):
            if candidate.is_dir():
                _reject_link_components(candidate)
                continue
            relative = self.public_relative_path(candidate)
            if candidate.suffix.casefold() in _TEXT_SUFFIXES:
                try:
                    content = candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise PrivacyArtifactError(
                        "public text artifact is unreadable"
                    ) from exc
                if candidate.suffix.casefold() == ".json":
                    try:
                        decoded: object = json.loads(content)
                    except json.JSONDecodeError as exc:
                        raise PrivacyArtifactError(
                            "public JSON artifact is invalid"
                        ) from exc
                    _validate_public_value(decoded)
                else:
                    _validate_public_text(content)
            files.append(relative)
        return tuple(files)


def validate_public_manifest(manifest: Mapping[str, JsonValue]) -> None:
    """Validate one public JSON manifest without exposing rejected values."""
    _validate_public_value(manifest)


def resolve_existing_job_root(root: Path) -> Path:
    """Resolve an existing non-link Safe Sharing job root without creating it."""
    candidate = Path(root)
    _reject_link_components(candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise PrivacyArtifactError("Safe Sharing job root is unavailable") from exc
    if not resolved.is_dir():
        raise PrivacyArtifactError("Safe Sharing job root is not a directory")
    return resolved


def private_artifact_identity(private_root: Path, path: Path) -> str:
    """Return a stable private-root-relative identity for an internal artifact."""
    root = Path(private_root)
    candidate = Path(path)
    _reject_link_components(root)
    _reject_link_components(candidate)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=False)
        relative = resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PrivacyArtifactError(
            "private artifact is outside the private root"
        ) from exc
    identity = relative.as_posix()
    if not identity or identity == "." or not _is_safe_private_identity(identity):
        raise PrivacyArtifactError("private artifact identity is invalid")
    return identity


def resolve_private_artifact(
    private_root: Path,
    identity: str,
    *,
    require_exists: bool = False,
) -> Path:
    """Resolve an untrusted persisted identity strictly below the private root."""
    if not _is_safe_private_identity(identity):
        raise PrivacyArtifactError("private artifact identity is invalid")
    root = Path(private_root)
    candidate = root.joinpath(*PurePosixPath(identity).parts)
    _reject_link_components(root)
    _reject_link_components(candidate)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=require_exists)
        relative = resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PrivacyArtifactError(
            "private artifact is outside the private root"
        ) from exc
    if not relative.parts:
        raise PrivacyArtifactError("private artifact must be below the private root")
    return resolved_candidate


def _is_allowlisted_public_path(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    if (
        path.is_absolute()
        or PureWindowsPath(relative_path).drive
        or ".." in path.parts
        or "\\" in relative_path
        or relative_path != path.as_posix()
    ):
        return False
    return relative_path in _PUBLIC_TOP_LEVEL_FILES | _PUBLIC_PREVIEW_FILES


def _is_safe_private_identity(identity: str) -> bool:
    if not isinstance(identity, str) or not identity:
        return False
    path = PurePosixPath(identity)
    windows_path = PureWindowsPath(identity)
    return not (
        path.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in path.parts
        or "\\" in identity
        or identity != path.as_posix()
        or path.as_posix() in {"", "."}
    )


def _validate_public_value(value: object) -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key).casefold()
            if key in _SENSITIVE_KEYS:
                raise PrivacyArtifactError("public artifact contains a private field")
            _validate_public_value(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _validate_public_value(item)
        return
    if isinstance(value, str):
        _validate_public_text(value)


def _validate_public_text(value: str) -> None:
    candidate = value
    for decode_round in range(_MAX_PERCENT_DECODE_ROUNDS + 1):
        if _FILE_URI.search(candidate):
            raise PrivacyArtifactError("public artifact contains a local file URI")
        if (
            _WINDOWS_ABSOLUTE.search(candidate)
            or _POSIX_ABSOLUTE.search(candidate)
            or _PERSONAL_POSIX.search(candidate)
        ):
            raise PrivacyArtifactError(
                "public artifact contains an absolute personal path"
            )
        if _GPS_PAIR.search(candidate) or _ISO_6709.search(candidate):
            raise PrivacyArtifactError("public artifact contains GPS-like coordinates")
        decoded = unquote(candidate)
        if decoded == candidate:
            return
        if decode_round == _MAX_PERCENT_DECODE_ROUNDS:
            raise PrivacyArtifactError(
                "public artifact exceeds the safe percent-decoding limit"
            )
        candidate = decoded


def _reject_link_components(path: Path) -> None:
    """Reject symlink or junction components without following them for trust."""
    absolute = path.absolute()
    current = absolute
    while True:
        if _is_link_like(current):
            raise PrivacyArtifactError("artifact path contains a link-like component")
        if current.parent == current:
            break
        current = current.parent


def _is_link_like(path: Path) -> bool:
    """Detect symlinks and Windows reparse points on Python 3.11 and later."""
    try:
        attributes = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as exc:
        raise PrivacyArtifactError("artifact path could not be inspected") from exc
    if stat.S_ISLNK(attributes.st_mode):
        return True
    file_attributes = getattr(attributes, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_flag and file_attributes & reparse_flag)


__all__ = [
    "PRIVATE_ROOT_NAME",
    "PUBLIC_ROOT_NAME",
    "PrivacyArtifactLayout",
    "private_artifact_identity",
    "resolve_existing_job_root",
    "resolve_private_artifact",
    "validate_public_manifest",
]
