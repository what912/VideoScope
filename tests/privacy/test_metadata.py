"""Tests for private ffprobe summaries and metadata privacy observations."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from videoscope.domain import Severity
from videoscope.privacy.metadata import (
    MetadataPrivacyScanner,
    PrivateProbeSummary,
    PrivateProbeTagSet,
    private_probe_summary_from_ffprobe,
)
from videoscope.privacy.models import PrivacyRiskType, RedactionStyle
from videoscope.privacy.profiles import get_share_audience_profile


def make_tagged_probe_summary() -> PrivateProbeSummary:
    return PrivateProbeSummary(
        duration_seconds=6.0,
        filename="trip source.mp4",
        global_tags={"location": "+31.2304+121.4737/", "encoder": "Phone App"},
        stream_tags=(
            PrivateProbeTagSet(
                scope="video_stream",
                index=0,
                tags={"author": "Alice", "title": "Project launch"},
            ),
        ),
        chapter_tags=(
            PrivateProbeTagSet(
                scope="chapter",
                index=0,
                tags={"title": "Home address"},
            ),
        ),
    )


def test_metadata_scanner_reports_private_global_stream_and_chapter_tags() -> None:
    risks = MetadataPrivacyScanner().scan(
        metadata=make_tagged_probe_summary(),
        input_hash="a" * 64,
        profile=get_share_audience_profile("public"),
    )

    observed = {(risk.metadata_scope, risk.metadata_key) for risk in risks}
    assert ("global", "location") in observed
    assert ("video_stream", "author") in observed
    assert ("chapter", "title") in observed


def test_metadata_risks_are_unique_deterministic_and_keep_values_private() -> None:
    scanner = MetadataPrivacyScanner()
    metadata = make_tagged_probe_summary()
    profile = get_share_audience_profile("public")

    first = scanner.scan(metadata, "a" * 64, profile)
    second = scanner.scan(metadata, "a" * 64, profile)

    assert [risk.id for risk in first] == [risk.id for risk in second]
    assert len({risk.id for risk in first}) == len(first)
    assert all(risk.risk_type is PrivacyRiskType.METADATA for risk in first)
    assert all(
        risk.recommended_style is RedactionStyle.REMOVE_METADATA for risk in first
    )
    assert all(risk.severity in Severity for risk in first)
    assert "Alice" not in str([risk.evidence for risk in first])
    assert "Alice" in str([risk.private_evidence for risk in first])


def test_profile_policy_filters_categories_not_forbidden_for_family() -> None:
    risks = MetadataPrivacyScanner().scan(
        metadata=make_tagged_probe_summary(),
        input_hash="b" * 64,
        profile=get_share_audience_profile("family"),
    )

    assert all(risk.metadata_key != "title" for risk in risks)
    assert any(risk.metadata_key == "location" for risk in risks)


def test_attachment_arbitrary_keys_remain_private() -> None:
    metadata = PrivateProbeSummary(
        duration_seconds=2.0,
        filename="source.mp4",
        attachment_tags=(
            PrivateProbeTagSet(
                scope="attachment",
                index=4,
                tags={
                    "alice@example.com": "private-user-name",
                    "filename": "private résumé.txt",
                },
            ),
        ),
    )

    risks = MetadataPrivacyScanner().scan(
        metadata,
        "c" * 64,
        get_share_audience_profile("public"),
    )

    attachment = next(risk for risk in risks if risk.metadata_scope == "attachment")
    public_payload = str(
        {
            "scanner_id": attachment.scanner_id,
            "metadata_key": attachment.metadata_key,
            "evidence": attachment.evidence,
            "title": attachment.title,
            "description": attachment.public_description,
        }
    )
    assert attachment.metadata_key == "attachment"
    assert "alice@example.com" not in public_payload
    assert "private-user-name" not in public_payload
    assert "alice@example.com" in str(attachment.private_evidence)
    assert "private-user-name" in str(attachment.private_evidence)


def test_private_probe_summary_sanitizes_keys_values_and_structures_scopes() -> None:
    payload = {
        "format": {
            "duration": "6",
            "tags": {" Location ": "+31.2+121.4/\u0000", "empty": "\u0000"},
        },
        "streams": [
            {
                "index": 2,
                "codec_type": "video",
                "tags": {" AUTHOR ": " 张三\n Studio "},
            },
            {
                "index": 3,
                "codec_type": "attachment",
                "tags": {"filename": "private résumé.txt"},
            },
        ],
        "chapters": [{"id": 9, "tags": {"TITLE": "家庭 地址"}}],
    }

    summary = private_probe_summary_from_ffprobe(
        payload,
        filename="源 视频 ü.mp4",
        duration_seconds=6.0,
    )

    assert summary.filename == "源 视频 ü.mp4"
    assert summary.global_tags == {"location": "+31.2+121.4/"}
    assert summary.stream_tags[0].scope == "video_stream"
    assert summary.stream_tags[0].tags == {"author": "张三 Studio"}
    assert summary.attachment_tags[0].tags["filename"] == "private résumé.txt"
    assert summary.chapter_tags[0].tags == {"title": "家庭 地址"}


def test_private_probe_summary_classifies_mp4_attached_picture_as_attachment() -> None:
    payload = {
        "format": {"tags": {}},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "disposition": {"attached_pic": 0},
                "tags": {"title": "Main video"},
            },
            {
                "index": 2,
                "codec_type": "video",
                "disposition": {"attached_pic": 1},
            },
        ],
    }

    summary = private_probe_summary_from_ffprobe(
        payload,
        filename="input.mp4",
        duration_seconds=4.0,
    )

    assert [tag_set.index for tag_set in summary.stream_tags] == [0]
    assert len(summary.attachment_tags) == 1
    assert summary.attachment_tags[0].index == 2
    assert summary.attachment_tags[0].scope == "attachment"
    assert summary.attachment_tags[0].tags == {"attachment": "attached_picture"}


def test_private_probe_tags_never_enter_public_video_metadata() -> None:
    from videoscope.video.probe import metadata_from_ffprobe

    payload = {
        "format": {
            "format_name": "mov,mp4",
            "duration": "1",
            "tags": {"location": "+31.2+121.4/", "author": "Alice"},
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 16,
                "height": 16,
                "duration": "1",
                "avg_frame_rate": "1/1",
                "tags": {"title": "Private title"},
            }
        ],
    }

    # The public model remains deliberately limited to normalized technical fields.
    # A local temporary file is unnecessary because only stat() is required here.
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.mp4"
        source.write_bytes(b"video")
        public = metadata_from_ffprobe(payload, input_path=source)

    serialized = public.model_dump_json()
    assert "Alice" not in serialized
    assert "Private title" not in serialized
    assert "+31.2+121.4/" not in serialized


def test_private_probe_runs_ffprobe_once_and_separates_private_tags(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from videoscope.video.probe import probe_video_with_private_summary

    source = tmp_path / "客户 视频 ü.mp4"
    source.write_bytes(b"video")
    payload = {
        "format": {
            "format_name": "mov,mp4",
            "duration": "1",
            "tags": {"location": "+31.2+121.4/"},
        },
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "width": 16,
                "height": 16,
                "duration": "1",
                "avg_frame_rate": "1/1",
                "tags": {"author": "Alice"},
            }
        ],
    }
    calls = 0

    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        assert args[-1] == str(source)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr("videoscope.video.probe.subprocess.run", fake_run)

    public, private = probe_video_with_private_summary(source, ffprobe="fake-ffprobe")

    assert calls == 1
    assert "Alice" not in public.model_dump_json()
    assert private.global_tags["location"] == "+31.2+121.4/"
    assert private.stream_tags[0].tags["author"] == "Alice"
