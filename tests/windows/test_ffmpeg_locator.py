from __future__ import annotations

import os
import subprocess
from pathlib import Path

from videoscope.windows.ffmpeg_locator import (
    FFmpegTools,
    detect_ffmpeg,
    install_ffmpeg_with_winget,
    locate_ffmpeg_tools,
    process_environment_with_tools,
    winget_install_command,
)


def _missing(_name: str) -> None:
    return None


def test_locator_requires_both_tools_and_supports_unicode_paths(tmp_path: Path) -> None:
    directory = tmp_path / "媒体 工具"
    directory.mkdir()
    (directory / "ffmpeg.exe").write_bytes(b"")

    assert (
        locate_ffmpeg_tools(candidate_directories=(directory,), which=_missing) is None
    )

    (directory / "ffprobe.exe").write_bytes(b"")
    tools = locate_ffmpeg_tools(candidate_directories=(directory,), which=_missing)

    assert tools == FFmpegTools(
        ffmpeg=(directory / "ffmpeg.exe").resolve(),
        ffprobe=(directory / "ffprobe.exe").resolve(),
    )


def test_detect_executes_argument_vectors_and_records_versions(tmp_path: Path) -> None:
    directory = tmp_path / "bin"
    directory.mkdir()
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        (directory / name).write_bytes(b"")
    observed: list[list[str]] = []

    def runner(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed.append(arguments)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            arguments,
            0,
            stdout=f"{Path(arguments[0]).stem} version 9.0\n",
            stderr="",
        )

    status = detect_ffmpeg(
        candidate_directories=(directory,),
        which=_missing,
        runner=runner,
    )

    assert status.ready is True
    assert status.ffmpeg_version == "ffmpeg version 9.0"
    assert status.ffprobe_version == "ffprobe version 9.0"
    assert [arguments[1:] for arguments in observed] == [["-version"], ["-version"]]


def test_process_environment_only_prepends_resolved_tool_directories(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one"
    second = tmp_path / "two"
    tools = FFmpegTools(first / "ffmpeg.exe", second / "ffprobe.exe")

    environment = process_environment_with_tools(
        tools,
        environment={"PATH": "existing", "SAFE": "value"},
    )

    assert environment["PATH"] == f"{first}{os.pathsep}{second}{os.pathsep}existing"
    assert environment["SAFE"] == "value"


def test_winget_install_is_exact_explicit_and_shell_free(tmp_path: Path) -> None:
    winget = tmp_path / "winget.exe"
    observed: list[list[str]] = []

    def runner(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        observed.append(arguments)
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(arguments, 0, stdout="ok", stderr="")

    result = install_ffmpeg_with_winget(winget, runner=runner)

    assert result.succeeded is True
    assert tuple(observed[0]) == winget_install_command(winget)
    assert "Gyan.FFmpeg" in observed[0]
    assert "--exact" in observed[0]
    assert "--disable-interactivity" in observed[0]
