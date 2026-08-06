"""Anonymous, local-only visual privacy proposals for sampled frames."""

from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from videoscope.domain import Severity
from videoscope.privacy.models import (
    NormalizedBox,
    PrivacyRisk,
    PrivacyRiskType,
    RedactionStyle,
    make_privacy_risk_id,
)
from videoscope.privacy.scanners import (
    PrivacyScanContext,
    PrivacyScannerRequirements,
)

_SCANNER_VERSION = "1.0.0"
_MAX_PRIVATE_PAYLOAD_LENGTH = 4096
_VISUAL_RISK_TYPES = frozenset(
    {
        PrivacyRiskType.FACE_REGION,
        PrivacyRiskType.QR_CODE,
        PrivacyRiskType.BARCODE,
    }
)


class _VisualModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VisualDetection(_VisualModel):
    """One adapter-local rectangle without biometric or identity attributes."""

    box: NormalizedBox
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    risk_type: PrivacyRiskType = PrivacyRiskType.FACE_REGION
    decoded_payload: str | None = Field(
        default=None,
        max_length=_MAX_PRIVATE_PAYLOAD_LENGTH,
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_visual_type(self) -> Self:
        if self.risk_type not in _VISUAL_RISK_TYPES:
            raise ValueError("visual detection has an unsupported privacy risk type")
        if (
            self.risk_type is PrivacyRiskType.FACE_REGION
            and self.decoded_payload is not None
        ):
            raise ValueError("face-region detections cannot carry decoded payloads")
        return self


class VisualObservation(_VisualModel):
    """One normalized observation associated with a sampled frame and scene."""

    timestamp_seconds: float = Field(ge=0, allow_inf_nan=False)
    sample_index: int = Field(ge=0)
    scene_index: int = Field(ge=0)
    relative_path: str = Field(min_length=1)
    risk_type: PrivacyRiskType
    box: NormalizedBox
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    decoded_payload: str | None = Field(
        default=None,
        max_length=_MAX_PRIVATE_PAYLOAD_LENGTH,
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_visual_type(self) -> Self:
        if self.risk_type not in _VISUAL_RISK_TYPES:
            raise ValueError("visual observation has an unsupported privacy risk type")
        if (
            self.risk_type is PrivacyRiskType.FACE_REGION
            and self.decoded_payload is not None
        ):
            raise ValueError("face-region observations cannot carry decoded payloads")
        return self


class VisualTrack(_VisualModel):
    """A scene-local anonymous sequence of geometrically related observations."""

    anonymous_id: str = Field(pattern=r"^(face|qr|barcode)_track_[0-9]{2,}$")
    risk_type: PrivacyRiskType
    scene_index: int = Field(ge=0)
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    box: NormalizedBox
    observations: tuple[VisualObservation, ...] = Field(min_length=1)
    has_gap: bool = False

    @model_validator(mode="after")
    def validate_track(self) -> Self:
        ordered = tuple(sorted(self.observations, key=_observation_sort_key))
        if ordered != self.observations:
            raise ValueError(
                "visual track observations must be deterministically sorted"
            )
        if any(item.risk_type is not self.risk_type for item in ordered):
            raise ValueError("visual track observations must share one risk type")
        if any(item.scene_index != self.scene_index for item in ordered):
            raise ValueError("visual track cannot cross a scene boundary")
        if self.start_seconds != ordered[0].timestamp_seconds:
            raise ValueError("visual track start must match its first observation")
        if self.end_seconds != ordered[-1].timestamp_seconds:
            raise ValueError("visual track end must match its final observation")
        return self


class AnonymousFaceConfig(_VisualModel):
    """Conservative local face-region proposal and tracking settings."""

    minimum_size_pixels: int = Field(default=24, ge=1)
    scale_factor: float = Field(default=1.1, gt=1, allow_inf_nan=False)
    neighbor_count: int = Field(default=5, ge=0)
    tracking_iou: float = Field(default=0.25, ge=0, le=1, allow_inf_nan=False)
    maximum_center_distance: float = Field(
        default=0.25,
        ge=0,
        le=math.sqrt(2),
        allow_inf_nan=False,
    )
    maximum_gap_seconds: float = Field(default=0.51, ge=0, allow_inf_nan=False)
    guard_seconds: float = Field(default=0.1, ge=0, allow_inf_nan=False)
    maximum_risks: int = Field(default=100, ge=1, le=10_000)


class QrBarcodeConfig(_VisualModel):
    """Local QR/barcode proposal and tracking settings."""

    minimum_size_pixels: int = Field(default=12, ge=1)
    scale_factor: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    neighbor_count: int = Field(default=0, ge=0)
    tracking_iou: float = Field(default=0.25, ge=0, le=1, allow_inf_nan=False)
    maximum_center_distance: float = Field(
        default=0.25,
        ge=0,
        le=math.sqrt(2),
        allow_inf_nan=False,
    )
    maximum_gap_seconds: float = Field(default=0.51, ge=0, allow_inf_nan=False)
    guard_seconds: float = Field(default=0.1, ge=0, allow_inf_nan=False)
    maximum_risks: int = Field(default=100, ge=1, le=10_000)


_VisualConfigT = TypeVar(
    "_VisualConfigT",
    AnonymousFaceConfig,
    QrBarcodeConfig,
)


class FaceAdapter(Protocol):
    def detect(
        self,
        image_path: Path,
        config: AnonymousFaceConfig,
    ) -> Sequence[VisualDetection]:
        """Return anonymous rectangles for one local sampled frame."""
        ...


class QrBarcodeAdapter(Protocol):
    def detect(
        self,
        image_path: Path,
        config: QrBarcodeConfig,
    ) -> Sequence[VisualDetection]:
        """Return QR/barcode rectangles and private-only decoded values."""
        ...


class OpenCvFaceAdapter:
    """Lazily use the frontal-face cascade packaged with local OpenCV."""

    def __init__(self) -> None:
        self._classifier: Any | None = None

    def detect(
        self,
        image_path: Path,
        config: AnonymousFaceConfig,
    ) -> tuple[VisualDetection, ...]:
        cv2 = importlib.import_module("cv2")
        image = _read_local_image(cv2, image_path)
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        classifier = self._load_classifier(cv2)
        rectangles = classifier.detectMultiScale(
            gray,
            scaleFactor=config.scale_factor,
            minNeighbors=config.neighbor_count,
            minSize=(config.minimum_size_pixels, config.minimum_size_pixels),
        )
        height, width = gray.shape[:2]
        detections = [
            VisualDetection(
                box=_pixel_box(x, y, item_width, item_height, width, height),
                confidence=0.5,
            )
            for x, y, item_width, item_height in rectangles
            if item_width >= config.minimum_size_pixels
            and item_height >= config.minimum_size_pixels
        ]
        return tuple(sorted(detections, key=_detection_sort_key))

    def _load_classifier(self, cv2: Any) -> Any:
        if self._classifier is None:
            cascade_root = Path(str(cv2.data.haarcascades))
            cascade_path = cascade_root / "haarcascade_frontalface_default.xml"
            classifier = cv2.CascadeClassifier(str(cascade_path))
            if classifier.empty():
                raise RuntimeError(
                    "OpenCV packaged frontal-face cascade is unavailable"
                )
            self._classifier = classifier
        return self._classifier


class OpenCvQrBarcodeAdapter:
    """Lazily use local OpenCV QR detection without network assets."""

    def __init__(self) -> None:
        self._qr_detector: Any | None = None

    def detect(
        self,
        image_path: Path,
        config: QrBarcodeConfig,
    ) -> tuple[VisualDetection, ...]:
        cv2 = importlib.import_module("cv2")
        image = _read_local_image(cv2, image_path)
        if config.scale_factor != 1.0:
            image = cv2.resize(
                image,
                None,
                fx=config.scale_factor,
                fy=config.scale_factor,
                interpolation=cv2.INTER_LINEAR,
            )
        if self._qr_detector is None:
            self._qr_detector = cv2.QRCodeDetector()
        height, width = image.shape[:2]
        detections = _detect_qr_codes(
            self._qr_detector,
            image,
            width=width,
            height=height,
            minimum_size_pixels=config.minimum_size_pixels,
        )
        return tuple(sorted(detections, key=_detection_sort_key))


class AnonymousFaceScanner:
    """Propose reviewable face-like regions without recognizing people."""

    id = "anonymous_face"
    display_name = "Anonymous face-region proposals"
    version = _SCANNER_VERSION
    description = "Proposes face-like regions using a local CPU cascade."
    requirements = PrivacyScannerRequirements(estimated_cost="medium")
    config_model: type[BaseModel] = AnonymousFaceConfig

    def __init__(self, adapter: FaceAdapter | None = None) -> None:
        self._adapter = adapter if adapter is not None else OpenCvFaceAdapter()

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        settings = AnonymousFaceConfig.model_validate(config.model_dump())
        observations = _collect_observations(
            context,
            settings,
            self._adapter.detect,
        )
        tracks = track_regions(
            observations,
            settings.maximum_gap_seconds,
            settings.tracking_iou,
            settings.maximum_center_distance,
        )
        return _risks_from_tracks(
            scanner_id=self.id,
            scanner_version=self.version,
            context=context,
            tracks=tracks[: settings.maximum_risks],
            guard_seconds=settings.guard_seconds,
        )


class QrBarcodeScanner:
    """Propose local QR/barcode regions while keeping decoded data private."""

    id = "qr_barcode"
    display_name = "QR and barcode proposals"
    version = _SCANNER_VERSION
    description = "Proposes QR and supported barcode regions using local OpenCV."
    requirements = PrivacyScannerRequirements(estimated_cost="medium")
    config_model: type[BaseModel] = QrBarcodeConfig

    def __init__(self, adapter: QrBarcodeAdapter | None = None) -> None:
        self._adapter = adapter if adapter is not None else OpenCvQrBarcodeAdapter()

    def scan(
        self,
        context: PrivacyScanContext,
        config: BaseModel,
    ) -> list[PrivacyRisk]:
        settings = QrBarcodeConfig.model_validate(config.model_dump())
        observations = _collect_observations(
            context,
            settings,
            self._adapter.detect,
        )
        tracks = track_regions(
            observations,
            settings.maximum_gap_seconds,
            settings.tracking_iou,
            settings.maximum_center_distance,
        )
        return _risks_from_tracks(
            scanner_id=self.id,
            scanner_version=self.version,
            context=context,
            tracks=tracks[: settings.maximum_risks],
            guard_seconds=settings.guard_seconds,
        )


def track_regions(
    observations: Iterable[VisualObservation],
    max_gap_seconds: float,
    minimum_iou: float,
    maximum_center_distance: float,
) -> tuple[VisualTrack, ...]:
    """Associate rectangles using only scene-local geometry and timing."""
    if not math.isfinite(max_gap_seconds) or max_gap_seconds < 0:
        raise ValueError("max_gap_seconds must be finite and non-negative")
    if not math.isfinite(minimum_iou) or not 0 <= minimum_iou <= 1:
        raise ValueError("minimum_iou must be between zero and one")
    if not math.isfinite(
        maximum_center_distance
    ) or not 0 <= maximum_center_distance <= math.sqrt(2):
        raise ValueError(
            "maximum_center_distance must be between zero and square root of two"
        )

    ordered = sorted(observations, key=_observation_sort_key)
    mutable_tracks: list[list[VisualObservation]] = []
    track_has_gap: list[bool] = []
    for observation in ordered:
        candidates: list[tuple[int, float, float, int]] = []
        for track_index, items in enumerate(mutable_tracks):
            previous = items[-1]
            if previous.scene_index != observation.scene_index:
                continue
            if previous.risk_type is not observation.risk_type:
                continue
            if previous.sample_index == observation.sample_index:
                continue
            time_gap = observation.timestamp_seconds - previous.timestamp_seconds
            if time_gap < 0 or time_gap > max_gap_seconds:
                continue
            overlap = _intersection_over_union(previous.box, observation.box)
            center_distance = _center_distance(previous.box, observation.box)
            if overlap < minimum_iou and center_distance > maximum_center_distance:
                continue
            candidates.append(
                (
                    0 if overlap >= minimum_iou else 1,
                    -overlap,
                    center_distance,
                    track_index,
                )
            )
        if candidates:
            selected = min(candidates)[-1]
            previous = mutable_tracks[selected][-1]
            if observation.sample_index > previous.sample_index + 1:
                track_has_gap[selected] = True
            mutable_tracks[selected].append(observation)
        else:
            mutable_tracks.append([observation])
            track_has_gap.append(False)

    prepared = sorted(
        zip(mutable_tracks, track_has_gap, strict=True),
        key=lambda item: _observation_sort_key(item[0][0]),
    )
    prefix_counts: dict[str, int] = {}
    result: list[VisualTrack] = []
    for items, has_gap in prepared:
        risk_type = items[0].risk_type
        prefix = _track_prefix(risk_type)
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
        result.append(
            VisualTrack(
                anonymous_id=f"{prefix}_track_{prefix_counts[prefix]:02d}",
                risk_type=risk_type,
                scene_index=items[0].scene_index,
                start_seconds=items[0].timestamp_seconds,
                end_seconds=items[-1].timestamp_seconds,
                box=_union_boxes(item.box for item in items),
                observations=tuple(items),
                has_gap=has_gap,
            )
        )
    return tuple(result)


def _collect_observations(
    context: PrivacyScanContext,
    config: _VisualConfigT,
    detect: Callable[[Path, _VisualConfigT], Sequence[VisualDetection]],
) -> tuple[VisualObservation, ...]:
    observations: list[VisualObservation] = []
    for sample in sorted(
        context.frame_samples,
        key=lambda item: (item.timestamp_seconds, item.sample_index),
    ):
        if context.is_cancelled():
            break
        frame_path = context.resolve_frame_path(sample.relative_path)
        scene_index = _scene_index(context, sample.timestamp_seconds)
        for detection in sorted(
            detect(frame_path, config),
            key=_detection_sort_key,
        ):
            observations.append(
                VisualObservation(
                    timestamp_seconds=sample.timestamp_seconds,
                    sample_index=sample.sample_index,
                    scene_index=scene_index,
                    relative_path=sample.relative_path,
                    risk_type=detection.risk_type,
                    box=detection.box,
                    confidence=detection.confidence,
                    decoded_payload=detection.decoded_payload,
                )
            )
    return tuple(sorted(observations, key=_observation_sort_key))


def _risks_from_tracks(
    *,
    scanner_id: str,
    scanner_version: str,
    context: PrivacyScanContext,
    tracks: Sequence[VisualTrack],
    guard_seconds: float,
) -> list[PrivacyRisk]:
    risks: list[PrivacyRisk] = []
    for track in tracks:
        scene_start, scene_end = _scene_bounds(context, track.scene_index)
        start_seconds = max(scene_start, track.start_seconds - guard_seconds)
        end_seconds = min(scene_end, track.end_seconds + guard_seconds)
        title, description, limitations = _risk_copy(track)
        private_evidence: list[dict[str, JsonValue]] = []
        for observation in track.observations:
            if observation.decoded_payload is not None:
                private_evidence.append(
                    {
                        "timestamp_seconds": observation.timestamp_seconds,
                        "decoded_payload": observation.decoded_payload,
                    }
                )
        risks.append(
            PrivacyRisk(
                id=make_privacy_risk_id(
                    context.input_hash,
                    scanner_id,
                    track.risk_type,
                    start_seconds,
                    end_seconds,
                    track.box,
                ),
                scanner_id=scanner_id,
                scanner_version=scanner_version,
                risk_type=track.risk_type,
                title=title,
                public_description=description,
                severity=(
                    Severity.MEDIUM
                    if track.risk_type is PrivacyRiskType.FACE_REGION
                    else Severity.HIGH
                ),
                confidence=sum(
                    observation.confidence for observation in track.observations
                )
                / len(track.observations),
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                box=track.box,
                track_id=track.anonymous_id,
                recommended_style=_recommended_style(context, track.risk_type),
                limitations=limitations,
                evidence=tuple(
                    {
                        "timestamp_seconds": observation.timestamp_seconds,
                        "sample_index": observation.sample_index,
                        "relative_path": observation.relative_path,
                        "box": observation.box.model_dump(mode="json"),
                    }
                    for observation in track.observations
                ),
                private_evidence=tuple(private_evidence),
            )
        )
    return risks


def _recommended_style(
    context: PrivacyScanContext,
    risk_type: PrivacyRiskType,
) -> RedactionStyle:
    del risk_type
    return context.profile.default_visual_style


def _risk_copy(track: VisualTrack) -> tuple[str, str, tuple[str, ...]]:
    gap_note = (
        "A sampling gap occurs inside this track and requires careful review."
        if track.has_gap
        else "Sampled-frame tracking may miss appearances between samples."
    )
    if track.risk_type is PrivacyRiskType.FACE_REGION:
        return (
            "Face-like region proposed for review",
            "A local CPU heuristic observed a face-like region in sampled frames.",
            (
                "The cascade can miss faces or propose non-face regions.",
                "Pose, occlusion, lighting, and small regions can reduce reliability.",
                gap_note,
            ),
        )
    return (
        "QR or barcode region proposed for review",
        "A local CPU detector observed a code-like region in sampled frames.",
        (
            "Detection support varies with the locally installed OpenCV build.",
            "Small, blurred, rotated, or partly hidden codes may be missed.",
            gap_note,
        ),
    )


def _scene_index(context: PrivacyScanContext, timestamp_seconds: float) -> int:
    if not context.scenes:
        return 0
    for index, scene in enumerate(context.scenes):
        is_last = index == len(context.scenes) - 1
        if scene.start_seconds <= timestamp_seconds < scene.end_seconds or (
            is_last and timestamp_seconds == scene.end_seconds
        ):
            return scene.scene_index
    raise ValueError("sample timestamp is outside declared scene intervals")


def _scene_bounds(context: PrivacyScanContext, scene_index: int) -> tuple[float, float]:
    if not context.scenes:
        return (0.0, context.duration_seconds)
    for scene in context.scenes:
        if scene.scene_index == scene_index:
            return (scene.start_seconds, scene.end_seconds)
    raise ValueError("visual track references an unknown scene")


def _detect_qr_codes(
    detector: Any,
    image: Any,
    *,
    width: int,
    height: int,
    minimum_size_pixels: int,
) -> list[VisualDetection]:
    detections: list[VisualDetection] = []
    try:
        detected, decoded_values, points, _ = detector.detectAndDecodeMulti(image)
    except (AttributeError, ValueError):
        detected = False
        decoded_values = ()
        points = None
    if detected and points is not None:
        for index, polygon in enumerate(points):
            payload = str(decoded_values[index]) if index < len(decoded_values) else ""
            if not payload:
                payload = _decode_small_qr(detector, image, polygon)
            detection = _code_detection_from_polygon(
                polygon,
                width=width,
                height=height,
                minimum_size_pixels=minimum_size_pixels,
                payload=payload,
            )
            if detection is not None:
                detections.append(detection)
        return detections

    payload, points, _ = detector.detectAndDecode(image)
    if points is not None:
        if not payload:
            payload = _decode_small_qr(detector, image, points)
        detection = _code_detection_from_polygon(
            points,
            width=width,
            height=height,
            minimum_size_pixels=minimum_size_pixels,
            payload=str(payload),
        )
        if detection is not None:
            detections.append(detection)
    return detections


def _decode_small_qr(detector: Any, image: Any, polygon: Any) -> str:
    """Retry a located tiny QR crop with a local quiet zone and integer scaling."""
    np = importlib.import_module("numpy")
    points = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
    if len(points) < 4:
        return ""
    image_height, image_width = image.shape[:2]
    x_min = max(0, int(math.floor(float(points[:, 0].min()))) - 1)
    y_min = max(0, int(math.floor(float(points[:, 1].min()))) - 1)
    x_max = min(image_width, int(math.ceil(float(points[:, 0].max()))) + 2)
    y_max = min(image_height, int(math.ceil(float(points[:, 1].max()))) + 2)
    crop = image[y_min:y_max, x_min:x_max]
    if crop.size == 0:
        return ""
    border = max(4, min(crop.shape[:2]) // 8)
    padding = (
        ((border, border), (border, border), (0, 0))
        if crop.ndim == 3
        else ((border, border), (border, border))
    )
    padded = np.pad(crop, padding, mode="constant", constant_values=255)
    enlarged = np.repeat(np.repeat(padded, 8, axis=0), 8, axis=1)
    payload, _, _ = detector.detectAndDecode(enlarged)
    return str(payload)


def _code_detection_from_polygon(
    polygon: Any,
    *,
    width: int,
    height: int,
    minimum_size_pixels: int,
    payload: str,
) -> VisualDetection | None:
    np = importlib.import_module("numpy")
    points = np.asarray(polygon, dtype=np.float64).reshape(-1, 2)
    if len(points) < 4:
        return None
    x_min = float(points[:, 0].min())
    y_min = float(points[:, 1].min())
    x_max = float(points[:, 0].max())
    y_max = float(points[:, 1].max())
    if x_max - x_min < minimum_size_pixels or y_max - y_min < minimum_size_pixels:
        return None
    box = _pixel_box(
        x_min,
        y_min,
        x_max - x_min,
        y_max - y_min,
        width,
        height,
    )
    return VisualDetection(
        box=box,
        confidence=0.95 if payload else 0.65,
        risk_type=PrivacyRiskType.QR_CODE,
        decoded_payload=payload or None,
    )


def _read_local_image(cv2: Any, image_path: Path) -> Any:
    np = importlib.import_module("numpy")
    encoded = np.frombuffer(image_path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("sampled frame could not be decoded")
    return image


def _pixel_box(
    x: float,
    y: float,
    width: float,
    height: float,
    frame_width: int,
    frame_height: int,
) -> NormalizedBox:
    x_min = max(0.0, min(1.0, float(x) / frame_width))
    y_min = max(0.0, min(1.0, float(y) / frame_height))
    x_max = max(0.0, min(1.0, float(x + width) / frame_width))
    y_max = max(0.0, min(1.0, float(y + height) / frame_height))
    return NormalizedBox(
        x_min=x_min,
        y_min=y_min,
        x_max=x_max,
        y_max=y_max,
    )


def _intersection_over_union(first: NormalizedBox, second: NormalizedBox) -> float:
    x_min = max(first.x_min, second.x_min)
    y_min = max(first.y_min, second.y_min)
    x_max = min(first.x_max, second.x_max)
    y_max = min(first.y_max, second.y_max)
    intersection = max(0.0, x_max - x_min) * max(0.0, y_max - y_min)
    first_area = (first.x_max - first.x_min) * (first.y_max - first.y_min)
    second_area = (second.x_max - second.x_min) * (second.y_max - second.y_min)
    union = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def _center_distance(first: NormalizedBox, second: NormalizedBox) -> float:
    first_x = (first.x_min + first.x_max) / 2
    first_y = (first.y_min + first.y_max) / 2
    second_x = (second.x_min + second.x_max) / 2
    second_y = (second.y_min + second.y_max) / 2
    return math.hypot(first_x - second_x, first_y - second_y)


def _union_boxes(boxes: Iterable[NormalizedBox]) -> NormalizedBox:
    values = tuple(boxes)
    if not values:
        raise ValueError("cannot union an empty box sequence")
    return NormalizedBox(
        x_min=min(box.x_min for box in values),
        y_min=min(box.y_min for box in values),
        x_max=max(box.x_max for box in values),
        y_max=max(box.y_max for box in values),
    )


def _track_prefix(risk_type: PrivacyRiskType) -> str:
    return {
        PrivacyRiskType.FACE_REGION: "face",
        PrivacyRiskType.QR_CODE: "qr",
        PrivacyRiskType.BARCODE: "barcode",
    }[risk_type]


def _box_sort_key(box: NormalizedBox) -> tuple[float, float, float, float]:
    return (box.x_min, box.y_min, box.x_max, box.y_max)


def _detection_sort_key(
    detection: VisualDetection,
) -> tuple[str, float, float, float, float, float, str]:
    return (
        detection.risk_type.value,
        *_box_sort_key(detection.box),
        -detection.confidence,
        detection.decoded_payload or "",
    )


def _observation_sort_key(
    observation: VisualObservation,
) -> tuple[int, float, int, str, float, float, float, float, str]:
    return (
        observation.scene_index,
        observation.timestamp_seconds,
        observation.sample_index,
        observation.risk_type.value,
        *_box_sort_key(observation.box),
        observation.relative_path,
    )


__all__ = [
    "AnonymousFaceConfig",
    "AnonymousFaceScanner",
    "OpenCvFaceAdapter",
    "OpenCvQrBarcodeAdapter",
    "QrBarcodeConfig",
    "QrBarcodeScanner",
    "VisualDetection",
    "VisualObservation",
    "VisualTrack",
    "track_regions",
]
