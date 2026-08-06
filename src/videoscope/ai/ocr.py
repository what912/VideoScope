"""Small optional-OCR boundary over the shared model runtime."""

from __future__ import annotations

from collections.abc import Sequence

from videoscope.ai.models import OCRBatch, OCRImageInput
from videoscope.ai.runtime import ModelRuntimeManager


class OCRRuntimeUnavailableError(RuntimeError):
    """The caller did not configure an optional local OCR runtime."""


def detect_with_optional_ocr(
    runtime: ModelRuntimeManager | None,
    *,
    provider_id: str,
    model_id: str,
    images: Sequence[OCRImageInput],
) -> OCRBatch:
    """Run OCR through the shared runtime or expose a distinct unavailable state."""
    if runtime is None:
        raise OCRRuntimeUnavailableError("optional OCR runtime is not configured")
    matching = tuple(
        spec
        for spec in runtime.list_models()
        if spec.provider_id == provider_id and spec.model_id == model_id
    )
    if not matching or "ocr" not in matching[0].capabilities:
        raise OCRRuntimeUnavailableError(
            "requested optional OCR provider is not registered"
        )
    return runtime.detect_and_recognize(provider_id, model_id, images)


__all__ = ["OCRRuntimeUnavailableError", "detect_with_optional_ocr"]
