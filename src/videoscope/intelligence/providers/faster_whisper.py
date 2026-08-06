"""Lazy local Faster Whisper ASR provider."""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from videoscope.ai.models import (
    Device,
    ModelHealth,
    ModelHealthStatus,
    Precision,
)
from videoscope.intelligence.models import AITranscriptSegmentDraft


class FasterWhisperUnavailableError(RuntimeError):
    """The optional ASR package, weights, or inference path is unavailable."""


class FasterWhisperASRProvider:
    provider_id = "faster_whisper"

    def __init__(
        self,
        *,
        model_id: str = "small",
        device: Device = Device.CPU,
        precision: Precision = Precision.FLOAT32,
        download_root: Path | None = None,
        language: str | None = None,
    ) -> None:
        self.model_id = model_id.strip()
        if not self.model_id:
            raise ValueError("Faster Whisper model ID must not be blank")
        self.device = device
        self.precision = precision
        self.download_root = Path(download_root) if download_root is not None else None
        self.language = language
        self._model: Any | None = None

    def health(self) -> ModelHealth:
        if importlib.util.find_spec("faster_whisper") is None:
            return ModelHealth(
                status=ModelHealthStatus.ERROR,
                local_files_available=False,
                message=(
                    "Install the optional local ASR runtime with genvideoscope[asr]."
                ),
            )
        local = self._local_model_available()
        return ModelHealth(
            status=(
                ModelHealthStatus.READY
                if self._model is not None
                else ModelHealthStatus.UNLOADED
            ),
            local_files_available=local,
            message=(
                "Faster Whisper is ready."
                if self._model is not None
                else (
                    "Faster Whisper model files appear in the configured local cache."
                    if local
                    else (
                        "Faster Whisper model files are not in the configured "
                        "local cache."
                    )
                )
            ),
        )

    def load(self) -> None:
        if self._model is not None:
            return
        if importlib.util.find_spec("faster_whisper") is None:
            raise FasterWhisperUnavailableError(
                "Install the optional local ASR runtime with genvideoscope[asr]."
            )
        try:
            module = importlib.import_module("faster_whisper")
            whisper_model = getattr(module, "WhisperModel")
            self._model = whisper_model(
                self.model_id,
                device=self.device.value,
                compute_type=self._compute_type(),
                download_root=(
                    str(self.download_root) if self.download_root is not None else None
                ),
            )
        except Exception as exc:
            raise FasterWhisperUnavailableError(
                f"Faster Whisper model could not load: {type(exc).__name__}."
            ) from None

    def unload(self) -> None:
        self._model = None

    def transcribe(self, media_path: Path) -> Sequence[AITranscriptSegmentDraft]:
        if self._model is None:
            raise FasterWhisperUnavailableError("Faster Whisper provider is not loaded")
        try:
            segments, _info = self._model.transcribe(
                str(media_path),
                language=self.language,
                beam_size=1,
                best_of=1,
                temperature=0.0,
                vad_filter=True,
                word_timestamps=False,
            )
            result = []
            for segment in segments:
                text = str(getattr(segment, "text", "")).strip()
                if not text:
                    continue
                avg_logprob = float(getattr(segment, "avg_logprob", -1.0))
                confidence = max(0.0, min(1.0, 1.0 + avg_logprob))
                result.append(
                    AITranscriptSegmentDraft(
                        start_seconds=float(getattr(segment, "start")),
                        end_seconds=float(getattr(segment, "end")),
                        text=text,
                        language=self.language,
                        confidence=confidence,
                    )
                )
            return tuple(result)
        except FasterWhisperUnavailableError:
            raise
        except Exception as exc:
            raise FasterWhisperUnavailableError(
                f"Faster Whisper inference failed: {type(exc).__name__}."
            ) from None

    def _local_model_available(self) -> bool:
        direct = Path(self.model_id)
        if direct.is_dir():
            return True
        if self.download_root is None or not self.download_root.is_dir():
            return False
        normalized = self.model_id.replace("/", "--")
        return any(
            candidate.is_dir()
            for candidate in (
                self.download_root / self.model_id,
                self.download_root / f"models--Systran--faster-whisper-{normalized}",
            )
        )

    def _compute_type(self) -> str:
        if self.precision is Precision.FLOAT16:
            return "float16"
        if self.precision is Precision.BFLOAT16:
            return "bfloat16"
        return "float32" if self.device is Device.CUDA else "int8"


__all__ = ["FasterWhisperASRProvider", "FasterWhisperUnavailableError"]
