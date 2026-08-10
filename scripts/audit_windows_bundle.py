"""Audit a frozen Windows connector bundle before installer packaging."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

BANNED_FILENAMES = {
    "ffmpeg.exe",
    "ffprobe.exe",
    "api-key.txt",
    ".env",
}
BANNED_MODEL_SUFFIXES = {
    ".ckpt",
    ".gguf",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
}
BANNED_DEVELOPMENT_DIRECTORIES = {
    "_pytest",
    "build",
    "mypy",
    "pyinstaller",
    "pytest",
    "ruff",
}
TEXT_SUFFIXES = {".cfg", ".ini", ".json", ".md", ".txt", ".xml", ".yaml", ".yml"}
SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)
REQUIRED_PATHS = (
    Path("VideoScopeConnector.exe"),
    Path("_internal/videoscope/web/static/index.html"),
    Path("_internal/videoscope/reporting/templates/report.html.j2"),
)


def audit_bundle(root: Path) -> tuple[str, ...]:
    """Return deterministic human-readable violations."""
    violations: list[str] = []
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        return ("bundle path is not a directory",)
    for required in REQUIRED_PATHS:
        if not (resolved / required).is_file():
            violations.append(f"missing required runtime asset: {required.as_posix()}")
    internal = resolved / "_internal"
    if internal.is_dir():
        for child in sorted(internal.iterdir(), key=lambda item: item.name.casefold()):
            if (
                child.is_dir()
                and child.name.casefold() in BANNED_DEVELOPMENT_DIRECTORIES
            ):
                violations.append(
                    f"development-only package in frozen runtime: {child.name}"
                )
    for path in sorted(resolved.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            try:
                path.resolve(strict=True).relative_to(resolved)
            except (OSError, ValueError):
                violations.append(
                    f"external symbolic link: {path.relative_to(resolved)}"
                )
            continue
        if not path.is_file():
            continue
        relative = path.relative_to(resolved).as_posix()
        lowered_name = path.name.casefold()
        if lowered_name in BANNED_FILENAMES:
            violations.append(f"prohibited bundled file: {relative}")
        if path.suffix.casefold() in BANNED_MODEL_SUFFIXES:
            violations.append(f"prohibited model weight: {relative}")
        if path.suffix.casefold() in TEXT_SUFFIXES and path.stat().st_size <= 2_000_000:
            data = path.read_bytes()
            if any(pattern.search(data) for pattern in SECRET_PATTERNS):
                violations.append(f"possible embedded secret: {relative}")
    return tuple(violations)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    arguments = parser.parse_args()
    violations = audit_bundle(arguments.bundle)
    if violations:
        print("FAIL Windows bundle audit")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print(
        "PASS Windows bundle contains the connector runtime and no "
        "FFmpeg/model/key assets"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
