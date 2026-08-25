"""Audit frozen Python and npm dependency-license identities without network I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

LOCK_ENTRY = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s#]+)"
    r"\s+#\s+SPDX-License-Identifier:\s+(?P<license>.+)$"
)
SHA256 = re.compile(r"[0-9a-f]{64}")
EXPECTED_PYTHON_RUNTIME_LOCK = "packaging/windows/requirements-runtime.lock"
EXPECTED_NPM_LOCK_PATHS = frozenset({"web/package-lock.json", "site/package-lock.json"})
EXPECTED_BUNDLE_MATERIALS = frozenset(
    {
        ("LICENSE", "LICENSE"),
        ("NOTICE", "NOTICE"),
        ("docs/third-party-licenses.md", "third-party-licenses.md"),
        (
            "packaging/windows/requirements-runtime.lock",
            "requirements-runtime.lock",
        ),
    }
)


def canonicalize_distribution_name(name: str) -> str:
    """Return the PEP 503 comparison form used for duplicate detection."""
    return re.sub(r"[-_.]+", "-", name).casefold()


def sha256_file(path: Path) -> str:
    """Return a lowercase SHA-256 digest for one binary file."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_canonical_text(path: Path) -> str:
    """Hash UTF-8 text after canonicalizing every line ending to LF."""
    text = path.read_text(encoding="utf-8")
    canonical = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_safe_relative_policy_path(value: str) -> bool:
    """Return whether a policy path is canonical POSIX-relative and contained."""
    if not value or "\\" in value or PureWindowsPath(value).drive:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and value == path.as_posix()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _contained_repository_path(repository: Path, relative: str) -> Path | None:
    if not is_safe_relative_policy_path(relative):
        return None
    repository_resolved = repository.resolve()
    candidate = repository.joinpath(*PurePosixPath(relative).parts)
    try:
        candidate.resolve(strict=False).relative_to(repository_resolved)
    except (OSError, ValueError):
        return None
    return candidate


def parse_python_lock(
    path: Path,
    reviewed_identifiers: frozenset[str],
) -> tuple[dict[str, tuple[str, str, str]], tuple[str, ...]]:
    """Parse exact Python identities and return deterministic violations."""
    entries: dict[str, tuple[str, str, str]] = {}
    violations: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        return {}, (f"unable to read Python runtime lock: {error}",)
    for line_number, line in enumerate(lines, start=1):
        if not line or line.startswith("#"):
            continue
        match = LOCK_ENTRY.fullmatch(line)
        if match is None:
            violations.append(f"invalid Python lock entry at line {line_number}")
            continue
        name = match.group("name")
        version = match.group("version").strip()
        license_identifier = match.group("license").strip()
        if not name or not version or not license_identifier:
            violations.append(f"empty Python lock field at line {line_number}")
            continue
        canonical_name = canonicalize_distribution_name(name)
        if canonical_name in entries:
            violations.append(f"duplicate Python distribution: {name}")
            continue
        if license_identifier not in reviewed_identifiers:
            violations.append(f"unreviewed license identifier: {license_identifier}")
        entries[canonical_name] = (name, version, license_identifier)
    if not entries:
        violations.append("Python runtime lock contains no distributions")
    return entries, tuple(violations)


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON object member: {key}")
        value[key] = item
    return value


def read_json_object(path: Path, description: str) -> tuple[dict[str, Any], str | None]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_json_object_without_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        return {}, f"unable to read {description}: {error}"
    if not isinstance(value, dict):
        return {}, f"{description} root must be an object"
    return value, None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _valid_sha256(value: object) -> str | None:
    return value if isinstance(value, str) and SHA256.fullmatch(value) else None


def parse_runtime_distribution_policy(
    value: object,
    locked: dict[str, tuple[str, str, str]],
    reviewed_identifiers: frozenset[str],
) -> tuple[dict[str, dict[str, Any]], tuple[str, ...]]:
    """Validate the exact runtime metadata/license-material policy."""
    violations: list[str] = []
    parsed: dict[str, dict[str, Any]] = {}
    if not isinstance(value, list) or not value:
        return {}, ("runtime distribution policy is empty or invalid",)
    for entry in value:
        if not isinstance(entry, dict):
            violations.append("invalid runtime distribution policy entry")
            continue
        name = _string(entry.get("name"))
        version = _string(entry.get("version"))
        license_identifier = _string(entry.get("license_identifier"))
        files = entry.get("license_files")
        if (
            name is None
            or version is None
            or license_identifier is None
            or not isinstance(files, list)
            or not files
        ):
            violations.append("invalid runtime distribution policy entry")
            continue
        canonical_name = canonicalize_distribution_name(name)
        if canonical_name in parsed:
            violations.append(f"duplicate runtime distribution policy: {name}")
            continue
        if license_identifier not in reviewed_identifiers:
            violations.append(
                f"unreviewed runtime license identifier: {license_identifier}"
            )
        expected_identity = locked.get(canonical_name)
        if (
            expected_identity is None
            or (
                version,
                license_identifier,
            )
            != expected_identity[1:]
        ):
            violations.append(f"runtime policy identity mismatch: {name}")
        parsed_files: dict[str, str] = {}
        for material in files:
            if not isinstance(material, dict):
                violations.append(f"invalid runtime license material policy: {name}")
                continue
            relative = _string(material.get("path"))
            digest = _valid_sha256(material.get("sha256"))
            if relative is None or not is_safe_relative_policy_path(relative):
                violations.append(
                    f"unsafe runtime license material path: {name}: {relative!r}"
                )
                continue
            if digest is None:
                violations.append(
                    f"invalid runtime license material digest: {name}: {relative}"
                )
                continue
            if relative in parsed_files:
                violations.append(
                    f"duplicate runtime license material path: {name}: {relative}"
                )
                continue
            parsed_files[relative] = digest
        if not parsed_files:
            violations.append(f"runtime license material policy is empty: {name}")
        parsed[canonical_name] = {
            "name": name,
            "version": version,
            "license_identifier": license_identifier,
            "license_files": parsed_files,
        }
    if set(parsed) != set(locked):
        violations.append(
            "runtime distribution policy must exactly match Python runtime lock"
        )
    return parsed, tuple(violations)


def _audit_npm_lock(
    repository: Path,
    expected: dict[str, Any],
    reviewed_identifiers: frozenset[str],
) -> tuple[str, ...]:
    violations: list[str] = []
    relative = _string(expected.get("path"))
    expected_digest = _valid_sha256(expected.get("sha256"))
    expected_count = expected.get("package_count")
    expected_license_counts = expected.get("license_counts")
    if (
        relative is None
        or expected_digest is None
        or not isinstance(expected_count, int)
        or expected_count < 0
        or not isinstance(expected_license_counts, dict)
    ):
        return ("invalid npm policy entry",)
    lock_path = _contained_repository_path(repository, relative)
    if lock_path is None:
        return (f"unsafe npm policy path: {relative!r}",)
    if not lock_path.is_file():
        return (f"missing npm lockfile: {relative}",)
    try:
        actual_digest = sha256_canonical_text(lock_path)
    except (OSError, UnicodeError) as error:
        return (f"unable to hash npm lockfile {relative}: {error}",)
    if actual_digest != expected_digest:
        violations.append(f"npm lock digest mismatch: {relative}")
    lock, read_error = read_json_object(lock_path, f"npm lockfile {relative}")
    if read_error is not None:
        violations.append(read_error)
        return tuple(violations)
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        violations.append(f"npm lock packages must be an object: {relative}")
        return tuple(violations)
    entries = {key: value for key, value in packages.items() if key != ""}
    if len(entries) != expected_count:
        violations.append(
            f"npm package count mismatch: {relative}: "
            f"expected {expected_count}, found {len(entries)}"
        )
    license_counts: Counter[str] = Counter()
    for package_path in sorted(entries):
        package = entries[package_path]
        if not isinstance(package, dict):
            violations.append(f"npm package entry must be an object: {package_path}")
            continue
        for field in ("version", "license", "integrity"):
            if _string(package.get(field)) is None:
                violations.append(f"npm package missing {field}: {package_path}")
        license_identifier = _string(package.get("license"))
        if license_identifier is None:
            continue
        license_counts[license_identifier] += 1
        if license_identifier not in reviewed_identifiers:
            violations.append(
                f"unreviewed license identifier: {license_identifier} ({package_path})"
            )
    normalized_expected_counts: dict[str, int] = {}
    for identifier, count in expected_license_counts.items():
        if not isinstance(identifier, str) or not isinstance(count, int) or count < 0:
            violations.append(f"invalid npm license count policy: {relative}")
            continue
        normalized_expected_counts[identifier] = count
        if identifier not in reviewed_identifiers:
            violations.append(f"unreviewed license identifier in policy: {identifier}")
    if dict(sorted(license_counts.items())) != dict(
        sorted(normalized_expected_counts.items())
    ):
        violations.append(f"npm license counts mismatch: {relative}")
    return tuple(violations)


def audit_repository(repository: Path, policy_path: Path) -> tuple[str, ...]:
    """Return fail-closed dependency and license-material policy violations."""
    violations: list[str] = []
    repository = repository.resolve()
    policy, error = read_json_object(policy_path, "license policy")
    if error is not None:
        return (error,)
    if policy.get("schema_version") != 1:
        violations.append("unsupported license policy schema version")
    reviewed = policy.get("reviewed_license_identifiers")
    if not isinstance(reviewed, list) or not reviewed:
        return tuple(violations + ["reviewed license allowlist is empty or invalid"])
    if any(not isinstance(item, str) or not item.strip() for item in reviewed):
        return tuple(violations + ["reviewed license allowlist contains an empty item"])
    reviewed_identifiers = frozenset(reviewed)
    if len(reviewed_identifiers) != len(reviewed):
        violations.append("reviewed license allowlist contains duplicates")

    lock_relative = _string(policy.get("python_runtime_lock"))
    if lock_relative is None or not is_safe_relative_policy_path(lock_relative):
        violations.append("unsafe Python runtime lock path in policy")
    if lock_relative != EXPECTED_PYTHON_RUNTIME_LOCK:
        violations.append(
            "Python runtime lock path must exactly match "
            f"{EXPECTED_PYTHON_RUNTIME_LOCK}"
        )
    lock_path = repository / EXPECTED_PYTHON_RUNTIME_LOCK
    locked, lock_violations = parse_python_lock(lock_path, reviewed_identifiers)
    violations.extend(lock_violations)
    _, runtime_policy_violations = parse_runtime_distribution_policy(
        policy.get("python_runtime_distributions"),
        locked,
        reviewed_identifiers,
    )
    violations.extend(runtime_policy_violations)

    npm_lockfiles = policy.get("npm_lockfiles")
    seen_paths: set[str] = set()
    if not isinstance(npm_lockfiles, list):
        violations.append("npm lockfile policy is empty or invalid")
    else:
        for expected in npm_lockfiles:
            if not isinstance(expected, dict):
                violations.append("invalid npm policy entry")
                continue
            relative = _string(expected.get("path"))
            if relative is not None and relative in seen_paths:
                violations.append(f"duplicate npm policy path: {relative}")
            if relative is not None:
                seen_paths.add(relative)
            if relative is None or not is_safe_relative_policy_path(relative):
                violations.append(f"unsafe npm policy path: {relative!r}")
            violations.extend(
                _audit_npm_lock(repository, expected, reviewed_identifiers)
            )
    if seen_paths != EXPECTED_NPM_LOCK_PATHS:
        violations.append("npm policy paths must exactly match frozen lock scope")

    materials = policy.get("bundle_materials")
    seen_sources: set[str] = set()
    seen_bundle_names: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    if not isinstance(materials, list):
        violations.append("bundle material policy is invalid")
    else:
        for material in materials:
            if not isinstance(material, dict):
                violations.append("invalid bundle material policy entry")
                continue
            source = _string(material.get("source"))
            bundle_name = _string(material.get("bundle_name"))
            expected_digest = _valid_sha256(material.get("sha256"))
            source_safe = source is not None and is_safe_relative_policy_path(source)
            name_safe = (
                bundle_name is not None
                and is_safe_relative_policy_path(bundle_name)
                and "/" not in bundle_name
            )
            if not source_safe:
                violations.append(f"unsafe bundle material source: {source!r}")
            if not name_safe:
                violations.append(f"unsafe bundle material name: {bundle_name!r}")
            if expected_digest is None:
                violations.append("invalid bundle material digest")
            if not source_safe or not name_safe or expected_digest is None:
                continue
            assert source is not None
            assert bundle_name is not None
            if source in seen_sources:
                violations.append(f"duplicate bundle material source: {source}")
            if bundle_name in seen_bundle_names:
                violations.append(f"duplicate bundle material name: {bundle_name}")
            seen_sources.add(source)
            seen_bundle_names.add(bundle_name)
            seen_pairs.add((source, bundle_name))
            source_path = _contained_repository_path(repository, source)
            if source_path is None:
                violations.append(f"unsafe bundle material source: {source!r}")
            elif not source_path.is_file():
                violations.append(f"missing bundle license material: {source}")
            else:
                try:
                    actual_digest = sha256_canonical_text(source_path)
                except (OSError, UnicodeError) as error:
                    violations.append(
                        f"unable to hash bundle license material {source}: {error}"
                    )
                else:
                    if actual_digest != expected_digest:
                        violations.append(
                            f"bundle license material digest mismatch: {source}"
                        )
    if seen_pairs != EXPECTED_BUNDLE_MATERIALS:
        violations.append("bundle material policy must exactly match frozen scope")
    return tuple(violations)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_repository = Path(__file__).resolve().parents[1]
    parser.add_argument("--repository", type=Path, default=default_repository)
    parser.add_argument(
        "--policy",
        type=Path,
        default=default_repository / "packaging" / "windows" / "license-policy.json",
    )
    arguments = parser.parse_args()
    violations = audit_repository(arguments.repository.resolve(), arguments.policy)
    if violations:
        print("FAIL dependency license inventory")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    print("PASS dependency license inventory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
