"""Keep the published v0.8.2 release metadata synchronized."""

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
EXPECTED_TAG = f"v{RELEASE_VERSION}"
EXPECTED_WHEEL = f"genvideoscope-{RELEASE_VERSION}-py3-none-any.whl"
RELEASE_DOWNLOAD_PREFIX = (
    "https://github.com/what912/VideoScope/releases/download/v0.8.2/"
)
RELEASE_WHEEL_DOWNLOAD_URL = f"{RELEASE_DOWNLOAD_PREFIX}{EXPECTED_WHEEL}"
RELEASE_INSTALLER_DOWNLOAD_URL = f"{RELEASE_DOWNLOAD_PREFIX}VideoScope-Setup-x64.exe"
RELEASE_INSTALLER_SHA256 = (
    "20027848361ce133ce15563603bcf2afa47ef793c90d3680714244ee441556db"
)
RELEASE_SOURCE_COMMIT = "162e9e7a5af03503429753c14f39c01e0a4679a7"
RELEASE_TAG_OBJECT = "c42905bc992c08ed16dc84088a8ea0068c625c4b"


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


def test_published_v082_surfaces_are_explicit() -> None:
    """Every live release surface must identify the published v0.8.2 assets."""
    readme = read_text("README.md")
    changelog = read_text("CHANGELOG.md")
    connector_install = read_text("site/src/config/connector-install.ts")
    assert readme.count(RELEASE_WHEEL_DOWNLOAD_URL) == 3
    assert RELEASE_WHEEL_DOWNLOAD_URL in connector_install
    assert RELEASE_INSTALLER_DOWNLOAD_URL in connector_install
    assert RELEASE_INSTALLER_SHA256 in connector_install
    assert "/releases/download/v0.8.1/genvideoscope-0.8.1" not in readme
    assert "/releases/download/v0.8.1/" not in connector_install
    assert "currently published stable `v0.8.2` release" in readme
    assert "development candidate" not in readme
    assert "python -m pip install genvideoscope==0.8.2" in readme
    assert "v0.8.2 正式上传 PyPI 后" not in readme
    assert "安装公开的 GitHub 开发候选版" not in readme
    assert "The immutable `0.8.2` release" in readme
    assert "Publication remains gated" not in readme
    assert "## [0.8.2] - 2026-08-30" in changelog
    assert "Pending publication" not in changelog

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
    assert "Status: **published and immutable**" in notes
    assert "Released: `2026-08-30`" in notes
    assert "Tag: `v0.8.2`" in notes
    assert "https://github.com/what912/VideoScope/releases/tag/v0.8.2" in notes
    assert "https://pypi.org/project/genvideoscope/0.8.2/" in notes
    assert "python -m pip install genvideoscope==0.8.2" in notes
    assert RELEASE_SOURCE_COMMIT in notes
    assert RELEASE_TAG_OBJECT in notes
    assert "Previous immutable release: [`v0.8.1`]" in notes
    assert "publication pending" not in notes.lower()
    assert "will contain" not in notes
    assert checklist.startswith("# GenVideoScope v0.8.2 published release record")
    assert "Status: **PUBLISHED AND IMMUTABLE**" in checklist
    assert f"Tag: `{EXPECTED_TAG}`" in checklist
    assert RELEASE_SOURCE_COMMIT in checklist
    assert RELEASE_TAG_OBJECT in checklist
    assert "https://github.com/what912/VideoScope/releases/tag/v0.8.2" in checklist
    assert "https://pypi.org/project/genvideoscope/0.8.2/" in checklist
    assert "Previous immutable release: `v0.8.1`" in checklist
    assert "publication pending" not in checklist.lower()
    assert "## Published asset identities" in checklist
    assert "## Publication verification" in checklist
    assert EXPECTED_WHEEL in notes
    assert EXPECTED_WHEEL in checklist
    assert "release-evidence.json" in notes
    assert "release-evidence.json" in checklist
    assert RELEASE_INSTALLER_SHA256 in checklist
    assert "- [ ]" not in checklist


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
