"""Generate the deterministic, fully local four-mode demonstration source."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from scripts.full_local_demo_contract import (
    DemoContract,
    canonical_json_bytes,
    load_demo_contract,
    stream_sha256,
)

SOURCE_FILENAME = "VideoScope-Full-Local-Demo-Source.mp4"
MANIFEST_FILENAME = "demo-manifest.json"
CONTRACT_RELATIVE_PATH = Path("demos", "full-local-four-mode", "demo-contract.json")
COMPOSITION_RELATIVE_PATH = Path("demos", "full-local-four-mode")
GENERATOR_VERSION = "1.0"
RENDER_TIMEOUT_SECONDS = 600.0
POSTPROCESS_TIMEOUT_SECONDS = 180.0
PROBE_TIMEOUT_SECONDS = 30.0
VERSION_TIMEOUT_SECONDS = 15.0
DIAGNOSTIC_LIMIT = 2000


class DemoGenerationError(RuntimeError):
    """A safe, path-scrubbed demo generation failure."""


class CommandRunner(Protocol):
    """Run an external command represented only by an argument sequence."""

    def __call__(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class GenerationSummary:
    source_path: Path
    manifest_path: Path
    source_sha256: str
    manifest_sha256: str
    source_size_bytes: int


def run_command(
    arguments: Sequence[str], *, cwd: Path, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    """Run one bounded no-shell command and expose only scrubbed diagnostics."""
    args = list(arguments)
    if not args or any(not isinstance(argument, str) for argument in args):
        raise DemoGenerationError("command arguments must be non-empty strings")
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise DemoGenerationError("external command timed out") from error
    except FileNotFoundError as error:
        raise DemoGenerationError("external executable not found") from error
    except OSError as error:
        raise DemoGenerationError("external command could not be started") from error
    if completed.returncode != 0:
        diagnostic = _scrub_diagnostic(completed.stderr[-DIAGNOSTIC_LIMIT:], cwd)
        suffix = f": {diagnostic}" if diagnostic else ""
        raise DemoGenerationError(
            f"external command failed with exit code {completed.returncode}{suffix}"
        )
    return completed


def build_postprocess_arguments(
    base_video: Path, output_video: Path, ffmpeg: str
) -> list[str]:
    """Build the deterministic FFmpeg argument array for the declared conditions."""
    audio_expression = (
        "aevalsrc=exprs='0.012*sin(2*PI*220*t)"
        "+if(between(t,5,10),0.025*sin(2*PI*60*t)"
        "+0.020*sin(2*PI*118*t),0)"
        "+if(between(t,25,32),0.080*sin(2*PI*880*t),0)'"
        "|'0.012*sin(2*PI*220*t)"
        "+if(between(t,5,10),0.025*sin(2*PI*60*t)"
        "+0.020*sin(2*PI*118*t),0)"
        "+if(between(t,25,32),0.080*sin(2*PI*880*t),0)'"
        ":s=48000:d=42:c=stereo"
    )
    filter_graph = (
        "[0:v]"
        "eq=brightness='-0.18+0.025*sin(2*PI*7*t)':contrast=0.92:"
        "enable='between(t,5,10)',"
        "boxblur=lr=3:lp=1:cr=2:cp=1:enable='between(t,5,10)',"
        "split=2[conditioned][shake];"
        "[shake]crop=iw-32:ih-18:"
        "x='16+14*sin(2*PI*2*t)':y='9+7*sin(2*PI*1.5*t)',"
        "scale=1280:720:flags=bicubic[shake_scaled];"
        "[conditioned][shake_scaled]overlay=0:0:"
        "enable='between(t,32,36)'[video_out]"
    )
    return [
        ffmpeg,
        "-y",
        "-i",
        str(base_video),
        "-f",
        "lavfi",
        "-i",
        audio_expression,
        "-filter_complex",
        filter_graph,
        "-map",
        "[video_out]",
        "-map",
        "1:a:0",
        "-t",
        "42",
        "-r",
        "24",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "16",
        "-g",
        "48",
        "-keyint_min",
        "48",
        "-x264-params",
        "scenecut=0:open-gop=0",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ac",
        "2",
        "-channel_layout",
        "stereo",
        "-ar",
        "48000",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-map_metadata",
        "-1",
        "-metadata",
        "title=VideoScope Full Local Four-Mode Demo",
        "-metadata",
        "artist=demo.user@example.invalid",
        "-metadata",
        "comment=+1 202-555-0107",
        "-metadata",
        "location=00.0000, 000.0000",
        "-movflags",
        "disable_chpl",
        str(output_video),
    ]


def probe_demo(path: Path, ffprobe: str) -> Mapping[str, object]:
    """Probe and strictly validate the final media contract."""
    result = run_command(
        _probe_arguments(path, ffprobe),
        cwd=path.parent,
        timeout_seconds=PROBE_TIMEOUT_SECONDS,
    )
    return _parse_probe_output(result.stdout)


def contract_digest(path: Path) -> str:
    """Hash the complete contract after canonical JSON normalization."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DemoGenerationError("cannot digest demo contract") from error
    if not isinstance(payload, Mapping):
        raise DemoGenerationError("demo contract must be a JSON object")
    try:
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    except (TypeError, ValueError) as error:
        raise DemoGenerationError("demo contract is not canonicalizable") from error


def generate_demo(
    project_root: Path,
    output_root: Path,
    *,
    force: bool,
    runner: CommandRunner = run_command,
) -> GenerationSummary:
    """Render, validate, and atomically publish the deterministic local demo."""
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    composition_root = project_root / COMPOSITION_RELATIVE_PATH
    contract_path = project_root / CONTRACT_RELATIVE_PATH
    published_source = output_root / SOURCE_FILENAME
    published_manifest = output_root / MANIFEST_FILENAME
    if not force and (published_source.exists() or published_manifest.exists()):
        raise DemoGenerationError(
            "published demo exists; pass force=True to replace it"
        )

    try:
        contract = load_demo_contract(contract_path)
    except ValueError as error:
        raise DemoGenerationError("demo contract validation failed") from error
    output_root.mkdir(parents=True, exist_ok=True)
    staging_root = output_root / f".staging-{uuid.uuid4().hex}"
    staging_root.mkdir(parents=False, exist_ok=False)
    base_video = staging_root / "base.mp4"
    staged_source = staging_root / SOURCE_FILENAME
    staged_manifest = staging_root / MANIFEST_FILENAME

    hyperframes = os.environ.get("VIDEOSCOPE_HYPERFRAMES", "hyperframes")
    ffmpeg = os.environ.get("VIDEOSCOPE_FFMPEG", "ffmpeg")
    ffprobe = os.environ.get("VIDEOSCOPE_FFPROBE", "ffprobe")
    render_arguments = [
        hyperframes,
        "render",
        "--output",
        str(base_video),
        "--fps",
        str(contract.frame_rate),
        "--quality",
        "high",
        "--strict",
    ]
    postprocess_arguments = build_postprocess_arguments(
        base_video, staged_source, ffmpeg
    )
    probe_arguments = _probe_arguments(staged_source, ffprobe)

    try:
        versions = {
            "generator": GENERATOR_VERSION,
            "hyperframes": _read_version(
                runner,
                [hyperframes, "--version"],
                cwd=composition_root,
            ),
            "ffmpeg": _read_version(
                runner,
                [ffmpeg, "-version"],
                cwd=project_root,
            ),
            "ffprobe": _read_version(
                runner,
                [ffprobe, "-version"],
                cwd=project_root,
            ),
        }
        runner(
            render_arguments,
            cwd=composition_root,
            timeout_seconds=RENDER_TIMEOUT_SECONDS,
        )
        _require_nonempty_file(base_video, "HyperFrames render")
        runner(
            postprocess_arguments,
            cwd=project_root,
            timeout_seconds=POSTPROCESS_TIMEOUT_SECONDS,
        )
        _require_nonempty_file(staged_source, "FFmpeg post-processing")
        probe_result = runner(
            probe_arguments,
            cwd=staging_root,
            timeout_seconds=PROBE_TIMEOUT_SECONDS,
        )
        probe = _parse_probe_output(probe_result.stdout)

        source_hash = stream_sha256(staged_source)
        source_size = staged_source.stat().st_size
        command_digests = {
            "render_sha256": _command_digest(
                render_arguments, project_root, staging_root, composition_root
            ),
            "postprocess_sha256": _command_digest(
                postprocess_arguments, project_root, staging_root, project_root
            ),
            "probe_sha256": _command_digest(
                probe_arguments, project_root, staging_root, staging_root
            ),
        }
        manifest = _build_manifest(
            contract,
            source_hash=source_hash,
            source_size=source_size,
            versions=versions,
            command_digests=command_digests,
            probe=probe,
            contract_sha256=contract_digest(contract_path),
        )
        staged_manifest.write_bytes(canonical_json_bytes(manifest))
        _publish_with_rollback(
            staged_source,
            staged_manifest,
            published_source,
            published_manifest,
            staging_root,
        )
        return GenerationSummary(
            source_path=published_source,
            manifest_path=published_manifest,
            source_sha256=source_hash,
            manifest_sha256=stream_sha256(published_manifest),
            source_size_bytes=source_size,
        )
    except DemoGenerationError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise DemoGenerationError("demo generation failed") from error
    finally:
        _remove_exact_staging_directory(staging_root, output_root)


def _probe_arguments(path: Path, ffprobe: str) -> list[str]:
    return [
        ffprobe,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]


def _parse_probe_output(stdout: str) -> Mapping[str, object]:
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, Mapping):
            raise TypeError
        streams = payload["streams"]
        format_value = payload["format"]
        if not isinstance(streams, list) or len(streams) != 2:
            raise ValueError
        if not isinstance(format_value, Mapping):
            raise TypeError
        video_streams = [
            stream
            for stream in streams
            if isinstance(stream, Mapping) and stream.get("codec_type") == "video"
        ]
        audio_streams = [
            stream
            for stream in streams
            if isinstance(stream, Mapping) and stream.get("codec_type") == "audio"
        ]
        if len(video_streams) != 1 or len(audio_streams) != 1:
            raise ValueError
        video = video_streams[0]
        audio = audio_streams[0]
        duration = float(format_value["duration"])
        if not math.isfinite(duration) or abs(duration - 42.0) > (1.0 / 24.0):
            raise ValueError
        if (
            video.get("codec_name") != "h264"
            or video.get("width") != 1280
            or video.get("height") != 720
            or video.get("avg_frame_rate") != "24/1"
            or video.get("pix_fmt") != "yuv420p"
            or audio.get("codec_name") != "aac"
            or audio.get("channels") != 2
            or audio.get("sample_rate") != "48000"
        ):
            raise ValueError
        format_name = format_value["format_name"]
        if not isinstance(format_name, str) or "mp4" not in format_name.split(","):
            raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DemoGenerationError(
            "ffprobe output violates the demo media contract"
        ) from error
    return {
        "duration_seconds": duration,
        "format_name": format_name,
        "video": {
            "codec": "h264",
            "width": 1280,
            "height": 720,
            "frame_rate": "24/1",
            "pixel_format": "yuv420p",
        },
        "audio": {"codec": "aac", "channels": 2, "sample_rate_hz": 48000},
    }


def _build_manifest(
    contract: DemoContract,
    *,
    source_hash: str,
    source_size: int,
    versions: Mapping[str, str],
    command_digests: Mapping[str, str],
    probe: Mapping[str, object],
    contract_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "source": {
            "path": SOURCE_FILENAME,
            "sha256": source_hash,
            "byte_size": source_size,
        },
        "contract": {
            "path": CONTRACT_RELATIVE_PATH.as_posix(),
            "sha256": contract_sha256,
        },
        "ranges": {
            "scenes": [
                {
                    "scene_id": scene.scene_id,
                    "start_seconds": scene.start_seconds,
                    "end_seconds": scene.end_seconds,
                }
                for scene in contract.scenes
            ],
            "privacy": {
                "start_seconds": contract.privacy.start_seconds,
                "end_seconds": contract.privacy.end_seconds,
                "box": list(contract.privacy.box),
            },
            "useful_keep_ranges": [
                list(time_range) for time_range in contract.useful_keep_ranges
            ],
        },
        "tools": dict(versions),
        "commands": dict(command_digests),
        "probe": dict(probe),
    }


def _command_digest(
    arguments: Sequence[str],
    project_root: Path,
    staging_root: Path,
    cwd: Path,
) -> str:
    normalized = [
        _replace_path_prefix(argument, staging_root, "<staging>")
        for argument in arguments
    ]
    normalized = [
        _replace_path_prefix(argument, project_root, "<project>")
        for argument in normalized
    ]
    normalized_cwd = _replace_path_prefix(str(cwd), staging_root, "<staging>")
    normalized_cwd = _replace_path_prefix(normalized_cwd, project_root, "<project>")
    payload: dict[str, object] = {"arguments": normalized, "cwd": normalized_cwd}
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _replace_path_prefix(value: str, path: Path, replacement: str) -> str:
    candidates = {str(path.resolve()), path.resolve().as_posix()}
    result = value
    for candidate in sorted(candidates, key=len, reverse=True):
        result = result.replace(candidate, replacement)
    return result.replace("\\", "/")


def _read_version(runner: CommandRunner, arguments: Sequence[str], *, cwd: Path) -> str:
    completed = runner(
        list(arguments), cwd=cwd, timeout_seconds=VERSION_TIMEOUT_SECONDS
    )
    first_line = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    if not first_line:
        raise DemoGenerationError("tool version command returned no version")
    return _scrub_diagnostic(first_line[:512], cwd)


def _require_nonempty_file(path: Path, producer: str) -> None:
    if not path.is_file() or path.stat().st_size <= 0:
        raise DemoGenerationError(f"{producer} did not create a non-empty file")


def _publish_with_rollback(
    staged_source: Path,
    staged_manifest: Path,
    published_source: Path,
    published_manifest: Path,
    staging_root: Path,
) -> None:
    previous_source = staging_root / ".previous-source.mp4"
    previous_manifest = staging_root / ".previous-manifest.json"
    source_was_present = published_source.exists()
    manifest_was_present = published_manifest.exists()
    previous_source_moved = False
    previous_manifest_moved = False
    new_source_published = False
    new_manifest_published = False
    try:
        if source_was_present:
            published_source.replace(previous_source)
            previous_source_moved = True
        if manifest_was_present:
            published_manifest.replace(previous_manifest)
            previous_manifest_moved = True
        staged_source.replace(published_source)
        new_source_published = True
        staged_manifest.replace(published_manifest)
        new_manifest_published = True
    except OSError as error:
        try:
            if new_source_published and published_source.exists():
                published_source.replace(staged_source)
            if new_manifest_published and published_manifest.exists():
                published_manifest.replace(staged_manifest)
            if previous_source_moved and previous_source.exists():
                previous_source.replace(published_source)
            if previous_manifest_moved and previous_manifest.exists():
                previous_manifest.replace(published_manifest)
        except OSError as rollback_error:
            raise DemoGenerationError(
                "atomic publication and rollback failed"
            ) from rollback_error
        raise DemoGenerationError(
            "atomic publication failed; previous files restored"
        ) from error


def _remove_exact_staging_directory(staging_root: Path, output_root: Path) -> None:
    resolved_staging = staging_root.resolve()
    resolved_output = output_root.resolve()
    if (
        resolved_staging.parent != resolved_output
        or not resolved_staging.name.startswith(".staging-")
    ):
        raise DemoGenerationError("refusing unsafe staging cleanup")
    if resolved_staging.exists():
        shutil.rmtree(resolved_staging)


def _scrub_diagnostic(value: str, cwd: Path) -> str:
    candidates: set[str] = set()
    for base in (cwd.resolve(), Path.cwd().resolve(), Path.home().resolve()):
        candidates.add(str(base))
        for parent in base.parents:
            if len(parent.parts) >= 3:
                candidates.add(str(parent))
    scrubbed = value
    for candidate in sorted(candidates, key=len, reverse=True):
        if candidate:
            scrubbed = scrubbed.replace(candidate, "<private-path>")
            scrubbed = scrubbed.replace(candidate.replace("\\", "/"), "<private-path>")
    scrubbed = re.sub(r"[A-Za-z]:[\\/][^\r\n\t]+", "<private-path>", scrubbed)
    return scrubbed[-DIAGNOSTIC_LIMIT:]


def _parse_cli(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--hyperframes",
        default=os.environ.get("VIDEOSCOPE_HYPERFRAMES", "hyperframes"),
    )
    parser.add_argument(
        "--ffmpeg", default=os.environ.get("VIDEOSCOPE_FFMPEG", "ffmpeg")
    )
    parser.add_argument(
        "--ffprobe", default=os.environ.get("VIDEOSCOPE_FFPROBE", "ffprobe")
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_cli(argv)
    os.environ["VIDEOSCOPE_HYPERFRAMES"] = args.hyperframes
    os.environ["VIDEOSCOPE_FFMPEG"] = args.ffmpeg
    os.environ["VIDEOSCOPE_FFPROBE"] = args.ffprobe
    try:
        summary = generate_demo(args.project_root, args.output, force=bool(args.force))
    except DemoGenerationError as error:
        print(f"generation failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "source": SOURCE_FILENAME,
                "manifest": MANIFEST_FILENAME,
                "source_sha256": summary.source_sha256,
                "manifest_sha256": summary.manifest_sha256,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
