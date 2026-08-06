"""Tests for staged, source-read-only Safe Sharing execution."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from array import array
from hashlib import sha256
from pathlib import Path

import pytest

from videoscope.privacy.artifacts import PrivacyArtifactLayout
from videoscope.privacy.errors import (
    PrivacyArtifactError,
    PrivacyCancelledError,
    PrivacyMediaError,
)
from videoscope.privacy.executor import (
    CommandResult,
    NativePrivacyExecutor,
    run_external_command,
)
from videoscope.privacy.manual import (
    ManualAudioIntervalInput,
    ManualVisualRegionInput,
    build_manual_audio_risk,
    build_manual_visual_risk,
)
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyEffectiveConfig,
    PrivacyPlan,
    PrivacyRiskMap,
    RedactionStyle,
)
from videoscope.privacy.planner import build_privacy_plan
from videoscope.privacy.profiles import get_share_audience_profile
from videoscope.privacy.renderer import VisualRenderResult
from videoscope.video import probe_video


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8192), b""):
            digest.update(block)
    return digest.hexdigest()


def _plan(
    input_hash: str,
    *,
    duration_seconds: float = 4.0,
    preview_seconds: float = 5.0,
) -> PrivacyPlan:
    visual = build_manual_visual_risk(
        input_hash,
        ManualVisualRegionInput(
            start_seconds=0.5,
            end_seconds=1.5,
            box=NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.4, y_max=0.4),
            style=RedactionStyle.BLUR,
            source_duration_seconds=duration_seconds,
        ),
    )
    audio = build_manual_audio_risk(
        input_hash,
        ManualAudioIntervalInput(
            start_seconds=1.0,
            end_seconds=2.0,
            source_duration_seconds=duration_seconds,
        ),
    )
    return build_privacy_plan(
        PrivacyRiskMap(
            input_hash=input_hash,
            profile="public",
            duration_seconds=duration_seconds,
            risks=(visual, audio),
        ),
        (),
        get_share_audience_profile("public"),
        PrivacyEffectiveConfig(preview_seconds=preview_seconds),
    )


def _audio_plan(input_hash: str, *, duration_seconds: float = 4.0) -> PrivacyPlan:
    audio = build_manual_audio_risk(
        input_hash,
        ManualAudioIntervalInput(
            start_seconds=1.0,
            end_seconds=2.0,
            source_duration_seconds=duration_seconds,
        ),
    )
    return build_privacy_plan(
        PrivacyRiskMap(
            input_hash=input_hash,
            profile="public",
            duration_seconds=duration_seconds,
            risks=(audio,),
        ),
        (),
        get_share_audience_profile("public"),
        PrivacyEffectiveConfig(),
    )


class WritingRenderer:
    def __init__(self, *, cancel: bool = False) -> None:
        self.cancel = cancel
        self.calls: list[tuple[Path, Path]] = []

    def render(
        self,
        source: Path,
        output: Path,
        plan: PrivacyPlan,
        cancellation: object,
    ) -> VisualRenderResult:
        del plan, cancellation
        self.calls.append((source, output))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"partial visual" if self.cancel else b"visual stream")
        if self.cancel:
            raise PrivacyCancelledError("cancelled after first visual frame")
        return VisualRenderResult(
            frames_read=4,
            frames_written=4,
            maximum_buffered_frames=1,
            width=32,
            height=18,
            frame_rate=1.0,
        )


class WritingRunner:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
    ) -> CommandResult:
        del timeout_seconds, sensitive_paths
        self.calls.append(arguments)
        if len(self.calls) == self.fail_at:
            return CommandResult(returncode=9, stderr_summary="sanitized failure")
        output = Path(arguments[-1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"processed media")
        return CommandResult(returncode=0, stderr_summary="")


def _accept_candidate(path: Path) -> object:
    assert path.is_file()
    return object()


def _write_complete_reports(result: object, plan: PrivacyPlan) -> Path:
    pending_root = getattr(result, "pending_root")
    assert isinstance(pending_root, Path)
    change_log = getattr(result, "change_log")
    candidate_sha256 = change_log.artifacts[0].sha256
    for name in (
        "privacy-summary.json",
        "technical-report.json",
        "verification.json",
    ):
        (pending_root / name).write_text(
            json.dumps(
                {
                    "schema_version": "0.1",
                    "plan_digest": plan.digest,
                }
            ),
            encoding="utf-8",
        )
    (pending_root / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "plan_digest": plan.digest,
                "artifacts": [
                    {
                        "relative_path": "share-safe.mp4",
                        "sha256": candidate_sha256,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return pending_root


def test_direct_executor_stages_only_below_private_root(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "job"

    result = NativePrivacyExecutor(
        renderer=WritingRenderer(),
        runner=WritingRunner(),
        candidate_probe=_accept_candidate,
    ).execute(_plan(_sha256_file(source)), source, workspace, lambda: False)

    assert result.pending_root is not None
    assert result.pending_root.parent == (
        workspace.resolve() / "privacy-review-private"
    )
    assert result.staged_video.parent == result.pending_root
    assert list((workspace / "share-package").iterdir()) == []


def test_executor_never_writes_source_and_publishes_expected_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "原始 source.mp4"
    source.write_bytes(b"source media bytes")
    before = _sha256_file(source)
    renderer = WritingRenderer()
    runner = WritingRunner()

    result = NativePrivacyExecutor(
        renderer=renderer,
        runner=runner,
        candidate_probe=_accept_candidate,
    ).execute(_plan(before), source, tmp_path / "任务 workspace", lambda: False)

    assert _sha256_file(source) == before
    assert result.staged_video.name == "share-safe.mp4"
    assert result.staged_video.read_bytes() == b"processed media"
    assert result.change_log.source_modified is False
    assert result.change_log.plan_digest == _plan(before).digest
    assert result.change_log.artifacts[0].relative_path == "share-safe.mp4"
    assert result.change_log.artifacts[0].sha256 == _sha256_file(result.staged_video)
    changes_path = result.staged_video.parent / "changes.json"
    public_changes = json.loads(changes_path.read_text(encoding="utf-8"))
    assert public_changes["source_modified"] is False
    assert str(source.resolve()) not in changes_path.read_text(encoding="utf-8")
    assert [Path(call[-1]).name for call in runner.calls] == [
        "audio-muted.mp4",
        "share-safe.partial.mp4",
    ]
    assert all(isinstance(call, tuple) for call in runner.calls)


def test_executor_cancellation_removes_incomplete_public_video(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    renderer = WritingRenderer(cancel=True)
    workspace = tmp_path / "job"

    with pytest.raises(PrivacyCancelledError):
        NativePrivacyExecutor(
            renderer=renderer,
            runner=WritingRunner(),
            candidate_probe=_accept_candidate,
        ).execute(_plan(_sha256_file(source)), source, workspace, lambda: False)

    assert source.read_bytes() == b"source"
    assert not (workspace / "share-package" / "share-safe.mp4").exists()
    assert not (workspace / "share-package" / "changes.json").exists()
    assert not any((workspace / "privacy-review-private").glob("staging-*"))


def test_command_failure_cleans_only_this_attempt_and_keeps_existing_artifacts(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "job"
    existing = workspace / "share-package" / "share-safe.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"existing trusted artifact")

    with pytest.raises(PrivacyArtifactError):
        NativePrivacyExecutor(
            renderer=WritingRenderer(),
            runner=WritingRunner(fail_at=1),
            candidate_probe=_accept_candidate,
        ).execute(_plan(_sha256_file(source)), source, workspace, lambda: False)

    assert existing.read_bytes() == b"existing trusted artifact"
    assert source.read_bytes() == b"source"


def test_failed_remux_does_not_publish_partial_video_or_change_log(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "job"

    with pytest.raises(PrivacyMediaError):
        NativePrivacyExecutor(
            renderer=WritingRenderer(),
            runner=WritingRunner(fail_at=2),
            candidate_probe=_accept_candidate,
        ).execute(_plan(_sha256_file(source)), source, workspace, lambda: False)

    assert not (workspace / "share-package" / "share-safe.mp4").exists()
    assert not (workspace / "share-package" / "changes.json").exists()
    assert source.read_bytes() == b"source"


def test_executor_rejects_plan_for_different_source_before_rendering(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    renderer = WritingRenderer()

    with pytest.raises(PrivacyArtifactError):
        NativePrivacyExecutor(
            renderer=renderer,
            runner=WritingRunner(),
            candidate_probe=_accept_candidate,
        ).execute(_plan("a" * 64), source, tmp_path / "job", lambda: False)

    assert renderer.calls == []
    assert source.read_bytes() == b"source"


def test_public_tree_validation_failure_rolls_back_published_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "job"

    def reject_public_tree(layout: PrivacyArtifactLayout) -> tuple[str, ...]:
        del layout
        raise PrivacyArtifactError("public validation failed")

    monkeypatch.setattr(
        PrivacyArtifactLayout,
        "validate_public_tree",
        reject_public_tree,
    )

    with pytest.raises(PrivacyArtifactError):
        NativePrivacyExecutor(
            renderer=WritingRenderer(),
            runner=WritingRunner(),
            candidate_probe=_accept_candidate,
        ).execute(_plan(_sha256_file(source)), source, workspace, lambda: False)

    assert not (workspace / "share-package" / "share-safe.mp4").exists()
    assert not (workspace / "share-package" / "changes.json").exists()


def test_nonempty_corrupt_candidate_probe_failure_never_publishes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "job"

    def reject_candidate(path: Path) -> object:
        assert path.read_bytes() == b"processed media"
        raise PrivacyMediaError("candidate has no processable video stream")

    with pytest.raises(PrivacyMediaError):
        NativePrivacyExecutor(
            renderer=WritingRenderer(),
            runner=WritingRunner(),
            candidate_probe=reject_candidate,
        ).execute(_plan(_sha256_file(source)), source, workspace, lambda: False)

    assert not (workspace / "share-package" / "share-safe.mp4").exists()
    assert not (workspace / "share-package" / "changes.json").exists()
    assert not any((workspace / "privacy-review-private").glob("staging-*"))
    assert not any(workspace.glob("share-package.pending-*"))


def test_complete_pending_package_is_observable_before_single_directory_swap(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "job"
    observations: list[tuple[set[str], set[str]]] = []

    def observe_pending(pending: Path, public: Path) -> None:
        observations.append(
            (
                {path.name for path in pending.iterdir()},
                {path.name for path in public.iterdir()},
            )
        )

    plan = _plan(_sha256_file(source))
    executor = NativePrivacyExecutor(
        renderer=WritingRenderer(),
        runner=WritingRunner(),
        candidate_probe=_accept_candidate,
        before_publish=observe_pending,
    )
    result = executor.execute(plan, source, workspace, lambda: False)
    pending_root = _write_complete_reports(result, plan)
    published_video = executor.publish_pending(
        pending_root,
        plan,
        source,
        workspace,
        lambda: False,
    )

    assert observations == [
        (
            {
                "share-safe.mp4",
                "changes.json",
                "manifest.json",
                "privacy-summary.json",
                "technical-report.json",
                "verification.json",
            },
            set(),
        )
    ]
    assert published_video.parent == workspace.resolve() / "share-package"
    assert {path.name for path in published_video.parent.iterdir()} == {
        "share-safe.mp4",
        "changes.json",
        "manifest.json",
        "privacy-summary.json",
        "technical-report.json",
        "verification.json",
    }
    assert not any((workspace / "privacy-review-private").glob("pending-*"))


def test_executor_can_hold_complete_pending_package_until_explicit_publish(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "job"
    plan = _plan(_sha256_file(source))
    executor = NativePrivacyExecutor(
        renderer=WritingRenderer(),
        runner=WritingRunner(),
        candidate_probe=_accept_candidate,
    )

    pending = executor.execute(
        plan,
        source,
        workspace,
        lambda: False,
    )

    assert pending.pending_root is not None
    assert pending.staged_video.parent == pending.pending_root
    assert pending.pending_root.parent == (
        workspace.resolve() / "privacy-review-private"
    )
    assert {path.name for path in pending.pending_root.iterdir()} == {
        "share-safe.mp4",
        "changes.json",
    }
    assert list((workspace / "share-package").iterdir()) == []

    with pytest.raises(PrivacyArtifactError):
        executor.publish_pending(
            pending.pending_root,
            plan,
            source,
            workspace,
            lambda: False,
        )
    assert list((workspace / "share-package").iterdir()) == []
    pending_root = _write_complete_reports(pending, plan)
    unexpected = pending_root / "unexpected-empty-directory"
    unexpected.mkdir()
    with pytest.raises(PrivacyArtifactError):
        executor.publish_pending(
            pending_root,
            plan,
            source,
            workspace,
            lambda: False,
        )
    unexpected.rmdir()

    published_video = executor.publish_pending(
        pending_root,
        plan,
        source,
        workspace,
        lambda: False,
    )

    assert published_video == workspace.resolve() / "share-package" / "share-safe.mp4"
    assert {path.name for path in published_video.parent.iterdir()} == {
        "share-safe.mp4",
        "changes.json",
        "manifest.json",
        "privacy-summary.json",
        "technical-report.json",
        "verification.json",
    }
    assert not pending.pending_root.exists()

    with pytest.raises(PrivacyArtifactError):
        executor.publish_pending(
            pending.pending_root,
            plan,
            source,
            workspace,
            lambda: False,
        )


def test_source_change_immediately_before_publish_blocks_package(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "job"
    original_hash = _sha256_file(source)

    def mutate_source(pending: Path, public: Path) -> None:
        del pending, public
        source.write_bytes(b"changed while processing")

    plan = _plan(original_hash)
    executor = NativePrivacyExecutor(
        renderer=WritingRenderer(),
        runner=WritingRunner(),
        candidate_probe=_accept_candidate,
        before_publish=mutate_source,
    )
    result = executor.execute(plan, source, workspace, lambda: False)
    pending_root = _write_complete_reports(result, plan)

    with pytest.raises(PrivacyArtifactError):
        executor.publish_pending(
            pending_root,
            plan,
            source,
            workspace,
            lambda: False,
        )

    assert _sha256_file(source) != original_hash
    assert not (workspace / "share-package" / "share-safe.mp4").exists()
    assert not (workspace / "share-package" / "changes.json").exists()
    assert pending_root.is_dir()


def test_external_runner_uses_argument_array_and_sanitizes_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "私有 source.mp4"
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(
            arguments,
            7,
            stdout="ffmpeg version test-build\n",
            stderr=f"failed while opening {source}",
        )

    monkeypatch.setattr("videoscope.privacy.executor.subprocess.run", fake_run)

    result = run_external_command(
        ("ffmpeg", "-i", str(source)),
        timeout_seconds=12.0,
        sensitive_paths=(source,),
    )

    assert result.returncode == 7
    assert result.stdout_summary == "ffmpeg version test-build"
    assert str(source) not in result.stderr_summary
    assert "<input>" in result.stderr_summary
    assert calls == [
        (
            ["ffmpeg", "-i", str(source)],
            {
                "shell": False,
                "check": False,
                "capture_output": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": 12.0,
            },
        )
    ]


def _local_video_tools() -> tuple[str, str]:
    ffmpeg = os.environ.get("VIDEOSCOPE_TEST_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("VIDEOSCOPE_TEST_FFPROBE") or shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required for privacy executor integration")
    assert ffmpeg is not None
    assert ffprobe is not None
    return ffmpeg, ffprobe


def _run_checked(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        arguments,
        shell=False,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        pytest.fail(completed.stderr.decode("utf-8", errors="replace")[:1000])
    return completed


def _tagged_av_fixture(tmp_path: Path, ffmpeg: str) -> Path:
    metadata = tmp_path / "private.ffmetadata"
    metadata.write_text(
        ";FFMETADATA1\n"
        "title=private title\n"
        "location=31.2304,121.4737\n"
        "[CHAPTER]\n"
        "TIMEBASE=1/1000\n"
        "START=0\n"
        "END=1000\n"
        "title=private chapter\n",
        encoding="utf-8",
    )
    source = tmp_path / "原始 有声音.mp4"
    _run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-i",
            str(metadata),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map_metadata",
            "2",
            "-map_chapters",
            "2",
            "-metadata:s:v:0",
            "author=private-author",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ]
    )
    return source


def _probe_tags(ffprobe: str, path: Path) -> dict[str, object]:
    completed = _run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format_tags:stream_tags:chapter_tags",
            "-show_chapters",
            "-of",
            "json",
            str(path),
        ]
    )
    decoded = json.loads(completed.stdout.decode("utf-8"))
    assert isinstance(decoded, dict)
    return decoded


def _rms_energy(ffmpeg: str, path: Path, start: float, duration: float) -> float:
    completed = _run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-t",
            str(duration),
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-f",
            "f32le",
            "-ac",
            "1",
            "-ar",
            "8000",
            "pipe:1",
        ]
    )
    samples = array("f")
    samples.frombytes(completed.stdout)
    assert samples
    return math.sqrt(sum(float(sample) ** 2 for sample in samples) / len(samples))


def test_real_executor_mutes_only_reviewed_interval_and_strips_tags(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = _local_video_tools()
    source = _tagged_av_fixture(tmp_path, ffmpeg)
    source_hash = _sha256_file(source)

    result = NativePrivacyExecutor(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        ffmpeg_version="integration test local FFmpeg",
    ).execute(
        _plan(source_hash),
        source,
        tmp_path / "隔离 job",
        lambda: False,
    )

    tags = _probe_tags(ffprobe, result.staged_video)
    format_tags = tags.get("format", {})
    assert isinstance(format_tags, dict)
    assert "location" not in format_tags.get("tags", {})
    streams = tags.get("streams", [])
    assert isinstance(streams, list)
    assert all("author" not in stream.get("tags", {}) for stream in streams)
    assert tags.get("chapters", []) == []
    assert _rms_energy(ffmpeg, result.staged_video, 1.2, 0.5) < 0.01
    assert _rms_energy(ffmpeg, result.staged_video, 2.5, 0.5) > 0.05
    assert _sha256_file(source) == source_hash


def test_real_private_preview_is_bounded_and_never_published(tmp_path: Path) -> None:
    ffmpeg, ffprobe = _local_video_tools()
    source = _tagged_av_fixture(tmp_path, ffmpeg)
    source_hash = _sha256_file(source)
    output = tmp_path / "private preview" / "preview.mp4"
    executor = NativePrivacyExecutor(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        ffmpeg_version="integration test local FFmpeg",
    )

    preview = executor.preview(
        _plan(source_hash, preview_seconds=1.25),
        source,
        output,
        lambda: False,
    )

    metadata = probe_video(preview, ffprobe=ffprobe)
    assert preview == output
    assert 1.0 <= metadata.duration_seconds <= 1.4
    assert _sha256_file(source) == source_hash
    assert not any(tmp_path.rglob("share-package"))
