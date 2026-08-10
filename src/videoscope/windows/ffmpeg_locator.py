"""Locate and validate external FFmpeg tools without bundling their binaries."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

_TOOL_TIMEOUT_SECONDS = 10.0
_WINGET_PACKAGE_ID = "Gyan.FFmpeg"


@dataclass(frozen=True, slots=True)
class FFmpegTools:
    """Resolved external executable paths."""

    ffmpeg: Path
    ffprobe: Path

    @property
    def directories(self) -> tuple[Path, ...]:
        """Return stable unique directories for process-local PATH injection."""
        result: list[Path] = []
        for directory in (self.ffmpeg.parent, self.ffprobe.parent):
            if directory not in result:
                result.append(directory)
        return tuple(result)


@dataclass(frozen=True, slots=True)
class FFmpegStatus:
    """Actionable external-tool readiness result."""

    tools: FFmpegTools | None
    ffmpeg_version: str | None = None
    ffprobe_version: str | None = None
    message: str = "FFmpeg and ffprobe were not found."

    @property
    def ready(self) -> bool:
        return self.tools is not None


@dataclass(frozen=True, slots=True)
class WingetInstallResult:
    """Sanitized result of an explicit Winget install attempt."""

    succeeded: bool
    return_code: int
    message: str


def _candidate_directories(environment: Mapping[str, str]) -> tuple[Path, ...]:
    """Return bounded common package-manager locations in deterministic order."""
    candidates: list[Path] = []
    path_value = environment.get("PATH", "")
    candidates.extend(Path(item) for item in path_value.split(os.pathsep) if item)

    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        package_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
        if package_root.is_dir():
            try:
                for package in sorted(package_root.glob("Gyan.FFmpeg_*")):
                    candidates.extend(sorted(package.glob("ffmpeg-*/*bin")))
            except OSError:
                pass

    program_data = environment.get("ProgramData")
    if program_data:
        chocolatey = Path(program_data) / "chocolatey"
        candidates.append(chocolatey / "bin")
        ffmpeg_tools = chocolatey / "lib" / "ffmpeg" / "tools"
        if ffmpeg_tools.is_dir():
            try:
                candidates.extend(sorted(ffmpeg_tools.glob("ffmpeg-*/*bin")))
                candidates.extend(sorted(ffmpeg_tools.glob("*bin")))
            except OSError:
                pass

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return tuple(unique)


def _tool_in_directories(name: str, directories: Iterable[Path]) -> Path | None:
    filenames = (f"{name}.exe", name)
    for directory in directories:
        for filename in filenames:
            candidate = directory / filename
            try:
                if candidate.is_file():
                    return candidate.resolve(strict=True)
            except OSError:
                continue
    return None


def locate_ffmpeg_tools(
    *,
    environment: Mapping[str, str] | None = None,
    candidate_directories: Iterable[Path] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> FFmpegTools | None:
    """Locate both tools without changing the machine or current process."""
    effective_environment = os.environ if environment is None else environment
    directories = (
        tuple(candidate_directories)
        if candidate_directories is not None
        else _candidate_directories(effective_environment)
    )

    ffmpeg_location = which("ffmpeg")
    ffprobe_location = which("ffprobe")
    ffmpeg = (
        Path(ffmpeg_location).resolve(strict=False)
        if ffmpeg_location
        else _tool_in_directories("ffmpeg", directories)
    )
    ffprobe = (
        Path(ffprobe_location).resolve(strict=False)
        if ffprobe_location
        else _tool_in_directories("ffprobe", directories)
    )
    if ffmpeg is None or ffprobe is None:
        return None
    return FFmpegTools(ffmpeg=ffmpeg, ffprobe=ffprobe)


def _version_line(
    executable: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str | None:
    try:
        completed = runner(
            [str(executable), "-version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TOOL_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    output = completed.stdout or completed.stderr or ""
    return next((line.strip() for line in output.splitlines() if line.strip()), None)


def detect_ffmpeg(
    *,
    environment: Mapping[str, str] | None = None,
    candidate_directories: Iterable[Path] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> FFmpegStatus:
    """Locate and execute both external tools before declaring readiness."""
    tools = locate_ffmpeg_tools(
        environment=environment,
        candidate_directories=candidate_directories,
        which=which,
    )
    if tools is None:
        return FFmpegStatus(
            tools=None,
            message="未找到 FFmpeg 和 ffprobe，完整视频处理暂不可用。",
        )
    ffmpeg_version = _version_line(tools.ffmpeg, runner=runner)
    ffprobe_version = _version_line(tools.ffprobe, runner=runner)
    if ffmpeg_version is None or ffprobe_version is None:
        return FFmpegStatus(
            tools=None,
            message="检测到了 FFmpeg 文件，但版本验证失败。",
        )
    return FFmpegStatus(
        tools=tools,
        ffmpeg_version=ffmpeg_version,
        ffprobe_version=ffprobe_version,
        message="FFmpeg 和 ffprobe 已就绪。",
    )


def process_environment_with_tools(
    tools: FFmpegTools,
    *,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a process-local environment with the detected tools first on PATH."""
    result = dict(os.environ if environment is None else environment)
    existing = result.get("PATH", "")
    prefix = os.pathsep.join(str(path) for path in tools.directories)
    result["PATH"] = prefix if not existing else f"{prefix}{os.pathsep}{existing}"
    return result


def find_winget(*, which: Callable[[str], str | None] = shutil.which) -> Path | None:
    """Return the existing Windows Package Manager executable, if available."""
    location = which("winget")
    return Path(location).resolve(strict=False) if location else None


def winget_install_command(winget: Path) -> tuple[str, ...]:
    """Build the exact, non-shell Winget command shown to and approved by users."""
    return (
        str(winget),
        "install",
        "--id",
        _WINGET_PACKAGE_ID,
        "--exact",
        "--source",
        "winget",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    )


def install_ffmpeg_with_winget(
    winget: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> WingetInstallResult:
    """Install only after UI consent and return no raw machine-specific output."""
    try:
        completed = runner(
            list(winget_install_command(winget)),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15 * 60,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return WingetInstallResult(False, -1, "Winget 无法启动或执行超时。")
    if completed.returncode != 0:
        return WingetInstallResult(
            False,
            completed.returncode,
            "Winget 未能安装 FFmpeg，请使用手动安装说明。",
        )
    return WingetInstallResult(True, 0, "Winget 已完成，正在重新检查 FFmpeg。")


__all__ = [
    "FFmpegStatus",
    "FFmpegTools",
    "WingetInstallResult",
    "detect_ffmpeg",
    "find_winget",
    "install_ffmpeg_with_winget",
    "locate_ffmpeg_tools",
    "process_environment_with_tools",
    "winget_install_command",
]
