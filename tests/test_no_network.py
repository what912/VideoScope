"""Proof that the base test suite blocks accidental internet access."""

from __future__ import annotations

import socket

import pytest


def test_external_socket_connections_are_blocked() -> None:
    sock = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="may not access"):
            sock.connect(("example.invalid", 443))
    finally:
        sock.close()
