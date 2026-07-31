"""Lazy local PaddleOCR provider with normalized timestamped results."""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image

from videoscope.ai.models import (
    Device,
    ModelHealth,
    ModelHealthStatus,
    ModelSpec,
    NormalizedBoundingBox,
    OCRBatch,
    OCRImageInput,
    OCRObservation,
    Precision,
)
from videoscope.ai.runtime import ModelRuntimeManager

PADDLEOCR_PROVIDER_ID = "paddleocr"
PADDLEOCR_VERSION = "PP-OCRv5"
PADDLEOCR_DETECTION_MODEL = "PP-OCRv5_mobile_det"
PADDLEOCR_CHINESE_RECOGNITION_MODEL = "PP-OCRv5_mobile_rec"
PADDLEOCR_ENGLISH_RECOGNITION_MODEL = "en_PP-OCRv5_mobile_rec"
PADDLEOCR_CHINESE_MODEL_ID = "PP-OCRv5-mobile/ch"
PADDLEOCR_ENGLISH_MODEL_ID = "PP-OCRv5-mobile/en"
PADDLEOCR_PREPROCESSING_VERSION = "paddleocr-v5-local-frame-v1"


class PaddleOCRUnavailableError(RuntimeError):
    """The optional PaddleOCR installation or local models are unusable."""


def _optional_install_message() -> str:
    return (
        "PaddleOCR is unavailable. Install the optional local OCR runtime with "
        '`python -m pip install "genvideoscope[ocr]"`, then retry with '
        "--enable-ocr."
    )


class PaddleOCRProvider:
    """Detect and recognize Chinese or English text in local sampled frames."""

    provider_id = PADDLEOCR_PROVIDER_ID

    def __init__(
        self,
        device: Device,
        precision: Precision,
        *,
        language: str,
        model_id: str,
        recognition_model_name: str,
        cache_home: Path | None = None,
    ) -> None:
        if language not in {"ch", "en"}:
            raise ValueError("PaddleOCR language must be 'ch' or 'en'")
        self.device = device
        self.precision = precision
        self.language = language
        self.model_id = model_id
        self.recognition_model_name = recognition_model_name
        self.cache_home = cache_home
        self._pipeline: Any | None = None

    @property
    def loaded(self) -> bool:
        """Whether the PaddleOCR pipeline is ready."""
        return self._pipeline is not None

    @staticmethod
    def _packages_available() -> bool:
        return all(
            importlib.util.find_spec(package) is not None
            for package in ("paddle", "paddleocr")
        )

    def _cache_root(self) -> Path:
        if self.cache_home is not None:
            return self.cache_home
        configured = os.environ.get("PADDLE_PDX_CACHE_HOME")
        return Path(configured) if configured else Path.home() / ".paddlex"

    @staticmethod
    def _directory_has_files(path: Path) -> bool:
        try:
            return path.is_dir() and any(item.is_file() for item in path.rglob("*"))
        except OSError:
            return False

    def _local_models_available(self) -> bool:
        model_root = self._cache_root() / "official_models"
        return all(
            self._directory_has_files(model_root / model_name)
            for model_name in (
                PADDLEOCR_DETECTION_MODEL,
                self.recognition_model_name,
            )
        )

    def health(self) -> ModelHealth:
        """Report package and cache state without importing PaddleOCR."""
        if self.loaded:
            return ModelHealth(
                status=ModelHealthStatus.READY,
                local_files_available=True,
                message=(
                    f"PaddleOCR {PADDLEOCR_VERSION} language={self.language} is ready."
                ),
            )
        if not self._packages_available():
            return ModelHealth(
                status=ModelHealthStatus.ERROR,
                local_files_available=False,
                message=_optional_install_message(),
            )
        local_files_available = self._local_models_available()
        return ModelHealth(
            status=ModelHealthStatus.UNLOADED,
            local_files_available=local_files_available,
            message=(
                "PaddleOCR detection and recognition models are available in "
                "the local PaddleX cache."
                if local_files_available
                else "PaddleOCR detection or recognition models are not "
                "present in the local PaddleX cache."
            ),
        )

    def load(self) -> None:
        """Construct PaddleOCR only after shared runtime policy approval."""
        if self.loaded:
            return
        if not self._packages_available():
            raise PaddleOCRUnavailableError(_optional_install_message())
        if self.precision is Precision.BFLOAT16:
            raise PaddleOCRUnavailableError(
                "PaddleOCR does not support the requested bfloat16 precision; "
                "select float32 or float16."
            )
        try:
            module = importlib.import_module("paddleocr")
            paddle_ocr = getattr(module, "PaddleOCR")
            pipeline = paddle_ocr(
                lang=self.language,
                ocr_version=PADDLEOCR_VERSION,
                text_detection_model_name=PADDLEOCR_DETECTION_MODEL,
                text_recognition_model_name=self.recognition_model_name,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="gpu:0" if self.device is Device.CUDA else "cpu",
                precision=("fp16" if self.precision is Precision.FLOAT16 else "fp32"),
            )
        except Exception as exc:
            raise PaddleOCRUnavailableError(
                f"PaddleOCR could not load {self.model_id}: {type(exc).__name__}."
            ) from exc
        self._pipeline = pipeline

    def unload(self) -> None:
        """Release the pipeline without deleting user model caches."""
        self._pipeline = None

    @staticmethod
    def _result_payload(result: Any) -> Mapping[str, Any]:
        payload = getattr(result, "json", None)
        if callable(payload):
            payload = payload()
        if not isinstance(payload, Mapping):
            raise PaddleOCRUnavailableError(
                "PaddleOCR returned an unsupported result object."
            )
        nested = payload.get("res")
        return nested if isinstance(nested, Mapping) else payload

    @staticmethod
    def _normalized_box(
        raw_box: Any,
        *,
        width: int,
        height: int,
    ) -> NormalizedBoundingBox:
        try:
            values = list(raw_box)
        except TypeError as exc:
            raise PaddleOCRUnavailableError(
                "PaddleOCR returned an invalid rectangular text box."
            ) from exc
        if len(values) != 4:
            raise PaddleOCRUnavailableError(
                "PaddleOCR returned an invalid rectangular text box."
            )
        x_min, y_min, x_max, y_max = (float(value) for value in values)
        return NormalizedBoundingBox(
            x_min=max(0.0, min(1.0, x_min / width)),
            y_min=max(0.0, min(1.0, y_min / height)),
            x_max=max(0.0, min(1.0, x_max / width)),
            y_max=max(0.0, min(1.0, y_max / height)),
        )

    def detect_and_recognize(
        self,
        images: Sequence[OCRImageInput],
    ) -> OCRBatch:
        """Run batched local OCR and normalize boxes to image coordinates."""
        if not images:
            raise ValueError("at least one OCR image input is required")
        if self._pipeline is None:
            raise RuntimeError("PaddleOCR provider is not loaded")
        dimensions: list[tuple[int, int]] = []
        for image_input in images:
            with Image.open(image_input.path) as image:
                dimensions.append(image.size)
        try:
            raw_results = list(
                self._pipeline.predict([str(image.path) for image in images])
            )
        except Exception as exc:
            raise PaddleOCRUnavailableError(
                f"PaddleOCR inference failed: {type(exc).__name__}."
            ) from exc
        if len(raw_results) != len(images):
            raise PaddleOCRUnavailableError(
                "PaddleOCR result count did not match the image batch."
            )

        observations: list[OCRObservation] = []
        for image_input, dimensions_for_image, raw_result in zip(
            images,
            dimensions,
            raw_results,
            strict=True,
        ):
            payload = self._result_payload(raw_result)
            texts = list(payload.get("rec_texts", ()))
            scores = list(payload.get("rec_scores", ()))
            boxes = list(payload.get("rec_boxes", ()))
            if not (len(texts) == len(scores) == len(boxes)):
                raise PaddleOCRUnavailableError(
                    "PaddleOCR text, score, and box counts are inconsistent."
                )
            width, height = dimensions_for_image
            for text, score, raw_box in zip(texts, scores, boxes, strict=True):
                normalized_text = str(text).strip()
                if not normalized_text:
                    continue
                observations.append(
                    OCRObservation(
                        text=normalized_text,
                        confidence=float(score),
                        bounding_box=self._normalized_box(
                            raw_box,
                            width=width,
                            height=height,
                        ),
                        timestamp_seconds=image_input.timestamp_seconds,
                    )
                )
        return OCRBatch(
            observations=tuple(observations),
            metadata={
                "provider_id": self.provider_id,
                "model_id": self.model_id,
                "ocr_version": PADDLEOCR_VERSION,
                "language": self.language,
                "detection_model": PADDLEOCR_DETECTION_MODEL,
                "recognition_model": self.recognition_model_name,
                "device": self.device.value,
                "precision": self.precision.value,
                "provider_batch_size": len(images),
            },
        )


def register_paddleocr_providers(
    runtime: ModelRuntimeManager,
) -> tuple[ModelSpec, ModelSpec]:
    """Register Chinese and English OCR variants without importing Paddle."""
    specifications = (
        ModelSpec(
            provider_id=PADDLEOCR_PROVIDER_ID,
            model_id=PADDLEOCR_CHINESE_MODEL_ID,
            capabilities=("ocr",),
            required_extra="ocr",
            preprocessing_version=PADDLEOCR_PREPROCESSING_VERSION,
        ),
        ModelSpec(
            provider_id=PADDLEOCR_PROVIDER_ID,
            model_id=PADDLEOCR_ENGLISH_MODEL_ID,
            capabilities=("ocr",),
            required_extra="ocr",
            preprocessing_version=PADDLEOCR_PREPROCESSING_VERSION,
        ),
    )
    runtime.register(
        specifications[0],
        lambda device, precision: PaddleOCRProvider(
            device,
            precision,
            language="ch",
            model_id=PADDLEOCR_CHINESE_MODEL_ID,
            recognition_model_name=PADDLEOCR_CHINESE_RECOGNITION_MODEL,
        ),
    )
    runtime.register(
        specifications[1],
        lambda device, precision: PaddleOCRProvider(
            device,
            precision,
            language="en",
            model_id=PADDLEOCR_ENGLISH_MODEL_ID,
            recognition_model_name=PADDLEOCR_ENGLISH_RECOGNITION_MODEL,
        ),
    )
    return specifications
