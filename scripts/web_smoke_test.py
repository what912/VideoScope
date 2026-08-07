"""Install an exact wheel with the Web extra and probe the loopback API."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import venv
from pathlib import Path

TIMEOUT_SECONDS = 180.0


def _python(environment: Path) -> Path:
    if sys.platform == "win32":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--ffmpeg-bin", type=Path)
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    wheel = arguments.wheel.resolve(strict=True)
    environment_variables = os.environ.copy()
    if arguments.ffmpeg_bin is not None:
        tool_directory = arguments.ffmpeg_bin.resolve(strict=True)
        environment_variables["PATH"] = (
            str(tool_directory) + os.pathsep + environment_variables.get("PATH", "")
        )
    with tempfile.TemporaryDirectory(prefix="videoscope-web-smoke-") as temporary:
        root = Path(temporary)
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = _python(environment)
        install = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                f"{wheel}[web]",
            ],
            check=False,
            shell=False,
            env=environment_variables,
        )
        if install.returncode != 0:
            print("Web smoke failed: exact wheel installation failed.", file=sys.stderr)
            return 1
        port = _available_port()
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "videoscope",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--job-directory",
                str(root / "jobs"),
            ],
            shell=False,
            env=environment_variables,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + TIMEOUT_SECONDS
            health: dict[str, object] | None = None
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/api/health", timeout=2
                    ) as response:
                        health = json.loads(response.read().decode("utf-8"))
                    break
                except (OSError, urllib.error.URLError, json.JSONDecodeError):
                    time.sleep(0.1)
            if health is None or health.get("local_only_default") is not True:
                print(
                    "Web smoke failed: loopback health did not pass.", file=sys.stderr
                )
                return 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    print("PASS exact-wheel Web extra and loopback /api/health")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
