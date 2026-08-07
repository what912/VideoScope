"""Generate deterministic, copyright-free micro video fixtures with FFmpeg."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from math import isfinite
from pathlib import Path
from typing import BinaryIO, cast

from PIL import Image, ImageDraw, ImageFont

WIDTH = 320
HEIGHT = 180
FRAME_RATE = 10
DURATION_SECONDS = 6.0
TIME_TOLERANCE_SECONDS = 0.11
FILE_CHUNK_BYTES = 1024 * 1024
ZERO_CHUNK_BYTES = 64 * 1024
PUBLISH_FRAME_RATE = 12
PUBLISH_DURATION_SECONDS = 4.0
PUBLISH_AUDIO_FREQUENCY_HZ = 440
PRIVACY_FRAME_RATE = 10
PRIVACY_DURATION_SECONDS = 4.0
PRIVACY_AUDIO_FREQUENCY_HZ = 660
PRIVACY_QR_PAYLOAD = "VIDEOSCOPE-PRIVATE-912"
CONTENT_FRAME_RATE = 10
CONTENT_DURATION_SECONDS = 12.0
CONTENT_AUDIO_FREQUENCY_HZ = 520
PRIVACY_TEXT_CASES = (
    ("phone", "zh-CN", "电话 TEL 138-0013-8000", True, 0.0, 0.8),
    ("email", "en", "邮箱 EMAIL alice@example.test", True, 0.8, 1.6),
    ("address", "zh-CN", "地址 ADDR 上海浦东 88号", True, 1.6, 2.4),
    ("account", "en", "账号 ACCT CN-912-PRIVATE", True, 2.4, 3.2),
    ("ordinary", "zh-CN", "今日天气晴朗 CREATIVE DEMO", False, 3.2, 4.0),
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIRECTORY = REPOSITORY_ROOT / "tests" / "fixtures" / "generated"
DEFAULT_MANIFEST_PATH = REPOSITORY_ROOT / "tests" / "fixtures" / "manifest.json"


class FixtureFactoryError(RuntimeError):
    """Actionable failure while generating or validating video fixtures."""


@dataclass(frozen=True, slots=True)
class ExpectedRange:
    """Expected anomaly interval for a generated fixture."""

    start_seconds: float
    end_seconds: float


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """Declarative FFmpeg and expectation specification for one fixture."""

    filename: str
    input_args: tuple[str, ...]
    expected_anomaly_type: str
    expected_ranges: tuple[ExpectedRange, ...] = ()
    expected_scene_cuts_seconds: tuple[float, ...] = ()
    video_filter: str | None = None
    filter_complex: str | None = None
    map_label: str | None = None
    text_mode: str | None = None
    audio_map_label: str | None = None
    frame_rate: int = FRAME_RATE
    duration_seconds: float = DURATION_SECONDS
    gop_size: int | None = None


@dataclass(frozen=True, slots=True)
class PrivacyFixtureSpec:
    """One local-only Safe Sharing acceptance fixture."""

    filename: str
    mode: str
    has_audio: bool = False


@dataclass(frozen=True, slots=True)
class RescueFixtureSpec:
    """One local-only damaged-media fixture for the Video Rescue workflow."""

    filename: str
    mode: str
    expected_damage_kinds: tuple[str, ...]
    expected_ranges: tuple[ExpectedRange, ...] = ()
    has_audio: bool = True


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Validated subset of ffprobe output."""

    duration_seconds: float
    width: int
    height: int
    frame_rate: float
    has_audio: bool = False


def fixture_specs() -> tuple[FixtureSpec, ...]:
    """Return all fixture definitions in stable filename order."""
    source = (
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FRAME_RATE}:duration=6",
    )
    return (
        FixtureSpec(
            filename="black_segment.mp4",
            input_args=source,
            expected_anomaly_type="black_segment",
            expected_ranges=(ExpectedRange(2.0, 3.5),),
            video_filter=(
                "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(t,2,3.5)'"
            ),
        ),
        FixtureSpec(
            filename="blur_segment.mp4",
            input_args=source,
            expected_anomaly_type="blur_segment",
            expected_ranges=(ExpectedRange(2.0, 4.0),),
            video_filter=(
                "boxblur=luma_radius=12:luma_power=2:"
                "chroma_radius=6:chroma_power=1:enable='between(t,2,4)'"
            ),
        ),
        FixtureSpec(
            filename="clean_motion.mp4",
            input_args=source,
            expected_anomaly_type="none",
        ),
        FixtureSpec(
            filename="flicker_segment.mp4",
            input_args=source,
            expected_anomaly_type="flicker_segment",
            expected_ranges=(ExpectedRange(2.0, 4.0),),
            video_filter=(
                "eq=brightness='if(between(t,2,4),"
                "if(mod(n,2),0.45,-0.45),0)':eval=frame"
            ),
        ),
        FixtureSpec(
            filename="freeze_segment.mp4",
            input_args=source,
            expected_anomaly_type="freeze_segment",
            expected_ranges=(ExpectedRange(2.0, 4.0),),
            filter_complex=(
                "[0:v]split=3[pre_src][freeze_src][post_src];"
                "[pre_src]trim=start=0:end=2,setpts=PTS-STARTPTS[pre];"
                "[freeze_src]trim=start=2:end=2.1,setpts=PTS-STARTPTS,"
                "tpad=stop_mode=clone:stop_duration=1.9[freeze];"
                "[post_src]trim=start=4:end=6,setpts=PTS-STARTPTS[post];"
                "[pre][freeze][post]concat=n=3:v=1:a=0[out]"
            ),
            map_label="[out]",
        ),
        FixtureSpec(
            filename="scene_cut.mp4",
            input_args=(
                "-f",
                "lavfi",
                "-i",
                f"color=c=red:size={WIDTH}x{HEIGHT}:rate={FRAME_RATE}:duration=2",
                "-f",
                "lavfi",
                "-i",
                f"color=c=lime:size={WIDTH}x{HEIGHT}:rate={FRAME_RATE}:duration=2",
                "-f",
                "lavfi",
                "-i",
                f"color=c=blue:size={WIDTH}x{HEIGHT}:rate={FRAME_RATE}:duration=2",
            ),
            expected_anomaly_type="none",
            expected_scene_cuts_seconds=(2.0, 4.0),
            filter_complex="[0:v][1:v][2:v]concat=n=3:v=1:a=0[out]",
            map_label="[out]",
        ),
        FixtureSpec(
            filename="stable_text.mp4",
            input_args=(),
            expected_anomaly_type="none",
            text_mode="stable",
        ),
        FixtureSpec(
            filename="changing_text.mp4",
            input_args=(),
            expected_anomaly_type="text_stability",
            expected_ranges=(ExpectedRange(2.0, 4.0),),
            text_mode="changing",
        ),
    )


def publish_fixture_spec() -> FixtureSpec:
    """Return the separate audio/video fixture used by Publish Ready tests."""
    return FixtureSpec(
        filename="publish_av.mp4",
        input_args=(
            "-f",
            "lavfi",
            "-i",
            (
                f"testsrc2=size={WIDTH}x{HEIGHT}:rate={PUBLISH_FRAME_RATE}:"
                f"duration={PUBLISH_DURATION_SECONDS:g}"
            ),
            "-f",
            "lavfi",
            "-i",
            (
                f"sine=frequency={PUBLISH_AUDIO_FREQUENCY_HZ}:sample_rate=48000:"
                f"duration={PUBLISH_DURATION_SECONDS:g}"
            ),
        ),
        expected_anomaly_type="none",
        audio_map_label="1:a:0",
        frame_rate=PUBLISH_FRAME_RATE,
        duration_seconds=PUBLISH_DURATION_SECONDS,
    )


def privacy_fixture_specs() -> tuple[PrivacyFixtureSpec, ...]:
    """Return the five Safe Sharing fixtures in stable filename order."""
    return (
        PrivacyFixtureSpec("privacy_clean.mp4", "clean"),
        PrivacyFixtureSpec("privacy_manual_visual.mp4", "manual_visual"),
        PrivacyFixtureSpec("privacy_qr.mp4", "qr"),
        PrivacyFixtureSpec("privacy_tags_av.mp4", "tags", has_audio=True),
        PrivacyFixtureSpec("privacy_text.mp4", "text"),
    )


def rescue_fixture_specs() -> tuple[RescueFixtureSpec, ...]:
    """Return stable, offline Video Rescue fixture declarations."""
    return (
        RescueFixtureSpec("rescue_clean_av.mp4", "clean", ("decodable",)),
        RescueFixtureSpec(
            "rescue_missing_audio.mp4",
            "missing_audio",
            ("missing_stream",),
            has_audio=False,
        ),
        RescueFixtureSpec("rescue_low_loudness.mp4", "low_loudness", ("low_loudness",)),
        RescueFixtureSpec(
            "rescue_fixed_av_offset.mp4", "fixed_av_offset", ("fixed_av_offset",)
        ),
        RescueFixtureSpec(
            "rescue_dark_noise.mp4", "dark_noise", ("dark", "video_noise")
        ),
        RescueFixtureSpec("rescue_soft_detail.mp4", "soft_detail", ("soft_detail",)),
        RescueFixtureSpec("rescue_flicker.mp4", "flicker", ("flicker",)),
        RescueFixtureSpec("rescue_shake.mp4", "shake", ("shake",)),
        RescueFixtureSpec(
            "rescue_tail_damaged.mp4",
            "payload_zeroing",
            ("undecodable",),
            (ExpectedRange(5.0, 6.0),),
        ),
        RescueFixtureSpec(
            "rescue_middle_damaged.mp4",
            "payload_zeroing",
            ("undecodable",),
            (ExpectedRange(2.0, 3.0),),
        ),
    )


def combined_rescue_fixture_spec() -> RescueFixtureSpec:
    """Return the local derivative used for mapping-safe deflicker acceptance."""
    return RescueFixtureSpec(
        "rescue_flicker_middle_damaged.mp4",
        "flicker_payload_zeroing",
        ("undecodable", "flicker"),
        (ExpectedRange(2.0, 3.0),),
    )


def content_fixture_specs() -> tuple[FixtureSpec, ...]:
    """Return purpose-built Long Video to Useful Content fixtures."""
    meeting_inputs: list[str] = []
    for video_source in (
        f"testsrc2=size={WIDTH}x{HEIGHT}:rate={CONTENT_FRAME_RATE}:duration=4",
        f"color=c=0x15191d:size={WIDTH}x{HEIGHT}:rate={CONTENT_FRAME_RATE}:duration=4",
        f"smptebars=size={WIDTH}x{HEIGHT}:rate={CONTENT_FRAME_RATE}:duration=4",
    ):
        meeting_inputs.extend(("-f", "lavfi", "-i", video_source))
    for audio_source in (
        f"sine=frequency={CONTENT_AUDIO_FREQUENCY_HZ}:sample_rate=48000:duration=4",
        "anullsrc=r=48000:cl=mono:d=4",
        (
            f"sine=frequency={CONTENT_AUDIO_FREQUENCY_HZ + 120}:"
            "sample_rate=48000:duration=4"
        ),
    ):
        meeting_inputs.extend(("-f", "lavfi", "-i", audio_source))
    meeting_filter = (
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[vout];"
        "[3:a][4:a][5:a]concat=n=3:v=0:a=1[aout]"
    )

    tutorial_inputs: list[str] = []
    for color in ("0x164e63", "0x713f12", "0x4c1d95"):
        tutorial_inputs.extend(
            (
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:size={WIDTH}x{HEIGHT}:rate={CONTENT_FRAME_RATE}:duration=4",
            )
        )
    tutorial_inputs.extend(
        (
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=700:sample_rate=48000:duration={CONTENT_DURATION_SECONDS:g}",
        )
    )

    return (
        FixtureSpec(
            filename="content_join_regression.mp4",
            input_args=tuple(tutorial_inputs),
            expected_anomaly_type="content_join_regression",
            filter_complex="[0:v][1:v][2:v]concat=n=3:v=1:a=0[vout]",
            map_label="[vout]",
            audio_map_label="3:a:0",
            frame_rate=CONTENT_FRAME_RATE,
            duration_seconds=CONTENT_DURATION_SECONDS,
            gop_size=1,
        ),
        FixtureSpec(
            filename="content_locked_context.mp4",
            input_args=tuple(meeting_inputs),
            expected_anomaly_type="content_locked_context",
            filter_complex=meeting_filter,
            map_label="[vout]",
            audio_map_label="[aout]",
            frame_rate=CONTENT_FRAME_RATE,
            duration_seconds=CONTENT_DURATION_SECONDS,
            gop_size=1,
        ),
        FixtureSpec(
            filename="content_meeting_structure.mp4",
            input_args=tuple(meeting_inputs),
            expected_anomaly_type="content_meeting_structure",
            filter_complex=meeting_filter,
            map_label="[vout]",
            audio_map_label="[aout]",
            frame_rate=CONTENT_FRAME_RATE,
            duration_seconds=CONTENT_DURATION_SECONDS,
            gop_size=1,
        ),
        FixtureSpec(
            filename="content_tutorial_chapters.mp4",
            input_args=tuple(tutorial_inputs),
            expected_anomaly_type="content_tutorial_chapters",
            filter_complex="[0:v][1:v][2:v]concat=n=3:v=1:a=0[vout]",
            map_label="[vout]",
            audio_map_label="3:a:0",
            frame_rate=CONTENT_FRAME_RATE,
            duration_seconds=CONTENT_DURATION_SECONDS,
            gop_size=1,
        ),
    )


def _fixture_font(
    size: int = 30,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Pillow's bundled deterministic font without system font lookup."""
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # pragma: no cover - compatibility with older Pillow
        return ImageFont.load_default()


def generate_text_frames(
    output_directory: Path,
    *,
    mode: str,
) -> tuple[Path, ...]:
    """Create a deterministic local image sequence for OCR video fixtures."""
    if mode not in {"stable", "changing"}:
        raise ValueError(f"Unsupported text fixture mode: {mode}")
    output_directory.mkdir(parents=True, exist_ok=True)
    font = _fixture_font()
    frame_paths: list[Path] = []
    total_frames = round(DURATION_SECONDS * FRAME_RATE)
    changing_labels = ("VIDEO SCOPE", "VIDE0 SCOPE", "VIDEO SC0PE", "V1DEO SCOPE")
    for frame_index in range(total_frames):
        timestamp = frame_index / FRAME_RATE
        label = "VIDEO SCOPE"
        if mode == "changing" and 2.0 <= timestamp < 4.0:
            label = changing_labels[((frame_index - 20) // 2) % len(changing_labels)]
            if 3.0 <= timestamp < 3.2:
                label = ""
        image = Image.new("RGB", (WIDTH, HEIGHT), (18, 24, 36))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 126, WIDTH, HEIGHT), fill=(4, 7, 12))
        if label:
            box = draw.textbbox((0, 0), label, font=font)
            text_width = box[2] - box[0]
            text_height = box[3] - box[1]
            x = (WIDTH - text_width) // 2
            y = 139 + (34 - text_height) // 2
            draw.text(
                (x, y),
                label,
                font=font,
                fill=(248, 250, 252),
                stroke_width=1,
                stroke_fill=(248, 250, 252),
            )
        frame_path = output_directory / f"frame_{frame_index:03d}.png"
        image.save(frame_path, format="PNG", optimize=False)
        frame_paths.append(frame_path)
    return tuple(frame_paths)


def _encode_local_qr() -> Image.Image:
    """Encode the deterministic QR fixture with the installed OpenCV build."""
    try:
        cv2 = importlib.import_module("cv2")
    except ImportError as exc:  # pragma: no cover - base dependency is installed
        raise FixtureFactoryError(
            "OpenCV is required to generate privacy_qr.mp4; install "
            "opencv-python-headless 4.5 or newer."
        ) from exc
    factory = getattr(cv2, "QRCodeEncoder_create", None)
    if factory is None:
        version = getattr(cv2, "__version__", "unknown")
        raise FixtureFactoryError(
            "The installed OpenCV build cannot encode QR fixtures "
            f"(detected version {version}); install an OpenCV build exposing "
            "QRCodeEncoder_create (OpenCV 4.5 or newer)."
        )
    try:
        encoded = factory().encode(PRIVACY_QR_PAYLOAD)
        qr = Image.fromarray(encoded).convert("L")
    except Exception as exc:
        version = getattr(cv2, "__version__", "unknown")
        raise FixtureFactoryError(
            "The installed OpenCV QR encoder could not create the local fixture "
            f"(detected version {version}); upgrade opencv-python-headless to a "
            "current build exposing a working QRCodeEncoder_create implementation."
        ) from exc
    return qr.resize((80, 80), Image.Resampling.NEAREST).convert("RGB")


def generate_privacy_frames(
    output_directory: Path,
    *,
    mode: str,
) -> tuple[Path, ...]:
    """Create deterministic local frames for visual Safe Sharing fixtures."""
    if mode not in {"manual_visual", "qr", "text"}:
        raise ValueError(f"Unsupported privacy fixture mode: {mode}")
    output_directory.mkdir(parents=True, exist_ok=True)
    font = _fixture_font(16)
    qr = _encode_local_qr() if mode == "qr" else None
    frame_paths: list[Path] = []
    total_frames = round(PRIVACY_DURATION_SECONDS * PRIVACY_FRAME_RATE)
    for frame_index in range(total_frames):
        timestamp = frame_index / PRIVACY_FRAME_RATE
        image = Image.new("RGB", (WIDTH, HEIGHT), (14, 22, 32))
        draw = ImageDraw.Draw(image)
        offset = (frame_index * 3) % WIDTH
        draw.rectangle((offset - 28, 18, offset + 28, 72), fill=(23, 128, 153))
        draw.line((0, 105, WIDTH, 105), fill=(62, 78, 92), width=2)
        if mode == "manual_visual" and (
            0.4 <= timestamp < 1.4 or 2.0 <= timestamp < 3.6
        ):
            # Pillow rectangles are inclusive; these coordinates implement the
            # manifest's half-open [32, 256) x [27, 135) review region.
            draw.rectangle((32, 27, 255, 134), fill=(31, 44, 58))
            if timestamp < 1.4:
                face_x = 44 + (frame_index - 4) * 6
            else:
                face_x = 120 + (frame_index - 20) * 6
            draw.ellipse(
                (face_x, 39, face_x + 45, 110),
                fill=(224, 169, 119),
                outline=(255, 225, 187),
                width=2,
            )
            draw.ellipse((face_x + 10, 60, face_x + 15, 66), fill=(20, 27, 33))
            draw.ellipse((face_x + 30, 60, face_x + 35, 66), fill=(20, 27, 33))
            draw.arc(
                (face_x + 12, 72, face_x + 34, 94),
                15,
                165,
                fill=(92, 47, 39),
                width=2,
            )
        elif mode == "qr":
            assert qr is not None
            if timestamp < 1.0:
                size, x, y = 80, 20, 20
            elif timestamp < 2.0:
                size, x, y = 80, 24 + (frame_index - 10) * 16, 60
            elif timestamp < 3.0:
                size = 56 + (frame_index - 20) * 4
                x, y = (WIDTH - size) // 2, (HEIGHT - size) // 2
            else:
                size, x, y = 80, 236, 96
            rendered = qr.resize((size, size), Image.Resampling.NEAREST)
            image.paste(rendered, (x, y))
        elif mode == "text":
            draw.rectangle((8, 124, 311, 171), fill=(2, 6, 12))
            label = next(
                value
                for _kind, _language, value, _sensitive, start, end in (
                    PRIVACY_TEXT_CASES
                )
                if start <= timestamp < end
            )
            draw.text(
                (12, 137),
                label,
                font=font,
                fill=(255, 255, 255),
            )
        frame_path = output_directory / f"frame_{frame_index:03d}.png"
        image.save(frame_path, format="PNG", optimize=False)
        frame_paths.append(frame_path)
    return tuple(frame_paths)


def build_ffmpeg_command(
    *,
    ffmpeg: str,
    spec: FixtureSpec,
    output_path: Path,
) -> list[str]:
    """Build one shell-free FFmpeg command."""
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *spec.input_args,
    ]
    if spec.filter_complex is not None:
        command.extend(["-filter_complex", spec.filter_complex])
    elif spec.video_filter is not None:
        command.extend(["-vf", spec.video_filter])

    command.extend(["-map", spec.map_label or "0:v:0"])
    if spec.audio_map_label is None:
        command.append("-an")
    else:
        command.extend(["-map", spec.audio_map_label])
    command.extend(
        [
            "-c:v",
            "mpeg4",
            "-q:v",
            "3",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(spec.frame_rate),
            "-t",
            f"{spec.duration_seconds:g}",
        ]
    )
    if spec.gop_size is not None:
        command.extend(["-g", str(spec.gop_size)])
    if spec.audio_map_label is not None:
        command.extend(
            [
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-flags:a",
                "+bitexact",
                "-shortest",
            ]
        )
    command.extend(
        [
            "-threads",
            "1",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-map_metadata",
            "-1",
            "-metadata",
            "creation_time=1970-01-01T00:00:00Z",
            "-movflags",
            "+faststart",
            "-video_track_timescale",
            str(spec.frame_rate * 1000),
            str(output_path),
        ]
    )
    return command


def build_privacy_ffmpeg_command(
    *,
    ffmpeg: str,
    spec: PrivacyFixtureSpec,
    output_path: Path,
    frame_directory: Path | None = None,
    asset_directory: Path | None = None,
) -> list[str]:
    """Build one shell-free command for a Safe Sharing fixture."""
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if frame_directory is None:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                (
                    f"testsrc2=size={WIDTH}x{HEIGHT}:rate={PRIVACY_FRAME_RATE}:"
                    f"duration={PRIVACY_DURATION_SECONDS:g}"
                ),
            ]
        )
    else:
        command.extend(
            [
                "-framerate",
                str(PRIVACY_FRAME_RATE),
                "-i",
                str(frame_directory / "frame_%03d.png"),
            ]
        )
    if spec.has_audio:
        command.extend(
            [
                "-f",
                "lavfi",
                "-i",
                (
                    f"sine=frequency={PRIVACY_AUDIO_FREQUENCY_HZ}:"
                    "sample_rate=48000:"
                    f"duration={PRIVACY_DURATION_SECONDS:g}"
                ),
            ]
        )
    if spec.mode == "tags":
        if asset_directory is None:
            raise ValueError("tag fixture requires a local asset directory")
        command.extend(
            [
                "-i",
                str(asset_directory / "attached-cover.jpg"),
                "-f",
                "ffmetadata",
                "-i",
                str(asset_directory / "private-metadata.txt"),
            ]
        )
    command.extend(["-map", "0:v:0"])
    if spec.has_audio:
        command.extend(["-map", "1:a:0"])
    else:
        command.append("-an")
    command.extend(
        [
            "-c:v",
            "mpeg4",
            "-q:v",
            "3",
            "-pix_fmt:v:0",
            "yuv420p",
            "-r:v:0",
            str(PRIVACY_FRAME_RATE),
            "-t",
            f"{PRIVACY_DURATION_SECONDS:g}",
        ]
    )
    if spec.has_audio:
        command.extend(["-c:a", "aac", "-b:a", "128k"])
        if spec.mode != "tags":
            command.append("-shortest")
    command.extend(["-threads", "1", "-fflags", "+bitexact", "-flags:v", "+bitexact"])
    if spec.mode == "tags":
        command.extend(
            [
                "-map",
                "2:v:0",
                "-map_metadata",
                "3",
                "-map_chapters",
                "3",
                "-c:v:1",
                "mjpeg",
                "-disposition:v:1",
                "attached_pic",
                "-metadata:s:v:0",
                "title=PRIVATE VIDEO STREAM",
                "-metadata:s:v:0",
                "handler_name=PRIVATE VIDEO STREAM",
                "-metadata:s:a:0",
                "title=PRIVATE AUDIO STREAM",
                "-metadata:s:a:0",
                "handler_name=PRIVATE AUDIO STREAM",
                "-metadata:s:v:1",
                "title=PRIVATE ATTACHED PICTURE",
                "-metadata:s:v:1",
                "handler_name=PRIVATE ATTACHED PICTURE",
                "-metadata:s:v:1",
                "filename=private-cover.jpg",
                "-metadata:s:v:1",
                "mimetype=image/jpeg",
            ]
        )
    else:
        command.extend(
            ["-map_metadata", "-1", "-metadata", "creation_time=1970-01-01T00:00:00Z"]
        )
    command.extend(
        [
            "-movflags",
            "+faststart",
            "-video_track_timescale",
            str(PRIVACY_FRAME_RATE * 1000),
            str(output_path),
        ]
    )
    return command


def _generate_privacy_tag_assets(asset_directory: Path) -> None:
    """Create a local chapter table and attached picture for metadata coverage."""
    asset_directory.mkdir(parents=True, exist_ok=True)
    cover = Image.new("RGB", (96, 96), (11, 18, 28))
    draw = ImageDraw.Draw(cover)
    draw.rectangle((7, 7, 88, 88), outline=(77, 225, 255), width=3)
    draw.text((14, 36), "PRIVATE", font=_fixture_font(18), fill=(255, 244, 210))
    cover.save(asset_directory / "attached-cover.jpg", format="JPEG", quality=95)
    (asset_directory / "private-metadata.txt").write_text(
        ";FFMETADATA1\n"
        "title=PRIVATE GLOBAL TITLE\n"
        "artist=PRIVATE AUTHOR\n"
        "comment=DEVICE PRIVATE CAMERA 912\n"
        "location=+37.7749-122.4194/\n"
        "[CHAPTER]\n"
        "TIMEBASE=1/1000\n"
        "START=0\n"
        "END=1000\n"
        "title=PRIVATE CHAPTER TITLE\n",
        encoding="utf-8",
        newline="\n",
    )


def run_checked(
    args: Sequence[str],
    *,
    timeout_seconds: float = 60.0,
    sensitive_paths: Sequence[Path] = (),
) -> subprocess.CompletedProcess[str]:
    """Run an external command using an argument array and no shell."""
    try:
        completed = subprocess.run(
            list(args),
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise FixtureFactoryError(f"Executable not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FixtureFactoryError(
            f"{Path(args[0]).name} timed out after {timeout_seconds:g} seconds"
        ) from exc
    except OSError as exc:
        raise FixtureFactoryError(
            f"Could not start {Path(args[0]).name}: {type(exc).__name__}"
        ) from exc

    if completed.returncode != 0:
        inferred_paths = tuple(
            Path(argument) for argument in args if Path(argument).is_absolute()
        )
        details = _sanitize_fixture_diagnostic(
            completed.stderr or completed.stdout,
            sensitive_paths=(*sensitive_paths, *inferred_paths, Path.home()),
        )
        raise FixtureFactoryError(
            f"{Path(args[0]).name} exited with status {completed.returncode}: "
            f"{details or 'no diagnostic output'}"
        )
    return completed


def _sanitize_fixture_diagnostic(
    value: str,
    *,
    sensitive_paths: Sequence[Path],
) -> str:
    """Bound one actionable diagnostic while removing known local paths."""
    sanitized = value.strip()
    spellings = {
        spelling
        for path in sensitive_paths
        for spelling in (str(path), Path(path).as_posix())
        if spelling
    }
    for spelling in sorted(spellings, key=len, reverse=True):
        sanitized = sanitized.replace(spelling, "[local-path]")
    if len(sanitized) > 2000:
        sanitized = f"{sanitized[:2000]}..."
    return sanitized


def generate_one(
    *,
    ffmpeg: str,
    spec: FixtureSpec,
    output_directory: Path,
    force: bool,
) -> Path:
    """Generate one fixture, atomically replacing it when forced."""
    output_path = output_directory / spec.filename
    if output_path.exists() and not force:
        return output_path

    staging_path = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")
    staging_path.unlink(missing_ok=True)
    try:
        if spec.text_mode is None:
            command = build_ffmpeg_command(
                ffmpeg=ffmpeg,
                spec=spec,
                output_path=staging_path,
            )
            run_checked(command)
        else:
            with tempfile.TemporaryDirectory(
                prefix="videoscope-text-frames-",
                dir=output_directory,
            ) as temporary_directory:
                frame_directory = Path(temporary_directory)
                generate_text_frames(frame_directory, mode=spec.text_mode)
                text_spec = replace(
                    spec,
                    input_args=(
                        "-framerate",
                        str(FRAME_RATE),
                        "-i",
                        str(frame_directory / "frame_%03d.png"),
                    ),
                )
                run_checked(
                    build_ffmpeg_command(
                        ffmpeg=ffmpeg,
                        spec=text_spec,
                        output_path=staging_path,
                    )
                )
        if not staging_path.is_file() or staging_path.stat().st_size == 0:
            raise FixtureFactoryError(
                f"FFmpeg did not create a non-empty fixture: {spec.filename}"
            )
        staging_path.replace(output_path)
    finally:
        staging_path.unlink(missing_ok=True)
    return output_path


def generate_privacy_one(
    *,
    ffmpeg: str,
    spec: PrivacyFixtureSpec,
    output_directory: Path,
    force: bool,
) -> Path:
    """Generate one Safe Sharing fixture and atomically replace it when forced."""
    output_path = output_directory / spec.filename
    if output_path.exists() and not force:
        return output_path
    staging_path = output_path.with_name(f".{output_path.stem}.tmp{output_path.suffix}")
    staging_path.unlink(missing_ok=True)
    try:
        if spec.mode in {"manual_visual", "qr", "text"}:
            with tempfile.TemporaryDirectory(
                prefix="videoscope-privacy-frames-",
                dir=output_directory,
            ) as temporary_directory:
                frame_directory = Path(temporary_directory)
                generate_privacy_frames(frame_directory, mode=spec.mode)
                run_checked(
                    build_privacy_ffmpeg_command(
                        ffmpeg=ffmpeg,
                        spec=spec,
                        output_path=staging_path,
                        frame_directory=frame_directory,
                    )
                )
        elif spec.mode == "tags":
            with tempfile.TemporaryDirectory(
                prefix="videoscope-privacy-tags-",
                dir=output_directory,
            ) as temporary_directory:
                asset_directory = Path(temporary_directory)
                _generate_privacy_tag_assets(asset_directory)
                run_checked(
                    build_privacy_ffmpeg_command(
                        ffmpeg=ffmpeg,
                        spec=spec,
                        output_path=staging_path,
                        asset_directory=asset_directory,
                    ),
                    sensitive_paths=(asset_directory, staging_path, output_path),
                )
        else:
            run_checked(
                build_privacy_ffmpeg_command(
                    ffmpeg=ffmpeg,
                    spec=spec,
                    output_path=staging_path,
                )
            )
        if not staging_path.is_file() or staging_path.stat().st_size == 0:
            raise FixtureFactoryError(
                f"FFmpeg did not create a non-empty fixture: {spec.filename}"
            )
        staging_path.replace(output_path)
    finally:
        staging_path.unlink(missing_ok=True)
    return output_path


def probe_video(*, ffprobe: str, video_path: Path) -> ProbeResult:
    """Read the stable metadata needed to validate one fixture."""
    completed = run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            str(video_path),
        ]
    )
    try:
        raw_payload: object = json.loads(completed.stdout)
        payload = cast(dict[str, object], raw_payload)
        raw_streams = cast(list[object], payload["streams"])
        streams = [cast(dict[str, object], item) for item in raw_streams]
        stream = next(item for item in streams if item.get("codec_type") == "video")
        media_format = cast(dict[str, object], payload["format"])
        return ProbeResult(
            duration_seconds=float(str(media_format["duration"])),
            width=int(str(stream["width"])),
            height=int(str(stream["height"])),
            frame_rate=float(Fraction(str(stream["avg_frame_rate"]))),
            has_audio=any(item.get("codec_type") == "audio" for item in streams),
        )
    except (
        KeyError,
        IndexError,
        StopIteration,
        TypeError,
        ValueError,
        ZeroDivisionError,
    ) as exc:
        raise FixtureFactoryError(
            f"ffprobe returned unexpected metadata for {video_path.name}"
        ) from exc


def validate_video(
    *,
    ffmpeg: str,
    ffprobe: str,
    video_path: Path,
    expected_duration_seconds: float = DURATION_SECONDS,
    expected_frame_rate: int = FRAME_RATE,
    expected_audio: bool = False,
) -> ProbeResult:
    """Validate metadata and fully decode one generated video."""
    result = probe_video(ffprobe=ffprobe, video_path=video_path)
    if (
        abs(result.duration_seconds - expected_duration_seconds)
        > TIME_TOLERANCE_SECONDS
    ):
        raise FixtureFactoryError(
            f"{video_path.name} duration is {result.duration_seconds:.3f}s; "
            f"expected {expected_duration_seconds:.1f}s"
        )
    if result.width != WIDTH or result.height != HEIGHT:
        raise FixtureFactoryError(
            f"{video_path.name} dimensions are {result.width}x{result.height}; "
            f"expected {WIDTH}x{HEIGHT}"
        )
    if abs(result.frame_rate - expected_frame_rate) > 0.01:
        raise FixtureFactoryError(
            f"{video_path.name} frame rate is {result.frame_rate:g}; "
            f"expected {expected_frame_rate}"
        )
    if expected_audio and not result.has_audio:
        raise FixtureFactoryError(
            f"{video_path.name} does not contain the expected audio stream"
        )

    decode_command = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
    ]
    if expected_audio:
        decode_command.extend(["-map", "0:a:0"])
    decode_command.extend(["-f", "null", "-"])
    run_checked(decode_command)
    return result


def manifest_data() -> dict[str, object]:
    """Build the canonical manifest without inspecting generated files."""
    videos: dict[str, object] = {}
    for spec in fixture_specs():
        entry: dict[str, object] = {
            "duration_seconds": DURATION_SECONDS,
            "expected_anomaly_type": spec.expected_anomaly_type,
            "expected_time_ranges": [
                {
                    "start_seconds": expected.start_seconds,
                    "end_seconds": expected.end_seconds,
                }
                for expected in spec.expected_ranges
            ],
            "tolerance_seconds": TIME_TOLERANCE_SECONDS,
        }
        if spec.expected_scene_cuts_seconds:
            entry["expected_scene_cuts_seconds"] = list(
                spec.expected_scene_cuts_seconds
            )
        videos[spec.filename] = entry

    return {
        "schema_version": "1.0",
        "generation": {
            "source": "FFmpeg lavfi and program-generated Pillow frames",
            "width": WIDTH,
            "height": HEIGHT,
            "frame_rate": FRAME_RATE,
            "video_codec": "mpeg4",
            "audio": False,
        },
        "publish_ready_fixture": {
            "audio": {
                "codec": "aac",
                "frequency_hz": PUBLISH_AUDIO_FREQUENCY_HZ,
                "source": "FFmpeg lavfi sine",
            },
            "duration_seconds": PUBLISH_DURATION_SECONDS,
            "filename": "publish_av.mp4",
            "frame_rate": PUBLISH_FRAME_RATE,
            "height": HEIGHT,
            "purpose": "Publish Ready profile end-to-end regression",
            "tolerance_seconds": TIME_TOLERANCE_SECONDS,
            "video_codec": "mpeg4",
            "width": WIDTH,
        },
        "content": content_manifest_data(),
        "privacy": privacy_manifest_data(),
        "rescue": rescue_manifest_data(),
        "rescue_derivatives": combined_rescue_manifest_data(),
        "videos": videos,
    }


def content_manifest_data() -> dict[str, object]:
    """Return deterministic C fixture recipes and human-authored expectations."""
    base: dict[str, object] = {
        "duration_seconds": CONTENT_DURATION_SECONDS,
        "width": WIDTH,
        "height": HEIGHT,
        "frame_rate": CONTENT_FRAME_RATE,
        "has_audio": True,
        "tolerance_seconds": TIME_TOLERANCE_SECONDS,
        "source": "FFmpeg lavfi only; no downloaded media",
    }
    return {
        "content_join_regression.mp4": {
            **base,
            "recipe_id": "videoscope-content-join-v1",
            "purpose": "selected clips and join regression verification",
            "goal": "selected_clips",
            "selected_ranges": [[1.0, 3.0], [5.0, 7.0], [9.0, 11.0]],
            "explicit_reorder": [2, 0, 1],
        },
        "content_locked_context.mp4": {
            **base,
            "recipe_id": "videoscope-content-lock-v1",
            "purpose": "locked keep wins over an explicit removal",
            "goal": "faithful_clean",
            "remove_range": [4.0, 8.0],
            "locked_keep_range": [5.0, 7.0],
            "expected_removed_ranges": [[4.0, 5.0], [7.0, 8.0]],
        },
        "content_meeting_structure.mp4": {
            **base,
            "recipe_id": "videoscope-content-meeting-v1",
            "purpose": "ordered meeting sections with a corroborated quiet static gap",
            "goal": "faithful_clean",
            "speech_like_ranges": [[0.0, 4.0], [8.0, 12.0]],
            "low_information_range": [4.0, 8.0],
            "reviewed_remove_range": [4.5, 7.5],
            "transcript": "content_meeting_valid.srt",
        },
        "content_tutorial_chapters.mp4": {
            **base,
            "recipe_id": "videoscope-content-tutorial-v1",
            "purpose": "complete-timeline chapter and subtitle mapping",
            "goal": "chaptered_full",
            "chapter_ranges": [[0.0, 4.0], [4.0, 8.0], [8.0, 12.0]],
            "chapter_titles": ["准备", "操作", "复盘"],
            "transcript": "content_tutorial_zh.vtt",
        },
        "transcripts": {
            "valid": ["content_meeting_valid.srt", "content_tutorial_zh.vtt"],
            "invalid": [
                "content_overlap.srt",
                "content_out_of_range.vtt",
                "content_malformed.srt",
            ],
            "privacy": "All transcript files are local generated fixtures.",
        },
    }


def rescue_manifest_data() -> dict[str, object]:
    """Return path-free, deterministic expectations for Rescue media fixtures."""
    entries: dict[str, object] = {}
    for spec in rescue_fixture_specs():
        acceptance: dict[str, object] = {
            "outcome_scope": "faithful_structural",
            "expected_outcome": (
                "partial"
                if spec.filename
                in {"rescue_middle_damaged.mp4", "rescue_tail_damaged.mp4"}
                else (
                    "needs_review"
                    if spec.filename == "rescue_missing_audio.mp4"
                    else "completed"
                )
            ),
            "duration_tolerance_seconds": 0.25,
        }
        if spec.filename == "rescue_dark_noise.mp4":
            acceptance.update(
                {
                    "minimum_luma_gain": 0.01,
                    "maximum_mean_luma": 0.35,
                    "maximum_noise_increase": 0.0,
                }
            )
        elif spec.filename == "rescue_fixed_av_offset.mp4":
            acceptance["fixed_offset_residual_tolerance_seconds"] = 0.04
        source_fixture = "rescue_clean_av.mp4"
        source_recipe_id = "videoscope-rescue-clean-av-v1"
        generation = (
            "payload_zeroing" if spec.mode == "payload_zeroing" else "ffmpeg_filter"
        )
        entry: dict[str, object] = {
            "duration_seconds": DURATION_SECONDS,
            "width": WIDTH,
            "height": HEIGHT,
            "frame_rate": FRAME_RATE,
            "has_audio": spec.has_audio,
            "source_fixture": source_fixture,
            "source_recipe_id": source_recipe_id,
            "source_sha256_record": "rescue-source-hashes.json",
            "generation": generation,
            "expected_damage_kinds": list(spec.expected_damage_kinds),
            "expected_damage_intervals": [
                {
                    "start_seconds": expected.start_seconds,
                    "end_seconds": expected.end_seconds,
                }
                for expected in spec.expected_ranges
            ],
            "damage_tolerance_seconds": 1.0,
            "acceptance": acceptance,
        }
        entries[spec.filename] = entry
    return entries


def combined_rescue_manifest_data() -> dict[str, object]:
    """Return the isolated combined structural/deflicker derivative contract."""
    combined = combined_rescue_fixture_spec()
    return {
        combined.filename: {
            "duration_seconds": DURATION_SECONDS,
            "width": WIDTH,
            "height": HEIGHT,
            "frame_rate": FRAME_RATE,
            "has_audio": True,
            "source_fixture": "local_ffmpeg_derivative",
            "source_recipe_id": "videoscope-rescue-flicker-middle-v1",
            "source_sha256_record": "rescue-source-hashes.json",
            "generation": "ffmpeg_filter_then_payload_zeroing",
            "expected_damage_kinds": list(combined.expected_damage_kinds),
            "expected_damage_intervals": [],
            "damage_tolerance_seconds": 1.0,
            "source_deletion_interval": {
                "start_seconds": 2.0,
                "end_seconds": 3.0,
            },
            "authorized_correction_intervals": [
                {"start_seconds": 0.5, "end_seconds": 3.5}
            ],
            "locked_interval": {"start_seconds": 3.5, "end_seconds": 4.5},
            "clean_interval": {"start_seconds": 5.0, "end_seconds": 6.0},
            "acceptance": {
                "outcome_scope": "faithful_structural",
                "expected_outcome": "partial",
                "duration_tolerance_seconds": 0.25,
                "decoded_frame_tolerance": 0.04,
                "mapping_tolerance_seconds": 0.11,
                "maximum_residual_luma": 0.14,
            },
        }
    }


def privacy_manifest_data() -> dict[str, object]:
    """Return hand-authored Safe Sharing regression annotations."""
    base = {
        "duration_seconds": PRIVACY_DURATION_SECONDS,
        "width": WIDTH,
        "height": HEIGHT,
        "frame_rate": PRIVACY_FRAME_RATE,
        "timing_tolerance_seconds": TIME_TOLERANCE_SECONDS,
    }
    visual_box = {"x_min": 0.1, "y_min": 0.15, "x_max": 0.8, "y_max": 0.75}
    text_box = {
        "x_min": 0.025,
        "y_min": 0.688889,
        "x_max": 0.975,
        "y_max": 0.955556,
    }
    qr_segments = (
        (
            "static",
            0.0,
            1.0,
            {"x_min": 0.0625, "y_min": 0.111111, "x_max": 0.3125, "y_max": 0.555556},
        ),
        (
            "moving",
            1.0,
            2.0,
            {"x_min": 0.075, "y_min": 0.333333, "x_max": 0.775, "y_max": 0.777778},
        ),
        (
            "scaling",
            2.0,
            3.0,
            {"x_min": 0.35625, "y_min": 0.244444, "x_max": 0.64375, "y_max": 0.755556},
        ),
        (
            "edge_adjacent",
            3.0,
            4.0,
            {"x_min": 0.7375, "y_min": 0.533333, "x_max": 0.9875, "y_max": 0.977778},
        ),
    )
    text_cases = [
        {
            "kind": kind,
            "language": language,
            "value": value,
            "sensitive": sensitive,
            "start_seconds": start,
            "end_seconds": end,
            "box": text_box,
        }
        for kind, language, value, sensitive, start, end in PRIVACY_TEXT_CASES
    ]
    return {
        "privacy_clean.mp4": {
            **base,
            "has_audio": False,
            "expected_categories": [],
            "risks": [],
            "manual_visual_regions": [],
            "manual_audio_intervals": [],
        },
        "privacy_manual_visual.mp4": {
            **base,
            "has_audio": False,
            "expected_categories": ["manual_visual"],
            "risks": [
                {
                    "category": "manual_visual",
                    "start_seconds": 0.4,
                    "end_seconds": 3.6,
                    "box": visual_box,
                    "decision": "redact",
                    "style": "blur",
                }
            ],
            "manual_visual_regions": [
                {
                    "start_seconds": 0.4,
                    "end_seconds": 3.6,
                    "box": visual_box,
                    "style": "blur",
                }
            ],
            "manual_audio_intervals": [],
            "visual_story": {
                "kind": "moving_face_like_region",
                "visible_intervals": [[0.4, 1.4], [2.0, 3.6]],
                "occluded_intervals": [[1.4, 2.0]],
                "reappears": True,
            },
        },
        "privacy_qr.mp4": {
            **base,
            "has_audio": False,
            "expected_categories": ["qr_code"],
            "risks": [
                {
                    "category": "qr_code",
                    "motion": motion,
                    "start_seconds": start,
                    "end_seconds": end,
                    "box": box,
                    "decision": "redact",
                    "style": "solid_fill",
                }
                for motion, start, end, box in qr_segments
            ],
            "manual_visual_regions": [],
            "manual_audio_intervals": [],
        },
        "privacy_tags_av.mp4": {
            **base,
            "has_audio": True,
            "expected_categories": ["metadata", "manual_audio"],
            "risks": [
                {
                    "category": "metadata",
                    "start_seconds": 0.0,
                    "end_seconds": 4.0,
                    "decision": "redact",
                    "style": "remove_metadata",
                },
                {
                    "category": "manual_audio",
                    "start_seconds": 1.0,
                    "end_seconds": 2.0,
                    "decision": "redact",
                    "style": "mute",
                },
            ],
            "manual_visual_regions": [],
            "manual_audio_intervals": [
                {
                    "start_seconds": 1.0,
                    "end_seconds": 2.0,
                    "style": "mute",
                }
            ],
            "metadata_expectations": {
                "scopes": ["global", "stream", "chapter", "attachment"],
                "semantic_fields": ["author", "title", "device", "location"],
                "keys": ["artist", "title", "comment", "location"],
                "device_semantic_key": "comment",
                "attached_picture": True,
            },
        },
        "privacy_text.mp4": {
            **base,
            "has_audio": False,
            "expected_categories": ["suspicious_text"],
            "risks": [
                {
                    "category": "suspicious_text",
                    "kind": case["kind"],
                    "start_seconds": case["start_seconds"],
                    "end_seconds": case["end_seconds"],
                    "box": text_box,
                    "decision": "redact",
                    "style": "solid_fill",
                }
                for case in text_cases
                if case["sensitive"]
            ],
            "manual_visual_regions": [],
            "manual_audio_intervals": [],
            "text_cases": text_cases,
        },
    }


def write_manifest(path: Path) -> None:
    """Write the canonical manifest with stable UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        manifest_data(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    path.write_text(
        f"{content}\n",
        encoding="utf-8",
        newline="\n",
    )


def generate_fixtures(
    *,
    output_directory: Path,
    manifest_path: Path,
    ffmpeg: str,
    ffprobe: str,
    force: bool,
) -> tuple[Path, ...]:
    """Generate, probe, and decode every declared fixture."""
    output_directory.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    for spec in fixture_specs():
        output_path = generate_one(
            ffmpeg=ffmpeg,
            spec=spec,
            output_directory=output_directory,
            force=force,
        )
        result = validate_video(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            video_path=output_path,
        )
        print(
            f"OK {output_path.name}: {result.duration_seconds:.3f}s, "
            f"{result.width}x{result.height}, {result.frame_rate:g}fps"
        )
        generated.append(output_path)

    write_manifest(manifest_path)
    return tuple(generated)


def generate_publish_fixture(
    *,
    output_directory: Path,
    ffmpeg: str,
    ffprobe: str,
    force: bool,
) -> Path:
    """Generate and validate the separate Publish Ready audio/video fixture."""
    spec = publish_fixture_spec()
    output_path = generate_one(
        ffmpeg=ffmpeg,
        spec=spec,
        output_directory=output_directory,
        force=force,
    )
    result = validate_video(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        video_path=output_path,
        expected_duration_seconds=spec.duration_seconds,
        expected_frame_rate=spec.frame_rate,
        expected_audio=True,
    )
    print(
        f"OK {output_path.name}: {result.duration_seconds:.3f}s, "
        f"{result.width}x{result.height}, {result.frame_rate:g}fps, audio"
    )
    return output_path


def write_content_transcripts(output_directory: Path) -> tuple[Path, ...]:
    """Write stable UTF-8 timed-text fixtures without external content."""
    payloads = {
        "content_meeting_valid.srt": (
            "1\n00:00:00,000 --> 00:00:03,800\nOpening and decisions.\n\n"
            "2\n00:00:08,100 --> 00:00:11,800\nActions and closing.\n"
        ),
        "content_tutorial_zh.vtt": (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:03.900\n准备材料\n\n"
            "00:00:04.000 --> 00:00:07.900\n执行操作\n\n"
            "00:00:08.000 --> 00:00:11.900\n检查结果\n"
        ),
        "content_overlap.srt": (
            "1\n00:00:00,000 --> 00:00:05,000\nFirst\n\n"
            "2\n00:00:04,000 --> 00:00:06,000\nOverlap\n"
        ),
        "content_out_of_range.vtt": (
            "WEBVTT\n\n00:00:11.500 --> 00:00:13.000\nOutside source\n"
        ),
        "content_malformed.srt": "not a timed transcript\n",
    }
    written: list[Path] = []
    for filename, content in sorted(payloads.items()):
        path = output_directory / filename
        path.write_text(content, encoding="utf-8", newline="\n")
        written.append(path)
    return tuple(written)


def generate_content_fixtures(
    *,
    output_directory: Path,
    ffmpeg: str,
    ffprobe: str,
    force: bool,
) -> tuple[Path, ...]:
    """Generate and fully validate every purpose-built useful-content fixture."""
    output_directory.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    for spec in content_fixture_specs():
        output_path = generate_one(
            ffmpeg=ffmpeg,
            spec=spec,
            output_directory=output_directory,
            force=force,
        )
        result = validate_video(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            video_path=output_path,
            expected_duration_seconds=CONTENT_DURATION_SECONDS,
            expected_frame_rate=CONTENT_FRAME_RATE,
            expected_audio=True,
        )
        print(
            f"OK {output_path.name}: {result.duration_seconds:.3f}s, "
            f"{result.width}x{result.height}, {result.frame_rate:g}fps, audio"
        )
        generated.append(output_path)
    write_content_transcripts(output_directory)
    return tuple(generated)


def generate_privacy_fixtures(
    *,
    output_directory: Path,
    ffmpeg: str,
    ffprobe: str,
    force: bool,
) -> tuple[Path, ...]:
    """Generate, probe, and fully decode every Safe Sharing fixture."""
    output_directory.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    for spec in privacy_fixture_specs():
        output_path = generate_privacy_one(
            ffmpeg=ffmpeg,
            spec=spec,
            output_directory=output_directory,
            force=force,
        )
        result = validate_video(
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            video_path=output_path,
            expected_duration_seconds=PRIVACY_DURATION_SECONDS,
            expected_frame_rate=PRIVACY_FRAME_RATE,
            expected_audio=spec.has_audio,
        )
        if result.has_audio is not spec.has_audio:
            raise FixtureFactoryError(
                f"{spec.filename} audio presence did not match its fixture contract"
            )
        suffix = ", audio" if result.has_audio else ""
        print(
            f"OK {output_path.name}: {result.duration_seconds:.3f}s, "
            f"{result.width}x{result.height}, {result.frame_rate:g}fps{suffix}"
        )
        generated.append(output_path)
    return tuple(generated)


def _rescue_source_args(*, offset_seconds: float = 0.0) -> tuple[str, ...]:
    """Return a stable test pattern plus sine audio without external media."""
    arguments = (
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FRAME_RATE}:duration={DURATION_SECONDS:g}",
    )
    if offset_seconds:
        return (
            *arguments,
            "-itsoffset",
            f"{offset_seconds:g}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=48000:duration={DURATION_SECONDS:g}",
        )
    return (
        *arguments,
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=48000:duration={DURATION_SECONDS:g}",
    )


def _rescue_fixture_spec(spec: RescueFixtureSpec) -> FixtureSpec:
    """Translate a declared Rescue mode into the existing generic FFmpeg spec."""
    video_filter: str | None = None
    audio_map_label: str | None = "1:a:0" if spec.has_audio else None
    input_args = _rescue_source_args()
    if spec.mode == "missing_audio":
        input_args = (
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={WIDTH}x{HEIGHT}:rate={FRAME_RATE}:duration={DURATION_SECONDS:g}",
        )
    elif spec.mode == "low_loudness":
        audio_map_label = "1:a:0"
        input_args = _rescue_source_args()
        input_args = (*input_args, "-filter:a", "volume=0.05")
    elif spec.mode == "fixed_av_offset":
        input_args = _rescue_source_args(offset_seconds=0.4)
    elif spec.mode == "dark_noise":
        video_filter = "eq=brightness=-0.35,noise=alls=20:allf=t"
    elif spec.mode == "soft_detail":
        video_filter = "boxblur=luma_radius=8:luma_power=2"
    elif spec.mode == "flicker":
        video_filter = "eq=brightness='if(mod(n,2),0.35,-0.35)':eval=frame"
    elif spec.mode == "flicker_payload_zeroing":
        video_filter = (
            "eq=brightness='if(between(t,0.5,4.5),"
            "if(mod(n,2),0.08,-0.08),0)':eval=frame"
        )
    elif spec.mode == "shake":
        video_filter = (
            "crop=iw-12:ih-8:abs(sin(n*0.7))*12:abs(sin(n*1.1))*8,"
            f"scale={WIDTH}:{HEIGHT}"
        )
    elif spec.mode not in {"clean", "payload_zeroing"}:
        raise ValueError(f"Unsupported Rescue fixture mode: {spec.mode}")
    return FixtureSpec(
        filename=spec.filename,
        input_args=input_args,
        expected_anomaly_type="rescue",
        video_filter=video_filter,
        audio_map_label=audio_map_label,
        gop_size=1,
    )


def _copy_with_zeroed_payload(
    *,
    source: Path,
    destination: Path,
    expected_range: ExpectedRange,
    ffprobe: str,
) -> None:
    """Copy a source and zero video packet bytes overlapping a source-time range."""
    source_hash = _sha256_file(source)
    source_size = source.stat().st_size
    shutil.copyfile(source, destination)
    arguments = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "packet=pts_time,dts_time,duration_time,pos,size",
        "-of",
        "compact=p=0:nk=0",
        str(source),
    ]
    selected_count = 0
    with destination.open("r+b") as damaged:
        for line in _iter_command_stdout_lines(
            arguments,
            source=source,
            timeout_seconds=60.0,
        ):
            packet = _parse_packet_span(line)
            if packet is None or not _packet_overlaps(packet, expected_range):
                continue
            if packet.position + packet.size > source_size:
                raise FixtureFactoryError(
                    "ffprobe packet positions were outside the source"
                )
            damaged.seek(packet.position)
            _write_zero_bytes(damaged, packet.size)
            selected_count += 1
    if selected_count == 0:
        raise FixtureFactoryError(
            "ffprobe did not expose overlapping packet positions for corruption"
        )
    if _sha256_file(source) != source_hash:
        raise FixtureFactoryError("Rescue fixture source changed during corruption")


@dataclass(frozen=True, slots=True)
class _PacketSpan:
    timestamp_seconds: float
    duration_seconds: float
    position: int
    size: int


def _iter_command_stdout_lines(
    arguments: list[str], *, source: Path, timeout_seconds: float
) -> Iterator[str]:
    """Use the scanner's bounded process supervisor for packet fixture metadata."""
    from videoscope.rescue.errors import RescueScanError
    from videoscope.rescue.scanner import (
        _iter_command_stdout_lines as iter_scanner_command_lines,
    )

    try:
        yield from iter_scanner_command_lines(
            arguments,
            source=source,
            timeout_seconds=timeout_seconds,
        )
    except RescueScanError as exc:
        raise FixtureFactoryError(
            exc.internal_message or "ffprobe could not inspect packet positions"
        ) from exc


def _parse_packet_span(line: str) -> _PacketSpan | None:
    values = {
        key: value
        for field in line.split("|")
        for key, separator, value in (field.partition("="),)
        if separator
    }
    raw_timestamp = values.get("pts_time")
    if raw_timestamp is None or raw_timestamp in ("", "N/A"):
        raw_timestamp = values.get("dts_time")
    if raw_timestamp is None or raw_timestamp in ("", "N/A"):
        return None
    try:
        timestamp = float(raw_timestamp)
        duration = float(values.get("duration_time", "0"))
        position = int(values["pos"])
        size = int(values["size"])
    except (KeyError, ValueError) as exc:
        raise FixtureFactoryError(
            "ffprobe did not expose packet positions for corruption"
        ) from exc
    if (
        not isfinite(timestamp)
        or not isfinite(duration)
        or duration < 0
        or position < 0
        or size <= 0
    ):
        raise FixtureFactoryError(
            "ffprobe returned invalid packet positions for corruption"
        )
    return _PacketSpan(timestamp, duration, position, size)


def _packet_overlaps(packet: _PacketSpan, expected_range: ExpectedRange) -> bool:
    if packet.duration_seconds == 0:
        return (
            expected_range.start_seconds
            <= packet.timestamp_seconds
            < expected_range.end_seconds
        )
    return (
        packet.timestamp_seconds < expected_range.end_seconds
        and packet.timestamp_seconds + packet.duration_seconds
        > expected_range.start_seconds
    )


def _write_zero_bytes(destination: BinaryIO, size: int) -> None:
    zero_chunk = b"\x00" * ZERO_CHUNK_BYTES
    remaining = size
    while remaining:
        count = min(remaining, len(zero_chunk))
        destination.write(zero_chunk[:count])
        remaining -= count


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(FILE_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _record_rescue_source_hash(*, output_directory: Path, pristine_path: Path) -> str:
    pristine_hash = _sha256_file(pristine_path)
    (output_directory / "rescue-source-hashes.json").write_text(
        json.dumps({pristine_path.name: pristine_hash}, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return pristine_hash


def validate_rescue_observed_intervals(
    *, output_directory: Path, ffmpeg: str, ffprobe: str
) -> tuple[str, ...]:
    """Run the Rescue scanner and compare observed damage with manifest ranges."""
    from videoscope.domain import VideoMetadata
    from videoscope.rescue.scanner import RescueScanConfig, RescueScanner

    manifest = cast(dict[str, dict[str, object]], rescue_manifest_data())
    scanner = RescueScanner()
    validated: list[str] = []
    for filename in sorted(manifest):
        entry = manifest[filename]
        expected_intervals = cast(
            list[dict[str, float]], entry["expected_damage_intervals"]
        )
        if not expected_intervals:
            continue
        video_path = output_directory / filename
        probe = probe_video(ffprobe=ffprobe, video_path=video_path)
        metadata = VideoMetadata(
            filename=filename,
            container_format="mov,mp4,m4a,3gp,3g2,mj2",
            codec="mpeg4",
            width=probe.width,
            height=probe.height,
            duration_seconds=probe.duration_seconds,
            average_frame_rate=probe.frame_rate,
            estimated_frame_count=int(round(probe.duration_seconds * probe.frame_rate)),
            has_audio=probe.has_audio,
            file_size_bytes=video_path.stat().st_size,
        )
        damage_map = scanner.scan(
            source=video_path,
            input_hash=_sha256_file(video_path),
            metadata=metadata,
            config=RescueScanConfig(
                ffmpeg_executable=ffmpeg,
                ffprobe_executable=ffprobe,
            ),
        )
        expected_kinds = set(cast(list[str], entry["expected_damage_kinds"]))
        tolerance = float(cast(float, entry["damage_tolerance_seconds"]))
        observed = [
            interval
            for interval in damage_map.intervals
            if interval.kind.value in expected_kinds
        ]
        for expected in expected_intervals:
            expected_start = float(expected["start_seconds"])
            expected_end = float(expected["end_seconds"])
            if not any(
                abs(interval.start_seconds - expected_start) <= tolerance
                and abs(interval.end_seconds - expected_end) <= tolerance
                for interval in observed
            ):
                raise FixtureFactoryError(
                    f"{filename} scanner observed interval did not match the "
                    "manifest tolerance"
                )
        validated.append(filename)
    return tuple(validated)


def generate_rescue_fixtures(
    *,
    output_directory: Path,
    ffmpeg: str,
    ffprobe: str,
    force: bool,
) -> tuple[Path, ...]:
    """Generate deterministic local Rescue fixtures without downloading media."""
    output_directory.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    pristine_path = output_directory / "rescue_clean_av.mp4"
    rescue_specs = rescue_fixture_specs()
    for rescue_spec in rescue_specs:
        output_path = output_directory / rescue_spec.filename
        if rescue_spec.mode == "payload_zeroing":
            if output_path.exists() and not force:
                generated.append(output_path)
                continue
            staging_path = output_path.with_name(
                f".{output_path.stem}.tmp{output_path.suffix}"
            )
            staging_path.unlink(missing_ok=True)
            try:
                _copy_with_zeroed_payload(
                    source=pristine_path,
                    destination=staging_path,
                    expected_range=rescue_spec.expected_ranges[0],
                    ffprobe=ffprobe,
                )
                staging_path.replace(output_path)
            finally:
                staging_path.unlink(missing_ok=True)
            result = probe_video(ffprobe=ffprobe, video_path=output_path)
        else:
            fixture_spec = _rescue_fixture_spec(rescue_spec)
            output_path = generate_one(
                ffmpeg=ffmpeg,
                spec=fixture_spec,
                output_directory=output_directory,
                force=force,
            )
            result = validate_video(
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                video_path=output_path,
                expected_audio=rescue_spec.has_audio,
            )
        if result.has_audio is not rescue_spec.has_audio:
            raise FixtureFactoryError(
                f"{rescue_spec.filename} audio presence did not match its "
                "fixture contract"
            )
        print(
            f"OK {output_path.name}: {result.duration_seconds:.3f}s, "
            f"{result.width}x{result.height}, {result.frame_rate:g}fps"
        )
        generated.append(output_path)
    if rescue_specs:
        combined = combined_rescue_fixture_spec()
        output_path = output_directory / combined.filename
        if force or not output_path.exists():
            staging_path = output_path.with_name(
                f".{output_path.stem}.tmp{output_path.suffix}"
            )
            staging_path.unlink(missing_ok=True)
            try:
                with tempfile.TemporaryDirectory(
                    prefix="videoscope-rescue-fixture-", dir=output_directory
                ) as temporary_directory:
                    filtered_source = generate_one(
                        ffmpeg=ffmpeg,
                        spec=_rescue_fixture_spec(combined),
                        output_directory=Path(temporary_directory),
                        force=True,
                    )
                    validate_video(
                        ffmpeg=ffmpeg,
                        ffprobe=ffprobe,
                        video_path=filtered_source,
                        expected_audio=True,
                    )
                    _copy_with_zeroed_payload(
                        source=filtered_source,
                        destination=staging_path,
                        expected_range=combined.expected_ranges[0],
                        ffprobe=ffprobe,
                    )
                staging_path.replace(output_path)
            finally:
                staging_path.unlink(missing_ok=True)
        result = probe_video(ffprobe=ffprobe, video_path=output_path)
        print(
            f"OK {output_path.name}: {result.duration_seconds:.3f}s, "
            f"{result.width}x{result.height}, {result.frame_rate:g}fps"
        )
        generated.append(output_path)
    _record_rescue_source_hash(
        output_directory=output_directory,
        pristine_path=pristine_path,
    )
    validate_rescue_observed_intervals(
        output_directory=output_directory,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
    return tuple(generated)


def build_parser() -> argparse.ArgumentParser:
    """Create the fixture factory argument parser."""
    parser = argparse.ArgumentParser(
        description="Generate deterministic VideoScope micro video fixtures.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate and atomically replace existing fixture videos.",
    )
    parser.add_argument(
        "--ffmpeg",
        help="Explicit FFmpeg executable path (defaults to PATH lookup).",
    )
    parser.add_argument(
        "--ffprobe",
        help="Explicit ffprobe executable path (defaults to PATH lookup).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixture factory CLI."""
    arguments = build_parser().parse_args(argv)
    ffmpeg = arguments.ffmpeg or shutil.which("ffmpeg")
    ffprobe = arguments.ffprobe or shutil.which("ffprobe")
    missing = [
        tool
        for tool, location in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe))
        if location is None
    ]
    if missing:
        print(
            "ERROR: Missing required system executable(s): "
            f"{', '.join(missing)}. Install FFmpeg and ensure both ffmpeg and "
            "ffprobe are available on PATH.",
            file=sys.stderr,
        )
        return 2

    assert ffmpeg is not None
    assert ffprobe is not None
    try:
        generated = generate_fixtures(
            output_directory=DEFAULT_OUTPUT_DIRECTORY,
            manifest_path=DEFAULT_MANIFEST_PATH,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            force=bool(arguments.force),
        )
        generate_publish_fixture(
            output_directory=DEFAULT_OUTPUT_DIRECTORY,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            force=bool(arguments.force),
        )
        content_generated = generate_content_fixtures(
            output_directory=DEFAULT_OUTPUT_DIRECTORY,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            force=bool(arguments.force),
        )
        privacy_generated = generate_privacy_fixtures(
            output_directory=DEFAULT_OUTPUT_DIRECTORY,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            force=bool(arguments.force),
        )
        rescue_generated = generate_rescue_fixtures(
            output_directory=DEFAULT_OUTPUT_DIRECTORY,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            force=bool(arguments.force),
        )
    except FixtureFactoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    fixture_count = (
        len(generated)
        + len(content_generated)
        + len(privacy_generated)
        + len(rescue_generated)
        + 1
    )
    print(f"Generated and validated {fixture_count} fixture videos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
