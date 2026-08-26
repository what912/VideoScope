"""Keep the v0.8.1 finalization metadata synchronized."""

from __future__ import annotations

import json
import re
import tomllib
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from scripts import smoke_test

REPOSITORY = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "0.8.1"
PREVIOUS_ASSET_VERSION = "0.8.0"
EXPECTED_TAG = f"v{RELEASE_VERSION}"
EXPECTED_WHEEL = f"genvideoscope-{RELEASE_VERSION}-py3-none-any.whl"
PREVIOUS_WHEEL = f"genvideoscope-{PREVIOUS_ASSET_VERSION}-py3-none-any.whl"
PREVIOUS_DOWNLOAD_URL = (
    "https://github.com/what912/VideoScope/releases/download/"
    f"v{PREVIOUS_ASSET_VERSION}/{PREVIOUS_WHEEL}"
)
RELEASE_DOWNLOAD_PREFIX = (
    "https://github.com/what912/VideoScope/releases/download/v0.8.1/"
)
RELEASE_WHEEL_DOWNLOAD_URL = f"{RELEASE_DOWNLOAD_PREFIX}{EXPECTED_WHEEL}"


def read_text(relative_path: str) -> str:
    """Read one tracked UTF-8 release surface."""
    return (REPOSITORY / relative_path).read_text(encoding="utf-8")


def read_json(relative_path: str) -> object:
    """Parse one tracked JSON release surface."""
    return json.loads(read_text(relative_path))


def test_active_release_version_surfaces_agree() -> None:
    """Every executable or build-facing version must identify v0.8.1."""
    pyproject = tomllib.loads(read_text("pyproject.toml"))
    assert pyproject["project"]["version"] == RELEASE_VERSION

    package_init = read_text("src/videoscope/__init__.py")
    assert f'__version__ = "{RELEASE_VERSION}"' in package_init

    for package_path in ("web/package.json", "site/package.json"):
        package = read_json(package_path)
        assert isinstance(package, dict)
        assert package["version"] == RELEASE_VERSION

    for lock_path in ("web/package-lock.json", "site/package-lock.json"):
        lock = read_json(lock_path)
        assert isinstance(lock, dict)
        assert lock["version"] == RELEASE_VERSION
        assert lock["packages"][""]["version"] == RELEASE_VERSION

    installer = read_text("packaging/windows/VideoScope.iss")
    assert f'#define MyAppVersion "{RELEASE_VERSION}"' in installer
    assert '#define MyVersionInfoVersion "0.8.1.0"' in installer

    citation = read_text("CITATION.cff")
    assert re.search(r"(?m)^version: 0\.8\.1$", citation)
    assert "date-released:" not in citation

    assert smoke_test.EXPECTED_VERSION == f"VideoScope {RELEASE_VERSION}"
    assert smoke_test.EXPECTED_DISTRIBUTION_PREFIX == (
        f"genvideoscope-{RELEASE_VERSION}-"
    )


def test_wheel_metadata_carries_dashboard_third_party_notices() -> None:
    pyproject = tomllib.loads(read_text("pyproject.toml"))

    assert pyproject["project"]["license-files"] == [
        "LICENSE",
        "NOTICE",
        "THIRD_PARTY_NOTICES.txt",
    ]


def test_dashboard_third_party_notice_checkout_is_canonical_lf() -> None:
    attributes = read_text(".gitattributes").splitlines()

    assert "THIRD_PARTY_NOTICES.txt text eol=lf" in attributes


def test_prepublication_and_finalization_surfaces_are_explicit() -> None:
    """Finalized docs must not advertise assets before publication."""
    readme = read_text("README.md")
    connector_install = read_text("site/src/config/connector-install.ts")
    assert PREVIOUS_DOWNLOAD_URL in readme
    assert PREVIOUS_DOWNLOAD_URL in connector_install
    assert RELEASE_WHEEL_DOWNLOAD_URL not in readme
    assert RELEASE_WHEEL_DOWNLOAD_URL not in connector_install
    assert "currently published stable `v0.8.0` release" in readme
    assert "development candidate" not in readme
    assert "v0.8.1 正式上传 PyPI 后" in readme
    assert "安装公开的 GitHub 开发候选版" not in readme

    notes = read_text("docs/releases/v0.8.1-notes.md")
    checklist = read_text("docs/releases/v0.8.1-checklist.md")
    assert "Status: **final release notes; publication pending**" in notes
    assert "draft candidate" not in notes.lower()
    assert "stale" not in notes.lower()
    assert "PREPARE-only" not in notes
    assert checklist.startswith("# GenVideoScope v0.8.1 finalization checklist")
    assert "Status: **FINALIZATION COMPLETE; PUBLICATION PENDING**" in checklist
    assert "candidate is **not release-ready**" not in checklist
    assert "PREPARE-only" not in checklist
    assert EXPECTED_WHEEL in notes
    assert "release-evidence.json" in notes
    assert "release-evidence.json" in checklist
    assert f"Reserved tag: `{EXPECTED_TAG}` (not created)" in checklist
    assert "six expected release files" in checklist.lower()
    assert "live publication state" in checklist.lower()
    for pending_gate in (
        "- [ ] Dashboard `npm test` and `npm run build` pass.",
        "- [ ] Public-site `npm audit --audit-level=high` and `npm run check` pass.",
        "- [ ] `python scripts/validate.py` passes from the exact merged commit.",
        (
            "- [ ] `python -m build --no-isolation` produces only the 0.8.1 "
            "wheel and sdist."
        ),
        "- [ ] `python scripts/audit_distribution.py dist` passes both archives.",
        "- [ ] The exact 0.8.1 wheel passes the offline base-wheel smoke.",
        "- [ ] The Windows bundle audit, pinned Inno Setup installer build and",
        "- [ ] Independent review confirms the rebuilt asset and release boundary.",
        "- [ ] Windows bundle LICENSE/NOTICE content and the Python/npm transitive",
        (
            "- [ ] `release-evidence.json` identifies the exact merged source "
            "commit and the"
        ),
        "- [ ] `SHA256SUMS.txt` and `VideoScope-Setup-x64.exe.sha256` agree with the",
    ):
        assert pending_gate in checklist


def test_v080_release_evidence_remains_historical() -> None:
    """The patch candidate must not rewrite v0.8.0's release evidence."""
    old_checklist = read_text("docs/release-checklist.md")
    old_audit = read_text("release-audit.md")
    assert old_checklist.startswith("# GenVideoScope v0.8.0 stable release checklist")
    assert old_audit.startswith("# GenVideoScope 0.8.0 stable release audit")


def test_public_case_reports_checkout_as_canonical_lf_bytes() -> None:
    """Hashed public reports must survive Windows checkout byte-for-byte."""
    attributes = read_text(".gitattributes")
    assert "site/public/cases/**/public-report.json text eol=lf" in attributes

    manifest = cast(dict[str, Any], read_json("site/src/data/case-studies.json"))
    cases = cast(list[dict[str, Any]], manifest["cases"])
    for case in cases:
        assets = cast(dict[str, str], case["assets"])
        hashes = cast(dict[str, str], case["sha256"])
        repository_path = assets["publicReport"].removeprefix("/VideoScope/")
        payload = (REPOSITORY / "site" / "public" / repository_path).read_bytes()
        assert b"\r\n" not in payload
        assert sha256(payload).hexdigest() == hashes["publicReport"]


def test_python_crlf_is_not_reported_as_trailing_whitespace() -> None:
    """Default diff checks must accept inherited CRLF Python blobs."""
    attributes = read_text(".gitattributes").splitlines()
    assert "*.py whitespace=cr-at-eol" in attributes
