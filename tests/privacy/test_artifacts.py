"""Security tests for physical Safe Sharing artifact separation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

from videoscope.privacy.artifacts import PrivacyArtifactLayout
from videoscope.privacy.errors import PrivacyArtifactError


def test_layout_creates_only_private_and_share_roots(tmp_path: Path) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)

    assert layout.private_root == tmp_path.resolve() / "privacy-review-private"
    assert layout.public_root == tmp_path.resolve() / "share-package"
    assert {path.name for path in tmp_path.iterdir()} == {
        "privacy-review-private",
        "share-package",
    }


def test_private_evidence_cannot_be_published(tmp_path: Path) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)
    private = layout.private_root / "evidence" / "raw.png"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"private")

    with pytest.raises(PrivacyArtifactError):
        layout.public_relative_path(private)


def test_share_manifest_rejects_absolute_and_sensitive_paths(tmp_path: Path) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)

    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"evidence": str(tmp_path.resolve())})
    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"evidence": "/srv/private/video.mp4"})
    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"username": "local-user"})
    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"private_evidence": [{"ocr_text": "secret"}]})
    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"sanitized_metadata_value": "Alice"})
    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"coordinates": "31.2304, 121.4737"})


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "value",
    (
        "file:///C:/Users/person/private.mp4",
        "FILE:///home/person/private.mp4",
        "file%3A///C%3A/Users/person/private.mp4",
        "FILE%253A///home/person/private.mp4",
    ),
)
def test_share_manifest_rejects_file_uri_variants(
    tmp_path: Path,
    value: str,
) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)

    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"evidence": value})


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "value",
    (
        "file%25253A///C%253A/Users/person/private.mp4",
        "FILE%252525253A///home/person/private.mp4",
    ),
)
def test_share_manifest_rejects_deeply_encoded_file_uri(
    tmp_path: Path,
    value: str,
) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)

    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"evidence": value})


def test_share_manifest_conservatively_rejects_percent_decoding_past_limit(
    tmp_path: Path,
) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)
    value = "file:///C:/Users/person/private.mp4"
    for _ in range(12):
        value = quote(value, safe="/")

    with pytest.raises(PrivacyArtifactError):
        layout.validate_share_manifest({"evidence": value})


def test_share_manifest_allows_stable_non_path_percent_text(tmp_path: Path) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)

    layout.validate_share_manifest({"description": "100% locally reviewed"})


def test_public_relative_path_rejects_escape_and_unknown_filename(
    tmp_path: Path,
) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    unknown = layout.public_root / "raw-evidence.png"
    unknown.write_bytes(b"raw")

    with pytest.raises(PrivacyArtifactError):
        layout.public_relative_path(outside)
    with pytest.raises(PrivacyArtifactError) as error:
        layout.public_relative_path(unknown)
    assert error.value.internal_message is not None
    assert "allowlisted" in error.value.internal_message


def test_public_tree_rejects_sensitive_json_content(tmp_path: Path) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)
    report = layout.public_root / "technical-report.json"
    report.write_text(
        json.dumps({"input_path": "C:\\Users\\person\\private.mp4"}),
        encoding="utf-8",
    )

    with pytest.raises(PrivacyArtifactError):
        layout.validate_public_tree()


def test_public_path_rejects_symlink_escape_when_supported(tmp_path: Path) -> None:
    layout = PrivacyArtifactLayout.create(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = layout.public_root / "preview"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable on this platform: {exc}")
    escaped = link / "privacy-preview.mp4"
    escaped.write_bytes(b"private")

    with pytest.raises(PrivacyArtifactError):
        layout.public_relative_path(escaped)


@pytest.mark.skipif(  # type: ignore[untyped-decorator]
    sys.platform != "win32", reason="Windows junction regression"
)
def test_layout_rejects_junction_job_root_without_python_312_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "outside target"
    target.mkdir()
    junction = tmp_path / "job junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        pytest.skip(
            f"Windows junction creation unavailable: {completed.stderr.strip()}"
        )
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    try:
        with pytest.raises(PrivacyArtifactError):
            PrivacyArtifactLayout.create(junction)
    finally:
        junction.rmdir()


@pytest.mark.skipif(  # type: ignore[untyped-decorator]
    sys.platform != "win32", reason="Windows junction regression"
)
def test_layout_rejects_junction_ancestor_before_creating_job_root(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside parent"
    target.mkdir()
    junction = tmp_path / "parent junction"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        pytest.skip(
            f"Windows junction creation unavailable: {completed.stderr.strip()}"
        )
    child = junction / "new job"
    try:
        with pytest.raises(PrivacyArtifactError):
            PrivacyArtifactLayout.create(child)
        assert not (target / "new job").exists()
    finally:
        junction.rmdir()
