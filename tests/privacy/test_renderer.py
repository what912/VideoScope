"""Tests for bounded, deterministic visual privacy redaction rendering."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from videoscope.domain import Severity
from videoscope.privacy.commands import build_privacy_frame_timestamp_arguments
from videoscope.privacy.errors import (
    PrivacyCancelledError,
    PrivacyMediaError,
    PrivacyPlanError,
)
from videoscope.privacy.manual import ManualVisualRegionInput, build_manual_visual_risk
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyDecision,
    PrivacyEffectiveConfig,
    PrivacyPlan,
    PrivacyRisk,
    PrivacyRiskMap,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_risk_id,
)
from videoscope.privacy.planner import build_privacy_plan
from videoscope.privacy.profiles import get_share_audience_profile
from videoscope.privacy.renderer import (
    DecodedFrame,
    FrameStreamInfo,
    VisualRedactionRenderer,
    VisualWriterRequest,
    _FFmpegFrameReader,
    apply_blur,
    apply_pixelate,
    apply_solid_fill,
    expand_box,
    interpolate_box,
    lockstep_decoded_frames,
    parse_frame_timestamp_line,
)

np: Any = importlib.import_module("numpy")


def _key_box(
    timestamp_seconds: float,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
) -> tuple[float, NormalizedBox]:
    return (
        timestamp_seconds,
        NormalizedBox(
            x_min=x_min,
            y_min=y_min,
            x_max=x_max,
            y_max=y_max,
        ),
    )


def _checkerboard(size: int = 32) -> Any:
    grid = np.indices((size, size)).sum(axis=0) % 2
    gray = (grid * 255).astype(np.uint8)
    return np.repeat(gray[:, :, np.newaxis], 3, axis=2)


def _outside_mask(
    height: int,
    width: int,
    box: NormalizedBox,
) -> Any:
    mask: Any = np.ones((height, width), dtype=bool)
    x_min = int(np.floor(box.x_min * width))
    y_min = int(np.floor(box.y_min * height))
    x_max = int(np.ceil(box.x_max * width))
    y_max = int(np.ceil(box.y_max * height))
    mask[y_min:y_max, x_min:x_max] = False
    return mask


def test_interpolated_box_expands_across_track_gap() -> None:
    box = interpolate_box(
        before=_key_box(1.0, 0.1, 0.1, 0.2, 0.2),
        after=_key_box(2.0, 0.2, 0.1, 0.3, 0.2),
        timestamp_seconds=1.5,
        guard_ratio=0.05,
        gap_requires_expansion=True,
    )

    assert box.x_min < 0.15
    assert box.x_max > 0.25


def test_interpolation_is_deterministic_and_clamped_to_keyframe_range() -> None:
    before = _key_box(1.0, 0.1, 0.2, 0.3, 0.5)
    after = _key_box(3.0, 0.5, 0.4, 0.8, 0.9)

    assert interpolate_box(before, after, 0.0, 0.0, False) == before[1]
    assert interpolate_box(before, after, 4.0, 0.0, False) == after[1]
    assert interpolate_box(before, after, 2.0, 0.0, False) == NormalizedBox(
        x_min=0.3,
        y_min=0.3,
        x_max=0.55,
        y_max=0.7,
    )


def test_expand_box_clamps_to_frame_bounds() -> None:
    expanded = expand_box(
        NormalizedBox(x_min=0.01, y_min=0.02, x_max=0.99, y_max=0.98),
        guard_ratio=0.2,
    )

    assert expanded == NormalizedBox(x_min=0.0, y_min=0.0, x_max=1.0, y_max=1.0)


def test_pixelate_changes_only_the_selected_region() -> None:
    source = _checkerboard()
    box = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.75, y_max=0.75)

    output = apply_pixelate(source.copy(), box, block_size=8)

    outside = _outside_mask(int(source.shape[0]), int(source.shape[1]), box)
    assert np.array_equal(output[outside], source[outside])
    assert not np.array_equal(output[8:24, 8:24], source[8:24, 8:24])


def test_blur_changes_only_the_selected_region() -> None:
    source = _checkerboard()
    box = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.75, y_max=0.75)

    output = apply_blur(source.copy(), box, kernel_size=7)

    outside = _outside_mask(int(source.shape[0]), int(source.shape[1]), box)
    assert np.array_equal(output[outside], source[outside])
    assert not np.array_equal(output[8:24, 8:24], source[8:24, 8:24])


def test_solid_fill_changes_only_the_selected_region() -> None:
    source = _checkerboard()
    box = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.75, y_max=0.75)

    output = apply_solid_fill(source.copy(), box, color=(12, 34, 56))

    outside = _outside_mask(int(source.shape[0]), int(source.shape[1]), box)
    assert np.array_equal(output[outside], source[outside])
    assert np.all(output[8:24, 8:24] == np.array([12, 34, 56], dtype=np.uint8))


def test_frame_operations_reject_non_uint8_bgr_frames() -> None:
    bad_frame: Any = np.zeros((16, 16), dtype=np.uint8)
    box = NormalizedBox(x_min=0.1, y_min=0.1, x_max=0.9, y_max=0.9)

    for operation, parameter in (
        (apply_blur, 3),
        (apply_pixelate, 4),
    ):
        try:
            operation(bad_frame, box, parameter)
        except ValueError as exc:
            assert "BGR" in str(exc)
        else:  # pragma: no cover - assertion branch
            raise AssertionError("invalid frame was accepted")

    try:
        apply_solid_fill(bad_frame, box, (0, 0, 0))
    except ValueError as exc:
        assert "BGR" in str(exc)
    else:  # pragma: no cover - assertion branch
        raise AssertionError("invalid frame was accepted")


class CountingFrameReader:
    def __init__(
        self,
        *,
        total_frames: int,
        frame: Any | None = None,
        frame_rate: float = 10.0,
        timestamps: tuple[float, ...] | None = None,
    ) -> None:
        self.total_frames = total_frames
        self.frame = frame if frame is not None else _checkerboard()
        self.stream_info = FrameStreamInfo(
            width=int(self.frame.shape[1]),
            height=int(self.frame.shape[0]),
            frame_rate=frame_rate,
        )
        self.frames_yielded = 0
        self.timestamps = timestamps
        self.closed = False
        self.terminated = False

    def frames(self) -> Iterator[DecodedFrame]:
        for index in range(self.total_frames):
            self.frames_yielded += 1
            yield DecodedFrame(
                pixels=self.frame.copy(),
                timestamp_seconds=(
                    self.timestamps[index]
                    if self.timestamps is not None
                    else index / self.stream_info.frame_rate
                ),
            )

    def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True


class CountingFrameWriter:
    def __init__(
        self,
        output: Path,
        *,
        fail_after: int | None = None,
    ) -> None:
        self.output = output
        self.fail_after = fail_after
        self.frames: list[Any] = []
        self.closed = False
        self.terminated = False

    @property
    def frames_written(self) -> int:
        return len(self.frames)

    def write(self, frame: Any) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(b"partial")
        if self.fail_after is not None and len(self.frames) >= self.fail_after:
            raise PrivacyMediaError("sanitized encoder failure")
        self.frames.append(frame.copy())

    def close(self) -> None:
        self.closed = True
        self.output.write_bytes(b"complete")

    def terminate(self) -> None:
        self.terminated = True


def _visual_plan(
    *regions: tuple[float, float, NormalizedBox, RedactionStyle],
    duration_seconds: float = 12.0,
    config: PrivacyEffectiveConfig | None = None,
) -> PrivacyPlan:
    risks = tuple(
        build_manual_visual_risk(
            "a" * 64,
            ManualVisualRegionInput(
                start_seconds=start,
                end_seconds=end,
                box=box,
                style=style,
                source_duration_seconds=duration_seconds,
            ),
        )
        for start, end, box, style in regions
    )
    return build_privacy_plan(
        risk_map=PrivacyRiskMap(
            input_hash="a" * 64,
            profile="public",
            duration_seconds=duration_seconds,
            risks=risks,
        ),
        reviews=(),
        profile=get_share_audience_profile("public"),
        config=config or PrivacyEffectiveConfig(),
    )


def _tracked_visual_plan(*, expand_track_gaps: bool) -> PrivacyPlan:
    union = NormalizedBox(x_min=0.1, y_min=0.25, x_max=0.7, y_max=0.75)
    first = NormalizedBox(x_min=0.1, y_min=0.25, x_max=0.3, y_max=0.75)
    second = NormalizedBox(x_min=0.5, y_min=0.25, x_max=0.7, y_max=0.75)
    risk = PrivacyRisk(
        id=make_privacy_risk_id(
            "a" * 64,
            "face_region",
            PrivacyRiskType.FACE_REGION,
            0.0,
            1.0,
            union,
        ),
        scanner_id="face_region",
        scanner_version="1.0.0",
        risk_type=PrivacyRiskType.FACE_REGION,
        title="Anonymous face-like region",
        public_description="A local heuristic proposed this region for review.",
        severity=Severity.MEDIUM,
        confidence=0.8,
        start_seconds=0.0,
        end_seconds=1.0,
        box=union,
        track_id="face_track_01",
        recommended_style=RedactionStyle.SOLID_FILL,
        decision=PrivacyDecision.REDACT,
        style=RedactionStyle.SOLID_FILL,
        limitations=("This is a heuristic region.",),
        evidence=(
            {"timestamp_seconds": 0.0, "box": first.model_dump(mode="json")},
            {"timestamp_seconds": 1.0, "box": second.model_dump(mode="json")},
        ),
    )
    return build_privacy_plan(
        PrivacyRiskMap(
            input_hash="a" * 64,
            profile="public",
            duration_seconds=1.0,
            risks=(risk,),
        ),
        (),
        get_share_audience_profile("public"),
        PrivacyEffectiveConfig(
            interpolation_guard_ratio=0.5,
            expand_track_gaps=expand_track_gaps,
        ),
    )


def test_effective_config_validates_renderer_thresholds() -> None:
    config = PrivacyEffectiveConfig(
        blur_kernel_size=9,
        pixelate_block_size=6,
        solid_fill_color=(1, 2, 3),
        interpolation_guard_ratio=0.1,
        expand_track_gaps=False,
    )

    assert config.blur_kernel_size == 9
    assert config.pixelate_block_size == 6
    assert config.solid_fill_color == (1, 2, 3)

    with pytest.raises(ValidationError):
        PrivacyEffectiveConfig(blur_kernel_size=8)
    with pytest.raises(ValidationError):
        PrivacyEffectiveConfig(pixelate_block_size=1)
    with pytest.raises(ValidationError):
        PrivacyEffectiveConfig(solid_fill_color=(0, 0, 300))


def test_renderer_streams_frames_without_collecting_video(tmp_path: Path) -> None:
    reader = CountingFrameReader(total_frames=120)
    output = tmp_path / "输出 share visual.mp4"
    writer = CountingFrameWriter(output)
    requests: list[VisualWriterRequest] = []

    def writer_factory(request: VisualWriterRequest) -> CountingFrameWriter:
        requests.append(request)
        return writer

    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader,
        writer_factory=writer_factory,
    )
    result = renderer.render(
        Path("源 source.mp4"),
        output,
        _visual_plan(),
        lambda: False,
    )

    assert result.frames_read == 120
    assert result.frames_written == 120
    assert result.maximum_buffered_frames <= 2
    assert writer.frames_written == 120
    assert reader.closed is True
    assert writer.closed is True
    assert requests == [
        VisualWriterRequest(
            output=output,
            width=32,
            height=32,
            frame_rate=10.0,
        )
    ]


def test_renderer_must_write_each_frame_before_requesting_the_next(
    tmp_path: Path,
) -> None:
    output = tmp_path / "strict-lazy.mp4"
    writer = CountingFrameWriter(output)
    writer_started = False

    class StrictLazyReader:
        stream_info = FrameStreamInfo(width=32, height=32, frame_rate=10.0)

        def frames(self) -> Iterator[DecodedFrame]:
            assert writer_started is True
            for index in range(120):
                assert writer.frames_written == index
                yield DecodedFrame(
                    pixels=_checkerboard(),
                    timestamp_seconds=index / 10.0,
                )

        def close(self) -> None:
            return

        def terminate(self) -> None:
            return

    def writer_factory(_: VisualWriterRequest) -> CountingFrameWriter:
        nonlocal writer_started
        writer_started = True
        return writer

    result = VisualRedactionRenderer(
        reader_factory=lambda _: StrictLazyReader(),
        writer_factory=writer_factory,
    ).render(
        Path("source.mp4"),
        output,
        _visual_plan(),
        lambda: False,
    )

    assert result.frames_read == 120
    assert result.maximum_buffered_frames == 1


def test_renderer_applies_actions_only_inside_their_time_window(
    tmp_path: Path,
) -> None:
    source_frame = _checkerboard()
    reader = CountingFrameReader(total_frames=3, frame=source_frame, frame_rate=1.0)
    writer = CountingFrameWriter(tmp_path / "visual.mp4")
    box = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.75, y_max=0.75)
    plan = _visual_plan((1.0, 2.0, box, RedactionStyle.SOLID_FILL), duration_seconds=3)
    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader,
        writer_factory=lambda _: writer,
    )

    renderer.render(Path("source.mp4"), writer.output, plan, lambda: False)

    assert np.array_equal(writer.frames[0], source_frame)
    assert not np.array_equal(writer.frames[1], source_frame)
    assert np.array_equal(writer.frames[2], source_frame)


def test_vfr_pts_select_the_action_frame_instead_of_average_rate(
    tmp_path: Path,
) -> None:
    source_frame = _checkerboard()
    reader = CountingFrameReader(
        total_frames=3,
        frame=source_frame,
        frame_rate=10.0,
        timestamps=(0.0, 0.35, 0.4),
    )
    writer = CountingFrameWriter(tmp_path / "vfr.mp4")
    box = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.75, y_max=0.75)
    plan = _visual_plan((0.3, 0.4, box, RedactionStyle.SOLID_FILL), duration_seconds=1)
    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader,
        writer_factory=lambda _: writer,
    )

    renderer.render(Path("source.mp4"), writer.output, plan, lambda: False)

    assert np.array_equal(writer.frames[0], source_frame)
    assert not np.array_equal(writer.frames[1], source_frame)
    assert np.array_equal(writer.frames[2], source_frame)


def test_renderer_interpolates_keyframes_at_pts_and_expands_track_gaps(
    tmp_path: Path,
) -> None:
    source_frame = np.full((32, 32, 3), 255, dtype=np.uint8)

    def render(expand: bool, name: str) -> Any:
        reader = CountingFrameReader(
            total_frames=1,
            frame=source_frame,
            timestamps=(0.5,),
        )
        writer = CountingFrameWriter(tmp_path / name)
        VisualRedactionRenderer(
            reader_factory=lambda _: reader,
            writer_factory=lambda _: writer,
        ).render(
            Path("source.mp4"),
            writer.output,
            _tracked_visual_plan(expand_track_gaps=expand),
            lambda: False,
        )
        return writer.frames[0]

    without_expansion = render(False, "without.mp4")
    with_expansion = render(True, "with.mp4")

    assert np.all(without_expansion[:, 6] == 255)
    assert np.all(with_expansion[8:24, 6] == 0)
    assert not np.array_equal(without_expansion, with_expansion)


def test_raw_frames_and_pts_are_consumed_in_lockstep() -> None:
    frames = (_checkerboard(8), _checkerboard(8), _checkerboard(8))

    decoded = tuple(lockstep_decoded_frames(iter(frames), iter((0.0, 0.04, 0.5))))

    assert [frame.timestamp_seconds for frame in decoded] == [0.0, 0.04, 0.5]
    assert all(decoded[index].pixels is frames[index] for index in range(3))


def _assert_raw_frame_and_pts_count_mismatch(
    frame_count: int,
    timestamps: tuple[float, ...],
) -> None:
    frames = iter(tuple(_checkerboard(8) for _ in range(frame_count)))

    with pytest.raises(PrivacyMediaError) as error:
        tuple(lockstep_decoded_frames(frames, iter(timestamps)))

    assert "timestamp" in (error.value.internal_message or "")


def test_missing_pts_fails_raw_frame_lockstep() -> None:
    _assert_raw_frame_and_pts_count_mismatch(2, (0.0,))


def test_extra_pts_fails_raw_frame_lockstep() -> None:
    _assert_raw_frame_and_pts_count_mismatch(1, (0.0, 0.5))


def test_real_vfr_render_uses_ffprobe_pts_for_action_timing(tmp_path: Path) -> None:
    ffmpeg = os.environ.get("VIDEOSCOPE_TEST_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("VIDEOSCOPE_TEST_FFPROBE") or shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("local FFmpeg and ffprobe are required for real VFR coverage")
    assert ffmpeg is not None
    assert ffprobe is not None

    for index in range(3):
        Image.new("RGB", (32, 32), (255, 255, 255)).save(
            tmp_path / f"frame-{index}.png"
        )
    concat = tmp_path / "frames.txt"
    concat.write_text(
        "\n".join(
            (
                "file 'frame-0.png'",
                "duration 0.04",
                "file 'frame-1.png'",
                "duration 0.80",
                "file 'frame-2.png'",
                "duration 0.04",
                "file 'frame-2.png'",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    source = tmp_path / "真实 VFR source.mp4"
    generated = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat),
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert generated.returncode == 0, generated.stderr[-1000:]

    probed = subprocess.run(
        build_privacy_frame_timestamp_arguments(source, ffprobe=ffprobe),
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert probed.returncode == 0, probed.stderr[-1000:]
    timestamps = tuple(
        parse_frame_timestamp_line(line)
        for line in probed.stdout.splitlines()
        if line.strip()
    )
    assert len(timestamps) >= 3
    assert timestamps[1] - timestamps[0] != pytest.approx(timestamps[2] - timestamps[1])

    selected_pts = timestamps[1]
    box = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.75, y_max=0.75)
    plan = _visual_plan(
        (
            max(0.0, selected_pts - 0.001),
            selected_pts + 0.001,
            box,
            RedactionStyle.SOLID_FILL,
        ),
        duration_seconds=timestamps[-1] + 0.5,
    )
    output = tmp_path / "VFR redacted visual.mp4"
    VisualRedactionRenderer(ffmpeg=ffmpeg, ffprobe=ffprobe).render(
        source,
        output,
        plan,
        lambda: False,
    )

    cv2 = importlib.import_module("cv2")
    capture = cv2.VideoCapture(str(output))
    rendered: list[Any] = []
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        rendered.append(frame)
    capture.release()

    assert len(rendered) == len(timestamps)
    assert float(rendered[0][8:24, 8:24].mean()) > 240
    assert float(rendered[1][8:24, 8:24].mean()) < 20
    assert float(rendered[2][8:24, 8:24].mean()) > 240


def test_real_rotation_metadata_is_rejected_before_output_starts(
    tmp_path: Path,
) -> None:
    ffmpeg = os.environ.get("VIDEOSCOPE_TEST_FFMPEG") or shutil.which("ffmpeg")
    ffprobe = os.environ.get("VIDEOSCOPE_TEST_FFPROBE") or shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("local FFmpeg and ffprobe are required for rotation coverage")
    assert ffmpeg is not None
    assert ffprobe is not None

    base = tmp_path / "rotation-zero.mp4"
    generated = subprocess.run(
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
            "color=white:size=32x32:rate=2:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(base),
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert generated.returncode == 0, generated.stderr[-1000:]
    rotated = tmp_path / "rotation-90.mp4"
    tagged = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-display_rotation",
            "90",
            "-i",
            str(base),
            "-c",
            "copy",
            str(rotated),
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    assert tagged.returncode == 0, tagged.stderr[-1000:]

    renderer = VisualRedactionRenderer(ffmpeg=ffmpeg, ffprobe=ffprobe)
    clean_output = tmp_path / "clean-output.mp4"
    renderer.render(base, clean_output, _visual_plan(duration_seconds=1), lambda: False)
    assert clean_output.is_file()

    rotated_output = tmp_path / "must-not-exist.mp4"
    with pytest.raises(PrivacyMediaError) as error:
        renderer.render(
            rotated,
            rotated_output,
            _visual_plan(duration_seconds=1),
            lambda: False,
        )

    assert "rotation" in (error.value.internal_message or "")
    assert not rotated_output.exists()


def test_crop_is_applied_before_region_redaction(tmp_path: Path) -> None:
    source_frame = _checkerboard()
    reader = CountingFrameReader(total_frames=1, frame=source_frame, frame_rate=1.0)
    writer = CountingFrameWriter(tmp_path / "visual.mp4")
    crop = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.75, y_max=0.75)
    region = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.5, y_max=0.5)
    plan = _visual_plan(
        (0.0, 1.0, crop, RedactionStyle.CROP),
        (0.0, 1.0, region, RedactionStyle.SOLID_FILL),
        duration_seconds=1.0,
    )
    requests: list[VisualWriterRequest] = []

    def writer_factory(request: VisualWriterRequest) -> CountingFrameWriter:
        requests.append(request)
        return writer

    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader,
        writer_factory=writer_factory,
    )

    renderer.render(Path("source.mp4"), writer.output, plan, lambda: False)

    assert requests[0].width == 16
    assert requests[0].height == 16
    assert writer.frames[0].shape == (16, 16, 3)
    assert np.all(writer.frames[0][0:8, 0:8] == 0)


def test_yuv420p_odd_crop_is_rejected_before_any_frame_is_read(
    tmp_path: Path,
) -> None:
    reader = CountingFrameReader(total_frames=1)
    output = tmp_path / "odd-crop.mp4"
    writer = CountingFrameWriter(output)
    crop = NormalizedBox(x_min=0.25, y_min=0.25, x_max=0.71875, y_max=0.75)
    plan = _visual_plan(
        (0.0, 1.0, crop, RedactionStyle.CROP),
        duration_seconds=1.0,
    )
    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader,
        writer_factory=lambda _: writer,
    )

    with pytest.raises(PrivacyPlanError) as error:
        renderer.render(Path("source.mp4"), output, plan, lambda: False)

    assert "even" in (error.value.internal_message or "")
    assert reader.frames_yielded == 0
    assert writer.frames_written == 0


def test_cancellation_terminates_streams_and_deletes_partial_output(
    tmp_path: Path,
) -> None:
    reader = CountingFrameReader(total_frames=10)
    output = tmp_path / "partial.mp4"
    writer = CountingFrameWriter(output)
    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader,
        writer_factory=lambda _: writer,
    )
    checks = 0

    def cancellation() -> bool:
        nonlocal checks
        checks += 1
        return checks > 2

    with pytest.raises(PrivacyCancelledError):
        renderer.render(Path("source.mp4"), output, _visual_plan(), cancellation)

    assert reader.terminated is True
    assert writer.terminated is True
    assert not output.exists()


def test_writer_failure_terminates_streams_and_deletes_partial_output(
    tmp_path: Path,
) -> None:
    reader = CountingFrameReader(total_frames=10)
    output = tmp_path / "partial.mp4"
    writer = CountingFrameWriter(output, fail_after=1)
    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader,
        writer_factory=lambda _: writer,
    )

    with pytest.raises(PrivacyMediaError):
        renderer.render(Path("source.mp4"), output, _visual_plan(), lambda: False)

    assert reader.terminated is True
    assert writer.terminated is True
    assert not output.exists()


def test_decoder_failure_reaps_timestamp_reader_and_remains_idempotent() -> None:
    class FakeStream:
        closed = False

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        def wait(self) -> int:
            return 7

    class FakeStderr:
        def diagnostic(self, *_: Path) -> str:
            return "bounded decoder failure"

    class FakeTimestampReader:
        terminated = 0

        def close(self) -> None:
            raise AssertionError("failed decoder must terminate timestamp reader")

        def terminate(self) -> None:
            self.terminated += 1

    reader: Any = object.__new__(_FFmpegFrameReader)
    reader._source = Path("source.mp4")
    reader._stdout = FakeStream()
    reader._process = FakeProcess()
    reader._stderr = FakeStderr()
    reader._timestamp_reader = FakeTimestampReader()
    reader._closed = False

    with pytest.raises(PrivacyMediaError) as error:
        reader.close()
    reader.terminate()

    assert "bounded decoder failure" in (error.value.internal_message or "")
    assert reader._stdout.closed is True
    assert reader._timestamp_reader.terminated == 1


def test_renderer_refuses_to_delete_or_overwrite_an_existing_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"keep-existing")
    reader = CountingFrameReader(total_frames=1)
    writer = CountingFrameWriter(output)
    renderer = VisualRedactionRenderer(
        reader_factory=lambda _: reader,
        writer_factory=lambda _: writer,
    )

    with pytest.raises(PrivacyMediaError):
        renderer.render(Path("source.mp4"), output, _visual_plan(), lambda: False)

    assert output.read_bytes() == b"keep-existing"
    assert reader.frames_yielded == 0
