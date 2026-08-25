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
CANDIDATE_VERSION = "0.8.1"
PUBLISHED_ASSET_VERSION = "0.8.0"
EXPECTED_TAG = f"v{CANDIDATE_VERSION}"
EXPECTED_WHEEL = f"genvideoscope-{CANDIDATE_VERSION}-py3-none-any.whl"
PUBLISHED_WHEEL = f"genvideoscope-{PUBLISHED_ASSET_VERSION}-py3-none-any.whl"
PUBLISHED_DOWNLOAD_URL = (
    "https://github.com/what912/VideoScope/releases/download/"
    f"v{PUBLISHED_ASSET_VERSION}/{PUBLISHED_WHEEL}"
)
CANDIDATE_DOWNLOAD_PREFIX = (
    "https://github.com/what912/VideoScope/releases/download/v0.8.1/"
)
CANDIDATE_WHEEL_DOWNLOAD_URL = f"{CANDIDATE_DOWNLOAD_PREFIX}{EXPECTED_WHEEL}"
HISTORICAL_PREPARE_COMMIT = "a155c2cbdd14081682ea57493afc34b9d135f963"
TONAL_FIX_COMMIT = "912fe467192f615e9ad3f6c338fbd388ac1a065a"


def read_text(relative_path: str) -> str:
    """Read one tracked UTF-8 release surface."""
    return (REPOSITORY / relative_path).read_text(encoding="utf-8")


def read_json(relative_path: str) -> object:
    """Parse one tracked JSON release surface."""
    return json.loads(read_text(relative_path))


def test_active_release_version_surfaces_agree() -> None:
    """Every executable or build-facing version must identify v0.8.1."""
    pyproject = tomllib.loads(read_text("pyproject.toml"))
    assert pyproject["project"]["version"] == CANDIDATE_VERSION

    package_init = read_text("src/videoscope/__init__.py")
    assert f'__version__ = "{CANDIDATE_VERSION}"' in package_init

    for package_path in ("web/package.json", "site/package.json"):
        package = read_json(package_path)
        assert isinstance(package, dict)
        assert package["version"] == CANDIDATE_VERSION

    for lock_path in ("web/package-lock.json", "site/package-lock.json"):
        lock = read_json(lock_path)
        assert isinstance(lock, dict)
        assert lock["version"] == CANDIDATE_VERSION
        assert lock["packages"][""]["version"] == CANDIDATE_VERSION

    installer = read_text("packaging/windows/VideoScope.iss")
    assert f'#define MyAppVersion "{CANDIDATE_VERSION}"' in installer
    assert '#define MyVersionInfoVersion "0.8.1.0"' in installer

    citation = read_text("CITATION.cff")
    assert re.search(r"(?m)^version: 0\.8\.1$", citation)
    assert "date-released:" not in citation

    assert smoke_test.EXPECTED_VERSION == f"VideoScope {CANDIDATE_VERSION}"
    assert smoke_test.EXPECTED_DISTRIBUTION_PREFIX == (
        f"genvideoscope-{CANDIDATE_VERSION}-"
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


def test_candidate_and_published_download_surfaces_are_separate() -> None:
    """A PREPARE candidate must not advertise assets that are not published."""
    readme = read_text("README.md")
    connector_install = read_text("site/src/config/connector-install.ts")
    assert PUBLISHED_DOWNLOAD_URL in readme
    assert PUBLISHED_DOWNLOAD_URL in connector_install
    assert CANDIDATE_DOWNLOAD_PREFIX not in readme
    assert CANDIDATE_WHEEL_DOWNLOAD_URL not in connector_install
    assert (
        "currently published stable `v0.8.0` release, not the `v0.8.1` "
        "development candidate"
    ) in readme
    assert "安装公开的 GitHub 开发候选版" not in readme

    notes = read_text("docs/releases/v0.8.1-notes.md")
    checklist = read_text("docs/releases/v0.8.1-checklist.md")
    assert "draft candidate; not tagged or published" in notes
    assert "historical" in notes.lower()
    assert "stale" in notes.lower()
    assert "The automated PREPARE gates are green." not in notes
    assert "The automated PREPARE gates are green." not in checklist
    assert "current candidate is frozen" not in notes.lower()
    assert "current candidate is frozen" not in checklist.lower()
    assert "Two Windows reliability blockers remain open" not in notes
    assert "PREPARE-ONLY; not tagged or published" in checklist
    assert EXPECTED_WHEEL in notes
    assert "release-evidence.json" in notes
    assert "release-evidence.json" in checklist
    assert f"Reserved tag: `{EXPECTED_TAG}`" in checklist
    for document in (notes, checklist):
        assert (
            f"Historical PREPARE evidence commit: `{HISTORICAL_PREPARE_COMMIT}`."
            in document
        )
        assert "This evidence does not cover any later commit." in document
        assert (
            f"The tonal probe retry fix is committed at `{TONAL_FIX_COMMIT}`; "
            "the release-compliance changes are also committed in the current "
            "candidate history."
        ) in document
        assert "remain uncommitted" not in document.lower()


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
