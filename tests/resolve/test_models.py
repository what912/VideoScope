"""Contract tests for strict Publish Ready domain models."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import ValidationError

from videoscope.domain import VideoMetadata
from videoscope.resolve import (
    EXPECTED_PUBLISH_ARTIFACTS,
    PUBLISH_PREVIEW_ARTIFACT,
    PublishAction,
    PublishActionKind,
    PublishBackend,
    PublishEffectiveConfig,
    PublishPlan,
    PublishProfileId,
    VerificationCheck,
    VerificationStatus,
    make_publish_plan_digest,
)

_TASK_ID = "1" * 32
_EFFECTIVE_CONFIG = PublishEffectiveConfig(
    preview_seconds=6.0,
    keep_workspace=False,
    run_diagnostics=True,
)


@pytest.fixture  # type: ignore[untyped-decorator]
def sample_metadata() -> VideoMetadata:
    """Provide safe normalized source metadata."""
    return VideoMetadata(
        filename="示例 视频.mp4",
        container_format="mp4",
        codec="h264",
        width=1920,
        height=1080,
        duration_seconds=4.0,
        average_frame_rate=30.0,
        estimated_frame_count=120,
        has_audio=True,
        file_size_bytes=1024,
    )


@pytest.fixture  # type: ignore[untyped-decorator]
def sample_actions() -> tuple[PublishAction, ...]:
    """Provide the ordered, non-semantic actions for one plan."""
    return (
        PublishAction(
            action_id="transcode",
            kind=PublishActionKind.TRANSCODE,
            description="转码为兼容 MP4",
            parameters={"video_codec": "h264"},
            affects=("video",),
            changes_content_semantics=False,
            confirmation_required=False,
        ),
        PublishAction(
            action_id="strip_metadata",
            kind=PublishActionKind.STRIP_METADATA,
            description="移除可识别元数据",
            parameters={},
            affects=("metadata",),
            changes_content_semantics=False,
            confirmation_required=False,
        ),
    )


def make_plan(
    sample_metadata: VideoMetadata,
    sample_actions: tuple[PublishAction, ...],
    *,
    output_filename: str = "publish-ready.mp4",
) -> PublishPlan:
    """Build a hand-specified valid plan for model boundary tests."""
    digest = make_publish_plan_digest(
        task_id=_TASK_ID,
        input_hash="a" * 64,
        source_read_only=True,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
        profile_version="1.0.0",
        backend=PublishBackend.NATIVE_LOCAL,
        actions=sample_actions,
        preview_artifact=PUBLISH_PREVIEW_ARTIFACT,
        confirmation_required=True,
        expected_artifacts=EXPECTED_PUBLISH_ARTIFACTS,
        effective_config=_EFFECTIVE_CONFIG,
        output_filename=output_filename,
    )
    return PublishPlan(
        task_id=_TASK_ID,
        input_hash="a" * 64,
        source_metadata=sample_metadata,
        source_read_only=True,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
        profile_version="1.0.0",
        backend=PublishBackend.NATIVE_LOCAL,
        actions=sample_actions,
        preview_artifact=PUBLISH_PREVIEW_ARTIFACT,
        confirmation_required=True,
        expected_artifacts=EXPECTED_PUBLISH_ARTIFACTS,
        effective_config=_EFFECTIVE_CONFIG,
        output_filename=output_filename,
        plan_digest=digest,
    )


def test_public_models_reject_undeclared_fields() -> None:
    """An added public JSON key must not silently pass validation."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PublishAction.model_validate(
            {
                "action_id": "remux",
                "kind": "remux",
                "description": "重新封装",
                "parameters": {},
                "affects": ["container"],
                "changes_content_semantics": False,
                "confirmation_required": False,
                "unexpected": True,
            }
        )


def test_plan_rejects_invalid_source_hash(
    sample_metadata: VideoMetadata,
    sample_actions: tuple[PublishAction, ...],
) -> None:
    """A non-SHA-256 source identifier must not create a plan."""
    payload = make_plan(sample_metadata, sample_actions).model_dump(mode="python")
    payload["input_hash"] = "not-a-digest"
    with pytest.raises(ValidationError, match="input_hash"):
        PublishPlan.model_validate(payload)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "relative_path",
    [
        "/publish-ready.mp4",
        "../cover.jpg",
        "preview\\cover.jpg",
        "C:/outside.mp4",
        "C:outside.mp4",
    ],
)
def test_artifact_paths_cannot_escape_output_root(relative_path: str) -> None:
    """Absolute and parent-traversal paths must be rejected at the boundary."""
    from videoscope.resolve import PublishArtifact

    with pytest.raises(ValidationError, match="normalized relative POSIX path"):
        PublishArtifact(
            relative_path=relative_path,
            sha256="a" * 64,
            description="输出文件",
        )


def test_plan_rejects_duplicate_action_ids(
    sample_metadata: VideoMetadata,
    sample_actions: tuple[PublishAction, ...],
) -> None:
    """Duplicate action IDs would make a confirmed plan ambiguous."""
    duplicate = sample_actions[0].model_copy(update={"kind": PublishActionKind.REMUX})
    with pytest.raises(ValidationError, match="duplicate action_id"):
        make_plan(sample_metadata, (sample_actions[0], duplicate))


def test_plan_rejects_reversed_action_order(
    sample_metadata: VideoMetadata,
    sample_actions: tuple[PublishAction, ...],
) -> None:
    """Actions must retain the stable declared processing order."""
    with pytest.raises(ValidationError, match="stable action order"):
        make_plan(sample_metadata, tuple(reversed(sample_actions)))


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "output_filename",
    ["/publish-ready.mp4", "C:/publish-ready.mp4", "C:publish-ready.mp4"],
)
def test_plan_rejects_an_output_filename_outside_the_output_root(
    sample_metadata: VideoMetadata,
    sample_actions: tuple[PublishAction, ...],
    output_filename: str,
) -> None:
    """Absolute and drive-qualified public output paths must be rejected."""
    with pytest.raises(ValidationError, match="normalized relative POSIX path"):
        make_plan(
            sample_metadata,
            sample_actions,
            output_filename=output_filename,
        )


def test_action_rejects_content_semantic_changes() -> None:
    """This MVP cannot schedule an action that changes content semantics."""
    with pytest.raises(ValidationError, match="content semantics"):
        PublishAction(
            action_id="crop",
            kind=PublishActionKind.SCALE_PAD,
            description="裁剪画面",
            parameters={},
            affects=("video",),
            changes_content_semantics=True,
            confirmation_required=True,
        )


def test_verification_check_rejects_unknown_status() -> None:
    """Verification outcomes are limited to the versioned status enum."""
    with pytest.raises(ValidationError):
        VerificationCheck(
            check_id="decodable",
            status=cast("VerificationStatus", "unknown"),
            message="无法判定",
            measured={},
        )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("check_status", "report_status", "expected_status"),
    [
        (
            VerificationStatus.FAILED,
            VerificationStatus.NEEDS_REVIEW,
            VerificationStatus.FAILED,
        ),
        (
            VerificationStatus.NEEDS_REVIEW,
            VerificationStatus.PASSED,
            VerificationStatus.NEEDS_REVIEW,
        ),
        (
            VerificationStatus.NEEDS_REVIEW,
            VerificationStatus.FAILED,
            VerificationStatus.NEEDS_REVIEW,
        ),
        (
            VerificationStatus.PASSED,
            VerificationStatus.NEEDS_REVIEW,
            VerificationStatus.PASSED,
        ),
    ],
)
def test_verification_report_requires_the_precedence_derived_status(
    check_status: VerificationStatus,
    report_status: VerificationStatus,
    expected_status: VerificationStatus,
) -> None:
    """Aggregate status must exactly reflect failed then review check precedence."""
    from videoscope.resolve import VerificationReport

    with pytest.raises(ValidationError, match=expected_status.value):
        VerificationReport(
            profile_id=PublishProfileId.COMPATIBLE_MP4,
            profile_version="1.0.0",
            status=report_status,
            checks=(
                VerificationCheck(
                    check_id="decodable",
                    status=check_status,
                    message="输出不可解码",
                    measured={},
                ),
            ),
        )


def test_plan_round_trip_preserves_chinese_descriptions(
    sample_metadata: VideoMetadata,
    sample_actions: tuple[PublishAction, ...],
) -> None:
    """JSON-compatible model values preserve unescaped Unicode text."""
    plan = make_plan(sample_metadata, sample_actions)

    restored = PublishPlan.model_validate_json(plan.model_dump_json())

    assert restored == plan
    assert restored.actions[0].description == "转码为兼容 MP4"


def test_plan_digest_is_deterministic(
    sample_actions: tuple[PublishAction, ...],
) -> None:
    first = make_publish_plan_digest(
        task_id=_TASK_ID,
        input_hash="a" * 64,
        source_read_only=True,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
        profile_version="1.0.0",
        backend=PublishBackend.NATIVE_LOCAL,
        actions=sample_actions,
        preview_artifact=PUBLISH_PREVIEW_ARTIFACT,
        confirmation_required=True,
        expected_artifacts=EXPECTED_PUBLISH_ARTIFACTS,
        effective_config=_EFFECTIVE_CONFIG,
        output_filename="publish-ready.mp4",
    )
    second = make_publish_plan_digest(
        task_id=_TASK_ID,
        input_hash="a" * 64,
        source_read_only=True,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
        profile_version="1.0.0",
        backend=PublishBackend.NATIVE_LOCAL,
        actions=sample_actions,
        preview_artifact=PUBLISH_PREVIEW_ARTIFACT,
        confirmation_required=True,
        expected_artifacts=EXPECTED_PUBLISH_ARTIFACTS,
        effective_config=_EFFECTIVE_CONFIG,
        output_filename="publish-ready.mp4",
    )
    assert first == second
    assert len(first) == 64
