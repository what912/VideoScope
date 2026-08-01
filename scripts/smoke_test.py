"""Install a built wheel into a clean environment and exercise the public CLI."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

EXPECTED_VERSION = "VideoScope 0.2.0rc1"
EXPECTED_DISTRIBUTION_PREFIX = "genvideoscope-0.2.0rc1-"


class SmokeTestError(RuntimeError):
    """Actionable wheel smoke-test failure."""


def select_wheel(dist: Path) -> Path:
    """Select the single release-candidate wheel from a distribution directory."""
    wheels = sorted(dist.glob(f"{EXPECTED_DISTRIBUTION_PREFIX}*.whl"))
    if len(wheels) != 1:
        raise SmokeTestError(
            f"Expected exactly one {EXPECTED_DISTRIBUTION_PREFIX} wheel in {dist}; "
            f"found {len(wheels)}."
        )
    return wheels[0].resolve()


def environment_python(environment: Path) -> Path:
    """Return the virtual environment interpreter on the current platform."""
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def run_command(
    args: list[str],
    *,
    cwd: Path,
    label: str,
) -> subprocess.CompletedProcess[str]:
    """Run one smoke-test command without a shell and require success."""
    print(f"==> {label}", flush=True)
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.stdout:
        print(completed.stdout.rstrip())
    if completed.stderr:
        print(completed.stderr.rstrip(), file=sys.stderr)
    if completed.returncode != 0:
        raise SmokeTestError(f"{label} exited with status {completed.returncode}.")
    return completed


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dist",
        type=Path,
        default=Path("dist"),
        help="Directory containing the wheel.",
    )
    parser.add_argument(
        "--video",
        type=Path,
        required=True,
        help="A small local video used for the analyze smoke test.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the clean-environment installation and CLI smoke test."""
    args = parse_args()
    try:
        wheel = select_wheel(args.dist)
        video = args.video.resolve(strict=True)
        with tempfile.TemporaryDirectory(prefix="videoscope-smoke-") as temporary:
            root = Path(temporary)
            environment = root / "venv"
            output = root / "output"
            venv.EnvBuilder(with_pip=True, clear=True).create(environment)
            python = environment_python(environment)

            run_command(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    str(wheel),
                ],
                cwd=root,
                label="Install wheel",
            )
            version = run_command(
                [str(python), "-m", "videoscope", "--version"],
                cwd=root,
                label="Check version",
            )
            if EXPECTED_VERSION not in version.stdout:
                raise SmokeTestError(
                    f"Unexpected version output: {version.stdout.strip()!r}"
                )
            run_command(
                [str(python), "-m", "videoscope", "doctor"],
                cwd=root,
                label="Run doctor",
            )
            run_command(
                [
                    str(python),
                    "-m",
                    "videoscope",
                    "analyze",
                    str(video),
                    "--output",
                    str(output),
                    "--quiet",
                ],
                cwd=root,
                label="Analyze fixture",
            )
            required_outputs = (output / "report.json", output / "report.html")
            missing = [path.name for path in required_outputs if not path.is_file()]
            if missing:
                raise SmokeTestError(
                    f"Analyze did not create required outputs: {', '.join(missing)}."
                )
    except (OSError, SmokeTestError) as exc:
        print(f"Smoke test failed: {exc}", file=sys.stderr)
        return 1

    print("PASS clean wheel installation and analysis")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
