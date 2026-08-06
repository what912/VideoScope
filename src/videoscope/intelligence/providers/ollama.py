"""Loopback-only Ollama provider for grounded structured suggestions."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from videoscope.ai.models import Device, ModelHealth, ModelHealthStatus, Precision
from videoscope.intelligence.models import (
    AISuggestionDraft,
    ContentIntelligenceRequest,
)


class OllamaUnavailableError(RuntimeError):
    """The explicitly selected local Ollama model could not be used."""


class _OllamaSuggestionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestions: tuple[AISuggestionDraft, ...]


class OllamaContentIntelligenceProvider:
    provider_id = "ollama"

    def __init__(
        self,
        *,
        model_id: str,
        endpoint: str = "http://127.0.0.1:11434",
        device: Device = Device.CPU,
        precision: Precision = Precision.FLOAT32,
        timeout_seconds: float = 120.0,
        maximum_response_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self.model_id = model_id.strip()
        if not self.model_id:
            raise ValueError("Ollama model ID must not be blank")
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("Ollama endpoint must use loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "Ollama endpoint must not contain credentials or query data"
            )
        self.endpoint = endpoint.rstrip("/")
        self.device = device
        self.precision = precision
        self.timeout_seconds = timeout_seconds
        self.maximum_response_bytes = maximum_response_bytes
        self._loaded = False

    def health(self) -> ModelHealth:
        return ModelHealth(
            status=(
                ModelHealthStatus.READY if self._loaded else ModelHealthStatus.UNLOADED
            ),
            local_files_available=True,
            message=(
                "The selected loopback Ollama model was verified."
                if self._loaded
                else "Ollama is checked only after explicit AI execution."
            ),
        )

    def load(self) -> None:
        if self._loaded:
            return
        payload = self._request_json("/api/tags", method="GET")
        models = payload.get("models")
        if not isinstance(models, list):
            raise OllamaUnavailableError("Ollama returned an invalid model list")
        names = {
            str(item.get("name"))
            for item in models
            if isinstance(item, dict) and item.get("name")
        }
        if self.model_id not in names:
            raise OllamaUnavailableError(
                "The selected Ollama model is not installed locally. "
                "VideoScope did not pull it automatically."
            )
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def suggest(
        self, request: ContentIntelligenceRequest
    ) -> Sequence[AISuggestionDraft]:
        if not self._loaded:
            raise OllamaUnavailableError("Ollama provider is not loaded")
        prompt = _build_grounded_prompt(request)
        payload = self._request_json(
            "/api/generate",
            body={
                "model": self.model_id,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0,
                    "seed": 0,
                    "num_predict": 4096,
                },
            },
        )
        response = payload.get("response")
        if not isinstance(response, str):
            raise OllamaUnavailableError("Ollama returned no structured response")
        try:
            validated = _OllamaSuggestionResponse.model_validate_json(response)
        except Exception as exc:
            raise OllamaUnavailableError(
                f"Ollama response failed schema validation: {type(exc).__name__}."
            ) from None
        return validated.suggestions

    def _request_json(
        self,
        path: str,
        *,
        method: str = "POST",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.endpoint}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                raw = response.read(self.maximum_response_bytes + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise OllamaUnavailableError(
                f"Loopback Ollama request failed: {type(exc).__name__}."
            ) from None
        if len(raw) > self.maximum_response_bytes:
            raise OllamaUnavailableError(
                "Ollama response exceeded the configured limit"
            )
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            raise OllamaUnavailableError("Ollama returned invalid JSON") from None
        if not isinstance(value, dict):
            raise OllamaUnavailableError("Ollama response must be a JSON object")
        return value


def _build_grounded_prompt(request: ContentIntelligenceRequest) -> str:
    schema = {
        "suggestions": [
            {
                "kind": "chapter|highlight|summary|title",
                "content": "short editable proposal",
                "rationale": "reason limited to supplied evidence",
                "evidence": {
                    "source_ranges": [{"start_seconds": 0.0, "end_seconds": 1.0}],
                    "transcript_cue_ids": ["ai_cue_..."],
                    "frame_timestamps_seconds": [],
                },
                "confidence": None,
                "limitations": ["model limitation"],
            }
        ]
    }
    evidence = {
        "duration_seconds": request.duration_seconds,
        "locale": request.locale,
        "maximum_suggestions": request.maximum_suggestions,
        "requested_kinds": [item.value for item in request.requested_kinds],
        "transcript_segments": [
            item.model_dump(mode="json") for item in request.transcript_segments
        ],
        "structural_ranges": [
            item.model_dump(mode="json") for item in request.structural_ranges
        ],
    }
    return (
        "Return only one JSON object matching the schema. Use only supplied evidence. "
        "Do not infer identities, facts outside the transcript, virality, truth, or an "
        "overall score. Every suggestion needs evidence. Chapter and highlight need "
        "exactly one bounded source range. Keep wording concise and reviewable.\n"
        f"SCHEMA={json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}\n"
        f"EVIDENCE={json.dumps(evidence, ensure_ascii=False, separators=(',', ':'))}"
    )


__all__ = ["OllamaContentIntelligenceProvider", "OllamaUnavailableError"]
