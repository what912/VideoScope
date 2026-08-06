"""Bounded-resource contracts; these tests make no universal speed claim."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from queue import Queue
from typing import Any

import pytest

from videoscope.rescue import assessment as assessment_module
from videoscope.rescue import scanner as scanner_module
from videoscope.rescue.assessment import (
    LocalRescueAssessmentService,
    RescueAssessmentConfig,
)
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueEffectiveConfig,
    RescueStrategy,
    make_damage_id,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.preview import RescuePreviewBuilder, SubprocessPreviewRunner
from videoscope.rescue.scanner import FFmpegMediaRunner, RescueScanConfig
from videoscope.video import sampling as sampling_module
from videoscope.video.errors import FrameSamplingError
from videoscope.video.probe import probe_video

_sampling_subprocess: Any = getattr(sampling_module, "subprocess")


@pytest.fixture(scope="module")
def real_resource_videos(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required for real resource contracts")
    assert ffmpeg is not None and ffprobe is not None
    root = tmp_path_factory.mktemp("real-resource-videos")
    outputs: list[Path] = []
    for name, duration in (("short local.mp4", 2.0), ("long 中文.mp4", 12.0)):
        output = root / name
        completed = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=96x64:rate=10:duration={duration:g}",
                "-an",
                "-c:v",
                "mpeg4",
                "-g",
                "1",
                str(output),
            ],
            check=False,
            shell=False,
            capture_output=True,
            timeout=30,
        )
        assert completed.returncode == 0
        outputs.append(output)
    return outputs[0], outputs[1]


@pytest.fixture(scope="module")
def real_timeline_edge_videos(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path, Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required for timeline sampling contracts")
    assert ffmpeg is not None and ffprobe is not None
    root = tmp_path_factory.mktemp("real-timeline-videos")
    vfr = root / "variable rate 中文.mkv"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=5:duration=2",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=20:duration=2",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-fps_mode",
            "vfr",
            "-c:v",
            "ffv1",
            str(vfr),
        ],
        check=False,
        shell=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0
    one_frame = root / "one frame.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=10:duration=0.05",
            "-an",
            "-c:v",
            "mpeg4",
            "-g",
            "1",
            str(one_frame),
        ],
        check=False,
        shell=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0
    nonzero_pts = root / "nonzero start 中文.mkv"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=10:duration=4",
            "-vf",
            "setpts=PTS+5/TB",
            "-an",
            "-c:v",
            "ffv1",
            "-fps_mode",
            "passthrough",
            str(nonzero_pts),
        ],
        check=False,
        shell=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0
    stale_duration = root / "stale duration.mp4"
    completed = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=10:duration=12",
            "-an",
            "-c:v",
            "mpeg4",
            "-g",
            "1",
            "-movflags",
            "+faststart",
            str(stale_duration),
        ],
        check=False,
        shell=False,
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0
    payload = stale_duration.read_bytes()
    stale_duration.write_bytes(payload[: int(len(payload) * 0.65)])
    return vfr, one_frame, nonzero_pts, stale_duration


def _damage_map(*intervals: DamageInterval, duration: float = 20.0) -> MediaDamageMap:
    return MediaDamageMap(
        input_hash="a" * 64,
        duration_seconds=duration,
        scan_coverage=((0.0, duration),),
        intervals=intervals,
    )


def _interval(kind: DamageKind, start: float, end: float) -> DamageInterval:
    return DamageInterval(
        id=make_damage_id("a" * 64, "video:0", kind, start, end),
        stream_id="video:0",
        kind=kind,
        start_seconds=start,
        end_seconds=end,
    )


def test_real_compatible_assessments_share_one_bounded_sample_decode(
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    records: list[tuple[Path, tuple[float, ...], bool]] = []
    real_provider = assessment_module._sample_frames_once

    def recording_provider(*args: Any, **kwargs: Any) -> Any:
        sampled = real_provider(*args, **kwargs)
        records.append(
            (
                Path(args[0]),
                tuple(sample.timestamp_seconds for sample in sampled.visual_samples),
                sampled.truncated,
            )
        )
        return sampled

    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )
    service = LocalRescueAssessmentService(
        config=config,
        frame_provider=recording_provider,
    )

    results = []
    for source in real_resource_videos:
        metadata = probe_video(source)
        results.append(
            service.assess(
                source,
                "a" * 64,
                metadata,
                _damage_map(duration=metadata.duration_seconds),
                tmp_path / source.stem,
                lambda: False,
            )
        )

    assert [record[0] for record in records] == list(real_resource_videos)
    assert [len(record[1]) for record in records] == [4, 6]
    assert [record[2] for record in records] == [False, True]
    assert [result.parameters["frame_decode_passes"] for result in results] == [1, 1]
    assert [result.parameters["sampled_frame_count"] for result in results] == [4, 6]
    assert [result.parameters["sample_limit"] for result in results] == [6, 6]
    assert [result.parameters["sample_truncated"] for result in results] == [
        False,
        True,
    ]
    assert any("bounded sample limit" in item for item in results[1].limitations)

    long_metadata = probe_video(real_resource_videos[1])
    long_timestamps = records[1][1]
    frame_period = 1.0 / long_metadata.average_frame_rate
    assert long_timestamps[0] <= frame_period
    assert long_metadata.duration_seconds - long_timestamps[-1] <= frame_period
    assert (
        max(
            later - earlier
            for earlier, later in zip(long_timestamps, long_timestamps[1:])
        )
        <= (long_metadata.duration_seconds / (config.maximum_sample_count - 1))
        + frame_period
    )


def test_real_uncapped_sampling_preserves_fixed_rate_cadence_in_one_streaming_decode(
    monkeypatch: pytest.MonkeyPatch,
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    metadata = probe_video(source)
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=30,
    )
    popen_commands: list[tuple[str, ...]] = []
    real_popen = _sampling_subprocess.Popen

    def recording_popen(args: list[str], **kwargs: Any) -> Any:
        popen_commands.append(tuple(str(argument) for argument in args))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(_sampling_subprocess, "Popen", recording_popen)

    sampled = assessment_module._sample_frames_once(
        source, tmp_path / "uncapped-fixed-rate", metadata, config, lambda: False
    )

    timestamps = [sample.timestamp_seconds for sample in sampled.visual_samples]
    ffmpeg_streams = [
        command
        for command in popen_commands
        if Path(command[0]).stem.lower() == "ffmpeg"
    ]
    assert timestamps == pytest.approx(
        [index / config.sample_rate for index in range(24)], abs=0.001
    )
    assert sampled.sample_rate == config.sample_rate
    assert sampled.truncated is False
    assert len(ffmpeg_streams) == 1
    assert "image2pipe" in ffmpeg_streams[0]
    assert "showinfo" in ffmpeg_streams[0][ffmpeg_streams[0].index("-vf") + 1]


def test_real_stale_low_probe_cannot_bypass_decoded_tail_audit(
    monkeypatch: pytest.MonkeyPatch,
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    metadata = probe_video(source)
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )
    commands: list[tuple[str, ...]] = []
    real_popen = _sampling_subprocess.Popen

    def stale_low_probe(*args: Any, **kwargs: Any) -> sampling_module._TimelineProbe:
        del args, kwargs
        return sampling_module._TimelineProbe(
            duration_seconds=2.0,
            raw_duration_seconds=2.0,
        )

    def recording_popen(args: list[str], **kwargs: Any) -> Any:
        commands.append(tuple(str(argument) for argument in args))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(sampling_module, "_timeline_probe", stale_low_probe)
    monkeypatch.setattr(_sampling_subprocess, "Popen", recording_popen)

    with pytest.raises(FrameSamplingError, match="duration") as error:
        assessment_module._sample_frames_once(
            source,
            tmp_path / "stale-low-tail-audit",
            metadata,
            config,
            lambda: False,
        )

    ffmpeg_streams = [
        command for command in commands if Path(command[0]).stem.lower() == "ffmpeg"
    ]
    assert len(ffmpeg_streams) == 1
    assert not list(error.value.work_directory.rglob("*.png"))


def test_real_stale_low_duration_cannot_bypass_sample_cap(
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    metadata = probe_video(source).model_copy(update={"duration_seconds": 2.0})
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )

    sampled = assessment_module._sample_frames_once(
        source, tmp_path / "stale-low-cap", metadata, config, lambda: False
    )

    timestamps = [sample.timestamp_seconds for sample in sampled.visual_samples]
    assert len(timestamps) == config.maximum_sample_count
    assert sampled.truncated is True
    assert timestamps[0] == pytest.approx(0.0, abs=0.051)
    assert timestamps[-1] == pytest.approx(11.9, abs=0.051)


def test_real_capped_sampling_uses_exactly_one_video_decode_process(
    monkeypatch: pytest.MonkeyPatch,
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    metadata = probe_video(source)
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )
    commands: list[tuple[str, ...]] = []
    real_popen = _sampling_subprocess.Popen

    def recording_popen(args: list[str], **kwargs: Any) -> Any:
        commands.append(tuple(str(argument) for argument in args))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(_sampling_subprocess, "Popen", recording_popen)

    sampled = assessment_module._sample_frames_once(
        source, tmp_path / "single-decode", metadata, config, lambda: False
    )

    decode_commands = [
        command
        for command in commands
        if Path(command[0]).stem.lower() == "ffmpeg"
        or "-show_frames" in command
        or "-count_frames" in command
    ]
    assert len(decode_commands) == 1
    assert Path(decode_commands[0][0]).stem.lower() == "ffmpeg"
    assert all("-show_frames" not in command for command in commands)
    assert all("-count_frames" not in command for command in commands)
    assert sampled.decode_passes == len(decode_commands)


def test_real_capped_sampling_uses_bounded_stream_queues_and_command(
    monkeypatch: pytest.MonkeyPatch,
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    metadata = probe_video(source)
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )
    capacities: list[int] = []
    high_water: list[int] = []
    commands: list[tuple[str, ...]] = []
    stream_results: list[Any] = []
    real_popen = _sampling_subprocess.Popen
    real_stream = sampling_module._stream_timeline_candidates

    class TrackingQueue(Queue[Any]):
        def __init__(self, maxsize: int = 0) -> None:
            super().__init__(maxsize=maxsize)
            capacities.append(maxsize)

        def put(self, item: Any, *args: Any, **kwargs: Any) -> None:
            super().put(item, *args, **kwargs)
            high_water.append(self.qsize())

    def recording_popen(args: list[str], **kwargs: Any) -> Any:
        commands.append(tuple(str(argument) for argument in args))
        return real_popen(args, **kwargs)

    def recording_stream(*args: Any, **kwargs: Any) -> Any:
        result = real_stream(*args, **kwargs)
        stream_results.append(result)
        return result

    monkeypatch.setattr(sampling_module, "Queue", TrackingQueue)
    monkeypatch.setattr(_sampling_subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        sampling_module, "_stream_timeline_candidates", recording_stream
    )

    sampled = assessment_module._sample_frames_once(
        source, tmp_path / "bounded-stream", metadata, config, lambda: False
    )

    ffmpeg_commands = [
        command for command in commands if Path(command[0]).stem.lower() == "ffmpeg"
    ]
    assert capacities == [2, 16]
    assert high_water and max(high_water) <= 16
    assert len(ffmpeg_commands) == 1
    assert len(ffmpeg_commands[0]) <= 32
    assert len(ffmpeg_commands[0][ffmpeg_commands[0].index("-vf") + 1]) <= 160
    assert len(sampled.visual_samples) == config.maximum_sample_count
    assert len(stream_results) == 1
    audit = stream_results[0]
    assert audit.retained_payload_high_water <= 2
    assert audit.target_advances == config.maximum_sample_count
    assert audit.distance_comparisons <= 2 * audit.target_advances
    assert audit.finalization_visits == audit.target_advances
    assert audit.decoded_frames > audit.target_advances


def test_real_capped_sampling_with_one_slot_is_deterministically_the_first_frame(
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[0]
    metadata = probe_video(source)
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=1,
    )

    first = assessment_module._sample_frames_once(
        source, tmp_path / "first", metadata, config, lambda: False
    )
    second = assessment_module._sample_frames_once(
        source, tmp_path / "second", metadata, config, lambda: False
    )

    assert [sample.timestamp_seconds for sample in first.visual_samples] == [0.0]
    assert [sample.timestamp_seconds for sample in second.visual_samples] == [0.0]


@pytest.mark.parametrize("stale_frame_count", [0, 3, 1200])
def test_real_capped_sampling_covers_cfr_when_estimated_count_is_missing_or_stale(
    stale_frame_count: int,
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    metadata = probe_video(source).model_copy(
        update={"estimated_frame_count": stale_frame_count}
    )
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )

    sampled = assessment_module._sample_frames_once(
        source,
        tmp_path / f"stale-{stale_frame_count}",
        metadata,
        config,
        lambda: False,
    )

    timestamps = [sample.timestamp_seconds for sample in sampled.visual_samples]
    assert len(timestamps) == 6
    assert timestamps == pytest.approx([0.0, 2.4, 4.8, 7.2, 9.6, 11.9], abs=0.051)


def test_real_capped_sampling_uses_actual_timeline_for_vfr_and_stale_count(
    real_timeline_edge_videos: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    source = real_timeline_edge_videos[0]
    metadata = probe_video(source)
    assert metadata.estimated_frame_count == 80
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )

    sampled = assessment_module._sample_frames_once(
        source, tmp_path / "vfr", metadata, config, lambda: False
    )

    timestamps = [sample.timestamp_seconds for sample in sampled.visual_samples]
    assert len(timestamps) == 6
    assert timestamps == pytest.approx([0.0, 0.8, 1.6, 2.4, 3.2, 3.95], abs=0.051)


def test_real_capped_sampling_of_one_frame_video_with_one_slot_is_stable(
    real_timeline_edge_videos: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    source = real_timeline_edge_videos[1]
    metadata = probe_video(source).model_copy(update={"estimated_frame_count": 999})
    config = RescueAssessmentConfig(
        sample_rate=12.0,
        maximum_frame_edge=96,
        maximum_sample_count=1,
    )

    sampled = assessment_module._sample_frames_once(
        source, tmp_path / "one-frame", metadata, config, lambda: False
    )

    assert [sample.timestamp_seconds for sample in sampled.visual_samples] == [0.0]


def test_real_capped_sampling_normalizes_nonzero_start_pts(
    real_timeline_edge_videos: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    source = real_timeline_edge_videos[2]
    metadata = probe_video(source)
    assert metadata.duration_seconds == pytest.approx(9.0, abs=0.001)
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )

    sampled = assessment_module._sample_frames_once(
        source, tmp_path / "nonzero-pts", metadata, config, lambda: False
    )

    timestamps = [sample.timestamp_seconds for sample in sampled.visual_samples]
    assert len(timestamps) == 6
    assert timestamps == pytest.approx([0.0, 0.8, 1.6, 2.4, 3.2, 3.9], abs=0.051)
    assert all(timestamp >= 0.0 for timestamp in timestamps)


def test_real_capped_sampling_audits_stale_container_duration_in_same_decode(
    real_timeline_edge_videos: tuple[Path, Path, Path, Path],
    tmp_path: Path,
) -> None:
    source = real_timeline_edge_videos[3]
    metadata = probe_video(source)
    assert metadata.duration_seconds == pytest.approx(12.0, abs=0.001)
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )

    with pytest.raises(FrameSamplingError, match="duration"):
        assessment_module._sample_frames_once(
            source, tmp_path / "stale-container", metadata, config, lambda: False
        )


@pytest.mark.parametrize("reported_duration", [0.0, 1200.0])
def test_real_capped_sampling_fails_closed_for_zero_or_stale_duration(
    reported_duration: float,
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    metadata = probe_video(source).model_copy(
        update={"duration_seconds": reported_duration}
    )
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )

    with pytest.raises(FrameSamplingError, match="duration"):
        assessment_module._sample_frames_once(
            source,
            tmp_path / f"duration-{reported_duration:g}",
            metadata,
            config,
            lambda: False,
        )


def test_real_capped_sampling_audits_stale_high_requested_duration_in_one_decode(
    monkeypatch: pytest.MonkeyPatch,
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    metadata = probe_video(source).model_copy(update={"duration_seconds": 1200.0})
    config = RescueAssessmentConfig(
        sample_rate=2.0,
        maximum_frame_edge=96,
        maximum_sample_count=6,
    )
    commands: list[tuple[str, ...]] = []
    real_popen = _sampling_subprocess.Popen

    def recording_popen(args: list[str], **kwargs: Any) -> Any:
        commands.append(tuple(str(argument) for argument in args))
        return real_popen(args, **kwargs)

    monkeypatch.setattr(_sampling_subprocess, "Popen", recording_popen)

    with pytest.raises(FrameSamplingError, match="duration") as error:
        assessment_module._sample_frames_once(
            source,
            tmp_path / "stale-high-duration",
            metadata,
            config,
            lambda: False,
        )

    ffmpeg_streams = [
        command for command in commands if Path(command[0]).stem.lower() == "ffmpeg"
    ]
    assert len(ffmpeg_streams) == 1
    assert not list(error.value.work_directory.rglob("*.png"))


def test_streaming_ffprobe_queue_high_water_is_bounded_independent_of_line_count(
    monkeypatch: pytest.MonkeyPatch,
    real_resource_videos: tuple[Path, Path],
) -> None:
    capacities: list[int] = []
    high_water: list[int] = []

    class TrackingQueue(Queue[Any]):
        def __init__(self, maxsize: int = 0) -> None:
            super().__init__(maxsize=maxsize)
            capacities.append(maxsize)

        def put(self, item: Any, *args: Any, **kwargs: Any) -> None:
            super().put(item, *args, **kwargs)
            high_water.append(self.qsize())

    monkeypatch.setattr(scanner_module, "Queue", TrackingQueue)
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None
    runner = FFmpegMediaRunner(RescueScanConfig(ffprobe_executable=ffprobe))

    packet_counts = [
        sum(1 for _packet in runner.iter_packets(source))
        for source in real_resource_videos
    ]

    assert packet_counts[1] > packet_counts[0] > 0
    assert capacities == [16, 16]
    assert high_water
    assert max(high_water) <= 16


def test_preview_commands_and_invocations_remain_within_ten_seconds(
    real_resource_videos: tuple[Path, Path],
    tmp_path: Path,
) -> None:
    source = real_resource_videos[1]
    damage = _damage_map(
        _interval(DamageKind.UNDECODABLE, 0.0, 3.0),
        _interval(DamageKind.VIDEO_NOISE, 3.0, 9.0),
        _interval(DamageKind.FLICKER, 9.0, 12.0),
        duration=12.0,
    )
    config = RescueEffectiveConfig(max_preview_total_seconds=10.0)
    plan = build_rescue_plan(
        metadata=probe_video(source),
        damage_map=damage,
        strategy=RescueStrategy.BALANCED,
        config=config,
    )
    commands: list[tuple[str, ...]] = []
    real_runner = SubprocessPreviewRunner()

    class RecordingRunner:
        def run(self, command: list[str]) -> None:
            commands.append(tuple(command))
            real_runner.run(command)

    previews = RescuePreviewBuilder(RecordingRunner()).build(
        plan,
        source,
        tmp_path / "private previews",
    )

    assert sum(end - start for start, end in plan.preview_ranges) <= 10.0
    assert len(plan.preview_ranges) <= config.max_preview_ranges
    assert len(commands) <= config.max_preview_ranges * 3
    assert all(path.is_file() for path in previews.all_paths())
    for command in commands:
        duration = float(command[command.index("-t") + 1])
        assert 0.0 < duration <= 10.0
