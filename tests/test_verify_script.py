from __future__ import annotations

import sys

from scripts.verify import verification_checks


def test_native_rescue_acceptance_runs_in_an_isolated_pytest_process() -> None:
    checks = dict(verification_checks())

    assert checks["pytest (base suite)"] == [
        sys.executable,
        "-m",
        "pytest",
        "--ignore=tests/rescue/test_fixture_rescue.py",
    ]
    assert checks["pytest (isolated native Rescue)"] == [
        sys.executable,
        "-m",
        "pytest",
        "tests/rescue/test_fixture_rescue.py",
    ]
