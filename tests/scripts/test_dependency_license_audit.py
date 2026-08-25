"""Synthetic fail-closed tests for the frozen dependency license inventory."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
AUDITOR = REPOSITORY_ROOT / "scripts" / "audit_dependency_licenses.py"
EXPECTED_NPM_LOCKS = ("web/package-lock.json", "site/package-lock.json")
EXPECTED_BUNDLE_MATERIALS = (
    ("LICENSE", "LICENSE"),
    ("NOTICE", "NOTICE"),
    ("THIRD_PARTY_NOTICES.txt", "THIRD_PARTY_NOTICES.txt"),
    ("docs/third-party-licenses.md", "third-party-licenses.md"),
    (
        "packaging/windows/requirements-runtime.lock",
        "requirements-runtime.lock",
    ),
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _canonical_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _synthetic_repository(root: Path) -> tuple[Path, dict[str, Any]]:
    python_lock = root / "packaging" / "windows" / "requirements-runtime.lock"
    python_lock.parent.mkdir(parents=True)
    python_lock.write_text(
        "demo-runtime==1.2.3  # SPDX-License-Identifier: MIT\n",
        encoding="utf-8",
    )
    npm_lock = {
        "name": "synthetic",
        "version": "1.0.0",
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "synthetic", "version": "1.0.0"},
            "node_modules/alpha": {
                "version": "1.0.0",
                "license": "MIT",
                "integrity": "sha512-alpha",
            },
            "node_modules/beta": {
                "version": "2.0.0",
                "license": "Apache-2.0",
                "integrity": "sha512-beta",
            },
        },
    }
    npm_entries: list[dict[str, Any]] = []
    for relative in EXPECTED_NPM_LOCKS:
        npm_path = root / relative
        _write_json(npm_path, npm_lock)
        npm_entries.append(
            {
                "path": relative,
                "sha256": _canonical_text_sha256(npm_path),
                "package_count": 2,
                "license_counts": {"Apache-2.0": 1, "MIT": 1},
            }
        )
    material_entries: list[dict[str, str]] = []
    for source, bundle_name in EXPECTED_BUNDLE_MATERIALS:
        source_path = root / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        if not source_path.exists():
            source_path.write_text(f"reviewed {bundle_name}\n", encoding="utf-8")
        material_entries.append(
            {
                "source": source,
                "bundle_name": bundle_name,
                "sha256": _canonical_text_sha256(source_path),
            }
        )
    policy = {
        "schema_version": 1,
        "reviewed_license_identifiers": ["Apache-2.0", "MIT"],
        "python_runtime_lock": "packaging/windows/requirements-runtime.lock",
        "python_runtime_distributions": [
            {
                "name": "demo-runtime",
                "version": "1.2.3",
                "license_identifier": "MIT",
                "license_files": [
                    {
                        "path": "licenses/LICENSE",
                        "sha256": hashlib.sha256(
                            b"reviewed runtime license\n"
                        ).hexdigest(),
                    }
                ],
            }
        ],
        "npm_lockfiles": npm_entries,
        "bundle_materials": material_entries,
    }
    policy_path = root / "packaging" / "windows" / "license-policy.json"
    _write_json(policy_path, policy)
    return policy_path, policy


def _run_audit(root: Path, policy_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(AUDITOR),
            "--repository",
            str(root),
            "--policy",
            str(policy_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_dependency_license_audit_accepts_complete_frozen_inventory(
    tmp_path: Path,
) -> None:
    policy_path, _ = _synthetic_repository(tmp_path)

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS dependency license inventory" in result.stdout


def test_dependency_license_audit_rejects_duplicate_python_distribution(
    tmp_path: Path,
) -> None:
    policy_path, _ = _synthetic_repository(tmp_path)
    lock = tmp_path / "packaging" / "windows" / "requirements-runtime.lock"
    lock.write_text(
        lock.read_text(encoding="utf-8")
        + "Demo_Runtime==1.2.3  # SPDX-License-Identifier: MIT\n",
        encoding="utf-8",
    )

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "duplicate Python distribution" in result.stdout


def test_dependency_license_audit_rejects_unreviewed_python_license(
    tmp_path: Path,
) -> None:
    policy_path, _ = _synthetic_repository(tmp_path)
    lock = tmp_path / "packaging" / "windows" / "requirements-runtime.lock"
    lock.write_text(
        "demo-runtime==1.2.3  # SPDX-License-Identifier: UNKNOWN\n",
        encoding="utf-8",
    )

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "unreviewed license identifier: UNKNOWN" in result.stdout


def test_dependency_license_audit_rejects_npm_digest_and_inventory_drift(
    tmp_path: Path,
) -> None:
    policy_path, _ = _synthetic_repository(tmp_path)
    npm_path = tmp_path / "web" / "package-lock.json"
    lock = json.loads(npm_path.read_text(encoding="utf-8"))
    for field in ("version", "license", "integrity"):
        lock["packages"]["node_modules/beta"].pop(field)
    _write_json(npm_path, lock)

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "npm lock digest mismatch" in result.stdout
    assert "npm license counts mismatch" in result.stdout
    for field in ("version", "license", "integrity"):
        assert f"npm package missing {field}" in result.stdout


def test_dependency_license_audit_accepts_crlf_checkout_of_frozen_text(
    tmp_path: Path,
) -> None:
    policy_path, _ = _synthetic_repository(tmp_path)
    for relative in ("web/package-lock.json", "NOTICE"):
        path = tmp_path / relative
        canonical_text = path.read_text(encoding="utf-8")
        path.write_text(canonical_text, encoding="utf-8", newline="\r\n")

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 0, result.stdout + result.stderr


def test_dependency_license_audit_rejects_semantic_text_drift(tmp_path: Path) -> None:
    policy_path, _ = _synthetic_repository(tmp_path)
    notice = tmp_path / "NOTICE"
    notice.write_text(
        notice.read_text(encoding="utf-8") + "changed\n", encoding="utf-8"
    )

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "bundle license material digest mismatch: NOTICE" in result.stdout


def test_dependency_license_audit_rejects_policy_scope_shrinkage(
    tmp_path: Path,
) -> None:
    policy_path, policy = _synthetic_repository(tmp_path)
    policy["npm_lockfiles"].pop()
    policy["bundle_materials"].pop()
    _write_json(policy_path, policy)

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "npm policy paths must exactly match" in result.stdout
    assert "bundle material policy must exactly match" in result.stdout


def test_dependency_license_audit_rejects_policy_path_escape_and_duplicates(
    tmp_path: Path,
) -> None:
    policy_path, policy = _synthetic_repository(tmp_path)
    policy["npm_lockfiles"][0]["path"] = "../outside/package-lock.json"
    policy["npm_lockfiles"][1]["path"] = "../outside/package-lock.json"
    policy["bundle_materials"][0]["source"] = str(
        (tmp_path / "absolute-license").resolve()
    )
    policy["bundle_materials"][1]["bundle_name"] = "../NOTICE"
    _write_json(policy_path, policy)

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "unsafe npm policy path" in result.stdout
    assert "duplicate npm policy path" in result.stdout
    assert "unsafe bundle material source" in result.stdout
    assert "unsafe bundle material name" in result.stdout


def test_dependency_license_audit_rejects_runtime_material_policy_drift(
    tmp_path: Path,
) -> None:
    policy_path, policy = _synthetic_repository(tmp_path)
    runtime = policy["python_runtime_distributions"][0]
    runtime["license_identifier"] = "Apache-2.0"
    runtime["license_files"].append(
        {
            "path": "../ESCAPE",
            "sha256": "0" * 64,
        }
    )
    _write_json(policy_path, policy)

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "runtime policy identity mismatch: demo-runtime" in result.stdout
    assert "unsafe runtime license material path" in result.stdout


def test_dependency_license_audit_rejects_duplicate_policy_identities(
    tmp_path: Path,
) -> None:
    policy_path, policy = _synthetic_repository(tmp_path)
    runtime = policy["python_runtime_distributions"][0]
    runtime["license_files"].append(dict(runtime["license_files"][0]))
    policy["python_runtime_distributions"].append(dict(runtime))
    policy["bundle_materials"].append(dict(policy["bundle_materials"][0]))
    _write_json(policy_path, policy)

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "duplicate runtime license material path" in result.stdout
    assert "duplicate runtime distribution policy" in result.stdout
    assert "duplicate bundle material source" in result.stdout
    assert "duplicate bundle material name" in result.stdout


def test_dependency_license_audit_rejects_duplicate_json_members_recursively(
    tmp_path: Path,
) -> None:
    policy_path, _ = _synthetic_repository(tmp_path)
    npm_path = tmp_path / "web" / "package-lock.json"
    npm_raw = npm_path.read_text(encoding="utf-8")
    npm_path.write_text(
        npm_raw.replace(
            '"license": "MIT",', '"license": "MIT",\n        "license": "MIT",'
        ),
        encoding="utf-8",
    )

    npm_result = _run_audit(tmp_path, policy_path)

    assert npm_result.returncode == 1
    assert "duplicate JSON object member: license" in npm_result.stdout

    policy_raw = policy_path.read_text(encoding="utf-8")
    policy_path.write_text(
        policy_raw.replace(
            '"schema_version": 1,',
            '"schema_version": 1,\n  "schema_version": 1,',
        ),
        encoding="utf-8",
    )

    policy_result = _run_audit(tmp_path, policy_path)

    assert policy_result.returncode == 1
    assert "duplicate JSON object member: schema_version" in policy_result.stdout


def test_dependency_license_audit_rejects_empty_python_fields(
    tmp_path: Path,
) -> None:
    policy_path, _ = _synthetic_repository(tmp_path)
    lock = tmp_path / "packaging" / "windows" / "requirements-runtime.lock"
    lock.write_text(
        "demo-runtime==  # SPDX-License-Identifier: MIT\n",
        encoding="utf-8",
    )

    result = _run_audit(tmp_path, policy_path)

    assert result.returncode == 1
    assert "invalid Python lock entry" in result.stdout


def test_repository_dependency_license_inventory_is_frozen() -> None:
    policy_path = REPOSITORY_ROOT / "packaging" / "windows" / "license-policy.json"

    result = _run_audit(REPOSITORY_ROOT, policy_path)

    assert result.returncode == 0, result.stdout + result.stderr
