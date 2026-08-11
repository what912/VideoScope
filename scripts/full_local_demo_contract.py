"""Strict, path-safe contract for the full local four-mode demo."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

_SCHEMA_VERSION = "1.0"
_DURATION_SECONDS = 42.0
_WIDTH = 1280
_HEIGHT = 720
_FRAME_RATE = 24
_CONTAINER = "mp4"
_FRAME_RATE_MODE = "cfr"
_SCENE_IDS = (
    "clean_hook",
    "rescue_evidence",
    "useful_tutorial",
    "low_information",
    "privacy_zone",
    "motion_retake",
    "verified_ending",
)
_SCENE_RANGES = (
    (0.0, 5.0),
    (5.0, 10.0),
    (10.0, 20.0),
    (20.0, 25.0),
    (25.0, 32.0),
    (32.0, 36.0),
    (36.0, 42.0),
)
_PRIVACY_RANGE = (25.0, 32.0)
_PRIVACY_BOX = (0.58, 0.18, 0.94, 0.78)
_USEFUL_KEEP_RANGES = ((0.0, 5.0), (10.0, 20.0), (36.0, 42.0))
_FICTIONAL_IDENTIFIERS = (
    "demo.user@example.invalid",
    "+1 202-555-0107",
    "00.0000, 000.0000",
)
_CONTRACT_KEYS = {
    "schema_version",
    "duration_seconds",
    "width",
    "height",
    "frame_rate",
    "container",
    "frame_rate_mode",
    "video",
    "audio",
    "scenes",
    "privacy",
    "useful_keep_ranges",
    "fictional_identifiers",
    "source_conditions",
}


@dataclass(frozen=True, slots=True)
class DemoScene:
    scene_id: str
    start_seconds: float
    end_seconds: float
    purpose: str


@dataclass(frozen=True, slots=True)
class DemoPrivacySelection:
    start_seconds: float
    end_seconds: float
    box: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class DemoContract:
    schema_version: str
    duration_seconds: float
    width: int
    height: int
    frame_rate: int
    container: str
    frame_rate_mode: str
    scenes: tuple[DemoScene, ...]
    privacy: DemoPrivacySelection
    useful_keep_ranges: tuple[tuple[float, float], ...]
    fictional_identifiers: tuple[str, ...]
    video_codec: str
    pixel_format: str
    audio_codec: str
    audio_channels: int
    audio_sample_rate_hz: int
    source_conditions: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DemoContract:
        payload = dict(value)
        _require_exact_keys(payload, _CONTRACT_KEYS, "contract")
        _reject_unsafe_strings(payload)

        scenes_value = _require_sequence(payload["scenes"], "scenes")
        scenes = tuple(_parse_scene(scene) for scene in scenes_value)
        privacy = _parse_privacy(payload["privacy"])
        useful_keep_ranges = tuple(
            _parse_range(item, "useful_keep_ranges")
            for item in _require_sequence(
                payload["useful_keep_ranges"], "useful_keep_ranges"
            )
        )
        identifiers = tuple(
            _require_string(item, "fictional_identifiers")
            for item in _require_sequence(
                payload["fictional_identifiers"], "fictional_identifiers"
            )
        )
        video = _parse_format(payload["video"], {"codec", "pixel_format"}, "video")
        audio = _parse_format(
            payload["audio"], {"codec", "channels", "sample_rate_hz"}, "audio"
        )
        source_conditions = tuple(
            _require_string(item, "source_conditions")
            for item in _require_sequence(
                payload["source_conditions"], "source_conditions"
            )
        )
        contract = cls(
            schema_version=_require_string(payload["schema_version"], "schema_version"),
            duration_seconds=_require_float(
                payload["duration_seconds"], "duration_seconds"
            ),
            width=_require_int(payload["width"], "width"),
            height=_require_int(payload["height"], "height"),
            frame_rate=_require_int(payload["frame_rate"], "frame_rate"),
            container=_require_string(payload["container"], "container"),
            frame_rate_mode=_require_string(
                payload["frame_rate_mode"], "frame_rate_mode"
            ),
            scenes=scenes,
            privacy=privacy,
            useful_keep_ranges=useful_keep_ranges,
            fictional_identifiers=identifiers,
            video_codec=_require_string(video["codec"], "video.codec"),
            pixel_format=_require_string(video["pixel_format"], "video.pixel_format"),
            audio_codec=_require_string(audio["codec"], "audio.codec"),
            audio_channels=_require_int(audio["channels"], "audio.channels"),
            audio_sample_rate_hz=_require_int(
                audio["sample_rate_hz"], "audio.sample_rate_hz"
            ),
            source_conditions=source_conditions,
        )
        _validate_contract(contract)
        return contract


def load_demo_contract(path: Path) -> DemoContract:
    """Load and validate a UTF-8 JSON demo contract."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot load demo contract: {path.name}") from error
    if not isinstance(loaded, Mapping):
        raise ValueError("demo contract must be a JSON object")
    return DemoContract.from_mapping(loaded)


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    """Serialize a JSON object deterministically for reproducible hashing."""
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def stream_sha256(path: Path) -> str:
    """Return the SHA-256 digest without loading the complete file into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(path: Path, root: Path) -> str:
    """Return a contained artifact path with portable POSIX separators."""
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("path must be contained in root") from error
    if ".." in relative.parts:
        raise ValueError("relative path must not contain '..'")
    return relative.as_posix()


def _parse_scene(value: object) -> DemoScene:
    payload = _require_mapping(value, "scene")
    _require_exact_keys(
        payload, {"scene_id", "start_seconds", "end_seconds", "purpose"}, "scene"
    )
    return DemoScene(
        scene_id=_require_string(payload["scene_id"], "scene_id"),
        start_seconds=_require_float(payload["start_seconds"], "start_seconds"),
        end_seconds=_require_float(payload["end_seconds"], "end_seconds"),
        purpose=_require_string(payload["purpose"], "purpose"),
    )


def _parse_privacy(value: object) -> DemoPrivacySelection:
    payload = _require_mapping(value, "privacy")
    _require_exact_keys(payload, {"start_seconds", "end_seconds", "box"}, "privacy")
    box_values = _require_sequence(payload["box"], "privacy.box")
    if len(box_values) != 4:
        raise ValueError("privacy.box must contain four values")
    parsed_box = tuple(_require_float(item, "privacy.box") for item in box_values)
    return DemoPrivacySelection(
        start_seconds=_require_float(payload["start_seconds"], "privacy.start_seconds"),
        end_seconds=_require_float(payload["end_seconds"], "privacy.end_seconds"),
        box=(parsed_box[0], parsed_box[1], parsed_box[2], parsed_box[3]),
    )


def _parse_range(value: object, name: str) -> tuple[float, float]:
    items = _require_sequence(value, name)
    if len(items) != 2:
        raise ValueError(f"{name} ranges must contain two values")
    return (_require_float(items[0], name), _require_float(items[1], name))


def _parse_format(
    value: object, expected_keys: set[str], name: str
) -> dict[str, object]:
    payload = _require_mapping(value, name)
    _require_exact_keys(payload, expected_keys, name)
    return payload


def _require_mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _require_sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an array")
    return value


def _require_exact_keys(
    payload: Mapping[str, object], expected: set[str], name: str
) -> None:
    if set(payload) != expected:
        raise ValueError(f"{name} has unknown or missing keys")


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _reject_unsafe_strings(value: object) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_unsafe_strings(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _reject_unsafe_strings(item)
    elif isinstance(value, str):
        if urlsplit(value).scheme:
            raise ValueError("URL schemes are not allowed")
        windows_path = PureWindowsPath(value)
        if (
            PurePosixPath(value).is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or windows_path.root
        ):
            raise ValueError("absolute paths are not allowed")


def _validate_contract(contract: DemoContract) -> None:
    if contract.schema_version != _SCHEMA_VERSION:
        raise ValueError("unsupported schema_version")
    if contract.duration_seconds != _DURATION_SECONDS:
        raise ValueError("duration_seconds must be 42.0")
    if (contract.width, contract.height, contract.frame_rate) != (
        _WIDTH,
        _HEIGHT,
        _FRAME_RATE,
    ):
        raise ValueError("contract must be 1280x720 at 24 fps")
    if contract.container != _CONTAINER:
        raise ValueError("container must be mp4")
    if contract.frame_rate_mode != _FRAME_RATE_MODE:
        raise ValueError("frame_rate_mode must be cfr")
    if (contract.video_codec, contract.pixel_format) != ("h264", "yuv420p"):
        raise ValueError("video format must be h264 yuv420p")
    if (
        contract.audio_codec,
        contract.audio_channels,
        contract.audio_sample_rate_hz,
    ) != ("aac", 2, 48000):
        raise ValueError("audio format must be AAC stereo at 48 kHz")
    if tuple(scene.scene_id for scene in contract.scenes) != _SCENE_IDS:
        raise ValueError("scene IDs do not match the approved contract")
    ranges = tuple(
        (scene.start_seconds, scene.end_seconds) for scene in contract.scenes
    )
    if ranges != _SCENE_RANGES:
        raise ValueError("scenes must be contiguous and match the approved timeline")
    if not contract.scenes or contract.scenes[-1].end_seconds != _DURATION_SECONDS:
        raise ValueError("final scene must end at 42.0")
    if any(scene.end_seconds <= scene.start_seconds for scene in contract.scenes):
        raise ValueError("each scene must have a positive duration")
    if (
        contract.privacy.start_seconds,
        contract.privacy.end_seconds,
        contract.privacy.box,
    ) != (*_PRIVACY_RANGE, _PRIVACY_BOX):
        raise ValueError("privacy selection must match the approved range and box")
    if contract.useful_keep_ranges != _USEFUL_KEEP_RANGES:
        raise ValueError("useful keep ranges must match the approved selection")
    if contract.fictional_identifiers != _FICTIONAL_IDENTIFIERS:
        raise ValueError("fictional identifiers must be the three approved values")
    if not contract.source_conditions:
        raise ValueError("source_conditions must not be empty")
