"""Lazy local DINOv2 image embedding provider."""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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

DINOV2_PROVIDER_ID = "dinov2"
DINOV2_REPOSITORY = "facebookresearch/dinov2"
DEFAULT_DINOV2_MODEL_NAME = "dinov2_vits14"
DEFAULT_DINOV2_MODEL_ID = f"{DINOV2_REPOSITORY}:{DEFAULT_DINOV2_MODEL_NAME}"
DINOV2_PREPROCESSING_VERSION = "dinov2-imagenet-224-v1"
_DEFAULT_CHECKPOINT_FILENAME = "dinov2_vits14_pretrain.pth"
_REPOSITORY_CACHE_PREFIX = "facebookresearch_dinov2_"


class DINOv2UnavailableError(RuntimeError):
    """The optional DINOv2 installation or local model cache is unusable."""


def _optional_install_message() -> str:
    return (
        "DINOv2 is unavailable. Install the optional runtime with "
        '`python -m pip install "genvideoscope[ai]"`, then retry with '
        "--enable-ai."
    )


class DINOv2EmbeddingProvider:
    """Encode local images with one lazily loaded DINOv2 visual backbone."""

    provider_id = DINOV2_PROVIDER_ID

    def __init__(
        self,
        device: Device,
        precision: Precision,
        *,
        model_name: str = DEFAULT_DINOV2_MODEL_NAME,
        model_id: str = DEFAULT_DINOV2_MODEL_ID,
        hub_directory: Path | None = None,
    ) -> None:
        self.device = device
        self.precision = precision
        self.model_name = model_name
        self.model_id = model_id
        self.hub_directory = hub_directory
        self._model: Any | None = None
        self._preprocess: Any | None = None
        self._torch: Any | None = None

    @property
    def loaded(self) -> bool:
        """Whether model and preprocessing objects are ready."""
        return self._model is not None and self._preprocess is not None

    @staticmethod
    def _packages_available() -> bool:
        return all(
            importlib.util.find_spec(package) is not None
            for package in ("torch", "torchvision")
        )

    def _import_runtime(self) -> tuple[Any, Any]:
        if not self._packages_available():
            raise DINOv2UnavailableError(_optional_install_message())
        try:
            torch = importlib.import_module("torch")
            transforms = importlib.import_module("torchvision.transforms")
        except (ImportError, OSError) as exc:
            raise DINOv2UnavailableError(_optional_install_message()) from exc
        return torch, transforms

    def _resolve_hub_directory(self, torch: Any) -> Path:
        if self.hub_directory is not None:
            return self.hub_directory
        return Path(str(torch.hub.get_dir()))

    @staticmethod
    def _cached_repository(hub_directory: Path) -> Path | None:
        candidates = sorted(
            (
                path
                for path in hub_directory.glob(f"{_REPOSITORY_CACHE_PREFIX}*")
                if path.is_dir() and (path / "hubconf.py").is_file()
            ),
            key=lambda path: (
                0 if path.name.endswith("_main") else 1,
                path.name,
            ),
        )
        return candidates[0] if candidates else None

    def _checkpoint_path(self, hub_directory: Path) -> Path:
        filename = (
            _DEFAULT_CHECKPOINT_FILENAME
            if self.model_name == DEFAULT_DINOV2_MODEL_NAME
            else f"{self.model_name}_pretrain.pth"
        )
        return hub_directory / "checkpoints" / filename

    def _local_files_available(self, torch: Any) -> bool:
        hub_directory = self._resolve_hub_directory(torch)
        return (
            self._cached_repository(hub_directory) is not None
            and self._checkpoint_path(hub_directory).is_file()
        )

    def health(self) -> ModelHealth:
        """Report package and local-cache state without loading the model."""
        if self.loaded:
            return ModelHealth(
                status=ModelHealthStatus.READY,
                local_files_available=True,
                message=f"DINOv2 {self.model_name} is ready.",
            )
        if not self._packages_available():
            return ModelHealth(
                status=ModelHealthStatus.ERROR,
                local_files_available=False,
                message=_optional_install_message(),
            )
        try:
            torch, _ = self._import_runtime()
            local_files_available = self._local_files_available(torch)
        except DINOv2UnavailableError as exc:
            return ModelHealth(
                status=ModelHealthStatus.ERROR,
                local_files_available=False,
                message=str(exc),
            )
        return ModelHealth(
            status=ModelHealthStatus.UNLOADED,
            local_files_available=local_files_available,
            message=(
                "DINOv2 repository and model weights are available in the "
                "local torch hub cache."
                if local_files_available
                else "DINOv2 repository or model weights are not present in "
                "the local torch hub cache."
            ),
        )

    @staticmethod
    def _precision_dtype(torch: Any, precision: Precision) -> Any:
        return {
            Precision.FLOAT32: torch.float32,
            Precision.FLOAT16: torch.float16,
            Precision.BFLOAT16: torch.bfloat16,
        }[precision]

    @staticmethod
    def _build_preprocess(transforms: Any) -> Any:
        return transforms.Compose(
            (
                transforms.Resize(256),
                transforms.CenterCrop(224),
                transforms.ToTensor(),
                transforms.Normalize(
                    mean=(0.485, 0.456, 0.406),
                    std=(0.229, 0.224, 0.225),
                ),
            )
        )

    def load(self) -> None:
        """Load cached resources or use torch hub after runtime approval."""
        if self.loaded:
            return
        torch, transforms = self._import_runtime()
        hub_directory = self._resolve_hub_directory(torch)
        repository = self._cached_repository(hub_directory)
        try:
            if repository is not None:
                model = torch.hub.load(
                    str(repository),
                    self.model_name,
                    source="local",
                    pretrained=True,
                )
            else:
                model = torch.hub.load(
                    DINOV2_REPOSITORY,
                    self.model_name,
                    pretrained=True,
                    trust_repo=True,
                )
            model = model.to(
                device=self.device.value,
                dtype=self._precision_dtype(torch, self.precision),
            )
            model.eval()
            preprocess = self._build_preprocess(transforms)
        except Exception as exc:
            raise DINOv2UnavailableError(
                f"DINOv2 could not load {self.model_name}: {type(exc).__name__}."
            ) from exc
        self._torch = torch
        self._model = model
        self._preprocess = preprocess

    def unload(self) -> None:
        """Release provider-owned model objects without deleting caches."""
        self._model = None
        self._preprocess = None
        torch = self._torch
        self._torch = None
        if torch is not None and self.device is Device.CUDA:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

    def _require_loaded(self) -> tuple[Any, Any, Any]:
        if not self.loaded or self._torch is None:
            raise RuntimeError("DINOv2 provider is not loaded")
        return self._model, self._preprocess, self._torch

    def _metadata(self, *, count: int) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_name": self.model_name,
            "repository": DINOV2_REPOSITORY,
            "device": self.device.value,
            "precision": self.precision.value,
            "operation": "encode_images",
            "provider_batch_size": count,
            "preprocessing_version": DINOV2_PREPROCESSING_VERSION,
        }

    def encode_images(self, image_paths: Sequence[str]) -> EmbeddingBatch:
        """Batch local RGB images and return normalized NumPy embeddings."""
        if not image_paths:
            raise ValueError("at least one image path is required")
        model, preprocess, torch = self._require_loaded()
        tensors: list[Any] = []
        for path in image_paths:
            with Image.open(path) as image:
                tensors.append(preprocess(image.convert("RGB")))
        batch = torch.stack(tensors).to(
            device=self.device.value,
            dtype=self._precision_dtype(torch, self.precision),
        )
        with torch.inference_mode():
            features = model(batch)
        if not hasattr(features, "ndim") or features.ndim != 2:
            raise DINOv2UnavailableError(
                "DINOv2 returned an unexpected visual embedding shape."
            )
        norms = features.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        embeddings = (
            (features / norms)
            .detach()
            .to(dtype=torch.float32)
            .cpu()
            .numpy()
            .astype("float32", copy=False)
        )
        return EmbeddingBatch(
            embeddings,
            self._metadata(count=len(image_paths)),
        )

    def encode_text(self, texts: Sequence[str]) -> EmbeddingBatch:
        """Reject text encoding because DINOv2 is an image-only backbone."""
        del texts
        raise NotImplementedError("DINOv2 does not provide text embeddings")


def register_dinov2_provider(runtime: ModelRuntimeManager) -> ModelSpec:
    """Register the default DINOv2 backbone without importing torch."""
    specification = ModelSpec(
        provider_id=DINOV2_PROVIDER_ID,
        model_id=DEFAULT_DINOV2_MODEL_ID,
        capabilities=("image_embedding",),
        required_extra="ai",
        preprocessing_version=DINOV2_PREPROCESSING_VERSION,
    )
    runtime.register(
        specification,
        lambda device, precision: DINOv2EmbeddingProvider(device, precision),
    )
    return specification
