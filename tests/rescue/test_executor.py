"""Tests for staged, source-traceable faithful Rescue execution."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

import pytest
from pydantic import JsonValue

import videoscope.rescue as rescue
from videoscope.domain import VideoMetadata
from videoscope.rescue.commands import build_decode_verification_command
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueInputError,
    RescueMediaError,
)
from videoscope.rescue.executor import (
    CommandResult,
    NativeRescueExecutor,
    SourceMapping,
    run_external_command,
)
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    make_damage_id,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.stabilization import MotionTransform, StabilizationAssessment
from videoscope.rescue.visual import (
    FlickerCorrectionPlan,
    VisualAssessment,
    VisualMetrics,
)


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _measured_dark_assessment() -> VisualAssessment:
    return VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.05,
            luma_p50=0.08,
            luma_p90=0.12,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.0,
            sharpness=0.1,
        ),
        recommended_actions=(RescueActionKind.ADJUST_LUMA,),
        preview_required=True,
        public_explanation="Measured dark samples support a preview.",
    )


def _plan(
    source_bytes: bytes,
    *,
    duration_seconds: float = 6.0,
    damage_ranges: tuple[tuple[float, float], ...] = (),
    timestamp_discontinuity: bool = False,
    input_hash_override: str | None = None,
    file_size_bytes: int | None = None,
    locked_ranges: tuple[tuple[float, float], ...] = (),
) -> RescuePlan:
    input_hash = input_hash_override or _sha256_bytes(source_bytes)
    intervals = [
        DamageInterval(
            id=make_damage_id(
                input_hash,
                "video:0",
                DamageKind.UNDECODABLE,
                start,
                end,
            ),
            stream_id="video:0",
            kind=DamageKind.UNDECODABLE,
            start_seconds=start,
            end_seconds=end,
        )
        for start, end in damage_ranges
    ]
    if timestamp_discontinuity:
        intervals.append(
            DamageInterval(
                id=make_damage_id(
                    input_hash,
                    "video:0",
                    DamageKind.TIMESTAMP_DISCONTINUITY,
                    2.0,
                    2.1,
                ),
                stream_id="video:0",
                kind=DamageKind.TIMESTAMP_DISCONTINUITY,
                start_seconds=2.0,
                end_seconds=2.1,
            )
        )
    return build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=duration_seconds,
            average_frame_rate=10.0,
            estimated_frame_count=int(duration_seconds * 10),
            has_audio=False,
            file_size_bytes=(
                len(source_bytes) if file_size_bytes is None else file_size_bytes
            ),
        ),
        damage_map=MediaDamageMap(
            input_hash=input_hash,
            duration_seconds=duration_seconds,
            scan_coverage=((0.0, duration_seconds),),
            intervals=tuple(intervals),
        ),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(locked_ranges=locked_ranges),
        locked_ranges=locked_ranges,
    )


class WritingRunner:
    """Write controlled command outputs while preserving command ordering."""

    def __init__(
        self,
        *,
        fail_segment: int | None = None,
        reject_probe_for_segment: int | None = None,
        fail_decode_for_segment: int | None = None,
        cancel_during_first_write: bool = False,
        mutate_source: Path | None = None,
        source_codec: str = "h264",
        keyframe_advance_seconds: float = 0.0,
        probe_durations_by_name: dict[str, float] | None = None,
        decode_error_names: set[str] | None = None,
        decode_error_summary: str = "fatal decode error",
        cancel_after_final_decode: bool = False,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_segment = fail_segment
        self.reject_probe_for_segment = reject_probe_for_segment
        self.fail_decode_for_segment = fail_decode_for_segment
        self.cancel_during_first_write = cancel_during_first_write
        self.mutate_source = mutate_source
        self.source_codec = source_codec
        self.keyframe_advance_seconds = keyframe_advance_seconds
        self.probe_durations_by_name = probe_durations_by_name or {}
        self.decode_error_names = decode_error_names or set()
        self.decode_error_summary = decode_error_summary
        self.cancel_after_final_decode = cancel_after_final_decode
        self.cancelled = False
        self.processed_segments = 0
        self.probed_segments = 0
        self.segment_durations: dict[int, float] = {}
        self.media_durations: dict[str, float] = {}

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del timeout_seconds, sensitive_paths
        self.calls.append(arguments)
        if "-skip_frame" in arguments:
            interval = arguments[arguments.index("-read_intervals") + 1]
            requested_start = interval.split("%", 1)[0]
            keyframe_start = float(requested_start) + self.keyframe_advance_seconds
            return CommandResult(
                returncode=0,
                stdout_summary=(
                    '{"frames":[{"best_effort_timestamp_time":"'
                    + str(keyframe_start)
                    + '"}]}'
                ),
                stderr_summary="",
            )
        if arguments[0].endswith("ffprobe") or arguments[0] == "ffprobe":
            self.probed_segments += 1
            if self.probed_segments == self.reject_probe_for_segment:
                return CommandResult(
                    returncode=1,
                    stdout_summary="",
                    stderr_summary="sanitized probe failure",
                )
            candidate_name = Path(arguments[-1]).name
            duration = self.probe_durations_by_name.get(
                candidate_name,
                self.media_durations.get(candidate_name, 6.0),
            )
            return CommandResult(
                returncode=0,
                stdout_summary=(
                    f'{{"format":{{"duration":"{duration}"}},"streams":'
                    f'[{{"codec_type":"video","codec_name":"{self.source_codec}"}}]}}'
                ),
                stderr_summary="",
            )
        if ("-f", "null") in tuple(zip(arguments, arguments[1:])):
            segment_name = Path(arguments[arguments.index("-i") + 1]).name
            if segment_name in self.decode_error_names:
                return CommandResult(9, self.decode_error_summary, "")
            if segment_name.startswith("segment-"):
                segment_index = int(segment_name.split("-")[1].split(".")[0]) + 1
                if segment_index == self.fail_decode_for_segment:
                    return CommandResult(9, "sanitized decode failure", "")
            if (
                self.cancel_after_final_decode
                and segment_name == "faithful-rescue.partial.mp4"
            ):
                self.cancelled = True
            return CommandResult(0, "", "")
        output = Path(arguments[-1])
        if "segment-" in output.name:
            self.processed_segments += 1
            if self.processed_segments == self.fail_segment:
                return CommandResult(7, "sanitized segment failure", "")
            duration = float(arguments[arguments.index("-t") + 1])
            self.segment_durations[self.processed_segments - 1] = duration
        elif ("-f", "concat") in tuple(zip(arguments, arguments[1:])):
            manifest = Path(arguments[arguments.index("-i") + 1])
            retained_indexes = [
                int(Path(line[6:-1]).name.split("-")[1].split(".")[0])
                for line in manifest.read_text(encoding="utf-8").splitlines()
                if line.startswith("file '")
            ]
            duration = sum(self.segment_durations[index] for index in retained_indexes)
        else:
            duration = 6.0
        self.media_durations[output.name] = duration
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"verified media")
        if self.mutate_source is not None:
            self.mutate_source.write_bytes(b"source changed during execution")
            self.mutate_source = None
        if self.cancel_during_first_write:
            self.cancel_during_first_write = False
            raise RescueCancelledError("cancelled by fake runner")
        assert cancellation_callback() is False
        return CommandResult(0, "", "")


def test_middle_damage_yields_two_traceable_segments(tmp_path: Path) -> None:
    """Catches merging across a damaged middle or losing source/output mapping."""
    source_bytes = b"source remains read only"
    source = tmp_path / "源 视频.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner()

    result = NativeRescueExecutor(runner=runner).execute_faithful(
        plan=_plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source=source,
        work_root=tmp_path / "工作区",
        cancellation_callback=lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (0.0, 2.0),
        (3.0, 6.0),
    ]
    assert [
        (item.output_start, item.output_end) for item in result.source_mappings
    ] == [(0.0, 2.0), (2.0, 5.0)]
    assert all(
        segment.output_relative_path.startswith("staging/")
        for segment in result.segments
    )
    assert result.output_relative_path == "staging/faithful-rescue.mp4"
    assert result.output_path.is_file()
    assert result.failed_source_ranges == ()
    assert source.read_bytes() == source_bytes
    segment_commands = [
        call
        for call in runner.calls
        if call[0] == "ffmpeg" and "segment-" in Path(call[-1]).name
    ]
    assert len(segment_commands) == 2
    assert all("libx264" in call and "-c" not in call for call in segment_commands)


def test_preview_mapping_removes_middle_damage_and_rebases_output() -> None:
    """Catches preview lineage preserving a deleted middle source interval."""
    mappings = rescue.preview_source_mappings(
        _plan(b"source", damage_ranges=((2.0, 3.0),)),
        (1.0, 4.0),
        "faithful-00.mp4",
    )

    assert [
        (item.source_start, item.source_end, item.output_start, item.output_end)
        for item in mappings
    ] == [
        (1.0, 2.0, 0.0, 1.0),
        (3.0, 4.0, 1.0, 2.0),
    ]


def test_locked_undecodable_range_is_retained_while_authorized_peer_is_removed(
    tmp_path: Path,
) -> None:
    source_bytes = b"locked source remains"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)

    result = NativeRescueExecutor(runner=WritingRunner()).execute_faithful(
        plan=_plan(
            source_bytes,
            damage_ranges=((2.0, 3.0), (4.0, 5.0)),
            locked_ranges=((2.0, 3.0),),
        ),
        source=source,
        work_root=tmp_path / "work",
        cancellation_callback=lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (0.0, 4.0),
        (5.0, 6.0),
    ]


def test_clean_remux_uses_stream_copy_only(tmp_path: Path) -> None:
    """Catches unnecessary transcoding on the one safe remux-only path."""
    source_bytes = b"clean source"
    source = tmp_path / "clean.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner()

    result = NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes), source, tmp_path / "work", lambda: False
    )

    ffmpeg_calls = [
        call for call in runner.calls if call[0] == "ffmpeg" and call[-1] != "-"
    ]
    assert len(ffmpeg_calls) == 1
    assert ("-c", "copy") in tuple(zip(ffmpeg_calls[0], ffmpeg_calls[0][1:]))
    assert "libx264" not in ffmpeg_calls[0]
    assert result.segments[0].source_start == 0.0
    assert result.segments[0].source_end == 6.0


def test_remux_only_reencodes_when_source_codec_is_not_mp4_copy_safe(
    tmp_path: Path,
) -> None:
    """Catches stream-copying a codec outside the conservative MP4 set."""
    source_bytes = b"clean but incompatible source"
    source = tmp_path / "clean.webm"
    source.write_bytes(source_bytes)
    runner = WritingRunner(source_codec="vp9")

    NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes), source, tmp_path / "work", lambda: False
    )

    ffmpeg_call = next(call for call in runner.calls if call[0] == "ffmpeg")
    assert "libx264" in ffmpeg_call
    assert ("-c", "copy") not in tuple(zip(ffmpeg_call, ffmpeg_call[1:]))


def test_timestamp_rebuild_reencodes_instead_of_stream_copy(tmp_path: Path) -> None:
    """Catches copying timestamps when the plan explicitly requires rebuilding."""
    source_bytes = b"timestamp source"
    source = tmp_path / "timeline.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner()

    NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes, timestamp_discontinuity=True),
        source,
        tmp_path / "work",
        lambda: False,
    )

    ffmpeg_call = next(call for call in runner.calls if call[0] == "ffmpeg")
    assert "libx264" in ffmpeg_call
    assert ("-c", "copy") not in tuple(zip(ffmpeg_call, ffmpeg_call[1:]))


def test_failed_segment_is_not_retained_but_verified_independent_segment_is(
    tmp_path: Path,
) -> None:
    """Catches retaining an unverified segment or discarding a verified peer."""
    source_bytes = b"partially salvageable source"
    source = tmp_path / "partial.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    result = NativeRescueExecutor(
        runner=WritingRunner(fail_segment=2)
    ).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        work,
        lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (0.0, 2.0)
    ]
    assert result.failed_source_ranges == ((3.0, 6.0),)
    assert (work / "staging/segments/segment-000.mp4").is_file()
    assert not (work / "staging/segments/segment-001.mp4").exists()
    assert not (work / "staging/segments/segment-001.partial.mp4").exists()
    assert result.output_path.is_file()


def test_failed_segment_verification_never_retains_that_segment(tmp_path: Path) -> None:
    """Catches retaining a nonempty but unverified media fragment."""
    source_bytes = b"verification matters"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    result = NativeRescueExecutor(
        runner=WritingRunner(reject_probe_for_segment=1)
    ).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        work,
        lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (3.0, 6.0)
    ]
    assert not (work / "staging/segments/segment-000.mp4").exists()
    assert result.failed_source_ranges == ((0.0, 2.0),)


def test_segment_that_fails_full_decode_is_not_retained(tmp_path: Path) -> None:
    """Catches trusting a structurally valid segment that cannot fully decode."""
    source_bytes = b"decode verification matters"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    result = NativeRescueExecutor(
        runner=WritingRunner(fail_decode_for_segment=1)
    ).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        work,
        lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (3.0, 6.0)
    ]
    assert result.failed_source_ranges == ((0.0, 2.0),)
    assert not (work / "staging/segments/segment-000.mp4").exists()


def test_strict_decode_error_is_fatal_and_preserves_sanitized_diagnostic(
    tmp_path: Path,
) -> None:
    """Catches FFmpeg decode errors being logged but accepted as verified."""
    source_bytes = b"decode error source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner(
        decode_error_names={"faithful-rescue.partial.mp4"},
        decode_error_summary="invalid packet while decoding",
    )

    with pytest.raises(RescueMediaError) as error:
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes), source, tmp_path / "work", lambda: False
        )

    decode_call = next(
        call for call in runner.calls if ("-f", "null") in tuple(zip(call, call[1:]))
    )
    assert "-xerror" in decode_call
    assert ("-err_detect", "explode") in tuple(zip(decode_call, decode_call[1:]))
    assert ("-max_error_rate", "0") in tuple(zip(decode_call, decode_call[1:]))
    assert error.value.internal_message == "invalid packet while decoding"


def test_source_mappings_use_measured_final_timing_and_final_path(
    tmp_path: Path,
) -> None:
    """Catches requested durations or segment paths leaking into final mappings."""
    source_bytes = b"measured timing source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner(
        probe_durations_by_name={
            "segment-000.partial.mp4": 1.9,
            "segment-001.partial.mp4": 3.1,
            "faithful-rescue.partial.mp4": 5.0,
        }
    )

    result = NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        tmp_path / "work",
        lambda: False,
    )

    assert [
        (mapping.output_start, mapping.output_end) for mapping in result.source_mappings
    ] == pytest.approx([(0.0, 1.9), (1.9, 5.0)])
    assert {mapping.output_relative_path for mapping in result.source_mappings} == {
        "staging/faithful-rescue.mp4"
    }
    assert [
        (segment.output_start, segment.output_end) for segment in result.segments
    ] == [
        (0.0, 1.9),
        (0.0, 3.1),
    ]


def test_final_duration_outside_tolerance_is_rejected(tmp_path: Path) -> None:
    """Catches publishing a concat whose measured duration contradicts its segments."""
    source_bytes = b"truncated concat source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner(probe_durations_by_name={"faithful-rescue.partial.mp4": 4.0})

    with pytest.raises(RescueMediaError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
            source,
            tmp_path / "work",
            lambda: False,
        )


def test_keyframe_advance_records_the_omitted_prefix_as_failed(tmp_path: Path) -> None:
    """Catches silently dropping valid source time before a later keyframe."""
    source_bytes = b"ordinary gop source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)

    result = NativeRescueExecutor(
        runner=WritingRunner(keyframe_advance_seconds=0.4)
    ).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        tmp_path / "work",
        lambda: False,
    )

    assert result.is_partial is True
    assert result.failed_source_ranges == ((0.0, 0.4), (3.0, 3.4))
    assert [
        (segment.source_start, segment.source_end) for segment in result.segments
    ] == [
        (0.4, 2.0),
        (3.4, 6.0),
    ]


def test_late_cancellation_before_atomic_rename_never_publishes(tmp_path: Path) -> None:
    """Catches cancellation becoming true after final decode but before rename."""
    source_bytes = b"late cancellation source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"
    runner = WritingRunner(cancel_after_final_decode=True)

    with pytest.raises(RescueCancelledError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes),
            source,
            work,
            lambda: runner.cancelled,
        )

    assert not (work / "staging/faithful-rescue.mp4").exists()
    assert not (work / "staging/faithful-rescue.partial.mp4").exists()


def test_source_hash_mismatch_is_rejected_before_any_command(tmp_path: Path) -> None:
    """Catches executing a plan confirmed for different source bytes."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"different source")
    runner = WritingRunner()

    with pytest.raises(RescueInputError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(b"confirmed source"), source, tmp_path / "work", lambda: False
        )

    assert runner.calls == []
    assert source.read_bytes() == b"different source"


def test_reserved_output_collision_is_rejected_without_touching_source(
    tmp_path: Path,
) -> None:
    """Catches a same-path output replacing the read-only source."""
    source_bytes = b"must survive"
    work = tmp_path / "work"
    source = work / "staging" / "faithful-rescue.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(source_bytes)
    runner = WritingRunner()

    with pytest.raises(RescueArtifactError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes), source, work, lambda: False
        )

    assert runner.calls == []
    assert source.read_bytes() == source_bytes


def test_staging_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """Catches writing through a staging symlink outside the validated work root."""
    source_bytes = b"source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (work / "staging").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    runner = WritingRunner()

    with pytest.raises(RescueArtifactError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes), source, work, lambda: False
        )

    assert runner.calls == []
    assert list(outside.iterdir()) == []


def test_source_change_during_execution_blocks_publication(tmp_path: Path) -> None:
    """Catches publishing a result after the source identity changed mid-run."""
    source_bytes = b"source before"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    with pytest.raises(RescueArtifactError):
        NativeRescueExecutor(
            runner=WritingRunner(mutate_source=source)
        ).execute_faithful(_plan(source_bytes), source, work, lambda: False)

    assert not (work / "staging/faithful-rescue.mp4").exists()
    assert not (work / "staging/faithful-rescue.partial.mp4").exists()


def test_cancellation_keeps_verified_segment_but_never_publishes_partial_final(
    tmp_path: Path,
) -> None:
    """Catches cancellation deleting verified work or publishing an incomplete file."""
    source_bytes = b"source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"
    runner = WritingRunner(cancel_during_first_write=True)

    with pytest.raises(RescueCancelledError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
            source,
            work,
            lambda: False,
        )

    assert not (work / "staging/faithful-rescue.mp4").exists()
    assert not (work / "staging/faithful-rescue.partial.mp4").exists()
    assert not (work / "staging/segments/segment-000.partial.mp4").exists()


def test_external_runner_terminates_child_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cooperative cancellation leaving an FFmpeg child running."""
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def tracking_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("videoscope.rescue.executor.subprocess.Popen", tracking_popen)
    started = monotonic()

    with pytest.raises(RescueCancelledError):
        run_external_command(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            timeout_seconds=10.0,
            sensitive_paths=(),
            cancellation_callback=lambda: monotonic() - started > 0.15,
        )

    assert len(processes) == 1
    for _ in range(50):
        if processes[0].poll() is not None:
            break
        sleep(0.02)
    assert processes[0].poll() is not None
    assert monotonic() - started < 3.0


def test_external_runner_uses_shell_false_and_sanitizes_bounded_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches shell execution or exposing an input path through stderr."""
    real_popen = subprocess.Popen
    calls: list[dict[str, Any]] = []
    sensitive = tmp_path / "秘密 source.mp4"

    def tracking_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        calls.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("videoscope.rescue.executor.subprocess.Popen", tracking_popen)
    result = run_external_command(
        (
            sys.executable,
            "-c",
            "import sys; sys.stderr.write(sys.argv[1] + 'x' * 20000)",
            str(sensitive),
        ),
        timeout_seconds=5.0,
        sensitive_paths=(sensitive,),
        cancellation_callback=lambda: False,
    )

    assert result.returncode == 0
    assert calls[0]["shell"] is False
    assert str(sensitive) not in result.stderr_summary
    assert len(result.stderr_summary) <= 2003


def _local_video_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("local FFmpeg and ffprobe are required for Rescue integration")
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


def _corrupted_unicode_fixture(tmp_path: Path, ffmpeg: str) -> Path:
    source = tmp_path / "损坏 媒体" / "源 视频.mp4"
    source.parent.mkdir(parents=True)
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
            "testsrc2=size=96x64:rate=10:duration=6",
            "-c:v",
            "libx264",
            "-g",
            "10",
            "-keyint_min",
            "10",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(source),
        ]
    )
    size = source.stat().st_size
    corruption_start = int(size * 0.42)
    corruption_bytes = max(256, int(size * 0.02))
    with source.open("r+b") as handle:
        handle.seek(corruption_start)
        handle.write(b"\0" * corruption_bytes)
    return source


def test_real_unicode_corrupted_media_is_playable_mapped_and_source_unchanged(
    tmp_path: Path,
) -> None:
    """Catches a fake-only executor that cannot salvage real local media."""
    ffmpeg, ffprobe = _local_video_tools()
    source = _corrupted_unicode_fixture(tmp_path, ffmpeg)
    source_hash = _sha256_file(source)
    result = NativeRescueExecutor(ffmpeg=ffmpeg, ffprobe=ffprobe).execute_faithful(
        _plan(
            b"",
            damage_ranges=((2.0, 3.0),),
            input_hash_override=source_hash,
            file_size_bytes=source.stat().st_size,
        ),
        source,
        tmp_path / "Unicode 工作区",
        lambda: False,
    )

    probe = _run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(result.output_path),
        ]
    )
    payload = json.loads(probe.stdout.decode("utf-8"))
    streams = payload["streams"]
    mapped_duration = sum(
        mapping.source_end - mapping.source_start for mapping in result.source_mappings
    )
    output_duration = float(payload["format"]["duration"])

    assert any(stream["codec_type"] == "video" for stream in streams)
    assert _sha256_file(source) == source_hash
    assert mapped_duration == pytest.approx(5.0)
    assert output_duration == pytest.approx(mapped_duration, abs=0.25)
    assert result.output_relative_path == "staging/faithful-rescue.mp4"


def test_real_corrupted_media_fails_strict_full_decode(tmp_path: Path) -> None:
    """Catches real FFmpeg logging damaged frames but returning success."""
    ffmpeg, _ffprobe = _local_video_tools()
    source = _corrupted_unicode_fixture(tmp_path, ffmpeg)

    result = run_external_command(
        tuple(build_decode_verification_command(source, ffmpeg=ffmpeg)),
        timeout_seconds=30.0,
        sensitive_paths=(source,),
        cancellation_callback=lambda: False,
    )

    assert result.returncode != 0


def test_all_failed_segments_raise_without_final_output(tmp_path: Path) -> None:
    """Catches producing an empty rescue after every independent segment failed."""
    source_bytes = b"unusable"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    with pytest.raises(RescueMediaError):
        NativeRescueExecutor(runner=WritingRunner(fail_segment=1)).execute_faithful(
            _plan(source_bytes, damage_ranges=((2.0, 6.0),)),
            source,
            work,
            lambda: False,
        )

    assert not (work / "staging/faithful-rescue.mp4").exists()
    assert not any((work / "staging/segments").glob("*.partial.mp4"))


def test_native_executor_renders_bound_balanced_improvement_from_faithful(
    tmp_path: Path,
) -> None:
    """Catches a pipeline fake being the only available improved executor."""
    source_bytes = b"original source"
    source_hash = _sha256_bytes(source_bytes)
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    interval = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.DARK, 0.5, 1.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=0.5,
        end_seconds=1.0,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=True,
            file_size_bytes=len(source_bytes),
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            scan_coverage=((0.0, 2.0),),
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=_measured_dark_assessment(),
    )
    commands: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(arguments)
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                '{"format":{"duration":"2.0"},"streams":'
                '[{"codec_type":"video"},{"codec_type":"audio"}]}',
            )
        if "null" in arguments:
            return CommandResult(0, "", "")
        Path(arguments[-1]).write_bytes(b"improved pixels")
        return CommandResult(0, "", "")

    output = NativeRescueExecutor(runner=runner).execute_improved(
        plan, faithful, tmp_path / "work", lambda: False
    )

    assert output.name == "improved-viewing.mp4"
    assert output.read_bytes() == b"improved pixels"
    render = next(command for command in commands if "-filter:v:0" in command)
    assert any("eq=brightness=0.04:contrast=1.02" in value for value in render)
    assert any("enable='gte(t,0.5)*lt(t,1)'" in value for value in render)
    assert faithful.read_bytes() == b"verified faithful"


def test_native_executor_applies_confirmed_deflicker_curve_frame_by_frame(
    tmp_path: Path,
) -> None:
    """Catches replacing an alternating sampled curve with midpoint constants."""
    source_bytes = b"original source"
    source_hash = _sha256_bytes(source_bytes)
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    interval = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.FLICKER, 0.5, 1.5),
        stream_id="video:0",
        kind=DamageKind.FLICKER,
        start_seconds=0.5,
        end_seconds=1.5,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=True,
            file_size_bytes=len(source_bytes),
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            scan_coverage=((0.0, 2.0),),
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        flicker_correction=FlickerCorrectionPlan(
            intervals=((0.5, 1.5),),
            gains=((0.5, 1.08), (1.0, 1.0 / 1.08), (1.5, 1.08)),
        ),
    )
    captured: dict[str, object] = {}

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                '{"format":{"duration":"2.0"},"streams":'
                '[{"codec_type":"video"},{"codec_type":"audio"}]}',
            )
        if "null" in arguments:
            return CommandResult(0, "", "")
        raise AssertionError(
            "deflicker-only execution must not use a midpoint FFmpeg render"
        )

    class Executor(NativeRescueExecutor):
        def execute_deflickered(self, **kwargs: object) -> None:
            captured.update(kwargs)
            Path(kwargs["output"]).write_bytes(b"deflickered pixels")  # type: ignore[arg-type]

    output = Executor(runner=runner).execute_improved(
        plan,
        faithful,
        tmp_path / "work",
        lambda: False,
        source_mappings=(SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
    )

    correction = captured["correction"]
    assert isinstance(correction, FlickerCorrectionPlan)
    assert correction.intervals == ((0.5, 1.5),)
    assert output.read_bytes() == b"deflickered pixels"
    assert faithful.read_bytes() == b"verified faithful"


def test_native_executor_does_not_dispatch_review_gated_stabilization(
    tmp_path: Path,
) -> None:
    source_hash = _sha256_bytes(b"original source")
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    interval = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SHAKE, 0.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.SHAKE,
        start_seconds=0.0,
        end_seconds=2.0,
    )
    transform = MotionTransform(
        timestamp_seconds=0.0,
        rotation_degrees=0.0,
        scale=1.0,
        translation_x=1.0,
        translation_y=0.0,
        inlier_ratio=0.9,
        residual_pixels=0.5,
        semantics="frame_correction",
    )
    assessment = StabilizationAssessment(
        recommended=True,
        reason="Measured stable correction.",
        crop_ratio=0.02,
        transforms=(transform,),
        parameters={
            "crop_ratio": 0.02,
            "frame_width": 64,
            "frame_height": 64,
            "maximum_timeline_gap_seconds": 1.0,
            "smoothing_window_samples": 5,
        },
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        stabilization_assessment=assessment,
    )
    commands: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(arguments)
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                '{"format":{"duration":"2.0"},"streams":[{"codec_type":"video"}]}',
            )
        return CommandResult(0, "", "")

    class Executor(NativeRescueExecutor):
        dispatched: tuple[MotionTransform, ...] = ()

        def execute_stabilized(self, **kwargs: object) -> None:
            self.dispatched = tuple(kwargs["transforms"])  # type: ignore[arg-type]
            Path(kwargs["output"]).write_bytes(b"stabilized")  # type: ignore[arg-type]

    executor = Executor(runner=runner)
    assert RescueActionKind.STABILIZE not in {action.kind for action in plan.actions}
    assert "preview_renderer_unavailable" in " ".join(plan.assessment_warnings)
    with pytest.raises(RescueMediaError):
        executor.execute_improved(plan, faithful, tmp_path / "work", lambda: False)

    assert executor.dispatched == ()
    assert all(
        "deshake" not in command and "deflicker" not in command for command in commands
    )


@pytest.mark.parametrize(
    ("kind", "source_ranges", "locked_ranges", "source_mappings"),
    (
        (
            RescueActionKind.STABILIZE,
            ((0.5, 1.5),),
            (),
            (SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
        ),
        (
            RescueActionKind.NORMALIZE_ROTATION,
            ((0.0, 2.0),),
            ((0.5, 1.0),),
            (SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
        ),
        (
            RescueActionKind.CORRECT_FIXED_AV_OFFSET,
            ((0.0, 2.0),),
            (),
            (SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),),
        ),
    ),
)
def test_forged_action_scope_fails_before_media_runner(
    tmp_path: Path,
    kind: RescueActionKind,
    source_ranges: tuple[tuple[float, float], ...],
    locked_ranges: tuple[tuple[float, float], ...],
    source_mappings: tuple[SourceMapping, ...],
) -> None:
    """Catches bypassing planner scope gates with a forged confirmed plan."""
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    plan = _plan(b"original source", duration_seconds=2.0)
    action = RescueAction(
        id=f"rescue_action_forged_{kind.value}",
        version="1",
        kind=kind,
        description="Forged content-changing action.",
        source_ranges=source_ranges,
        parameters={},
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    effective_config = plan.effective_config.model_copy(
        update={"locked_ranges": locked_ranges}
    )
    object.__setattr__(plan, "strategy", RescueStrategy.BALANCED)
    object.__setattr__(plan, "effective_config", effective_config)
    object.__setattr__(plan, "actions", (*plan.actions, action))
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError) as exc_info:
        NativeRescueExecutor(runner=runner).execute_improved(
            plan,
            faithful,
            tmp_path / "work",
            lambda: False,
            source_mappings=source_mappings,
        )

    assert str(exc_info.value) == "The selected media could not be processed locally."
    assert (
        exc_info.value.internal_message
        == "confirmed Rescue action scope is not executable"
    )
    assert runner_calls == []


@pytest.mark.parametrize(
    ("kind", "damage_ranges", "locked_ranges"),
    ((RescueActionKind.NORMALIZE_ROTATION, (), ((0.5, 1.0),)),),
)
def test_forged_global_faithful_action_fails_before_media_runner(
    tmp_path: Path,
    kind: RescueActionKind,
    damage_ranges: tuple[tuple[float, float], ...],
    locked_ranges: tuple[tuple[float, float], ...],
) -> None:
    """Catches a locked global rotation action bypassing faithful scope checks."""
    source_bytes = b"original source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    plan = _plan(source_bytes, duration_seconds=2.0, damage_ranges=damage_ranges)
    action = RescueAction(
        id=f"rescue_action_forged_faithful_{kind.value}",
        version="1",
        kind=kind,
        description="Forged global faithful action.",
        source_ranges=((0.0, 2.0),),
        parameters={},
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.CONSERVATIVE,
    )
    effective_config = plan.effective_config.model_copy(
        update={"locked_ranges": locked_ranges}
    )
    object.__setattr__(plan, "effective_config", effective_config)
    object.__setattr__(plan, "actions", (*plan.actions, action))
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError) as exc_info:
        NativeRescueExecutor(runner=runner).execute_faithful(
            plan, source, tmp_path / "work", lambda: False
        )

    assert (
        exc_info.value.internal_message
        == "confirmed Rescue action scope is not executable"
    )
    assert runner_calls == []


@pytest.mark.parametrize(
    ("kind", "parameters"),
    (
        (
            RescueActionKind.ADJUST_LUMA,
            {"brightness": 0.04, "contrast": 1.02},
        ),
        (
            RescueActionKind.DEFLICKER,
            {
                "affected_ranges": [[0.0, 2.0]],
                "gain_curve": [[0.0, 1.0], [1.0, 1.1], [2.0, 1.0]],
                "excluded_fade_ranges": [],
            },
        ),
    ),
)
def test_missing_mappings_after_faithful_deletion_fail_before_media_runner(
    tmp_path: Path,
    kind: RescueActionKind,
    parameters: dict[str, object],
) -> None:
    """Catches fabricating identity mapping after faithful timeline compaction."""
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    plan = _plan(
        b"original source", duration_seconds=2.0, damage_ranges=((0.75, 1.25),)
    )
    action = RescueAction(
        id=f"rescue_action_forged_missing_mapping_{kind.value}",
        version="1",
        kind=kind,
        description="Forged local improvement without a retained mapping.",
        source_ranges=((0.0, 2.0),),
        parameters=cast(dict[str, JsonValue], parameters),
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    object.__setattr__(plan, "strategy", RescueStrategy.BALANCED)
    object.__setattr__(plan, "actions", (*plan.actions, action))
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError) as exc_info:
        NativeRescueExecutor(runner=runner).execute_improved(
            plan, faithful, tmp_path / "work", lambda: False
        )

    assert (
        exc_info.value.internal_message
        == "confirmed faithful source mapping is required"
    )
    assert runner_calls == []
