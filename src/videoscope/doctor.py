"""Local runtime diagnostics for the VideoScope CLI."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from platformdirs import user_cache_dir
from rich.console import Console
from rich.table import Table

MINIMUM_PYTHON = (3, 11)


class DoctorStatus(StrEnum):
    """Outcome of one doctor check."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One deterministic doctor check result."""

    name: str
    status: DoctorStatus
    message: str


def check_python(
    version_info: tuple[int, int, int] | None = None,
) -> DoctorCheck:
    """Check that the active interpreter meets the supported minimum."""
    current = version_info or tuple(sys.version_info[:3])
    version = ".".join(str(part) for part in current)
    if current[:2] >= MINIMUM_PYTHON:
        return DoctorCheck(
            name="Python",
            status=DoctorStatus.PASS,
            message=f"Python {version} meets the >=3.11 requirement.",
        )
    return DoctorCheck(
        name="Python",
        status=DoctorStatus.FAIL,
        message=f"Python {version} is unsupported; Python 3.11+ is required.",
    )


def check_external_tool(
    command: str,
    *,
    timeout_seconds: float = 5.0,
) -> DoctorCheck:
    """Check an external executable using a shell-free argument array."""
    try:
        completed = subprocess.run(
            [command, "-version"],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError:
        return DoctorCheck(
            name=command,
            status=DoctorStatus.FAIL,
            message=f"{command} was not found on PATH.",
        )
    except subprocess.TimeoutExpired:
        return DoctorCheck(
            name=command,
            status=DoctorStatus.FAIL,
            message=f"{command} did not respond within {timeout_seconds:g} seconds.",
        )
    except OSError as exc:
        return DoctorCheck(
            name=command,
            status=DoctorStatus.FAIL,
            message=f"{command} could not be started: {type(exc).__name__}.",
        )

    if completed.returncode != 0:
        return DoctorCheck(
            name=command,
            status=DoctorStatus.FAIL,
            message=f"{command} exited with status {completed.returncode}.",
        )

    output = completed.stdout.strip() or completed.stderr.strip()
    if not output:
        return DoctorCheck(
            name=command,
            status=DoctorStatus.WARN,
            message=f"{command} ran successfully but did not report a version.",
        )

    version_line = output.splitlines()[0]
    return DoctorCheck(
        name=command,
        status=DoctorStatus.PASS,
        message=version_line,
    )


def check_cache_directory(cache_dir: Path | None = None) -> DoctorCheck:
    """Verify that the platform-appropriate cache directory is writable."""
    path = cache_dir or Path(user_cache_dir("videoscope", "VideoScope"))
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(
            dir=path,
            prefix=".videoscope-write-test-",
        ) as probe:
            probe.write(b"ok")
            probe.flush()
    except OSError as exc:
        return DoctorCheck(
            name="Cache directory",
            status=DoctorStatus.FAIL,
            message=f"Cache directory is not writable: {type(exc).__name__}.",
        )

    return DoctorCheck(
        name="Cache directory",
        status=DoctorStatus.PASS,
        message="The platform cache directory is writable.",
    )


def run_doctor(cache_dir: Path | None = None) -> tuple[DoctorCheck, ...]:
    """Run all local checks in a stable order."""
    return (
        check_python(),
        check_external_tool("ffmpeg"),
        check_external_tool("ffprobe"),
        check_cache_directory(cache_dir),
    )


def has_failures(checks: Sequence[DoctorCheck]) -> bool:
    """Return whether any doctor check failed."""
    return any(check.status is DoctorStatus.FAIL for check in checks)


def render_doctor(
    checks: Sequence[DoctorCheck],
    *,
    console: Console | None = None,
) -> None:
    """Render doctor results with clear pass, warning, and failure states."""
    output = console or Console()
    table = Table(title="VideoScope doctor")
    table.add_column("Check", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Details")
    styles = {
        DoctorStatus.PASS: "green",
        DoctorStatus.WARN: "yellow",
        DoctorStatus.FAIL: "red",
    }
    for check in checks:
        table.add_row(
            check.name,
            f"[{styles[check.status]}]{check.status}[/{styles[check.status]}]",
            check.message,
        )
    output.print(table)
