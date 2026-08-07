"""In-memory pairing sessions and BYOK profiles for the loopback connector."""

from __future__ import annotations

import hmac
import re
import threading
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from secrets import token_urlsafe
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ConnectorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderProtocol(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    OLLAMA = "ollama"


class ProviderCapability(StrEnum):
    STRUCTURED_TEXT = "structured_text"
    VISION = "vision"
    AUDIO = "audio"
    TRANSCRIPTION = "transcription"


class ConnectorStatus(ConnectorModel):
    status: str = "ready"
    service: str = "VideoScope Local Connector"
    version: str
    pairing_required: bool = True
    credentials_persisted: bool = False
    modes: tuple[str, ...] = (
        "publish_ready",
        "safe_sharing",
        "video_rescue",
        "useful_content",
        "advanced_ai",
    )


class ConnectorPairingRequest(ConnectorModel):
    pairing_code: SecretStr = Field(min_length=6, max_length=200)


class ConnectorSession(ConnectorModel):
    session_token: str = Field(min_length=32, max_length=200)
    expires_at: datetime


class ProviderProfileInput(ConnectorModel):
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=100)
    provider_id: str = Field(min_length=1, max_length=100)
    protocol: ProviderProtocol
    api_base_url: str = Field(min_length=1, max_length=500)
    model_id: str = Field(min_length=1, max_length=300)
    api_key: SecretStr = Field(min_length=1, max_length=1000)
    capabilities: tuple[ProviderCapability, ...] = (ProviderCapability.STRUCTURED_TEXT,)
    request_json_object: bool = False

    @model_validator(mode="after")
    def validate_endpoint_and_capabilities(self) -> ProviderProfileInput:
        parsed = urlparse(self.api_base_url)
        is_loopback = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if parsed.scheme != "https" and not is_loopback:
            raise ValueError("provider endpoint must use HTTPS or loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("provider endpoint must not contain credentials")
        if not parsed.hostname:
            raise ValueError("provider endpoint requires a host")
        if not self.capabilities:
            raise ValueError("provider profile requires at least one capability")
        if len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("provider capabilities must be unique")
        return self


class ProviderProfileSummary(ConnectorModel):
    profile_id: str
    display_name: str
    provider_id: str
    protocol: ProviderProtocol
    api_base_url: str
    model_id: str
    capabilities: tuple[ProviderCapability, ...]
    request_json_object: bool
    credential_state: str = "memory_only"


class ProviderProfileSecret:
    __slots__ = ("summary", "api_key")

    def __init__(self, summary: ProviderProfileSummary, api_key: SecretStr) -> None:
        self.summary = summary
        self.api_key = api_key


class ConnectorSessionStore:
    def __init__(self, pairing_code: str, *, ttl_seconds: float = 12 * 3600) -> None:
        if len(pairing_code.strip()) < 6:
            raise ValueError("pairing code must contain at least six characters")
        self._pairing_code = pairing_code
        self._ttl = timedelta(seconds=ttl_seconds)
        self._sessions: dict[str, datetime] = {}
        self._lock = threading.RLock()

    def pair(self, pairing_code: SecretStr) -> ConnectorSession:
        if not hmac.compare_digest(pairing_code.get_secret_value(), self._pairing_code):
            raise PermissionError("pairing code is invalid")
        token = token_urlsafe(32)
        expires_at = datetime.now(UTC) + self._ttl
        with self._lock:
            self._sessions[token] = expires_at
            self._prune_locked()
        return ConnectorSession(session_token=token, expires_at=expires_at)

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            self._prune_locked()
            expires_at = self._sessions.get(token)
            return expires_at is not None and expires_at > datetime.now(UTC)

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(token, None)

    def _prune_locked(self) -> None:
        now = datetime.now(UTC)
        expired = [token for token, expiry in self._sessions.items() if expiry <= now]
        for token in expired:
            self._sessions.pop(token, None)


class ProviderCredentialVault:
    """Hold explicit provider keys in process memory only."""

    def __init__(self) -> None:
        self._profiles: dict[str, ProviderProfileSecret] = {}
        self._lock = threading.RLock()

    def put(self, value: ProviderProfileInput) -> ProviderProfileSummary:
        if not _PROFILE_ID.fullmatch(value.profile_id):
            raise ValueError("invalid provider profile ID")
        summary = ProviderProfileSummary(**value.model_dump(exclude={"api_key"}))
        with self._lock:
            self._profiles[value.profile_id] = ProviderProfileSecret(
                summary, value.api_key
            )
        return summary

    def list(self) -> tuple[ProviderProfileSummary, ...]:
        with self._lock:
            return tuple(self._profiles[key].summary for key in sorted(self._profiles))

    def get(self, profile_id: str) -> ProviderProfileSecret:
        with self._lock:
            try:
                return self._profiles[profile_id]
            except KeyError:
                raise KeyError("provider profile not found") from None

    def delete(self, profile_id: str) -> None:
        with self._lock:
            self._profiles.pop(profile_id, None)

    def clear(self) -> None:
        with self._lock:
            self._profiles.clear()


__all__ = [
    "ConnectorPairingRequest",
    "ConnectorSession",
    "ConnectorSessionStore",
    "ConnectorStatus",
    "ProviderCapability",
    "ProviderCredentialVault",
    "ProviderProfileInput",
    "ProviderProfileSecret",
    "ProviderProfileSummary",
    "ProviderProtocol",
]
