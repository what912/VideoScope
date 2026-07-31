"""Generate deterministic, copyright-free micro video fixtures with FFmpeg."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import cast

from PIL import Image, ImageDraw, ImageFont

WIDTH = 320
HEIGHT = 180
FRAME_RATE = 10
DURATION_SECONDS = 6.0
TIME_TOLERANCE_SECONDS = 0.11
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


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Validated subset of ffprobe output."""

    duration_seconds: float
    width: int
    height: int
    frame_rate: float


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


def _fixture_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load Pillow's bundled deterministic font without system font lookup."""
    try:
        return ImageFont.load_default(size=30)
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
    command.extend(
        [
            "-an",
            "-c:v",
            "mpeg4",
            "-q:v",
            "3",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(FRAME_RATE),
            "-t",
            f"{DURATION_SECONDS:g}",
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
            "10000",
            str(output_path),
        ]
    )
    return command


def run_checked(
    args: Sequence[str],
    *,
    timeout_seconds: float = 60.0,
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
        details = (completed.stderr or completed.stdout).strip()
        if len(details) > 2000:
            details = f"{details[:2000]}..."
        raise FixtureFactoryError(
            f"{Path(args[0]).name} exited with status {completed.returncode}: "
            f"{details or 'no diagnostic output'}"
        )
    return completed


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


def probe_video(*, ffprobe: str, video_path: Path) -> ProbeResult:
    """Read the stable metadata needed to validate one fixture."""
    completed = run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,avg_frame_rate:format=duration",
            "-of",
            "json",
            str(video_path),
        ]
    )
    try:
        raw_payload: object = json.loads(completed.stdout)
        payload = cast(dict[str, object], raw_payload)
        raw_streams = cast(list[object], payload["streams"])
        stream = cast(dict[str, object], raw_streams[0])
        media_format = cast(dict[str, object], payload["format"])
        return ProbeResult(
            duration_seconds=float(str(media_format["duration"])),
            width=int(str(stream["width"])),
            height=int(str(stream["height"])),
            frame_rate=float(Fraction(str(stream["avg_frame_rate"]))),
        )
    except (KeyError, IndexError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise FixtureFactoryError(
            f"ffprobe returned unexpected metadata for {video_path.name}"
        ) from exc


def validate_video(
    *,
    ffmpeg: str,
    ffprobe: str,
    video_path: Path,
) -> ProbeResult:
    """Validate metadata and fully decode one generated video."""
    result = probe_video(ffprobe=ffprobe, video_path=video_path)
    if abs(result.duration_seconds - DURATION_SECONDS) > TIME_TOLERANCE_SECONDS:
        raise FixtureFactoryError(
            f"{video_path.name} duration is {result.duration_seconds:.3f}s; "
            f"expected {DURATION_SECONDS:.1f}s"
        )
    if result.width > WIDTH or result.height > HEIGHT:
        raise FixtureFactoryError(
            f"{video_path.name} exceeds {WIDTH}x{HEIGHT}: "
            f"{result.width}x{result.height}"
        )
    if abs(result.frame_rate - FRAME_RATE) > 0.01:
        raise FixtureFactoryError(
            f"{video_path.name} frame rate is {result.frame_rate:g}; "
            f"expected {FRAME_RATE}"
        )

    run_checked(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ]
    )
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
        "videos": videos,
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixture factory CLI."""
    arguments = build_parser().parse_args(argv)
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
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
    except FixtureFactoryError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Generated and validated {len(generated)} fixture videos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
