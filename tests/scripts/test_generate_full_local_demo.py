from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from scripts.full_local_demo_contract import stream_sha256
from scripts.generate_full_local_demo import (
    DemoGenerationError,
    build_postprocess_arguments,
    contract_digest,
    generate_demo,
    probe_demo,
    run_command,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE_NAME = "VideoScope-Full-Local-Demo-Source.mp4"
MANIFEST_NAME = "demo-manifest.json"


@dataclass(frozen=True, slots=True)
class RecordedCall:
    arguments: list[str]
    cwd: Path
    timeout_seconds: float


def _probe_payload(*, duration: str = "42.000000") -> dict[str, object]:
    return {
        "format": {"duration": duration, "format_name": "mov,mp4,m4a,3gp,3g2,mj2"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
                "avg_frame_rate": "24/1",
                "pix_fmt": "yuv420p",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "sample_rate": "48000",
            },
        ],
    }


def _probe_payload_with_extra_stream() -> dict[str, object]:
    payload = _probe_payload()
    streams = payload["streams"]
    assert isinstance(streams, list)
    payload["streams"] = [*streams, {}]
    return payload


class SuccessfulRunner:
    def __init__(self, *, media_bytes: bytes = b"deterministic-mp4") -> None:
        self.calls: list[RecordedCall] = []
        self.media_bytes = media_bytes

    def __call__(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        assert isinstance(arguments, list)
        args = list(arguments)
        self.calls.append(RecordedCall(args, cwd, timeout_seconds))
        assert args

        if len(args) >= 2 and args[1] == "render":
            output_index = args.index("--output") + 1
            Path(args[output_index]).write_bytes(b"clean-base")
            return subprocess.CompletedProcess(args, 0, "rendered\n", "")
        if len(args) >= 2 and args[1] == "-version":
            return subprocess.CompletedProcess(args, 0, "ffmpeg version 8.1.2\n", "")
        if len(args) >= 2 and args[1] == "--version":
            return subprocess.CompletedProcess(args, 0, "hyperframes 0.7.106\n", "")
        if "-show_format" in args:
            assert args[-1].endswith(".mp4")
            return subprocess.CompletedProcess(
                args, 0, json.dumps(_probe_payload()), ""
            )
        if "-filter_complex" in args:
            assert args[1:3] == ["-y", "-i"]
            Path(args[-1]).write_bytes(self.media_bytes)
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected command shape: {args}")


class ProbeFailureRunner(SuccessfulRunner):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        if "-show_format" in args:
            self.calls.append(RecordedCall(args, cwd, timeout_seconds))
            raise DemoGenerationError("ffprobe failed: invalid staged media")
        return super().__call__(args, cwd=cwd, timeout_seconds=timeout_seconds)


def test_run_command_uses_argument_array_no_shell_and_exact_subprocess_options(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        captured["arguments"] = arguments
        captured.update(kwargs)
        return subprocess.CompletedProcess(arguments, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = run_command(["tool", "--flag"], cwd=tmp_path, timeout_seconds=7.5)

    assert result.stdout == "ok"
    assert captured == {
        "arguments": ["tool", "--flag"],
        "cwd": tmp_path,
        "shell": False,
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 7.5,
    }


def test_run_command_scrubs_paths_and_bounds_failure_diagnostics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = str(tmp_path.resolve())
    stderr = "x" * 2300 + f" private={private}"

    def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 9, "", stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DemoGenerationError) as captured:
        run_command(["tool"], cwd=tmp_path, timeout_seconds=1)

    message = str(captured.value)
    assert private not in message
    assert "<private-path>" in message
    assert len(message) < 2100


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("raised", "expected"),
    [
        (subprocess.TimeoutExpired(["tool"], 2), "timed out"),
        (FileNotFoundError("missing private executable"), "executable not found"),
    ],
)
def test_run_command_normalizes_timeout_and_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raised: BaseException,
    expected: str,
) -> None:
    def fake_run(arguments: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise raised

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(DemoGenerationError, match=expected) as captured:
        run_command(["private-tool"], cwd=tmp_path, timeout_seconds=2)
    assert "private-tool" not in str(captured.value)


def test_postprocess_has_exact_bounded_conditions_codecs_and_muxing() -> None:
    args = build_postprocess_arguments(Path("base.mp4"), Path("final.mp4"), "ffmpeg")
    joined = " ".join(args)

    assert args[:4] == ["ffmpeg", "-y", "-i", "base.mp4"]
    assert "aevalsrc=" in joined
    assert "random" not in joined
    assert "between(t,5,10)" in joined
    assert "between(t,25,32)" in joined
    assert "between(t,32,36)" in joined
    assert "boxblur" in joined and "eq=" in joined
    assert "crop=iw-32:ih-18" in joined
    assert "sin(" in joined and "scale=1280:720" in joined and "overlay=" in joined
    assert "220" in joined and "60" in joined and "118" in joined and "880" in joined
    for expected in (
        "libx264",
        "medium",
        "16",
        "48",
        "yuv420p",
        "aac",
        "192k",
        "stereo",
        "48000",
        "+bitexact",
    ):
        assert expected in args or expected in joined
    assert "scenecut=0" in joined
    assert "+faststart" not in joined
    assert "VideoScope Full Local Four-Mode Demo" in joined
    assert "demo.user@example.invalid" in joined
    assert "+1 202-555-0107" in joined
    assert "00.0000, 000.0000" in joined


def test_probe_demo_requests_strict_json_and_normalizes_valid_streams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    calls: list[list[str]] = []

    def fake_run(
        arguments: Sequence[str], *, cwd: Path, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        args = list(arguments)
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(_probe_payload()), "")

    monkeypatch.setattr("scripts.generate_full_local_demo.run_command", fake_run)
    result = probe_demo(source, "ffprobe")

    assert calls == [
        [
            "ffprobe",
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(source),
        ]
    ]
    assert result == {
        "duration_seconds": 42.0,
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "video": {
            "codec": "h264",
            "width": 1280,
            "height": 720,
            "frame_rate": "24/1",
            "pixel_format": "yuv420p",
        },
        "audio": {"codec": "aac", "channels": 2, "sample_rate_hz": 48000},
    }


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "payload",
    [
        {"format": {}, "streams": []},
        _probe_payload(duration="42.042000"),
        _probe_payload(duration="NaN"),
        _probe_payload_with_extra_stream(),
    ],
)
def test_probe_demo_rejects_missing_extra_or_out_of_tolerance_stream_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, payload: dict[str, object]
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")

    def fake_run(
        arguments: Sequence[str], *, cwd: Path, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(arguments), 0, json.dumps(payload), "")

    monkeypatch.setattr("scripts.generate_full_local_demo.run_command", fake_run)
    with pytest.raises(DemoGenerationError, match="ffprobe"):
        probe_demo(source, "ffprobe")


def test_generate_uses_private_staging_and_publishes_only_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIDEOSCOPE_HYPERFRAMES", "hyperframes")
    monkeypatch.setenv("VIDEOSCOPE_FFMPEG", "ffmpeg")
    monkeypatch.setenv("VIDEOSCOPE_FFPROBE", "ffprobe")
    output = tmp_path / "输出 with space"
    runner = SuccessfulRunner()

    summary = generate_demo(ROOT, output, force=True, runner=runner)

    assert summary.source_path == output / SOURCE_NAME
    assert summary.manifest_path == output / MANIFEST_NAME
    assert summary.source_path.read_bytes() == b"deterministic-mp4"
    assert not list(output.glob(".staging-*"))
    render = next(call for call in runner.calls if call.arguments[1] == "render")
    assert render.cwd == ROOT / "demos" / "full-local-four-mode"
    assert render.arguments[2:8] == [
        "--output",
        render.arguments[3],
        "--fps",
        "24",
        "--quality",
        "high",
    ]
    assert render.arguments[8] == "--strict"
    assert Path(render.arguments[3]).parent.name.startswith(".staging-")


def test_source_is_immutable_without_force(tmp_path: Path) -> None:
    output = tmp_path / "out"
    generate_demo(ROOT, output, force=True, runner=SuccessfulRunner())

    with pytest.raises(DemoGenerationError, match="force"):
        generate_demo(ROOT, output, force=False, runner=SuccessfulRunner())


def test_failed_second_generation_keeps_previous_published_source_and_manifest(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out"
    first = generate_demo(ROOT, output, force=True, runner=SuccessfulRunner())
    original_source = first.source_path.read_bytes()
    original_manifest = first.manifest_path.read_bytes()

    with pytest.raises(DemoGenerationError, match="ffprobe"):
        generate_demo(ROOT, output, force=True, runner=ProbeFailureRunner())

    assert first.source_path.read_bytes() == original_source
    assert first.manifest_path.read_bytes() == original_manifest
    assert not list(output.glob(".staging-*"))


def test_publication_failure_rolls_back_both_previous_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    first = generate_demo(ROOT, output, force=True, runner=SuccessfulRunner())
    original_source = first.source_path.read_bytes()
    original_manifest = first.manifest_path.read_bytes()
    original_replace = Path.replace

    def fail_moving_previous_manifest(self: Path, target: Path) -> Path:
        if self == first.manifest_path and target.name == ".previous-manifest.json":
            raise OSError("injected publication failure")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_moving_previous_manifest)
    with pytest.raises(DemoGenerationError, match="atomic publication"):
        generate_demo(
            ROOT,
            output,
            force=True,
            runner=SuccessfulRunner(media_bytes=b"replacement-media"),
        )

    assert first.source_path.read_bytes() == original_source
    assert first.manifest_path.read_bytes() == original_manifest
    assert not list(output.glob(".staging-*"))


def test_fake_two_run_outputs_and_canonical_manifests_are_byte_identical(
    tmp_path: Path,
) -> None:
    first_output = tmp_path / "first"
    second_output = tmp_path / "second"
    first = generate_demo(ROOT, first_output, force=True, runner=SuccessfulRunner())
    second = generate_demo(ROOT, second_output, force=True, runner=SuccessfulRunner())

    assert first.source_path.read_bytes() == second.source_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert stream_sha256(first.source_path) == first.source_sha256
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    serialized = first.manifest_path.read_text(encoding="utf-8")
    assert str(tmp_path.resolve()) not in serialized
    assert "commands" in manifest
    assert all(key.endswith("_sha256") for key in manifest["commands"])
    assert manifest["source"]["path"] == SOURCE_NAME
    assert manifest["contract"]["path"] == (
        "demos/full-local-four-mode/demo-contract.json"
    )
    assert manifest["ranges"]["privacy"] == {
        "start_seconds": 25.0,
        "end_seconds": 32.0,
        "box": [0.58, 0.18, 0.94, 0.78],
    }


def test_contract_digest_changes_when_a_contract_value_changes(tmp_path: Path) -> None:
    original = ROOT / "demos" / "full-local-four-mode" / "demo-contract.json"
    changed = tmp_path / "demo-contract.json"
    payload = json.loads(original.read_text(encoding="utf-8"))
    payload["scenes"][0]["purpose"] = "Changed deterministic purpose."
    changed.write_text(json.dumps(payload), encoding="utf-8")

    assert contract_digest(original) != contract_digest(changed)
