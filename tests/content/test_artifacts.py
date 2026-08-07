"""Atomic private/public useful-content artifact boundaries."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.content.test_verification import make_plan, mappings_for, passing_evidence
from videoscope.content.artifacts import (
    ContentArtifactLayout,
    publish_verified_content,
    validate_public_tree,
)
from videoscope.content.errors import ContentArtifactError, ContentCancelledError
from videoscope.content.models import ContentPlan, ContentVerificationReport
from videoscope.content.verification import verify_content_result


def verified_plan() -> tuple[ContentPlan, ContentVerificationReport]:
    plan = make_plan()
    verification = verify_content_result(
        plan=plan,
        mappings=mappings_for(plan),
        evidence=passing_evidence(),
    )
    return plan, verification


def bundle_inputs(
    tmp_path: Path,
    declared: tuple[str, ...],
) -> tuple[dict[str, Path], dict[str, str]]:
    media = tmp_path / "staged useful.mp4"
    media.write_bytes(b"verified-media")
    files = {"content-output/useful-content.mp4": media}
    documents = {
        path: (
            "<!doctype html><html><body>offline report</body></html>"
            if path.endswith(".html")
            else "{}"
        )
        for path in declared
        if path not in files
    }
    return files, documents


def test_private_allowlist_accepts_review_files_and_rejects_traversal(
    tmp_path: Path,
) -> None:
    layout = ContentArtifactLayout.create(tmp_path / "job")
    layout.write_private_text("content-map.json", "{}")
    layout.write_private_text("preview/action-001.json", "{}")

    assert layout.validate_private_tree() == (
        "content-map.json",
        "preview/action-001.json",
    )
    with pytest.raises(ContentArtifactError):
        layout.write_private_text("../escape.json", "{}")
    with pytest.raises(ContentArtifactError):
        layout.write_private_text("unexpected.json", "{}")


def test_verified_exact_bundle_is_atomically_published(tmp_path: Path) -> None:
    plan, verification = verified_plan()
    layout = ContentArtifactLayout.create(tmp_path / "job")
    files, documents = bundle_inputs(tmp_path, plan.public_artifacts)

    artifacts = publish_verified_content(
        layout,
        plan=plan,
        verification=verification,
        file_sources=files,
        text_documents=documents,
    )

    assert layout.public_root.is_dir()
    assert validate_public_tree(layout, plan) == plan.public_artifacts
    assert tuple(item.relative_path for item in artifacts) == plan.public_artifacts
    assert all(len(item.sha256) == 64 for item in artifacts)


def test_incomplete_unexpected_or_unverified_bundle_never_becomes_public(
    tmp_path: Path,
) -> None:
    plan, verification = verified_plan()
    layout = ContentArtifactLayout.create(tmp_path / "job")
    files, documents = bundle_inputs(tmp_path, plan.public_artifacts)
    documents.pop("content-output/report.html")
    with pytest.raises(ContentArtifactError) as error:
        publish_verified_content(
            layout,
            plan=plan,
            verification=verification,
            file_sources=files,
            text_documents=documents,
        )
    assert "exact" in (error.value.internal_message or "")
    assert not layout.public_root.exists()

    failed = verification.model_copy(
        update={
            "checks": (
                verification.checks[0].model_copy(update={"status": "failed"}),
                *verification.checks[1:],
            )
        }
    )
    files, documents = bundle_inputs(tmp_path, plan.public_artifacts)
    with pytest.raises(ContentArtifactError) as error:
        publish_verified_content(
            layout,
            plan=plan,
            verification=failed,
            file_sources=files,
            text_documents=documents,
        )
    assert "does not permit" in (error.value.internal_message or "")


def test_cancellation_collision_and_private_leak_roll_back(tmp_path: Path) -> None:
    plan, verification = verified_plan()
    layout = ContentArtifactLayout.create(tmp_path / "job")
    files, documents = bundle_inputs(tmp_path, plan.public_artifacts)
    with pytest.raises(ContentCancelledError):
        publish_verified_content(
            layout,
            plan=plan,
            verification=verification,
            file_sources=files,
            text_documents=documents,
            cancellation_callback=lambda: True,
        )
    assert not layout.public_root.exists()

    documents["content-output/report.html"] = "content-review-private/transcript"
    with pytest.raises(ContentArtifactError) as error:
        publish_verified_content(
            layout,
            plan=plan,
            verification=verification,
            file_sources=files,
            text_documents=documents,
        )
    assert "private" in (error.value.internal_message or "")

    layout.public_root.mkdir()
    with pytest.raises(ContentArtifactError) as error:
        publish_verified_content(
            layout,
            plan=plan,
            verification=verification,
            file_sources=files,
            text_documents={
                **documents,
                "content-output/report.html": "<html>offline</html>",
            },
        )
    assert "already exists" in (error.value.internal_message or "")


def test_links_are_rejected_and_expiry_removes_only_private_state(
    tmp_path: Path,
) -> None:
    layout = ContentArtifactLayout.create(tmp_path / "job")
    layout.write_private_text("content-map.json", "{}")
    layout.public_root.mkdir()
    (layout.public_root / "sentinel.txt").write_text("public", encoding="utf-8")

    assert layout.expire_private(
        maximum_age_seconds=1, now=os.path.getmtime(layout.private_root) + 2
    )
    assert not layout.private_root.exists()
    assert (layout.public_root / "sentinel.txt").is_file()

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-job"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    with pytest.raises(ContentArtifactError, match="link"):
        ContentArtifactLayout.create(link)
