"""Keep the v0.8.1 candidate's active release metadata synchronized."""

from __future__ import annotations

import json
import re
import tomllib
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from scripts import smoke_test

REPOSITORY = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.8.1"
EXPECTED_TAG = f"v{EXPECTED_VERSION}"
EXPECTED_WHEEL = f"genvideoscope-{EXPECTED_VERSION}-py3-none-any.whl"
EXPECTED_DOWNLOAD_URL = (
    "https://github.com/what912/VideoScope/releases/download/"
    f"{EXPECTED_TAG}/{EXPECTED_WHEEL}"
)


def read_text(relative_path: str) -> str:
    """Read one tracked UTF-8 release surface."""
    return (REPOSITORY / relative_path).read_text(encoding="utf-8")


def read_json(relative_path: str) -> object:
    """Parse one tracked JSON release surface."""
    return json.loads(read_text(relative_path))


def test_active_release_version_surfaces_agree() -> None:
    """Every executable or build-facing version must identify v0.8.1."""
    pyproject = tomllib.loads(read_text("pyproject.toml"))
    assert pyproject["project"]["version"] == EXPECTED_VERSION

    package_init = read_text("src/videoscope/__init__.py")
    assert f'__version__ = "{EXPECTED_VERSION}"' in package_init

    for package_path in ("web/package.json", "site/package.json"):
        package = read_json(package_path)
        assert isinstance(package, dict)
        assert package["version"] == EXPECTED_VERSION

    for lock_path in ("web/package-lock.json", "site/package-lock.json"):
        lock = read_json(lock_path)
        assert isinstance(lock, dict)
        assert lock["version"] == EXPECTED_VERSION
        assert lock["packages"][""]["version"] == EXPECTED_VERSION

    installer = read_text("packaging/windows/VideoScope.iss")
    assert f'#define MyAppVersion "{EXPECTED_VERSION}"' in installer
    assert '#define MyVersionInfoVersion "0.8.1.0"' in installer

    citation = read_text("CITATION.cff")
    assert re.search(r"(?m)^version: 0\.8\.1$", citation)
    assert "date-released:" not in citation

    assert smoke_test.EXPECTED_VERSION == f"VideoScope {EXPECTED_VERSION}"
    assert smoke_test.EXPECTED_DISTRIBUTION_PREFIX == (
        f"genvideoscope-{EXPECTED_VERSION}-"
    )


def test_candidate_download_and_release_documents_agree() -> None:
    """Candidate-facing install and release documents must name v0.8.1."""
    readme = read_text("README.md")
    connector_install = read_text("site/src/config/connector-install.ts")
    assert EXPECTED_DOWNLOAD_URL in readme
    assert EXPECTED_DOWNLOAD_URL in connector_install

    notes = read_text("docs/releases/v0.8.1-notes.md")
    checklist = read_text("docs/releases/v0.8.1-checklist.md")
    assert "draft candidate; not tagged or published" in notes
    assert "The automated PREPARE gates are green." in notes
    assert "Two Windows reliability blockers remain open" not in notes
    assert "PREPARE-ONLY; not tagged or published" in checklist
    assert EXPECTED_WHEEL in notes
    assert f"Reserved tag: `{EXPECTED_TAG}`" in checklist


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
