from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import scripts.audit_windows_bundle as bundle_audit_module
from scripts.audit_windows_bundle import (
    REQUIRED_PATHS,
    audit_bundle,
    normalized_metadata_license_identifier,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _canonical_text_sha256(path: Path) -> str:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_bundle(
    root: Path,
    *,
    declared_license_files: bool = True,
) -> tuple[Path, Path, Path]:
    repository = root / "repository"
    bundle = root / "bundle"
    runtime_lock = repository / "packaging" / "windows" / "requirements-runtime.lock"
    runtime_lock.parent.mkdir(parents=True)
    runtime_lock.write_text(
        "demo-runtime==1.2.3  # SPDX-License-Identifier: MIT\n",
        encoding="utf-8",
    )
    material_pairs = (
        ("LICENSE", "LICENSE"),
        ("NOTICE", "NOTICE"),
        ("docs/third-party-licenses.md", "third-party-licenses.md"),
        (
            "packaging/windows/requirements-runtime.lock",
            "requirements-runtime.lock",
        ),
    )
    materials = []
    for source, bundle_name in material_pairs:
        source_path = repository / source
        source_path.parent.mkdir(parents=True, exist_ok=True)
        if not source_path.exists():
            source_path.write_text(f"reviewed {bundle_name}\n", encoding="utf-8")
        materials.append(
            {
                "source": source,
                "bundle_name": bundle_name,
                "sha256": _canonical_text_sha256(source_path),
            }
        )
    runtime_license_files = {
        "licenses/LICENSE": "reviewed runtime license\n",
        "licenses/NOTICE": "reviewed runtime notice\n",
    }
    if declared_license_files:
        runtime_license_files["licenses/AUTHORS"] = "reviewed authors\n"
    policy: dict[str, Any] = {
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
                        "path": path,
                        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    }
                    for path, content in runtime_license_files.items()
                ],
            }
        ],
        "npm_lockfiles": [],
        "bundle_materials": materials,
    }
    policy_path = repository / "packaging" / "windows" / "license-policy.json"
    policy_path.write_text(json.dumps(policy, indent=2) + "\n", encoding="utf-8")
    for required_relative in REQUIRED_PATHS:
        path = bundle / required_relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime\n", encoding="utf-8")
    license_root = bundle / "_internal" / "licenses"
    for material in policy["bundle_materials"]:
        source = repository / material["source"]
        destination = license_root / material["bundle_name"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    shutil.copyfile(policy_path, license_root / "license-policy.json")
    metadata = bundle / "_internal" / "demo_runtime-1.2.3.dist-info"
    metadata.mkdir(parents=True)
    declared_headers = ""
    if declared_license_files:
        declared_headers = (
            "License-File: LICENSE\nLicense-File: NOTICE\nLicense-File: AUTHORS\n"
        )
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.4\n"
        "Name: demo-runtime\n"
        "Version: 1.2.3\n"
        "License-Expression: MIT\n" + declared_headers,
        encoding="utf-8",
    )
    for license_relative, content in runtime_license_files.items():
        license_file = metadata / license_relative
        license_file.parent.mkdir(parents=True, exist_ok=True)
        license_file.write_text(content, encoding="utf-8")
    return bundle, repository, policy_path


def _audit(bundle: Path, repository: Path, policy_path: Path) -> tuple[str, ...]:
    return audit_bundle(
        bundle,
        repository_root=repository,
        policy_path=policy_path,
    )


def test_bundle_audit_accepts_required_runtime_without_external_media(
    tmp_path: Path,
) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)

    assert _audit(bundle, repository, policy_path) == ()


def test_bundle_audit_rejects_ffmpeg_models_and_secrets(tmp_path: Path) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    (bundle / "ffmpeg.exe").write_bytes(b"binary")
    (bundle / "model.onnx").write_bytes(b"weight")
    (bundle / "settings.txt").write_text(
        "sk-this-is-not-a-real-key-but-must-never-ship",
        encoding="utf-8",
    )

    violations = _audit(bundle, repository, policy_path)

    assert any("ffmpeg.exe" in item for item in violations)
    assert any("model.onnx" in item for item in violations)
    assert any("embedded secret" in item for item in violations)


def test_bundle_audit_rejects_development_tooling(tmp_path: Path) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    development_module = bundle / "_internal" / "mypy" / "__init__.py"
    development_module.parent.mkdir(parents=True)
    development_module.write_text("", encoding="utf-8")

    violations = _audit(bundle, repository, policy_path)

    assert any("development-only package" in item for item in violations)


def test_bundle_audit_rejects_drifted_license_inventory(tmp_path: Path) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    notice = bundle / "_internal" / "licenses" / "NOTICE"
    notice.write_text("drifted notice\n", encoding="utf-8")

    violations = _audit(bundle, repository, policy_path)

    assert any("license material digest mismatch" in item for item in violations)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("target", "expected_violation"),
    [
        ("runtime_lock", "runtime lock path"),
        ("material_source", "unsafe repository bundle material policy path"),
        ("bundle_name", "unsafe repository bundle material policy path"),
    ],
)
def test_bundle_audit_rejects_license_policy_path_traversal(
    tmp_path: Path,
    target: str,
    expected_violation: str,
) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    if target == "runtime_lock":
        policy["python_runtime_lock"] = "../outside.lock"
    elif target == "material_source":
        policy["bundle_materials"][0]["source"] = "../outside-license"
    else:
        policy["bundle_materials"][0]["bundle_name"] = "../ESCAPE"
    serialized = json.dumps(policy, indent=2) + "\n"
    policy_path.write_text(serialized, encoding="utf-8")
    (bundle / "_internal" / "licenses" / "license-policy.json").write_text(
        serialized,
        encoding="utf-8",
    )

    violations = _audit(bundle, repository, policy_path)

    assert any(expected_violation in item for item in violations)
    assert not (bundle / "_internal" / "ESCAPE").exists()


def test_bundle_audit_rejects_unexpected_license_root_entry(tmp_path: Path) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    (bundle / "_internal" / "licenses" / "EXTRA.txt").write_text(
        "unexpected\n",
        encoding="utf-8",
    )

    violations = _audit(bundle, repository, policy_path)

    assert any(
        "unexpected bundled license-root entry: EXTRA.txt" in item
        for item in violations
    )


def test_bundle_audit_rejects_duplicate_material_policy_entries(
    tmp_path: Path,
) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["bundle_materials"].append(dict(policy["bundle_materials"][0]))
    serialized = json.dumps(policy, indent=2) + "\n"
    policy_path.write_text(serialized, encoding="utf-8")
    (bundle / "_internal" / "licenses" / "license-policy.json").write_text(
        serialized,
        encoding="utf-8",
    )

    violations = _audit(bundle, repository, policy_path)

    assert any(
        "duplicate repository bundle material source" in item for item in violations
    )
    assert any(
        "duplicate repository bundle material name" in item for item in violations
    )


def test_bundle_audit_rejects_runtime_metadata_version_drift(tmp_path: Path) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    metadata = next((bundle / "_internal").glob("demo_runtime-*.dist-info/METADATA"))
    metadata.write_text(
        "Metadata-Version: 2.4\n"
        "Name: demo-runtime\n"
        "Version: 0.0.0\n"
        "License-Expression: MIT\n",
        encoding="utf-8",
    )

    violations = _audit(bundle, repository, policy_path)

    assert any(
        "runtime distribution version mismatch: demo-runtime" in item
        for item in violations
    )


def test_bundle_audit_rejects_missing_distribution_license(tmp_path: Path) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    metadata = next((bundle / "_internal").glob("demo_runtime-*.dist-info"))
    (metadata / "licenses" / "NOTICE").unlink()

    violations = _audit(bundle, repository, policy_path)

    assert any(
        "runtime distribution license material set mismatch: demo-runtime" in item
        for item in violations
    )


def test_bundle_audit_rejects_replaced_distribution_license(tmp_path: Path) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    metadata = next((bundle / "_internal").glob("demo_runtime-*.dist-info"))
    (metadata / "licenses" / "LICENSE").write_text("truncated\n", encoding="utf-8")

    violations = _audit(bundle, repository, policy_path)

    assert any(
        "runtime distribution license material digest mismatch: "
        "demo-runtime: licenses/LICENSE" in item
        for item in violations
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "change",
    ["remove", "mutate"],
)
def test_bundle_audit_rejects_declared_nonmarker_license_material_drift(
    tmp_path: Path,
    change: str,
) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    authors = next(
        (bundle / "_internal").glob("demo_runtime-*.dist-info/licenses/AUTHORS")
    )
    if change == "remove":
        authors.unlink()
    else:
        authors.write_text("mutated authors\n", encoding="utf-8")

    violations = _audit(bundle, repository, policy_path)

    assert any("runtime distribution license material" in item for item in violations)


def test_bundle_audit_uses_filename_marker_fallback_without_declarations(
    tmp_path: Path,
) -> None:
    bundle, repository, policy_path = _valid_bundle(
        tmp_path,
        declared_license_files=False,
    )

    assert _audit(bundle, repository, policy_path) == ()


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "declaration_case",
    ["duplicate", "unsafe", "missing", "ambiguous"],
)
def test_bundle_audit_rejects_invalid_license_file_declarations(
    tmp_path: Path,
    declaration_case: str,
) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    metadata_directory = next((bundle / "_internal").glob("demo_runtime-*.dist-info"))
    metadata_file = metadata_directory / "METADATA"
    metadata = metadata_file.read_text(encoding="utf-8")
    if declaration_case == "duplicate":
        metadata += "License-File: AUTHORS\n"
    elif declaration_case == "unsafe":
        metadata += "License-File: ../AUTHORS\n"
    elif declaration_case == "missing":
        metadata += "License-File: MISSING\n"
    else:
        (metadata_directory / "AUTHORS").write_text(
            "ambiguous authors\n",
            encoding="utf-8",
        )
    metadata_file.write_text(metadata, encoding="utf-8")

    violations = _audit(bundle, repository, policy_path)

    assert any("License-File declaration" in item for item in violations)


def test_bundle_audit_rejects_unexpected_distribution_license(tmp_path: Path) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    metadata = next((bundle / "_internal").glob("demo_runtime-*.dist-info"))
    (metadata / "licenses" / "COPYING").write_text("unexpected\n", encoding="utf-8")

    violations = _audit(bundle, repository, policy_path)

    assert any(
        "runtime distribution license material set mismatch: demo-runtime" in item
        for item in violations
    )


def test_bundle_audit_rejects_runtime_metadata_license_identity_drift(
    tmp_path: Path,
) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    metadata = next((bundle / "_internal").glob("demo_runtime-*.dist-info/METADATA"))
    metadata.write_text(
        "Metadata-Version: 2.4\n"
        "Name: demo-runtime\n"
        "Version: 1.2.3\n"
        "License-Expression: Apache-2.0\n",
        encoding="utf-8",
    )

    violations = _audit(bundle, repository, policy_path)

    assert any(
        "runtime distribution license identity mismatch: demo-runtime" in item
        for item in violations
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("metadata_text", "expected"),
    [
        ("License-Expression: MIT\n", "MIT"),
        ("License: Apache 2.0\n", "Apache-2.0"),
        ("Classifier: License :: OSI Approved :: BSD License\n", "BSD-3-Clause"),
    ],
)
def test_metadata_license_identity_normalizes_all_supported_sources(
    metadata_text: str,
    expected: str,
) -> None:
    assert normalized_metadata_license_identifier(metadata_text.encode()) == expected


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "metadata_text",
    [
        "License-Expression: MIT\nLicense-Expression: MIT\n",
        "License-Expression:\n",
        "License-Expression: MIT\nLicense: Apache 2.0\n",
        "License: MIT\nLicense: MIT\n",
        "License:\n",
        "Classifier: License :: Other/Proprietary License\n",
        (
            "Classifier: License :: OSI Approved :: MIT License\n"
            "Classifier: License :: OSI Approved :: BSD License\n"
        ),
        "Summary: no license metadata\n",
    ],
)
def test_metadata_license_identity_rejects_ambiguous_or_unmapped_headers(
    metadata_text: str,
) -> None:
    assert normalized_metadata_license_identifier(metadata_text.encode()) is None


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("header", "replacement"),
    [
        ("Name", "Name: demo-runtime\nName: demo-runtime"),
        ("Name", "Name:"),
        ("Version", "Version: 1.2.3\nVersion: 1.2.3"),
        ("Version", "Version:"),
    ],
)
def test_bundle_audit_rejects_duplicate_or_empty_identity_headers(
    tmp_path: Path,
    header: str,
    replacement: str,
) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    metadata_file = next(
        (bundle / "_internal").glob("demo_runtime-*.dist-info/METADATA")
    )
    metadata = metadata_file.read_text(encoding="utf-8")
    original = "Name: demo-runtime" if header == "Name" else "Version: 1.2.3"
    metadata_file.write_text(
        metadata.replace(original, replacement),
        encoding="utf-8",
    )

    violations = _audit(bundle, repository, policy_path)

    assert any(
        f"runtime distribution {header} header invalid" in item for item in violations
    )


def test_bundle_audit_preflight_rejects_before_reading_any_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    escaped = tmp_path.parent / "escaped-policy.json"
    monkeypatch.setattr(
        bundle_audit_module,
        "_preflight_unsafe_links",
        lambda root: (f"unsafe link or reparse point: {escaped}",),
    )

    def forbidden_read(*args: object, **kwargs: object) -> None:
        raise AssertionError("bundle preflight must reject before reading a file")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    monkeypatch.setattr(Path, "read_bytes", forbidden_read)

    assert audit_bundle(tmp_path) == (f"unsafe link or reparse point: {escaped}",)


def test_bundle_audit_rejects_duplicate_members_in_bundled_policy(
    tmp_path: Path,
) -> None:
    bundle, repository, policy_path = _valid_bundle(tmp_path)
    bundled_policy = bundle / "_internal" / "licenses" / "license-policy.json"
    raw = bundled_policy.read_text(encoding="utf-8")
    bundled_policy.write_text(
        raw.replace(
            '"schema_version": 1,', '"schema_version": 1,\n  "schema_version": 1,'
        ),
        encoding="utf-8",
    )

    violations = _audit(bundle, repository, policy_path)

    assert any("duplicate JSON object member" in item for item in violations)


def test_installer_registers_user_scoped_start_protocol() -> None:
    installer = (
        REPOSITORY_ROOT / "packaging" / "windows" / "VideoScope.iss"
    ).read_text(encoding="utf-8")

    assert "[Registry]" in installer
    assert 'Root: HKCU; Subkey: "Software\\Classes\\videoscope"' in installer
    assert 'ValueName: "URL Protocol"' in installer
    assert '"%1"' in installer
    assert 'RunOnceId: "StopConnector"' in installer


def test_installer_uses_self_contained_bilingual_messages() -> None:
    installer = (
        REPOSITORY_ROOT / "packaging" / "windows" / "VideoScope.iss"
    ).read_text(encoding="utf-8")

    assert "ChineseSimplified.isl" not in installer
    assert "[Messages]" in installer
    assert "欢迎安装" in installer
    assert "Welcome" in installer


def test_build_script_discovers_user_scoped_inno_setup() -> None:
    build_script = (
        REPOSITORY_ROOT / "scripts" / "build_windows_installer.ps1"
    ).read_text(encoding="utf-8")

    assert "LOCALAPPDATA" in build_script
    assert "Programs\\Inno Setup 6\\ISCC.exe" in build_script


def test_windows_workflow_uses_the_exact_build_dependency_path() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github" / "workflows" / "windows-installer.yml"
    ).read_text(encoding="utf-8")

    build_script = (
        REPOSITORY_ROOT / "scripts" / "build_windows_installer.ps1"
    ).read_text(encoding="utf-8")

    assert "-SkipDependencyInstall" not in workflow
    assert "./scripts/build_windows_installer.ps1" in workflow
    assert 'python -m pip install -e ".[web,dev]"' not in workflow
    assert "-m venv --clear" in build_script
    assert "requirements-runtime.lock" in build_script
    assert "--no-deps -e $repositoryRoot" in build_script
    assert "requirements-build.txt" in build_script
    assert "& $buildPython" in build_script


def test_windows_workflow_tracks_every_license_audit_input() -> None:
    workflow = (
        REPOSITORY_ROOT / ".github" / "workflows" / "windows-installer.yml"
    ).read_text(encoding="utf-8")

    for required_path in (
        "LICENSE",
        "NOTICE",
        "docs/third-party-licenses.md",
        "pyproject.toml",
        "scripts/audit_dependency_licenses.py",
        "web/package-lock.json",
        "site/package-lock.json",
    ):
        assert workflow.count(f'      - "{required_path}"') == 2
