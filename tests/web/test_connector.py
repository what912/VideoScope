from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from videoscope.web.app import create_app
from videoscope.web.connector import ConnectorSessionStore
from videoscope.web.models import WebServerConfig

PUBLIC_ORIGIN = "https://what912.github.io"


def _config(tmp_path: Path) -> WebServerConfig:
    return WebServerConfig(
        job_root=tmp_path / "connector-jobs",
        connector_pairing_code="pair-123456",
        allowed_browser_origins=(PUBLIC_ORIGIN,),
    )


def _pair(client: TestClient) -> str:
    response = client.post(
        "/api/connector/sessions",
        headers={"Origin": PUBLIC_ORIGIN},
        json={"pairing_code": "pair-123456"},
    )
    assert response.status_code == 200
    return str(response.json()["session_token"])


def test_public_origin_requires_pairing_and_gets_exact_cors(tmp_path: Path) -> None:
    with TestClient(
        create_app(_config(tmp_path)), base_url="http://127.0.0.1:8765"
    ) as client:
        status = client.get("/api/connector/status", headers={"Origin": PUBLIC_ORIGIN})
        assert status.status_code == 200
        assert status.headers["access-control-allow-origin"] == PUBLIC_ORIGIN
        assert status.json()["credentials_persisted"] is False
        assert isinstance(status.json()["ffmpeg_available"], bool)
        assert isinstance(status.json()["ffprobe_available"], bool)
        assert status.json()["status"] in {"ready", "degraded"}

        blocked = client.get(
            "/api/connector/providers", headers={"Origin": PUBLIC_ORIGIN}
        )
        assert blocked.status_code == 401
        token = _pair(client)
        allowed = client.get(
            "/api/connector/providers",
            headers={
                "Origin": PUBLIC_ORIGIN,
                "X-VideoScope-Session": token,
            },
        )
        assert allowed.status_code == 200
        assert allowed.json() == []


def test_preflight_is_bounded_and_unknown_origin_is_rejected(tmp_path: Path) -> None:
    with TestClient(
        create_app(_config(tmp_path)), base_url="http://127.0.0.1:8765"
    ) as client:
        preflight = client.options(
            "/api/content/jobs",
            headers={
                "Origin": PUBLIC_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": ("content-type,x-videoscope-session"),
                "Access-Control-Request-Private-Network": "true",
            },
        )
        assert preflight.status_code == 204
        assert preflight.headers["access-control-allow-origin"] == PUBLIC_ORIGIN
        assert preflight.headers["access-control-allow-private-network"] == "true"
        assert "*" not in preflight.headers["access-control-allow-origin"]

        rejected = client.get(
            "/api/connector/status",
            headers={"Origin": "https://malicious.example"},
        )
        assert rejected.status_code == 403


def test_provider_key_can_only_be_set_from_loopback_and_never_returns(
    tmp_path: Path,
) -> None:
    payload = {
        "profile_id": "my-provider",
        "display_name": "My provider",
        "provider_id": "custom-openai",
        "protocol": "openai_compatible",
        "api_base_url": "https://provider.example/v1",
        "model_id": "example-model",
        "api_key": "never-return-this-secret",
        "capabilities": ["structured_text"],
        "request_json_object": True,
    }
    with TestClient(
        create_app(_config(tmp_path)), base_url="http://127.0.0.1:8765"
    ) as client:
        token = _pair(client)
        public_write = client.put(
            "/api/connector/providers/my-provider",
            headers={
                "Origin": PUBLIC_ORIGIN,
                "X-VideoScope-Session": token,
            },
            json=payload,
        )
        assert public_write.status_code == 403

        local_write = client.put(
            "/api/connector/providers/my-provider",
            headers={"Origin": "http://127.0.0.1:8765"},
            json=payload,
        )
        assert local_write.status_code == 200
        assert "api_key" not in local_write.text
        assert "never-return-this-secret" not in local_write.text

        listed = client.get(
            "/api/connector/providers",
            headers={
                "Origin": PUBLIC_ORIGIN,
                "X-VideoScope-Session": token,
            },
        )
        assert listed.status_code == 200
        assert "never-return-this-secret" not in listed.text
        assert listed.json()[0]["credential_state"] == "memory_only"

        openapi = client.get("/openapi.json").text
        assert "api_key" not in openapi
        assert "/api/connector/providers/{profile_id}" not in openapi


def test_pairing_failure_does_not_echo_code(tmp_path: Path) -> None:
    with TestClient(
        create_app(_config(tmp_path)), base_url="http://127.0.0.1:8765"
    ) as client:
        response = client.post(
            "/api/connector/sessions",
            headers={"Origin": PUBLIC_ORIGIN},
            json={"pairing_code": "wrong-code"},
        )
        assert response.status_code == 401
        assert "wrong-code" not in response.text
        assert "pair-123456" not in response.text


def test_pairing_code_is_one_time_and_rate_limited() -> None:
    now = 100.0
    store = ConnectorSessionStore(
        "pair-123456",
        pairing_ttl_seconds=600,
        max_pairing_attempts=3,
        attempt_window_seconds=60,
        clock=lambda: now,
    )

    for _ in range(3):
        with pytest.raises(PermissionError):
            store.pair(SecretStr("wrong-code"))
    with pytest.raises(PermissionError):
        store.pair(SecretStr("pair-123456"))

    fresh = ConnectorSessionStore("pair-123456", clock=lambda: now)
    fresh.pair(SecretStr("pair-123456"))
    with pytest.raises(PermissionError):
        fresh.pair(SecretStr("pair-123456"))


def test_pairing_code_expires_independently_from_browser_session() -> None:
    now = 100.0
    store = ConnectorSessionStore(
        "pair-123456",
        pairing_ttl_seconds=600,
        clock=lambda: now,
    )
    now = 701.0

    with pytest.raises(PermissionError):
        store.pair(SecretStr("pair-123456"))


def test_other_loopback_origin_cannot_use_local_settings(tmp_path: Path) -> None:
    payload = {
        "profile_id": "blocked",
        "display_name": "Blocked",
        "provider_id": "custom-openai",
        "protocol": "openai_compatible",
        "api_base_url": "https://provider.example/v1",
        "model_id": "example-model",
        "api_key": "must-not-be-stored",
        "capabilities": ["structured_text"],
        "request_json_object": False,
    }
    with TestClient(
        create_app(_config(tmp_path)), base_url="http://127.0.0.1:8765"
    ) as client:
        response = client.put(
            "/api/connector/providers/blocked",
            headers={"Origin": "http://127.0.0.1:9999"},
            json=payload,
        )

    assert response.status_code == 403
    assert "must-not-be-stored" not in response.text
