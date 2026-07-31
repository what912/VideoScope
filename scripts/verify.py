"""Run VideoScope's cross-platform development verification."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def run_check(label: str, args: list[str], *, repository: Path) -> bool:
    """Run one verification command without a shell."""
    print(f"\n==> {label}", flush=True)
    completed = subprocess.run(
        args,
        check=False,
        cwd=repository,
        shell=False,
    )
    return completed.returncode == 0


def main() -> int:
    """Run lint, format, type, and test checks in the required order."""
    repository = Path(__file__).resolve().parents[1]
    checks = (
        (
            "ruff check",
            [sys.executable, "-m", "ruff", "check", "."],
        ),
        (
            "ruff format --check",
            [sys.executable, "-m", "ruff", "format", "--check", "."],
        ),
        (
            "mypy",
            [sys.executable, "-m", "mypy"],
        ),
        (
            "pytest",
            [sys.executable, "-m", "pytest"],
        ),
    )

    succeeded = True
    for label, args in checks:
        succeeded = run_check(label, args, repository=repository) and succeeded
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
