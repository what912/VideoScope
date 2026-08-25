"""Audit a frozen Windows connector bundle before installer packaging."""

from __future__ import annotations

import argparse
import re
import stat
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any

if __package__:
    from scripts.audit_dependency_licenses import (
        EXPECTED_BUNDLE_MATERIALS,
        EXPECTED_PYTHON_RUNTIME_LOCK,
        canonicalize_distribution_name,
        is_safe_relative_policy_path,
        parse_python_lock,
        parse_runtime_distribution_policy,
        read_json_object,
        sha256_canonical_text,
    )
else:
    from audit_dependency_licenses import (  # type: ignore[import-not-found,no-redef]
        EXPECTED_BUNDLE_MATERIALS,
        EXPECTED_PYTHON_RUNTIME_LOCK,
        canonicalize_distribution_name,
        is_safe_relative_policy_path,
        parse_python_lock,
        parse_runtime_distribution_policy,
        read_json_object,
        sha256_canonical_text,
    )

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
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPOSITORY_ROOT / "packaging" / "windows" / "license-policy.json"
LICENSE_FILENAME_MARKERS = ("license", "licence", "copying", "notice")
LEGACY_LICENSE_VALUE_MAP = {
    "Apache 2.0": "Apache-2.0",
    "ISC License": "ISC",
    "MIT": "MIT",
    "MPL-2.0 AND MIT": "MPL-2.0 AND MIT",
}
LEGACY_LICENSE_CLASSIFIER_MAP = {
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "License :: OSI Approved :: BSD License": "BSD-3-Clause",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: MIT License": "MIT",
}


def _read_policy(policy_path: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    policy, error = read_json_object(policy_path, "repository license policy")
    return (policy, ()) if error is None else ({}, (error,))


def _marker_license_material_files(metadata: Path) -> dict[str, Path]:
    return {
        path.relative_to(metadata).as_posix(): path
        for path in metadata.rglob("*")
        if path.is_file()
        and any(marker in path.name.casefold() for marker in LICENSE_FILENAME_MARKERS)
    }


def _license_material_files(
    metadata_directory: Path,
    metadata: Message,
) -> tuple[dict[str, Path], tuple[str, ...]]:
    """Resolve every declared License-File plus bounded legacy markers."""
    marker_files = _marker_license_material_files(metadata_directory)
    declarations = metadata.get_all("License-File", [])
    if not declarations:
        return marker_files, ()

    violations: list[str] = []
    declared_files: dict[str, Path] = {}
    seen_declarations: set[str] = set()
    for raw_declaration in declarations:
        declaration = raw_declaration.strip()
        if (
            not declaration
            or not is_safe_relative_policy_path(declaration)
            or declaration in seen_declarations
        ):
            violations.append(f"invalid License-File declaration: {raw_declaration!r}")
            continue
        seen_declarations.add(declaration)
        declaration_parts = Path(*declaration.split("/"))
        candidates = (
            metadata_directory / "licenses" / declaration_parts,
            metadata_directory / declaration_parts,
        )
        existing = [
            candidate for candidate in dict.fromkeys(candidates) if candidate.is_file()
        ]
        if len(existing) != 1:
            violations.append(
                "License-File declaration must resolve to exactly one file: "
                f"{declaration}"
            )
            continue
        resolved = existing[0]
        declared_files[resolved.relative_to(metadata_directory).as_posix()] = resolved
    return marker_files | declared_files, tuple(violations)


def _single_nonempty_header(metadata: Message, header: str) -> str | None:
    values = metadata.get_all(header, [])
    if len(values) != 1:
        return None
    value = values[0].strip()
    return value or None


def normalized_metadata_license_identifier(metadata_bytes: bytes) -> str | None:
    """Return a deterministic reviewed identifier from modern or legacy metadata."""
    metadata = BytesParser().parsebytes(metadata_bytes)
    expression_headers = metadata.get_all("License-Expression", [])
    legacy_headers = metadata.get_all("License", [])
    if expression_headers:
        if legacy_headers or len(expression_headers) != 1:
            return None
        expression = expression_headers[0].strip()
        return expression or None
    if legacy_headers:
        if len(legacy_headers) != 1:
            return None
        legacy_value = legacy_headers[0].strip()
        return LEGACY_LICENSE_VALUE_MAP.get(legacy_value)

    license_classifiers = [
        classifier.strip()
        for classifier in metadata.get_all("Classifier", [])
        if classifier.strip().startswith("License ::")
    ]
    if not license_classifiers or len(set(license_classifiers)) != len(
        license_classifiers
    ):
        return None
    if any(
        classifier not in LEGACY_LICENSE_CLASSIFIER_MAP
        for classifier in license_classifiers
    ):
        return None
    classifier_identifiers = {
        LEGACY_LICENSE_CLASSIFIER_MAP[classifier] for classifier in license_classifiers
    }
    if len(classifier_identifiers) == 1:
        return classifier_identifiers.pop()
    return None


def _preflight_unsafe_links(root: Path) -> tuple[str, ...]:
    """Reject links and Windows reparse points before any bundle file read."""
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            metadata = current.lstat()
        except OSError as error:
            return (
                f"unable to inspect bundle path before reading: {current}: {error}",
            )
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & reparse_flag:
            return (f"unsafe link or reparse point: {current}",)
        if stat.S_ISDIR(metadata.st_mode):
            try:
                pending.extend(
                    sorted(current.iterdir(), key=lambda path: path.name, reverse=True)
                )
            except OSError as error:
                return (
                    f"unable to enumerate bundle path before reading: "
                    f"{current}: {error}",
                )
    return ()


def _audit_license_inventory(
    resolved: Path,
    repository_root: Path,
    policy_path: Path,
) -> tuple[str, ...]:
    violations: list[str] = []
    policy, policy_violations = _read_policy(policy_path)
    if policy_violations:
        return policy_violations
    reviewed = policy.get("reviewed_license_identifiers")
    lock_relative = policy.get("python_runtime_lock")
    materials = policy.get("bundle_materials")
    runtime_distributions = policy.get("python_runtime_distributions")
    if (
        not isinstance(reviewed, list)
        or any(not isinstance(item, str) or not item for item in reviewed)
        or not isinstance(lock_relative, str)
        or not isinstance(materials, list)
        or not isinstance(runtime_distributions, list)
    ):
        return ("repository license policy is incomplete",)
    if (
        not is_safe_relative_policy_path(lock_relative)
        or lock_relative != EXPECTED_PYTHON_RUNTIME_LOCK
    ):
        return ("unsafe or unexpected repository Python runtime lock path",)
    expected, lock_violations = parse_python_lock(
        repository_root / lock_relative,
        frozenset(reviewed),
    )
    violations.extend(lock_violations)
    runtime_policy, runtime_policy_violations = parse_runtime_distribution_policy(
        runtime_distributions,
        expected,
        frozenset(reviewed),
    )
    violations.extend(runtime_policy_violations)
    license_root = resolved / "_internal" / "licenses"
    bundled_policy = license_root / "license-policy.json"
    if not bundled_policy.is_file():
        violations.append(
            "missing bundled license policy: _internal/licenses/license-policy.json"
        )
    else:
        _, bundled_policy_error = read_json_object(
            bundled_policy,
            "bundled license policy",
        )
        if bundled_policy_error is not None:
            violations.append(bundled_policy_error)
        try:
            bundled_policy_digest = sha256_canonical_text(bundled_policy)
            repository_policy_digest = sha256_canonical_text(policy_path)
        except (OSError, UnicodeError) as error:
            violations.append(f"unable to hash bundled license policy: {error}")
        else:
            if bundled_policy_digest != repository_policy_digest:
                violations.append("bundled license policy digest mismatch")
    allowed_license_root_names = {"license-policy.json"}
    material_pairs: set[tuple[str, str]] = set()
    material_sources: set[str] = set()
    material_bundle_names: set[str] = set()
    for material in materials:
        if not isinstance(material, dict):
            violations.append("invalid repository bundle material policy")
            continue
        source = material.get("source")
        bundle_name = material.get("bundle_name")
        expected_digest = material.get("sha256")
        if (
            not isinstance(source, str)
            or not isinstance(bundle_name, str)
            or not isinstance(expected_digest, str)
        ):
            violations.append("invalid repository bundle material policy")
            continue
        if (
            not is_safe_relative_policy_path(source)
            or not is_safe_relative_policy_path(bundle_name)
            or "/" in bundle_name
        ):
            violations.append("unsafe repository bundle material policy path")
            continue
        if source in material_sources:
            violations.append(f"duplicate repository bundle material source: {source}")
        if bundle_name in material_bundle_names:
            violations.append(
                f"duplicate repository bundle material name: {bundle_name}"
            )
        material_sources.add(source)
        material_bundle_names.add(bundle_name)
        material_pairs.add((source, bundle_name))
        allowed_license_root_names.add(bundle_name)
        bundled = license_root / bundle_name
        if not bundled.is_file():
            violations.append(f"missing bundled license material: {bundle_name}")
        else:
            try:
                actual_digest = sha256_canonical_text(bundled)
            except (OSError, UnicodeError) as error:
                violations.append(
                    f"unable to hash bundled license material {bundle_name}: {error}"
                )
            else:
                if actual_digest != expected_digest:
                    violations.append(
                        f"license material digest mismatch: {bundle_name}"
                    )
    if material_pairs != EXPECTED_BUNDLE_MATERIALS:
        violations.append("repository bundle material policy scope mismatch")
    if license_root.is_dir():
        actual_license_root_names = {child.name for child in license_root.iterdir()}
        for unexpected_name in sorted(
            actual_license_root_names - allowed_license_root_names,
            key=str.casefold,
        ):
            violations.append(
                f"unexpected bundled license-root entry: {unexpected_name}"
            )

    metadata_by_name: dict[str, tuple[str, str, str | None, Path, Message]] = {}
    internal = resolved / "_internal"
    for metadata_directory in sorted(
        internal.glob("*.dist-info"), key=lambda item: item.name.casefold()
    ):
        metadata_file = metadata_directory / "METADATA"
        if not metadata_file.is_file():
            violations.append(
                f"runtime distribution metadata missing: {metadata_directory.name}"
            )
            continue
        try:
            metadata_bytes = metadata_file.read_bytes()
            metadata = BytesParser().parsebytes(metadata_bytes)
        except OSError as error:
            violations.append(
                f"unable to read runtime distribution metadata: "
                f"{metadata_directory.name}: {error}"
            )
            continue
        name = _single_nonempty_header(metadata, "Name")
        version = _single_nonempty_header(metadata, "Version")
        if name is None:
            violations.append(
                f"runtime distribution Name header invalid: {metadata_directory.name}"
            )
            continue
        if version is None:
            violations.append(
                "runtime distribution Version header invalid: "
                f"{metadata_directory.name}"
            )
            continue
        canonical_name = canonicalize_distribution_name(name)
        if canonical_name in metadata_by_name:
            violations.append(f"duplicate runtime distribution metadata: {name}")
            continue
        license_identifier = normalized_metadata_license_identifier(metadata_bytes)
        metadata_by_name[canonical_name] = (
            name,
            version,
            license_identifier,
            metadata_directory,
            metadata,
        )

    for canonical_name, (expected_name, expected_version, expected_license) in sorted(
        expected.items()
    ):
        actual = metadata_by_name.pop(canonical_name, None)
        if actual is None:
            violations.append(f"runtime distribution metadata missing: {expected_name}")
            continue
        _, actual_version, actual_license, metadata_directory, metadata = actual
        if actual_version != expected_version:
            violations.append(
                f"runtime distribution version mismatch: {expected_name}: "
                f"expected {expected_version}, found {actual_version}"
            )
        if actual_license != expected_license:
            violations.append(
                f"runtime distribution license identity mismatch: {expected_name}: "
                f"expected {expected_license}, found {actual_license or 'missing'}"
            )
        policy_entry = runtime_policy.get(canonical_name)
        if policy_entry is None:
            continue
        expected_files = policy_entry["license_files"]
        actual_files, declaration_violations = _license_material_files(
            metadata_directory,
            metadata,
        )
        violations.extend(
            f"{expected_name}: {violation}" for violation in declaration_violations
        )
        if set(actual_files) != set(expected_files):
            violations.append(
                f"runtime distribution license material set mismatch: {expected_name}"
            )
        for relative in sorted(set(actual_files) & set(expected_files)):
            try:
                actual_digest = sha256_canonical_text(actual_files[relative])
            except (OSError, UnicodeError) as error:
                violations.append(
                    "unable to hash runtime distribution license material: "
                    f"{expected_name}: {relative}: {error}"
                )
            else:
                if actual_digest != expected_files[relative]:
                    violations.append(
                        "runtime distribution license material digest mismatch: "
                        f"{expected_name}: {relative}"
                    )
    for _, (name, version, _, _, _) in sorted(metadata_by_name.items()):
        violations.append(
            f"unexpected runtime distribution metadata: {name}=={version}"
        )
    return tuple(violations)


def audit_bundle(
    root: Path,
    *,
    repository_root: Path = REPOSITORY_ROOT,
    policy_path: Path = POLICY_PATH,
) -> tuple[str, ...]:
    """Return deterministic human-readable violations."""
    violations: list[str] = []
    unsafe_links = _preflight_unsafe_links(root)
    if unsafe_links:
        return unsafe_links
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        return ("bundle path is not a directory",)
    for required in REQUIRED_PATHS:
        if not (resolved / required).is_file():
            violations.append(f"missing required runtime asset: {required.as_posix()}")
    violations.extend(
        _audit_license_inventory(
            resolved,
            repository_root.resolve(),
            policy_path,
        )
    )
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
