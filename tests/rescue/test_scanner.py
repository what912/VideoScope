"""Contract tests for the bounded Video Rescue damage scanner."""

from __future__ import annotations

import sys
from collections.abc import Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import cast

import pytest

from videoscope.domain import VideoMetadata
from videoscope.rescue import DamageKind
from videoscope.rescue.errors import RescueScanError
from videoscope.rescue.scanner import (
    MAX_STDERR_BYTES,
    DecodeObservation,
    FFmpegMediaRunner,
    PacketObservation,
    RescueScanConfig,
    RescueScanner,
    _decode_progress_covers_window,
    _decode_window,
    _iter_command_stdout_lines,
    _ProcessContext,
    _start_process,
)


@dataclass(frozen=True)
class FakeMediaRunner:
    packet_observations: tuple[PacketObservation, ...]
    decode_observations: tuple[DecodeObservation, ...]

    def iter_packets(self, source: Path) -> Iterator[PacketObservation]:
        del source
        yield from self.packet_observations

    def iter_decodes(
        self,
        source: Path,
        *,
        duration_seconds: float,
        chunk_seconds: float,
    ) -> Iterator[DecodeObservation]:
        del source, duration_seconds, chunk_seconds
        yield from self.decode_observations


def video_metadata(*, duration_seconds: float, has_audio: bool = True) -> VideoMetadata:
    return VideoMetadata(
        filename="damage scan.mp4",
        container_format="mov,mp4,m4a,3gp,3g2,mj2",
        codec="mpeg4",
        width=320,
        height=180,
        duration_seconds=duration_seconds,
        average_frame_rate=10.0,
        estimated_frame_count=int(duration_seconds * 10),
        has_audio=has_audio,
        file_size_bytes=1,
    )


def observations_with_gap(
    start_seconds: float, end_seconds: float
) -> tuple[PacketObservation, ...]:
    return tuple(
        PacketObservation(
            stream_id="video:0",
            pts_seconds=float(second),
            dts_seconds=float(second),
            duration_seconds=1.0,
        )
        for second in (0, 1, 3, 4, 5)
        if not start_seconds <= second < end_seconds
    )


def test_scanner_recovers_after_middle_decode_failure(tmp_path: Path) -> None:
    runner = FakeMediaRunner(
        packet_observations=observations_with_gap(2.0, 3.0),
        decode_observations=(
            DecodeObservation("video:0", 0.0, 2.0, True),
            DecodeObservation("video:0", 2.0, 3.0, False, "invalid data"),
            DecodeObservation("video:0", 3.0, 6.0, True),
        ),
    )

    damage_map = RescueScanner(runner=runner).scan(
        source=tmp_path / "损坏 视频.mp4",
        input_hash="a" * 64,
        metadata=video_metadata(duration_seconds=6.0),
        config=RescueScanConfig(),
    )

    assert [
        (item.kind, item.start_seconds, item.end_seconds)
        for item in damage_map.intervals
    ] == [
        (DamageKind.DECODABLE, 0.0, 2.0),
        (DamageKind.TIMESTAMP_DISCONTINUITY, 2.0, 3.0),
        (DamageKind.UNDECODABLE, 2.0, 3.0),
        (DamageKind.DECODABLE, 3.0, 6.0),
    ]
    assert damage_map.scan_coverage == ((0.0, 6.0),)
    error_summary = next(
        item.measurements["error_summary"]
        for item in damage_map.intervals
        if item.kind is DamageKind.UNDECODABLE
    )
    assert isinstance(error_summary, str)
    assert "invalid data" in error_summary


def test_scanner_reports_missing_audio_and_timestamp_regression_deterministically(
    tmp_path: Path,
) -> None:
    runner = FakeMediaRunner(
        packet_observations=(
            PacketObservation("video:0", 0.0, 0.0, 1.0),
            PacketObservation("video:0", 1.0, 1.0, 1.0),
            PacketObservation("video:0", 2.0, 0.5, 1.0),
        ),
        decode_observations=(DecodeObservation("video:0", 0.0, 3.0, True),),
    )

    damage_map = RescueScanner(runner=runner).scan(
        source=tmp_path / "中文 space.mp4",
        input_hash="b" * 64,
        metadata=video_metadata(duration_seconds=3.0, has_audio=False),
        config=RescueScanConfig(),
    )

    assert [(item.kind, item.stream_id) for item in damage_map.intervals] == [
        (DamageKind.DECODABLE, "video:0"),
        (DamageKind.MISSING_STREAM, "audio"),
        (DamageKind.TIMESTAMP_DISCONTINUITY, "video:0"),
    ]
    timestamp_interval = next(
        item
        for item in damage_map.intervals
        if item.kind is DamageKind.TIMESTAMP_DISCONTINUITY
    )
    assert timestamp_interval.measurements["dts_monotonic"] is False
    assert all(item.id.startswith("damage_") for item in damage_map.intervals)


def test_scanner_merges_only_same_kind_intervals_inside_the_configured_tolerance(
    tmp_path: Path,
) -> None:
    runner = FakeMediaRunner(
        packet_observations=(),
        decode_observations=(
            DecodeObservation("video:0", 0.0, 1.0, True),
            DecodeObservation("video:0", 1.04, 2.0, True),
            DecodeObservation("video:0", 2.2, 3.0, True),
        ),
    )

    damage_map = RescueScanner(runner=runner).scan(
        source=tmp_path / "bounded.mp4",
        input_hash="c" * 64,
        metadata=video_metadata(duration_seconds=3.0),
        config=RescueScanConfig(merge_tolerance_seconds=0.05),
    )

    assert [
        (item.start_seconds, item.end_seconds) for item in damage_map.intervals
    ] == [(0.0, 2.0), (2.2, 3.0)]


def test_packet_runner_preserves_negative_dts_for_monotonicity_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "videoscope.rescue.scanner._iter_command_stdout_lines",
        lambda *_args, **_kwargs: iter(
            ["stream_index=0|pts_time=-0.2|dts_time=-0.2|duration_time=0.1"]
        ),
    )

    observations = tuple(
        FFmpegMediaRunner(RescueScanConfig()).iter_packets(tmp_path / "negative.mp4")
    )

    assert observations[0].pts_seconds == -0.2
    assert observations[0].dts_seconds == -0.2


def test_decode_runner_fails_each_bounded_window_on_any_decode_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []

    def observe(
        arguments: list[str],
        *,
        source: Path,
        timeout_seconds: float,
        requested_duration_seconds: float,
    ) -> None:
        del source, timeout_seconds, requested_duration_seconds
        commands.append(arguments)
        return None

    monkeypatch.setattr("videoscope.rescue.scanner._decode_window", observe)

    tuple(
        FFmpegMediaRunner(RescueScanConfig()).iter_decodes(
            tmp_path / "middle damage.mp4",
            duration_seconds=2.0,
            chunk_seconds=1.0,
        )
    )

    assert len(commands) == 2
    assert all("-xerror" in command for command in commands)
    assert all(command[command.index("-t") + 1] == "1" for command in commands)


def test_decode_runner_uses_progress_for_source_window_attribution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches treating boundary read-ahead as damage in the preceding window."""
    commands: list[list[str]] = []

    def observe(
        arguments: list[str],
        *,
        source: Path,
        timeout_seconds: float,
        requested_duration_seconds: float,
    ) -> None:
        del source, timeout_seconds
        assert requested_duration_seconds == 1.0
        commands.append(arguments)
        return None

    monkeypatch.setattr("videoscope.rescue.scanner._decode_window", observe)

    tuple(
        FFmpegMediaRunner(RescueScanConfig()).iter_decodes(
            tmp_path / "clean before damage.mp4",
            duration_seconds=2.0,
            chunk_seconds=1.0,
        )
    )

    assert len(commands) == 2
    assert all(command.index("-ss") < command.index("-i") for command in commands)
    assert all(
        command[command.index("-progress") + 1] == "pipe:1" for command in commands
    )


def test_decode_progress_distinguishes_boundary_read_ahead_from_in_window_failure() -> (
    None
):
    complete = b"frame=10\nout_time_us=1000000\nprogress=end\n"
    failed_inside = b"frame=0\nout_time_us=N/A\nprogress=end\n"

    assert _decode_progress_covers_window(complete, 1.0)
    assert not _decode_progress_covers_window(failed_inside, 1.0)


def test_scanner_records_a_forward_packet_gap_with_timestamp_evidence(
    tmp_path: Path,
) -> None:
    runner = FakeMediaRunner(
        packet_observations=(
            PacketObservation("video:0", 0.0, 0.0, 1.0),
            PacketObservation("video:0", 3.0, 3.0, 1.0),
            PacketObservation("video:0", 4.0, 4.0, 1.0),
        ),
        decode_observations=(DecodeObservation("video:0", 0.0, 5.0, True),),
    )
    result = RescueScanner(runner=runner).scan(
        tmp_path / "gap.mp4",
        "e" * 64,
        video_metadata(duration_seconds=5.0),
        RescueScanConfig(),
    )
    gap = next(
        item
        for item in result.intervals
        if item.kind is DamageKind.TIMESTAMP_DISCONTINUITY
    )
    assert (gap.start_seconds, gap.end_seconds) == (1.0, 3.0)
    assert gap.measurements["first_valid_timestamp_seconds"] == 0.0
    assert gap.measurements["last_valid_timestamp_seconds"] == 4.0


def test_scanner_records_packet_timestamp_summary_for_an_ordinary_stream(
    tmp_path: Path,
) -> None:
    runner = FakeMediaRunner(
        packet_observations=(
            PacketObservation("video:0", 0.0, 0.0, 1.0),
            PacketObservation("video:0", 1.0, 1.0, 1.0),
            PacketObservation("video:0", 2.0, 2.0, 1.0),
        ),
        decode_observations=(DecodeObservation("video:0", 0.0, 3.0, True),),
    )

    result = RescueScanner(runner=runner).scan(
        tmp_path / "ordinary.mp4",
        "8" * 64,
        video_metadata(duration_seconds=3.0),
        RescueScanConfig(),
    )

    assert [item.kind for item in result.intervals] == [DamageKind.DECODABLE]
    assert result.intervals[0].measurements["first_valid_timestamp_seconds"] == 0.0
    assert result.intervals[0].measurements["last_valid_timestamp_seconds"] == 2.0


def test_scanner_uses_pts_for_a_packet_stream_without_dts(tmp_path: Path) -> None:
    runner = FakeMediaRunner(
        packet_observations=(
            PacketObservation("video:0", 0.25, None, 0.5),
            PacketObservation("video:0", 0.75, None, 0.5),
        ),
        decode_observations=(DecodeObservation("video:0", 0.0, 1.25, True),),
    )

    result = RescueScanner(runner=runner).scan(
        tmp_path / "pts-only.mp4",
        "9" * 64,
        video_metadata(duration_seconds=1.25),
        RescueScanConfig(),
    )

    assert [item.kind for item in result.intervals] == [DamageKind.DECODABLE]
    assert result.intervals[0].measurements["first_valid_timestamp_seconds"] == 0.25
    assert result.intervals[0].measurements["last_valid_timestamp_seconds"] == 0.75


def test_scanner_marks_missing_decode_observations(tmp_path: Path) -> None:
    result = RescueScanner(runner=FakeMediaRunner((), ())).scan(
        tmp_path / "none.mp4",
        "f" * 64,
        video_metadata(duration_seconds=2.0),
        RescueScanConfig(),
    )
    assert result.intervals[0].kind is DamageKind.MISSING_INFORMATION


def test_scanner_does_not_modify_the_source(tmp_path: Path) -> None:
    source = tmp_path / "只读 源.mp4"
    original = b"source-bytes-remain-unchanged"
    source.write_bytes(original)
    runner = FakeMediaRunner(
        packet_observations=(PacketObservation("video:0", 0.0, 0.0, 1.0),),
        decode_observations=(DecodeObservation("video:0", 0.0, 1.0, True),),
    )

    RescueScanner(runner=runner).scan(
        source,
        "1" * 64,
        video_metadata(duration_seconds=1.0),
        RescueScanConfig(),
    )

    assert source.read_bytes() == original


def test_missing_ffprobe_executable_is_actionable(tmp_path: Path) -> None:
    executable = tmp_path / "missing-ffprobe-executable"

    with pytest.raises(RescueScanError) as error:
        tuple(
            _iter_command_stdout_lines(
                [str(executable), "-version"],
                source=tmp_path / "input.mp4",
                timeout_seconds=1.0,
            )
        )

    assert error.value.internal_message is not None
    assert error.value.internal_message == (
        "missing-ffprobe-executable executable was not found"
    )


def _assert_external_command_drains_and_keeps_stderr_tail(
    *, command_kind: str, tmp_path: Path
) -> None:
    source = tmp_path / "私密 路径" / "损坏 视频.mp4"
    script = (
        "import os, sys\n"
        "source = sys.argv[1]\n"
        "os.write(2, b'x' * 262144)\n"
        "os.write(2, ('TAIL ' + source + ' ' + 'z' * 512).encode('utf-8'))\n"
        "raise SystemExit(7)\n"
    )
    arguments = [sys.executable, "-c", script, str(source)]

    if command_kind == "ffprobe":
        with pytest.raises(RescueScanError) as error:
            tuple(
                _iter_command_stdout_lines(
                    arguments,
                    source=source,
                    timeout_seconds=5.0,
                )
            )
        message = error.value.internal_message
        assert message is not None
    else:
        message = _decode_window(
            arguments,
            source=source,
            timeout_seconds=5.0,
            requested_duration_seconds=1.0,
        )
        assert message is not None

    assert len(message.encode("utf-8")) == MAX_STDERR_BYTES
    assert "TAIL " in message
    assert "<input>" in message
    assert str(source) not in message


def test_ffprobe_drains_and_keeps_an_exact_sanitized_stderr_tail(
    tmp_path: Path,
) -> None:
    _assert_external_command_drains_and_keeps_stderr_tail(
        command_kind="ffprobe", tmp_path=tmp_path
    )


def test_ffmpeg_drains_and_keeps_an_exact_sanitized_stderr_tail(
    tmp_path: Path,
) -> None:
    _assert_external_command_drains_and_keeps_stderr_tail(
        command_kind="ffmpeg", tmp_path=tmp_path
    )


def _track_process_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> list[_ProcessContext]:
    contexts: list[_ProcessContext] = []

    def tracking_start_process(
        arguments: list[str],
        stderr_data: bytearray,
        *,
        stdout: int = -1,
    ) -> _ProcessContext:
        context = _start_process(arguments, stderr_data, stdout=stdout)
        contexts.append(context)
        return context

    monkeypatch.setattr(
        "videoscope.rescue.scanner._start_process", tracking_start_process
    )
    return contexts


def _assert_external_command_timeout_stops_child(
    *,
    command_kind: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contexts = _track_process_contexts(monkeypatch)
    source = tmp_path / "hung child.mp4"
    script = "import os\nwhile True:\n    os.write(2, b'x' * 65536)\n"
    arguments = [sys.executable, "-c", script]
    started = monotonic()

    with pytest.raises(RescueScanError) as error:
        if command_kind == "ffprobe":
            tuple(
                _iter_command_stdout_lines(
                    arguments,
                    source=source,
                    timeout_seconds=0.2,
                )
            )
        else:
            _decode_window(
                arguments,
                source=source,
                timeout_seconds=0.2,
                requested_duration_seconds=1.0,
            )

    assert error.value.internal_message is not None
    assert "timed out" in error.value.internal_message
    assert monotonic() - started < 3.0
    assert len(contexts) == 1
    assert contexts[0].process.poll() is not None


def test_ffprobe_timeout_stops_the_hung_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _assert_external_command_timeout_stops_child(
        command_kind="ffprobe", monkeypatch=monkeypatch, tmp_path=tmp_path
    )


def test_ffmpeg_timeout_stops_the_hung_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _assert_external_command_timeout_stops_child(
        command_kind="ffmpeg", monkeypatch=monkeypatch, tmp_path=tmp_path
    )


def test_closing_packet_stream_stops_child_and_reader_threads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    contexts = _track_process_contexts(monkeypatch)
    source = tmp_path / "early close.mp4"
    script = (
        "import sys, time\n"
        "print('stream_index=0|pts_time=0|dts_time=0|duration_time=1', flush=True)\n"
        "time.sleep(5)\n"
    )
    lines = cast(
        Generator[str, None, None],
        _iter_command_stdout_lines(
            [sys.executable, "-c", script],
            source=source,
            timeout_seconds=30.0,
        ),
    )

    assert next(lines).startswith("stream_index=0")
    try:
        started = monotonic()
        lines.close()
        assert monotonic() - started < 2.0
        assert len(contexts) == 1
        assert contexts[0].process.poll() is not None
    finally:
        for context in contexts:
            process = context.process
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3.0)
