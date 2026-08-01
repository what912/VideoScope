"""Detector plugin protocol independent of concrete quality algorithms."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from videoscope.detectors.models import (
    AnalysisContext,
    DetectorRequirements,
)
from videoscope.domain import Finding


class Detector(Protocol):
    """Metadata and execution contract implemented by every detector."""

    @property
    def id(self) -> str:
        """Stable detector ID."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable detector name."""
        ...

    @property
    def version(self) -> str:
        """Detector algorithm version."""
        ...

    @property
    def description(self) -> str:
        """Concise detector capability description."""
        ...

    @property
    def requirements(self) -> DetectorRequirements:
        """Declared runtime capabilities and cost."""
        ...

    @property
    def default_enabled(self) -> bool:
        """Whether built-in profiles enable the detector by default."""
        ...

    @property
    def config_model(self) -> type[BaseModel]:
        """Pydantic model used to validate effective configuration."""
        ...

    def analyze(
        self,
        context: AnalysisContext,
        config: BaseModel,
    ) -> list[Finding]:
        """Return unified Findings without writing reports."""
        ...
