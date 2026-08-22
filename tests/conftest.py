"""Repository-wide test safety fixtures."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from tests.rescue.clarity_runtime_provenance import (
    CLARITY_CALL_REPORT_KEY,
    EXACT_CLARITY_NODE_ID,
    ClarityPytestCallReport,
    ClarityRuntimeGuard,
)


@pytest.fixture(autouse=True)  # type: ignore[untyped-decorator]
def block_non_loopback_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail tests before they can connect to an external network address."""
    original_connect = socket.socket.connect

    def guarded_connect(
        instance: socket.socket,
        address: Any,
    ) -> None:
        if not isinstance(address, tuple):
            original_connect(instance, address)
            return
        host = str(address[0]).strip().casefold()
        if host == "localhost":
            original_connect(instance, address)
            return
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise RuntimeError(
                f"Tests may not access non-loopback network host {host!r}."
            )
        original_connect(instance, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.hookimpl(wrapper=True)  # type: ignore[untyped-decorator]
def pytest_runtest_makereport(
    item: pytest.Item,
    call: pytest.CallInfo[None],
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Retain only sanitized call outcome facts for the exact-node finalizer."""

    report = yield
    if report.when == "call":
        outcome = cast(Literal["passed", "failed", "skipped"], report.outcome)
        exception_type = None if call.excinfo is None else type(call.excinfo.value)
        item.stash[CLARITY_CALL_REPORT_KEY] = ClarityPytestCallReport(
            outcome=outcome,
            exception_type=exception_type,
        )
    return report


@pytest.fixture(autouse=True)  # type: ignore[untyped-decorator]
def clarity_runtime_provenance_guard(
    request: pytest.FixtureRequest,
) -> Iterator[ClarityRuntimeGuard | None]:
    """Install the clarity provenance guard for exactly one native node."""

    if request.node.nodeid != EXACT_CLARITY_NODE_ID:
        yield None
        return
    tmp_path = request.getfixturevalue("tmp_path")
    if not isinstance(tmp_path, Path):
        raise TypeError("pytest tmp_path fixture did not return a Path")
    guard = ClarityRuntimeGuard(tmp_path / "clarity-runtime-provenance")
    guard.start()
    try:
        yield guard
    finally:
        guard.finalize_from_pytest_item(request.node)
