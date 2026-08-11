"""Run VideoScope's cross-platform development verification."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_ISOLATED_NATIVE_TESTS = ("tests/rescue/test_fixture_rescue.py",)


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


def verification_checks() -> tuple[tuple[str, list[str]], ...]:
    """Return the stable verification commands in their execution order."""
    return (
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
            "pytest (base suite)",
            [
                sys.executable,
                "-m",
                "pytest",
                *(f"--ignore={path}" for path in _ISOLATED_NATIVE_TESTS),
            ],
        ),
        (
            "pytest (isolated native Rescue)",
            [sys.executable, "-m", "pytest", *_ISOLATED_NATIVE_TESTS],
        ),
    )


def main() -> int:
    """Run lint, format, type, and test checks in the required order."""
    repository = Path(__file__).resolve().parents[1]
    succeeded = True
    for label, args in verification_checks():
        succeeded = run_check(label, args, repository=repository) and succeeded
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
