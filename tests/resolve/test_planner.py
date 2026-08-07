"""Tests for deterministic, planning-only Publish Ready actions."""

from __future__ import annotations

import pytest

from videoscope.domain import VideoMetadata
from videoscope.resolve.models import (
    PublishActionKind,
    PublishBackend,
    PublishProfileId,
)
from videoscope.resolve.planner import build_publish_plan


@pytest.fixture  # type: ignore[untyped-decorator]
def landscape_metadata() -> VideoMetadata:
    return VideoMetadata(
        filename="private customer source video.mp4",
        container_format="mov,mp4,m4a,3gp,3g2,mj2",
        codec="h264",
        width=1920,
        height=1080,
        duration_seconds=4.0,
        average_frame_rate=30.0,
        estimated_frame_count=120,
        has_audio=True,
        file_size_bytes=1024,
        raw_probe={"pixel_format": "yuv420p", "audio_codec": "aac"},
    )


def test_compatible_plan_remuxes_only_already_compatible_media(
    landscape_metadata: VideoMetadata,
) -> None:
    plan = build_publish_plan(
        landscape_metadata,
        input_hash="a" * 64,
        profile_id=PublishProfileId.COMPATIBLE_MP4,
    )

    assert plan.backend is PublishBackend.NATIVE_LOCAL
    assert plan.output_filename == "publish-ready.mp4"
    assert [action.kind for action in plan.actions] == [
        PublishActionKind.REMUX,
        PublishActionKind.STRIP_METADATA,
        PublishActionKind.FASTSTART,
        PublishActionKind.EXTRACT_COVER,
    ]
    assert plan.actions[0].parameters == {"container": "mp4"}
    assert all(action.changes_content_semantics is False for action in plan.actions)
    assert all(action.confirmation_required is False for action in plan.actions)


def test_codec_incompatible_media_is_transcoded(
    landscape_metadata: VideoMetadata,
) -> None:
    metadata = landscape_metadata.model_copy(update={"codec": "hevc"})

    plan = build_publish_plan(
        metadata,
        input_hash="a" * 64,
        profile_id=PublishProfileId.COMPATIBLE_MP4,
    )

    assert plan.actions[0].kind is PublishActionKind.TRANSCODE
    assert plan.actions[0].parameters == {
        "container": "mp4",
        "video_codec": "h264",
        "audio_codec": "aac",
        "pixel_format": "yuv420p",
        "maximum_fps": 60.0,
    }


def test_vertical_plan_preserves_content_with_scale_and_pad(
    landscape_metadata: VideoMetadata,
) -> None:
    plan = build_publish_plan(
        landscape_metadata,
        input_hash="a" * 64,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
    )
    scale = next(
        action for action in plan.actions if action.kind is PublishActionKind.SCALE_PAD
    )

    assert scale.parameters == {
        "width": 1080,
        "height": 1920,
        "mode": "fit",
        "pad_color": "black",
    }
    assert scale.changes_content_semantics is False
    assert [action.kind for action in plan.actions] == [
        PublishActionKind.TRANSCODE,
        PublishActionKind.SCALE_PAD,
        PublishActionKind.STRIP_METADATA,
        PublishActionKind.FASTSTART,
        PublishActionKind.EXTRACT_COVER,
    ]


def test_horizontal_plan_preserves_content_with_scale_and_pad(
    landscape_metadata: VideoMetadata,
) -> None:
    plan = build_publish_plan(
        landscape_metadata,
        input_hash="b" * 64,
        profile_id=PublishProfileId.SOCIAL_HORIZONTAL,
    )
    scale = next(
        action for action in plan.actions if action.kind is PublishActionKind.SCALE_PAD
    )

    assert scale.parameters == {
        "width": 1920,
        "height": 1080,
        "mode": "fit",
        "pad_color": "black",
    }
    assert scale.changes_content_semantics is False


def test_high_frame_rate_forces_transcode_with_profile_limit(
    landscape_metadata: VideoMetadata,
) -> None:
    metadata = landscape_metadata.model_copy(update={"average_frame_rate": 120.0})

    plan = build_publish_plan(
        metadata,
        input_hash="c" * 64,
        profile_id=PublishProfileId.COMPATIBLE_MP4,
    )

    assert plan.actions[0].kind is PublishActionKind.TRANSCODE
    assert plan.actions[0].parameters["maximum_fps"] == 60.0


def test_transcode_audio_parameter_matches_source_audio_presence(
    landscape_metadata: VideoMetadata,
) -> None:
    with_audio = build_publish_plan(
        landscape_metadata.model_copy(update={"codec": "hevc"}),
        input_hash="d" * 64,
        profile_id=PublishProfileId.COMPATIBLE_MP4,
    )
    without_audio = build_publish_plan(
        landscape_metadata.model_copy(
            update={
                "codec": "hevc",
                "has_audio": False,
                "raw_probe": {"pixel_format": "yuv420p"},
            }
        ),
        input_hash="e" * 64,
        profile_id=PublishProfileId.COMPATIBLE_MP4,
    )

    assert with_audio.actions[0].parameters["audio_codec"] == "aac"
    assert "audio_codec" not in without_audio.actions[0].parameters


def test_plan_redacts_source_filename_and_has_deterministic_digest(
    landscape_metadata: VideoMetadata,
) -> None:
    first = build_publish_plan(
        landscape_metadata,
        input_hash="f" * 64,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
    )
    second = build_publish_plan(
        landscape_metadata,
        input_hash="f" * 64,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
    )

    assert landscape_metadata.filename not in str(first.model_dump(mode="json"))
    assert first.source_metadata.filename == "source"
    assert first.actions == second.actions
    assert first.plan_digest == second.plan_digest
    assert len(first.plan_digest) == 64


def test_plan_records_every_confirmation_relevant_contract_field(
    landscape_metadata: VideoMetadata,
) -> None:
    """The digest-bearing plan must expose the full normative confirmation contract."""
    plan = build_publish_plan(
        landscape_metadata,
        input_hash="1" * 64,
        profile_id=PublishProfileId.SOCIAL_VERTICAL,
        preview_seconds=2.5,
        keep_workspace=True,
        run_diagnostics=False,
    )

    assert len(plan.task_id) == 32
    assert plan.source_read_only is True
    assert plan.preview_artifact == "preview/publish-preview.mp4"
    assert plan.confirmation_required is True
    assert plan.expected_artifacts == (
        "plan.json",
        "preview/publish-preview.mp4",
        "publish-ready.mp4",
        "cover.jpg",
        "changes.json",
        "technical-report.json",
        "analysis-before/report.json",
        "analysis-after/report.json",
    )
    assert plan.effective_config.model_dump() == {
        "preview_seconds": 2.5,
        "keep_workspace": True,
        "run_diagnostics": False,
    }
