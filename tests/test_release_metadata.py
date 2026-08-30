"""Keep the v0.8.2 finalization metadata synchronized."""

from __future__ import annotations

import json
import re
import tomllib
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from scripts import release_assets, smoke_test

REPOSITORY = Path(__file__).resolve().parents[1]
RELEASE_VERSION = "0.8.2"
PUBLISHED_STABLE_VERSION = "0.8.1"
EXPECTED_TAG = f"v{RELEASE_VERSION}"
EXPECTED_WHEEL = f"genvideoscope-{RELEASE_VERSION}-py3-none-any.whl"
PUBLISHED_STABLE_WHEEL = f"genvideoscope-{PUBLISHED_STABLE_VERSION}-py3-none-any.whl"
PUBLISHED_STABLE_DOWNLOAD_URL = (
    "https://github.com/what912/VideoScope/releases/download/"
    f"v{PUBLISHED_STABLE_VERSION}/{PUBLISHED_STABLE_WHEEL}"
)
PUBLISHED_STABLE_INSTALLER_URL = (
    "https://github.com/what912/VideoScope/releases/download/v0.8.1/"
    "VideoScope-Setup-x64.exe"
)
RELEASE_DOWNLOAD_PREFIX = (
    "https://github.com/what912/VideoScope/releases/download/v0.8.2/"
)
RELEASE_WHEEL_DOWNLOAD_URL = f"{RELEASE_DOWNLOAD_PREFIX}{EXPECTED_WHEEL}"
RELEASE_INSTALLER_DOWNLOAD_URL = f"{RELEASE_DOWNLOAD_PREFIX}VideoScope-Setup-x64.exe"


def read_text(relative_path: str) -> str:
    """Read one tracked UTF-8 release surface."""
    return (REPOSITORY / relative_path).read_text(encoding="utf-8")


def read_json(relative_path: str) -> object:
    """Parse one tracked JSON release surface."""
    return json.loads(read_text(relative_path))


def test_active_release_version_surfaces_agree() -> None:
    """Every executable or build-facing version must identify v0.8.2."""
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
    assert '#define MyVersionInfoVersion "0.8.2.0"' in installer

    citation = read_text("CITATION.cff")
    assert re.search(r"(?m)^version: 0\.8\.2$", citation)
    assert "date-released:" not in citation

    assert smoke_test.EXPECTED_VERSION == f"VideoScope {RELEASE_VERSION}"
    assert smoke_test.EXPECTED_DISTRIBUTION_PREFIX == (
        f"genvideoscope-{RELEASE_VERSION}-"
    )
    assert release_assets.RELEASE_VERSION == RELEASE_VERSION


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


def test_published_stable_and_v082_finalization_surfaces_are_explicit() -> None:
    """Live v0.8.1 links and finalized v0.8.2 boundaries stay distinct."""
    readme = read_text("README.md")
    changelog = read_text("CHANGELOG.md")
    connector_install = read_text("site/src/config/connector-install.ts")
    assert PUBLISHED_STABLE_DOWNLOAD_URL in readme
    assert PUBLISHED_STABLE_DOWNLOAD_URL in connector_install
    assert PUBLISHED_STABLE_INSTALLER_URL in connector_install
    assert RELEASE_WHEEL_DOWNLOAD_URL not in readme
    assert RELEASE_WHEEL_DOWNLOAD_URL not in connector_install
    assert RELEASE_INSTALLER_DOWNLOAD_URL not in connector_install
    assert "currently published stable `v0.8.1` release" in readme
    assert "development candidate" not in readme
    assert "v0.8.2 正式上传 PyPI 后" in readme
    assert "安装公开的 GitHub 开发候选版" not in readme
    assert "The `0.8.2` finalization packages" in readme
    assert "0.8.2` PREPARE-only" not in readme
    assert "## [0.8.2] - Pending publication" in changelog
    assert "## [0.8.2] - 2026-" not in changelog

    published_notes = read_text("docs/releases/v0.8.1-notes.md")
    published_checklist = read_text("docs/releases/v0.8.1-checklist.md")
    assert "Status: **published and immutable**" in published_notes
    assert "Released: `2026-08-27`" in published_notes
    assert "Tag: `v0.8.1`" in published_notes
    assert "https://github.com/what912/VideoScope/releases/tag/v0.8.1" in (
        published_notes
    )
    assert "python -m pip install genvideoscope==0.8.1" in published_notes
    assert published_checklist.startswith(
        "# GenVideoScope v0.8.1 published release record"
    )
    assert "Status: **PUBLISHED AND IMMUTABLE**" in published_checklist
    assert "Tag: `v0.8.1`" in published_checklist
    assert "publication pending" not in published_notes.lower()
    assert "publication pending" not in published_checklist.lower()

    notes = read_text("docs/releases/v0.8.2-notes.md")
    checklist = read_text("docs/releases/v0.8.2-checklist.md")
    assert "Status: **final release notes; publication pending**" in notes
    assert "Planned release: `0.8.2`" in notes
    assert "Reserved tag: `v0.8.2` (not created)" in notes
    assert "Previous immutable release: [`v0.8.1`]" in notes
    assert "PREPARE-only" not in notes
    assert "exact merged `main` commit" in notes
    assert checklist.startswith("# GenVideoScope v0.8.2 finalization checklist")
    assert "Status: **FINALIZATION COMPLETE; PUBLICATION PENDING**" in checklist
    assert f"Reserved tag: `{EXPECTED_TAG}` (not created)" in checklist
    assert "Previous immutable release: `v0.8.1`" in checklist
    assert "PREPARE-only" not in checklist
    assert "## Finalization-branch validation" in checklist
    assert "## Publication sequence" in checklist
    assert EXPECTED_WHEEL in notes
    assert EXPECTED_WHEEL in checklist
    assert "release-evidence.json" in notes
    assert "release-evidence.json" in checklist
    assert "six expected release files" in checklist.lower()
    for pending_gate in (
        "- [ ] Dashboard `npm test` and `npm run build` pass.",
        "- [ ] Public-site `npm audit --audit-level=high` and `npm run check` pass.",
        "- [ ] `python scripts/validate.py` passes from the exact merged commit.",
        (
            "- [ ] `python -m build --no-isolation` produces only the 0.8.2 "
            "wheel and sdist."
        ),
        "- [ ] `python scripts/audit_distribution.py dist` passes both archives.",
        "- [ ] The exact 0.8.2 wheel passes the offline base-wheel smoke.",
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


def test_documented_npm_lock_digests_match_reviewed_policy() -> None:
    """The human-readable inventory must identify the reviewed lock graphs."""
    policy = cast(dict[str, Any], read_json("packaging/windows/license-policy.json"))
    inventory = read_text("docs/third-party-licenses.md")

    lockfiles = cast(list[dict[str, Any]], policy["npm_lockfiles"])
    for lockfile in lockfiles:
        expected_row_prefix = f"| `{lockfile['path']}` | `{lockfile['sha256']}` |"
        assert expected_row_prefix in inventory


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
