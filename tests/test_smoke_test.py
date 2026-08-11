"""Timeout and error-mapping coverage for the clean-wheel smoke runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import smoke_test


def test_offline_install_command_denies_index_and_dependency_resolution(
    tmp_path: Path,
) -> None:
    python = tmp_path / "venv" / "python"
    wheel = tmp_path / "genvideoscope-0.8.0-py3-none-any.whl"

    command = smoke_test.wheel_install_command(
        python,
        wheel,
        offline_installed_dependencies=True,
    )

    assert command == [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--no-deps",
        str(wheel),
    ]


def test_clean_install_command_keeps_normal_dependency_resolution(
    tmp_path: Path,
) -> None:
    python = tmp_path / "venv" / "python"
    wheel = tmp_path / "genvideoscope-0.8.0-py3-none-any.whl"

    command = smoke_test.wheel_install_command(
        python,
        wheel,
        offline_installed_dependencies=False,
    )

    assert "--no-index" not in command
    assert "--no-deps" not in command
    assert command[-1] == str(wheel)


def test_offline_dependency_path_exposes_packages_not_workspace_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace_source = tmp_path / "src"
    dependencies = tmp_path / "venv" / "Lib" / "site-packages"
    workspace_source.mkdir()
    dependencies.mkdir(parents=True)
    monkeypatch.setattr(sys, "path", [str(workspace_source), str(dependencies)])

    inherited = smoke_test.installed_dependency_path()

    assert inherited == str(dependencies.resolve())
    assert str(workspace_source.resolve()) not in inherited


def test_smoke_cache_isolated_from_real_user_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("VIDEOSCOPE_CACHE_DIR", raising=False)

    smoke_test.isolate_smoke_cache(tmp_path)

    assert os.environ["LOCALAPPDATA"] == str(tmp_path / "local-app-data")
    assert os.environ["XDG_CACHE_HOME"] == str(tmp_path / "cache")
    assert os.environ["VIDEOSCOPE_CACHE_DIR"] == str(tmp_path / "videoscope-cache")


def test_smoke_inputs_are_mode_specific_and_offline_generated(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Publish, privacy, and Rescue must not share one fixture contract."""
    observed: list[tuple[list[str], str]] = []

    def record(
        args: list[str], *, cwd: Path, label: str
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == tmp_path
        observed.append((args, label))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(smoke_test, "run_command", record)

    publish, privacy, rescue = smoke_test.prepare_smoke_inputs(
        root=tmp_path,
        requested_video=None,
    )

    assert len({publish, privacy, rescue}) == 3
    assert [label for _args, label in observed] == [
        "Generate Publish smoke fixture",
        "Generate Safe Sharing smoke fixture",
        "Generate Video Rescue smoke fixture",
    ]
    assert "-an" in observed[1][0]
    assert any("eq=brightness=-0.35,noise=" in value for value in observed[2][0])


def test_run_command_maps_a_bounded_timeout_to_smoke_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stuck install or CLI command must not wait for the whole CI job timeout."""
    observed_timeout: list[float | None] = []

    def time_out(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout = kwargs.get("timeout")
        timeout_seconds = float(timeout) if isinstance(timeout, (int, float)) else 0.0
        observed_timeout.append(timeout_seconds)
        raise subprocess.TimeoutExpired(args, timeout_seconds)

    monkeypatch.setattr(subprocess, "run", time_out)

    with pytest.raises(smoke_test.SmokeTestError, match="timed out"):
        smoke_test.run_command(["videoscope", "doctor"], cwd=tmp_path, label="Doctor")

    assert observed_timeout and observed_timeout[0] is not None
    assert observed_timeout[0] > 0


def test_run_command_bounds_and_sanitizes_child_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed clean-wheel command must not echo a private temp path or huge log."""
    private = str(tmp_path.resolve())
    unrelated_private = r"C:\Users\Alice\repository\dist\candidate.whl"

    def fail(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(
            args,
            7,
            stdout=f"installing {unrelated_private}\n" + "x" * 50_000,
            stderr=(
                f"failure while reading {private}\\private.mp4 "
                "and /home/alice/input.mp4"
            ),
        )

    monkeypatch.setattr(subprocess, "run", fail)

    with pytest.raises(smoke_test.SmokeTestError, match="status 7"):
        smoke_test.run_command(["videoscope", "privacy"], cwd=tmp_path, label="Privacy")

    captured = capsys.readouterr()
    assert private.casefold() not in (captured.out + captured.err).casefold()
    assert unrelated_private.casefold() not in (captured.out + captured.err).casefold()
    assert "/home/alice" not in captured.err
    assert len(captured.out) < 20_000


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "private_path",
    [
        r"C:\Users\示例用户\private\candidate.whl",
        r"C:\Users\John Doe\private\candidate.whl",
        "/home/john doe/private/candidate.whl",
        "/root/private/candidate.whl",
    ],
)
def test_safe_diagnostic_sanitizes_personal_home(
    tmp_path: Path,
    private_path: str,
) -> None:
    """Unicode, spaced, and root home paths must not survive diagnostics."""

    sanitized = smoke_test._safe_diagnostic(private_path, tmp_path)

    assert private_path not in sanitized
    assert sanitized == "<private-path>"


def test_main_sanitizes_top_level_path_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Argument resolution errors must not print a caller's personal path."""
    private_wheel = Path(r"C:\Users\Alice\private\missing.whl")
    monkeypatch.setattr(
        smoke_test,
        "parse_args",
        lambda: SimpleNamespace(
            dist=tmp_path / "dist",
            wheel=private_wheel,
            video=None,
        ),
    )

    assert smoke_test.main() == 1

    captured = capsys.readouterr()
    assert str(private_wheel).casefold() not in captured.err.casefold()
    assert "<private-path>" in captured.err


def test_verified_privacy_package_requires_completed_manual_region_workflow(
    tmp_path: Path,
) -> None:
    """Smoke success requires the real verified public package, not a private draft."""
    output = tmp_path / "privacy-smoke"
    public = output / "share-package"
    public.mkdir(parents=True)
    (public / "share-safe.mp4").write_bytes(b"video")
    (public / "verification.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "checks": [
                    {
                        "check_id": "visual_coverage",
                        "status": "passed",
                        "measured": {
                            "actions": 1,
                            "checked_samples": 10,
                            "missing_samples": 0,
                            "uncovered_samples": 0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "changes.json",
        "privacy-summary.json",
        "technical-report.json",
        "manifest.json",
    ):
        (public / name).write_text("{}", encoding="utf-8")

    smoke_test.validate_verified_privacy_package(output)


def test_verified_privacy_package_rejects_needs_review(tmp_path: Path) -> None:
    """A copy needing review cannot satisfy the clean-wheel smoke gate."""
    output = tmp_path / "privacy-smoke"
    public = output / "share-package"
    public.mkdir(parents=True)
    (public / "share-safe.mp4").write_bytes(b"video")
    (public / "verification.json").write_text(
        json.dumps({"status": "needs_review", "checks": []}),
        encoding="utf-8",
    )
    for name in (
        "changes.json",
        "privacy-summary.json",
        "technical-report.json",
        "manifest.json",
    ):
        (public / name).write_text("{}", encoding="utf-8")

    with pytest.raises(smoke_test.SmokeTestError, match="completed"):
        smoke_test.validate_verified_privacy_package(output)


def test_verified_privacy_package_rejects_unexpected_directory(tmp_path: Path) -> None:
    """The public smoke gate must reject directories outside the six-file allowlist."""
    output = tmp_path / "privacy-smoke"
    public = output / "share-package"
    public.mkdir(parents=True)
    (public / "share-safe.mp4").write_bytes(b"video")
    (public / "verification.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "checks": [
                    {
                        "check_id": "visual_coverage",
                        "status": "passed",
                        "measured": {
                            "actions": 1,
                            "checked_samples": 10,
                            "missing_samples": 0,
                            "uncovered_samples": 0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "changes.json",
        "privacy-summary.json",
        "technical-report.json",
        "manifest.json",
    ):
        (public / name).write_text("{}", encoding="utf-8")
    (public / "private-evidence").mkdir()

    with pytest.raises(smoke_test.SmokeTestError, match="public package mismatch"):
        smoke_test.validate_verified_privacy_package(output)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "private_path",
    [
        r"C:\Users\Alice\private\clip.mp4",
        r"C:\Users\示例用户\private\clip.mp4",
        r"C:\Users\John Doe\private\clip.mp4",
        "/Users/alice/private/clip.mp4",
        "/home/alice/private/clip.mp4",
        "/home/john doe/private/clip.mp4",
        "/root/private/clip.mp4",
    ],
)
def test_verified_privacy_package_rejects_any_personal_absolute_path(
    tmp_path: Path,
    private_path: str,
) -> None:
    """Public JSON must not contain a personal path unrelated to the smoke workspace."""
    output = tmp_path / "privacy-smoke"
    public = output / "share-package"
    public.mkdir(parents=True)
    (public / "share-safe.mp4").write_bytes(b"video")
    (public / "verification.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "checks": [
                    {
                        "check_id": "visual_coverage",
                        "status": "passed",
                        "measured": {
                            "actions": 1,
                            "checked_samples": 10,
                            "missing_samples": 0,
                            "uncovered_samples": 0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for name in (
        "changes.json",
        "privacy-summary.json",
        "technical-report.json",
        "manifest.json",
    ):
        (public / name).write_text(
            json.dumps({"source": private_path}),
            encoding="utf-8",
        )

    with pytest.raises(smoke_test.SmokeTestError, match="private data"):
        smoke_test.validate_verified_privacy_package(output)


def test_balanced_rescue_smoke_rejects_a_copied_improved_output(
    tmp_path: Path,
) -> None:
    """Balanced smoke must prove two independent outputs, not two names for one file."""
    public = tmp_path / "balanced" / "rescue-output"
    public.mkdir(parents=True)
    media = b"same encoded video"
    (public / "faithful-rescue.mp4").write_bytes(media)
    (public / "improved-viewing.mp4").write_bytes(media)
    (public / "verification-report.json").write_text(
        json.dumps(
            {
                "faithful_status": "passed",
                "improved_status": "passed",
                "artifacts": [
                    {
                        "relative_path": "faithful-rescue.mp4",
                        "artifact_role": "faithful",
                    },
                    {
                        "relative_path": "improved-viewing.mp4",
                        "artifact_role": "improved",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    validator = getattr(
        smoke_test,
        "validate_verified_rescue_package",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(smoke_test.SmokeTestError, match="independent"):
        validator(tmp_path / "balanced", require_improved=True)
