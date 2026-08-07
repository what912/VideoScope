"""Strict private/public Rescue roots and rollback-safe atomic publication."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import JsonValue

from videoscope.rescue.errors import RescueArtifactError, RescueCancelledError
from videoscope.rescue.executor import SourceMapping
from videoscope.rescue.models import (
    RescueActionKind,
    RescueArtifact,
    RescuePlan,
    RescueVerificationReport,
    RescueVerificationStatus,
    rescue_public_artifacts,
)

PRIVATE_ROOT_NAME = "rescue-review-private"
PUBLIC_ROOT_NAME = "rescue-output"
_PUBLIC_ALLOWED = frozenset(
    {
        "rescue-plan.json",
        "faithful-rescue.mp4",
        "improved-viewing.mp4",
        "damaged-segments.json",
        "changes.json",
        "verification-report.json",
        "technical-report.json",
        "report.html",
    }
)
_CALLER_DOCUMENTS = frozenset({"changes.json", "technical-report.json", "report.html"})
_WINDOWS_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s<>\"']+")
_UNC_PATH = re.compile(r"(?<![\\])\\\\[^\\/\s<>\"']+[\\/][^\s<>\"']+")
_POSIX_NETWORK_PATH = re.compile(r"(?<![:/])//[^\s<>\"']+")
_UNIX_ABSOLUTE = re.compile(r"(?<![A-Za-z0-9_.:/-])/(?!/)[^\s<>\"']+")
_LONE_ROOT = re.compile(r"(?<!\S)/(?!\S)")


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    relative_path: str
    entry_type: str
    identity: tuple[int, int] | None
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _TreeManifest:
    root_identity: tuple[int, int]
    entries: tuple[_TreeEntry, ...]
    digest: str


class _PublicHTMLPrivacyParser(HTMLParser):
    """Inspect visible text and attributes without mistaking closing tags for paths."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)

    def handle_data(self, data: str) -> None:
        _reject_filesystem_tokens(data)

    def handle_comment(self, data: str) -> None:
        _reject_filesystem_tokens(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del tag
        for name, value in attrs:
            if value is None:
                continue
            if name.casefold() in {"href", "src"}:
                if _safe_report_relative_url(value):
                    continue
                raise RescueArtifactError("public report URL is not safely relative")
            _reject_filesystem_tokens(value)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_decl(self, decl: str) -> None:
        _reject_filesystem_tokens(decl)

    def handle_pi(self, data: str) -> None:
        _reject_filesystem_tokens(data)


@dataclass(frozen=True, slots=True)
class RescueArtifactLayout:
    job_root: Path
    private_root: Path
    public_root: Path
    _job_identity: tuple[int, int] = field(repr=False)
    _private_identity: tuple[int, int] = field(repr=False)
    _previews_identity: tuple[int, int] = field(repr=False)
    _staging_identity: tuple[int, int] = field(repr=False)

    @classmethod
    def create(cls, job_root: Path) -> RescueArtifactLayout:
        root = Path(job_root)
        _reject_links(root)
        root.mkdir(parents=True, exist_ok=True)
        _reject_links(root)
        resolved = _resolved_directory(root, "Rescue job root is unavailable")
        private = resolved / PRIVATE_ROOT_NAME
        public = resolved / PUBLIC_ROOT_NAME
        private.mkdir(exist_ok=True)
        previews = private / "previews"
        staging = private / "staging"
        previews.mkdir(exist_ok=True)
        staging.mkdir(exist_ok=True)
        damage_map = private / "damage-map-private.json"
        if not damage_map.exists():
            damage_map.write_text("{}\n", encoding="utf-8")
        _reject_links(private)
        _reject_links(previews)
        _reject_links(staging)
        _reject_links(damage_map)
        _require_private_control_file(damage_map)
        # Public is intentionally absent until the first atomic publication.
        if public.exists() or public.is_symlink():
            _reject_links(public)
            if not public.is_dir():
                raise RescueArtifactError("Rescue public root is not a directory")
        return cls(
            resolved,
            private.resolve(strict=True),
            public,
            _identity(resolved),
            _identity(private),
            _identity(previews),
            _identity(staging),
        )

    def validate_public_manifest(self, manifest: Mapping[str, JsonValue]) -> None:
        def visit(value: object, key: str = "") -> None:
            if isinstance(value, Mapping):
                for item_key, item in value.items():
                    key_text = str(item_key)
                    visit(key_text)
                    visit(item, key_text.lower())
                return
            if isinstance(value, (tuple, list)):
                for item in value:
                    visit(item, key)
                return
            if not isinstance(value, str):
                return
            lower = value.casefold()
            _validate_public_text(value)
            path_key = any(
                token in key for token in ("path", "artifact", "file", "url")
            )
            if (
                PRIVATE_ROOT_NAME.casefold() in lower
                or _is_absolute(value)
                or ".." in PurePosixPath(value).parts
                or ".." in PureWindowsPath(value).parts
                or path_key
                and not (
                    _safe_relative(value)
                    or "url" in key
                    and _safe_report_relative_url(value)
                )
            ):
                raise RescueArtifactError(
                    "public manifest contains a private or unsafe path"
                )

        visit(manifest)

    def validate_public_tree(self) -> tuple[str, ...]:
        _ensure_layout_identity(self)
        if not self.public_root.exists():
            return ()
        _reject_links(self.public_root)
        if not self.public_root.is_dir():
            raise RescueArtifactError("Rescue public root is not a directory")
        names: list[str] = []
        for entry in sorted(self.public_root.rglob("*")):
            _reject_links(entry)
            info = os.lstat(entry)
            if stat.S_ISDIR(info.st_mode):
                raise RescueArtifactError("public output must not contain directories")
            relative = entry.relative_to(self.public_root).as_posix()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not _allowed_public_name(relative)
            ):
                raise RescueArtifactError(
                    "public artifact is not an allowed standalone file"
                )
            names.append(relative)
        return tuple(names)


def publish_verified_rescue(
    layout: RescueArtifactLayout,
    *,
    verification: RescueVerificationReport,
    plan: RescuePlan,
    mappings: Sequence[SourceMapping],
    damaged_ranges: Sequence[tuple[float, float]],
    public_documents: Mapping[str, object],
    cancellation_callback: Callable[[], bool],
) -> tuple[RescueArtifact, ...]:
    """Atomically rename one complete, verified public bundle into visibility."""
    _ensure_layout_identity(layout)
    if layout.public_root.exists() or layout.public_root.is_symlink():
        raise RescueArtifactError("Rescue public root already exists")
    if verification.plan_digest != plan.plan_digest:
        raise RescueArtifactError("verification report is bound to another plan")
    if verification.faithful_status is not RescueVerificationStatus.PASSED:
        return ()
    selected = ["faithful-rescue.mp4"]
    if verification.improved_status in {
        RescueVerificationStatus.PASSED,
        RescueVerificationStatus.NEEDS_REVIEW,
    }:
        selected.append("improved-viewing.mp4")
    expected_declaration = rescue_public_artifacts(
        include_improved="improved-viewing.mp4" in selected
    )
    allowed_declarations = {expected_declaration}
    if "improved-viewing.mp4" not in selected and _plan_has_improvement(plan):
        allowed_declarations.add(rescue_public_artifacts(include_improved=True))
    if plan.public_artifacts not in allowed_declarations:
        raise RescueArtifactError(
            "confirmed Rescue plan does not declare the exact public bundle"
        )
    bindings = {item.relative_path: item for item in verification.artifacts}
    if any(name not in bindings for name in selected):
        raise RescueArtifactError("verification report lacks a selected artifact hash")
    if set(public_documents) != _CALLER_DOCUMENTS:
        raise RescueArtifactError("public Rescue document set is incomplete")
    if cancellation_callback():
        raise RescueCancelledError("Rescue publication was cancelled")
    transaction = Path(
        tempfile.mkdtemp(prefix=".rescue-output-publish-", dir=layout.job_root)
    )
    transaction_for_cleanup: Path | None = transaction
    try:
        _reject_links(transaction)
        generated_documents: dict[str, object] = {
            "rescue-plan.json": plan.model_dump(mode="json"),
            "damaged-segments.json": build_damaged_segments_manifest(
                mappings=mappings, damaged_ranges=damaged_ranges
            ),
            "verification-report.json": verification.model_dump(mode="json"),
            **dict(public_documents),
        }
        for name, content in generated_documents.items():
            _write_public_document(layout, transaction / name, content)
        source_seals: dict[str, _TreeEntry] = {}
        for name in selected:
            source_seals[name] = _copy_verified_file(
                layout.private_root / "staging" / name,
                transaction / name,
                bindings[name].sha256,
                cancellation_callback,
            )
        expected_names = frozenset((*generated_documents, *selected))
        sealed_manifest = _snapshot_transaction_tree(transaction, expected_names)
        _validate_complete_bundle(layout, transaction, expected_names)
        for name in selected:
            source = layout.private_root / "staging" / name
            if _sha256(source) != bindings[name].sha256:
                raise RescueArtifactError("staged Rescue artifact changed during copy")
        _ensure_layout_identity(layout)
        if layout.public_root.exists() or layout.public_root.is_symlink():
            raise RescueArtifactError("Rescue public root appeared during publication")
        if cancellation_callback():
            raise RescueCancelledError("Rescue publication was cancelled")
        _fsync_directory(transaction)
        final_manifest = _snapshot_transaction_tree(transaction, expected_names)
        if final_manifest != sealed_manifest:
            raise RescueArtifactError("Rescue publication transaction changed")
        for name in selected:
            source = layout.private_root / "staging" / name
            current_source = _snapshot_tree_file(source, name)
            if (
                current_source != source_seals[name]
                or current_source.sha256 != bindings[name].sha256
            ):
                raise RescueArtifactError(
                    "staged Rescue artifact changed before publication commit"
                )
        _ensure_layout_identity(layout)
        if cancellation_callback():
            raise RescueCancelledError("Rescue publication was cancelled")
        published = tuple(bindings[name] for name in selected)
        _rename_transaction(transaction, layout.public_root)
        transaction_for_cleanup = None
        return published
    except Exception as exc:
        if isinstance(exc, (RescueArtifactError, RescueCancelledError)):
            raise
        raise RescueArtifactError(
            "Rescue artifacts could not be published atomically"
        ) from exc
    finally:
        if transaction_for_cleanup is not None:
            shutil.rmtree(transaction_for_cleanup, ignore_errors=True)


def _write_public_document(
    layout: RescueArtifactLayout, destination: Path, content: object
) -> None:
    if destination.name.endswith(".json"):
        if isinstance(content, str):
            raise RescueArtifactError("public JSON must be structured data")
        structured = content
        try:
            text = (
                json.dumps(
                    structured,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
        except (TypeError, ValueError) as exc:
            raise RescueArtifactError("public JSON is not canonicalizable") from exc
        payload = json.loads(text)
        if isinstance(payload, Mapping):
            layout.validate_public_manifest(payload)
        else:
            layout.validate_public_manifest({"value": payload})
    elif destination.name == "report.html" and isinstance(content, str):
        text = content
    else:
        raise RescueArtifactError("public Rescue document has an invalid type")
    _validate_public_text(text)
    data = text.encode("utf-8")
    try:
        with destination.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RescueArtifactError("public Rescue document could not be staged") from exc


def _plan_has_improvement(plan: RescuePlan) -> bool:
    kinds = {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.SHARPEN,
        RescueActionKind.DEFLICKER,
        RescueActionKind.STABILIZE,
        RescueActionKind.NORMALIZE_AUDIO,
        RescueActionKind.DENOISE_AUDIO,
        RescueActionKind.CORRECT_FIXED_AV_OFFSET,
    }
    return any(action.kind in kinds for action in plan.actions)


def _copy_verified_file(
    source: Path,
    destination: Path,
    expected_sha256: str,
    cancellation_callback: Callable[[], bool],
) -> _TreeEntry:
    _reject_links(source)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise RescueArtifactError(
            "verified Rescue artifact could not be opened"
        ) from exc
    digest = sha256()
    try:
        initial = os.fstat(descriptor)
        path_info = os.lstat(source)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or initial.st_size <= 0
            or not stat.S_ISREG(path_info.st_mode)
            or (path_info.st_dev, path_info.st_ino) != (initial.st_dev, initial.st_ino)
        ):
            raise RescueArtifactError(
                "verified Rescue artifact is not a standalone regular file"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as source_handle:
            try:
                with destination.open("xb") as destination_handle:
                    while True:
                        if cancellation_callback():
                            raise RescueCancelledError(
                                "Rescue publication was cancelled"
                            )
                        chunk = source_handle.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        destination_handle.write(chunk)
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
            except OSError as exc:
                raise RescueArtifactError(
                    "verified Rescue artifact could not be copied"
                ) from exc
        final_open = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final_path = os.lstat(source)
    identity = (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
    if identity != (
        final_open.st_dev,
        final_open.st_ino,
        final_open.st_size,
        final_open.st_mtime_ns,
    ) or identity != (
        final_path.st_dev,
        final_path.st_ino,
        final_path.st_size,
        final_path.st_mtime_ns,
    ):
        raise RescueArtifactError("verified Rescue artifact changed during copy")
    if digest.hexdigest() != expected_sha256 or _sha256(destination) != expected_sha256:
        raise RescueArtifactError("verified Rescue artifact hash does not match report")
    source_identity = (initial.st_dev, initial.st_ino)
    return _TreeEntry(
        relative_path=source.name,
        entry_type="file",
        identity=source_identity if initial.st_ino else None,
        size=initial.st_size,
        mtime_ns=initial.st_mtime_ns,
        sha256=digest.hexdigest(),
    )


def _snapshot_transaction_tree(
    root: Path, expected_names: frozenset[str]
) -> _TreeManifest:
    """Re-open and hash every transaction member into one stable manifest."""
    _reject_links(root)
    entries: list[_TreeEntry] = []
    for entry in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = entry.relative_to(root).as_posix()
        entries.append(_snapshot_tree_file(entry, relative))
    if {entry.relative_path for entry in entries} != set(expected_names):
        raise RescueArtifactError("public Rescue bundle is incomplete")
    serialized = [
        {
            "relative_path": entry.relative_path,
            "entry_type": entry.entry_type,
            "identity": list(entry.identity) if entry.identity is not None else None,
            "size": entry.size,
            "mtime_ns": entry.mtime_ns,
            "sha256": entry.sha256,
        }
        for entry in entries
    ]
    digest = sha256(
        json.dumps(
            serialized,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return _TreeManifest(_identity(root), tuple(entries), digest)


def _snapshot_tree_file(path: Path, relative_path: str) -> _TreeEntry:
    _reject_links(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RescueArtifactError("public Rescue bundle member is unreadable") from exc
    digest = sha256()
    try:
        initial = os.fstat(descriptor)
        path_initial = os.lstat(path)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_nlink != 1
            or not stat.S_ISREG(path_initial.st_mode)
            or (initial.st_dev, initial.st_ino)
            != (path_initial.st_dev, path_initial.st_ino)
        ):
            raise RescueArtifactError("public Rescue bundle contains a non-file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        final = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    path_final = os.lstat(path)
    identity = (initial.st_dev, initial.st_ino)
    state = (identity, initial.st_size, initial.st_mtime_ns)
    if state != (
        (final.st_dev, final.st_ino),
        final.st_size,
        final.st_mtime_ns,
    ) or state != (
        (path_final.st_dev, path_final.st_ino),
        path_final.st_size,
        path_final.st_mtime_ns,
    ):
        raise RescueArtifactError("public Rescue bundle changed while hashing")
    stable_identity = identity if initial.st_ino else None
    return _TreeEntry(
        relative_path=relative_path,
        entry_type="file",
        identity=stable_identity,
        size=initial.st_size,
        mtime_ns=initial.st_mtime_ns,
        sha256=digest.hexdigest(),
    )


def _validate_complete_bundle(
    layout: RescueArtifactLayout, root: Path, expected_names: frozenset[str]
) -> None:
    _reject_links(root)
    actual: set[str] = set()
    for entry in root.rglob("*"):
        _reject_links(entry)
        info = os.lstat(entry)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RescueArtifactError("public Rescue bundle contains a non-file")
        relative = entry.relative_to(root).as_posix()
        if not _allowed_public_name(relative):
            raise RescueArtifactError(
                "public Rescue bundle contains an unexpected file"
            )
        actual.add(relative)
        if entry.suffix in {".json", ".html", ".txt"}:
            try:
                text = entry.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise RescueArtifactError("public text artifact is unreadable") from exc
            _validate_public_text(text)
            if entry.suffix == ".json":
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise RescueArtifactError(
                        "public JSON artifact is invalid"
                    ) from exc
                if isinstance(payload, Mapping):
                    layout.validate_public_manifest(payload)
                else:
                    layout.validate_public_manifest({"value": payload})
    if actual != set(expected_names):
        raise RescueArtifactError("public Rescue bundle is incomplete")


def _validate_public_text(text: str) -> None:
    folded = text.casefold()
    if PRIVATE_ROOT_NAME.casefold() in folded or "file://" in folded:
        raise RescueArtifactError("public Rescue text contains a private path")
    if "<" in text and ">" in text:
        parser = _PublicHTMLPrivacyParser()
        try:
            parser.feed(text)
            parser.close()
        except (TypeError, ValueError) as exc:
            raise RescueArtifactError("public Rescue text is ambiguous") from exc
        return
    _reject_filesystem_tokens(text)


def _reject_filesystem_tokens(text: str) -> None:
    if (
        _WINDOWS_ABSOLUTE.search(text)
        or _UNC_PATH.search(text)
        or _POSIX_NETWORK_PATH.search(text)
        or _UNIX_ABSOLUTE.search(text)
    ):
        raise RescueArtifactError("public Rescue text contains a private path")
    for match in _LONE_ROOT.finditer(text):
        left = text[: match.start()].rstrip()
        right = text[match.end() :].lstrip()
        if left and right and left[-1].isdigit() and right[0].isdigit():
            continue
        raise RescueArtifactError("public Rescue text contains a private path")


def _safe_report_relative_url(value: str) -> bool:
    if not value or "://" in value or value.startswith(("/", "\\")):
        return False
    path = value.split("#", 1)[0].split("?", 1)[0]
    return not path or _safe_relative(path)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_transaction(source: Path, destination: Path) -> None:
    try:
        _atomic_rename_directory_no_replace(source, destination)
    except RescueArtifactError:
        raise
    except OSError as exc:
        raise RescueArtifactError("Rescue public bundle rename failed") from exc


def _atomic_rename_directory_no_replace(source: Path, destination: Path) -> None:
    """Rename one directory without ever replacing a concurrent destination."""
    if os.name == "nt":
        os.rename(source, destination)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RescueArtifactError("atomic no-replace rename is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            -100,
            os.fsencode(source),
            -100,
            os.fsencode(destination),
            1,
        )
        if result != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise RescueArtifactError("Rescue public root already exists")
            raise OSError(error, os.strerror(error), destination)
        return
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise RescueArtifactError("atomic no-replace rename is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(os.fsencode(source), os.fsencode(destination), 0x00000004)
        if result != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise RescueArtifactError("Rescue public root already exists")
            raise OSError(error, os.strerror(error), destination)
        return
    raise RescueArtifactError("atomic no-replace rename is unavailable")


def deterministic_segment_name(index: int) -> str:
    """Return a stable one-based name for a retained partial-recovery segment."""
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index >= 9999
    ):
        raise ValueError("segment index must be between 0 and 9998")
    return f"faithful-segment-{index + 1:04d}.mp4"


def build_damaged_segments_manifest(
    *,
    mappings: Sequence[SourceMapping],
    damaged_ranges: Sequence[tuple[float, float]],
) -> dict[str, JsonValue]:
    """Build a path-free deterministic partial-recovery trace manifest."""
    ordered_mappings = sorted(
        mappings,
        key=lambda item: (
            item.output_start,
            item.source_start,
            item.output_relative_path,
        ),
    )
    mapping_values: list[JsonValue] = []
    for item in ordered_mappings:
        if not _safe_relative(item.output_relative_path):
            raise RescueArtifactError("source mapping contains an unsafe output path")
        values = (
            item.source_start,
            item.source_end,
            item.output_start,
            item.output_end,
        )
        if any(
            not isinstance(value, (int, float))
            or not math_is_finite(value)
            or value < 0
            for value in values
        ):
            raise RescueArtifactError("source mapping contains invalid seconds")
        mapping_values.append(
            {
                "source_start": float(item.source_start),
                "source_end": float(item.source_end),
                "output_start": float(item.output_start),
                "output_end": float(item.output_end),
                "output_relative_path": item.output_relative_path,
            }
        )
    ranges: list[JsonValue] = []
    for start, end in sorted(damaged_ranges):
        if (
            not math_is_finite(start)
            or not math_is_finite(end)
            or start < 0
            or end < start
        ):
            raise RescueArtifactError("damaged range contains invalid seconds")
        ranges.append([float(start), float(end)])
    return {"damaged_ranges": ranges, "source_mappings": mapping_values}


def _ensure_layout_identity(layout: RescueArtifactLayout) -> None:
    _reject_links(layout.job_root)
    _reject_links(layout.private_root)
    if _identity(layout.job_root) != layout._job_identity:
        raise RescueArtifactError("Rescue job root identity changed")
    if _identity(layout.private_root) != layout._private_identity:
        raise RescueArtifactError("Rescue private root identity changed")
    previews = layout.private_root / "previews"
    staging = layout.private_root / "staging"
    _reject_links(previews)
    _reject_links(staging)
    if _identity(previews) != layout._previews_identity:
        raise RescueArtifactError("Rescue previews root identity changed")
    if _identity(staging) != layout._staging_identity:
        raise RescueArtifactError("Rescue staging root identity changed")
    if (
        layout.private_root.parent != layout.job_root
        or layout.public_root.parent != layout.job_root
    ):
        raise RescueArtifactError("Rescue artifact roots escaped the job root")


def _resolved_directory(path: Path, message: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise RescueArtifactError(message) from exc
    if not resolved.is_dir():
        raise RescueArtifactError(message)
    return resolved


def _identity(path: Path) -> tuple[int, int]:
    try:
        info = path.stat()
    except OSError as exc:
        raise RescueArtifactError("artifact path identity is unavailable") from exc
    return info.st_dev, info.st_ino


def _require_standalone_file(path: Path) -> tuple[int, int]:
    _reject_links(path)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RescueArtifactError("staged Rescue artifact is missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_nlink != 1:
        raise RescueArtifactError(
            "staged Rescue artifact is not a standalone regular file"
        )
    return info.st_dev, info.st_ino


def _require_private_control_file(path: Path) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RescueArtifactError("private Rescue control file is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RescueArtifactError("private Rescue control file is not standalone")


def _allowed_public_name(value: str) -> bool:
    if value in _PUBLIC_ALLOWED:
        return True
    return (
        value.startswith("faithful-segment-")
        and value.endswith(".mp4")
        and len(value) == len("faithful-segment-0001.mp4")
        and value[len("faithful-segment-") : -4].isdigit()
    )


def _safe_relative(value: str) -> bool:
    path, windows = PurePosixPath(value), PureWindowsPath(value)
    return bool(
        value
        and value != "."
        and path.as_posix() == value
        and "\\" not in value
        and not path.is_absolute()
        and not windows.is_absolute()
        and not windows.drive
        and ".." not in path.parts
        and "." not in path.parts
    )


def _is_absolute(value: str) -> bool:
    return (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or bool(PureWindowsPath(value).drive)
    )


def _reject_links(path: Path) -> None:
    current = path.absolute()
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RescueArtifactError("artifact path could not be inspected") from exc
        else:
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise RescueArtifactError(
                    "artifact path contains a link-like component"
                )
        if current.parent == current:
            return
        current = current.parent


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def math_is_finite(value: float) -> bool:
    import math

    return math.isfinite(float(value))


__all__ = [
    "PRIVATE_ROOT_NAME",
    "PUBLIC_ROOT_NAME",
    "RescueArtifactLayout",
    "build_damaged_segments_manifest",
    "deterministic_segment_name",
    "publish_verified_rescue",
]
