"""Tests for local runtime diagnostics."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import videoscope.cli as cli
import videoscope.doctor as doctor
from videoscope.doctor import DoctorCheck, DoctorStatus

runner = CliRunner()


def test_python_check_accepts_supported_version() -> None:
    result = doctor.check_python((3, 11, 0))

    assert result.status is DoctorStatus.PASS
    assert "3.11.0" in result.message


def test_external_tool_uses_argument_array(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        assert args == ["ffmpeg", "-version"]
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="ffmpeg version test-build\n",
            stderr="",
        )

    monkeypatch.setattr("videoscope.doctor.subprocess.run", fake_run)

    result = doctor.check_external_tool("ffmpeg")

    assert result.status is DoctorStatus.PASS
    assert result.message == "ffmpeg version test-build"


def test_external_tool_missing_is_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr("videoscope.doctor.subprocess.run", fake_run)

    result = doctor.check_external_tool("ffprobe")

    assert result.status is DoctorStatus.FAIL
    assert "not found" in result.message


def test_cache_directory_supports_spaces_and_chinese(tmp_path: Path) -> None:
    cache_dir = tmp_path / "缓存 目录"

    result = doctor.check_cache_directory(cache_dir)

    assert result.status is DoctorStatus.PASS
    assert cache_dir.is_dir()
    assert not list(cache_dir.iterdir())


def test_doctor_cli_renders_failure_and_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = (
        DoctorCheck("Python", DoctorStatus.PASS, "Supported."),
        DoctorCheck("ffmpeg", DoctorStatus.WARN, "Version unavailable."),
        DoctorCheck("ffprobe", DoctorStatus.FAIL, "Not found."),
    )
    monkeypatch.setattr(cli, "run_doctor", lambda: checks)

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    assert "PASS" in result.stdout
    assert "WARN" in result.stdout
    assert "FAIL" in result.stdout
