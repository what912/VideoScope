"""Prepare and verify deterministic, no-clobber v0.8.2 release evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 10
MAX_GIT_ERROR_CHARS = 2_000
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
RELEASE_VERSION = "0.8.2"
PRIMARY_ASSET_NAMES = (
    f"genvideoscope-{RELEASE_VERSION}-py3-none-any.whl",
    f"genvideoscope-{RELEASE_VERSION}.tar.gz",
    "VideoScope-Setup-x64.exe",
)
INSTALLER_NAME = "VideoScope-Setup-x64.exe"
INSTALLER_SIDECAR_NAME = f"{INSTALLER_NAME}.sha256"
CHECKSUMS_NAME = "SHA256SUMS.txt"
EVIDENCE_NAME = "release-evidence.json"
RELEASE_FILE_NAMES = frozenset(
    (*PRIMARY_ASSET_NAMES, INSTALLER_SIDECAR_NAME, CHECKSUMS_NAME, EVIDENCE_NAME)
)


class ReleaseAssetError(ValueError):
    """A release evidence precondition or canonicality check failed."""


GitRunner = Callable[[tuple[str, ...], Path], subprocess.CompletedProcess[str]]


def run_git_command(
    argv: tuple[str, ...], cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git argv command without invoking a shell."""
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReleaseAssetError(
            f"Git command timed out after {GIT_TIMEOUT_SECONDS} seconds: {argv!r}"
        ) from exc
    except OSError as exc:
        raise ReleaseAssetError(
            f"Git command could not be started: {type(exc).__name__}: {exc}"
        ) from exc


def _bounded_process_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "no process output").strip()
    return detail[:MAX_GIT_ERROR_CHARS]


def _validate_repository(
    repository_root: Path,
    expected_commit: str,
    runner: GitRunner,
) -> None:
    if COMMIT_PATTERN.fullmatch(expected_commit) is None:
        raise ReleaseAssetError(
            "expected commit must be exactly 40 lowercase hex characters"
        )
    if repository_root.is_symlink() or not repository_root.is_dir():
        raise ReleaseAssetError(
            "repository root must be an existing non-symlink directory"
        )

    head_result = runner(
        ("git", "rev-parse", "--verify", "HEAD^{commit}"), repository_root
    )
    if head_result.returncode != 0:
        raise ReleaseAssetError(
            "Git commit inspection failed: " + _bounded_process_error(head_result)
        )
    actual_commit = head_result.stdout.strip()
    if actual_commit != expected_commit:
        raise ReleaseAssetError(
            f"repository commit {actual_commit!r} does not match {expected_commit!r}"
        )

    status_result = runner(
        ("git", "status", "--porcelain=v1", "--untracked-files=all"),
        repository_root,
    )
    if status_result.returncode != 0:
        raise ReleaseAssetError(
            "Git cleanliness inspection failed: "
            + _bounded_process_error(status_result)
        )
    if status_result.stdout:
        raise ReleaseAssetError("repository is not clean at the frozen commit")


def _canonical_paths(primary_assets: Sequence[Path | str]) -> dict[str, Path]:
    paths = [Path(path) for path in primary_assets]
    if len(paths) != len(PRIMARY_ASSET_NAMES):
        raise ReleaseAssetError(
            "release preparation requires exactly three primary assets"
        )

    resolved: list[Path] = []
    for path in paths:
        try:
            resolved.append(path.resolve(strict=True))
        except OSError as exc:
            raise ReleaseAssetError(f"primary asset does not exist: {path}") from exc
    if len(set(resolved)) != len(resolved):
        raise ReleaseAssetError("duplicate primary asset paths are not allowed")

    by_name = {path.name: path for path in paths}
    if set(by_name) != set(PRIMARY_ASSET_NAMES):
        raise ReleaseAssetError(
            f"primary assets must use the exact canonical v{RELEASE_VERSION} filenames"
        )
    for name in PRIMARY_ASSET_NAMES:
        path = by_name[name]
        if path.is_symlink():
            raise ReleaseAssetError(
                f"primary asset must not be a symbolic link: {name}"
            )
        if not path.is_file():
            raise ReleaseAssetError(f"primary asset must be a regular file: {name}")
    return by_name


def _measure(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ReleaseAssetError(
            f"could not read release asset {path.name}: {type(exc).__name__}"
        ) from exc
    return digest.hexdigest(), size


def _asset_records(paths: dict[str, Path]) -> list[dict[str, str | int]]:
    records: list[dict[str, str | int]] = []
    for name in sorted(PRIMARY_ASSET_NAMES):
        digest, size = _measure(paths[name])
        records.append({"name": name, "sha256": digest, "size_bytes": size})
    return records


def _snapshot_release_files(root: Path) -> dict[str, tuple[str, int]]:
    """Return one complete hash/size snapshot of the canonical release set."""
    try:
        children = list(root.iterdir())
    except OSError as exc:
        raise ReleaseAssetError(
            f"could not enumerate release output: {type(exc).__name__}"
        ) from exc
    if {child.name for child in children} != RELEASE_FILE_NAMES:
        raise ReleaseAssetError(
            "release output does not contain the exact release file set"
        )
    if any(child.is_symlink() or not child.is_file() for child in children):
        raise ReleaseAssetError(
            "every release output entry must be a regular non-symlink file"
        )
    return {
        child.name: _measure(child)
        for child in sorted(children, key=lambda candidate: candidate.name)
    }


def _records_from_snapshot(
    snapshot: dict[str, tuple[str, int]],
) -> list[dict[str, str | int]]:
    return [
        {
            "name": name,
            "sha256": snapshot[name][0],
            "size_bytes": snapshot[name][1],
        }
        for name in sorted(PRIMARY_ASSET_NAMES)
    ]


def _record_hashes(records: Sequence[dict[str, str | int]]) -> dict[str, str]:
    return {str(record["name"]): str(record["sha256"]) for record in records}


def _canonical_sums(records: Sequence[dict[str, str | int]]) -> bytes:
    hashes = _record_hashes(records)
    return "".join(
        f"{hashes[name]}  {name}\n" for name in sorted(PRIMARY_ASSET_NAMES)
    ).encode("utf-8")


def _canonical_installer_sidecar(
    records: Sequence[dict[str, str | int]],
) -> bytes:
    installer_hash = _record_hashes(records)[INSTALLER_NAME]
    return f"{installer_hash}  {INSTALLER_NAME}\n".encode()


def _evidence_payload(
    records: Sequence[dict[str, str | int]], expected_commit: str
) -> dict[str, Any]:
    return {
        "assets": list(records),
        "commit": expected_commit,
        "schema_version": "1",
    }


def _canonical_evidence(
    records: Sequence[dict[str, str | int]], expected_commit: str
) -> bytes:
    payload = _evidence_payload(records, expected_commit)
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as destination:
            destination.write(payload)
    except FileExistsError as exc:
        raise ReleaseAssetError(f"release target already exists: {path.name}") from exc


def _copy_exclusive(
    source: Path,
    destination: Path,
    expected_hash: str,
    expected_size: int,
) -> None:
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            while chunk := input_stream.read(1024 * 1024):
                output_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except FileExistsError as exc:
        raise ReleaseAssetError(
            f"release target already exists: {destination.name}"
        ) from exc
    if size != expected_size or digest.hexdigest() != expected_hash:
        raise ReleaseAssetError(f"primary asset changed after preflight: {source.name}")


def _read_release_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReleaseAssetError(
            f"could not read release file {path.name}: {type(exc).__name__}"
        ) from exc


def _verify_release_output(output: Path, expected_commit: str) -> None:
    """Validate canonical evidence between two complete file snapshots."""
    initial_snapshot = _snapshot_release_files(output)
    records = _records_from_snapshot(initial_snapshot)
    if _read_release_bytes(
        output / INSTALLER_SIDECAR_NAME
    ) != _canonical_installer_sidecar(records):
        raise ReleaseAssetError("installer checksum sidecar is not canonical")
    if _read_release_bytes(output / CHECKSUMS_NAME) != _canonical_sums(records):
        raise ReleaseAssetError(
            "SHA256SUMS.txt is not canonical or contains a checksum mismatch"
        )

    evidence_bytes = _read_release_bytes(output / EVIDENCE_NAME)
    try:
        evidence = json.loads(evidence_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseAssetError("release-evidence.json is not canonical JSON") from exc
    if not isinstance(evidence, dict) or evidence.get("commit") != expected_commit:
        raise ReleaseAssetError(
            "release-evidence commit does not match expected commit"
        )
    if evidence_bytes != _canonical_evidence(records, expected_commit):
        raise ReleaseAssetError("release-evidence.json is not canonical")

    final_snapshot = _snapshot_release_files(output)
    if final_snapshot != initial_snapshot:
        raise ReleaseAssetError("release files changed during verification")


def prepare_release_assets(
    primary_assets: Sequence[Path | str],
    output_root: Path | str,
    *,
    expected_commit: str,
    repository_root: Path | str,
    runner: GitRunner | None = None,
) -> None:
    """Copy exactly three assets and create deterministic release evidence."""
    paths = _canonical_paths(primary_assets)
    output = Path(output_root)
    repository = Path(repository_root)
    if output.exists() or output.is_symlink():
        raise ReleaseAssetError(f"release output already exists: {output}")
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise ReleaseAssetError("release output parent must be an existing directory")

    records = _asset_records(paths)
    _validate_repository(repository, expected_commit, runner or run_git_command)

    try:
        output.mkdir()
    except FileExistsError as exc:
        raise ReleaseAssetError(f"release output already exists: {output}") from exc

    records_by_name = {str(record["name"]): record for record in records}
    for name in sorted(PRIMARY_ASSET_NAMES):
        record = records_by_name[name]
        _copy_exclusive(
            paths[name],
            output / name,
            str(record["sha256"]),
            int(record["size_bytes"]),
        )
    _write_exclusive(
        output / INSTALLER_SIDECAR_NAME, _canonical_installer_sidecar(records)
    )
    _write_exclusive(output / CHECKSUMS_NAME, _canonical_sums(records))
    _write_exclusive(
        output / EVIDENCE_NAME, _canonical_evidence(records, expected_commit)
    )
    _verify_release_output(output, expected_commit)
    _validate_repository(repository, expected_commit, runner or run_git_command)


def verify_release_assets(
    output_root: Path | str,
    *,
    expected_commit: str,
    repository_root: Path | str,
    runner: GitRunner | None = None,
) -> None:
    """Verify the exact release file set, checksums, evidence and Git identity."""
    output = Path(output_root)
    repository = Path(repository_root)
    _validate_repository(repository, expected_commit, runner or run_git_command)
    if output.is_symlink() or not output.is_dir():
        raise ReleaseAssetError(
            "release output must be an existing non-symlink directory"
        )

    _verify_release_output(output, expected_commit)
    _validate_repository(repository, expected_commit, runner or run_git_command)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--commit", required=True)
        subparser.add_argument("--repo-root", type=Path, required=True)
        subparser.add_argument("--output-root", type=Path, required=True)
        if command == "prepare":
            subparser.add_argument("assets", nargs="+", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the prepare or verify command."""
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            prepare_release_assets(
                arguments.assets,
                arguments.output_root,
                expected_commit=arguments.commit,
                repository_root=arguments.repo_root,
            )
        else:
            verify_release_assets(
                arguments.output_root,
                expected_commit=arguments.commit,
                repository_root=arguments.repo_root,
            )
    except ReleaseAssetError as exc:
        print(f"release asset error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
