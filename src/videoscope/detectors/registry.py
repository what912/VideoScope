"""Deterministic registry for built-in detector plugins."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel

from videoscope.detectors.interface import Detector
from videoscope.detectors.models import DetectorRequirements


class DetectorRegistrationError(ValueError):
    """A detector does not satisfy registry metadata requirements."""


class DuplicateDetectorError(DetectorRegistrationError):
    """A detector ID is already registered."""


class UnknownDetectorError(KeyError):
    """No detector exists for the requested ID."""


class DetectorRegistry:
    """Register and query built-in detectors by stable ID."""

    def __init__(self, builtins: Iterable[Detector] = ()) -> None:
        self._detectors: dict[str, Detector] = {}
        for detector in builtins:
            self.register(detector)

    def register(self, detector: Detector) -> None:
        """Register one built-in detector after validating its manifest."""
        detector_id = detector.id.strip()
        if not detector_id or detector_id != detector.id:
            raise DetectorRegistrationError(
                "detector id must be non-empty and have no surrounding whitespace"
            )
        if detector_id in self._detectors:
            raise DuplicateDetectorError(
                f"detector id is already registered: {detector_id}"
            )
        for attribute in ("display_name", "version", "description"):
            value = getattr(detector, attribute, None)
            if not isinstance(value, str) or not value.strip():
                raise DetectorRegistrationError(
                    f"detector {detector_id!r} has invalid {attribute}"
                )
        if not isinstance(detector.requirements, DetectorRequirements):
            raise DetectorRegistrationError(
                f"detector {detector_id!r} has invalid requirements"
            )
        if not isinstance(detector.default_enabled, bool):
            raise DetectorRegistrationError(
                f"detector {detector_id!r} has invalid default_enabled"
            )
        if not isinstance(detector.config_model, type) or not issubclass(
            detector.config_model,
            BaseModel,
        ):
            raise DetectorRegistrationError(
                f"detector {detector_id!r} config_model must be a Pydantic model"
            )
        self._detectors[detector_id] = detector

    def get(self, detector_id: str) -> Detector:
        """Return one detector or raise a structured lookup error."""
        try:
            return self._detectors[detector_id]
        except KeyError as exc:
            raise UnknownDetectorError(detector_id) from exc

    def list_available(self) -> tuple[Detector, ...]:
        """List detectors in stable ID order."""
        return tuple(
            self._detectors[detector_id] for detector_id in sorted(self._detectors)
        )

    def list_default_enabled(self) -> tuple[Detector, ...]:
        """List default-enabled detectors in stable ID order."""
        return tuple(
            detector for detector in self.list_available() if detector.default_enabled
        )
