from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from pydantic import SecretStr

from tests.intelligence.helpers import content_map
from videoscope.intelligence import (
    ContentIntelligenceRequest,
    FakeASRProvider,
    build_intelligence_request,
)
from videoscope.intelligence.providers import (
    OpenAICompatibleContentIntelligenceProvider,
    OpenAICompatibleUnavailableError,
)
from videoscope.intelligence.service import normalize_asr_transcript


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.payload[:size]


def _request() -> ContentIntelligenceRequest:
    transcript, _ = normalize_asr_transcript(
        FakeASRProvider(), Path("unused.mp4"), duration_seconds=10
    )
    return build_intelligence_request(content_map(), transcript)


def test_openai_compatible_is_lazy_and_parses_grounded_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    cue = request.transcript_segments[0]
    calls: list[urllib.request.Request] = []

    def urlopen(req: urllib.request.Request, *, timeout: float) -> _Response:
        del timeout
        calls.append(req)
        result = {
            "suggestions": [
                {
                    "kind": "summary",
                    "content": "A bounded BYOK summary.",
                    "rationale": "It cites the supplied transcript cue.",
                    "evidence": {
                        "source_ranges": [],
                        "transcript_cue_ids": [cue.id],
                        "frame_timestamps_seconds": [],
                    },
                    "confidence": None,
                    "limitations": ["Requires human review."],
                }
            ]
        }
        return _Response({"choices": [{"message": {"content": json.dumps(result)}}]})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    provider = OpenAICompatibleContentIntelligenceProvider(
        provider_id="user-openai",
        model_id="user-model",
        api_base_url="https://provider.example/v1",
        api_key=SecretStr("private-test-key"),
    )
    assert provider.health().status.value == "unloaded"
    assert calls == []
    provider.load()
    suggestions = provider.suggest(request)
    assert suggestions[0].content == "A bounded BYOK summary."
    assert len(calls) == 1
    assert calls[0].full_url == "https://provider.example/v1/chat/completions"
    assert calls[0].headers["Authorization"] == "Bearer private-test-key"
    encoded = calls[0].data
    assert isinstance(encoded, bytes)
    assert b"private-test-key" not in encoded


def test_openai_compatible_rejects_insecure_remote_and_sanitizes_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAICompatibleContentIntelligenceProvider(
            provider_id="remote",
            model_id="model",
            api_base_url="http://provider.example/v1",
            api_key=SecretStr("top-secret"),
        )

    def unavailable(*_args: object, **_kwargs: object) -> _Response:
        raise OSError("top-secret https://provider.example/v1")

    monkeypatch.setattr(urllib.request, "urlopen", unavailable)
    provider = OpenAICompatibleContentIntelligenceProvider(
        provider_id="remote",
        model_id="model",
        api_base_url="https://provider.example/v1",
        api_key=SecretStr("top-secret"),
    )
    provider.load()
    with pytest.raises(OpenAICompatibleUnavailableError) as captured:
        provider.suggest(_request())
    assert "top-secret" not in str(captured.value)
    assert "provider.example" not in str(captured.value)
