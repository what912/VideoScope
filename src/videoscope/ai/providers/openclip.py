"""Lazy local OpenCLIP image/text embedding provider."""

from __future__ import annotations

import importlib
import importlib.util
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PIL import Image

from videoscope.ai.models import (
    Device,
    EmbeddingBatch,
    ModelHealth,
    ModelHealthStatus,
    ModelSpec,
    Precision,
)
from videoscope.ai.runtime import ModelRuntimeManager

OPENCLIP_PROVIDER_ID = "openclip"
DEFAULT_OPENCLIP_MODEL_NAME = "ViT-B-32"
DEFAULT_OPENCLIP_PRETRAINED = "laion2b_s34b_b79k"
DEFAULT_OPENCLIP_MODEL_ID = (
    f"{DEFAULT_OPENCLIP_MODEL_NAME}/{DEFAULT_OPENCLIP_PRETRAINED}"
)
OPENCLIP_PREPROCESSING_VERSION = "openclip-val-v1"
_HF_DEFAULT_WEIGHTS = "open_clip_pytorch_model.bin"
_HF_WEIGHT_ALTERNATIVES = (
    "open_clip_model.safetensors",
    "open_clip_pytorch_model.safetensors",
    "open_clip_pytorch_model.bin",
)


class OpenCLIPUnavailableError(RuntimeError):
    """The optional OpenCLIP installation is missing or unusable."""


def _optional_install_message() -> str:
    return (
        "OpenCLIP is unavailable. Install the optional runtime with "
        '`python -m pip install "genvideoscope[ai]"`, then retry with '
        "--enable-ai."
    )


class OpenCLIPEmbeddingProvider:
    """Encode local images and text with one lazily loaded OpenCLIP model."""

    provider_id = OPENCLIP_PROVIDER_ID

    def __init__(
        self,
        device: Device,
        precision: Precision,
        *,
        model_name: str = DEFAULT_OPENCLIP_MODEL_NAME,
        pretrained: str = DEFAULT_OPENCLIP_PRETRAINED,
        model_id: str = DEFAULT_OPENCLIP_MODEL_ID,
        cache_directory: Path | None = None,
    ) -> None:
        self.device = device
        self.precision = precision
        self.model_name = model_name
        self.pretrained = pretrained
        self.model_id = model_id
        self.cache_directory = cache_directory
        self._model: Any | None = None
        self._preprocess: Any | None = None
        self._tokenizer: Any | None = None
        self._torch: Any | None = None
        self._open_clip: Any | None = None

    @property
    def loaded(self) -> bool:
        """Whether model, preprocessing, and tokenizer objects are ready."""
        return (
            self._model is not None
            and self._preprocess is not None
            and self._tokenizer is not None
        )

    @staticmethod
    def _package_available() -> bool:
        return importlib.util.find_spec("open_clip") is not None

    def _import_openclip(self) -> tuple[Any, Any]:
        if not self._package_available():
            raise OpenCLIPUnavailableError(_optional_install_message())
        try:
            open_clip = importlib.import_module("open_clip")
            torch = importlib.import_module("torch")
        except (ImportError, OSError) as exc:
            raise OpenCLIPUnavailableError(_optional_install_message()) from exc
        return open_clip, torch

    def _hf_checkpoint_available(self, reference: str) -> bool:
        if importlib.util.find_spec("huggingface_hub") is None:
            return False
        repository, filename = os.path.split(reference)
        if not repository:
            return False
        try:
            hub = importlib.import_module("huggingface_hub")
            lookup = getattr(hub, "try_to_load_from_cache")
        except (ImportError, AttributeError):
            return False
        candidates = (
            (filename,) if filename else (_HF_DEFAULT_WEIGHTS,)
        ) + _HF_WEIGHT_ALTERNATIVES
        for candidate in dict.fromkeys(candidates):
            try:
                cached = lookup(
                    repository,
                    candidate,
                    cache_dir=(
                        None
                        if self.cache_directory is None
                        else str(self.cache_directory)
                    ),
                )
            except Exception:
                continue
            if isinstance(cached, str) and Path(cached).is_file():
                return True
        return False

    def _url_checkpoint_available(self, url: str) -> bool:
        filename = Path(urlparse(url).path).name
        if not filename:
            return False
        cache_directory = self.cache_directory or (Path.home() / ".cache" / "clip")
        return (cache_directory / filename).is_file()

    def _local_checkpoint_available(self, open_clip: Any) -> bool:
        pretrained_path = Path(self.pretrained)
        if pretrained_path.is_file():
            return True
        try:
            configuration = open_clip.get_pretrained_cfg(
                self.model_name,
                self.pretrained,
            )
        except Exception:
            return False
        if not isinstance(configuration, dict):
            return False
        local_file = configuration.get("file")
        if isinstance(local_file, str) and Path(local_file).is_file():
            return True
        hub_reference = configuration.get("hf_hub")
        if isinstance(hub_reference, str) and hub_reference:
            return self._hf_checkpoint_available(hub_reference)
        url = configuration.get("url")
        return (
            isinstance(url, str) and bool(url) and self._url_checkpoint_available(url)
        )

    def health(self) -> ModelHealth:
        """Report package and local-weight state without loading the model."""
        if self.loaded:
            return ModelHealth(
                status=ModelHealthStatus.READY,
                local_files_available=True,
                message=(f"OpenCLIP {self.model_name} ({self.pretrained}) is ready."),
            )
        if not self._package_available():
            return ModelHealth(
                status=ModelHealthStatus.ERROR,
                local_files_available=False,
                message=_optional_install_message(),
            )
        try:
            open_clip, _ = self._import_openclip()
            local_files_available = self._local_checkpoint_available(open_clip)
        except OpenCLIPUnavailableError as exc:
            return ModelHealth(
                status=ModelHealthStatus.ERROR,
                local_files_available=False,
                message=str(exc),
            )
        return ModelHealth(
            status=ModelHealthStatus.UNLOADED,
            local_files_available=local_files_available,
            message=(
                "OpenCLIP model files are available in the local cache."
                if local_files_available
                else "OpenCLIP model files are not present in the local cache."
            ),
        )

    def load(self) -> None:
        """Load model weights only after runtime download policy approval."""
        if self.loaded:
            return
        open_clip, torch = self._import_openclip()
        precision = {
            Precision.FLOAT32: "fp32",
            Precision.FLOAT16: "fp16",
            Precision.BFLOAT16: "bf16",
        }[self.precision]
        try:
            model, _, preprocess = open_clip.create_model_and_transforms(
                model_name=self.model_name,
                pretrained=self.pretrained,
                precision=precision,
                device=self.device.value,
                cache_dir=(
                    None if self.cache_directory is None else str(self.cache_directory)
                ),
            )
            model.eval()
            tokenizer = open_clip.get_tokenizer(self.model_name)
        except Exception as exc:
            raise OpenCLIPUnavailableError(
                f"OpenCLIP could not load {self.model_name} "
                f"({self.pretrained}): {type(exc).__name__}."
            ) from exc
        self._open_clip = open_clip
        self._torch = torch
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer

    def unload(self) -> None:
        """Release provider-owned model objects without touching disk caches."""
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._open_clip = None
        torch = self._torch
        self._torch = None
        if torch is not None and self.device is Device.CUDA:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

    def _require_loaded(self) -> tuple[Any, Any, Any, Any]:
        if not self.loaded or self._torch is None:
            raise RuntimeError("OpenCLIP provider is not loaded")
        return self._model, self._preprocess, self._tokenizer, self._torch

    @staticmethod
    def _normalized_numpy(features: Any, torch: Any) -> Any:
        norms = features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        normalized = features / norms
        return (
            normalized.detach()
            .to(dtype=torch.float32)
            .cpu()
            .numpy()
            .astype("float32", copy=False)
        )

    def _metadata(self, *, operation: str, count: int) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "pretrained": self.pretrained,
            "device": self.device.value,
            "precision": self.precision.value,
            "operation": operation,
            "provider_batch_size": count,
        }

    def encode_images(self, image_paths: Sequence[str]) -> EmbeddingBatch:
        """Batch local RGB images through OpenCLIP validation preprocessing."""
        if not image_paths:
            raise ValueError("at least one image path is required")
        model, preprocess, _, torch = self._require_loaded()
        tensors: list[Any] = []
        for path in image_paths:
            with Image.open(path) as image:
                tensors.append(preprocess(image.convert("RGB")))
        batch = torch.stack(tensors).to(self.device.value)
        with torch.inference_mode():
            features = model.encode_image(batch)
        return EmbeddingBatch(
            self._normalized_numpy(features, torch),
            self._metadata(operation="encode_images", count=len(image_paths)),
        )

    def encode_text(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Batch UTF-8 prompt text through the model-specific tokenizer."""
        if not texts:
            raise ValueError("at least one text value is required")
        model, _, tokenizer, torch = self._require_loaded()
        tokens = tokenizer(list(texts)).to(self.device.value)
        with torch.inference_mode():
            features = model.encode_text(tokens)
        return EmbeddingBatch(
            self._normalized_numpy(features, torch),
            self._metadata(operation="encode_text", count=len(texts)),
        )


def register_openclip_provider(runtime: ModelRuntimeManager) -> ModelSpec:
    """Register the default OpenCLIP model without constructing its provider."""
    specification = ModelSpec(
        provider_id=OPENCLIP_PROVIDER_ID,
        model_id=DEFAULT_OPENCLIP_MODEL_ID,
        required_extra="ai",
        preprocessing_version=OPENCLIP_PREPROCESSING_VERSION,
    )
    runtime.register(
        specification,
        lambda device, precision: OpenCLIPEmbeddingProvider(
            device,
            precision,
        ),
    )
    return specification
