from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest

from tests.intelligence.helpers import content_map
from videoscope.ai.models import Device, Precision
from videoscope.intelligence import FakeASRProvider, build_intelligence_request
from videoscope.intelligence.providers import (
    FasterWhisperASRProvider,
    OllamaContentIntelligenceProvider,
    OllamaUnavailableError,
)
from videoscope.intelligence.service import normalize_asr_transcript


class _Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.payload[:size]


def test_ollama_rejects_non_loopback_and_credentials() -> None:
    with pytest.raises(ValueError, match="loopback"):
        OllamaContentIntelligenceProvider(
            model_id="local-model", endpoint="https://example.com"
        )
    with pytest.raises(ValueError, match="credentials"):
        OllamaContentIntelligenceProvider(
            model_id="local-model", endpoint="http://user:pass@127.0.0.1:11434"
        )


def test_ollama_never_pulls_and_validates_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript, _ = normalize_asr_transcript(
        FakeASRProvider(), Path("unused.mp4"), duration_seconds=10
    )
    request = build_intelligence_request(content_map(), transcript)
    calls: list[tuple[str, str]] = []

    def urlopen(req: urllib.request.Request, *, timeout: float) -> _Response:
        del timeout
        calls.append((req.full_url, req.get_method()))
        if req.full_url.endswith("/api/tags"):
            return _Response({"models": [{"name": "qwen-test"}]})
        cue = request.transcript_segments[0]
        response = {
            "suggestions": [
                {
                    "kind": "summary",
                    "content": "A grounded local summary.",
                    "rationale": "It uses the cited cue.",
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
        return _Response({"response": json.dumps(response)})

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    provider = OllamaContentIntelligenceProvider(model_id="qwen-test")
    provider.load()
    suggestions = provider.suggest(request)
    assert suggestions[0].content == "A grounded local summary."
    assert calls == [
        ("http://127.0.0.1:11434/api/tags", "GET"),
        ("http://127.0.0.1:11434/api/generate", "POST"),
    ]
    assert all("pull" not in url for url, _method in calls)


def test_ollama_requires_preinstalled_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response({"models": []}),
    )
    provider = OllamaContentIntelligenceProvider(model_id="missing")
    with pytest.raises(OllamaUnavailableError, match="not installed"):
        provider.load()


def test_faster_whisper_health_is_lazy_and_path_free(tmp_path: Path) -> None:
    provider = FasterWhisperASRProvider(
        model_id="small",
        device=Device.CPU,
        precision=Precision.FLOAT32,
        download_root=tmp_path,
    )
    health = provider.health()
    assert provider._model is None
    assert "small" not in health.message
    assert str(tmp_path) not in health.message
