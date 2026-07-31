"""Validated configuration and JSON loading for local analysis."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

from videoscope.analysis.errors import AnalysisConfigError

DEFAULT_OUTPUT_DIRECTORY = Path("videoscope-output")


class AnalysisConfig(BaseModel):
    """Complete configuration for one CPU analysis run."""

    model_config = ConfigDict(extra="forbid")

    sample_fps: float = Field(default=2.0, gt=0, allow_inf_nan=False)
    thumbnail_max_size: int = Field(default=640, gt=0)
    enabled_detectors: tuple[str, ...] | None = None
    detector_configurations: dict[str, dict[str, JsonValue]] = Field(
        default_factory=dict
    )
    keep_workspace: bool = False
    output_directory: Path = DEFAULT_OUTPUT_DIRECTORY
    locale: str = Field(default="en", min_length=1)
    json_only: bool = False
    bundle_video: bool = False

    @model_validator(mode="after")
    def validate_artifact_options(self) -> Self:
        if self.json_only and self.bundle_video:
            raise ValueError("bundle_video cannot be enabled when json_only is true")
        return self

    @field_validator("enabled_detectors")
    @classmethod
    def normalize_detector_ids(
        cls,
        detector_ids: tuple[str, ...] | None,
    ) -> tuple[str, ...] | None:
        """Store explicitly selected detectors in stable unique order."""
        if detector_ids is None:
            return None
        normalized = tuple(sorted(set(detector_ids)))
        if any(
            not detector_id.strip() or detector_id != detector_id.strip()
            for detector_id in normalized
        ):
            raise ValueError(
                "enabled detector IDs must be non-empty without surrounding whitespace"
            )
        return normalized

    @field_validator("detector_configurations")
    @classmethod
    def validate_detector_configuration_ids(
        cls,
        configurations: dict[str, dict[str, JsonValue]],
    ) -> dict[str, dict[str, JsonValue]]:
        """Reject ambiguous blank detector configuration keys."""
        if any(not detector_id.strip() for detector_id in configurations):
            raise ValueError("detector configuration IDs must not be blank")
        return dict(sorted(configurations.items()))

    @field_validator("locale")
    @classmethod
    def normalize_locale(cls, locale: str) -> str:
        normalized = locale.strip()
        if not normalized:
            raise ValueError("locale must not be blank")
        return normalized

    def with_cli_overrides(
        self,
        *,
        output_directory: Path | None = None,
        sample_fps: float | None = None,
        enabled_detectors: tuple[str, ...] | None = None,
        disabled_detectors: tuple[str, ...] = (),
        keep_workspace: bool = False,
        json_only: bool = False,
        bundle_video: bool = False,
    ) -> Self:
        """Return a revalidated config after explicit CLI overrides."""
        data = self.model_dump(mode="python")
        if output_directory is not None:
            data["output_directory"] = output_directory
        if sample_fps is not None:
            data["sample_fps"] = sample_fps
        if enabled_detectors is not None:
            data["enabled_detectors"] = enabled_detectors
        current = data["enabled_detectors"]
        if disabled_detectors:
            if current is None:
                data["enabled_detectors"] = None
            else:
                disabled = set(disabled_detectors)
                data["enabled_detectors"] = tuple(
                    detector_id
                    for detector_id in current
                    if detector_id not in disabled
                )
        if keep_workspace:
            data["keep_workspace"] = True
        if json_only:
            data["json_only"] = True
        if bundle_video:
            data["bundle_video"] = True
        return type(self).model_validate(data)


def load_analysis_config(path: Path) -> AnalysisConfig:
    """Load a strict UTF-8 JSON analysis configuration."""
    config_path = Path(path)
    if not config_path.is_file():
        raise AnalysisConfigError(f"Configuration file not found: {config_path.name}")
    try:
        raw: object = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AnalysisConfigError(
            f"Could not read configuration file: {config_path.name}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AnalysisConfigError(
            f"Configuration file is not valid JSON: {config_path.name}"
        ) from exc
    if not isinstance(raw, dict):
        raise AnalysisConfigError("Configuration JSON root must be an object")
    try:
        return AnalysisConfig.model_validate(cast(dict[str, Any], raw))
    except ValidationError as exc:
        raise AnalysisConfigError(f"Invalid analysis configuration: {exc}") from exc
