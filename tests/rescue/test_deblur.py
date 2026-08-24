from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import ValidationError

import videoscope.rescue.deblur as deblur_module
import videoscope.rescue.verification as verification_module
from videoscope.rescue.deblur import (
    BlurKernelEstimate,
    DeblurConfig,
    estimate_blur_kernel,
    measure_edge_spread_width,
    render_deblurred_video,
    restore_deblurred_frame,
)
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueInputError,
    RescueMediaError,
)
from videoscope.rescue.executor import CommandResult, run_external_command


@dataclass
class _ProbeRunner:
    payload: dict[str, object]
    commands: list[tuple[str, ...]]

    def __call__(self, arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        self.commands.append(arguments)
        if "-show_frames" in arguments:
            streams = cast(list[dict[str, object]], self.payload.get("streams", []))
            video = next(
                (item for item in streams if item.get("codec_type") == "video"), None
            )
            if video is None:
                return CommandResult(0, "", "stream|0\n")
            numerator, denominator = str(video["avg_frame_rate"]).split("/", 1)
            fps = float(numerator) / float(denominator)
            return CommandResult(
                0,
                "",
                _compact_frame_probe_output(
                    frame_count=int(str(video["nb_frames"])), fps=fps
                ),
            )
        return CommandResult(0, "", json.dumps(self.payload))


class _StreamingCapture:
    def __init__(
        self,
        *,
        frame_count: int = 10,
        fail_at: int | None = None,
    ) -> None:
        self.frame_count = frame_count
        self.fail_at = fail_at
        self.index = 0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def get(self, property_id: int) -> float:
        return {
            cv2.CAP_PROP_FPS: 10.0,
            cv2.CAP_PROP_FRAME_WIDTH: 64.0,
            cv2.CAP_PROP_FRAME_HEIGHT: 48.0,
        }.get(property_id, 0.0)

    def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
        if self.fail_at is not None and self.index == self.fail_at:
            raise RuntimeError("injected decoder read failure")
        if self.index >= self.frame_count:
            return False, None
        self.index += 1
        return True, np.full((48, 64, 3), self.index, dtype=np.uint8)

    def release(self) -> None:
        self.released = True


class _StreamingWriter:
    def __init__(self, *, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.writes = 0
        self.released = False

    def isOpened(self) -> bool:
        return True

    def write(self, _frame: NDArray[np.uint8]) -> None:
        if self.fail_at is not None and self.writes == self.fail_at:
            raise RuntimeError("injected writer failure")
        self.writes += 1

    def release(self) -> None:
        self.released = True


def _install_streaming_codec(
    monkeypatch: pytest.MonkeyPatch,
    *,
    frame_count: int = 10,
    capture_fail_at: int | None = None,
    writer_fail_at: int | None = None,
) -> tuple[_StreamingCapture, _StreamingWriter]:
    capture = _StreamingCapture(frame_count=frame_count, fail_at=capture_fail_at)
    writer = _StreamingWriter(fail_at=writer_fail_at)
    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: capture)
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args: writer)
    return capture, writer


def _successful_renderer_runner(
    commands: list[tuple[str, ...]],
    *,
    candidate_probe_result: CommandResult | None = None,
) -> Callable[..., CommandResult]:
    media_probe_calls = 0

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal media_probe_calls
        commands.append(arguments)
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(0, "", _compact_frame_probe_output())
            media_probe_calls += 1
            if media_probe_calls == 2 and candidate_probe_result is not None:
                return candidate_probe_result
            return CommandResult(0, "", json.dumps(_probe_payload()))
        if "null" in arguments:
            return CommandResult(0, "", "")
        Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    return runner


def _estimate_fixture() -> BlurKernelEstimate:
    return BlurKernelEstimate(
        kernel_kind="box",
        radius=2,
        regularization=0.003,
        confidence=0.8,
        edge_width_before=4.0,
        predicted_edge_width_after=2.0,
        edge_continuity_ratio=0.9,
        reblur_error_ratio=0.02,
        ringing_ratio=0.01,
        noise_gain_ratio=1.2,
        temporal_change_ratio=0.01,
    )


def _probe_payload(
    *, duration: float = 1.0, fps: int = 10, start_time: float = 0.0
) -> dict[str, object]:
    return {
        "format": {"duration": str(duration)},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 64,
                "height": 48,
                "avg_frame_rate": f"{fps}/1",
                "r_frame_rate": f"{fps}/1",
                "nb_frames": str(round(duration * fps)),
                "start_time": str(start_time),
            }
        ],
    }


def _audio_stream(
    *,
    codec_name: str = "aac",
    sample_rate: str = "48000",
    channels: int = 2,
    channel_layout: str = "stereo",
) -> dict[str, object]:
    return {
        "codec_type": "audio",
        "codec_name": codec_name,
        "sample_rate": sample_rate,
        "channels": channels,
        "channel_layout": channel_layout,
    }


def _frame_probe_payload(
    *, frame_count: int = 10, fps: float = 10
) -> dict[str, object]:
    return {
        "frames": [
            {
                "best_effort_timestamp_time": str(index / fps),
                "pkt_duration_time": str(1 / fps),
            }
            for index in range(frame_count)
        ]
    }


def _compact_frame_probe_output(
    *,
    frame_count: int = 10,
    fps: float = 10,
    reported_count: int | None = None,
    terminal_newline: bool = True,
) -> str:
    frames = [
        {
            "best_effort_timestamp_time": f"{index / fps:.9f}",
        }
        for index in range(frame_count)
    ]
    return _compact_frame_probe_from_frames(
        frames,
        reported_count=reported_count,
        terminal_newline=terminal_newline,
    )


def _compact_frame_probe_from_frames(
    frames: Sequence[Mapping[str, object]],
    *,
    reported_count: int | None = None,
    terminal_newline: bool = True,
) -> str:
    lines: list[str] = []
    for frame in frames:
        fields = ["frame"]
        if "best_effort_timestamp_time" in frame:
            fields.append(str(frame["best_effort_timestamp_time"]))
        lines.append("|".join(fields))
    lines.append(f"stream|{len(frames) if reported_count is None else reported_count}")
    output = "\n".join(lines)
    return output + ("\n" if terminal_newline else "")


def test_cfr_timing_probe_accepts_compact_long_actual_pts_inventory() -> None:
    """The 42 s/24 fps case must fit below the immutable 64 KiB runner cap."""
    frame_count = 1008
    fps = 24.0
    old_json = json.dumps(
        _frame_probe_payload(frame_count=frame_count, fps=fps), indent=2
    )
    compact = _compact_frame_probe_output(frame_count=frame_count, fps=fps)
    assert len(old_json.encode("utf-8")) > 64 * 1024
    assert len(compact.encode("utf-8")) < 60 * 1024
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        return CommandResult(0, "", compact)

    timestamps = deblur_module._probe_and_validate_cfr_timing(
        Path("中文 space") / "视频.mp4",
        ffprobe_path=Path("ffprobe"),
        runner=cast(Any, runner),
        cancellation_callback=lambda: False,
        fps=fps,
        expected_frames=frame_count,
        stream_origin_seconds=0.0,
    )

    assert len(timestamps) == frame_count
    assert timestamps[0] == 0.0
    assert timestamps[-1] == pytest.approx((frame_count - 1) / fps)
    command = calls[0]
    assert "-count_frames" in command
    assert "-show_streams" in command
    assert (
        "frame=best_effort_timestamp_time:stream=nb_read_frames:"
        "frame_side_data=:stream_tags=:stream_disposition=:stream_side_data=" in command
    )
    assert "compact=p=1:nk=1" in command


def test_cfr_timing_inventory_without_optional_durations_is_deterministic() -> None:
    output = _compact_frame_probe_output()

    first = deblur_module._parse_cfr_timing_inventory(output, expected_frames=10)
    second = deblur_module._parse_cfr_timing_inventory(output, expected_frames=10)

    assert first == second
    assert first == tuple(index / 10 for index in range(10))


def test_cfr_timing_inventory_accepts_explicit_empty_frame_side_data_marker() -> None:
    output = _compact_frame_probe_output().replace(
        "frame|0.000000000", "frame|0.000000000|", 1
    )

    assert deblur_module._parse_cfr_timing_inventory(
        output, expected_frames=10
    ) == tuple(index / 10 for index in range(10))


@pytest.mark.parametrize(
    ("output", "expected_frames"),
    [
        ("", 10),
        (_compact_frame_probe_output(terminal_newline=False), 10),
        ("\n".join(_compact_frame_probe_output().splitlines()[:-1]) + "\n", 10),
        (_compact_frame_probe_output(reported_count=9), 10),
        (_compact_frame_probe_output(reported_count=10), 9),
        (_compact_frame_probe_output().replace("frame|", "packet|", 1), 10),
        (_compact_frame_probe_output().replace("frame|", "frame|extra|", 1), 10),
        (_compact_frame_probe_output().replace("frame|0.000000000", "frame", 1), 10),
        (
            _compact_frame_probe_output().replace("frame|0.100000000", "frame|0.0", 1),
            10,
        ),
        (
            _compact_frame_probe_output().replace("frame|0.100000000", "frame|-0.1", 1),
            10,
        ),
        (
            _compact_frame_probe_output().replace("frame|0.100000000", "frame|nan", 1),
            10,
        ),
        (
            _compact_frame_probe_output().replace(
                "frame|0.000000000", "frame|0.000000000|0.100000000", 1
            ),
            10,
        ),
        (_compact_frame_probe_output().replace("stream|10", "stream|many"), 10),
        ("x" * (60 * 1024 + 1) + "\n", 10),
        ("frame|0\n" * 4097 + "stream|4097\n", 4097),
    ],
    ids=(
        "empty",
        "truncated-mid-record",
        "missing-footer",
        "footer-count-mismatch",
        "expected-count-mismatch",
        "bad-record-type",
        "extra-field",
        "missing-timestamp",
        "duplicate-timestamp",
        "negative-timestamp",
        "nonfinite-timestamp",
        "unexpected-nonempty-field",
        "bad-footer-count",
        "byte-overflow",
        "frame-inventory-overflow",
    ),
)
def test_cfr_timing_inventory_rejects_incomplete_or_unsafe_output(
    output: str, expected_frames: int
) -> None:
    with pytest.raises(RescueMediaError):
        deblur_module._parse_cfr_timing_inventory(
            output, expected_frames=expected_frames
        )


@pytest.mark.parametrize(
    "timestamps",
    [
        (0.0, 0.1, 0.2, 0.35, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9),
        tuple(index * 0.1018 for index in range(10)),
    ],
    ids=("variable-cadence", "incomplete-terminal-coverage"),
)
def test_cfr_timing_probe_rejects_vfr_or_incomplete_terminal_coverage(
    timestamps: tuple[float, ...],
) -> None:
    frames = [
        {
            "best_effort_timestamp_time": value,
            "pkt_duration_time": 0.1,
        }
        for value in timestamps
    ]

    def runner(_arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(0, "", _compact_frame_probe_from_frames(frames))

    with pytest.raises(RescueMediaError):
        deblur_module._probe_and_validate_cfr_timing(
            Path("video.mp4"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
            cancellation_callback=lambda: False,
            fps=10.0,
            expected_frames=10,
            stream_origin_seconds=0.0,
        )


def test_cfr_timing_probe_returns_actual_pts_normalized_from_stream_origin() -> None:
    origin = 0.083008
    frames = [
        {"best_effort_timestamp_time": origin + index / 10} for index in range(10)
    ]

    def runner(_arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(0, "", _compact_frame_probe_from_frames(frames))

    timestamps = deblur_module._probe_and_validate_cfr_timing(
        Path("staged faithful.mp4"),
        ffprobe_path=Path("ffprobe"),
        runner=cast(Any, runner),
        cancellation_callback=lambda: False,
        fps=10.0,
        expected_frames=10,
        stream_origin_seconds=origin,
    )

    assert timestamps == pytest.approx(tuple(index / 10 for index in range(10)))


@pytest.mark.parametrize("stream_origin", (-0.1, float("nan"), 0.05))
def test_cfr_timing_probe_rejects_invalid_or_mismatched_stream_origin(
    stream_origin: float,
) -> None:
    def runner(_arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        return CommandResult(0, "", _compact_frame_probe_output())

    with pytest.raises(RescueMediaError):
        deblur_module._probe_and_validate_cfr_timing(
            Path("staged faithful.mp4"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
            cancellation_callback=lambda: False,
            fps=10.0,
            expected_frames=10,
            stream_origin_seconds=stream_origin,
        )


@pytest.mark.parametrize("start_time", (None, "nan", "-0.1"))
def test_video_stream_start_probe_rejects_missing_or_invalid_origin(
    start_time: str | None,
) -> None:
    payload = _probe_payload()
    stream = cast(dict[str, object], cast(list[object], payload["streams"])[0])
    if start_time is None:
        del stream["start_time"]
    else:
        stream["start_time"] = start_time

    with pytest.raises(RescueMediaError):
        deblur_module._video_stream_start_seconds(payload)


def test_video_renderer_selects_half_open_range_from_actual_offset_pts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid 1 ms origin offset must select frames 0 and 1, not only ordinal 1."""
    source = tmp_path / "offset source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "offset output.mp4"
    _capture, _writer = _install_streaming_codec(monkeypatch)
    actual_frames = [
        {"best_effort_timestamp_time": 0.001 + index / 10} for index in range(10)
    ]
    restored_indices: list[int] = []

    def restore(
        frame: NDArray[np.generic],
        estimate: BlurKernelEstimate,
        config: DeblurConfig,
    ) -> NDArray[np.uint8]:
        del estimate, config
        restored_indices.append(int(frame[0, 0, 0]) - 1)
        return np.asarray(frame, dtype=np.uint8).copy()

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(
                    0, "", _compact_frame_probe_from_frames(actual_frames)
                )
            return CommandResult(0, "", json.dumps(_probe_payload()))
        return CommandResult(1, "expected stop after source render", "")

    monkeypatch.setattr("videoscope.rescue.deblur.restore_deblurred_frame", restore)
    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            output,
            ((0.0005, 0.1015),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert restored_indices == [0, 1]
    assert source.read_bytes() == b"source"
    assert not output.exists()


def test_video_renderer_uses_actual_pts_for_adjacent_ranges_and_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "adjacent source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "adjacent output.mp4"
    _capture, _writer = _install_streaming_codec(monkeypatch)
    actual_timestamps = tuple(0.001 + index / 10 for index in range(10))
    actual_frames = [
        {"best_effort_timestamp_time": timestamp} for timestamp in actual_timestamps
    ]
    weighted: list[tuple[float, float, float]] = []
    original_boundary_weight = deblur_module._boundary_weight

    def boundary_weight(
        timestamp: float, start: float, end: float, fade: float
    ) -> float:
        weighted.append((timestamp, start, end))
        return original_boundary_weight(timestamp, start, end, fade)

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(
                    0, "", _compact_frame_probe_from_frames(actual_frames)
                )
            return CommandResult(0, "", json.dumps(_probe_payload()))
        return CommandResult(1, "expected stop after source render", "")

    monkeypatch.setattr(deblur_module, "_boundary_weight", boundary_weight)
    monkeypatch.setattr(
        deblur_module,
        "restore_deblurred_frame",
        lambda frame, _estimate, _config: np.asarray(frame, dtype=np.uint8).copy(),
    )
    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            output,
            ((0.0005, 0.101), (0.101, 0.2015)),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert weighted == [
        (actual_timestamps[0], 0.0005, 0.101),
        (actual_timestamps[1], 0.101, 0.2015),
        (actual_timestamps[2], 0.101, 0.2015),
    ]
    assert source.read_bytes() == b"source"
    assert not output.exists()


def test_video_renderer_normalizes_valid_nonzero_stream_origin_for_ranges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan-relative ranges use actual PTS measured from the stream origin."""
    source = tmp_path / "nonzero origin source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "nonzero origin output.mp4"
    _capture, _writer = _install_streaming_codec(monkeypatch)
    origin = 0.083
    actual_frames = [
        {"best_effort_timestamp_time": origin + index / 10} for index in range(10)
    ]
    restored_indices: list[int] = []

    def restore(
        frame: NDArray[np.generic],
        estimate: BlurKernelEstimate,
        config: DeblurConfig,
    ) -> NDArray[np.uint8]:
        del estimate, config
        restored_indices.append(int(frame[0, 0, 0]) - 1)
        return np.asarray(frame, dtype=np.uint8).copy()

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(
                    0, "", _compact_frame_probe_from_frames(actual_frames)
                )
            return CommandResult(0, "", json.dumps(_probe_payload(start_time=origin)))
        return CommandResult(1, "expected stop after source render", "")

    monkeypatch.setattr(deblur_module, "restore_deblurred_frame", restore)
    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            output,
            ((0.05, 0.15),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert restored_indices == [1]
    assert source.read_bytes() == b"source"
    assert not output.exists()


def _pattern(seed: int, *, height: int = 192, width: int = 320) -> NDArray[np.uint8]:
    rng = np.random.default_rng(seed)
    image: NDArray[np.uint8] = np.full((height, width), 24, dtype=np.uint8)
    for x in range(14, width, 24):
        cv2.line(image, (x, 8), (x, height - 9), 225, 1, cv2.LINE_8)
    for y in range(18, height, 28):
        cv2.line(image, (8, y), (width - 9, y), 180, 1, cv2.LINE_8)
    for _ in range(12):
        x = int(rng.integers(12, width - 62))
        y = int(rng.integers(20, height - 10))
        cv2.putText(
            image,
            chr(65 + int(rng.integers(0, 26))),
            (x, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            255,
            1,
            cv2.LINE_8,
        )
    return image


def _offset_rectangles(
    seed: int, *, height: int = 192, width: int = 320
) -> NDArray[np.uint8]:
    rng = np.random.default_rng(seed)
    image: NDArray[np.uint8] = np.full((height, width), 32, dtype=np.uint8)
    for index in range(10):
        left = 10 + index * 29
        top = 12 + (index % 4) * 38
        cv2.rectangle(image, (left, top), (left + 17, top + 23), 210, 1, cv2.LINE_8)
    for _ in range(18):
        x = int(rng.integers(8, width - 18))
        y = int(rng.integers(8, height - 18))
        cv2.circle(image, (x, y), 4, 245, 1, cv2.LINE_8)
    return image


def _perforated_tiles(
    seed: int, *, height: int = 192, width: int = 320
) -> NDArray[np.uint8]:
    image: NDArray[np.uint8] = np.full((height, width), 32, dtype=np.uint8)
    for top in range(10, height, 18):
        for left in range(10, width, 24):
            value = 210 if ((left + top + seed) // 10) % 2 else 255
            cv2.rectangle(
                image, (left, top), (left + 8, top + 5), value, -1, cv2.LINE_8
            )
            cv2.circle(image, (left + 4, top + 2), 2, 32, -1, cv2.LINE_8)
    return image


def _compressed_text_pattern() -> NDArray[np.uint8]:
    """Render a path-free low-light text card with bounded codec artifacts."""
    image: NDArray[np.uint8] = np.full((180, 320), 18, dtype=np.uint8)
    for x in range(0, image.shape[1], 20):
        cv2.line(image, (x, 0), (x, image.shape[0] - 1), 32, 1, cv2.LINE_8)
    for y in range(0, image.shape[0], 20):
        cv2.line(image, (0, y), (image.shape[1] - 1, y), 32, 1, cv2.LINE_8)
    cv2.putText(
        image,
        "OBSERVE THE SIGNAL.",
        (10, 60),
        cv2.FONT_HERSHEY_DUPLEX,
        0.70,
        235,
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        "LOCAL CPU / PRIVATE",
        (10, 100),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        190,
        1,
        cv2.LINE_AA,
    )
    cv2.putText(
        image,
        "THREE LOCAL STEPS",
        (10, 138),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        210,
        1,
        cv2.LINE_AA,
    )
    blurred = cv2.GaussianBlur(image, (5, 5), 0, borderType=cv2.BORDER_REFLECT_101)
    encoded, payload = cv2.imencode(".jpg", blurred, [cv2.IMWRITE_JPEG_QUALITY, 35])
    assert encoded
    decoded = cv2.imdecode(payload, cv2.IMREAD_GRAYSCALE)
    assert decoded is not None
    return decoded


def _blur(image: NDArray[np.uint8], kernel_kind: str, radius: int) -> NDArray[np.uint8]:
    size = radius * 2 + 1
    if kernel_kind == "box":
        return cv2.blur(image, (size, size), borderType=cv2.BORDER_REFLECT_101)
    return cv2.GaussianBlur(image, (size, size), 0, borderType=cv2.BORDER_REFLECT_101)


def _ssim(left: NDArray[np.uint8], right: NDArray[np.uint8]) -> float:
    x = left.astype(np.float64)
    y = right.astype(np.float64)
    c1 = 6.5025
    c2 = 58.5225
    mean_x = float(np.mean(x))
    mean_y = float(np.mean(y))
    var_x = float(np.var(x))
    var_y = float(np.var(y))
    covariance = float(np.mean((x - mean_x) * (y - mean_y)))
    return ((2 * mean_x * mean_y + c1) * (2 * covariance + c2)) / (
        (mean_x * mean_x + mean_y * mean_y + c1) * (var_x + var_y + c2)
    )


def _estimate(
    kind: str,
    radius: int,
    seed: int = 2,
    pattern: Callable[[int], NDArray[np.uint8]] = _pattern,
) -> tuple[NDArray[np.uint8], NDArray[np.uint8], BlurKernelEstimate]:
    clean = pattern(seed)
    blurred = _blur(clean, kind, radius)
    estimate = estimate_blur_kernel((blurred, blurred.copy()), DeblurConfig())
    assert estimate is not None
    return clean, blurred, estimate


def _isolate_legacy_candidate_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep candidate-search tests independent from decoded-observable policy."""
    monkeypatch.setattr(
        deblur_module,
        "_decoded_observable_candidate_metrics",
        lambda *_args, **_kwargs: {
            "edge_width_ratio": 0.5,
            "edge_continuity_ratio": 1.0,
            "ringing_ratio": 0.0,
            "noise_gain_ratio": 1.0,
            "temporal_change_ratio": 0.0,
        },
    )


def test_video_renderer_rejects_a_missing_source(tmp_path: Path) -> None:
    """Catches a renderer that starts media work before validating its source."""
    estimate = _estimate_fixture()

    with pytest.raises(RescueInputError):
        render_deblurred_video(
            tmp_path / "missing.mp4",
            tmp_path / "output.mp4",
            ((0.0, 1.0),),
            estimate,
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
        )


def test_video_renderer_rejects_unconfirmed_ringing_suppression_strength(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands: list[tuple[str, ...]] = []
    estimate = _estimate_fixture().model_copy(
        update={"ringing_suppression_strength": 0.5}
    )

    with pytest.raises(RescueInputError):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.0, 1.0),),
            estimate,
            DeblurConfig(candidate_ringing_suppression_strengths=(0.0, 1.0)),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), commands)),
        )

    assert commands == []


def test_video_renderer_rejects_existing_non_file_source(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.mkdir()
    output = tmp_path / "output.mp4"

    with pytest.raises(RescueInputError):
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
        )

    assert source.is_dir()
    assert not output.exists()


def test_video_renderer_rejects_symlink_and_hardlink_source_aliases(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    aliases: list[Path] = []
    symlink = tmp_path / "symlink-output.mp4"
    try:
        symlink.symlink_to(source)
    except OSError:
        pass
    else:
        aliases.append(symlink)
    hardlink = tmp_path / "hardlink-output.mp4"
    try:
        os.link(source, hardlink)
    except OSError:
        pass
    else:
        aliases.append(hardlink)
    if not aliases:
        pytest.skip("filesystem does not support source aliases")

    for alias in aliases:
        with pytest.raises(RescueArtifactError):
            render_deblurred_video(
                source,
                alias,
                ((0.1, 0.5),),
                _estimate_fixture(),
                DeblurConfig(),
                ffmpeg_path=Path("ffmpeg"),
                ffprobe_path=Path("ffprobe"),
                runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
            )
        assert source.read_bytes() == b"source"
        assert alias.read_bytes() == b"source"


@pytest.mark.parametrize("missing_tool", ["ffmpeg", "ffprobe"])
def test_video_renderer_missing_media_tool_leaves_source_and_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_tool: str
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    calls: list[str] = []
    if missing_tool == "ffmpeg":
        _install_streaming_codec(monkeypatch)

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        executable = Path(arguments[0]).name
        calls.append(executable)
        if executable == missing_tool:
            raise RescueMediaError("required media executable was not found")
        if "-show_frames" in arguments:
            return CommandResult(0, "", _compact_frame_probe_output())
        return CommandResult(0, "", json.dumps(_probe_payload()))

    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]
    assert missing_tool in calls
    if missing_tool == "ffmpeg":
        assert calls[:2] == ["ffprobe", "ffprobe"]


@pytest.mark.parametrize(
    "ranges",
    [
        (),
        ((1.0, 0.0),),
        ((float("nan"), 1.0),),
        ((0.0, float("inf")),),
        ((0.5, 0.9), (0.1, 0.4)),
        ((0.1, 0.6), (0.5, 0.9)),
        ((0.01, 0.09),),
        ((0.0, 1.1),),
    ],
)
def test_video_renderer_rejects_unsafe_ranges_before_decode(
    tmp_path: Path, ranges: tuple[tuple[float, float], ...]
) -> None:
    """Catches reversed, nonfinite, unordered, overlapping or frame-empty ranges."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"probe-only")
    runner = _ProbeRunner(_probe_payload(), [])

    with pytest.raises(RescueInputError):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ranges,
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert not (tmp_path / "output.mp4").exists()


@pytest.mark.parametrize(
    "frames",
    [
        [],
        [{}],
        [
            {
                "best_effort_timestamp_time": str(value),
                "pkt_duration_time": "0.1",
            }
            for value in (0.0, 0.1, 0.1, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
        ],
        [
            {
                "best_effort_timestamp_time": str(value),
                "pkt_duration_time": "0.1",
            }
            for value in (0.0, 0.1, 0.2, 0.35, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
        ],
        [
            {
                "best_effort_timestamp_time": "nan" if index == 4 else str(index / 10),
                "pkt_duration_time": "0.1",
            }
            for index in range(10)
        ],
        [
            {
                "best_effort_timestamp_time": str(0.05 + index / 10),
                "pkt_duration_time": "0.1",
            }
            for index in range(10)
        ],
    ],
)
def test_video_renderer_rejects_unproven_cfr_before_opening_decoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    frames: list[dict[str, str]],
) -> None:
    """Catches aggregate fps fields accepting missing or nonuniform frame PTS."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    probe_calls = 0

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal probe_calls
        probe_calls += 1
        if "-show_frames" in arguments:
            return CommandResult(0, "", _compact_frame_probe_from_frames(frames))
        return CommandResult(0, "", json.dumps(_probe_payload()))

    monkeypatch.setattr(
        cv2,
        "VideoCapture",
        lambda _path: pytest.fail("decoder opened before CFR was proven"),
    )
    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert probe_calls >= 1
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_accepts_uniform_timestamps_without_optional_packet_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real CFR streams can omit per-packet duration while retaining exact PTS."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    frames = [{"best_effort_timestamp_time": str(index / 10)} for index in range(10)]

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(
                    0, "", _compact_frame_probe_from_frames(cast(Any, frames))
                )
            return CommandResult(0, "", json.dumps(_probe_payload()))
        return CommandResult(1, "expected stop after CFR proof", "")

    monkeypatch.setattr(
        cv2,
        "VideoCapture",
        lambda _path: pytest.fail("CFR proof passed and decoder was opened"),
    )
    with pytest.raises(pytest.fail.Exception, match="decoder was opened"):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )


def test_video_renderer_rejects_alias_and_existing_destination(tmp_path: Path) -> None:
    """Catches overwriting either the source identity or an existing artifact."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    runner = _ProbeRunner(_probe_payload(), [])
    kwargs = {
        "ffmpeg_path": Path("ffmpeg"),
        "ffprobe_path": Path("ffprobe"),
        "runner": cast(Any, runner),
    }

    with pytest.raises(RescueArtifactError):
        render_deblurred_video(
            source, source, ((0.1, 0.5),), _estimate_fixture(), DeblurConfig(), **kwargs
        )
    existing = tmp_path / "existing.mp4"
    existing.write_bytes(b"keep")
    with pytest.raises(RescueArtifactError):
        render_deblurred_video(
            source,
            existing,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            **kwargs,
        )
    assert existing.read_bytes() == b"keep"


def test_video_renderer_cancellation_before_decode_leaves_no_partial(
    tmp_path: Path,
) -> None:
    """Catches cancellation that creates output-local residue before processing."""
    source = tmp_path / "源 视频.mp4"
    source.write_bytes(b"source")

    with pytest.raises(RescueCancelledError):
        render_deblurred_video(
            source,
            tmp_path / "恢复 输出.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
            cancellation_callback=lambda: True,
        )

    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


@pytest.mark.parametrize(
    ("payload", "returncode"),
    [({}, 1), ({"not": "json serializable contract"}, 0)],
)
def test_video_renderer_probe_failure_leaves_no_partial(
    tmp_path: Path, payload: dict[str, object], returncode: int
) -> None:
    """Catches probe failure being treated as a valid zero-finding media result."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        del arguments
        return CommandResult(returncode, "sanitized probe failure", json.dumps(payload))

    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_callback_exception_leaves_no_partial(tmp_path: Path) -> None:
    """Catches callback exceptions being swallowed after creating task-owned paths."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    def cancellation_callback() -> bool:
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
            cancellation_callback=cancellation_callback,
        )
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_converts_callback_exception_to_cancellation_for_runner(
    tmp_path: Path,
) -> None:
    """The child runner must see cancellation before the callback error is re-raised."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    callback_calls = 0

    def cancellation_callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls == 1:
            return False
        raise RuntimeError("callback failed after child start")

    def runner(arguments: tuple[str, ...], **kwargs: object) -> CommandResult:
        del arguments
        child_callback = cast(Callable[[], bool], kwargs["cancellation_callback"])
        try:
            assert child_callback() is True
        except RuntimeError:
            pytest.fail("raw callback exception escaped into the child runner")
        raise RescueCancelledError("child stopped")

    with pytest.raises(RuntimeError, match="callback failed after child start"):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
            cancellation_callback=cancellation_callback,
        )

    assert callback_calls == 2
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_reraises_callback_exception_during_decode_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A callback failure during frame streaming must not be mislabeled cancellation."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    callback_calls = 0

    def cancellation_callback() -> bool:
        nonlocal callback_calls
        callback_calls += 1
        if callback_calls >= 5:
            raise RuntimeError("callback failed during decode")
        return False

    class Capture:
        index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, property_id: int) -> float:
            return {
                cv2.CAP_PROP_FPS: 10.0,
                cv2.CAP_PROP_FRAME_WIDTH: 64.0,
                cv2.CAP_PROP_FRAME_HEIGHT: 48.0,
            }.get(property_id, 0.0)

        def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

        def release(self) -> None:
            return None

    class Writer:
        def isOpened(self) -> bool:
            return True

        def write(self, _frame: NDArray[np.uint8]) -> None:
            return None

        def release(self) -> None:
            return None

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: Capture())
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args: Writer())
    with pytest.raises(RuntimeError, match="callback failed during decode"):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
            cancellation_callback=cancellation_callback,
        )

    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_ordinary_cancellation_during_decode_cleans_every_owned_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    capture, writer = _install_streaming_codec(monkeypatch)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 5

    with pytest.raises(RescueCancelledError):
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
            cancellation_callback=cancelled,
        )

    assert 0 < capture.index < 10
    assert capture.released and writer.released
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_early_decoder_eof_is_not_accepted_as_normal_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    capture, writer = _install_streaming_codec(monkeypatch, frame_count=6)

    with pytest.raises(RescueMediaError) as caught:
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
        )

    assert (
        caught.value.internal_message
        == "source decode ended before the probed frame count"
    )
    assert writer.writes == 6
    assert capture.released and writer.released
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_writer_exception_becomes_path_safe_media_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    capture, writer = _install_streaming_codec(monkeypatch, writer_fail_at=3)

    with pytest.raises(RescueMediaError) as caught:
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
        )

    assert caught.value.internal_message == "deblur lossless writer failed"
    assert capture.released and writer.released
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_decoder_exception_becomes_path_safe_media_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    capture, writer = _install_streaming_codec(monkeypatch, capture_fail_at=3)

    with pytest.raises(RescueMediaError) as caught:
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
        )

    assert caught.value.internal_message == "source frame decode failed"
    assert capture.released and writer.released
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_runner_exception_after_temp_ownership_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    capture, writer = _install_streaming_codec(monkeypatch)

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(0, "", _compact_frame_probe_output())
            return CommandResult(0, "", json.dumps(_probe_payload()))
        raise RuntimeError(f"runner leaked path {source}")

    with pytest.raises(RescueMediaError) as caught:
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert caught.value.internal_message == "deblur media command failed"
    assert str(source) not in str(caught.value)
    assert capture.released and writer.released
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_runner_timeout_after_temp_ownership_cleans_owned_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    capture, writer = _install_streaming_codec(monkeypatch)
    probe_calls = 0

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal probe_calls
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(0, "", _compact_frame_probe_output())
            probe_calls += 1
            return CommandResult(0, "", json.dumps(_probe_payload()))
        raise RescueMediaError("media command timed out")

    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert probe_calls == 1
    assert capture.released and writer.released
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


@pytest.mark.parametrize(
    "candidate_probe_result",
    [
        CommandResult(9, "sanitized candidate probe failure", ""),
        CommandResult(0, "", "{not-json"),
    ],
)
def test_video_renderer_candidate_probe_failure_after_creation_cleans_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    candidate_probe_result: CommandResult,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    _install_streaming_codec(monkeypatch)
    commands: list[tuple[str, ...]] = []
    runner = _successful_renderer_runner(
        commands, candidate_probe_result=candidate_probe_result
    )

    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert any(
        Path(command[-1]).name.startswith(f".{output.name}.deblur-")
        for command in commands
        if command[0] == "ffmpeg"
    )
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_uses_exact_argv_tuples_without_shell_or_path_interpolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media_root = tmp_path / "中文 space ; & $()"
    media_root.mkdir()
    source = media_root / "源 ; video.mp4"
    source.write_bytes(b"source")
    output = media_root / "输出 & result.mp4"
    _install_streaming_codec(monkeypatch)
    commands: list[tuple[str, ...]] = []
    observed_kwargs: list[dict[str, object]] = []
    delegate = _successful_renderer_runner(commands)

    def runner(arguments: tuple[str, ...], **kwargs: object) -> CommandResult:
        assert isinstance(arguments, tuple)
        assert all(isinstance(argument, str) for argument in arguments)
        observed_kwargs.append(kwargs)
        return delegate(arguments, **kwargs)

    render_deblurred_video(
        source,
        output,
        ((0.1, 0.5),),
        _estimate_fixture(),
        DeblurConfig(),
        ffmpeg_path=Path("ffmpeg"),
        ffprobe_path=Path("ffprobe"),
        runner=cast(Any, runner),
    )

    assert output.exists() and source.exists()
    assert len(commands) == 6
    assert commands[0] == (
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(source),
    )
    assert commands[1][-1] == str(source)
    mux = commands[2]
    assert mux[0:9] == (
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-i",
        mux[7],
        "-i",
    )
    assert mux[9] == str(source)
    assert mux[mux.index("-c:v:0") + 1] == "libx264"
    assert mux[mux.index("-profile:v:0") + 1] == "high"
    assert mux[mux.index("-level:v:0") + 1] == "3.1"
    assert mux[mux.index("-pix_fmt:v:0") + 1] == "yuv420p"
    assert mux[mux.index("-r:v:0") + 1] == "10/1"
    assert mux[mux.index("-fps_mode:v:0") + 1] == "cfr"
    assert mux[mux.index("-video_track_timescale") + 1] == "120000"
    assert mux[-1] != str(output)
    assert commands[3][-1] == mux[-1]
    assert commands[4][-1] == mux[-1]
    assert commands[5][0] == "ffmpeg" and commands[5][-1] == "-"
    assert all("shell" not in kwargs for kwargs in observed_kwargs)
    assert all(
        set(kwargs) == {"timeout_seconds", "sensitive_paths", "cancellation_callback"}
        for kwargs in observed_kwargs
    )


@pytest.mark.parametrize(
    ("mutate", "expected_message"),
    [
        (
            lambda payload: cast(list[object], payload["streams"]).clear(),
            "deblur requires exactly one video stream",
        ),
        (
            lambda payload: cast(
                dict[str, object], cast(list[object], payload["streams"])[0]
            ).update(avg_frame_rate="0/1"),
            "source frame rate is unsupported",
        ),
        (
            lambda payload: cast(
                dict[str, object], cast(list[object], payload["streams"])[0]
            ).update(r_frame_rate="30000/1001"),
            "deblur requires positive CFR source metadata",
        ),
        (
            lambda payload: cast(
                dict[str, object], cast(list[object], payload["streams"])[0]
            ).update(avg_frame_rate="ten/one"),
            "source frame rate is unsupported",
        ),
        (
            lambda payload: cast(
                dict[str, object], cast(list[object], payload["streams"])[0]
            ).update(avg_frame_rate="10"),
            "source frame rate is unsupported",
        ),
    ],
)
def test_video_renderer_rejects_missing_video_and_unsupported_fps_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[dict[str, object]], object],
    expected_message: str,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "output.mp4"
    payload = _probe_payload()
    mutate(payload)
    monkeypatch.setattr(
        cv2,
        "VideoCapture",
        lambda _path: pytest.fail("decoder opened for unsupported media"),
    )

    with pytest.raises(RescueMediaError) as caught:
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(payload, [])),
        )

    assert caught.value.internal_message == expected_message
    assert source.read_bytes() == b"source"
    assert not output.exists()
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_streams_one_frame_at_a_time_and_cleans_mux_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches retaining restored frames and publishing a failed mux candidate."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    payload = _probe_payload(duration=20.0, fps=10)
    restored_live = 0
    maximum_live = 0

    class Capture:
        index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, property_id: int) -> float:
            if property_id == cv2.CAP_PROP_FPS:
                return 10.0
            if property_id == cv2.CAP_PROP_FRAME_WIDTH:
                return 64.0
            if property_id == cv2.CAP_PROP_FRAME_HEIGHT:
                return 48.0
            return 0.0

        def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
            if self.index >= 200:
                return False, None
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

        def release(self) -> None:
            return None

    class Writer:
        def isOpened(self) -> bool:
            return True

        def write(self, frame: NDArray[np.uint8]) -> None:
            nonlocal restored_live
            assert frame.shape == (48, 64, 3)
            restored_live = 0

        def release(self) -> None:
            return None

    def restore(
        frame: NDArray[np.generic],
        estimate: BlurKernelEstimate,
        config: DeblurConfig,
    ) -> NDArray[np.uint8]:
        nonlocal restored_live, maximum_live
        del estimate, config
        restored_live += 1
        maximum_live = max(maximum_live, restored_live)
        return np.asarray(frame, dtype=np.uint8).copy()

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(
                    0,
                    "",
                    _compact_frame_probe_output(frame_count=200, fps=10),
                )
            return CommandResult(0, "", json.dumps(payload))
        return CommandResult(7, "sanitized mux failure", "")

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: Capture())
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args: Writer())
    monkeypatch.setattr("videoscope.rescue.deblur.restore_deblurred_frame", restore)
    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.0, 20.0),),
            _estimate_fixture(),
            DeblurConfig(boundary_transition_seconds=0.1),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert maximum_live == 1
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_rejects_extra_decoded_frames_before_writing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a decode that writes frames beyond the probed CFR inventory."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    written_frames = 0

    class Capture:
        index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, property_id: int) -> float:
            return {
                cv2.CAP_PROP_FPS: 10.0,
                cv2.CAP_PROP_FRAME_WIDTH: 64.0,
                cv2.CAP_PROP_FRAME_HEIGHT: 48.0,
            }.get(property_id, 0.0)

        def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
            if self.index >= 11:
                return False, None
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

        def release(self) -> None:
            return None

    class Writer:
        def isOpened(self) -> bool:
            return True

        def write(self, frame: NDArray[np.uint8]) -> None:
            nonlocal written_frames
            assert frame.shape == (48, 64, 3)
            written_frames += 1

        def release(self) -> None:
            return None

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: Capture())
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args: Writer())
    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.0, 1.0),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, _ProbeRunner(_probe_payload(), [])),
        )

    assert written_frames == 10
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_publication_race_never_overwrites_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a destination created at publication being silently overwritten."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    payload = _probe_payload()
    output = tmp_path / "output.mp4"

    class Capture:
        index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, property_id: int) -> float:
            return {
                cv2.CAP_PROP_FPS: 10.0,
                cv2.CAP_PROP_FRAME_WIDTH: 64.0,
                cv2.CAP_PROP_FRAME_HEIGHT: 48.0,
            }.get(property_id, 0.0)

        def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
            if self.index >= 10:
                return False, None
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

        def release(self) -> None:
            return None

    class Writer:
        def isOpened(self) -> bool:
            return True

        def write(self, _frame: NDArray[np.uint8]) -> None:
            return None

        def release(self) -> None:
            return None

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(0, "", _compact_frame_probe_output())
            return CommandResult(0, "", json.dumps(payload))
        if "null" in arguments:
            return CommandResult(0, "", "")
        Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: Capture())
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args: Writer())

    original_replace = os.replace
    original_link = os.link

    def racing_replace(candidate: Path, destination: Path) -> None:
        Path(destination).write_bytes(b"racer")
        original_replace(candidate, destination)

    def racing_link(candidate: Path, destination: Path) -> None:
        Path(destination).write_bytes(b"racer")
        original_link(candidate, destination)

    monkeypatch.setattr(os, "replace", racing_replace)
    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(RescueArtifactError):
        render_deblurred_video(
            source,
            output,
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )
    assert output.read_bytes() == b"racer"
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        output.name,
        source.name,
    ]


def test_video_renderer_rejects_candidate_without_required_h264_yuv420p(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches publishing a candidate whose probe breaks the explicit codec contract."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    probe_calls = 0

    class Capture:
        index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, property_id: int) -> float:
            return {
                cv2.CAP_PROP_FPS: 10.0,
                cv2.CAP_PROP_FRAME_WIDTH: 64.0,
                cv2.CAP_PROP_FRAME_HEIGHT: 48.0,
            }.get(property_id, 0.0)

        def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
            if self.index >= 10:
                return False, None
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

        def release(self) -> None:
            return None

    class Writer:
        def isOpened(self) -> bool:
            return True

        def write(self, _frame: NDArray[np.uint8]) -> None:
            return None

        def release(self) -> None:
            return None

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal probe_calls
        if arguments[0] == "ffprobe":
            probe_calls += 1
            payload = _probe_payload()
            if probe_calls == 2:
                streams = cast(list[object], payload["streams"])
                source_video = cast(dict[str, object], streams[0])
                payload["streams"] = [
                    {
                        **source_video,
                        "codec_name": "mpeg4",
                        "pix_fmt": "yuv444p",
                    }
                ]
            return CommandResult(0, "", json.dumps(payload))
        Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: Capture())
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args: Writer())
    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_rejects_nonuniform_candidate_frame_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final H.264 candidate must prove CFR from frame timestamps too."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    frame_probe_calls = 0

    class Capture:
        index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, property_id: int) -> float:
            return {
                cv2.CAP_PROP_FPS: 10.0,
                cv2.CAP_PROP_FRAME_WIDTH: 64.0,
                cv2.CAP_PROP_FRAME_HEIGHT: 48.0,
            }.get(property_id, 0.0)

        def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
            if self.index >= 10:
                return False, None
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

        def release(self) -> None:
            return None

    class Writer:
        def isOpened(self) -> bool:
            return True

        def write(self, _frame: NDArray[np.uint8]) -> None:
            return None

        def release(self) -> None:
            return None

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal frame_probe_calls
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                frame_probe_calls += 1
                payload = _frame_probe_payload()
                if frame_probe_calls == 2:
                    frames = cast(list[dict[str, object]], payload["frames"])
                    frames[5]["best_effort_timestamp_time"] = "0.55"
                return CommandResult(
                    0,
                    "",
                    _compact_frame_probe_from_frames(
                        cast(list[dict[str, object]], payload["frames"])
                    ),
                )
            return CommandResult(0, "", json.dumps(_probe_payload()))
        if "null" in arguments:
            return CommandResult(0, "", "")
        Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: Capture())
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args: Writer())
    with pytest.raises(RescueMediaError):
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )

    assert frame_probe_calls == 2
    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def test_video_renderer_rejects_changed_audio_stream_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stream copy must preserve every ordered stable audio descriptor."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_payload = _probe_payload()
    cast(list[object], source_payload["streams"]).extend(
        [_audio_stream(), _audio_stream(channel_layout="mono", channels=1)]
    )
    probe_calls = 0

    class Capture:
        index = 0

        def isOpened(self) -> bool:
            return True

        def get(self, property_id: int) -> float:
            return {
                cv2.CAP_PROP_FPS: 10.0,
                cv2.CAP_PROP_FRAME_WIDTH: 64.0,
                cv2.CAP_PROP_FRAME_HEIGHT: 48.0,
            }.get(property_id, 0.0)

        def read(self) -> tuple[bool, NDArray[np.uint8] | None]:
            if self.index >= 10:
                return False, None
            self.index += 1
            return True, np.zeros((48, 64, 3), dtype=np.uint8)

        def release(self) -> None:
            return None

    class Writer:
        def isOpened(self) -> bool:
            return True

        def write(self, _frame: NDArray[np.uint8]) -> None:
            return None

        def release(self) -> None:
            return None

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal probe_calls
        if arguments[0] == "ffprobe":
            if "-show_frames" in arguments:
                return CommandResult(0, "", _compact_frame_probe_output())
            probe_calls += 1
            payload = json.loads(json.dumps(source_payload))
            if probe_calls == 2:
                streams = cast(list[dict[str, object]], payload["streams"])
                streams[2]["sample_rate"] = "44100"
            return CommandResult(0, "", json.dumps(payload))
        if "null" in arguments:
            return CommandResult(0, "", "")
        Path(arguments[-1]).write_bytes(b"candidate")
        return CommandResult(0, "", "")

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: Capture())
    monkeypatch.setattr(cv2, "VideoWriter", lambda *_args: Writer())
    with pytest.raises(RescueMediaError) as caught:
        render_deblurred_video(
            source,
            tmp_path / "output.mp4",
            ((0.1, 0.5),),
            _estimate_fixture(),
            DeblurConfig(),
            ffmpeg_path=Path("ffmpeg"),
            ffprobe_path=Path("ffprobe"),
            runner=cast(Any, runner),
        )
    assert (
        caught.value.internal_message
        == "deblur candidate does not preserve audio streams"
    )

    assert sorted(item.name for item in tmp_path.iterdir()) == [source.name]


def _local_video_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        local_bin = (
            Path.home()
            / "AppData"
            / "Local"
            / "VideoScope"
            / "tools"
            / "ffmpeg-8.1.2"
            / "ffmpeg-8.1.2-essentials_build"
            / "bin"
        )
        local_ffmpeg = local_bin / "ffmpeg.exe"
        local_ffprobe = local_bin / "ffprobe.exe"
        if local_ffmpeg.is_file() and local_ffprobe.is_file():
            ffmpeg = str(local_ffmpeg)
            ffprobe = str(local_ffprobe)
    if ffmpeg is None or ffprobe is None:
        pytest.skip("local FFmpeg and ffprobe are required for deblur integration")
    assert ffmpeg is not None
    assert ffprobe is not None
    return ffmpeg, ffprobe


def _run_media(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        arguments,
        shell=False,
        check=True,
        capture_output=True,
        timeout=30,
    )


def _decode_frames(path: Path, ffmpeg: str) -> list[NDArray[np.uint8]]:
    result = _run_media(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-pix_fmt",
            "bgr24",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    size = 64 * 48 * 3
    assert len(result.stdout) % size == 0
    return [
        np.frombuffer(result.stdout[offset : offset + size], np.uint8)
        .reshape((48, 64, 3))
        .copy()
        for offset in range(0, len(result.stdout), size)
    ]


def _decoded_frame_hashes(path: Path, ffmpeg: str) -> tuple[str, ...]:
    return tuple(
        hashlib.sha256(frame.tobytes()).hexdigest()
        for frame in _decode_frames(path, ffmpeg)
    )


def _video_timestamps(path: Path, ffprobe: str) -> tuple[str, ...]:
    probe = _run_media(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(path),
        ]
    )
    payload = json.loads(probe.stdout)
    return tuple(
        str(frame["best_effort_timestamp_time"]) for frame in payload["frames"]
    )


def test_decoded_observable_gate_rejects_native_codec_round_trip_side_effects(
    tmp_path: Path,
) -> None:
    ffmpeg, _ffprobe = _local_video_tools()
    version = _run_media([ffmpeg, "-version"]).stdout.decode("utf-8", "replace")
    assert "ffmpeg version 8.1.2" in version
    source = tmp_path / "observable source.mp4"
    candidate = tmp_path / "observable noisy candidate.mp4"
    for path, video_filter in (
        (source, "testsrc2=size=64x48:rate=10:duration=1"),
        (
            candidate,
            "testsrc2=size=64x48:rate=10:duration=1,noise=alls=30:allf=t+u:all_seed=7",
        ),
    ):
        _run_media(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                video_filter,
                "-c:v",
                "libx264",
                "-crf",
                "0",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ]
        )
    config = DeblurConfig()
    measured = deblur_module._decoded_observable_candidate_metrics(
        tuple(_decode_frames(source, ffmpeg)),
        tuple(_decode_frames(candidate, ffmpeg)),
        config,
    )

    assert (
        measured["edge_width_ratio"] > config.maximum_edge_width_ratio
        or measured["edge_continuity_ratio"] < config.minimum_edge_continuity_ratio
        or measured["ringing_ratio"] > config.maximum_ringing_ratio
        or measured["noise_gain_ratio"] > config.maximum_noise_gain_ratio
        or measured["temporal_change_ratio"] > config.maximum_temporal_change_ratio
    )


def test_video_renderer_native_preserves_timing_audio_and_range_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches non-streaming selection, range leakage and media-contract loss."""
    ffmpeg, ffprobe = _local_video_tools()
    media_root = tmp_path / "中文 space"
    media_root.mkdir()
    source = media_root / "源 video.mp4"
    output = media_root / "恢复 result.mp4"
    _run_media(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:sample_rate=48000:duration=1",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-map",
            "2:a:0",
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
    calls = 0

    def replace_with_white(
        frame: NDArray[np.generic],
        estimate: BlurKernelEstimate,
        config: DeblurConfig,
    ) -> NDArray[np.uint8]:
        nonlocal calls
        del estimate, config
        calls += 1
        return np.full(frame.shape, 255, dtype=np.uint8)

    monkeypatch.setattr(
        "videoscope.rescue.deblur.restore_deblurred_frame", replace_with_white
    )
    render_deblurred_video(
        source,
        output,
        ((0.2, 0.6),),
        _estimate_fixture(),
        DeblurConfig(boundary_transition_seconds=0.1),
        ffmpeg_path=Path(ffmpeg),
        ffprobe_path=Path(ffprobe),
        runner=run_external_command,
    )

    source_frames = _decode_frames(source, ffmpeg)
    output_frames = _decode_frames(output, ffmpeg)
    assert calls == 4
    assert len(source_frames) == len(output_frames) == 10
    assert np.mean(np.abs(output_frames[1].astype(float) - source_frames[1])) < 3.0
    assert np.mean(np.abs(output_frames[6].astype(float) - source_frames[6])) < 3.0
    assert np.mean(output_frames[3]) > np.mean(source_frames[3]) + 50.0
    probe = _run_media(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(output),
        ]
    )
    payload = json.loads(probe.stdout)
    video = next(item for item in payload["streams"] if item["codec_type"] == "video")
    assert video["width"] == 64
    assert video["height"] == 48
    assert video["avg_frame_rate"] == "10/1"
    assert video["nb_frames"] == "10"
    audio_streams = [
        item for item in payload["streams"] if item["codec_type"] == "audio"
    ]
    assert len(audio_streams) == 2
    assert [item["codec_name"] for item in audio_streams] == ["aac", "aac"]
    assert [item["sample_rate"] for item in audio_streams] == ["48000", "48000"]
    assert [item["channels"] for item in audio_streams] == [1, 1]
    assert [item["channel_layout"] for item in audio_streams] == ["mono", "mono"]
    assert float(payload["format"]["duration"]) == pytest.approx(1.0, abs=0.1)
    assert source.exists()
    assert not any("deblur" in item.name for item in media_root.iterdir())


def test_video_renderer_native_repeated_outputs_have_deterministic_decoded_evidence(
    tmp_path: Path,
) -> None:
    ffmpeg, ffprobe = _local_video_tools()
    source = tmp_path / "repeat source.mp4"
    first = tmp_path / "repeat output 1.mp4"
    second = tmp_path / "repeat output 2.mp4"
    _run_media(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ]
    )

    for output in (first, second):
        render_deblurred_video(
            source,
            output,
            ((0.2, 0.6),),
            _estimate_fixture(),
            DeblurConfig(boundary_transition_seconds=0.1),
            ffmpeg_path=Path(ffmpeg),
            ffprobe_path=Path(ffprobe),
            runner=run_external_command,
        )

    assert source.exists() and first.exists() and second.exists()
    first_hashes = _decoded_frame_hashes(first, ffmpeg)
    second_hashes = _decoded_frame_hashes(second, ffmpeg)
    assert len(first_hashes) == len(second_hashes) == 10
    assert first_hashes == second_hashes
    assert _video_timestamps(first, ffprobe) == _video_timestamps(second, ffprobe)
    assert not any("deblur" in item.name for item in tmp_path.iterdir())


@pytest.mark.parametrize(
    ("kind", "radius", "seed", "pattern"),
    [
        (kind, radius, seed, pattern)
        for kind, seed in (("box", 3), ("gaussian", 17))
        for radius in range(1, 6)
        for pattern in (_pattern, _perforated_tiles)
    ],
)
def test_estimate_recovers_kernel_family_and_radius_neighborhood(
    kind: str,
    radius: int,
    seed: int,
    pattern: Callable[[int], NDArray[np.uint8]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    _clean, _blurred, estimate = _estimate(kind, radius, seed, pattern)

    assert estimate.kernel_kind == kind
    assert abs(estimate.radius - radius) <= 1
    assert 0.0 <= estimate.confidence <= 1.0


@pytest.mark.parametrize(("kind", "radius"), [("box", 3), ("gaussian", 3)])
def test_restore_reduces_edge_spread_and_improves_similarity(
    kind: str, radius: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    clean, blurred, estimate = _estimate(kind, radius)

    restored = restore_deblurred_frame(blurred, estimate, DeblurConfig())

    blurred_width = measure_edge_spread_width(blurred, DeblurConfig())
    restored_width = measure_edge_spread_width(restored, DeblurConfig())
    config = DeblurConfig()
    assert restored_width < config.maximum_edge_width_ratio * blurred_width
    assert _ssim(clean, restored) > _ssim(clean, blurred)


def test_side_effect_metrics_and_temporal_change_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    clean = _pattern(23)
    second = np.roll(clean, 1, axis=1)
    blurred = _blur(clean, "gaussian", 2)
    blurred_second = _blur(second, "gaussian", 2)
    config = DeblurConfig()

    estimate = estimate_blur_kernel((blurred, blurred_second), config)

    assert estimate is not None
    assert estimate.ringing_ratio <= config.maximum_ringing_ratio
    assert estimate.noise_gain_ratio <= config.maximum_noise_gain_ratio
    assert estimate.temporal_change_ratio <= config.maximum_temporal_change_ratio


def test_candidate_output_temporal_gate_rejects_inverse_amplified_noise() -> None:
    clean = _pattern(23)
    first = _blur(clean, "gaussian", 2)
    second = first.copy()
    rng = np.random.default_rng(99)
    changed = rng.choice(second.size, 2000, replace=False)
    second.flat[changed] = rng.integers(0, 256, changed.size, dtype=np.uint8)
    config = DeblurConfig(
        candidate_kernel_kinds=("gaussian",),
        candidate_radii=(2,),
        candidate_regularizations=(0.001,),
    )

    input_change = float(np.mean(np.abs(first.astype(float) - second.astype(float))))
    assert input_change / 255.0 < config.maximum_temporal_change_ratio
    assert estimate_blur_kernel((first, second), config) is None


def test_temporal_graph_selects_stable_majority_medoid_with_one_outlier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    stable = _blur(_pattern(67), "gaussian", 2)
    outlier = np.roll(stable, 17, axis=1)
    estimates: list[BlurKernelEstimate] = []
    for outlier_index in (0, 2, 4):
        observations = [stable.copy() for _ in range(4)]
        observations.insert(outlier_index, outlier.copy())
        estimate = estimate_blur_kernel(tuple(observations), DeblurConfig())
        assert estimate is not None
        estimates.append(estimate)

    assert estimates[0] == estimates[1] == estimates[2]


def test_temporal_graph_rejects_observations_without_a_stable_majority() -> None:
    stable = _blur(_pattern(71), "gaussian", 2)
    observations = tuple(np.roll(stable, offset, axis=1) for offset in (0, 12, 24))

    assert estimate_blur_kernel(observations, DeblurConfig()) is None


def test_temporal_graph_does_not_merge_two_equal_scene_clusters() -> None:
    first_scene = _blur(_pattern(73), "gaussian", 2)
    rng = np.random.default_rng(73)
    second_scene = rng.integers(0, 256, first_scene.shape, dtype=np.uint8)

    assert (
        estimate_blur_kernel(
            (first_scene, second_scene, first_scene.copy(), second_scene.copy()),
            DeblurConfig(),
        )
        is None
    )


def test_temporal_graph_rejects_a_bridge_between_incompatible_scenes() -> None:
    middle = _blur(_pattern(79), "gaussian", 2).astype(np.int16)
    first = np.clip(middle - 36, 0, 255).astype(np.uint8)
    bridge = middle.astype(np.uint8)
    last = np.clip(middle + 36, 0, 255).astype(np.uint8)

    assert estimate_blur_kernel((first, bridge, last), DeblurConfig()) is None


def test_observation_inventory_accepts_exact_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    observed = _blur(_pattern(83), "gaussian", 2)
    config = DeblurConfig(maximum_observations=3)

    assert (
        estimate_blur_kernel(
            (observed, observed.copy(), observed.copy()),
            config,
        )
        is not None
    )


def test_observation_inventory_rejects_limit_plus_one_before_quadratic_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = _blur(_pattern(89), "gaussian", 2)
    config = DeblurConfig(maximum_observations=3)

    def unexpected_work(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("over-budget observations reached quadratic work")

    monkeypatch.setattr(cv2, "phaseCorrelate", unexpected_work)
    monkeypatch.setattr(deblur_module, "_restore_luma", unexpected_work)

    assert (
        estimate_blur_kernel(
            (observed, observed.copy(), observed.copy(), observed.copy()),
            config,
        )
        is None
    )


def test_observation_inventory_config_has_a_safe_exhaustive_search_ceiling() -> None:
    assert DeblurConfig(maximum_observations=1).maximum_observations == 1
    assert DeblurConfig(maximum_observations=16).maximum_observations == 16
    for unsafe_maximum in (17, 64):
        with pytest.raises(ValidationError):
            DeblurConfig(maximum_observations=unsafe_maximum)


def test_maximum_observation_hostile_graph_fails_closed_without_restoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rng = np.random.default_rng(97)
    observations = tuple(
        rng.integers(0, 256, (96, 128), dtype=np.uint8) for _ in range(16)
    )

    def unexpected_restore(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("hostile compatibility graph reached restoration")

    monkeypatch.setattr(deblur_module, "_restore_luma", unexpected_restore)
    assert estimate_blur_kernel(observations, DeblurConfig()) is None


def test_bounded_ringing_suppression_resolves_compressed_text_pareto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    observed = _compressed_text_pattern()
    baseline = DeblurConfig(
        candidate_kernel_kinds=("gaussian",),
        candidate_radii=(2,),
        candidate_regularizations=(0.003,),
        candidate_ringing_suppression_strengths=(0.0,),
    )
    assert estimate_blur_kernel((observed, observed.copy()), baseline) is None

    config = DeblurConfig(
        candidate_kernel_kinds=("gaussian",),
        candidate_radii=(2,),
        candidate_regularizations=(0.003,),
        candidate_ringing_suppression_strengths=(0.0, 1.0),
        ringing_suppression_feather_pixels=1,
    )
    estimate = estimate_blur_kernel((observed, observed.copy()), config)

    assert estimate is not None
    assert estimate.ringing_suppression_strength == 1.0
    assert estimate.predicted_edge_width_after <= (
        config.maximum_edge_width_ratio * estimate.edge_width_before
    )
    assert estimate.ringing_ratio <= config.maximum_ringing_ratio
    assert estimate.noise_gain_ratio <= config.maximum_noise_gain_ratio
    assert estimate.temporal_change_ratio <= config.maximum_temporal_change_ratio
    restored = restore_deblurred_frame(observed, estimate, config)
    assert measure_edge_spread_width(restored, config) <= (
        config.maximum_edge_width_ratio * measure_edge_spread_width(observed, config)
    )


def test_estimator_rejects_locally_unobservable_compressed_text_candidate() -> None:
    """A direct candidate must satisfy the same decoded-pixel gates as publication."""
    observed = _compressed_text_pattern()

    assert estimate_blur_kernel((observed, observed.copy()), DeblurConfig()) is None


def test_decoded_observable_metrics_match_verifier_and_cover_all_observations() -> None:
    source = _compressed_text_pattern()
    estimate = _estimate_fixture()
    config = DeblurConfig()
    candidate = restore_deblurred_frame(source, estimate, config)

    measured = deblur_module._decoded_observable_candidate_metrics(
        (source, source.copy()), (candidate, candidate.copy()), config
    )
    expected = verification_module._independent_deblur_pair_metrics(source, candidate)

    for key, value in expected.items():
        assert measured[key] == pytest.approx(value, abs=1e-12)
    damaged = candidate.copy()
    damaged[::2, ::2] = 255 - damaged[::2, ::2]
    inventory = deblur_module._decoded_observable_candidate_metrics(
        (source, source.copy(), source.copy()),
        (candidate, candidate.copy(), damaged),
        config,
    )
    assert (
        inventory["ringing_ratio"] > config.maximum_ringing_ratio
        or inventory["noise_gain_ratio"] > config.maximum_noise_gain_ratio
    )


def test_decoded_observable_inventory_mismatch_fails_closed() -> None:
    source = _compressed_text_pattern()
    with pytest.raises(ValueError, match="inventories"):
        deblur_module._decoded_observable_candidate_metrics(
            (source,), (), DeblurConfig()
        )


@pytest.mark.parametrize(
    "frames_factory",
    [
        lambda: (np.full((96, 128), 112, dtype=np.uint8),),
        lambda: (np.pad(np.full((2, 2), 255, dtype=np.uint8), 31),),
        lambda: (_blur(_pattern(3, height=128, width=192), "box", 11),),
        lambda: (
            _pattern(4, height=128, width=192),
            np.rot90(_pattern(9, height=192, width=128)),
        ),
        lambda: (
            np.random.default_rng(5)
            .normal(127, 75, size=(128, 192))
            .clip(0, 255)
            .astype(np.uint8),
        ),
    ],
)
def test_unsupported_observations_fail_closed(
    frames_factory: Callable[[], tuple[NDArray[np.uint8], ...]],
) -> None:
    assert estimate_blur_kernel(frames_factory(), DeblurConfig()) is None


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf])
def test_non_finite_observations_fail_closed(value: float) -> None:
    frame = _pattern(1).astype(np.float64)
    frame[30, 40] = value

    assert estimate_blur_kernel((frame,), DeblurConfig()) is None


@pytest.mark.parametrize(
    "frame",
    [
        np.zeros((0, 32), dtype=np.uint8),
        np.zeros((32, 0, 3), dtype=np.uint8),
        np.zeros((32,), dtype=np.uint8),
        np.zeros((32, 32, 4), dtype=np.uint8),
        np.zeros((32, 32), dtype=np.int32),
    ],
)
def test_malformed_frames_raise_path_free_errors(frame: NDArray[np.generic]) -> None:
    with pytest.raises(ValueError, match="frame") as error:
        estimate_blur_kernel((frame,), DeblurConfig())
    assert "\\" not in str(error.value)
    assert ":/" not in str(error.value)


def test_empty_frame_sequence_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="at least one frame"):
        estimate_blur_kernel((), DeblurConfig())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidate_radii": (0, 2)},
        {"candidate_radii": (1, 6)},
        {"candidate_radii": (2, 2)},
        {"candidate_regularizations": (0.0, 0.01)},
        {"candidate_regularizations": (float("nan"),)},
        {"candidate_regularizations": (float("inf"),)},
        {"candidate_regularizations": (-float("inf"),)},
        {"candidate_ringing_suppression_strengths": ()},
        {"candidate_ringing_suppression_strengths": (0.0, 0.0)},
        {"candidate_ringing_suppression_strengths": (float("nan"),)},
        {"candidate_ringing_suppression_strengths": (-0.01,)},
        {"candidate_ringing_suppression_strengths": (1.01,)},
        {"candidate_ringing_suppression_strengths": (1.0, 0.0)},
        {"maximum_observations": 0},
        {"maximum_observations": 65},
        {"maximum_observations": 2.0},
        {"candidate_kernel_kinds": ("box", "triangle")},
        {"maximum_ringing_ratio": 1.1},
    ],
)
def test_config_rejects_invalid_candidate_space(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        DeblurConfig(**cast(dict[str, Any], kwargs))


def test_estimate_and_restore_are_deterministic_and_do_not_mutate_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    clean = _pattern(31)
    color = cv2.cvtColor(_blur(clean, "box", 2), cv2.COLOR_GRAY2BGR)
    original = color.copy()
    config = DeblurConfig()

    first = estimate_blur_kernel((color, color.copy()), config)
    second = estimate_blur_kernel((color, color.copy()), config)

    assert first is not None
    assert first == second
    assert np.array_equal(color, original)
    restored_first = restore_deblurred_frame(color, first, config)
    restored_second = restore_deblurred_frame(color, second, config)
    assert restored_first.dtype == np.uint8
    assert restored_first.shape == color.shape
    assert np.array_equal(restored_first, restored_second)
    assert np.array_equal(color, original)


def test_grayscale_and_bgr_measurement_agree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    gray = _blur(_pattern(17), "gaussian", 2)
    bgr = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    config = DeblurConfig()

    gray_estimate = estimate_blur_kernel((gray,), config)
    bgr_estimate = estimate_blur_kernel((bgr,), config)

    assert gray_estimate is not None
    assert bgr_estimate is not None
    assert gray_estimate.kernel_kind == bgr_estimate.kernel_kind
    assert gray_estimate.radius == bgr_estimate.radius
    assert gray_estimate.regularization == bgr_estimate.regularization


def test_laplacian_halo_does_not_satisfy_combined_side_effect_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    blurred = _blur(_perforated_tiles(41), "gaussian", 3)
    halo = cv2.addWeighted(
        blurred,
        1.5,
        cv2.Laplacian(blurred, cv2.CV_8U, ksize=3),
        -0.5,
        0,
    )
    config = DeblurConfig()

    assert float(np.mean(np.abs(halo.astype(float) - blurred.astype(float)))) > 8.0
    assert estimate_blur_kernel((halo,), config) is None
    loosened = DeblurConfig(maximum_ringing_ratio=1.0)
    accepted = estimate_blur_kernel((halo,), loosened)

    assert accepted is not None
    assert accepted.ringing_ratio > config.maximum_ringing_ratio
    assert accepted.noise_gain_ratio <= config.maximum_noise_gain_ratio


def test_blur_kernel_estimate_is_immutable_and_path_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _isolate_legacy_candidate_search(monkeypatch)
    _clean, _blurred, estimate = _estimate("box", 2)

    with pytest.raises(ValidationError):
        estimate.radius = 4  # type: ignore[misc]
    dumped = estimate.model_dump_json()
    assert "path" not in dumped.lower()
    assert "fixture" not in dumped.lower()
