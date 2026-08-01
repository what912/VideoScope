"""Tests for the deterministic FFmpeg fixture factory."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts import generate_test_videos as factory

EXPECTED_FILENAMES = {
    "black_segment.mp4",
    "blur_segment.mp4",
    "clean_motion.mp4",
    "flicker_segment.mp4",
    "freeze_segment.mp4",
    "scene_cut.mp4",
    "stable_text.mp4",
    "changing_text.mp4",
}


def test_manifest_matches_canonical_factory_data() -> None:
    manifest_path = Path(__file__).parent / "fixtures" / "manifest.json"
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert on_disk == factory.manifest_data()
    assert set(on_disk["videos"]) == EXPECTED_FILENAMES


def test_ffmpeg_command_is_an_argument_array(tmp_path: Path) -> None:
    spec = factory.fixture_specs()[0]
    output_path = tmp_path / "含 空格" / spec.filename

    command = factory.build_ffmpeg_command(
        ffmpeg="ffmpeg",
        spec=spec,
        output_path=output_path,
    )

    assert isinstance(command, list)
    assert command[0] == "ffmpeg"
    assert command[-1] == str(output_path)
    assert "-vf" in command
    assert "-y" in command


def test_program_generated_text_sequences_are_stable_and_change(
    tmp_path: Path,
) -> None:
    stable = factory.generate_text_frames(tmp_path / "stable", mode="stable")
    changing = factory.generate_text_frames(tmp_path / "changing", mode="changing")

    assert len(stable) == len(changing) == 60
    assert stable[10].read_bytes() == stable[30].read_bytes()
    assert changing[10].read_bytes() != changing[30].read_bytes()
    assert changing[20].read_bytes() != changing[22].read_bytes()


def test_run_checked_disables_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args == ["ffmpeg", "-version"]
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="ffmpeg version test",
            stderr="",
        )

    monkeypatch.setattr("scripts.generate_test_videos.subprocess.run", fake_run)

    result = factory.run_checked(["ffmpeg", "-version"])

    assert result.returncode == 0


def test_force_replaces_old_files_with_mocked_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output_directory = tmp_path / "中文 fixtures"
    output_directory.mkdir()
    old_path = output_directory / "clean_motion.mp4"
    old_path.write_bytes(b"old")

    def fake_run_checked(
        args: Sequence[str],
        *,
        timeout_seconds: float = 60.0,
    ) -> subprocess.CompletedProcess[str]:
        output_path = Path(args[-1])
        output_path.write_bytes(b"generated")
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=0,
            stdout="",
            stderr="",
        )

    def fake_validate_video(
        *,
        ffmpeg: str,
        ffprobe: str,
        video_path: Path,
    ) -> factory.ProbeResult:
        assert ffmpeg == "fake-ffmpeg"
        assert ffprobe == "fake-ffprobe"
        assert video_path.read_bytes() == b"generated"
        return factory.ProbeResult(6.0, 320, 180, 10.0)

    monkeypatch.setattr(factory, "run_checked", fake_run_checked)
    monkeypatch.setattr(factory, "validate_video", fake_validate_video)

    generated = factory.generate_fixtures(
        output_directory=output_directory,
        manifest_path=tmp_path / "manifest.json",
        ffmpeg="fake-ffmpeg",
        ffprobe="fake-ffprobe",
        force=True,
    )

    assert {path.name for path in generated} == EXPECTED_FILENAMES
    assert old_path.read_bytes() == b"generated"


def test_missing_ffmpeg_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "scripts.generate_test_videos.shutil.which",
        lambda name: None,
    )

    exit_code = factory.main(["--force"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "Missing required system executable" in captured.err
    assert "ffmpeg" in captured.err
    assert "ffprobe" in captured.err


def test_real_ffmpeg_factory_when_available(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("FFmpeg and ffprobe are required for the integration fixture test")
    assert ffmpeg is not None
    assert ffprobe is not None

    generated = factory.generate_fixtures(
        output_directory=tmp_path / "generated videos",
        manifest_path=tmp_path / "manifest.json",
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        force=True,
    )

    assert {path.name for path in generated} == EXPECTED_FILENAMES
    assert all(path.stat().st_size > 0 for path in generated)
