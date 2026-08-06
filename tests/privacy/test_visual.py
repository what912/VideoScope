"""Tests for deterministic anonymous visual privacy proposals."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from videoscope.privacy.models import NormalizedBox, PrivacyRiskType
from videoscope.privacy.profiles import PUBLIC
from videoscope.privacy.scanners import PrivacyScanContext
from videoscope.privacy.visual import (
    AnonymousFaceConfig,
    AnonymousFaceScanner,
    OpenCvQrBarcodeAdapter,
    QrBarcodeConfig,
    QrBarcodeScanner,
    VisualDetection,
    VisualObservation,
    track_regions,
)
from videoscope.scenes import VideoScene
from videoscope.video import FrameSample


def _box(x_min: float, *, width: float = 0.2) -> NormalizedBox:
    return NormalizedBox(
        x_min=x_min,
        y_min=0.2,
        x_max=x_min + width,
        y_max=0.6,
    )


def _observation(
    *,
    timestamp: float,
    sample_index: int,
    scene_index: int = 0,
    x_min: float = 0.1,
    width: float = 0.2,
    risk_type: PrivacyRiskType = PrivacyRiskType.FACE_REGION,
) -> VisualObservation:
    return VisualObservation(
        timestamp_seconds=timestamp,
        sample_index=sample_index,
        scene_index=scene_index,
        relative_path=f"frames/frame_{sample_index:06d}.png",
        risk_type=risk_type,
        box=_box(x_min, width=width),
        confidence=0.9,
    )


def test_track_regions_keeps_anonymous_id_through_short_occlusion() -> None:
    observations = (
        _observation(timestamp=0.0, sample_index=0, x_min=0.10),
        _observation(timestamp=0.3, sample_index=2, x_min=0.12),
    )

    tracks = track_regions(
        observations=observations,
        max_gap_seconds=0.35,
        minimum_iou=0.25,
        maximum_center_distance=0.25,
    )

    assert len(tracks) == 1
    assert tracks[0].anonymous_id == "face_track_01"
    assert tracks[0].has_gap is True
    assert [item.sample_index for item in tracks[0].observations] == [0, 2]


def test_scene_boundary_starts_a_new_visual_track() -> None:
    observations = (
        _observation(timestamp=0.9, sample_index=4, scene_index=0),
        _observation(timestamp=1.0, sample_index=5, scene_index=1),
    )

    tracks = track_regions(
        observations=observations,
        max_gap_seconds=0.35,
        minimum_iou=0.25,
        maximum_center_distance=0.25,
    )

    assert [track.anonymous_id for track in tracks] == [
        "face_track_01",
        "face_track_02",
    ]
    assert [track.scene_index for track in tracks] == [0, 1]


def test_tracking_uses_center_continuity_when_iou_is_below_threshold() -> None:
    observations = (
        _observation(timestamp=0.0, sample_index=0, x_min=0.10, width=0.1),
        _observation(timestamp=0.2, sample_index=1, x_min=0.17, width=0.1),
    )

    tracks = track_regions(
        observations,
        max_gap_seconds=0.3,
        minimum_iou=0.5,
        maximum_center_distance=0.25,
    )

    assert len(tracks) == 1


def test_tracking_is_deterministic_for_crossing_candidates() -> None:
    observations = (
        _observation(timestamp=0.0, sample_index=0, x_min=0.10),
        _observation(timestamp=0.0, sample_index=0, x_min=0.60),
        _observation(timestamp=0.2, sample_index=1, x_min=0.12),
        _observation(timestamp=0.2, sample_index=1, x_min=0.58),
    )

    first = track_regions(
        observations,
        max_gap_seconds=0.3,
        minimum_iou=0.25,
        maximum_center_distance=0.25,
    )
    second = track_regions(reversed(observations), 0.3, 0.25, 0.25)

    assert first == second
    assert [track.anonymous_id for track in first] == [
        "face_track_01",
        "face_track_02",
    ]


def test_tracking_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="max_gap_seconds"):
        track_regions((), -0.1, 0.25, 0.25)
    with pytest.raises(ValueError, match="minimum_iou"):
        track_regions((), 0.2, 1.1, 0.25)
    with pytest.raises(ValueError, match="maximum_center_distance"):
        track_regions((), 0.2, 0.25, 1.5)


def test_tracking_default_gap_supports_two_fps_sampling_cadence() -> None:
    observations = (
        _observation(timestamp=0.0, sample_index=0, x_min=0.10),
        _observation(timestamp=0.5, sample_index=1, x_min=0.12),
    )
    config = AnonymousFaceConfig()

    tracks = track_regions(
        observations,
        config.maximum_gap_seconds,
        config.tracking_iou,
        config.maximum_center_distance,
    )

    assert config.maximum_gap_seconds > 0.5
    assert len(tracks) == 1


class FakeFaceAdapter:
    def detect(
        self,
        image_path: Path,
        config: AnonymousFaceConfig,
    ) -> tuple[VisualDetection, ...]:
        assert image_path.is_file()
        assert config.minimum_size_pixels >= 1
        return (VisualDetection(box=_box(0.2), confidence=0.82),)


class FakeQrAdapter:
    def __init__(self, payload: str = "https://private.example") -> None:
        self.payload = payload

    def detect(
        self,
        image_path: Path,
        config: QrBarcodeConfig,
    ) -> tuple[VisualDetection, ...]:
        assert image_path.is_file()
        assert config.minimum_size_pixels >= 1
        return (
            VisualDetection(
                box=_box(0.5),
                confidence=0.95,
                risk_type=PrivacyRiskType.QR_CODE,
                decoded_payload=self.payload,
            ),
        )


def _visual_context(tmp_path: Path, *, frame_count: int = 2) -> PrivacyScanContext:
    source = tmp_path / "来源 视频.mp4"
    source.write_bytes(b"source")
    workspace = tmp_path / "私有 审查"
    frames = workspace / "帧 序列"
    frames.mkdir(parents=True)
    samples: list[FrameSample] = []
    for index in range(frame_count):
        relative_path = f"帧 序列/帧_{index:02d}.png"
        Image.new("RGB", (80, 60), "white").save(workspace / relative_path)
        samples.append(
            FrameSample(
                timestamp_seconds=index * 0.2,
                sample_index=index,
                relative_path=relative_path,
                width=80,
                height=60,
            )
        )
    return PrivacyScanContext(
        input_path=source,
        input_hash="d" * 64,
        duration_seconds=1.0,
        profile=PUBLIC,
        workspace=workspace,
        frame_samples=tuple(samples),
        scenes=(
            VideoScene(
                scene_index=0,
                start_seconds=0.0,
                end_seconds=1.0,
                duration_seconds=1.0,
                representative_timestamp=0.5,
            ),
        ),
    )


def test_face_scanner_uses_anonymous_track_names(tmp_path: Path) -> None:
    scanner = AnonymousFaceScanner(adapter=FakeFaceAdapter())

    risks = scanner.scan(_visual_context(tmp_path), AnonymousFaceConfig())

    assert len(risks) == 1
    assert risks[0].track_id == "face_track_01"
    assert risks[0].risk_type is PrivacyRiskType.FACE_REGION
    assert "identity" not in risks[0].model_dump_json().lower()
    assert risks[0].evidence
    assert risks[0].limitations


def test_qr_scanner_returns_decoded_region_without_public_payload(
    tmp_path: Path,
) -> None:
    payload = "https://private.example/account?id=42"
    scanner = QrBarcodeScanner(adapter=FakeQrAdapter(payload=payload))

    risks = scanner.scan(_visual_context(tmp_path), QrBarcodeConfig())

    assert len(risks) == 1
    risk = risks[0]
    assert risk.risk_type is PrivacyRiskType.QR_CODE
    assert risk.private_evidence[0]["decoded_payload"] == payload
    assert "private.example" not in risk.public_description
    assert all("decoded_payload" not in item for item in risk.evidence)


def test_scanners_apply_guard_clip_duration_and_stable_risk_limit(
    tmp_path: Path,
) -> None:
    context = _visual_context(tmp_path, frame_count=3)
    scanner = AnonymousFaceScanner(adapter=FakeFaceAdapter())
    config = AnonymousFaceConfig(guard_seconds=0.3, maximum_risks=1)

    first = scanner.scan(context, config)
    second = scanner.scan(context, config)

    assert first == second
    assert len(first) == 1
    assert first[0].start_seconds == 0.0
    assert first[0].end_seconds <= context.duration_seconds


def test_guard_expansion_is_clamped_to_track_scene(tmp_path: Path) -> None:
    context = _visual_context(tmp_path, frame_count=1)
    sample = context.frame_samples[0].model_copy(update={"timestamp_seconds": 0.95})
    bounded = context.model_copy(
        update={
            "duration_seconds": 2.0,
            "frame_samples": (sample,),
            "scenes": (
                VideoScene(
                    scene_index=0,
                    start_seconds=0.0,
                    end_seconds=1.0,
                    duration_seconds=1.0,
                    representative_timestamp=0.5,
                ),
                VideoScene(
                    scene_index=1,
                    start_seconds=1.0,
                    end_seconds=2.0,
                    duration_seconds=1.0,
                    representative_timestamp=1.5,
                ),
            ),
        }
    )

    risk = AnonymousFaceScanner(adapter=FakeFaceAdapter()).scan(
        bounded,
        AnonymousFaceConfig(guard_seconds=0.25),
    )[0]

    assert risk.start_seconds == 0.7
    assert risk.end_seconds == 1.0


def test_scanner_does_not_follow_frame_path_outside_workspace(tmp_path: Path) -> None:
    context = _visual_context(tmp_path, frame_count=1)
    escaped = context.frame_samples[0].model_copy(
        update={"relative_path": "../outside.png"}
    )
    unsafe = context.model_copy(update={"frame_samples": (escaped,)})

    with pytest.raises(ValueError, match="workspace"):
        AnonymousFaceScanner(adapter=FakeFaceAdapter()).scan(
            unsafe,
            AnonymousFaceConfig(),
        )


def test_visual_scanner_configs_reject_unsafe_values() -> None:
    with pytest.raises(ValidationError):
        AnonymousFaceConfig(scale_factor=1.0)
    with pytest.raises(ValidationError):
        QrBarcodeConfig(maximum_risks=0)
    with pytest.raises(ValidationError):
        AnonymousFaceConfig(maximum_center_distance=1.5)


@pytest.mark.optional  # type: ignore[untyped-decorator]
def test_real_opencv_qr_adapter_reads_local_generated_qr(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "QRCodeEncoder_create"):
        pytest.skip("installed OpenCV build does not provide QRCodeEncoder_create")
    encoder = cv2.QRCodeEncoder_create()
    image = encoder.encode("local-private-payload")
    encoded, buffer = cv2.imencode(".png", image)
    if not encoded:
        pytest.skip("installed OpenCV build could not encode the local QR fixture")
    path = tmp_path / "本地 二维码.png"
    path.write_bytes(buffer.tobytes())

    detections = OpenCvQrBarcodeAdapter().detect(path, QrBarcodeConfig())

    assert detections
    assert detections[0].decoded_payload == "local-private-payload"
