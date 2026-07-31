"""Repository-wide test safety fixtures."""

from __future__ import annotations

import ipaddress
import socket
from typing import Any

import pytest


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
