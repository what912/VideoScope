"""Bounded, deterministic visual redaction primitives for Safe Sharing."""

from __future__ import annotations

import importlib
import math
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol, TypeAlias, cast

from videoscope.privacy.commands import (
    build_privacy_frame_timestamp_arguments,
    build_privacy_rawvideo_decode_arguments,
    build_privacy_rawvideo_encode_arguments,
)
from videoscope.privacy.errors import (
    PrivacyCancelledError,
    PrivacyMediaError,
    PrivacyPlanError,
)
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyAction,
    PrivacyActionKind,
    PrivacyPlan,
    RedactionStyle,
)
from videoscope.video.errors import MAX_DIAGNOSTIC_LENGTH, sanitize_diagnostic
from videoscope.video.probe import probe_video

Frame: TypeAlias = Any
TimedBox = tuple[float, NormalizedBox]
CancellationCallback = Callable[[], bool]
cv2: Any = importlib.import_module("cv2")
np: Any = importlib.import_module("numpy")


@dataclass(frozen=True)
class FrameStreamInfo:
    """The bounded information needed to decode and encode a frame stream."""

    width: int
    height: int
    frame_rate: float

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("frame stream dimensions must be positive")
        if not math.isfinite(self.frame_rate) or self.frame_rate <= 0:
            raise ValueError("frame stream rate must be positive and finite")


@dataclass(frozen=True)
class DecodedFrame:
    """One decoded BGR frame and its deterministic stream timestamp."""

    pixels: Frame
    timestamp_seconds: float

    def __post_init__(self) -> None:
        _validate_frame(self.pixels)
        if not math.isfinite(self.timestamp_seconds) or self.timestamp_seconds < 0:
            raise ValueError("decoded frame timestamp must be finite and non-negative")


@dataclass(frozen=True)
class VisualWriterRequest:
    """Output parameters passed once to an injected or FFmpeg writer."""

    output: Path
    width: int
    height: int
    frame_rate: float

    def __post_init__(self) -> None:
        FrameStreamInfo(self.width, self.height, self.frame_rate)


@dataclass(frozen=True)
class VisualRenderResult:
    """Bounded execution measurements for one visual render."""

    frames_read: int
    frames_written: int
    maximum_buffered_frames: int
    width: int
    height: int
    frame_rate: float


class FrameReader(Protocol):
    """Incremental decoded-frame source used by the renderer."""

    stream_info: FrameStreamInfo

    def frames(self) -> Iterator[DecodedFrame]: ...

    def close(self) -> None: ...

    def terminate(self) -> None: ...


class FrameWriter(Protocol):
    """Incremental encoded-frame sink used by the renderer."""

    def write(self, frame: Frame) -> None: ...

    def close(self) -> None: ...

    def terminate(self) -> None: ...


class FrameTimestampReader(Protocol):
    """Incremental real-PTS source paired with one rawvideo decoder."""

    def timestamps(self) -> Iterator[float]: ...

    def close(self) -> None: ...

    def terminate(self) -> None: ...


ReaderFactory = Callable[[Path], FrameReader]
WriterFactory = Callable[[VisualWriterRequest], FrameWriter]
TimestampReaderFactory = Callable[[Path], FrameTimestampReader]


class VisualRedactionRenderer:
    """Apply reviewed visual actions while buffering at most one video frame."""

    def __init__(
        self,
        *,
        reader_factory: ReaderFactory | None = None,
        writer_factory: WriterFactory | None = None,
        timestamp_reader_factory: TimestampReaderFactory | None = None,
        ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe",
    ) -> None:
        effective_timestamp_factory = timestamp_reader_factory or (
            lambda source: _FFprobeTimestampReader(source, ffprobe=ffprobe)
        )
        self._reader_factory = reader_factory or (
            lambda source: _FFmpegFrameReader(
                source,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                timestamp_reader_factory=effective_timestamp_factory,
            )
        )
        self._writer_factory = writer_factory or (
            lambda request: _FFmpegFrameWriter(request, ffmpeg=ffmpeg)
        )

    def render(
        self,
        source: Path,
        output: Path,
        plan: PrivacyPlan,
        cancellation: CancellationCallback,
    ) -> VisualRenderResult:
        """Stream source frames through crop and reviewed region redactions."""
        source = Path(source)
        output = Path(output)
        if source.resolve(strict=False) == output.resolve(strict=False):
            raise PrivacyPlanError("source read-only contract forbids in-place output")
        if output.exists() or output.is_symlink():
            raise PrivacyMediaError("visual output path already exists")
        output.parent.mkdir(parents=True, exist_ok=True)
        crop, visual_actions = _validated_visual_actions(plan)

        reader: FrameReader | None = None
        writer: FrameWriter | None = None
        frames_read = 0
        frames_written = 0
        maximum_buffered_frames = 0
        try:
            reader = self._reader_factory(source)
            source_info = reader.stream_info
            output_width, output_height = _output_dimensions(source_info, crop)
            _require_yuv420p_dimensions(output_width, output_height)
            writer = self._writer_factory(
                VisualWriterRequest(
                    output=output,
                    width=output_width,
                    height=output_height,
                    frame_rate=source_info.frame_rate,
                )
            )
            for decoded in reader.frames():
                if cancellation():
                    raise PrivacyCancelledError("visual redaction was cancelled")
                frames_read += 1
                maximum_buffered_frames = max(maximum_buffered_frames, 1)
                frame = _validate_decoded_dimensions(decoded.pixels, source_info)
                frame = _apply_crop(frame, crop)
                frame = _apply_actions(
                    frame,
                    decoded.timestamp_seconds,
                    visual_actions,
                    plan,
                    source_info,
                    crop,
                )
                writer.write(frame)
                frames_written += 1
            reader.close()
            writer.close()
        except (KeyboardInterrupt, SystemExit):
            _terminate_streams(reader, writer)
            _remove_incomplete_output(output)
            raise
        except (PrivacyCancelledError, PrivacyMediaError, PrivacyPlanError):
            _terminate_streams(reader, writer)
            _remove_incomplete_output(output)
            raise
        except Exception as exc:
            _terminate_streams(reader, writer)
            _remove_incomplete_output(output)
            raise PrivacyMediaError("visual redaction failed locally") from exc

        return VisualRenderResult(
            frames_read=frames_read,
            frames_written=frames_written,
            maximum_buffered_frames=maximum_buffered_frames,
            width=output_width,
            height=output_height,
            frame_rate=source_info.frame_rate,
        )


def interpolate_box(
    before: TimedBox,
    after: TimedBox,
    timestamp_seconds: float,
    guard_ratio: float,
    gap_requires_expansion: bool,
) -> NormalizedBox:
    """Linearly interpolate one normalized box between ordered keyframes."""
    before_time, before_box = before
    after_time, after_box = after
    if not all(
        math.isfinite(value) for value in (before_time, after_time, timestamp_seconds)
    ):
        raise ValueError("box timestamps must be finite")
    if after_time <= before_time:
        raise ValueError("box keyframes must have increasing timestamps")
    if timestamp_seconds <= before_time:
        return before_box
    if timestamp_seconds >= after_time:
        return after_box
    progress = (timestamp_seconds - before_time) / (after_time - before_time)
    interpolated = NormalizedBox(
        x_min=_lerp(before_box.x_min, after_box.x_min, progress),
        y_min=_lerp(before_box.y_min, after_box.y_min, progress),
        x_max=_lerp(before_box.x_max, after_box.x_max, progress),
        y_max=_lerp(before_box.y_max, after_box.y_max, progress),
    )
    return (
        expand_box(interpolated, guard_ratio)
        if gap_requires_expansion
        else interpolated
    )


def expand_box(box: NormalizedBox, guard_ratio: float) -> NormalizedBox:
    """Expand a box by a fraction of its dimensions and clamp to frame bounds."""
    if not math.isfinite(guard_ratio) or guard_ratio < 0:
        raise ValueError("guard_ratio must be a finite non-negative number")
    horizontal_guard = (box.x_max - box.x_min) * guard_ratio
    vertical_guard = (box.y_max - box.y_min) * guard_ratio
    return NormalizedBox(
        x_min=max(0.0, box.x_min - horizontal_guard),
        y_min=max(0.0, box.y_min - vertical_guard),
        x_max=min(1.0, box.x_max + horizontal_guard),
        y_max=min(1.0, box.y_max + vertical_guard),
    )


def apply_blur(frame: Frame, box: NormalizedBox, kernel_size: int) -> Frame:
    """Blur only the selected region while preserving every outside pixel."""
    _validate_frame(frame)
    if kernel_size < 3 or kernel_size % 2 == 0:
        raise ValueError("blur kernel size must be an odd integer of at least 3")
    x_min, y_min, x_max, y_max = _pixel_bounds(frame, box)
    region = frame[y_min:y_max, x_min:x_max]
    frame[y_min:y_max, x_min:x_max] = cv2.GaussianBlur(
        region,
        (kernel_size, kernel_size),
        sigmaX=0,
    )
    return frame


def apply_pixelate(frame: Frame, box: NormalizedBox, block_size: int) -> Frame:
    """Pixelate only the selected region while preserving every outside pixel."""
    _validate_frame(frame)
    if block_size < 2:
        raise ValueError("pixelation block size must be at least 2")
    x_min, y_min, x_max, y_max = _pixel_bounds(frame, box)
    region = frame[y_min:y_max, x_min:x_max]
    region_height, region_width = region.shape[:2]
    reduced = cv2.resize(
        region,
        (
            max(1, region_width // block_size),
            max(1, region_height // block_size),
        ),
        interpolation=cv2.INTER_AREA,
    )
    frame[y_min:y_max, x_min:x_max] = cv2.resize(
        reduced,
        (region_width, region_height),
        interpolation=cv2.INTER_NEAREST,
    )
    return frame


def apply_solid_fill(
    frame: Frame,
    box: NormalizedBox,
    color: tuple[int, int, int],
) -> Frame:
    """Fill only the selected region with one deterministic BGR color."""
    _validate_frame(frame)
    if len(color) != 3 or any(channel < 0 or channel > 255 for channel in color):
        raise ValueError("solid fill color must contain three byte values")
    x_min, y_min, x_max, y_max = _pixel_bounds(frame, box)
    frame[y_min:y_max, x_min:x_max] = np.asarray(color, dtype=np.uint8)
    return frame


def lockstep_decoded_frames(
    raw_frames: Iterator[Frame],
    timestamps: Iterator[float],
) -> Iterator[DecodedFrame]:
    """Pair one raw frame with one real PTS without collecting either stream."""
    previous_timestamp = -1.0
    for frame in raw_frames:
        try:
            timestamp = next(timestamps)
        except StopIteration as exc:
            raise PrivacyMediaError(
                "per-frame timestamp stream ended before rawvideo"
            ) from exc
        if timestamp < previous_timestamp:
            raise PrivacyMediaError("per-frame timestamps are not monotonic")
        decoded = DecodedFrame(pixels=frame, timestamp_seconds=timestamp)
        previous_timestamp = timestamp
        yield decoded
    try:
        next(timestamps)
    except StopIteration:
        return
    raise PrivacyMediaError("per-frame timestamp stream exceeds rawvideo")


def parse_frame_timestamp_line(line: bytes | str) -> float:
    """Parse the first ffprobe CSV column while ignoring bounded side-data."""
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    first_column = text.strip().split(",", maxsplit=1)[0].strip()
    try:
        timestamp = float(first_column)
    except ValueError as exc:
        raise PrivacyMediaError("ffprobe frame timestamp is missing") from exc
    if not math.isfinite(timestamp) or timestamp < 0:
        raise PrivacyMediaError("ffprobe frame timestamp is invalid")
    return timestamp


def _lerp(start: float, end: float, progress: float) -> float:
    return round(start + (end - start) * progress, 12)


def _validate_frame(frame: Frame) -> None:
    if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("frame must be a uint8 BGR image")
    if frame.shape[0] <= 0 or frame.shape[1] <= 0:
        raise ValueError("frame must have positive dimensions")


def _pixel_bounds(
    frame: Frame,
    box: NormalizedBox,
) -> tuple[int, int, int, int]:
    height, width = frame.shape[:2]
    x_min = max(0, min(width - 1, math.floor(box.x_min * width)))
    y_min = max(0, min(height - 1, math.floor(box.y_min * height)))
    x_max = max(x_min + 1, min(width, math.ceil(box.x_max * width)))
    y_max = max(y_min + 1, min(height, math.ceil(box.y_max * height)))
    return x_min, y_min, x_max, y_max


class _BoundedStderr:
    """Drain child stderr continuously while retaining only a bounded prefix."""

    def __init__(self, stream: BinaryIO | None) -> None:
        self._stream = stream
        self._retained = bytearray()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        if self._stream is None:
            return
        try:
            while chunk := self._stream.read(4096):
                remaining = MAX_DIAGNOSTIC_LENGTH - len(self._retained)
                if remaining > 0:
                    self._retained.extend(chunk[:remaining])
        except OSError:
            return

    def diagnostic(self, *sensitive_paths: Path) -> str:
        self._thread.join(timeout=2.0)
        text = bytes(self._retained).decode("utf-8", errors="replace")
        return sanitize_diagnostic(text, sensitive_paths=tuple(sensitive_paths))


class _FFprobeTimestampReader:
    """Stream one best-effort video PTS per decoded source frame."""

    _MAX_LINE_BYTES = 256

    def __init__(self, source: Path, *, ffprobe: str) -> None:
        self._source = source
        arguments = build_privacy_frame_timestamp_arguments(source, ffprobe=ffprobe)
        try:
            self._process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (FileNotFoundError, OSError) as exc:
            raise PrivacyMediaError("ffprobe timestamp reader could not start") from exc
        if self._process.stdout is None:
            self._process.terminate()
            raise PrivacyMediaError("ffprobe did not expose frame timestamps")
        self._stdout = cast(BinaryIO, self._process.stdout)
        self._stderr = _BoundedStderr(cast(BinaryIO | None, self._process.stderr))
        self._closed = False

    def timestamps(self) -> Iterator[float]:
        while line := self._stdout.readline(self._MAX_LINE_BYTES + 1):
            if len(line) > self._MAX_LINE_BYTES and not line.endswith(b"\n"):
                raise PrivacyMediaError("ffprobe frame timestamp line is too long")
            yield parse_frame_timestamp_line(line)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stdout.close()
        return_code = self._process.wait()
        if return_code != 0:
            diagnostic = self._stderr.diagnostic(self._source)
            raise PrivacyMediaError(f"ffprobe timestamp reader failed: {diagnostic}")

    def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        _terminate_process(self._process)
        self._stderr.diagnostic(self._source)


class _FFmpegFrameReader:
    """Decode BGR frames incrementally from one FFmpeg stdout pipe."""

    def __init__(
        self,
        source: Path,
        *,
        ffmpeg: str,
        ffprobe: str,
        timestamp_reader_factory: TimestampReaderFactory,
    ) -> None:
        self._source = source
        try:
            metadata = probe_video(source, ffprobe=ffprobe)
        except Exception as exc:
            raise PrivacyMediaError("visual source probe failed") from exc
        try:
            self.stream_info = FrameStreamInfo(
                width=metadata.width,
                height=metadata.height,
                frame_rate=metadata.average_frame_rate,
            )
        except ValueError as exc:
            raise PrivacyMediaError(
                "visual source stream information is invalid"
            ) from exc
        rotation_degrees = metadata.raw_probe.get("rotation_degrees", 0.0)
        if isinstance(rotation_degrees, (int, float)) and not isinstance(
            rotation_degrees, bool
        ):
            if float(rotation_degrees) != 0:
                raise PrivacyMediaError(
                    "visual source rotation metadata is not supported safely"
                )
        self._timestamp_reader = timestamp_reader_factory(source)
        arguments = build_privacy_rawvideo_decode_arguments(source, ffmpeg=ffmpeg)
        try:
            self._process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (FileNotFoundError, OSError) as exc:
            self._timestamp_reader.terminate()
            raise PrivacyMediaError("FFmpeg decoder could not start") from exc
        if self._process.stdout is None:
            self._process.terminate()
            self._timestamp_reader.terminate()
            raise PrivacyMediaError("FFmpeg decoder did not expose a frame stream")
        self._stdout = cast(BinaryIO, self._process.stdout)
        self._stderr = _BoundedStderr(cast(BinaryIO | None, self._process.stderr))
        self._closed = False

    def frames(self) -> Iterator[DecodedFrame]:
        return lockstep_decoded_frames(
            self._raw_frames(),
            self._timestamp_reader.timestamps(),
        )

    def _raw_frames(self) -> Iterator[Frame]:
        frame_size = self.stream_info.width * self.stream_info.height * 3
        while True:
            payload = _read_exact_frame(self._stdout, frame_size)
            if payload is None:
                return
            pixels: Frame = np.frombuffer(payload, dtype=np.uint8).reshape(
                self.stream_info.height,
                self.stream_info.width,
                3,
            )
            yield pixels.copy()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stdout.close()
        return_code = self._process.wait()
        if return_code != 0:
            self._timestamp_reader.terminate()
            diagnostic = self._stderr.diagnostic(self._source)
            raise PrivacyMediaError(f"FFmpeg decoder failed: {diagnostic}")
        self._timestamp_reader.close()

    def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        _terminate_process(self._process)
        self._timestamp_reader.terminate()
        self._stderr.diagnostic(self._source)


class _FFmpegFrameWriter:
    """Encode BGR frames incrementally through one FFmpeg stdin pipe."""

    def __init__(self, request: VisualWriterRequest, *, ffmpeg: str) -> None:
        self._request = request
        arguments = build_privacy_rawvideo_encode_arguments(
            request.output,
            width=request.width,
            height=request.height,
            frame_rate=request.frame_rate,
            ffmpeg=ffmpeg,
        )
        try:
            self._process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except (FileNotFoundError, OSError) as exc:
            raise PrivacyMediaError("FFmpeg encoder could not start") from exc
        if self._process.stdin is None:
            self._process.terminate()
            raise PrivacyMediaError("FFmpeg encoder did not expose a frame input")
        self._stdin = cast(BinaryIO, self._process.stdin)
        self._stderr = _BoundedStderr(cast(BinaryIO | None, self._process.stderr))
        self._closed = False

    def write(self, frame: Frame) -> None:
        _validate_frame(frame)
        if frame.shape[:2] != (self._request.height, self._request.width):
            raise PrivacyMediaError("visual frame dimensions changed during encoding")
        try:
            self._stdin.write(frame.tobytes(order="C"))
        except (BrokenPipeError, OSError) as exc:
            diagnostic = self._stderr.diagnostic(self._request.output)
            raise PrivacyMediaError(f"FFmpeg encoder failed: {diagnostic}") from exc

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stdin.close()
        return_code = self._process.wait()
        if return_code != 0:
            diagnostic = self._stderr.diagnostic(self._request.output)
            raise PrivacyMediaError(f"FFmpeg encoder failed: {diagnostic}")

    def terminate(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._stdin.close()
        except OSError:
            pass
        _terminate_process(self._process)
        self._stderr.diagnostic(self._request.output)


def _read_exact_frame(stream: BinaryIO, size: int) -> bytes | None:
    payload = bytearray()
    while len(payload) < size:
        chunk = stream.read(size - len(payload))
        if not chunk:
            if not payload:
                return None
            raise PrivacyMediaError("FFmpeg decoder returned an incomplete frame")
        payload.extend(chunk)
    return bytes(payload)


def _validated_visual_actions(
    plan: PrivacyPlan,
) -> tuple[PrivacyAction | None, tuple[PrivacyAction, ...]]:
    if plan.duration_seconds is None:
        raise PrivacyPlanError("visual rendering requires the source duration")
    crops = tuple(
        action for action in plan.actions if action.kind is PrivacyActionKind.CROP
    )
    if len(crops) > 1:
        raise PrivacyPlanError("visual rendering accepts only one crop action")
    crop = crops[0] if crops else None
    if crop is not None and (
        crop.box is None
        or crop.start_seconds != 0.0
        or crop.end_seconds != plan.duration_seconds
    ):
        raise PrivacyPlanError("crop must be one static full-duration rectangle")

    actions = tuple(
        action
        for action in plan.actions
        if action.kind is PrivacyActionKind.VISUAL_REDACTION
    )
    for action in actions:
        if action.box is None:
            raise PrivacyPlanError("visual redaction action requires a box")
        raw_style = action.parameters.get("style")
        if not isinstance(raw_style, str):
            raise PrivacyPlanError("visual redaction action has an invalid style")
        try:
            style = RedactionStyle(raw_style)
        except ValueError as exc:
            raise PrivacyPlanError(
                "visual redaction action has an invalid style"
            ) from exc
        if style not in {
            RedactionStyle.BLUR,
            RedactionStyle.PIXELATE,
            RedactionStyle.SOLID_FILL,
        }:
            raise PrivacyPlanError("visual redaction action has a nonvisual style")
    return crop, actions


def _output_dimensions(
    info: FrameStreamInfo,
    crop: PrivacyAction | None,
) -> tuple[int, int]:
    if crop is None:
        return info.width, info.height
    assert crop.box is not None
    x_min, y_min, x_max, y_max = _box_bounds(
        info.width,
        info.height,
        crop.box,
    )
    return x_max - x_min, y_max - y_min


def _require_yuv420p_dimensions(width: int, height: int) -> None:
    if width % 2 != 0 or height % 2 != 0:
        raise PrivacyPlanError("yuv420p visual output requires even width and height")


def _validate_decoded_dimensions(frame: Frame, info: FrameStreamInfo) -> Frame:
    _validate_frame(frame)
    if frame.shape[:2] != (info.height, info.width):
        raise PrivacyMediaError("decoded frame dimensions changed during rendering")
    return frame


def _apply_crop(frame: Frame, crop: PrivacyAction | None) -> Frame:
    if crop is None:
        return frame
    assert crop.box is not None
    x_min, y_min, x_max, y_max = _pixel_bounds(frame, crop.box)
    return np.ascontiguousarray(frame[y_min:y_max, x_min:x_max])


def _apply_actions(
    frame: Frame,
    timestamp_seconds: float,
    actions: tuple[PrivacyAction, ...],
    plan: PrivacyPlan,
    source_info: FrameStreamInfo,
    crop: PrivacyAction | None,
) -> Frame:
    crop_box = crop.box if crop is not None else None
    for action in actions:
        if not (action.start_seconds <= timestamp_seconds < action.end_seconds):
            continue
        assert action.box is not None
        guard_pixels = action.parameters.get(
            "guard_pixels",
            plan.effective_config.guard_pixels,
        )
        if isinstance(guard_pixels, bool) or not isinstance(guard_pixels, int):
            raise PrivacyPlanError("visual guard pixels must be an integer")
        action_box = _action_box_at_timestamp(
            action,
            timestamp_seconds,
            plan,
        )
        guarded = _expand_box_pixels(
            action_box,
            guard_pixels,
            source_info.width,
            source_info.height,
        )
        transformed = _box_relative_to_crop(guarded, crop_box)
        if transformed is None:
            continue
        raw_style = action.parameters["style"]
        if not isinstance(raw_style, str):
            raise PrivacyPlanError("visual redaction action has an invalid style")
        style = RedactionStyle(raw_style)
        if style is RedactionStyle.BLUR:
            frame = apply_blur(
                frame,
                transformed,
                plan.effective_config.blur_kernel_size,
            )
        elif style is RedactionStyle.PIXELATE:
            frame = apply_pixelate(
                frame,
                transformed,
                plan.effective_config.pixelate_block_size,
            )
        else:
            frame = apply_solid_fill(
                frame,
                transformed,
                plan.effective_config.solid_fill_color,
            )
    return frame


def _action_box_at_timestamp(
    action: PrivacyAction,
    timestamp_seconds: float,
    plan: PrivacyPlan,
) -> NormalizedBox:
    assert action.box is not None
    raw_keyframes = action.parameters.get("keyframes")
    if not isinstance(raw_keyframes, (list, tuple)) or not raw_keyframes:
        return action.box
    keyframes: list[TimedBox] = []
    for raw_keyframe in raw_keyframes:
        if not isinstance(raw_keyframe, dict):
            raise PrivacyPlanError("visual action keyframe must be an object")
        raw_timestamp = raw_keyframe.get("timestamp_seconds")
        raw_box = raw_keyframe.get("box")
        if (
            isinstance(raw_timestamp, bool)
            or not isinstance(raw_timestamp, (int, float))
            or not isinstance(raw_box, dict)
        ):
            raise PrivacyPlanError("visual action keyframe is invalid")
        try:
            box = NormalizedBox.model_validate(raw_box)
        except ValueError as exc:
            raise PrivacyPlanError("visual action keyframe box is invalid") from exc
        keyframes.append((float(raw_timestamp), box))
    keyframes.sort(key=lambda item: item[0])
    if len(keyframes) == 1 or timestamp_seconds <= keyframes[0][0]:
        return keyframes[0][1]
    if timestamp_seconds >= keyframes[-1][0]:
        return keyframes[-1][1]
    for before, after in zip(keyframes, keyframes[1:], strict=True):
        if before[0] <= timestamp_seconds <= after[0]:
            return interpolate_box(
                before,
                after,
                timestamp_seconds,
                plan.effective_config.interpolation_guard_ratio,
                plan.effective_config.expand_track_gaps,
            )
    raise PrivacyPlanError("visual action keyframes do not cover the frame timestamp")


def _expand_box_pixels(
    box: NormalizedBox,
    guard_pixels: int,
    width: int,
    height: int,
) -> NormalizedBox:
    if guard_pixels < 0:
        raise PrivacyPlanError("visual guard pixels must not be negative")
    return NormalizedBox(
        x_min=max(0.0, box.x_min - guard_pixels / width),
        y_min=max(0.0, box.y_min - guard_pixels / height),
        x_max=min(1.0, box.x_max + guard_pixels / width),
        y_max=min(1.0, box.y_max + guard_pixels / height),
    )


def _box_relative_to_crop(
    box: NormalizedBox,
    crop: NormalizedBox | None,
) -> NormalizedBox | None:
    if crop is None:
        return box
    x_min = max(box.x_min, crop.x_min)
    y_min = max(box.y_min, crop.y_min)
    x_max = min(box.x_max, crop.x_max)
    y_max = min(box.y_max, crop.y_max)
    if x_min >= x_max or y_min >= y_max:
        return None
    crop_width = crop.x_max - crop.x_min
    crop_height = crop.y_max - crop.y_min
    return NormalizedBox(
        x_min=(x_min - crop.x_min) / crop_width,
        y_min=(y_min - crop.y_min) / crop_height,
        x_max=(x_max - crop.x_min) / crop_width,
        y_max=(y_max - crop.y_min) / crop_height,
    )


def _box_bounds(
    width: int,
    height: int,
    box: NormalizedBox,
) -> tuple[int, int, int, int]:
    placeholder: Frame = np.empty((height, width, 3), dtype=np.uint8)
    return _pixel_bounds(placeholder, box)


def _terminate_streams(
    reader: FrameReader | None,
    writer: FrameWriter | None,
) -> None:
    if reader is not None:
        try:
            reader.terminate()
        except Exception:
            pass
    if writer is not None:
        try:
            writer.terminate()
        except Exception:
            pass


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)


def _remove_incomplete_output(output: Path) -> None:
    try:
        if output.exists() or output.is_symlink():
            output.unlink()
    except OSError as exc:
        raise PrivacyMediaError(
            "incomplete visual output could not be removed"
        ) from exc


__all__ = [
    "CancellationCallback",
    "DecodedFrame",
    "Frame",
    "FrameReader",
    "FrameStreamInfo",
    "FrameTimestampReader",
    "FrameWriter",
    "TimedBox",
    "TimestampReaderFactory",
    "VisualRedactionRenderer",
    "VisualRenderResult",
    "VisualWriterRequest",
    "apply_blur",
    "apply_pixelate",
    "apply_solid_fill",
    "expand_box",
    "interpolate_box",
    "lockstep_decoded_frames",
    "parse_frame_timestamp_line",
]
