from __future__ import annotations

import pytest

from videoscope.windows.launcher import (
    CONNECTOR_HOST,
    CONNECTOR_PORT,
    PUBLIC_CONNECT_URL,
    PUBLIC_SITE_ORIGIN,
    ConnectorServerParameters,
    connector_server_parameters,
    parse_arguments,
    should_open_public_site,
)


def test_default_launcher_arguments_match_public_connector_contract() -> None:
    parsed = parse_arguments([])

    assert parsed.port == CONNECTOR_PORT
    assert parsed.headless is False
    assert parsed.shutdown is False
    assert PUBLIC_CONNECT_URL == "https://what912.github.io/VideoScope/connect"


def test_internal_smoke_port_is_validated() -> None:
    assert parse_arguments(["--headless", "--port", "49152"]).headless is True
    with pytest.raises(SystemExit):
        parse_arguments(["--port", "0"])


def test_registered_protocol_is_accepted_but_other_urls_are_rejected() -> None:
    parsed = parse_arguments(["videoscope://start"])

    assert parsed.protocol_url == "videoscope://start"
    with pytest.raises(SystemExit):
        parse_arguments(["videoscope://unexpected"])


def test_public_site_opens_only_after_server_is_ready() -> None:
    assert not should_open_public_site(
        server_started=False,
        already_opened=False,
        closing=False,
    )
    assert should_open_public_site(
        server_started=True,
        already_opened=False,
        closing=False,
    )
    assert not should_open_public_site(
        server_started=True,
        already_opened=True,
        closing=False,
    )
    assert not should_open_public_site(
        server_started=True,
        already_opened=False,
        closing=True,
    )


def test_server_parameters_cannot_expose_connector_to_the_network() -> None:
    parameters = connector_server_parameters(port=8765, pairing_code="pair-123456")

    assert parameters == ConnectorServerParameters(
        host=CONNECTOR_HOST,
        port=8765,
        job_directory=None,
        max_upload_bytes=1024 * 1024 * 1024,
        cpu_concurrency=2,
        heavy_ai_concurrency=1,
        job_ttl_seconds=24 * 60 * 60,
        allow_network=False,
        public_site_origin=PUBLIC_SITE_ORIGIN,
        pairing_code="pair-123456",
        access_log=False,
    )
