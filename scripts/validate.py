"""Compatibility entry point for the repository-wide validation rule."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Delegate to the canonical cross-platform verification script."""
    verify_script = Path(__file__).with_name("verify.py")
    completed = subprocess.run(
        [sys.executable, str(verify_script)],
        check=False,
        shell=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
