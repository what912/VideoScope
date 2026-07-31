"""Audit built VideoScope archives for release-only distribution contents."""

from __future__ import annotations

import argparse
import re
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

ARCHIVE_SUFFIXES = (".whl", ".tar.gz")
VIDEO_SUFFIXES = {".avi", ".mkv", ".mov", ".mp4", ".webm"}
CACHE_PARTS = {
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
}
TEXT_SUFFIXES = {
    ".cff",
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".j2",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
PERSONAL_PATH_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+", re.IGNORECASE),
    re.compile(r"/Users/[^/\s]+"),
    re.compile(r"/home/[^/\s]+"),
)
MAX_TEXT_SCAN_BYTES = 2_000_000
REQUIRED_WHEEL_MEMBERS = {
    "videoscope/reporting/templates/report.html.j2",
}


def is_distribution(path: Path) -> bool:
    """Return whether a file is a supported distribution archive."""
    return path.name.endswith(ARCHIVE_SUFFIXES)


def distribution_paths(path: Path) -> tuple[Path, ...]:
    """Resolve one archive or all supported archives in a directory."""
    if path.is_file():
        return (path,) if is_distribution(path) else ()
    if not path.is_dir():
        return ()
    return tuple(
        item
        for item in sorted(path.iterdir(), key=lambda item: item.name)
        if item.is_file() and is_distribution(item)
    )


def zip_members(path: Path) -> Iterable[tuple[str, bytes]]:
    """Yield normalized member names and bytes from a wheel."""
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            yield info.filename, archive.read(info)


def tar_members(path: Path) -> Iterable[tuple[str, bytes]]:
    """Yield normalized member names and bytes from a source archive."""
    with tarfile.open(path, mode="r:gz") as archive:
        for info in archive.getmembers():
            if not info.isfile():
                continue
            extracted = archive.extractfile(info)
            if extracted is not None:
                yield info.name, extracted.read()


def archive_members(path: Path) -> Iterable[tuple[str, bytes]]:
    """Yield members from a supported distribution archive."""
    if path.suffix == ".whl":
        yield from zip_members(path)
        return
    if path.name.endswith(".tar.gz"):
        yield from tar_members(path)
        return
    raise ValueError(f"Unsupported distribution archive: {path.name}")


def prohibited_member_reason(name: str) -> str | None:
    """Explain why an archive member must not be distributed."""
    normalized = PurePosixPath(name.replace("\\", "/"))
    parts = tuple(part.casefold() for part in normalized.parts)

    if any(part in CACHE_PARTS for part in parts):
        return "local cache or virtual environment"
    if "runs" in parts or "videoscope-output" in parts:
        return "local analysis output"
    if ("tests", "fixtures", "generated") in zip(parts, parts[1:], parts[2:]):
        return "generated synthetic fixture"
    if normalized.suffix.casefold() in VIDEO_SUFFIXES:
        return "video file"
    if normalized.name.casefold().endswith(".log"):
        return "local log"
    return None


def personal_path_matches(name: str, data: bytes) -> tuple[str, ...]:
    """Return personal absolute path fragments found in a small text member."""
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix not in TEXT_SUFFIXES or len(data) > MAX_TEXT_SCAN_BYTES:
        return ()
    text = data.decode("utf-8", errors="replace")
    matches: list[str] = []
    for pattern in PERSONAL_PATH_PATTERNS:
        matches.extend(match.group(0) for match in pattern.finditer(text))
    return tuple(matches)


def audit_archive(path: Path) -> tuple[str, ...]:
    """Return human-readable violations for one archive."""
    violations: list[str] = []
    scan_content_paths = path.suffix == ".whl"
    member_names: set[str] = set()
    for name, data in archive_members(path):
        member_names.add(name.replace("\\", "/"))
        reason = prohibited_member_reason(name)
        if reason is not None:
            violations.append(f"{name}: {reason}")
        if scan_content_paths:
            for match in personal_path_matches(name, data):
                violations.append(f"{name}: personal absolute path {match!r}")
    if scan_content_paths:
        for required in sorted(REQUIRED_WHEEL_MEMBERS - member_names):
            violations.append(f"{required}: required runtime asset is missing")
    return tuple(violations)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Audit VideoScope wheel and sdist contents.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=Path("dist"),
        help="Distribution archive or directory (default: dist).",
    )
    return parser.parse_args()


def main() -> int:
    """Audit requested archives and print a compact result."""
    args = parse_args()
    archives = distribution_paths(args.path)
    if not archives:
        print(f"No wheel or source archive found at {args.path}.")
        return 2

    failed = False
    for archive in archives:
        violations = audit_archive(archive)
        if violations:
            failed = True
            print(f"FAIL {archive.name}")
            for violation in violations:
                print(f"  - {violation}")
        else:
            print(f"PASS {archive.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
