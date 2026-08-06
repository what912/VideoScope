"""Real native Publish Ready coverage over the generated audio/video fixture."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import cast

import pytest
from PIL import Image

from scripts import generate_test_videos as fixture_factory
from videoscope.resolve import PublishProfileId, VerificationStatus
from videoscope.resolve.pipeline import (
    PublishReadyConfig,
    PublishReadyPipeline,
)
from videoscope.video import probe_video

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
MANIFEST_PATH = FIXTURE_ROOT / "manifest.json"
PUBLISH_SOURCE = FIXTURE_ROOT / "generated" / "publish_av.mp4"

PROFILE_DIMENSIONS = (
    pytest.param(
        PublishProfileId.COMPATIBLE_MP4,
        (320, 180),
        id="compatible-mp4",
    ),
    pytest.param(
        PublishProfileId.SOCIAL_VERTICAL,
        (1080, 1920),
        id="social-vertical",
    ),
    pytest.param(
        PublishProfileId.SOCIAL_HORIZONTAL,
        (1920, 1080),
        id="social-horizontal",
    ),
)


def _publish_fixture_manifest() -> dict[str, object]:
    raw_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest = cast(dict[str, object], raw_manifest)
    return cast(dict[str, object], manifest["publish_ready_fixture"])


def _local_video_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip(
            "FFmpeg and ffprobe are required for native Publish Ready fixture tests"
        )
    assert ffmpeg is not None
    assert ffprobe is not None
    return ffmpeg, ffprobe


def _run_media_command(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run one bounded fixture command and surface its sanitized test diagnostic."""
    completed = subprocess.run(
        list(arguments),
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return completed


def _tag_probe(ffprobe: str, path: Path) -> dict[str, object]:
    completed = _run_media_command(
        (
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags:chapter_tags",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        )
    )
    return cast(dict[str, object], json.loads(completed.stdout))


def _decoded_strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in cast(dict[str, object], value).values():
            yield from _decoded_strings(item)
    elif isinstance(value, list):
        for item in cast(list[object], value):
            yield from _decoded_strings(item)


def _values_named(value: object, name: str) -> Iterator[object]:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        if name in mapping:
            yield mapping[name]
        for item in mapping.values():
            yield from _values_named(item, name)
    elif isinstance(value, list):
        for item in cast(list[object], value):
            yield from _values_named(item, name)


def _assert_posix_relative_artifact_path(relative_path: str) -> None:
    assert "\\" not in relative_path
    posix_path = PurePosixPath(relative_path)
    windows_path = PureWindowsPath(relative_path)
    assert relative_path == posix_path.as_posix()
    assert not posix_path.is_absolute()
    assert not windows_path.is_absolute()
    assert not windows_path.drive
    assert "." not in posix_path.parts
    assert ".." not in posix_path.parts


def test_publish_audio_video_fixture_has_a_non_anomaly_manifest_contract() -> None:
    assert _publish_fixture_manifest() == {
        "audio": {
            "codec": "aac",
            "frequency_hz": 440,
            "source": "FFmpeg lavfi sine",
        },
        "duration_seconds": 4.0,
        "filename": "publish_av.mp4",
        "frame_rate": 12,
        "height": 180,
        "purpose": "Publish Ready profile end-to-end regression",
        "tolerance_seconds": 0.11,
        "video_codec": "mpeg4",
        "width": 320,
    }


def test_posix_artifact_path_check_rejects_backslash_separator() -> None:
    with pytest.raises(AssertionError):
        _assert_posix_relative_artifact_path("preview\\cover.jpg")


def test_expected_audio_is_fully_decoded_during_fixture_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        fixture_factory,
        "probe_video",
        lambda **_: fixture_factory.ProbeResult(4.0, 320, 180, 12.0, True),
    )

    def capture_command(
        args: Sequence[str],
        *,
        timeout_seconds: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        commands.append(tuple(args))
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr(fixture_factory, "run_checked", capture_command)

    fixture_factory.validate_video(
        ffmpeg="ffmpeg",
        ffprobe="ffprobe",
        video_path=tmp_path / "publish_av.mp4",
        expected_duration_seconds=4.0,
        expected_frame_rate=12,
        expected_audio=True,
    )

    decode_command = commands[-1]
    audio_map_index = decode_command.index("0:a:0")
    assert decode_command[audio_map_index - 1 : audio_map_index + 1] == (
        "-map",
        "0:a:0",
    )


def test_fixture_validation_rejects_stale_undersized_video(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        fixture_factory,
        "probe_video",
        lambda **_: fixture_factory.ProbeResult(4.0, 319, 180, 12.0, True),
    )
    monkeypatch.setattr(
        fixture_factory,
        "run_checked",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    with pytest.raises(fixture_factory.FixtureFactoryError, match="dimensions"):
        fixture_factory.validate_video(
            ffmpeg="ffmpeg",
            ffprobe="ffprobe",
            video_path=tmp_path / "publish_av.mp4",
            expected_duration_seconds=4.0,
            expected_frame_rate=12,
            expected_audio=True,
        )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("profile_id", "expected_dimensions"), PROFILE_DIMENSIONS
)
def test_real_publish_ready_profiles_preserve_source_and_pass_verification(
    tmp_path: Path,
    profile_id: PublishProfileId,
    expected_dimensions: tuple[int, int],
) -> None:
    _, ffprobe = _local_video_tools()
    assert PUBLISH_SOURCE.is_file(), (
        "publish_av.mp4 is missing; run "
        "python scripts/generate_test_videos.py --force first"
    )
    original_source_bytes = PUBLISH_SOURCE.read_bytes()
    output_directory = tmp_path / f"发布 输出 {profile_id.value}"
    pipeline = PublishReadyPipeline(
        PublishReadyConfig(
            profile_id=profile_id,
            output_directory=output_directory,
        )
    )

    preparation = pipeline.prepare(PUBLISH_SOURCE)
    result = pipeline.execute(
        preparation,
        confirmed_plan_digest=preparation.plan.plan_digest,
    )

    assert result.video_path.is_file()
    assert result.technical_report.verification.status is VerificationStatus.PASSED
    assert result.video_path.read_bytes() != original_source_bytes
    assert PUBLISH_SOURCE.read_bytes() == original_source_bytes

    json_reports = sorted(result.output_directory.rglob("*.json"))
    assert json_reports
    forbidden_paths = (
        tmp_path.resolve(),
        preparation.workspace_directory.resolve(),
        output_directory.resolve(),
        PUBLISH_SOURCE.resolve(),
    )
    forbidden_spellings = {
        spelling.casefold()
        for forbidden_path in forbidden_paths
        for spelling in (str(forbidden_path), forbidden_path.as_posix())
    }
    relative_artifact_paths: list[str] = []
    for report_path in json_reports:
        payload: object = json.loads(report_path.read_text(encoding="utf-8"))
        decoded_strings = tuple(item.casefold() for item in _decoded_strings(payload))
        assert all(
            forbidden not in value
            for forbidden in forbidden_spellings
            for value in decoded_strings
        )
        for value in _values_named(payload, "relative_path"):
            assert isinstance(value, str)
            relative_artifact_paths.append(value)

    assert relative_artifact_paths
    for relative_path in relative_artifact_paths:
        _assert_posix_relative_artifact_path(relative_path)

    output_metadata = probe_video(result.video_path, ffprobe=ffprobe)
    assert (output_metadata.width, output_metadata.height) == expected_dimensions
    assert output_metadata.codec == "h264"
    assert output_metadata.has_audio is True
    assert output_metadata.raw_probe["audio_codec"] == "aac"
    manifest = _publish_fixture_manifest()
    expected_duration = float(cast(float, manifest["duration_seconds"]))
    tolerance = float(cast(float, manifest["tolerance_seconds"]))
    assert abs(output_metadata.duration_seconds - expected_duration) <= tolerance

    assert result.cover_path.is_file()
    with Image.open(result.cover_path) as cover:
        cover.verify()
    with Image.open(result.cover_path) as cover:
        assert cover.width > 0
        assert cover.height > 0


def test_real_publish_strips_global_stream_and_chapter_source_metadata(
    tmp_path: Path,
) -> None:
    """Tagged source metadata must not survive the real remux publication path."""
    ffmpeg, ffprobe = _local_video_tools()
    metadata_path = tmp_path / "private metadata.ffmeta"
    metadata_path.write_text(
        ";FFMETADATA1\n"
        "title=PRIVATE_GLOBAL_TITLE\n"
        "comment=PRIVATE_GLOBAL_COMMENT\n"
        "[CHAPTER]\n"
        "TIMEBASE=1/1000\n"
        "START=0\n"
        "END=1000\n"
        "title=PRIVATE_CHAPTER_TITLE\n",
        encoding="utf-8",
    )
    source = tmp_path / "tagged 源 video.mp4"
    _run_media_command(
        (
            ffmpeg,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=12:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-f",
            "ffmetadata",
            "-i",
            str(metadata_path),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map_metadata",
            "2",
            "-map_chapters",
            "2",
            "-metadata:s:v:0",
            "title=PRIVATE_VIDEO_TITLE",
            "-metadata:s:a:0",
            "title=PRIVATE_AUDIO_TITLE",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        )
    )
    source_probe = _tag_probe(ffprobe, source)
    source_text = json.dumps(source_probe, sort_keys=True)
    assert all(
        token in source_text
        for token in (
            "PRIVATE_GLOBAL_TITLE",
            "PRIVATE_GLOBAL_COMMENT",
            "PRIVATE_VIDEO_TITLE",
            "PRIVATE_AUDIO_TITLE",
            "PRIVATE_CHAPTER_TITLE",
        )
    )

    pipeline = PublishReadyPipeline(
        PublishReadyConfig(
            profile_id=PublishProfileId.COMPATIBLE_MP4,
            output_directory=tmp_path / "tagged publish output",
        )
    )
    preparation = pipeline.prepare(source)
    result = pipeline.execute(
        preparation,
        confirmed_plan_digest=preparation.plan.plan_digest,
    )

    output_probe = _tag_probe(ffprobe, result.video_path)
    output_text = json.dumps(output_probe, sort_keys=True)
    assert all(
        token not in output_text
        for token in (
            "PRIVATE_GLOBAL_TITLE",
            "PRIVATE_GLOBAL_COMMENT",
            "PRIVATE_VIDEO_TITLE",
            "PRIVATE_AUDIO_TITLE",
            "PRIVATE_CHAPTER_TITLE",
        )
    )
    assert output_probe.get("chapters") == []
