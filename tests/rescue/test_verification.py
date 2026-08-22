"""Independent, deterministic Rescue verification gates."""

from __future__ import annotations

import json
import math
import shutil
import wave
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

import cv2
import numpy as np
import pytest
from numpy.typing import NDArray
from pydantic import JsonValue

import videoscope.rescue.tonal as tonal_module
from videoscope.domain import VideoMetadata
from videoscope.rescue import verification as verification_module
from videoscope.rescue.assessment import (
    LocalRescueAssessmentService,
    SyncEventMeasurements,
)
from videoscope.rescue.audio import AudioNoiseInterval
from videoscope.rescue.capabilities import require_executable_action_scopes
from videoscope.rescue.deblur import BlurKernelEstimate, DeblurConfig
from videoscope.rescue.errors import RescueCancelledError, RescueMediaError
from videoscope.rescue.executor import CommandResult, SourceMapping
from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueConfirmation,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    canonical_video_encode_contract,
    make_damage_id,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.pipeline import (
    RescueConfig,
    RescuePipelineDependencies,
    RescueStatus,
    VideoRescuePipeline,
)
from videoscope.rescue.qualification import (
    SharpenProfileMeasurementV1,
    SharpenQualificationMetricsV1,
    SharpenQualificationThresholdsV1,
    SharpenVerificationControlHandle,
    SharpenVerificationControlRecipeV1,
    TonalVerificationControlHandle,
    TonalVerificationControlRecipeV1,
    VerificationControlHandle,
    VerificationControlRecipeV1,
    apply_qualified_sharpen_profile,
    build_sharpen_qualification_evidence,
    qualification_action_parameters,
)
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationConfig,
    TransitionConsensusStep,
)
from videoscope.rescue.timeline import normalize_actual_video_timestamps
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
)
from videoscope.rescue.tonal_qualification import (
    TonalAudioEncodeContractV2,
    TonalAudioTimelineV1,
    TonalAudioTopologyV2,
    TonalEncodedCandidateAttemptV2,
    TonalEncodedMetricsV2,
    TonalEncodedProfileQualificationV2,
    TonalEncodedQualificationEvidenceV3,
    TonalEncodedThresholdsV2,
    TonalRangeMappingV2,
    qualified_tonal_action_parameters,
)
from videoscope.rescue.verification import (
    MediaVerificationSnapshot,
    NativeMediaMeasurementProvider,
    ReferenceRenderOptions,
    RescueVerifier,
)
from videoscope.rescue.visual import (
    LumaAdjustmentConfig,
    SharpenConfig,
    VisualEvidence,
    VisualMetrics,
    derive_visual_action_parameters,
)

REAL_FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "generated"
REAL_MANIFEST_PATH = Path(__file__).parents[1] / "fixtures" / "manifest.json"


def _real_fixture(filename: str) -> Path:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip(
            "FFmpeg and ffprobe on PATH are required for real Rescue verification"
        )
    source = REAL_FIXTURE_ROOT / filename
    if not source.is_file():
        pytest.skip(
            "run `python scripts/generate_test_videos.py --force` before real "
            "Rescue verification"
        )
    return source


def _real_confirmation(preparation: Any) -> RescueConfirmation:
    accepted = tuple(
        action.id for action in preparation.plan.actions if action.requires_confirmation
    )
    improvement_kinds = {
        RescueActionKind.ADJUST_LUMA,
        RescueActionKind.DENOISE_VIDEO,
        RescueActionKind.SHARPEN,
        RescueActionKind.DEFLICKER,
        RescueActionKind.STABILIZE,
        RescueActionKind.NORMALIZE_AUDIO,
        RescueActionKind.DENOISE_AUDIO,
    }
    return RescueConfirmation(
        plan_digest=preparation.plan.plan_digest,
        publish_faithful=True,
        publish_improved=any(
            action.id in accepted and action.kind in improvement_kinds
            for action in preparation.plan.actions
        ),
        accepted_action_ids=accepted,
    )


def test_native_measurement_propagates_cancellation(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.mp4"
    candidate.write_bytes(b"candidate")

    def cancelled_probe(_path: Path) -> VideoMetadata:
        raise RescueCancelledError("cancelled")

    provider = NativeMediaMeasurementProvider(probe=cancelled_probe)
    with pytest.raises(RescueCancelledError):
        provider.measure(candidate, candidate.name, lambda: True)


def _native_provider_with_ffprobe_json(
    payload: object,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[NativeMediaMeasurementProvider, list[tuple[str, ...]]]:
    monkeypatch.setattr(
        verification_module,
        "_measure_visual_stream",
        lambda _path, _cancel: (0, 0, 0, 0.0, 0.0, 0.0),
    )
    calls: list[tuple[str, ...]] = []

    def probe(path: Path) -> VideoMetadata:
        return VideoMetadata(
            filename=path.name,
            container_format="mp4",
            codec="h264",
            width=16,
            height=16,
            duration_seconds=1.0,
            average_frame_rate=1.0,
            estimated_frame_count=1,
            has_audio=True,
            file_size_bytes=path.stat().st_size,
        )

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        if arguments[0] == "test-ffprobe" and arguments[1:] == ("-version",):
            return CommandResult(
                returncode=0,
                stderr_summary="",
                stdout_summary="ffprobe test-version\nconfiguration omitted",
            )
        if arguments[0] == "test-ffprobe":
            return CommandResult(
                returncode=0,
                stderr_summary="",
                stdout_summary=json.dumps(payload),
            )
        return CommandResult(returncode=0, stderr_summary="")

    return (
        NativeMediaMeasurementProvider(
            ffprobe="test-ffprobe", probe=probe, command_runner=runner
        ),
        calls,
    )


def _packet_payload(
    *,
    video_packet: dict[str, object] | None,
    audio_packet: dict[str, object] | None,
) -> dict[str, object]:
    packets: list[dict[str, object]] = []
    if audio_packet is not None:
        packets.append({"stream_index": 7, **audio_packet})
    if video_packet is not None:
        packets.append({"stream_index": 3, **video_packet})
    return {
        "streams": [
            {"index": 3, "codec_type": "video"},
            {"index": 7, "codec_type": "audio", "sample_rate": "48000"},
        ],
        "packets": packets,
    }


def test_packet_timestamps_determine_audio_video_residual(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches using stream start metadata instead of first packet timestamps."""
    provider, calls = _native_provider_with_ffprobe_json(
        _packet_payload(
            video_packet={"pts_time": "0.04"},
            audio_packet={"pts_time": "0.31"},
        ),
        monkeypatch,
    )
    media = tmp_path / "媒体 packet source.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, "faithful-rescue.mp4", lambda: False)
    provider.measure(media, "faithful-rescue.mp4", lambda: False)

    assert snapshot.av_offset_seconds == pytest.approx(0.27)
    assert snapshot.av_offset_method == "first_usable_packet_timestamp"
    assert snapshot.av_offset_tool_version == "ffprobe test-version"
    assert sum(call[1:] == ("-version",) for call in calls) == 1


def test_packet_timestamp_applies_aac_skip_samples_to_the_usable_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, _ = _native_provider_with_ffprobe_json(
        _packet_payload(
            video_packet={"pts_time": "0.000000"},
            audio_packet={
                "pts_time": "-0.042667",
                "side_data_list": [
                    {
                        "side_data_type": "Skip Samples",
                        "skip_samples": 2048,
                    }
                ],
            },
        ),
        monkeypatch,
    )
    media = tmp_path / "aac priming.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, media.name, lambda: False)

    assert snapshot.av_offset_seconds == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("sample_rate", [None, "invalid", "0"])
def test_packet_timestamp_fails_closed_when_skip_sample_rate_is_unusable(
    sample_rate: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _packet_payload(
        video_packet={"pts_time": "0.0"},
        audio_packet={
            "pts_time": "-0.042667",
            "side_data_list": [
                {"side_data_type": "Skip Samples", "skip_samples": 2048}
            ],
        },
    )
    audio_stream = cast(dict[str, object], cast(list[object], payload["streams"])[1])
    if sample_rate is None:
        audio_stream.pop("sample_rate")
    else:
        audio_stream["sample_rate"] = sample_rate
    provider, _ = _native_provider_with_ffprobe_json(payload, monkeypatch)
    media = tmp_path / "malformed rate.mp4"
    media.write_bytes(b"candidate")

    assert provider.measure(media, media.name, lambda: False).av_offset_seconds is None


def test_packet_timestamp_falls_back_to_dts_only_when_pts_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches treating malformed PTS as permission to substitute DTS."""
    valid, _ = _native_provider_with_ffprobe_json(
        _packet_payload(
            video_packet={"dts_time": "0.04"},
            audio_packet={"dts_time": "0.31"},
        ),
        monkeypatch,
    )
    malformed, _ = _native_provider_with_ffprobe_json(
        _packet_payload(
            video_packet={"pts_time": "invalid", "dts_time": "0.04"},
            audio_packet={"pts_time": "0.31"},
        ),
        monkeypatch,
    )
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"candidate")

    assert valid.measure(media, media.name, lambda: False).av_offset_seconds == (
        pytest.approx(0.27)
    )
    assert malformed.measure(media, media.name, lambda: False).av_offset_seconds is None


def test_packet_timestamp_uses_first_finite_non_negative_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches accepting negative or non-finite packet clock evidence."""
    provider, _ = _native_provider_with_ffprobe_json(
        {
            "streams": [
                {"index": 3, "codec_type": "video"},
                {"index": 7, "codec_type": "audio"},
            ],
            "packets": [
                {"stream_index": 3, "pts_time": "nan"},
                {"stream_index": 3, "pts_time": "-0.2"},
                {"stream_index": 7, "pts_time": "inf"},
                {"stream_index": 7, "pts_time": "0.31"},
                {"stream_index": 3, "pts_time": "0.04"},
            ],
        },
        monkeypatch,
    )
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, media.name, lambda: False)

    assert snapshot.av_offset_seconds == pytest.approx(0.27)


@pytest.mark.parametrize(
    ("packet_stream_index", "declared_video_index", "expected_status"),
    [
        (3.0, 3, RescueVerificationStatus.NEEDS_REVIEW),
        (True, 1, RescueVerificationStatus.NEEDS_REVIEW),
        (-1, 3, RescueVerificationStatus.NEEDS_REVIEW),
        (3, 3, RescueVerificationStatus.PASSED),
    ],
)
def test_packet_stream_index_requires_non_negative_non_boolean_integer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packet_stream_index: object,
    declared_video_index: int,
    expected_status: RescueVerificationStatus,
) -> None:
    """Catches Python equality accepting malformed packet stream indexes."""
    provider, _ = _native_provider_with_ffprobe_json(
        {
            "streams": [
                {"index": declared_video_index, "codec_type": "video"},
                {"index": 7, "codec_type": "audio"},
            ],
            "packets": [
                {"stream_index": 7, "pts_time": "0.31"},
                {"stream_index": packet_stream_index, "pts_time": "0.30"},
            ],
        },
        monkeypatch,
    )
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, media.name, lambda: False)
    report = _verify(
        tmp_path / "verification",
        faithful_updates={
            "av_offset_seconds": snapshot.av_offset_seconds,
            "av_offset_method": snapshot.av_offset_method,
            "av_offset_tool_version": snapshot.av_offset_tool_version,
        },
        actions=(_fixed_offset_action(),),
    )

    if expected_status is RescueVerificationStatus.PASSED:
        assert snapshot.av_offset_seconds == pytest.approx(0.01)
    else:
        assert snapshot.av_offset_seconds is None
    assert _check(report, "faithful", "fixed_av_offset").status is expected_status


@pytest.mark.parametrize(
    "payload",
    [
        _packet_payload(video_packet={"pts_time": "0.04"}, audio_packet=None),
        {
            "streams": [
                {"index": 3, "codec_type": "video"},
                {"index": 3, "codec_type": "audio"},
            ],
            "packets": [{"stream_index": 3, "pts_time": "0.04"}],
        },
        {"streams": "malformed", "packets": []},
    ],
)
def test_missing_or_ambiguous_packet_evidence_is_not_inferred(
    tmp_path: Path, payload: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches manufacturing an A/V pass from incomplete or ambiguous evidence."""
    provider, _ = _native_provider_with_ffprobe_json(payload, monkeypatch)
    media = tmp_path / "candidate.mp4"
    media.write_bytes(b"candidate")

    snapshot = provider.measure(media, media.name, lambda: False)

    assert snapshot.av_offset_seconds is None
    assert snapshot.av_offset_method is None
    assert snapshot.av_offset_tool_version is None


class _FakeMeasurementProvider:
    """Explicit test-only measurements; production never selects this provider."""

    def __init__(
        self,
        snapshots: dict[Path, MediaVerificationSnapshot],
        *,
        mapped_reference_updates: dict[str, object] | Exception | None = None,
        range_updates: dict[
            tuple[Path, tuple[tuple[float, float], ...]], dict[str, float] | Exception
        ]
        | None = None,
        audio_noise_updates: dict[Path, tuple[AudioNoiseInterval, ...]] | None = None,
        stabilization_measurement: dict[str, float] | None = None,
        perceptual_measurements: dict[
            tuple[RescueActionKind, Path], dict[str, float] | Exception
        ]
        | None = None,
        stabilization_freeze_measurement: dict[str, float] | Exception | None = None,
        sharpen_measurement: dict[str, JsonValue] | Exception | None = None,
        luma_measurement: dict[str, JsonValue] | Exception | None = None,
    ) -> None:
        self.snapshots = snapshots
        self.calls: list[tuple[Path, str]] = []
        self.mapped_reference_updates = mapped_reference_updates
        self.range_updates = range_updates or {}
        self.audio_noise_updates = audio_noise_updates or {}
        self.stabilization_measurement = stabilization_measurement
        self.perceptual_measurements = perceptual_measurements or {}
        self.stabilization_freeze_measurement = stabilization_freeze_measurement
        self.sharpen_measurement = sharpen_measurement
        self.luma_measurement = luma_measurement
        self.mapped_reference_calls: list[
            tuple[Path, tuple[SourceMapping, ...], ReferenceRenderOptions]
        ] = []
        self.range_calls: list[tuple[Path, tuple[tuple[float, float], ...]]] = []

    def measure(
        self,
        path: Path,
        relative_path: str,
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> MediaVerificationSnapshot:
        self.calls.append((path, relative_path))
        return replace(self.snapshots[path], path=path)

    def measure_mapped_reference(
        self,
        path: Path,
        mappings: tuple[SourceMapping, ...],
        render_mode: str,
        reference_options: ReferenceRenderOptions,
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> MediaVerificationSnapshot:
        del render_mode, cancellation_callback
        self.mapped_reference_calls.append((path, mappings, reference_options))
        if isinstance(self.mapped_reference_updates, Exception):
            raise self.mapped_reference_updates
        return cast(
            MediaVerificationSnapshot,
            cast(Any, replace)(
                self.snapshots[path],
                relative_path="mapped-source-reference",
                duration_seconds=sum(
                    mapping.source_end - mapping.source_start for mapping in mappings
                ),
                **(self.mapped_reference_updates or {}),
            ),
        )

    def measure_ranges(
        self,
        path: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> dict[str, float]:
        del cancellation_callback
        self.range_calls.append((path, ranges))
        result = self.range_updates.get(
            (path, ranges),
            {
                "luma_p10": 0.1,
                "luma_p50": 0.1,
                "clipping_ratio": 0.0,
                "noise_residual": 0.01,
                "sharpness": 0.01,
                "black_events": 0.0,
                "freeze_events": 0.0,
                "flicker_events": 0.0,
            },
        )
        if isinstance(result, Exception):
            raise result
        return result

    def compare_ranges(
        self,
        reference: Path,
        candidate: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> dict[str, float]:
        del reference, candidate, ranges, cancellation_callback
        return {
            "mean_absolute_pixel_difference": 0.0,
            "p95_frame_difference": 0.0,
            "compared_frames": 1.0,
        }

    def measure_audio_noise(
        self,
        path: Path,
        config: Any,
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> tuple[AudioNoiseInterval, ...]:
        del config, cancellation_callback
        return self.audio_noise_updates.get(path, ())

    def measure_stabilization(
        self,
        reference: Path,
        candidate: Path,
        ranges: tuple[tuple[float, float], ...],
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> dict[str, float]:
        del reference, candidate, ranges, cancellation_callback
        if self.stabilization_measurement is None:
            raise ValueError("stabilization measurement unavailable")
        return self.stabilization_measurement

    def measure_perceptual_restoration(
        self,
        kind: RescueActionKind,
        source: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool] = lambda: False,
        *,
        boundary_reference: Path | None = None,
    ) -> dict[str, float]:
        del (
            source,
            source_ranges,
            output_ranges,
            parameters,
            cancellation_callback,
            boundary_reference,
        )
        result = self.perceptual_measurements.get((kind, candidate))
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise ValueError("perceptual measurement unavailable")
        return result

    def inspect_tonal_audio_topology(
        self, path: Path, cancellation_callback: Callable[[], bool]
    ) -> dict[str, JsonValue]:
        del path, cancellation_callback
        return _tonal_audio_topology().model_dump(mode="json")

    def inspect_tonal_audio_timeline(
        self, path: Path, cancellation_callback: Callable[[], bool]
    ) -> dict[str, JsonValue]:
        del path, cancellation_callback
        return _tonal_audio_timeline().model_dump(mode="json")

    def measure_stabilization_freeze_attribution(
        self,
        source: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> dict[str, float]:
        del (
            source,
            candidate,
            source_ranges,
            output_ranges,
            parameters,
            cancellation_callback,
        )
        if isinstance(self.stabilization_freeze_measurement, Exception):
            raise self.stabilization_freeze_measurement
        if self.stabilization_freeze_measurement is None:
            raise ValueError("stabilization freeze measurement unavailable")
        return self.stabilization_freeze_measurement

    def measure_stabilization_freeze_attribution_with_control(
        self,
        source: Path,
        parent: Path,
        identity_control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> dict[str, JsonValue]:
        result = self.measure_stabilization_freeze_attribution(
            source,
            candidate,
            source_ranges,
            output_ranges,
            parameters,
            cancellation_callback,
        )
        assert parent.is_file() and identity_control.is_file()
        return {
            **result,
            "control_normalized_pts_digest": "d" * 64,
            "control_stream_topology_digest": "e" * 64,
            "control_frame_count": 120,
            "parent_normalized_pts_digest": "d" * 64,
            "parent_stream_topology_digest": "e" * 64,
            "parent_frame_count": 120,
            "candidate_normalized_pts_digest": "d" * 64,
            "candidate_stream_topology_digest": "e" * 64,
            "candidate_frame_count": 120,
        }

    def measure_sharpen_improvement(
        self,
        source: Path,
        control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> dict[str, JsonValue]:
        del (
            source,
            control,
            candidate,
            source_ranges,
            output_ranges,
            parameters,
            cancellation_callback,
        )
        raise AssertionError("legacy raw-source SHARPEN provider must not be consumed")

    def measure_sharpen_qualification(
        self,
        baseline: Path,
        visibility_control: Path,
        candidate: Path,
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> dict[str, JsonValue]:
        del output_ranges, parameters, cancellation_callback
        if isinstance(self.sharpen_measurement, Exception):
            raise self.sharpen_measurement
        if self.sharpen_measurement is None:
            raise ValueError("sharpen measurement unavailable")
        result = dict(self.sharpen_measurement)
        result.setdefault("minimum_recovered_baseline_ratio", 1.0)
        result.setdefault("maximum_edge_overshoot_amplitude", 0.0)
        result.setdefault(
            "control_sha256", sha256(visibility_control.read_bytes()).hexdigest()
        )
        result.setdefault(
            "candidate_sha256", sha256(candidate.read_bytes()).hexdigest()
        )
        result.setdefault("control_topology_sha256", "a" * 64)
        result.setdefault("candidate_topology_sha256", "a" * 64)
        result.setdefault("baseline_sha256", sha256(baseline.read_bytes()).hexdigest())
        result.setdefault("baseline_topology_sha256", "a" * 64)
        result.setdefault("normalized_pts_digest", "d" * 64)
        result.setdefault("inventory_frame_count", 1008)
        return result

    def measure_luma_adjustment(
        self,
        source: Path,
        control: Path,
        candidate: Path,
        source_ranges: tuple[tuple[float, float], ...],
        output_ranges: tuple[tuple[float, float], ...],
        parameters: dict[str, JsonValue],
        cancellation_callback: Callable[[], bool] = lambda: False,
    ) -> dict[str, JsonValue]:
        del source, source_ranges, parameters, cancellation_callback
        if isinstance(self.luma_measurement, Exception):
            raise self.luma_measurement
        if self.luma_measurement is not None:
            result = dict(self.luma_measurement)
        else:
            before = self.measure_ranges(control, output_ranges)
            after = self.measure_ranges(candidate, output_ranges)
            luma_delta = after["luma_p50"] - before["luma_p50"]
            result = {
                "range_coverage_ratio": 1.0,
                "expected_frames": 1.0,
                "compared_frames": 1.0,
                "range_count": float(len(output_ranges)),
                "minimum_luma_delta": luma_delta,
                "maximum_luma_delta": luma_delta,
                "maximum_noise_increase": (
                    after["noise_residual"] - before["noise_residual"]
                ),
                "maximum_clipping_increase": (
                    after["clipping_ratio"] - before["clipping_ratio"]
                ),
                "maximum_chroma_shift": 0.0,
                "maximum_source_control_chroma_shift": 0.0,
            }
        result.setdefault("control_sha256", sha256(control.read_bytes()).hexdigest())
        result.setdefault(
            "candidate_sha256", sha256(candidate.read_bytes()).hexdigest()
        )
        result.setdefault("control_topology_sha256", "a" * 64)
        result.setdefault("candidate_topology_sha256", "a" * 64)
        return result


def _action(kind: RescueActionKind, parameters: dict[str, object]) -> RescueAction:
    source_ranges = ((0.0, 4.0),)
    if kind is RescueActionKind.ADJUST_LUMA:
        luma = LumaAdjustmentConfig()
        metrics = VisualMetrics(
            luma_p10=0.05,
            luma_p50=0.10,
            luma_p90=0.30,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.03,
            sharpness=0.02,
        )
        parameters = {
            **derive_visual_action_parameters(
                RescueActionKind.ADJUST_LUMA,
                metrics,
                luma_config=luma,
            ),
            "strength_limit": 1.0,
            "assessment_metrics": metrics.model_dump(mode="json"),
            "assessment_evidence": [
                VisualEvidence(
                    action=RescueActionKind.ADJUST_LUMA,
                    timestamp_seconds=(start + end) / 2.0,
                    metric="luma_p10",
                    observed=metrics.luma_p10,
                    threshold=luma.dark_percentile_threshold,
                    context_luma_p50=metrics.luma_p50,
                ).model_dump(mode="json")
                for start, end in source_ranges
            ],
            "assessment_limitations": [],
        }
    if kind is RescueActionKind.SHARPEN:
        sharpen = SharpenConfig()
        parameters = {
            "minimum_perceptible_sharpness_gain_ratio": (
                sharpen.minimum_perceptible_sharpness_gain_ratio
            ),
            "minimum_recovered_baseline_ratio": (
                sharpen.minimum_recovered_baseline_ratio
            ),
            "minimum_improved_frame_fraction": (
                sharpen.minimum_improved_frame_fraction
            ),
            "scene_baseline_sharpness": 0.05,
            "maximum_noise_increase": sharpen.maximum_noise_increase,
            "edge_gradient_threshold": sharpen.edge_gradient_threshold,
            "edge_neighborhood_radius": sharpen.edge_neighborhood_radius,
            "edge_overshoot_minimum_amplitude": (
                sharpen.edge_overshoot_minimum_amplitude
            ),
            "maximum_edge_overshoot_ratio": (sharpen.maximum_edge_overshoot_ratio),
            "maximum_edge_overshoot_amplitude": (
                sharpen.maximum_edge_overshoot_amplitude
            ),
            "ringing_minimum_amplitude": sharpen.ringing_minimum_amplitude,
            "maximum_ringing_ratio": sharpen.maximum_ringing_ratio,
            **parameters,
        }
    return RescueAction(
        id=f"action_{kind.value}",
        version="1",
        kind=kind,
        description="Measured local operation.",
        source_ranges=source_ranges,
        parameters=cast(dict[str, JsonValue], parameters),
        changes_content=kind is not RescueActionKind.REMUX,
        requires_confirmation=kind is not RescueActionKind.REMUX,
        strategy=RescueStrategy.BALANCED,
    )


def _bind_content_action(
    action: RescueAction, effective_config: RescueEffectiveConfig
) -> RescueAction:
    contract = canonical_video_encode_contract(effective_config).model_dump(mode="json")
    parameters: dict[str, JsonValue] = {
        **action.parameters,
        "video_encode_contract": contract,
    }
    return RescueAction(
        id=make_rescue_action_id(
            kind=action.kind,
            parameters=parameters,
            source_ranges=action.source_ranges,
            strategy=action.strategy,
            version=action.version,
        ),
        version=action.version,
        kind=action.kind,
        description=action.description,
        source_ranges=action.source_ranges,
        parameters=parameters,
        changes_content=True,
        requires_confirmation=True,
        strategy=action.strategy,
    )


def _tonal_audio_topology() -> TonalAudioTopologyV2:
    raw = {
        "codec_name": "aac",
        "codec_tag_string": "mp4a",
        "profile": "LC",
        "sample_fmt": "fltp",
        "sample_rate_hz": 48000,
        "channels": 2,
        "channel_layout": "stereo",
        "time_base": "1/48000",
    }
    digest = sha256(
        json.dumps(raw, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    return TonalAudioTopologyV2.model_validate({**raw, "topology_sha256": digest})


def _tonal_audio_timeline() -> TonalAudioTimelineV1:
    tokens = ["0", "0.021333333", "0.042666667"]
    return TonalAudioTimelineV1(
        packet_count=3,
        first_normalized_pts_seconds=0.0,
        last_normalized_pts_seconds=0.042666667,
        normalized_pts_sha256=sha256(
            json.dumps(tokens, separators=(",", ":")).encode("ascii")
        ).hexdigest(),
    )


def _tonal_metrics(reduction_db: float) -> TonalEncodedMetricsV2:
    return TonalEncodedMetricsV2(
        range_coverage_ratio=1.0,
        measured_windows=80,
        excluded_transition_windows=0,
        minimum_target_reduction_db=reduction_db,
        minimum_target_margin_db=reduction_db - 24.0,
        maximum_non_target_attenuation_db=0.1,
        maximum_boundary_energy_jump_db=0.1,
        maximum_boundary_crest_jump_db=1.0,
        maximum_boundary_adjacent_delta=0.01,
    )


def _qualify_tonal_test_action(
    draft: RescueAction,
    input_hash: str,
    effective_config: RescueEffectiveConfig,
) -> RescueAction:
    config = TonalInterferenceConfig.model_validate_json(
        json.dumps(draft.parameters["config"])
    )
    raw_profile = InterferenceTone.model_validate_json(
        json.dumps(cast(list[JsonValue], draft.parameters["interference_profiles"])[0])
    )
    thresholds = TonalEncodedThresholdsV2(
        minimum_target_reduction_db=raw_profile.attenuation_target_db,
        maximum_non_target_attenuation_db=config.max_non_target_band_attenuation_db,
        maximum_boundary_energy_jump_db=config.max_boundary_energy_jump_db,
        maximum_boundary_crest_jump_db=config.max_boundary_crest_jump_db,
        maximum_boundary_adjacent_delta=config.max_boundary_adjacent_delta,
    )
    topology = _tonal_audio_topology()
    timeline = _tonal_audio_timeline()
    attempts = tuple(
        TonalEncodedCandidateAttemptV2(
            notch_q=notch_q,
            candidate_sha256=sha256(f"attempt-{notch_q}".encode("ascii")).hexdigest(),
            candidate_audio_topology=topology,
            metrics=_tonal_metrics(reduction_db),
            thresholds=thresholds,
        )
        for notch_q, reduction_db in ((18.0, 23.0), (12.0, 23.0), (8.0, 25.0))
    )
    selected_profile = raw_profile.model_copy(
        update={
            "render_qualification": TonalRenderQualification(
                boundary_mode="full_interval_v1",
                notch_q=8.0,
                complete_window_count=80,
                minimum_target_reduction_db=25.0,
                maximum_non_target_attenuation_db=0.1,
                maximum_boundary_energy_jump_db=0.1,
                maximum_boundary_crest_jump_db=1.0,
                maximum_boundary_adjacent_delta=0.01,
            )
        }
    )
    evidence = TonalEncodedQualificationEvidenceV3(
        input_hash=input_hash,
        draft_action_id=draft.id,
        draft_parameters=draft.parameters,
        source_ranges=draft.source_ranges,
        output_ranges=draft.source_ranges,
        range_mappings=(
            TonalRangeMappingV2(
                source_start=0.0,
                source_end=4.0,
                output_start=0.0,
                output_end=4.0,
            ),
        ),
        audio_encode_contract=TonalAudioEncodeContractV2(
            parent_bitrate_kbps=effective_config.improved_audio_bitrate_kbps,
            candidate_bitrate_kbps=config.audio_bitrate_kbps,
        ),
        parent_sha256="2" * 64,
        parent_audio_topology=topology,
        boundary_control_sha256=sha256(b"tonal-control").hexdigest(),
        boundary_control_audio_topology=topology,
        boundary_control_audio_timeline=timeline,
        profile_candidate_audio_timelines=((timeline, timeline, timeline),),
        combined_audio_timeline=timeline,
        profile_qualifications=(
            TonalEncodedProfileQualificationV2(
                profile_index=0,
                attempts=attempts,
                selected_notch_q=8.0,
            ),
        ),
        combined_candidate_sha256=sha256(b"faithful").hexdigest(),
        combined_audio_topology=topology,
        combined_metrics=(_tonal_metrics(25.0),),
        combined_thresholds=(thresholds,),
        selected_profiles=(selected_profile,),
    )
    parameters = qualified_tonal_action_parameters(evidence)
    return draft.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=draft.kind,
                parameters=parameters,
                source_ranges=draft.source_ranges,
                strategy=draft.strategy,
                version=draft.version,
            ),
        }
    )


def _plan(input_hash: str, *extra_actions: RescueAction) -> RescuePlan:
    effective_config = RescueEffectiveConfig()
    bound_actions = tuple(
        _qualify_tonal_test_action(bound, input_hash, effective_config)
        if bound.kind is RescueActionKind.DENOISE_AUDIO
        and bound.parameters.get("interference_profiles")
        else bound
        for bound in (
            _bind_content_action(action, effective_config) for action in extra_actions
        )
    )
    actions = (_action(RescueActionKind.REMUX, {}), *bound_actions)
    payload: dict[str, JsonValue] = {
        "input_hash": input_hash,
        "strategy": RescueStrategy.BALANCED,
        "effective_config": effective_config.model_dump(mode="json"),
        "actions": [action.model_dump(mode="json") for action in actions],
        "preview_ranges": [[0.0, 4.0]],
        "private_artifacts": [],
        "public_artifacts": ["faithful-rescue.mp4"],
        "damage_intervals": [],
    }
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    if any(action.kind is RescueActionKind.SHARPEN for action in bound_actions):
        # Verification-core tests intentionally construct an internal candidate
        # draft. Public model/command/preview/executor boundaries reject it.
        return RescuePlan.model_construct(
            input_hash=input_hash,
            strategy=RescueStrategy.BALANCED,
            effective_config=effective_config,
            actions=actions,
            preview_ranges=((0.0, 4.0),),
            public_artifacts=("faithful-rescue.mp4",),
            damage_intervals=(),
            plan_digest=cast(str, payload["plan_digest"]),
        )
    return RescuePlan.model_validate(payload)


_EXACT_FINAL_SHARPEN_METRICS: dict[str, JsonValue] = {
    "range_coverage_ratio": 1.0,
    "expected_frames": 96,
    "compared_frames": 96,
    "range_count": 1,
    "passing_range_count": 1,
    "minimum_aggregate_gain_ratio": 0.1,
    "minimum_recovered_baseline_ratio": 1.0,
    "minimum_improved_frame_fraction": 1.0,
    "maximum_noise_increase": 0.0,
    "maximum_edge_overshoot_ratio": 0.0,
    "maximum_edge_overshoot_amplitude": 0.0,
    "maximum_ringing_ratio": 0.0,
}


def _qualified_sharpen_plan(
    input_hash: str,
    *,
    selected_identity_updates: dict[str, JsonValue] | None = None,
    selected_metric_updates: dict[str, JsonValue] | None = None,
) -> RescuePlan:
    """Build a strict qualified action whose selected wire mirrors fake runtime."""
    config = RescueEffectiveConfig()
    draft = _bind_content_action(
        _action(
            RescueActionKind.SHARPEN,
            {
                "radius": 2,
                "adaptive_strength": 0.32,
                "amount": 0.8,
                "detail_passes": 3,
                "visibility_brightness": 0.12,
                "boundary_transition_seconds": 0.2,
            },
        ),
        config,
    )
    thresholds = SharpenQualificationThresholdsV1(
        minimum_aggregate_gain_ratio=float(
            cast(float, draft.parameters["minimum_perceptible_sharpness_gain_ratio"])
        ),
        minimum_recovered_baseline_ratio=float(
            cast(float, draft.parameters["minimum_recovered_baseline_ratio"])
        ),
        minimum_improved_frame_fraction=float(
            cast(float, draft.parameters["minimum_improved_frame_fraction"])
        ),
        maximum_noise_increase=float(
            cast(float, draft.parameters["maximum_noise_increase"])
        ),
        maximum_edge_overshoot_ratio=float(
            cast(float, draft.parameters["maximum_edge_overshoot_ratio"])
        ),
        maximum_edge_overshoot_amplitude=float(
            cast(float, draft.parameters["maximum_edge_overshoot_amplitude"])
        ),
        maximum_ringing_ratio=float(
            cast(float, draft.parameters["maximum_ringing_ratio"])
        ),
    )
    identity_updates = selected_identity_updates or {}
    shared_keys = {
        "baseline_sha256",
        "normalized_pts_digest",
        "stream_topology_digest",
        "inventory_frame_count",
    }
    measurements: list[SharpenProfileMeasurementV1] = []
    for index, profile in enumerate(config.sharpen_qualification_profiles):
        identities: dict[str, JsonValue] = {
            "baseline_sha256": sha256(b"sharpen-baseline").hexdigest(),
            "visibility_control_sha256": (
                sha256(b"sharpen-visibility").hexdigest()
                if index == 0
                else str(index) * 64
            ),
            "candidate_sha256": (
                sha256(b"improved").hexdigest() if index == 0 else str(index + 3) * 64
            ),
            "normalized_pts_digest": "d" * 64,
            "stream_topology_digest": "a" * 64,
            "inventory_frame_count": 1008,
        }
        identities.update(
            {
                key: value
                for key, value in identity_updates.items()
                if index == 0 or key in shared_keys
            }
        )
        metrics = {key: value for key, value in _EXACT_FINAL_SHARPEN_METRICS.items()}
        if index == 0 and selected_metric_updates:
            metrics.update(selected_metric_updates)
        measurements.append(
            SharpenProfileMeasurementV1(
                profile=profile,
                baseline_sha256=cast(str, identities["baseline_sha256"]),
                visibility_control_sha256=cast(
                    str, identities["visibility_control_sha256"]
                ),
                candidate_sha256=cast(str, identities["candidate_sha256"]),
                normalized_pts_digest=cast(str, identities["normalized_pts_digest"]),
                stream_topology_digest=cast(str, identities["stream_topology_digest"]),
                decoded_width=320,
                decoded_height=180,
                inventory_frame_count=cast(int, identities["inventory_frame_count"]),
                metrics=SharpenQualificationMetricsV1.model_validate(metrics),
                thresholds=thresholds,
            )
        )
    evidence = build_sharpen_qualification_evidence(
        input_hash=input_hash,
        draft_action_id=draft.id,
        draft_parameters=draft.parameters,
        source_ranges=draft.source_ranges,
        output_ranges=draft.source_ranges,
        encode_contract=canonical_video_encode_contract(config),
        configured_profiles=config.sharpen_qualification_profiles,
        measurements=measurements,
    )
    selected = evidence.selected
    assert selected is not None
    parameters = apply_qualified_sharpen_profile(draft.parameters, selected.profile)
    parameters.update(qualification_action_parameters(evidence))
    qualified = draft.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=draft.kind,
                parameters=parameters,
                source_ranges=draft.source_ranges,
                strategy=draft.strategy,
                version=draft.version,
            ),
        }
    )
    actions = (_action(RescueActionKind.REMUX, {}), qualified)
    payload: dict[str, JsonValue] = {
        "input_hash": input_hash,
        "strategy": RescueStrategy.BALANCED,
        "effective_config": config.model_dump(mode="json"),
        "actions": [action.model_dump(mode="json") for action in actions],
        "preview_ranges": [[0.0, 4.0]],
        "private_artifacts": [],
        "public_artifacts": ["faithful-rescue.mp4", "improved-viewing.mp4"],
        "damage_intervals": [],
    }
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _plan_with_tonal_selected_combined_metric_mismatch(input_hash: str) -> RescuePlan:
    plan = _plan(input_hash, _tonal_action())
    tonal = next(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.DENOISE_AUDIO
    )
    evidence = TonalEncodedQualificationEvidenceV3.model_validate(
        tonal.parameters["encoded_candidate_qualification"]
    )
    combined = evidence.combined_metrics[0].model_copy(
        update={"maximum_non_target_attenuation_db": 0.2}
    )
    profile = evidence.selected_profiles[0]
    render = profile.render_qualification
    assert render is not None
    selected_profile = profile.model_copy(
        update={
            "render_qualification": render.model_copy(
                update={"maximum_non_target_attenuation_db": 0.2}
            )
        }
    )
    tampered_evidence = evidence.model_copy(
        update={
            "combined_metrics": (combined,),
            "selected_profiles": (selected_profile,),
        }
    )
    parameters = qualified_tonal_action_parameters(tampered_evidence)
    tampered_action = tonal.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=tonal.kind,
                parameters=parameters,
                source_ranges=tonal.source_ranges,
                strategy=tonal.strategy,
                version=tonal.version,
            ),
        }
    )
    payload = plan.model_dump(mode="json", exclude={"plan_digest"})
    payload["actions"] = [
        (tampered_action if action is tonal else action).model_dump(mode="json")
        for action in plan.actions
    ]
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _unqualified_tonal_plan(input_hash: str) -> RescuePlan:
    effective_config = RescueEffectiveConfig()
    tonal = _bind_content_action(_tonal_action(), effective_config)
    actions = (_action(RescueActionKind.REMUX, {}), tonal)
    payload: dict[str, JsonValue] = {
        "input_hash": input_hash,
        "strategy": RescueStrategy.BALANCED,
        "effective_config": effective_config.model_dump(mode="json"),
        "actions": [action.model_dump(mode="json") for action in actions],
        "preview_ranges": [[0.0, 4.0]],
        "private_artifacts": [],
        "public_artifacts": ["faithful-rescue.mp4"],
        "damage_intervals": [],
    }
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_construct(
        input_hash=input_hash,
        strategy=RescueStrategy.BALANCED,
        effective_config=effective_config,
        actions=actions,
        preview_ranges=((0.0, 4.0),),
        public_artifacts=("faithful-rescue.mp4",),
        damage_intervals=(),
        plan_digest=cast(str, payload["plan_digest"]),
    )


def test_verifier_rejects_stale_plan_before_measuring_media(tmp_path: Path) -> None:
    """Verification must not turn a stale plan into runner-visible output."""
    valid_plan = _plan("a" * 64)
    plan = RescuePlan.model_construct(
        **{
            **{
                field_name: getattr(valid_plan, field_name)
                for field_name in RescuePlan.model_fields
            },
            "plan_digest": "b" * 64,
        }
    )

    class Provider:
        def measure(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("stale plan reached the media measurement provider")

    with pytest.raises(ValueError, match="plan digest"):
        RescueVerifier(measurement_provider=cast(Any, Provider())).verify(
            tmp_path / "source.mp4",
            tmp_path / "faithful.mp4",
            None,
            plan,
            (),
        )


def test_verifier_rejects_unqualified_sharpen_before_measuring_media(
    tmp_path: Path,
) -> None:
    plan = _plan("a" * 64, _action(RescueActionKind.SHARPEN, {}))

    class Provider:
        def measure(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("unqualified SHARPEN reached media measurement")

    with pytest.raises(ValueError, match="qualification is missing"):
        RescueVerifier(measurement_provider=cast(Any, Provider())).verify(
            tmp_path / "source.mp4",
            tmp_path / "faithful.mp4",
            tmp_path / "improved.mp4",
            plan,
            (SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
        )


def test_verifier_rejects_unqualified_tonal_before_measuring_media(
    tmp_path: Path,
) -> None:
    plan = _unqualified_tonal_plan("a" * 64)

    class Provider:
        def measure(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("unqualified tonal action reached media measurement")

    with pytest.raises(ValueError, match="encoded candidate qualification"):
        RescueVerifier(measurement_provider=cast(Any, Provider())).verify(
            tmp_path / "source.mp4",
            tmp_path / "faithful.mp4",
            None,
            plan,
            (SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
        )


def _plan_with_locked_ranges(
    input_hash: str,
    locked_ranges: tuple[tuple[float, float], ...],
    *extra_actions: RescueAction,
) -> RescuePlan:
    payload = _plan(input_hash, *extra_actions).model_dump(
        mode="json", exclude={"plan_digest"}
    )
    effective_config = cast(dict[str, JsonValue], payload["effective_config"])
    effective_config["locked_ranges"] = [list(item) for item in locked_ranges]
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    if any(
        cast(dict[str, JsonValue], raw)["kind"] == RescueActionKind.SHARPEN.value
        for raw in cast(list[JsonValue], payload["actions"])
    ):
        return RescuePlan.model_construct(
            input_hash=input_hash,
            strategy=RescueStrategy.BALANCED,
            effective_config=RescueEffectiveConfig.model_validate(effective_config),
            actions=tuple(
                RescueAction.model_validate(raw)
                for raw in cast(list[JsonValue], payload["actions"])
            ),
            preview_ranges=((0.0, 4.0),),
            public_artifacts=("faithful-rescue.mp4",),
            damage_intervals=(),
            plan_digest=cast(str, payload["plan_digest"]),
        )
    return RescuePlan.model_validate(payload)


def _two_segment_plan(input_hash: str, *mapped_actions: RescueAction) -> RescuePlan:
    """Build a valid partial-salvage plan for mapping-boundary tests."""
    damaged = DamageInterval(
        id=make_damage_id(input_hash, "video:0", DamageKind.UNDECODABLE, 1.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=1.0,
        end_seconds=2.0,
    )
    effective_config = RescueEffectiveConfig()
    content_actions = (
        _action(RescueActionKind.REMUX, {}),
        RescueAction(
            id="action_salvage_segments",
            version="1",
            kind=RescueActionKind.SALVAGE_SEGMENTS,
            description="Preserve the decodable source ranges.",
            source_ranges=((1.0, 2.0),),
            parameters={"damage_ids": [damaged.id]},
            changes_content=True,
            requires_confirmation=True,
            strategy=RescueStrategy.BALANCED,
        ),
        *mapped_actions,
    )
    actions = (
        content_actions[0],
        *(
            _bind_content_action(action, effective_config)
            for action in content_actions[1:]
        ),
    )
    payload: dict[str, JsonValue] = {
        "input_hash": input_hash,
        "strategy": RescueStrategy.BALANCED,
        "effective_config": effective_config.model_dump(mode="json"),
        "actions": [action.model_dump(mode="json") for action in actions],
        "preview_ranges": [[0.0, 4.0]],
        "private_artifacts": [],
        "public_artifacts": ["faithful-rescue.mp4"],
        "damage_intervals": [damaged.model_dump(mode="json")],
    }
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate(payload)


def _two_segment_fixed_offset_plan(input_hash: str) -> RescuePlan:
    return _two_segment_plan(input_hash, _fixed_offset_action())


def _two_segment_luma_plan(input_hash: str) -> RescuePlan:
    return _two_segment_plan(
        input_hash,
        _action(
            RescueActionKind.ADJUST_LUMA,
            {"brightness": 0.04, "contrast": 1.02},
        ),
    )


def _snapshot(path: Path, **updates: object) -> MediaVerificationSnapshot:
    values: dict[str, object] = {
        "path": path,
        "relative_path": path.name,
        "duration_seconds": 4.0,
        "video_stream_count": 1,
        "audio_stream_count": 1,
        "audio_sample_rate_hz": 48000,
        "complete_decode": True,
        "sha256": sha256(path.read_bytes()).hexdigest(),
    }
    values.update(updates)
    return MediaVerificationSnapshot(**values)  # type: ignore[arg-type]


def _verify(
    tmp_path: Path,
    *,
    faithful_updates: dict[str, object] | None = None,
    improved_updates: dict[str, object] | None = None,
    actions: tuple[RescueAction, ...] = (),
    mappings: tuple[SourceMapping, ...] | None = None,
    mapped_reference_updates: dict[str, object] | Exception | None = None,
    faithful_render_mode: Literal[
        "stream_copy", "single_reencode", "segment_concat_reencode"
    ] = "stream_copy",
    plan: RescuePlan | None = None,
    range_updates: dict[
        tuple[str, tuple[tuple[float, float], ...]], dict[str, float] | Exception
    ]
    | None = None,
    audio_noise_updates: dict[str, tuple[AudioNoiseInterval, ...]] | None = None,
    stabilization_measurement: dict[str, float] | None = None,
    perceptual_measurements: dict[
        tuple[str, RescueActionKind], dict[str, float] | Exception
    ]
    | None = None,
    stabilization_freeze_measurement: dict[str, float] | Exception | None = None,
    sharpen_measurement: dict[str, JsonValue] | Exception | None = None,
    luma_measurement: dict[str, JsonValue] | Exception | None = None,
    control_recipe_updates: dict[str, object] | None = None,
    sharpen_recipe_updates: dict[str, object] | None = None,
    measurement_providers: list[_FakeMeasurementProvider] | None = None,
) -> RescueVerificationReport:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"faithful")
    improved_path = None
    snapshots = {
        source: _snapshot(source),
        faithful: _snapshot(faithful, **(faithful_updates or {})),
    }
    if improved_updates is not None:
        improved_path = tmp_path / "improved-viewing.mp4"
        improved_path.write_bytes(b"improved")
        snapshots[improved_path] = _snapshot(improved_path, **improved_updates)
    resolved_range_updates = {
        (
            source
            if filename == "source.mp4"
            else faithful
            if filename == "faithful-rescue.mp4"
            else improved_path,
            ranges,
        ): values
        for (filename, ranges), values in (range_updates or {}).items()
    }
    resolved_audio_noise_updates = {
        (
            source
            if filename == "source.mp4"
            else faithful
            if filename == "faithful-rescue.mp4"
            else improved_path
        ): values
        for filename, values in (audio_noise_updates or {}).items()
    }
    resolved_perceptual_measurements = {
        (
            kind,
            source
            if filename == "source.mp4"
            else faithful
            if filename == "faithful-rescue.mp4"
            else improved_path,
        ): values
        for (filename, kind), values in (perceptual_measurements or {}).items()
    }
    resolved_plan = plan or _plan(sha256(source.read_bytes()).hexdigest(), *actions)
    controls: tuple[
        VerificationControlHandle
        | SharpenVerificationControlHandle
        | TonalVerificationControlHandle,
        ...,
    ] = ()
    stabilization = next(
        (
            action
            for action in resolved_plan.actions
            if action.kind is RescueActionKind.STABILIZE
        ),
        None,
    )
    if stabilization is not None:
        parent = tmp_path / "stabilization-parent.private.mp4"
        parent.write_bytes(b"identity-parent")
        control = tmp_path / "stabilization-identity-control.private.mp4"
        control.write_bytes(b"identity-control")
        recipe = VerificationControlRecipeV1(
            plan_digest=resolved_plan.plan_digest,
            action_id=stabilization.id,
            parent_sha256=sha256(parent.read_bytes()).hexdigest(),
            control_sha256=sha256(control.read_bytes()).hexdigest(),
            candidate_sha256=sha256(faithful.read_bytes()).hexdigest(),
            encode_contract=canonical_video_encode_contract(
                resolved_plan.effective_config
            ),
            normalized_pts_digest="d" * 64,
            stream_topology_digest="e" * 64,
            parent_normalized_pts_digest="d" * 64,
            parent_stream_topology_digest="e" * 64,
            candidate_normalized_pts_digest="d" * 64,
            candidate_stream_topology_digest="e" * 64,
            source_ranges=stabilization.source_ranges,
            frame_count=120,
            parent_frame_count=120,
            candidate_frame_count=120,
        )
        if control_recipe_updates:
            recipe = recipe.model_copy(update=control_recipe_updates)
        controls = (
            VerificationControlHandle(path=control, parent_path=parent, recipe=recipe),
        )
    sharpen = next(
        (
            action
            for action in resolved_plan.actions
            if action.kind is RescueActionKind.SHARPEN
        ),
        None,
    )
    if sharpen is not None and improved_path is not None:
        baseline = tmp_path / "sharpen-baseline.private.mp4"
        visibility = tmp_path / "sharpen-visibility.private.mp4"
        baseline.write_bytes(b"sharpen-baseline")
        visibility.write_bytes(b"sharpen-visibility")
        sharpen_recipe = SharpenVerificationControlRecipeV1(
            plan_digest=resolved_plan.plan_digest,
            action_id=sharpen.id,
            baseline_sha256=sha256(baseline.read_bytes()).hexdigest(),
            visibility_control_sha256=sha256(visibility.read_bytes()).hexdigest(),
            candidate_sha256=sha256(improved_path.read_bytes()).hexdigest(),
            encode_contract=canonical_video_encode_contract(
                resolved_plan.effective_config
            ),
            normalized_pts_digest="d" * 64,
            stream_topology_digest="a" * 64,
            source_ranges=sharpen.source_ranges,
            output_ranges=sharpen.source_ranges,
            inventory_frame_count=1008,
        )
        if sharpen_recipe_updates:
            sharpen_recipe = sharpen_recipe.model_copy(update=sharpen_recipe_updates)
        controls += (
            SharpenVerificationControlHandle(
                baseline_path=baseline,
                visibility_path=visibility,
                recipe=sharpen_recipe,
            ),
        )
    tonal = next(
        (
            action
            for action in resolved_plan.actions
            if action.kind is RescueActionKind.DENOISE_AUDIO
            and action.parameters.get("encoded_qualification_version") == "3"
        ),
        None,
    )
    if tonal is not None:
        evidence = TonalEncodedQualificationEvidenceV3.model_validate(
            tonal.parameters["encoded_candidate_qualification"]
        )
        control = tmp_path / "tonal-control.private.mp4"
        control.write_bytes(b"tonal-control")
        controls += (
            TonalVerificationControlHandle(
                path=control,
                recipe=TonalVerificationControlRecipeV1(
                    plan_digest=resolved_plan.plan_digest,
                    action_id=tonal.id,
                    parent_sha256=evidence.parent_sha256,
                    control_sha256=evidence.boundary_control_sha256,
                    qualified_candidate_sha256=cast(
                        str, evidence.combined_candidate_sha256
                    ),
                    source_ranges=tonal.source_ranges,
                    output_ranges=evidence.output_ranges,
                    encode_contract=evidence.audio_encode_contract.model_dump(
                        mode="json"
                    ),
                    control_audio_topology=(
                        evidence.boundary_control_audio_topology.model_dump(mode="json")
                    ),
                    candidate_audio_topology=cast(
                        TonalAudioTopologyV2, evidence.combined_audio_topology
                    ).model_dump(mode="json"),
                    control_audio_timeline=(
                        evidence.boundary_control_audio_timeline.model_dump(mode="json")
                    ),
                    candidate_audio_timeline=cast(
                        TonalAudioTimelineV1, evidence.combined_audio_timeline
                    ).model_dump(mode="json"),
                ),
            ),
        )
    provider = _FakeMeasurementProvider(
        snapshots,
        mapped_reference_updates=mapped_reference_updates,
        range_updates=cast(Any, resolved_range_updates),
        audio_noise_updates=cast(Any, resolved_audio_noise_updates),
        stabilization_measurement=stabilization_measurement,
        perceptual_measurements=cast(Any, resolved_perceptual_measurements),
        stabilization_freeze_measurement=stabilization_freeze_measurement,
        sharpen_measurement=sharpen_measurement,
        luma_measurement=luma_measurement,
    )
    if measurement_providers is not None:
        measurement_providers.append(provider)
    return RescueVerifier(measurement_provider=provider).verify(
        source=source,
        faithful=faithful,
        improved=improved_path,
        plan=resolved_plan,
        mappings=mappings
        or (SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
        faithful_render_mode=faithful_render_mode,
        verification_controls=controls,
        _allow_unqualified_sharpen_draft=True,
    )


def _check(
    report: RescueVerificationReport,
    artifact: Literal["faithful", "improved"],
    check_id: str,
) -> RescueVerificationCheck:
    return next(
        check
        for check in report.checks
        if check.artifact == artifact and check.check_id == check_id
    )


def _json_number(value: JsonValue) -> float:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _deblur_action() -> RescueAction:
    config = DeblurConfig()
    estimate = BlurKernelEstimate(
        kernel_kind="box",
        radius=3,
        regularization=0.01,
        confidence=0.95,
        edge_width_before=5.0,
        predicted_edge_width_after=2.5,
        edge_continuity_ratio=0.95,
        reblur_error_ratio=0.02,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )
    return _action(
        RescueActionKind.DEBLUR,
        {
            "algorithm_version": "1",
            "operations": [
                {
                    "source_ranges": [[0.0, 4.0]],
                    "estimate": estimate.model_dump(mode="json"),
                    "config": config.model_dump(mode="json"),
                }
            ],
        },
    )


def _two_operation_deblur_action() -> RescueAction:
    single = _deblur_action()
    raw_operations = single.parameters["operations"]
    assert isinstance(raw_operations, list) and len(raw_operations) == 1
    template = raw_operations[0]
    assert isinstance(template, dict)
    return _action(
        RescueActionKind.DEBLUR,
        {
            "algorithm_version": "1",
            "operations": [
                {**template, "source_ranges": [[0.0, 3.5]]},
                {**template, "source_ranges": [[3.5, 4.0]]},
            ],
        },
    )


def _tonal_action() -> RescueAction:
    config = TonalInterferenceConfig(window_seconds=0.2, hop_seconds=0.1)
    tone = InterferenceTone(
        start_seconds=0.0,
        end_seconds=4.0,
        center_frequency_hz=880.0,
        confidence=0.95,
        baseline_before_dbfs=-52.0,
        baseline_after_dbfs=-51.0,
        peak_dbfs=-14.0,
        local_peak_over_baseline_db=37.0,
        persistence_window_count=40,
        frequency_standard_deviation_hz=1.5,
        channel_indices=(0,),
        attenuation_target_db=config.attenuation_db,
        render_qualification=TonalRenderQualification(
            boundary_mode="full_interval_v1",
            notch_q=8.0,
            complete_window_count=80,
            minimum_target_reduction_db=25.0,
            maximum_non_target_attenuation_db=0.1,
            maximum_boundary_energy_jump_db=0.1,
            maximum_boundary_crest_jump_db=0.1,
            maximum_boundary_adjacent_delta=0.01,
        ),
    )
    return _action(
        RescueActionKind.DENOISE_AUDIO,
        {
            "algorithm_version": "1",
            "interference_profiles": [tone.model_dump(mode="json")],
            "config": config.model_dump(mode="json"),
        },
    )


def _tonal_measurement(**updates: float) -> dict[str, float]:
    measured = {
        "range_coverage_ratio": 1.0,
        "measured_windows": 80.0,
        "excluded_transition_windows": 0.0,
        "minimum_target_reduction_db": 25.0,
        "minimum_target_margin_db": 1.0,
        "maximum_non_target_attenuation_db": 0.1,
        "maximum_boundary_energy_jump_db": 0.1,
        "maximum_boundary_crest_jump_db": 1.0,
        "maximum_boundary_adjacent_delta": 0.01,
        "profile_0_range_coverage_ratio": 1.0,
        "profile_0_measured_windows": 80.0,
        "profile_0_excluded_transition_windows": 0.0,
        "profile_0_minimum_target_reduction_db": 25.0,
        "profile_0_minimum_target_margin_db": 1.0,
        "profile_0_maximum_non_target_attenuation_db": 0.1,
        "profile_0_maximum_boundary_energy_jump_db": 0.1,
        "profile_0_maximum_boundary_crest_jump_db": 1.0,
        "profile_0_maximum_boundary_adjacent_delta": 0.01,
    }
    measured.update(updates)
    return measured


def _anchor_action(
    method: str = "anchor_v1", *, discontinuous_boundary: bool = False
) -> RescueAction:
    config = StabilizationConfig(accepted_ranges=((0.0, 4.0),))
    timestamps = (
        tuple(index / 24.0 for index in range(96))
        if method == "transition_anchor_v1"
        else (0.0, 2.0)
    )
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            translation_x=(
                50.0
                if discontinuous_boundary and index == 23
                else -50.0
                if discontinuous_boundary and index == 24
                else 0.0
            ),
            translation_y=0.0,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.1,
            scene_boundary=False,
            semantics="frame_correction",
        )
        for index, timestamp in enumerate(timestamps)
    )
    parameters: dict[str, object] = {
        "method": method,
        "algorithm_version": "1",
        "motion_transforms": [item.model_dump(mode="json") for item in transforms],
        "config": config.model_dump(mode="json"),
    }
    if method == "transition_anchor_v1":
        parameters.update(
            {
                "estimator_algorithm_version": "transition_anchor_v1",
                "transition_range": [0.0, 1.0],
                "following_anchor_range": [1.0, 4.0],
                "transition_correction_count": len(transforms),
            }
        )
    return _action(
        RescueActionKind.STABILIZE,
        parameters,
    )


def test_deblur_rejects_high_apparent_sharpness_with_visible_halo(
    tmp_path: Path,
) -> None:
    """High-frequency gain cannot hide independently measured ringing."""
    report = _verify(
        tmp_path,
        actions=(_deblur_action(),),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.DEBLUR): {
                "range_coverage_ratio": 1.0,
                "compared_frames": 24.0,
                "operation_count": 1.0,
                "edge_recovery_passed_operations": 1.0,
                "ringing_passed_operations": 0.0,
                "temporal_passed_operations": 1.0,
                "edge_width_ratio": 0.5,
                "edge_continuity_ratio": 0.95,
                "ringing_ratio": 0.2,
                "noise_gain_ratio": 1.1,
                "temporal_change_ratio": 0.02,
            }
        },
    )

    assert _check(report, "faithful", "deblur_edge_recovery").status is (
        RescueVerificationStatus.PASSED
    )
    assert _check(report, "faithful", "deblur_ringing").status is (
        RescueVerificationStatus.NEEDS_REVIEW
    )
    assert _check(report, "faithful", "deblur_temporal_consistency").status is (
        RescueVerificationStatus.PASSED
    )
    assert report.faithful_status is RescueVerificationStatus.NEEDS_REVIEW


def test_deblur_short_failed_operation_cannot_hide_in_long_passing_operation(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        actions=(_two_operation_deblur_action(),),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.DEBLUR): {
                "range_coverage_ratio": 1.0,
                "compared_frames": 32.0,
                "operation_count": 2.0,
                "edge_recovery_passed_operations": 1.0,
                "ringing_passed_operations": 2.0,
                "temporal_passed_operations": 2.0,
                "edge_width_ratio": 0.6,
                "edge_continuity_ratio": 0.95,
                "ringing_ratio": 0.01,
                "noise_gain_ratio": 1.05,
                "temporal_change_ratio": 0.01,
            }
        },
    )

    assert _check(report, "faithful", "deblur_edge_recovery").status is (
        RescueVerificationStatus.NEEDS_REVIEW
    )


def test_tonal_reduction_uses_confirmed_24_db_restoration_target(
    tmp_path: Path,
) -> None:
    """One failing 50 ms boundary window remains a required failure."""
    report = _verify(
        tmp_path,
        actions=(_tonal_action(),),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.DENOISE_AUDIO): _tonal_measurement(
                minimum_target_reduction_db=20.0,
                minimum_target_margin_db=-4.0,
                maximum_boundary_adjacent_delta=0.5,
                profile_0_minimum_target_reduction_db=20.0,
                profile_0_maximum_boundary_adjacent_delta=0.5,
            )
        },
    )

    assert _check(report, "faithful", "tonal_interference_reduction").status is (
        RescueVerificationStatus.NEEDS_REVIEW
    )
    boundary = _check(report, "faithful", "tonal_boundary_transient")
    assert boundary.status is RescueVerificationStatus.NEEDS_REVIEW
    assert boundary.measured["maximum_boundary_adjacent_delta"] == pytest.approx(0.5)
    assert report.faithful_status is RescueVerificationStatus.NEEDS_REVIEW


def test_tonal_measurement_payload_preserves_measured_margin_and_threshold_round_trip(
    tmp_path: Path,
) -> None:
    """A same-named threshold must not overwrite the independently measured value."""
    report = _verify(
        tmp_path,
        actions=(_tonal_action(),),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.DENOISE_AUDIO): _tonal_measurement(
                minimum_target_reduction_db=20.0,
                minimum_target_margin_db=-4.0,
                profile_0_minimum_target_reduction_db=20.0,
            )
        },
    )

    reduction = _check(report, "faithful", "tonal_interference_reduction")
    assert reduction.status is RescueVerificationStatus.NEEDS_REVIEW
    assert reduction.measured["minimum_target_margin_db"] == pytest.approx(-4.0)
    assert reduction.measured["thresholds"] == {
        "minimum_target_margin_db": 0.0,
        "configured_maximum_attenuation_target_db": 24.0,
        "configured_maximum_non_target_attenuation_db": 0.25,
        "configured_maximum_boundary_energy_jump_db": 0.5,
        "configured_maximum_boundary_crest_jump_db": 3.0,
        "configured_maximum_boundary_adjacent_delta": 0.08,
    }

    round_tripped = RescueVerificationReport.model_validate_json(
        report.model_dump_json()
    )
    round_trip_reduction = _check(
        round_tripped, "faithful", "tonal_interference_reduction"
    )
    assert round_trip_reduction.measured["minimum_target_margin_db"] == pytest.approx(
        -4.0
    )
    assert (
        round_trip_reduction.measured["thresholds"] == reduction.measured["thresholds"]
    )


@pytest.mark.parametrize("method", ("anchor_v1", "transition_anchor_v1"))
def test_anchor_motion_reduction_does_not_hide_four_pixel_residual(
    tmp_path: Path,
    method: str,
) -> None:
    """A percentage reduction is insufficient when absolute residual is visible."""
    report = _verify(
        tmp_path,
        actions=(_anchor_action(method),),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.STABILIZE): {
                "range_coverage_ratio": 1.0,
                "expected_frames": 24.0,
                "reliable_transforms": 24.0,
                "residual_median_pixels": 4.0,
                "residual_p90_pixels": 5.0,
                "crop_ratio": 0.05,
            }
        },
    )

    check = _check(report, "faithful", "anchor_stabilization_residual")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    assert check.measured["residual_median_pixels"] == pytest.approx(4.0)
    assert report.faithful_status is RescueVerificationStatus.NEEDS_REVIEW


@pytest.mark.parametrize(
    ("transition_measurement", "review_check_ids"),
    [
        ({}, {"consensus", "seam", "coverage"}),
        (
            {
                "transition_consensus_coverage_ratio": 0.95,
                "transition_consensus_p90_pixels": 0.1,
                "transition_seam_residual_pixels": 0.1,
                "transition_boundary_source_translation_x": 0.0,
                "transition_boundary_source_translation_y": 0.0,
                "transition_expected_frames": 24.0,
                "transition_reliable_frames": 23.0,
            },
            {"consensus", "coverage"},
        ),
        (
            {
                "transition_consensus_coverage_ratio": 1.0,
                "transition_consensus_p90_pixels": 4.1,
                "transition_seam_residual_pixels": 0.3,
                "transition_boundary_source_translation_x": 0.0,
                "transition_boundary_source_translation_y": 0.0,
                "transition_expected_frames": 24.0,
                "transition_reliable_frames": 24.0,
            },
            {"consensus", "seam"},
        ),
    ],
)
def test_transition_anchor_missing_partial_or_over_threshold_evidence_requires_review(
    tmp_path: Path,
    transition_measurement: dict[str, float],
    review_check_ids: set[str],
) -> None:
    measured = {
        "range_coverage_ratio": 1.0,
        "expected_frames": 24.0,
        "reliable_transforms": 24.0,
        "residual_median_pixels": 0.1,
        "residual_p90_pixels": 0.2,
        "crop_ratio": 0.05,
        **transition_measurement,
    }
    report = _verify(
        tmp_path,
        actions=(_anchor_action("transition_anchor_v1"),),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.STABILIZE): measured
        },
    )

    for check_id in (
        "transition_stabilization_consensus",
        "transition_stabilization_seam",
        "transition_stabilization_coverage",
    ):
        expected_status = (
            RescueVerificationStatus.NEEDS_REVIEW
            if check_id.removeprefix("transition_stabilization_") in review_check_ids
            else RescueVerificationStatus.PASSED
        )
        assert _check(report, "faithful", check_id).status is expected_status
        assert check_id in report.required_check_ids


def test_transition_anchor_complete_independent_evidence_passes_required_checks(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        actions=(_anchor_action("transition_anchor_v1"),),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.STABILIZE): {
                "range_coverage_ratio": 1.0,
                "expected_frames": 24.0,
                "reliable_transforms": 24.0,
                "residual_median_pixels": 0.1,
                "residual_p90_pixels": 0.2,
                "crop_ratio": 0.05,
                "transition_consensus_coverage_ratio": 1.0,
                "transition_consensus_p90_pixels": 0.1,
                "transition_seam_residual_pixels": 0.1,
                "transition_boundary_source_translation_x": 0.0,
                "transition_boundary_source_translation_y": 0.0,
                "transition_expected_frames": 24.0,
                "transition_reliable_frames": 24.0,
            }
        },
    )

    for check_id in (
        "transition_stabilization_consensus",
        "transition_stabilization_seam",
        "transition_stabilization_coverage",
    ):
        assert _check(report, "faithful", check_id).status is (
            RescueVerificationStatus.PASSED
        )


def test_confirmed_stabilization_freezes_use_direct_outside_inventory(
    tmp_path: Path,
) -> None:
    action = _anchor_action("transition_anchor_v1")
    report = _verify(
        tmp_path,
        actions=(action,),
        faithful_updates={"freeze_events": 29},
        mapped_reference_updates={"freeze_events": 13},
        faithful_render_mode="single_reencode",
        stabilization_measurement={
            "crop_ratio": 0.01,
            "source_motion_median_pixels": 10.0,
            "output_motion_median_pixels": 1.0,
            "source_motion_p90_pixels": 12.0,
            "output_motion_p90_pixels": 1.5,
            "source_reliable_transforms": 20.0,
            "output_reliable_transforms": 20.0,
        },
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.STABILIZE): {
                "range_coverage_ratio": 1.0,
                "expected_frames": 96.0,
                "reliable_transforms": 96.0,
                "residual_median_pixels": 0.1,
                "residual_p90_pixels": 0.2,
                "crop_ratio": 0.01,
                "transition_consensus_coverage_ratio": 1.0,
                "transition_consensus_p90_pixels": 0.1,
                "transition_seam_residual_pixels": 0.1,
                "transition_boundary_source_translation_x": 0.0,
                "transition_boundary_source_translation_y": 0.0,
                "transition_expected_frames": 24.0,
                "transition_reliable_frames": 24.0,
            }
        },
        stabilization_freeze_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "source_freeze_events": 0.0,
            "candidate_freeze_events": 15.0,
            "attributed_candidate_freeze_events": 12.0,
            "explained_freeze_events": 12.0,
            "unexplained_near_static_pairs": 0.0,
            "exact_duplicate_pairs": 0.0,
            "maximum_candidate_expected_mae": 1.0,
            "outside_range_coverage_ratio": 1.0,
            "outside_expected_frames": 912.0,
            "outside_compared_frames": 912.0,
            "source_outside_freeze_events": 13.0,
            "candidate_outside_freeze_events": 13.0,
            "outside_exact_duplicate_pairs": 0.0,
        },
    )

    check = _check(report, "faithful", "freeze_regression")
    assert check.status is RescueVerificationStatus.PASSED
    assert check.measured["explained_freeze_events"] == pytest.approx(12.0)
    assert check.measured["source_outside_events"] == pytest.approx(13.0)
    assert check.measured["output_outside_events"] == pytest.approx(13.0)
    assert check.measured["reference"] == (
        "identity_generation_control_and_confirmed_affine_warp"
    )


@pytest.mark.parametrize(
    "recipe_updates",
    (
        {"plan_digest": "b" * 64},
        {"action_id": "different-action"},
        {"source_ranges": ((0.0, 0.5),)},
        {"normalized_pts_digest": "c" * 64},
        {"stream_topology_digest": "c" * 64},
        {"frame_count": 119},
        {"parent_sha256": "c" * 64},
        {"parent_normalized_pts_digest": "c" * 64},
        {"parent_stream_topology_digest": "c" * 64},
        {"parent_frame_count": 119},
    ),
)
def test_stabilization_control_recipe_tamper_fails_closed(
    tmp_path: Path, recipe_updates: dict[str, object]
) -> None:
    report = _verify(
        tmp_path,
        actions=(_anchor_action("transition_anchor_v1"),),
        stabilization_freeze_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "source_freeze_events": 0.0,
            "candidate_freeze_events": 0.0,
            "attributed_candidate_freeze_events": 0.0,
            "explained_freeze_events": 0.0,
            "unexplained_near_static_pairs": 0.0,
            "exact_duplicate_pairs": 0.0,
            "maximum_candidate_expected_mae": 0.0,
            "outside_range_coverage_ratio": 1.0,
            "outside_expected_frames": 24.0,
            "outside_compared_frames": 24.0,
            "source_outside_freeze_events": 0.0,
            "candidate_outside_freeze_events": 0.0,
            "outside_exact_duplicate_pairs": 0.0,
        },
        control_recipe_updates=recipe_updates,
    )
    check = _check(report, "faithful", "freeze_regression")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    assert "manual review is required" in check.message


@pytest.mark.parametrize(
    ("candidate_outside_events", "expected_status"),
    [
        (2.0, RescueVerificationStatus.PASSED),
        (3.0, RescueVerificationStatus.NEEDS_REVIEW),
    ],
)
def test_stabilization_direct_outside_inventory_keeps_codec_event_bound(
    candidate_outside_events: float,
    expected_status: RescueVerificationStatus,
) -> None:
    base_check = RescueVerificationCheck(
        check_id="freeze_regression",
        artifact="faithful",
        status=RescueVerificationStatus.PASSED,
        message="Measured global freeze inventory.",
        measured={
            "applicable": True,
            "source_events": 0.0,
            "output_events": candidate_outside_events,
            "codec_event_tolerance": 2.0,
        },
        required=False,
    )
    measured = {
        "control_recipe_valid": 1.0,
        "range_coverage_ratio": 1.0,
        "expected_frames": 96.0,
        "compared_frames": 96.0,
        "source_freeze_events": 0.0,
        "candidate_freeze_events": 12.0,
        "attributed_candidate_freeze_events": 12.0,
        "explained_freeze_events": 12.0,
        "unexplained_near_static_pairs": 0.0,
        "exact_duplicate_pairs": 0.0,
        "maximum_candidate_expected_mae": 1.0,
        "outside_range_coverage_ratio": 1.0,
        "outside_expected_frames": 912.0,
        "outside_compared_frames": 912.0,
        "source_outside_freeze_events": 0.0,
        "candidate_outside_freeze_events": candidate_outside_events,
        "outside_exact_duplicate_pairs": 0.0,
    }

    check = verification_module._stabilization_freeze_verification_check(
        "faithful", _anchor_action("transition_anchor_v1"), measured, base_check
    )

    assert check.status is expected_status


@pytest.mark.parametrize(
    "measurement_update",
    [
        {"exact_duplicate_pairs": 1.0},
        {"unexplained_near_static_pairs": 1.0},
        {"maximum_candidate_expected_mae": 4.1},
        {"attributed_candidate_freeze_events": 0.0},
        {"explained_freeze_events": 0.0},
        {"range_coverage_ratio": 0.99},
        {"compared_frames": 95.0},
        {"outside_exact_duplicate_pairs": 1.0},
        {"outside_range_coverage_ratio": 0.99},
        {"outside_compared_frames": 1.0},
    ],
)
def test_stabilization_freeze_attribution_integrity_fails_closed(
    tmp_path: Path,
    measurement_update: dict[str, float],
) -> None:
    measurement = {
        "range_coverage_ratio": 1.0,
        "expected_frames": 96.0,
        "compared_frames": 96.0,
        "source_freeze_events": 0.0,
        "candidate_freeze_events": 1.0,
        "attributed_candidate_freeze_events": 1.0,
        "explained_freeze_events": 1.0,
        "unexplained_near_static_pairs": 0.0,
        "exact_duplicate_pairs": 0.0,
        "maximum_candidate_expected_mae": 1.0,
        "outside_range_coverage_ratio": 1.0,
        "outside_expected_frames": 0.0,
        "outside_compared_frames": 0.0,
        "source_outside_freeze_events": 0.0,
        "candidate_outside_freeze_events": 0.0,
        "outside_exact_duplicate_pairs": 0.0,
        **measurement_update,
    }
    report = _verify(
        tmp_path,
        actions=(_anchor_action("transition_anchor_v1"),),
        faithful_updates={"freeze_events": 1},
        mapped_reference_updates={"freeze_events": 0},
        faithful_render_mode="single_reencode",
        stabilization_freeze_measurement=measurement,
    )

    assert _check(report, "faithful", "freeze_regression").status is (
        RescueVerificationStatus.NEEDS_REVIEW
    )


def test_stabilization_control_recipe_binds_actual_candidate_bytes(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        actions=(_anchor_action("transition_anchor_v1"),),
        faithful_updates={"freeze_events": 0},
        mapped_reference_updates={"freeze_events": 0},
        faithful_render_mode="single_reencode",
        stabilization_freeze_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "source_freeze_events": 0.0,
            "candidate_freeze_events": 0.0,
            "attributed_candidate_freeze_events": 0.0,
            "explained_freeze_events": 0.0,
            "unexplained_near_static_pairs": 0.0,
            "exact_duplicate_pairs": 0.0,
            "maximum_candidate_expected_mae": 0.0,
            "outside_range_coverage_ratio": 1.0,
            "outside_expected_frames": 0.0,
            "outside_compared_frames": 0.0,
            "source_outside_freeze_events": 0.0,
            "candidate_outside_freeze_events": 0.0,
            "outside_exact_duplicate_pairs": 0.0,
        },
        control_recipe_updates={"candidate_sha256": "0" * 64},
    )

    assert _check(report, "faithful", "freeze_regression").status is (
        RescueVerificationStatus.NEEDS_REVIEW
    )


def _stabilization_freeze_native_fixture(
    *,
    wrong_correction: bool = False,
    duplicate_candidate: bool = False,
    scaled_analysis: bool = False,
) -> tuple[
    dict[str, JsonValue],
    tuple[tuple[int, int, int, float, np.ndarray, np.ndarray], ...],
]:
    config = StabilizationConfig(
        frame_width=32 if scaled_analysis else 16,
        frame_height=32 if scaled_analysis else 16,
        minimum_motion_amplitude_pixels=64.0,
        accepted_ranges=((0.0, 2.0),),
    )
    first: NDArray[np.uint8] = np.zeros((16, 16), dtype=np.uint8)
    first[5:11, 4:8] = 120
    second = np.zeros_like(first)
    second[5:11, 6:10] = 120
    first_candidate = first.copy()
    second_candidate = first.copy()
    second_candidate[0, 0] = 1
    if duplicate_candidate:
        second_candidate = first_candidate.copy()
    corrections = (
        MotionTransform(
            timestamp_seconds=0.0,
            rotation_degrees=0.0,
            scale=1.0,
            translation_x=0.0,
            translation_y=0.0,
            inlier_ratio=1.0,
            residual_pixels=0.0,
            semantics="frame_correction",
        ),
        MotionTransform(
            timestamp_seconds=1.0,
            rotation_degrees=0.0,
            scale=1.0,
            translation_x=(
                0.0 if wrong_correction else (-4.0 if scaled_analysis else -2.0)
            ),
            translation_y=0.0,
            inlier_ratio=1.0,
            residual_pixels=0.0,
            semantics="frame_correction",
        ),
    )
    parameters: dict[str, JsonValue] = {
        "method": "anchor_v1",
        "algorithm_version": "1",
        "config": config.model_dump(mode="json"),
        "motion_transforms": [item.model_dump(mode="json") for item in corrections],
    }
    frames = (
        (0, 2, 0, 0.0, first, first_candidate),
        (0, 2, 1, 1.0, second, second_candidate),
    )
    return parameters, frames


def _patch_empty_outside_stabilization_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        verification_module,
        "_measure_outside_stabilization_freezes",
        lambda *_args, **_kwargs: {
            "outside_range_coverage_ratio": 1.0,
            "outside_expected_frames": 0.0,
            "outside_compared_frames": 0.0,
            "source_outside_freeze_events": 0.0,
            "candidate_outside_freeze_events": 0.0,
            "outside_exact_duplicate_pairs": 0.0,
        },
    )


def test_native_stabilization_freeze_attribution_explains_expected_near_static(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters, frames = _stabilization_freeze_native_fixture()
    _patch_empty_outside_stabilization_measurement(monkeypatch)
    monkeypatch.setattr(
        verification_module,
        "_iter_aligned_perceptual_frames",
        lambda *_args, **_kwargs: iter(frames),
    )

    measured = verification_module._measure_stabilization_freeze_attribution(
        Path("source.mp4"),
        Path("candidate.mp4"),
        ((0.0, 2.0),),
        ((0.0, 2.0),),
        parameters,
        "ffprobe",
        cast(Any, None),
        1.0,
        lambda: False,
    )

    assert measured["candidate_freeze_events"] == pytest.approx(1.0)
    assert measured["explained_freeze_events"] == pytest.approx(1.0)
    assert measured["unexplained_near_static_pairs"] == pytest.approx(0.0)
    assert measured["exact_duplicate_pairs"] == pytest.approx(0.0)


def test_native_stabilization_freeze_scales_confirmed_affine_to_analysis_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters, frames = _stabilization_freeze_native_fixture(scaled_analysis=True)
    _patch_empty_outside_stabilization_measurement(monkeypatch)
    monkeypatch.setattr(
        verification_module,
        "_iter_aligned_perceptual_frames",
        lambda *_args, **_kwargs: iter(frames),
    )

    measured = verification_module._measure_stabilization_freeze_attribution(
        Path("source.mp4"),
        Path("candidate.mp4"),
        ((0.0, 2.0),),
        ((0.0, 2.0),),
        parameters,
        "ffprobe",
        cast(Any, None),
        1.0,
        lambda: False,
    )

    assert measured["explained_freeze_events"] == pytest.approx(1.0)
    assert measured["unexplained_near_static_pairs"] == pytest.approx(0.0)


def test_native_stabilization_expected_near_static_uses_configured_p90_goal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_empty_outside_stabilization_measurement(monkeypatch)
    config = StabilizationConfig(
        frame_width=16,
        frame_height=16,
        residual_goal_median_pixels=0.5,
        residual_goal_p90_pixels=1.0,
        accepted_ranges=((0.0, 2.0),),
    )
    corrections = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            rotation_degrees=0.0,
            scale=1.0,
            translation_x=0.0,
            translation_y=0.0,
            inlier_ratio=1.0,
            residual_pixels=0.0,
            semantics="frame_correction",
        )
        for timestamp in (0.0, 1.0)
    )
    first: NDArray[np.uint8] = np.zeros((16, 16), dtype=np.uint8)
    second_source = first.copy()
    second_source[:12, :] = 1
    second_candidate = first.copy()
    second_candidate[:4, :] = 1
    frames = (
        (0, 2, 0, 0.0, first, first),
        (0, 2, 1, 1.0, second_source, second_candidate),
    )
    monkeypatch.setattr(
        verification_module,
        "_iter_aligned_perceptual_frames",
        lambda *_args, **_kwargs: iter(frames),
    )

    measured = verification_module._measure_stabilization_freeze_attribution(
        Path("source.mp4"),
        Path("candidate.mp4"),
        ((0.0, 2.0),),
        ((0.0, 2.0),),
        {
            "config": config.model_dump(mode="json"),
            "motion_transforms": [item.model_dump(mode="json") for item in corrections],
        },
        "ffprobe",
        cast(Any, None),
        1.0,
        lambda: False,
    )

    assert measured["candidate_freeze_events"] == pytest.approx(1.0)
    assert measured["attributed_candidate_freeze_events"] == pytest.approx(1.0)
    assert measured["explained_freeze_events"] == pytest.approx(1.0)
    assert measured["unexplained_near_static_pairs"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("duplicate_candidate", "expected_duplicates"),
    [(False, 0.0), (True, 1.0)],
)
def test_native_stabilization_outside_inventory_uses_exact_pts_and_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    duplicate_candidate: bool,
    expected_duplicates: float,
) -> None:
    timestamps = tuple(float(index) for index in range(6))
    source_frames: tuple[NDArray[np.uint8], ...] = tuple(
        np.full((8, 8), value, dtype=np.uint8) for value in (0, 2, 3, 4, 5, 7)
    )
    first_candidate = source_frames[0].copy()
    second_candidate = first_candidate.copy()
    if not duplicate_candidate:
        second_candidate[0, 0] = 1
    candidate_frames = (
        first_candidate,
        second_candidate,
        source_frames[2],
        source_frames[3],
        source_frames[4],
        source_frames[5],
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_video_timestamps",
        lambda *_args, **_kwargs: timestamps,
    )
    monkeypatch.setattr(
        verification_module,
        "_iter_video_frames_by_index",
        lambda path, *_args, **_kwargs: iter(
            source_frames if path.name == "source.mp4" else candidate_frames
        ),
    )

    measured = verification_module._measure_outside_stabilization_freezes(
        Path("source.mp4"),
        Path("candidate.mp4"),
        ((2.0, 4.0),),
        ((2.0, 4.0),),
        "ffprobe",
        cast(Any, None),
        1.0,
        lambda: False,
    )

    assert measured["outside_range_coverage_ratio"] == pytest.approx(1.0)
    assert measured["outside_expected_frames"] == pytest.approx(4.0)
    assert measured["source_outside_freeze_events"] == pytest.approx(0.0)
    assert measured["candidate_outside_freeze_events"] == pytest.approx(1.0)
    assert measured["outside_exact_duplicate_pairs"] == pytest.approx(
        expected_duplicates
    )


def test_native_stabilization_outside_inventory_rejects_pts_cardinality(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        verification_module,
        "_probe_video_timestamps",
        lambda path, *_args, **_kwargs: (
            (0.0, 1.0, 2.0) if path.name == "source.mp4" else (0.0, 1.0)
        ),
    )

    with pytest.raises(ValueError, match="timestamp inventories differ"):
        verification_module._measure_outside_stabilization_freezes(
            Path("source.mp4"),
            Path("candidate.mp4"),
            ((1.0, 2.0),),
            ((1.0, 2.0),),
            "ffprobe",
            cast(Any, None),
            1.0,
            lambda: False,
        )


@pytest.mark.parametrize(
    ("wrong_correction", "duplicate_candidate", "expected_key"),
    [
        (True, False, "unexplained_near_static_pairs"),
        (False, True, "exact_duplicate_pairs"),
    ],
)
def test_native_stabilization_freeze_attribution_rejects_mismatch_or_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    wrong_correction: bool,
    duplicate_candidate: bool,
    expected_key: str,
) -> None:
    parameters, frames = _stabilization_freeze_native_fixture(
        wrong_correction=wrong_correction,
        duplicate_candidate=duplicate_candidate,
    )
    _patch_empty_outside_stabilization_measurement(monkeypatch)
    monkeypatch.setattr(
        verification_module,
        "_iter_aligned_perceptual_frames",
        lambda *_args, **_kwargs: iter(frames),
    )

    measured = verification_module._measure_stabilization_freeze_attribution(
        Path("source.mp4"),
        Path("candidate.mp4"),
        ((0.0, 2.0),),
        ((0.0, 2.0),),
        parameters,
        "ffprobe",
        cast(Any, None),
        1.0,
        lambda: False,
    )

    assert measured[expected_key] >= 1.0


def test_native_stabilization_freeze_attribution_rejects_missing_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters, frames = _stabilization_freeze_native_fixture()
    raw = parameters["motion_transforms"]
    assert isinstance(raw, list)
    parameters["motion_transforms"] = raw[:1]
    monkeypatch.setattr(
        verification_module,
        "_iter_aligned_perceptual_frames",
        lambda *_args, **_kwargs: iter(frames),
    )

    with pytest.raises(ValueError, match="correction PTS is incomplete"):
        verification_module._measure_stabilization_freeze_attribution(
            Path("source.mp4"),
            Path("candidate.mp4"),
            ((0.0, 2.0),),
            ((0.0, 2.0),),
            parameters,
            "ffprobe",
            cast(Any, None),
            1.0,
            lambda: False,
        )


def test_stabilization_freeze_attribution_rejects_pts_gap_and_removed_mapping() -> None:
    with pytest.raises(ValueError, match="uniform decoded timestamps"):
        verification_module._uniform_timestamp_cadence((0.0, 0.041667, 0.1))
    parameters, _frames = _stabilization_freeze_native_fixture()
    with pytest.raises(ValueError, match="ranges do not align"):
        verification_module._measure_stabilization_freeze_attribution(
            Path("source.mp4"),
            Path("candidate.mp4"),
            ((0.0, 2.0),),
            (),
            parameters,
            "ffprobe",
            cast(Any, None),
            1.0,
            lambda: False,
        )


def test_transition_anchor_discontinuous_curve_fails_consensus_and_real_seam(
    tmp_path: Path,
) -> None:
    """A faithful renderer cannot make a +50/-50 correction boundary acceptable."""
    report = _verify(
        tmp_path,
        actions=(
            _anchor_action(
                "transition_anchor_v1",
                discontinuous_boundary=True,
            ),
        ),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.STABILIZE): {
                "range_coverage_ratio": 1.0,
                "expected_frames": 96.0,
                "reliable_transforms": 96.0,
                "residual_median_pixels": 0.1,
                "residual_p90_pixels": 0.2,
                "crop_ratio": 0.05,
                "transition_consensus_coverage_ratio": 1.0,
                "transition_consensus_p90_pixels": 0.1,
                "transition_seam_residual_pixels": 0.1,
                "transition_boundary_source_translation_x": 0.0,
                "transition_boundary_source_translation_y": 0.0,
                "transition_expected_frames": 24.0,
                "transition_reliable_frames": 24.0,
            }
        },
    )

    assert (
        _check(report, "faithful", "transition_stabilization_consensus").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(report, "faithful", "transition_stabilization_seam").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(report, "faithful", "transition_stabilization_coverage").status
        is RescueVerificationStatus.PASSED
    )


def test_transition_verification_bounds_frames_before_retaining_copies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An 8K-like frame may not be copied before strict analysis resizing."""

    class CopyGuardFrame(np.ndarray):
        def __new__(cls, shape: tuple[int, int]) -> CopyGuardFrame:
            storage: NDArray[np.float32] = np.zeros((1, 1), dtype=np.float32)
            logical_8k = np.lib.stride_tricks.as_strided(
                storage,
                shape=shape,
                strides=(0, 0),
            )
            return cast(CopyGuardFrame, logical_8k.view(cls))

        def copy(self, *args: Any, **kwargs: Any) -> NDArray[np.float32]:
            if self.shape != (9, 16):
                raise AssertionError("unbounded transition frame was copied")
            return cast(NDArray[np.float32], super().copy(*args, **kwargs))

        def astype(self, *args: Any, **kwargs: Any) -> NDArray[Any]:
            if self.shape != (9, 16):
                raise AssertionError("unbounded transition frame was converted")
            return cast(NDArray[Any], super().astype(*args, **kwargs))

    config = StabilizationConfig(
        frame_width=16,
        frame_height=9,
        accepted_ranges=((0.0, 4.0),),
    )
    corrections = tuple(
        MotionTransform(
            timestamp_seconds=float(index),
            translation_x=0.0,
            translation_y=0.0,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.1,
            semantics="frame_correction",
        )
        for index in range(4)
    )
    parameters: dict[str, JsonValue] = {
        "method": "transition_anchor_v1",
        "algorithm_version": "1",
        "estimator_algorithm_version": "transition_anchor_v1",
        "transition_range": [0.0, 1.0],
        "following_anchor_range": [1.0, 4.0],
        "transition_correction_count": 4,
        "motion_transforms": [item.model_dump(mode="json") for item in corrections],
        "config": config.model_dump(mode="json"),
    }

    def aligned_frames(*_args: Any, **_kwargs: Any) -> Any:
        for index in range(4):
            yield (
                0,
                4,
                index,
                float(index),
                CopyGuardFrame((4320, 7680)),
                CopyGuardFrame((4320, 7680)),
            )

    observed_consensus_shapes: list[tuple[int, int]] = []
    original_asarray = np.asarray

    def preserving_guard_asarray(value: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(value, CopyGuardFrame):
            return value
        return original_asarray(value, *args, **kwargs)

    def bounded_resize(
        frame: np.ndarray,
        size: tuple[int, int],
        *,
        interpolation: int,
    ) -> NDArray[np.float32]:
        del interpolation
        assert frame.shape == (4320, 7680)
        return np.zeros((size[1], size[0]), dtype=np.float32)

    def consensus(
        frames: tuple[tuple[float, NDArray[np.uint8]], ...],
        _config: StabilizationConfig,
        *,
        cancellation_callback: Callable[[], bool],
    ) -> tuple[TransitionConsensusStep, ...]:
        assert cancellation_callback() is False
        observed_consensus_shapes.extend(frame.shape for _timestamp, frame in frames)
        assert all(frame.dtype == np.uint8 for _timestamp, frame in frames)
        return (
            TransitionConsensusStep(
                previous_timestamp_seconds=0.0,
                current_timestamp_seconds=1.0,
                translation_x=0.0,
                translation_y=0.0,
                residual_pixels=0.0,
            ),
        )

    monkeypatch.setattr(
        verification_module, "_iter_aligned_perceptual_frames", aligned_frames
    )
    monkeypatch.setattr(np, "asarray", preserving_guard_asarray)
    monkeypatch.setattr(cv2, "resize", bounded_resize)
    monkeypatch.setattr(
        verification_module,
        "_independent_affine_measurement",
        lambda *_args, **_kwargs: {
            "scale": 1.0,
            "translation_pixels": 0.0,
        },
    )
    monkeypatch.setattr(
        verification_module,
        "_affine_correction_residual_pixels",
        lambda *_args, **_kwargs: 0.0,
    )
    monkeypatch.setattr(
        verification_module, "measure_transition_source_consensus", consensus
    )

    measured = verification_module._measure_anchor_outcome(
        Path("source.mp4"),
        Path("candidate.mp4"),
        ((0.0, 4.0),),
        ((0.0, 4.0),),
        parameters,
        "ffprobe",
        lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        1.0,
        lambda: False,
    )

    assert observed_consensus_shapes == [(9, 16), (9, 16)]
    assert measured["transition_consensus_coverage_ratio"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    "measurement",
    [
        {},
        {
            "range_coverage_ratio": float("nan"),
            "compared_frames": 24.0,
            "edge_width_ratio": 0.5,
            "edge_continuity_ratio": 0.95,
            "ringing_ratio": 0.01,
            "noise_gain_ratio": 1.1,
            "temporal_change_ratio": 0.02,
        },
        {
            "range_coverage_ratio": 0.75,
            "compared_frames": 18.0,
            "edge_width_ratio": 0.5,
            "edge_continuity_ratio": 0.95,
            "ringing_ratio": 0.01,
            "noise_gain_ratio": 1.1,
            "temporal_change_ratio": 0.02,
        },
    ],
)
def test_deblur_missing_nonfinite_or_range_shifted_measurement_requires_review(
    tmp_path: Path, measurement: dict[str, float]
) -> None:
    report = _verify(
        tmp_path,
        actions=(_deblur_action(),),
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.DEBLUR): measurement
        },
    )

    for check_id in (
        "deblur_edge_recovery",
        "deblur_ringing",
        "deblur_temporal_consistency",
    ):
        assert _check(report, "faithful", check_id).status is (
            RescueVerificationStatus.NEEDS_REVIEW
        )
    assert report.faithful_status is RescueVerificationStatus.NEEDS_REVIEW


def test_deblur_measurement_accepts_current_single_operation_wire_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = DeblurConfig()
    estimate = BlurKernelEstimate(
        kernel_kind="gaussian",
        radius=3,
        regularization=0.003,
        confidence=0.95,
        edge_width_before=5.0,
        predicted_edge_width_after=2.5,
        edge_continuity_ratio=0.95,
        reblur_error_ratio=0.02,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )
    monkeypatch.setattr(
        verification_module,
        "_measure_deblur_pairs",
        lambda *_args, **_kwargs: {
            "range_coverage_ratio": 1.0,
            "compared_frames": 132.0,
            "edge_width_ratio": 0.5,
            "edge_continuity_ratio": 0.95,
            "ringing_ratio": 0.01,
            "noise_gain_ratio": 1.1,
            "temporal_change_ratio": 0.02,
        },
    )

    measured = verification_module._measure_deblur_outcome(
        Path("source.mp4"),
        Path("candidate.mp4"),
        ((4.75, 10.25),),
        ((4.75, 10.25),),
        {
            "algorithm_version": "1",
            "estimate": estimate.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
        },
        "test-ffprobe",
        lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        10.0,
        lambda: False,
    )

    assert measured["operation_count"] == 1.0
    assert measured["range_coverage_ratio"] == pytest.approx(1.0)


def test_independent_deblur_pair_metrics_reward_real_edge_recovery() -> None:
    crisp: NDArray[np.uint8] = np.zeros((96, 128), dtype=np.uint8)
    crisp[:, 16:112] = 224
    cv2.line(crisp, (16, 16), (111, 79), 24, 2)
    blurred = cv2.GaussianBlur(crisp, (11, 11), 3.0)

    measured = verification_module._independent_deblur_pair_metrics(blurred, crisp)

    assert measured["edge_width_ratio"] < 0.72
    assert measured["edge_continuity_ratio"] >= 0.72
    assert measured["ringing_ratio"] <= 0.08


def test_independent_tonal_window_metrics_preserve_non_target_audio() -> None:
    sample_rate = 16000
    timeline = np.arange(sample_rate // 2, dtype=np.float64) / sample_rate
    voice = 0.2 * np.sin(2.0 * np.pi * 220.0 * timeline)
    interference = 0.35 * np.sin(2.0 * np.pi * 880.0 * timeline)
    source = voice + interference
    candidate = voice

    measured = verification_module._independent_tonal_window_metrics(
        source,
        candidate,
        sample_rate,
        target_frequency_hz=880.0,
        window_seconds=0.05,
    )

    assert measured["target_reduction_db"] >= 14.0
    assert measured["non_target_attenuation_db"] <= 0.25


def test_independent_tonal_metrics_reject_one_non_target_loss_window() -> None:
    sample_rate = 16_000
    window_size = sample_rate // 20
    window_count = 21
    timeline = np.arange(window_count * window_size, dtype=np.float64) / sample_rate
    voice = 0.2 * np.sin(2.0 * np.pi * 220.0 * timeline)
    interference = 0.35 * np.sin(2.0 * np.pi * 880.0 * timeline)
    source = voice + interference
    candidate = voice + interference * math.pow(10.0, -25.0 / 20.0)
    last_window = slice((window_count - 1) * window_size, window_count * window_size)
    candidate[last_window] = voice[last_window] * 0.1 + interference[
        last_window
    ] * math.pow(10.0, -25.0 / 20.0)

    measured = verification_module._independent_tonal_window_metrics(
        source,
        candidate,
        sample_rate,
        target_frequency_hz=880.0,
        window_seconds=0.05,
    )

    assert measured["target_reduction_db"] >= 24.0
    assert measured["non_target_attenuation_db"] >= 19.0


def test_independent_tonal_metrics_reject_untreated_final_50ms_window() -> None:
    sample_rate = 16000
    timeline = np.arange(sample_rate // 2, dtype=np.float64) / sample_rate
    voice = 0.2 * np.sin(2.0 * np.pi * 220.0 * timeline)
    interference = 0.35 * np.sin(2.0 * np.pi * 880.0 * timeline)
    source = voice + interference
    candidate = voice.copy()
    candidate[-(sample_rate // 20) :] = source[-(sample_rate // 20) :]

    measured = verification_module._independent_tonal_window_metrics(
        source,
        candidate,
        sample_rate,
        target_frequency_hz=880.0,
        window_seconds=0.05,
    )

    assert measured["target_reduction_db"] < 24.0


def test_tonal_full_target_excludes_only_raised_cosine_transition_windows() -> None:
    sample_rate = 16_000
    window = sample_rate // 20
    timeline = np.arange(window * 8, dtype=np.float64) / sample_rate
    source = 0.35 * np.sin(2.0 * np.pi * 880.0 * timeline)
    candidate = source * 10.0 ** (-27.0 / 20.0)
    candidate[:window] = source[:window]
    candidate[-window:] = source[-window:]

    measured = verification_module._independent_tonal_window_metrics(
        source,
        candidate,
        sample_rate,
        target_frequency_hz=880.0,
        window_seconds=0.05,
        boundary_transition_seconds=0.05,
    )

    assert measured["window_count"] == 6.0
    assert measured["excluded_transition_window_count"] == 2.0
    assert measured["target_reduction_db"] >= 24.0


@pytest.mark.parametrize(
    "defect_side",
    (
        "left_positive_click",
        "left_negative_click",
        "right_positive_click",
        "right_negative_click",
        "positive_finite_ramp",
        "negative_finite_ramp",
    ),
)
@pytest.mark.parametrize("boundary_side", ("start", "end"))
def test_source_relative_tonal_boundary_ignores_smooth_envelope_but_rejects_defect(
    defect_side: str,
    boundary_side: Literal["start", "end"],
) -> None:
    sample_rate = 16_000
    config = TonalInterferenceConfig()
    timeline = np.arange(sample_rate, dtype=np.float64) / sample_rate
    source = 0.08 * np.sin(2.0 * np.pi * 220.0 * timeline)
    event = (timeline >= 0.2) & (timeline < 0.8)
    source[event] += 0.35 * np.sin(2.0 * np.pi * 880.0 * timeline[event])
    profile = InterferenceTone(
        start_seconds=0.2,
        end_seconds=0.8,
        center_frequency_hz=880.0,
        confidence=0.95,
        baseline_before_dbfs=-60.0,
        baseline_after_dbfs=-60.0,
        peak_dbfs=-9.0,
        local_peak_over_baseline_db=40.0,
        persistence_window_count=12,
        frequency_standard_deviation_hz=0.0,
        channel_indices=(0,),
        attenuation_target_db=config.attenuation_db,
    )
    candidate = tonal_module.apply_tonal_reduction_to_pcm(
        source, sample_rate, (profile,), config
    )[:, 0]
    window_size = round(0.05 * sample_rate)
    boundary = round(
        (profile.start_seconds if boundary_side == "start" else profile.end_seconds)
        * sample_rate
    )
    smooth = verification_module._source_relative_tonal_boundary_metrics(
        source,
        candidate,
        boundary,
        window_size,
        sample_rate,
        profile.center_frequency_hz,
        boundary_side=boundary_side,
        boundary_transition_seconds=config.boundary_transition_seconds,
        derivative_numerical_floor=config.max_boundary_adjacent_delta,
    )
    assert smooth["energy_jump_db"] <= config.max_boundary_energy_jump_db
    assert smooth["crest_jump_db"] <= config.max_boundary_crest_jump_db
    assert smooth["adjacent_delta"] <= config.max_boundary_adjacent_delta

    defective = candidate.copy()
    if defect_side == "left_positive_click":
        defective[boundary - 1] += 0.4
    elif defect_side == "left_negative_click":
        defective[boundary - 1] -= 0.4
    elif defect_side == "right_positive_click":
        defective[boundary] += 0.4
    elif defect_side == "right_negative_click":
        defective[boundary] -= 0.4
    elif defect_side == "positive_finite_ramp":
        ramp = np.linspace(0.0, 0.4, 32, endpoint=True)
        if boundary_side == "start":
            defective[boundary : boundary + ramp.size] += ramp
        else:
            defective[boundary - ramp.size : boundary] += ramp
    else:
        ramp = np.linspace(0.0, -0.2, 32, endpoint=True)
        if boundary_side == "start":
            defective[boundary : boundary + ramp.size] += ramp
        else:
            defective[boundary - ramp.size : boundary] += ramp
    measured = verification_module._source_relative_tonal_boundary_metrics(
        source,
        defective,
        boundary,
        window_size,
        sample_rate,
        profile.center_frequency_hz,
        boundary_side=boundary_side,
        boundary_transition_seconds=config.boundary_transition_seconds,
        derivative_numerical_floor=config.max_boundary_adjacent_delta,
    )
    assert (
        measured["energy_jump_db"] > config.max_boundary_energy_jump_db
        or measured["crest_jump_db"] > config.max_boundary_crest_jump_db
        or measured["adjacent_delta"] > config.max_boundary_adjacent_delta
    )


@pytest.mark.parametrize("polarity", (-1.0, 1.0))
def test_source_relative_tonal_onset_excursion_honors_existing_threshold(
    polarity: float,
) -> None:
    sample_rate = 16_000
    config = TonalInterferenceConfig()
    window_size = round(0.05 * sample_rate)
    boundary = window_size
    timeline = np.arange(window_size * 2, dtype=np.float64) / sample_rate
    source = 0.2 * np.sin(2.0 * np.pi * 220.0 * timeline)

    at_limit = source.copy()
    at_limit[boundary : boundary + 32] += np.linspace(
        0.0,
        polarity * config.max_boundary_adjacent_delta,
        32,
        endpoint=True,
    )
    accepted = verification_module._source_relative_tonal_boundary_metrics(
        source,
        at_limit,
        boundary,
        window_size,
        sample_rate,
        880.0,
        boundary_side="start",
        boundary_transition_seconds=config.boundary_transition_seconds,
        derivative_numerical_floor=config.max_boundary_adjacent_delta,
    )
    assert verification_module._at_or_below_with_ulp(
        accepted["adjacent_delta"], config.max_boundary_adjacent_delta
    )

    over_limit = source.copy()
    over_limit[boundary : boundary + 32] += np.linspace(
        0.0,
        polarity * (config.max_boundary_adjacent_delta + 0.0001),
        32,
        endpoint=True,
    )
    rejected = verification_module._source_relative_tonal_boundary_metrics(
        source,
        over_limit,
        boundary,
        window_size,
        sample_rate,
        880.0,
        boundary_side="start",
        boundary_transition_seconds=config.boundary_transition_seconds,
        derivative_numerical_floor=config.max_boundary_adjacent_delta,
    )
    assert rejected["adjacent_delta"] > config.max_boundary_adjacent_delta
    assert not verification_module._at_or_below_with_ulp(
        rejected["adjacent_delta"], config.max_boundary_adjacent_delta
    )


@pytest.mark.parametrize("source_polarity", (-1.0, 1.0))
@pytest.mark.parametrize("residual_polarity", (-1.0, 1.0))
@pytest.mark.parametrize(
    ("residual_magnitude", "within_limit"),
    ((0.08, True), (0.0801, False)),
)
def test_source_relative_exact_boundary_delta_is_owned_only_by_adjacent_gate(
    source_polarity: float,
    residual_polarity: float,
    residual_magnitude: float,
    within_limit: bool,
) -> None:
    sample_rate = 16_000
    config = TonalInterferenceConfig()
    window_size = round(0.05 * sample_rate)
    boundary = window_size
    source: NDArray[np.float64] = np.zeros(window_size * 2, dtype=np.float64)
    source[boundary:] = source_polarity * 0.04
    candidate = source.copy()
    candidate[boundary:] += residual_polarity * residual_magnitude

    measured = verification_module._source_relative_tonal_boundary_metrics(
        source,
        candidate,
        boundary,
        window_size,
        sample_rate,
        880.0,
        boundary_side="start",
        boundary_transition_seconds=config.boundary_transition_seconds,
        derivative_numerical_floor=config.max_boundary_adjacent_delta,
    )

    if within_limit:
        assert measured["energy_jump_db"] <= config.max_boundary_energy_jump_db
        assert measured["crest_jump_db"] <= config.max_boundary_crest_jump_db
        assert verification_module._at_or_below_with_ulp(
            measured["adjacent_delta"], config.max_boundary_adjacent_delta
        )
    else:
        assert measured["adjacent_delta"] > config.max_boundary_adjacent_delta
        assert not verification_module._at_or_below_with_ulp(
            measured["adjacent_delta"], config.max_boundary_adjacent_delta
        )


@pytest.mark.parametrize("boundary_side", ("start", "end"))
def test_source_relative_boundary_projects_only_declared_smooth_tone_envelope(
    boundary_side: Literal["start", "end"],
) -> None:
    sample_rate = 16_000
    config = TonalInterferenceConfig()
    window_size = round(0.05 * sample_rate)
    boundary = window_size
    relative_time = np.arange(-window_size, window_size, dtype=np.float64) / sample_rate
    tone = np.sin(2.0 * np.pi * 880.0 * relative_time)
    source = 0.95 * tone
    envelope = np.zeros_like(relative_time)
    if boundary_side == "start":
        selected = relative_time >= 0.0
        distance = relative_time[selected]
    else:
        selected = relative_time < 0.0
        distance = -relative_time[selected]
    envelope[selected] = 0.5 - 0.5 * np.cos(
        np.pi
        * np.minimum(distance, config.boundary_transition_seconds)
        / config.boundary_transition_seconds
    )
    candidate = source - 3.5 * envelope * tone

    measured = verification_module._source_relative_tonal_boundary_metrics(
        source,
        candidate,
        boundary,
        window_size,
        sample_rate,
        880.0,
        boundary_side=boundary_side,
        boundary_transition_seconds=config.boundary_transition_seconds,
        derivative_numerical_floor=config.max_boundary_adjacent_delta,
    )

    assert measured["energy_jump_db"] <= config.max_boundary_energy_jump_db
    assert measured["crest_jump_db"] <= config.max_boundary_crest_jump_db
    assert measured["adjacent_delta"] <= config.max_boundary_adjacent_delta


def test_source_relative_boundary_rejects_insufficient_tone_envelope_basis() -> None:
    sample_rate = 16_000
    window_size = round(0.05 * sample_rate)
    samples: NDArray[np.float64] = np.zeros(window_size * 2, dtype=np.float64)

    with pytest.raises(ValueError, match="basis"):
        verification_module._source_relative_tonal_boundary_metrics(
            samples,
            samples,
            window_size,
            window_size,
            sample_rate,
            1e-12,
            boundary_side="start",
            boundary_transition_seconds=0.05,
            derivative_numerical_floor=0.08,
        )


@pytest.mark.parametrize("amplitude", (0.0, 1e-12))
def test_source_relative_boundary_is_finite_at_silence_floor(amplitude: float) -> None:
    sample_rate = 16_000
    config = TonalInterferenceConfig()
    window_size = round(0.05 * sample_rate)
    timeline = np.arange(window_size * 2, dtype=np.float64) / sample_rate
    source = amplitude * np.sin(2.0 * np.pi * 220.0 * timeline)

    measured = verification_module._source_relative_tonal_boundary_metrics(
        source,
        source.copy(),
        window_size,
        window_size,
        sample_rate,
        880.0,
        boundary_side="start",
        boundary_transition_seconds=config.boundary_transition_seconds,
        derivative_numerical_floor=config.max_boundary_adjacent_delta,
    )

    assert all(math.isfinite(value) for value in measured.values())
    assert measured == {
        "energy_jump_db": 0.0,
        "crest_jump_db": 0.0,
        "adjacent_delta": 0.0,
    }


def test_independent_affine_measurement_reports_real_translation() -> None:
    source: NDArray[np.uint8] = np.zeros((96, 128), dtype=np.uint8)
    for y in range(12, 90, 14):
        for x in range(12, 120, 14):
            cv2.circle(source, (x, y), 3, 180 + (x + y) % 70, -1)
    matrix = np.float32([[1.0, 0.0, 4.0], [0.0, 1.0, -2.0]])
    shifted = cv2.warpAffine(source, matrix, (128, 96))

    measured = verification_module._independent_affine_measurement(source, shifted)

    assert measured is not None
    assert measured["translation_pixels"] == pytest.approx(
        float(np.hypot(4.0, -2.0)), abs=0.35
    )
    assert measured["inlier_ratio"] >= 0.8


def test_anchor_residual_reconstructs_renderer_translation_scaling_and_crop() -> None:
    correction = MotionTransform(
        timestamp_seconds=0.0,
        translation_x=2.0,
        translation_y=-1.0,
        rotation_degrees=0.0,
        scale=1.0,
        inlier_ratio=0.95,
        residual_pixels=0.1,
        semantics="frame_correction",
    )
    crop = 2.0 / 64.0
    zoom = 1.0 / (1.0 - crop)
    center_x = (128 - 1) / 2.0
    center_y = (96 - 1) / 2.0
    observed = {
        "scale": zoom,
        "rotation_degrees": 0.0,
        "translation_x": zoom * 4.0 + center_x * (1.0 - zoom),
        "translation_y": zoom * -2.0 + center_y * (1.0 - zoom),
    }

    residual = verification_module._affine_correction_residual_pixels(
        observed,
        correction,
        width=128,
        height=96,
        analysis_width=64,
        analysis_height=48,
        safe_crop_ratio=crop,
    )

    assert residual == pytest.approx(0.0, abs=1e-9)


def test_timestamp_alignment_uses_actual_non_frame_aligned_half_open_pts() -> None:
    aligned = verification_module._aligned_timestamp_index_pairs(
        (0.0, 0.1, 0.2, 0.3, 0.4),
        (0.0, 0.1, 0.2, 0.3, 0.4),
        (0.06, 0.31, 0.06, 0.31),
    )

    assert aligned == ((1, 1), (2, 2), (3, 3))


def test_timestamp_alignment_rejects_vfr_cadence() -> None:
    with pytest.raises(ValueError, match="uniform"):
        verification_module._aligned_timestamp_index_pairs(
            (0.0, 0.1, 0.23, 0.3),
            (0.0, 0.1, 0.2, 0.3),
            (0.0, 0.31, 0.0, 0.31),
        )


@pytest.mark.parametrize(
    "timestamps",
    (
        (0.0, 0.1, 0.200001, 0.300001),
        (4.0, 4.041667, 4.083333, 4.125),
    ),
    ids=("exact-one-microsecond", "ffprobe-decimal-rounding"),
)
def test_uniform_timestamp_cadence_accepts_exact_decimal_tolerance(
    timestamps: tuple[float, ...],
) -> None:
    verification_module._uniform_timestamp_cadence(timestamps)


def test_uniform_timestamp_cadence_rejects_real_deviation_above_tolerance() -> None:
    with pytest.raises(ValueError, match="uniform"):
        verification_module._uniform_timestamp_cadence(
            (4.0, 4.1, 4.2000010001, 4.3000010001)
        )


def test_timestamp_alignment_rejects_source_candidate_pts_mismatch() -> None:
    with pytest.raises(ValueError, match="correspond"):
        verification_module._aligned_timestamp_index_pairs(
            (0.0, 0.1, 0.2, 0.3),
            (0.0005, 0.1005, 0.2005, 0.3005),
            (0.0, 0.4, 0.0, 0.4),
        )


def test_timestamp_alignment_rejects_range_beyond_actual_terminal_coverage() -> None:
    with pytest.raises(ValueError, match="coverage"):
        verification_module._aligned_timestamp_index_pairs(
            (0.0, 0.1, 0.2, 0.3),
            (0.0, 0.1, 0.2, 0.3),
            (0.0, 0.400002, 0.0, 0.400002),
        )


@pytest.mark.parametrize(
    ("origin", "raw_timestamps", "normalized"),
    (
        (0.021, (0.021, 0.121, 0.221, 0.321), (0.0, 0.1, 0.2, 0.3)),
        (0.0, (0.001, 0.101, 0.201, 0.301), (0.001, 0.101, 0.201, 0.301)),
    ),
    ids=("faithful-nonzero-origin", "first-representation-offset-preserved"),
)
def test_video_timestamp_probe_normalizes_only_by_explicit_stream_start(
    origin: float,
    raw_timestamps: tuple[float, ...],
    normalized: tuple[float, ...],
) -> None:
    stdout = "".join(f"frame|{value:.9f}\n" for value in raw_timestamps)
    stdout += f"stream|{origin:.9f}|{len(raw_timestamps)}\n"

    def runner(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del arguments, timeout_seconds, sensitive_paths, cancellation_callback
        return CommandResult(0, "", stdout)

    measured = verification_module._probe_video_timestamps(
        Path("origin.mp4"),
        "test-ffprobe",
        runner,
        10.0,
        lambda: False,
    )

    assert measured == pytest.approx(normalized)


def test_source_faithful_improved_actual_pts_align_after_per_stream_normalization() -> (
    None
):
    paths = {
        "source.mp4": (0.0, (0.0, 0.1, 0.2, 0.3)),
        "faithful.mp4": (0.021, (0.021, 0.121, 0.221, 0.321)),
        "improved.mp4": (0.0, (0.0, 0.1, 0.2, 0.3)),
    }

    def runner(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del timeout_seconds, sensitive_paths, cancellation_callback
        origin, timestamps = paths[Path(arguments[-1]).name]
        stdout = "".join(f"frame|{value:.9f}\n" for value in timestamps)
        stdout += f"stream|{origin:.9f}|{len(timestamps)}\n"
        return CommandResult(0, "", stdout)

    measured = tuple(
        verification_module._probe_video_timestamps(
            Path(name),
            "test-ffprobe",
            runner,
            10.0,
            lambda: False,
        )
        for name in paths
    )

    assert measured[0] == measured[1] == measured[2]
    assert verification_module._aligned_timestamp_index_pairs(
        measured[0], measured[1], (0.0, 0.4, 0.0, 0.4)
    ) == ((0, 0), (1, 1), (2, 2), (3, 3))
    assert verification_module._aligned_timestamp_index_pairs(
        measured[1], measured[2], (0.0, 0.4, 0.0, 0.4)
    ) == ((0, 0), (1, 1), (2, 2), (3, 3))


def test_stream_origin_normalization_preserves_real_decimal_pts_drift() -> None:
    source = normalize_actual_video_timestamps((0.0, 0.1, 0.2), 0.0)
    drifted = normalize_actual_video_timestamps(
        (0.021, 0.121001, 0.221001),
        0.021,
    )

    assert drifted == (0.0, 0.100001, 0.200001)
    assert drifted != source


def _native_chroma_test_runner(
    *,
    native_result: CommandResult | None = None,
    cancel_native: bool = False,
) -> tuple[Callable[..., CommandResult], list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []
    topology = json.dumps(
        {
            "streams": [
                {
                    "avg_frame_rate": "10/1",
                    "chroma_location": "left",
                    "codec_name": "h264",
                    "color_primaries": None,
                    "color_range": None,
                    "color_space": None,
                    "color_transfer": None,
                    "field_order": "progressive",
                    "height": 64,
                    "level": 31,
                    "pix_fmt": "yuv420p",
                    "profile": "High",
                    "r_frame_rate": "10/1",
                    "sample_aspect_ratio": "1:1",
                    "time_base": "1/120000",
                    "width": 96,
                }
            ]
        }
    )

    def runner(
        arguments: tuple[str, ...],
        **_kwargs: object,
    ) -> CommandResult:
        calls.append(arguments)
        if "-filter_complex" in arguments:
            if cancel_native:
                raise RescueCancelledError("cancelled native chroma helper")
            return native_result or CommandResult(
                0,
                "",
                (
                    "frame:0 pts:0 pts_time:0\n"
                    "lavfi.signalstats.UAVG=2\n"
                    "lavfi.signalstats.VAVG=4\n"
                    "frame:1 pts:12000 pts_time:0.1\n"
                    "lavfi.signalstats.UAVG=3\n"
                    "lavfi.signalstats.VAVG=5\n"
                ),
            )
        if "-count_frames" in arguments:
            origin = 0.021 if Path(arguments[-1]).name == "control.mp4" else 0.0
            return CommandResult(
                0,
                "",
                "".join(f"frame|{origin + index / 10:.9f}\n" for index in range(4))
                + f"stream|{origin:.9f}|4\n",
            )
        return CommandResult(0, "", topology)

    return runner, calls


def test_native_chroma_helper_uses_stream_origins_and_uv_plane_means() -> None:
    runner, calls = _native_chroma_test_runner()

    measured = verification_module._measure_native_chroma_ranges(
        Path("control.mp4"),
        Path("candidate.mp4"),
        ((0.0, 0.2, 0.0, 0.2),),
        "fixed-ffmpeg",
        "fixed-ffprobe",
        runner,
        10.0,
        lambda: False,
    )

    assert measured == pytest.approx(((3.0 + 5.0) / (2.0 * 255.0),))
    native = next(call for call in calls if "-filter_complex" in call)
    graph = native[native.index("-filter_complex") + 1]
    assert "setpts=PTS-0.021/TB" in graph
    assert "setpts=PTS-0/TB" in graph
    assert "blend=all_mode=difference" in graph
    assert "signalstats" in graph
    assert "metadata=mode=print:file=-" in graph


def test_native_chroma_helper_accepts_realistic_full_signalstats_stdout() -> None:
    realistic = CommandResult(
        0,
        "",
        (
            "frame:0 pts:0 pts_time:0\n"
            "lavfi.signalstats.YMIN=0\n"
            "lavfi.signalstats.YLOW=1\n"
            "lavfi.signalstats.YAVG=17\n"
            "lavfi.signalstats.YHIGH=30\n"
            "lavfi.signalstats.YMAX=40\n"
            "lavfi.signalstats.UMIN=1\n"
            "lavfi.signalstats.ULOW=1\n"
            "lavfi.signalstats.UAVG=2\n"
            "lavfi.signalstats.UHIGH=8\n"
            "lavfi.signalstats.UMAX=9\n"
            "lavfi.signalstats.VMIN=1\n"
            "lavfi.signalstats.VLOW=1\n"
            "lavfi.signalstats.VAVG=4\n"
            "lavfi.signalstats.VHIGH=10\n"
            "lavfi.signalstats.VMAX=11\n"
            "lavfi.signalstats.SATMIN=0\n"
            "lavfi.signalstats.SATLOW=1\n"
            "lavfi.signalstats.SATAVG=3\n"
            "lavfi.signalstats.SATHIGH=7\n"
            "lavfi.signalstats.SATMAX=9\n"
            "lavfi.signalstats.HUEMED=180\n"
            "lavfi.signalstats.HUEAVG=181\n"
            "lavfi.signalstats.YDIF=1\n"
            "lavfi.signalstats.UDIF=1\n"
            "lavfi.signalstats.VDIF=1\n"
            "lavfi.signalstats.YBITDEPTH=8\n"
            "lavfi.signalstats.UBITDEPTH=8\n"
            "lavfi.signalstats.VBITDEPTH=8\n"
            "lavfi.signalstats.BRNG=0\n"
            "lavfi.signalstats.TOUT=0\n"
            "lavfi.signalstats.VREP=0\n"
            "frame:1 pts:12000 pts_time:0.1\n"
            "lavfi.signalstats.YMIN=0\n"
            "lavfi.signalstats.YLOW=1\n"
            "lavfi.signalstats.YAVG=18\n"
            "lavfi.signalstats.YHIGH=31\n"
            "lavfi.signalstats.YMAX=41\n"
            "lavfi.signalstats.UMIN=1\n"
            "lavfi.signalstats.ULOW=1\n"
            "lavfi.signalstats.UAVG=3\n"
            "lavfi.signalstats.UHIGH=9\n"
            "lavfi.signalstats.UMAX=10\n"
            "lavfi.signalstats.VMIN=1\n"
            "lavfi.signalstats.VLOW=1\n"
            "lavfi.signalstats.VAVG=5\n"
            "lavfi.signalstats.VHIGH=11\n"
            "lavfi.signalstats.VMAX=12\n"
            "lavfi.signalstats.SATMIN=0\n"
            "lavfi.signalstats.SATLOW=1\n"
            "lavfi.signalstats.SATAVG=4\n"
            "lavfi.signalstats.SATHIGH=8\n"
            "lavfi.signalstats.SATMAX=10\n"
            "lavfi.signalstats.HUEMED=180\n"
            "lavfi.signalstats.HUEAVG=181\n"
            "lavfi.signalstats.YDIF=1\n"
            "lavfi.signalstats.UDIF=1\n"
            "lavfi.signalstats.VDIF=1\n"
            "lavfi.signalstats.YBITDEPTH=8\n"
            "lavfi.signalstats.UBITDEPTH=8\n"
            "lavfi.signalstats.VBITDEPTH=8\n"
            "lavfi.signalstats.BRNG=0\n"
            "lavfi.signalstats.TOUT=0\n"
            "lavfi.signalstats.VREP=0\n"
        ),
    )
    runner, _calls = _native_chroma_test_runner(native_result=realistic)

    measured = verification_module._measure_native_chroma_ranges(
        Path("control.mp4"),
        Path("candidate.mp4"),
        ((0.0, 0.2, 0.0, 0.2),),
        "fixed-ffmpeg",
        "fixed-ffprobe",
        runner,
        10.0,
        lambda: False,
    )

    assert measured == pytest.approx(((3.0 + 5.0) / (2.0 * 255.0),))


def test_realistic_signalstats_produces_complete_passing_luma_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    control = tmp_path / "control.mp4"
    candidate = tmp_path / "candidate.mp4"
    source.write_bytes(b"source")
    control.write_bytes(b"control")
    candidate.write_bytes(b"candidate")
    source_frame: NDArray[np.uint8] = np.full((8, 8, 3), 50, dtype=np.uint8)
    control_frame: NDArray[np.uint8] = np.full((8, 8, 3), 50, dtype=np.uint8)
    candidate_frame: NDArray[np.uint8] = np.full((8, 8, 3), 65, dtype=np.uint8)

    def aligned_frames(
        first: Path,
        second: Path,
        *_args: object,
    ) -> tuple[tuple[Any, ...], ...]:
        first_frame = source_frame if first.name == "source.mp4" else control_frame
        second_frame = (
            candidate_frame if second.name == "candidate.mp4" else control_frame
        )
        return (
            (0, 2, 0, 0.0, first_frame, second_frame),
            (0, 2, 1, 0.1, first_frame, second_frame),
        )

    monkeypatch.setattr(
        verification_module, "_iter_aligned_perceptual_frames", aligned_frames
    )
    realistic = CommandResult(
        0,
        "",
        (
            "frame:0 pts:0 pts_time:0\n"
            "lavfi.signalstats.YMIN=0\n"
            "lavfi.signalstats.UAVG=1\n"
            "lavfi.signalstats.VAVG=1\n"
            "lavfi.signalstats.SATAVG=2\n"
            "frame:1 pts:12000 pts_time:0.1\n"
            "lavfi.signalstats.YMIN=0\n"
            "lavfi.signalstats.UAVG=2\n"
            "lavfi.signalstats.VAVG=2\n"
            "lavfi.signalstats.SATAVG=3\n"
        ),
    )
    runner, _calls = _native_chroma_test_runner(native_result=realistic)
    measured = verification_module._measure_luma_adjustment(
        source,
        control,
        candidate,
        ((0.0, 0.2),),
        ((0.0, 0.2),),
        _action(RescueActionKind.ADJUST_LUMA, {}).parameters,
        "fixed-ffmpeg",
        "fixed-ffprobe",
        runner,
        10.0,
        lambda: False,
    )
    action = _action(RescueActionKind.ADJUST_LUMA, {}).model_copy(
        update={"source_ranges": ((0.0, 0.2),)}
    )
    checks = verification_module._luma_adjustment_verification_checks(
        action,
        (SourceMapping(0.0, 0.2, 0.0, 0.2, "faithful-rescue.mp4"),),
        (),
        measured,
        expected_control_sha256=sha256(control.read_bytes()).hexdigest(),
        expected_candidate_sha256=sha256(candidate.read_bytes()).hexdigest(),
    )

    assert {check.check_id for check in checks} == {
        "perceptible_luma_improvement",
        "noise_side_effects",
        "luma_chroma_side_effects",
    }
    assert all(check.status is RescueVerificationStatus.PASSED for check in checks)
    for check in checks:
        assert check.measured["measurement_valid"] is True
        assert check.measured["minimum_luma_delta"] == pytest.approx(15.0 / 255.0)
        assert check.measured["maximum_noise_increase"] == pytest.approx(0.0)
        assert check.measured["maximum_clipping_increase"] == pytest.approx(0.0)
        assert check.measured["maximum_chroma_shift"] == pytest.approx(4.0 / 510.0)
        assert "measurement_error" not in check.measured


@pytest.mark.parametrize(
    "native_result",
    (
        CommandResult(1, "No such filter: signalstats", ""),
        CommandResult(
            0,
            "",
            "frame:0 pts:0 pts_time:0\nlavfi.signalstats.UAVG=2\n",
        ),
    ),
    ids=("unsupported", "incomplete-metadata"),
)
def test_native_chroma_helper_fails_closed_on_ffmpeg_or_metadata_error(
    native_result: CommandResult,
) -> None:
    runner, _calls = _native_chroma_test_runner(native_result=native_result)

    with pytest.raises(
        ValueError, match="native_chroma_(command_failed|metadata_invalid)"
    ):
        verification_module._measure_native_chroma_ranges(
            Path("control.mp4"),
            Path("candidate.mp4"),
            ((0.0, 0.2, 0.0, 0.2),),
            "fixed-ffmpeg",
            "fixed-ffprobe",
            runner,
            10.0,
            lambda: False,
        )


@pytest.mark.parametrize(
    "metadata_line",
    (
        "lavfi.signalstats.UNKNOWN=1",
        "lavfi.signalstats.UAVG=2\nlavfi.signalstats.UAVG=3",
        "lavfi.signalstats.UAVG=nan",
        "lavfi.signalstats.UAVG=256",
    ),
    ids=("unknown-key", "duplicate-target", "nonfinite-target", "out-of-range"),
)
def test_native_chroma_helper_rejects_untrusted_signalstats_metadata(
    metadata_line: str,
) -> None:
    stdout = (
        "frame:0 pts:0 pts_time:0\n"
        f"{metadata_line}\n"
        "lavfi.signalstats.VAVG=4\n"
        "frame:1 pts:12000 pts_time:0.1\n"
        "lavfi.signalstats.UAVG=3\n"
        "lavfi.signalstats.VAVG=5\n"
    )
    runner, _calls = _native_chroma_test_runner(
        native_result=CommandResult(0, "", stdout)
    )

    with pytest.raises(ValueError, match="native_chroma_metadata_invalid"):
        verification_module._measure_native_chroma_ranges(
            Path("control.mp4"),
            Path("candidate.mp4"),
            ((0.0, 0.2, 0.0, 0.2),),
            "fixed-ffmpeg",
            "fixed-ffprobe",
            runner,
            10.0,
            lambda: False,
        )


def test_native_chroma_helper_propagates_cancellation_without_artifacts() -> None:
    runner, _calls = _native_chroma_test_runner(cancel_native=True)

    with pytest.raises(RescueCancelledError, match="cancelled"):
        verification_module._measure_native_chroma_ranges(
            Path("control.mp4"),
            Path("candidate.mp4"),
            ((0.0, 0.2, 0.0, 0.2),),
            "fixed-ffmpeg",
            "fixed-ffprobe",
            runner,
            10.0,
            lambda: False,
        )


@pytest.mark.parametrize(
    "footer",
    (
        "stream|4\n",
        "stream|nan|4\n",
        "stream|-0.001|4\n",
        "stream|0.0|4|unexpected\n",
    ),
    ids=("missing-start", "nonfinite-start", "negative-start", "extra-field"),
)
def test_video_timestamp_probe_rejects_invalid_explicit_stream_start(
    footer: str,
) -> None:
    stdout = "".join(f"frame|{index / 10:.9f}\n" for index in range(4)) + footer

    def runner(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del arguments, timeout_seconds, sensitive_paths, cancellation_callback
        return CommandResult(0, "", stdout)

    with pytest.raises(ValueError, match="incomplete"):
        verification_module._probe_video_timestamps(
            Path("invalid-origin.mp4"),
            "test-ffprobe",
            runner,
            10.0,
            lambda: False,
        )


def test_video_timestamp_probe_rejects_first_pts_inconsistent_with_stream_start() -> (
    None
):
    stdout = "".join(f"frame|{0.003 + index / 10:.9f}\n" for index in range(4))
    stdout += "stream|0.000000000|4\n"

    def runner(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del arguments, timeout_seconds, sensitive_paths, cancellation_callback
        return CommandResult(0, "", stdout)

    with pytest.raises(ValueError, match="incomplete"):
        verification_module._probe_video_timestamps(
            Path("inconsistent-origin.mp4"),
            "test-ffprobe",
            runner,
            10.0,
            lambda: False,
        )


def test_video_timestamp_probe_accepts_compact_inventory_over_old_json_cap() -> None:
    timestamps = tuple(index / 24.0 for index in range(1008))
    old_json_inventory = json.dumps(
        {
            "frames": [
                {"best_effort_timestamp_time": f"{timestamp:.9f}"}
                for timestamp in timestamps
            ]
        },
        indent=4,
    )
    assert len(old_json_inventory.encode("utf-8")) > 64 * 1024
    stdout = "".join(f"frame|{timestamp:.9f}\n" for timestamp in timestamps)
    stdout += f"stream|0.000000000|{len(timestamps)}\n"
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        return CommandResult(returncode=0, stderr_summary="", stdout_summary=stdout)

    measured = verification_module._probe_video_timestamps(
        Path("long-video.mp4"),
        "test-ffprobe",
        runner,
        10.0,
        lambda: False,
    )

    assert measured == pytest.approx(timestamps)
    assert "compact=p=1:nk=1" in calls[0]
    assert "-count_frames" in calls[0]
    show_entries = calls[0][calls[0].index("-show_entries") + 1]
    assert "stream=start_time,nb_read_frames" in show_entries


def test_video_timestamp_probe_suppresses_h264_sei_frame_side_data() -> None:
    """Catches native compact records gaining a non-timestamp SEI field."""
    calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        calls.append(arguments)
        show_entries = arguments[arguments.index("-show_entries") + 1]
        if "frame_side_data=" in show_entries:
            stdout = "frame|0.000000000|\nframe|0.041666667|\nstream|0.000000000|2\n"
        else:
            stdout = (
                "frame|0.000000000|H.26[45] User Data Unregistered SEI message\n"
                "frame|0.041666667\nstream|0.000000000|2\n"
            )
        return CommandResult(0, "", stdout)

    measured = verification_module._probe_video_timestamps(
        Path("native-h264.mp4"),
        "test-ffprobe",
        runner,
        10.0,
        lambda: False,
    )

    assert measured == pytest.approx((0.0, 1.0 / 24.0))
    show_entries = calls[0][calls[0].index("-show_entries") + 1]
    assert (
        "frame_side_data=:stream_tags=:stream_disposition=:stream_side_data="
        in show_entries
    )


@pytest.mark.parametrize(
    "stdout",
    (
        "frame|0.000000000\nframe|0.041666667\n",
        "frame|0.000000000\nframe|bad\nstream|0.000000000|2\n",
        "frame|0.000000000\nframe|0.000000000\nstream|0.000000000|2\n",
        "frame|0.000000000\nframe|nan\nstream|0.000000000|2\n",
        "frame|0.000000000\nframe|0.041666667\nstream|0.000000000|3\n",
        ("frame|0.000000000|unexpected\nframe|0.041666667\nstream|0.000000000|2\n"),
        "frame|0.000000000\nframe|0.041666667\nstream|0.000000000|2|unexpected\n",
        "".join(f"frame|{index}\n" for index in range(4097))
        + "stream|0.000000000|4097\n",
        "".join(f"frame|{index:090d}\n" for index in range(700))
        + "stream|0.000000000|700\n",
    ),
    ids=(
        "missing-footer",
        "bad-line",
        "duplicate-pts",
        "non-finite",
        "count-mismatch",
        "unknown-frame-field",
        "unknown-footer-field",
        "over-inventory-budget",
        "over-byte-budget",
    ),
)
def test_video_timestamp_probe_rejects_incomplete_or_invalid_inventory(
    stdout: str,
) -> None:
    def runner(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        _ = arguments, timeout_seconds, sensitive_paths, cancellation_callback
        return CommandResult(returncode=0, stderr_summary="", stdout_summary=stdout)

    with pytest.raises(ValueError):
        verification_module._probe_video_timestamps(
            Path("invalid-video.mp4"),
            "test-ffprobe",
            runner,
            10.0,
            lambda: False,
        )


def _write_verification_video(
    path: Path, frames: tuple[np.ndarray, ...], *, fps: float = 8.0
) -> None:
    height, width = frames[0].shape[:2]
    fourcc = cast(
        Callable[[str, str, str, str], int], getattr(cv2, "VideoWriter_fourcc")
    )
    writer = cv2.VideoWriter(str(path), fourcc(*"mp4v"), fps, (width, height))
    if not writer.isOpened():
        pytest.skip("OpenCV mp4v writer is unavailable")
    try:
        for frame in frames:
            writer.write(frame)
    finally:
        writer.release()


def test_native_deblur_measurement_decodes_exact_half_open_ranges(
    tmp_path: Path,
) -> None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("FFprobe is required for native video verification")
    assert ffprobe is not None
    crisp: NDArray[np.uint8] = np.zeros((96, 128), dtype=np.uint8)
    crisp[:, 16:112] = 224
    cv2.line(crisp, (16, 16), (111, 79), 24, 2)
    blurred = cv2.GaussianBlur(crisp, (11, 11), 3.0)
    source = tmp_path / "模糊 source.mp4"
    candidate = tmp_path / "清晰 candidate.mp4"
    _write_verification_video(
        source, tuple(cv2.cvtColor(blurred, cv2.COLOR_GRAY2BGR) for _ in range(8))
    )
    _write_verification_video(
        candidate, tuple(cv2.cvtColor(crisp, cv2.COLOR_GRAY2BGR) for _ in range(8))
    )
    estimate = BlurKernelEstimate(
        kernel_kind="gaussian",
        radius=3,
        regularization=0.003,
        confidence=0.95,
        edge_width_before=5.0,
        predicted_edge_width_after=2.5,
        edge_continuity_ratio=0.95,
        reblur_error_ratio=0.02,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )

    measured = NativeMediaMeasurementProvider(
        ffprobe=ffprobe
    ).measure_perceptual_restoration(
        RescueActionKind.DEBLUR,
        source,
        candidate,
        ((0.25, 0.75),),
        ((0.25, 0.75),),
        {
            "algorithm_version": "1",
            "operations": [
                {
                    "source_ranges": [[0.25, 0.75]],
                    "estimate": estimate.model_dump(mode="json"),
                    "config": DeblurConfig().model_dump(mode="json"),
                }
            ],
        },
        lambda: False,
    )

    assert measured["range_coverage_ratio"] == pytest.approx(1.0)
    assert measured["compared_frames"] == pytest.approx(4.0)
    assert measured["edge_width_ratio"] < 1.0


def test_native_deblur_measures_short_failed_operation_independently(
    tmp_path: Path,
) -> None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("FFprobe is required for native video verification")
    assert ffprobe is not None
    crisp: NDArray[np.uint8] = np.zeros((96, 128), dtype=np.uint8)
    crisp[:, 16:112] = 224
    cv2.line(crisp, (16, 16), (111, 79), 24, 2)
    blurred = cv2.GaussianBlur(crisp, (11, 11), 3.0)
    source_frame = cv2.cvtColor(blurred, cv2.COLOR_GRAY2BGR)
    crisp_frame = cv2.cvtColor(crisp, cv2.COLOR_GRAY2BGR)
    source = tmp_path / "two operations source.mp4"
    candidate = tmp_path / "short failure candidate.mp4"
    _write_verification_video(source, tuple(source_frame for _ in range(8)))
    _write_verification_video(
        candidate,
        tuple(crisp_frame if index < 6 else source_frame for index in range(8)),
    )
    config = DeblurConfig()
    estimate = BlurKernelEstimate(
        kernel_kind="gaussian",
        radius=3,
        regularization=0.003,
        confidence=0.95,
        edge_width_before=5.0,
        predicted_edge_width_after=2.5,
        edge_continuity_ratio=0.95,
        reblur_error_ratio=0.02,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )

    measured = NativeMediaMeasurementProvider(
        ffprobe=ffprobe
    ).measure_perceptual_restoration(
        RescueActionKind.DEBLUR,
        source,
        candidate,
        ((0.0, 1.0),),
        ((0.0, 1.0),),
        {
            "algorithm_version": "1",
            "operations": [
                {
                    "source_ranges": [[0.0, 0.75]],
                    "estimate": estimate.model_dump(mode="json"),
                    "config": config.model_dump(mode="json"),
                },
                {
                    "source_ranges": [[0.75, 1.0]],
                    "estimate": estimate.model_dump(mode="json"),
                    "config": config.model_dump(mode="json"),
                },
            ],
        },
        lambda: False,
    )

    assert measured["operation_count"] == pytest.approx(2.0)
    assert measured["edge_recovery_passed_operations"] < measured["operation_count"]


def test_native_anchor_measurement_requires_every_exact_frame(tmp_path: Path) -> None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("FFprobe is required for native video verification")
    assert ffprobe is not None
    texture: NDArray[np.uint8] = np.zeros((96, 128), dtype=np.uint8)
    for y in range(12, 90, 14):
        for x in range(12, 120, 14):
            cv2.circle(texture, (x, y), 3, 180 + (x + y) % 70, -1)
    frame = cv2.cvtColor(texture, cv2.COLOR_GRAY2BGR)
    source = tmp_path / "锚点 source.mp4"
    candidate = tmp_path / "稳定 candidate.mp4"
    _write_verification_video(source, tuple(frame for _ in range(8)))
    _write_verification_video(candidate, tuple(frame for _ in range(8)))
    config = StabilizationConfig(accepted_ranges=((0.25, 0.75),))
    corrections = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            translation_x=0.0,
            translation_y=0.0,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.1,
            semantics="frame_correction",
        )
        for timestamp in (0.25, 0.375, 0.5, 0.625)
    )

    measured = NativeMediaMeasurementProvider(
        ffprobe=ffprobe
    ).measure_perceptual_restoration(
        RescueActionKind.STABILIZE,
        source,
        candidate,
        ((0.25, 0.75),),
        ((0.25, 0.75),),
        {
            "method": "anchor_v1",
            "algorithm_version": "1",
            "motion_transforms": [item.model_dump(mode="json") for item in corrections],
            "config": config.model_dump(mode="json"),
        },
        lambda: False,
    )

    assert measured["range_coverage_ratio"] == pytest.approx(1.0)
    assert measured["expected_frames"] == pytest.approx(4.0)
    assert measured["reliable_transforms"] == pytest.approx(4.0)
    assert measured["residual_p90_pixels"] <= 0.1


def test_native_anchor_accepts_renderer_scaled_translation_and_safe_crop(
    tmp_path: Path,
) -> None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("FFprobe is required for native video verification")
    assert ffprobe is not None
    generator = np.random.default_rng(20260814)
    texture = generator.integers(0, 256, size=(96, 128), dtype=np.uint8)
    texture = cv2.GaussianBlur(texture, (3, 3), 0.6)
    source_frame = cv2.cvtColor(texture, cv2.COLOR_GRAY2BGR)
    config = StabilizationConfig(
        frame_width=64,
        frame_height=48,
        accepted_ranges=((0.0, 1.0),),
    )
    safe_crop_ratio = 2.0 / config.frame_width
    zoom = 1.0 / (1.0 - safe_crop_ratio)
    center_x = (source_frame.shape[1] - 1) / 2.0
    center_y = (source_frame.shape[0] - 1) / 2.0
    correction = np.asarray(
        [[1.0, 0.0, 4.0], [0.0, 1.0, -2.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    centered_zoom = np.asarray(
        [
            [zoom, 0.0, center_x * (1.0 - zoom)],
            [0.0, zoom, center_y * (1.0 - zoom)],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    candidate_frame = cv2.warpAffine(
        source_frame,
        np.asarray((centered_zoom @ correction)[:2, :], dtype=np.float32),
        (source_frame.shape[1], source_frame.shape[0]),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    source = tmp_path / "scaled translation source.mp4"
    candidate = tmp_path / "scaled translation candidate.mp4"
    _write_verification_video(source, tuple(source_frame for _ in range(8)))
    _write_verification_video(candidate, tuple(candidate_frame for _ in range(8)))
    corrections = tuple(
        MotionTransform(
            timestamp_seconds=index / 8.0,
            translation_x=2.0,
            translation_y=-1.0,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.1,
            semantics="frame_correction",
        )
        for index in range(8)
    )

    measured = NativeMediaMeasurementProvider(
        ffprobe=ffprobe
    ).measure_perceptual_restoration(
        RescueActionKind.STABILIZE,
        source,
        candidate,
        ((0.0, 1.0),),
        ((0.0, 1.0),),
        {
            "method": "anchor_v1",
            "algorithm_version": "1",
            "motion_transforms": [item.model_dump(mode="json") for item in corrections],
            "config": config.model_dump(mode="json"),
        },
        lambda: False,
    )

    assert measured["residual_p90_pixels"] <= config.residual_goal_p90_pixels
    assert measured["crop_ratio"] == pytest.approx(safe_crop_ratio, abs=0.01)


def test_native_anchor_rejects_static_output_that_ignores_expected_corrections(
    tmp_path: Path,
) -> None:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        pytest.skip("FFprobe is required for native video verification")
    assert ffprobe is not None
    texture: NDArray[np.uint8] = np.zeros((96, 128), dtype=np.uint8)
    for y in range(12, 90, 14):
        for x in range(12, 120, 14):
            cv2.circle(texture, (x, y), 3, 180 + (x + y) % 70, -1)
    source_frames = tuple(
        cv2.cvtColor(
            cv2.warpAffine(
                texture,
                np.float32([[1.0, 0.0, float(index)], [0.0, 1.0, 0.0]]),
                (128, 96),
            ),
            cv2.COLOR_GRAY2BGR,
        )
        for index in range(8)
    )
    static = cv2.cvtColor(texture, cv2.COLOR_GRAY2BGR)
    source = tmp_path / "moving source.mp4"
    candidate = tmp_path / "wrong static candidate.mp4"
    _write_verification_video(source, source_frames)
    _write_verification_video(candidate, tuple(static for _ in range(8)))
    config = StabilizationConfig(accepted_ranges=((0.0, 1.0),))
    corrections = tuple(
        MotionTransform(
            timestamp_seconds=index / 8.0,
            translation_x=0.0,
            translation_y=0.0,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.1,
            semantics="frame_correction",
        )
        for index in range(8)
    )

    measured = NativeMediaMeasurementProvider(
        ffprobe=ffprobe
    ).measure_perceptual_restoration(
        RescueActionKind.STABILIZE,
        source,
        candidate,
        ((0.0, 1.0),),
        ((0.0, 1.0),),
        {
            "method": "anchor_v1",
            "algorithm_version": "1",
            "motion_transforms": [item.model_dump(mode="json") for item in corrections],
            "config": config.model_dump(mode="json"),
        },
        lambda: False,
    )

    assert measured["residual_p90_pixels"] > 1.0


def _write_pcm_wave(path: Path, samples: np.ndarray, sample_rate: int) -> None:
    payload = np.clip(np.rint(samples * 32767.0), -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(payload.tobytes())


def test_native_tonal_measurement_uses_50ms_event_and_boundary_windows(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("FFmpeg is required for native tonal verification")
    assert ffmpeg is not None
    sample_rate = 16000
    timeline = np.arange(sample_rate, dtype=np.float64) / sample_rate
    voice = 0.2 * np.sin(2.0 * np.pi * 220.0 * timeline)
    event = (timeline >= 0.2) & (timeline < 0.8)
    source_samples = voice.copy()
    source_samples[event] += 0.35 * np.sin(2.0 * np.pi * 880.0 * timeline[event])
    source = tmp_path / "音频 source.wav"
    candidate = tmp_path / "音频 candidate.wav"
    _write_pcm_wave(source, source_samples, sample_rate)
    _write_pcm_wave(candidate, voice, sample_rate)
    config = TonalInterferenceConfig()
    profile = InterferenceTone(
        start_seconds=0.2,
        end_seconds=0.8,
        center_frequency_hz=880.0,
        confidence=0.95,
        baseline_before_dbfs=-50.0,
        baseline_after_dbfs=-50.0,
        peak_dbfs=-9.0,
        local_peak_over_baseline_db=30.0,
        persistence_window_count=12,
        frequency_standard_deviation_hz=1.0,
        channel_indices=(0,),
        attenuation_target_db=config.attenuation_db,
        render_qualification=TonalRenderQualification(
            boundary_mode="full_interval_v1",
            notch_q=8.0,
            complete_window_count=12,
            minimum_target_reduction_db=25.0,
            maximum_non_target_attenuation_db=0.1,
            maximum_boundary_energy_jump_db=0.1,
            maximum_boundary_crest_jump_db=0.1,
            maximum_boundary_adjacent_delta=0.01,
        ),
    )

    measured = NativeMediaMeasurementProvider(
        ffmpeg=ffmpeg
    ).measure_perceptual_restoration(
        RescueActionKind.DENOISE_AUDIO,
        source,
        candidate,
        ((0.2, 0.8),),
        ((0.2, 0.8),),
        {
            "config": config.model_dump(mode="json"),
            "interference_profiles": [profile.model_dump(mode="json")],
        },
        lambda: False,
    )

    assert measured["range_coverage_ratio"] == pytest.approx(1.0)
    assert measured["measured_windows"] == pytest.approx(12.0)
    assert measured["excluded_transition_windows"] == pytest.approx(0.0)
    assert measured["measured_windows"] + measured[
        "excluded_transition_windows"
    ] == pytest.approx(float(profile.persistence_window_count))
    assert measured["minimum_target_reduction_db"] >= 24.0
    assert measured["minimum_target_margin_db"] >= 0.0
    assert "maximum_boundary_adjacent_delta" in measured
    assert measured["profile_0_measured_windows"] == measured["measured_windows"]
    assert (
        measured["profile_0_minimum_target_reduction_db"]
        == measured["minimum_target_reduction_db"]
    )
    assert (
        measured["profile_0_maximum_boundary_adjacent_delta"]
        == measured["maximum_boundary_adjacent_delta"]
    )

    qualification = profile.render_qualification
    assert qualification is not None
    incomplete = profile.model_copy(
        update={
            "end_seconds": 0.7,
            "render_qualification": qualification.model_copy(
                update={"complete_window_count": 10}
            ),
        }
    )
    with pytest.raises(ValueError, match="exactly cover"):
        NativeMediaMeasurementProvider(ffmpeg=ffmpeg).measure_perceptual_restoration(
            RescueActionKind.DENOISE_AUDIO,
            source,
            candidate,
            ((0.2, 0.8),),
            ((0.2, 0.8),),
            {
                "config": config.model_dump(mode="json"),
                "interference_profiles": [incomplete.model_dump(mode="json")],
            },
            lambda: False,
        )


def test_perceptual_checks_are_required_only_for_present_actions(
    tmp_path: Path,
) -> None:
    report = _verify(tmp_path)

    assert report.required_check_ids == RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
    assert not any(
        check.check_id
        in {
            "deblur_edge_recovery",
            "deblur_ringing",
            "deblur_temporal_consistency",
            "tonal_interference_reduction",
            "tonal_boundary_transient",
            "anchor_stabilization_residual",
        }
        for check in report.checks
    )
    assert report.faithful_status is RescueVerificationStatus.PASSED


def test_perceptual_required_failure_is_isolated_to_its_artifact(
    tmp_path: Path,
) -> None:
    passing = {
        "range_coverage_ratio": 1.0,
        "compared_frames": 24.0,
        "operation_count": 1.0,
        "edge_recovery_passed_operations": 1.0,
        "ringing_passed_operations": 1.0,
        "temporal_passed_operations": 1.0,
        "edge_width_ratio": 0.5,
        "edge_continuity_ratio": 0.95,
        "ringing_ratio": 0.01,
        "noise_gain_ratio": 1.1,
        "temporal_change_ratio": 0.02,
    }
    failing = {
        **passing,
        "ringing_ratio": 0.2,
        "ringing_passed_operations": 0.0,
    }
    report = _verify(
        tmp_path,
        actions=(_deblur_action(),),
        improved_updates={},
        perceptual_measurements={
            ("faithful-rescue.mp4", RescueActionKind.DEBLUR): passing,
            ("improved-viewing.mp4", RescueActionKind.DEBLUR): failing,
        },
    )

    assert report.faithful_status is RescueVerificationStatus.PASSED
    assert report.improved_status is RescueVerificationStatus.NEEDS_REVIEW


def test_check_ids_values_and_order_are_stable(tmp_path: Path) -> None:
    report = _verify(tmp_path, improved_updates={})
    expected_supplementary = (
        "artifact_integrity",
        "audio_loudness",
        "audio_peak",
        "audio_sample_rate",
        "black_regression",
        "fixed_av_offset",
        "flicker_regression",
        "freeze_regression",
        "luma_chroma_side_effects",
        "luma_clipping",
        "noise_side_effects",
        "perceptible_audio_noise_reduction",
        "perceptible_luma_improvement",
        "perceptible_sharpness_improvement",
        "perceptible_stabilization_improvement",
        "sharpness_side_effects",
        "source_mapping",
        "stabilization_crop",
    )
    assert report.required_check_ids == RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
    for artifact in ("faithful", "improved"):
        artifact_checks = tuple(c for c in report.checks if c.artifact == artifact)
        assert tuple(c.check_id for c in artifact_checks[:4]) == (
            "decodable",
            "duration",
            "streams",
            "source_read_only",
        )
        assert tuple(c.check_id for c in artifact_checks[4:]) == expected_supplementary
        assert all(c.measured for c in artifact_checks)


def test_failed_improved_copy_does_not_invalidate_faithful_copy(tmp_path: Path) -> None:
    report = _verify(tmp_path, improved_updates={"complete_decode": False})
    assert report.faithful_status is RescueVerificationStatus.PASSED
    assert report.improved_status is RescueVerificationStatus.FAILED
    assert report.outcome is RescueOutcome.PARTIAL


def test_needs_review_improved_does_not_change_faithful_status(tmp_path: Path) -> None:
    report = _verify(tmp_path, improved_updates={"clipping_ratio": 0.01})
    assert report.faithful_status is RescueVerificationStatus.PASSED
    assert report.improved_status is RescueVerificationStatus.NEEDS_REVIEW
    assert report.outcome is RescueOutcome.NEEDS_REVIEW


def test_improved_luma_requires_perceptible_gain_in_confirmed_range(
    tmp_path: Path,
) -> None:
    action = _action(
        RescueActionKind.ADJUST_LUMA,
        {
            "brightness": 0.06,
            "contrast": 1.02,
            "gamma": 1.4,
            "gamma_weight": 0.85,
            "minimum_perceptible_luma_delta": 0.04,
        },
    )
    ranges = action.source_ranges

    weak = _verify(
        tmp_path / "weak",
        actions=(action,),
        improved_updates={},
        range_updates={
            ("faithful-rescue.mp4", ranges): {
                "luma_p10": 0.03,
                "luma_p50": 0.04,
                "clipping_ratio": 0.0,
                "noise_residual": 0.01,
                "sharpness": 0.01,
            },
            ("improved-viewing.mp4", ranges): {
                "luma_p10": 0.04,
                "luma_p50": 0.05,
                "clipping_ratio": 0.0,
                "noise_residual": 0.01,
                "sharpness": 0.01,
            },
        },
    )
    strong = _verify(
        tmp_path / "strong",
        actions=(action,),
        improved_updates={},
        range_updates={
            ("faithful-rescue.mp4", ranges): {
                "luma_p10": 0.03,
                "luma_p50": 0.04,
                "clipping_ratio": 0.0,
                "noise_residual": 0.01,
                "sharpness": 0.01,
            },
            ("improved-viewing.mp4", ranges): {
                "luma_p10": 0.10,
                "luma_p50": 0.12,
                "clipping_ratio": 0.0,
                "noise_residual": 0.01,
                "sharpness": 0.01,
            },
        },
    )

    assert (
        _check(weak, "improved", "perceptible_luma_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(strong, "improved", "perceptible_luma_improvement").status
        is RescueVerificationStatus.PASSED
    )


@pytest.mark.parametrize(
    ("update", "failed_check_id", "expected_message"),
    [
        (
            {"minimum_luma_delta": 0.039},
            "perceptible_luma_improvement",
            "Observed luma change or clipping falls outside the configured "
            "bounds; manual review is required.",
        ),
        (
            {"maximum_luma_delta": 0.0801},
            "perceptible_luma_improvement",
            "Observed luma change or clipping falls outside the configured "
            "bounds; manual review is required.",
        ),
        (
            {"maximum_noise_increase": 1e-9},
            "noise_side_effects",
            "Observed noise increase exceeds the configured bound; manual review "
            "is required.",
        ),
        (
            {"maximum_chroma_shift": 0.0101},
            "luma_chroma_side_effects",
            "Observed decoded chroma shift exceeds the configured bound; manual "
            "review is required.",
        ),
    ],
)
def test_luma_exact_range_gates_fail_independently(
    tmp_path: Path,
    update: dict[str, float],
    failed_check_id: str,
    expected_message: str,
) -> None:
    action = _action(RescueActionKind.ADJUST_LUMA, {})
    measurement: dict[str, JsonValue] = {
        "range_coverage_ratio": 1.0,
        "expected_frames": 1.0,
        "compared_frames": 1.0,
        "range_count": 1.0,
        "minimum_luma_delta": 0.04,
        "maximum_luma_delta": 0.06,
        "maximum_noise_increase": 0.0,
        "maximum_clipping_increase": 0.0,
        "maximum_chroma_shift": 0.0,
        "maximum_source_control_chroma_shift": 0.001,
        **update,
    }

    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        luma_measurement=measurement,
    )

    failed = _check(report, "improved", failed_check_id)
    assert failed.status is RescueVerificationStatus.NEEDS_REVIEW
    assert failed.message == expected_message
    pass_messages = {
        "perceptible_luma_improvement": (
            "Exact confirmed ranges meet both luma lift and upper-bound limits."
        ),
        "noise_side_effects": (
            "Exact confirmed ranges introduce no noise above the bound."
        ),
        "luma_chroma_side_effects": (
            "Exact confirmed ranges keep decoded chroma shift within the bound."
        ),
    }
    for check_id in {
        "perceptible_luma_improvement",
        "noise_side_effects",
        "luma_chroma_side_effects",
    } - {failed_check_id}:
        passed = _check(report, "improved", check_id)
        assert passed.status is RescueVerificationStatus.PASSED
        assert passed.message == pass_messages[check_id]


def test_luma_measurement_inventory_is_fail_closed(tmp_path: Path) -> None:
    action = _action(RescueActionKind.ADJUST_LUMA, {})
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        luma_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 1.0,
            "compared_frames": 1.0,
            "range_count": 1.0,
        },
    )

    for check_id in (
        "perceptible_luma_improvement",
        "noise_side_effects",
        "luma_chroma_side_effects",
    ):
        check = _check(report, "improved", check_id)
        assert check.status is RescueVerificationStatus.NEEDS_REVIEW
        assert check.message == (
            "Exact confirmed range measurement is unavailable or invalid; manual "
            "review is required."
        )


def test_luma_measurement_failure_keeps_stable_sanitized_error_code(
    tmp_path: Path,
) -> None:
    action = _action(RescueActionKind.ADJUST_LUMA, {})
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        luma_measurement=verification_module._LumaMeasurementError(
            "native_chroma_metadata_invalid"
        ),
    )

    for check_id in (
        "perceptible_luma_improvement",
        "noise_side_effects",
        "luma_chroma_side_effects",
    ):
        check = _check(report, "improved", check_id)
        assert check.status is RescueVerificationStatus.NEEDS_REVIEW
        assert check.message == (
            "Exact confirmed range measurement is unavailable or invalid; manual "
            "review is required."
        )
        assert check.measured["measurement_valid"] is False
        assert check.measured["measurement_error"] == ("native_chroma_metadata_invalid")
        assert "\\" not in str(check.measured)


def test_sharpen_is_non_applicable_to_faithful_but_strict_for_improved(
    tmp_path: Path,
) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "radius": 2,
            "amount": 0.8,
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "maximum_noise_increase": 0.02,
            "scene_baseline_sharpness": 0.05,
            "minimum_recovered_baseline_ratio": 0.2,
        },
    )
    ranges = action.source_ranges
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        range_updates={
            ("source.mp4", ranges): {
                "luma_p10": 0.1,
                "luma_p50": 0.2,
                "clipping_ratio": 0.0,
                "noise_residual": 0.01,
                "sharpness": 0.01,
            },
            ("faithful-rescue.mp4", ranges): {
                "luma_p10": 0.1,
                "luma_p50": 0.2,
                "clipping_ratio": 0.0,
                "noise_residual": 0.02,
                "sharpness": 0.012,
            },
            ("improved-viewing.mp4", ranges): {
                "luma_p10": 0.1,
                "luma_p50": 0.2,
                "clipping_ratio": 0.0,
                "noise_residual": 0.02,
                "sharpness": 0.009,
            },
        },
    )

    faithful = _check(report, "faithful", "perceptible_sharpness_improvement")
    assert faithful.status is RescueVerificationStatus.PASSED
    assert faithful.measured == {
        "applicable": False,
        "reason": "evaluated_after_both_artifacts",
    }
    assert report.faithful_status is RescueVerificationStatus.PASSED
    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_improved_sharpen_uses_exact_faithful_codec_control(
    tmp_path: Path,
) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "radius": 2,
            "amount": 0.8,
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
            "scene_baseline_sharpness": 0.05,
            "minimum_recovered_baseline_ratio": 0.8,
        },
    )
    ranges = action.source_ranges
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        range_updates={
            ("source.mp4", ranges): {
                "noise_residual": 0.01,
                "sharpness": 0.05,
            },
            ("improved-viewing.mp4", ranges): {
                "noise_residual": 0.011,
                "sharpness": 0.039,
            },
        },
        sharpen_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "range_count": 1.0,
            "passing_range_count": 1.0,
            "minimum_aggregate_gain_ratio": 0.02,
            "minimum_improved_frame_fraction": 0.9,
            "maximum_noise_increase": 0.001,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_ringing_ratio": 0.001,
        },
    )

    check = _check(report, "improved", "perceptible_sharpness_improvement")
    assert check.status is RescueVerificationStatus.PASSED
    assert check.measured["reference"] == "runtime_same_generation_visibility_control"
    assert check.measured["minimum_improved_frame_fraction"] == pytest.approx(0.9)


def test_final_sharpen_accepts_exact_selected_qualification_binding(
    tmp_path: Path,
) -> None:
    source_hash = sha256(b"source").hexdigest()
    report = _verify(
        tmp_path,
        plan=_qualified_sharpen_plan(source_hash),
        improved_updates={},
        sharpen_measurement=_EXACT_FINAL_SHARPEN_METRICS,
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.PASSED
    )


@pytest.mark.parametrize(
    ("identity_updates", "metric_updates"),
    [
        ({"baseline_sha256": "f" * 64}, None),
        ({"visibility_control_sha256": "f" * 64}, None),
        ({"candidate_sha256": "f" * 64}, None),
        ({"normalized_pts_digest": "e" * 64}, None),
        ({"stream_topology_digest": "b" * 64}, None),
        ({"inventory_frame_count": 1007}, None),
        (None, {"expected_frames": 95, "compared_frames": 95}),
        (None, {"minimum_aggregate_gain_ratio": 0.11}),
        (None, {"minimum_recovered_baseline_ratio": 0.99}),
        (None, {"minimum_improved_frame_fraction": 0.99}),
        (None, {"maximum_noise_increase": 0.001}),
        (None, {"maximum_edge_overshoot_ratio": 0.001}),
        (None, {"maximum_edge_overshoot_amplitude": 0.001}),
        (None, {"maximum_ringing_ratio": 0.001}),
    ],
    ids=(
        "baseline-hash",
        "visibility-hash",
        "candidate-hash",
        "pts",
        "topology",
        "inventory-count",
        "bounded-frame-inventory",
        "gain",
        "recovery",
        "frame-fraction",
        "noise",
        "overshoot-ratio",
        "overshoot-amplitude",
        "ringing",
    ),
)
def test_final_sharpen_rejects_selected_qualification_runtime_or_metric_drift(
    tmp_path: Path,
    identity_updates: dict[str, JsonValue] | None,
    metric_updates: dict[str, JsonValue] | None,
) -> None:
    source_hash = sha256(b"source").hexdigest()
    report = _verify(
        tmp_path,
        plan=_qualified_sharpen_plan(
            source_hash,
            selected_identity_updates=identity_updates,
            selected_metric_updates=metric_updates,
        ),
        improved_updates={},
        sharpen_measurement=_EXACT_FINAL_SHARPEN_METRICS,
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


@pytest.mark.parametrize(
    ("inventory_frame_count", "expected_status"),
    [
        (1008, RescueVerificationStatus.PASSED),
        (1007, RescueVerificationStatus.NEEDS_REVIEW),
    ],
)
def test_sharpen_recipe_separates_whole_inventory_from_action_frames(
    tmp_path: Path,
    inventory_frame_count: int,
    expected_status: RescueVerificationStatus,
) -> None:
    action = _action(RescueActionKind.SHARPEN, {})
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        sharpen_recipe_updates={"inventory_frame_count": inventory_frame_count},
        sharpen_measurement={
            "inventory_frame_count": 1008,
            "range_coverage_ratio": 1.0,
            "expected_frames": 132.0,
            "compared_frames": 132.0,
            "range_count": 1.0,
            "passing_range_count": 1.0,
            "minimum_aggregate_gain_ratio": 0.1,
            "minimum_recovered_baseline_ratio": 1.0,
            "minimum_improved_frame_fraction": 1.0,
            "maximum_noise_increase": 0.0,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_edge_overshoot_amplitude": 0.0,
            "maximum_ringing_ratio": 0.0,
        },
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is expected_status
    )


def test_sharpen_decision_uses_custom_digest_bound_thresholds(
    tmp_path: Path,
) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "minimum_recovered_baseline_ratio": 0.91,
            "minimum_improved_frame_fraction": 0.95,
            "scene_baseline_sharpness": 0.05,
            "maximum_noise_increase": 0.007,
            "maximum_edge_overshoot_ratio": 0.02,
            "maximum_ringing_ratio": 0.03,
        },
    )
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        sharpen_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "range_count": 1.0,
            "passing_range_count": 1.0,
            "minimum_aggregate_gain_ratio": 0.02,
            "minimum_recovered_baseline_ratio": 0.90,
            "minimum_improved_frame_fraction": 0.94,
            "maximum_noise_increase": 0.008,
            "maximum_edge_overshoot_ratio": 0.021,
            "maximum_edge_overshoot_amplitude": 0.0,
            "maximum_ringing_ratio": 0.031,
        },
    )

    check = _check(report, "improved", "perceptible_sharpness_improvement")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    assert "manual review is required" in check.message
    assert check.measured["thresholds"] == {
        "minimum_aggregate_gain_ratio": 0.01,
        "minimum_recovered_baseline_ratio": 0.91,
        "minimum_improved_frame_fraction": 0.95,
        "decoded_scene_baseline_sharpness": 0.05,
        "maximum_noise_increase": 0.007,
        "maximum_edge_overshoot_ratio": 0.02,
        "maximum_edge_overshoot_amplitude": 0.05,
        "maximum_ringing_ratio": 0.03,
    }


@pytest.mark.parametrize(
    "measurement_update",
    [
        {
            "passing_range_count": 0.0,
            "minimum_aggregate_gain_ratio": 0.0,
        },
        {"minimum_improved_frame_fraction": 0.79},
        {"maximum_noise_increase": 0.021},
        {"maximum_edge_overshoot_ratio": 0.051},
        {"maximum_edge_overshoot_amplitude": 0.051},
        {"maximum_ringing_ratio": 0.081},
        {"range_coverage_ratio": 0.99},
        {"compared_frames": 95.0},
    ],
)
def test_sharpen_codec_control_evidence_fails_closed(
    tmp_path: Path,
    measurement_update: dict[str, float],
) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "radius": 2,
            "amount": 0.8,
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
            "minimum_recovered_baseline_ratio": 0.8,
        },
    )
    measurement: dict[str, JsonValue] = {
        "range_coverage_ratio": 1.0,
        "expected_frames": 96.0,
        "compared_frames": 96.0,
        "range_count": 1.0,
        "passing_range_count": 1.0,
        "minimum_aggregate_gain_ratio": 0.02,
        "minimum_improved_frame_fraction": 0.9,
        "maximum_noise_increase": 0.001,
        "maximum_edge_overshoot_ratio": 0.0,
        "maximum_ringing_ratio": 0.001,
        **measurement_update,
    }
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        sharpen_measurement=measurement,
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_sharpen_multi_range_requires_every_range_to_pass(tmp_path: Path) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "radius": 2,
            "amount": 0.8,
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
            "minimum_recovered_baseline_ratio": 0.8,
        },
    ).model_copy(update={"source_ranges": ((0.0, 2.0), (2.0, 4.0))})
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        sharpen_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "range_count": 2.0,
            "passing_range_count": 1.0,
            "minimum_aggregate_gain_ratio": 0.02,
            "minimum_improved_frame_fraction": 0.9,
            "maximum_noise_increase": 0.001,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_ringing_ratio": 0.001,
        },
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


@pytest.mark.parametrize(
    "mappings",
    [
        (SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
        (SourceMapping(0.0, 3.5, 0.0, 3.5, "faithful-rescue.mp4"),),
    ],
)
def test_sharpen_removed_or_partial_source_obligation_fails_closed(
    tmp_path: Path,
    mappings: tuple[SourceMapping, ...],
) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "minimum_recovered_baseline_ratio": 0.8,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
        },
    ).model_copy(update={"source_ranges": ((0.0, 2.0), (3.0, 4.0))})
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        mappings=mappings,
        sharpen_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 48.0,
            "compared_frames": 48.0,
            "range_count": 1.0,
            "passing_range_count": 1.0,
            "minimum_aggregate_gain_ratio": 0.02,
            "minimum_improved_frame_fraction": 0.9,
            "maximum_noise_increase": 0.0,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_ringing_ratio": 0.0,
        },
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_sharpen_locked_source_obligation_fails_closed(tmp_path: Path) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "minimum_recovered_baseline_ratio": 0.8,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
        },
    )
    source_hash = sha256(b"source").hexdigest()
    report = _verify(
        tmp_path,
        improved_updates={},
        plan=_plan_with_locked_ranges(source_hash, ((1.0, 2.0),), action),
        sharpen_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "range_count": 1.0,
            "passing_range_count": 1.0,
            "minimum_aggregate_gain_ratio": 0.02,
            "minimum_improved_frame_fraction": 0.9,
            "maximum_noise_increase": 0.0,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_ringing_ratio": 0.0,
        },
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_sharpen_gain_below_decoded_scene_baseline_fails_closed(
    tmp_path: Path,
) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "minimum_recovered_baseline_ratio": 0.8,
            "minimum_improved_frame_fraction": 0.8,
            "scene_baseline_sharpness": 0.05,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
        },
    )
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        sharpen_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "range_count": 1.0,
            "passing_range_count": 1.0,
            "minimum_aggregate_gain_ratio": 4.0,
            "minimum_recovered_baseline_ratio": 0.79,
            "minimum_improved_frame_fraction": 1.0,
            "maximum_noise_increase": 0.0,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_ringing_ratio": 0.0,
        },
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_sharpen_requires_explicit_frame_fraction_and_bound_control_identity(
    tmp_path: Path,
) -> None:
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "minimum_recovered_baseline_ratio": 0.8,
            "scene_baseline_sharpness": 0.05,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
        },
    )
    incomplete_parameters = dict(action.parameters)
    del incomplete_parameters["minimum_improved_frame_fraction"]
    action = action.model_copy(update={"parameters": incomplete_parameters})
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        sharpen_measurement=cast(
            Any,
            {
                "range_coverage_ratio": 1.0,
                "expected_frames": 96.0,
                "compared_frames": 96.0,
                "range_count": 1.0,
                "passing_range_count": 1.0,
                "minimum_aggregate_gain_ratio": 4.0,
                "minimum_recovered_baseline_ratio": 1.0,
                "minimum_improved_frame_fraction": 1.0,
                "maximum_noise_increase": 0.0,
                "maximum_edge_overshoot_ratio": 0.0,
                "maximum_ringing_ratio": 0.0,
                "control_sha256": "0" * 64,
                "candidate_sha256": "1" * 64,
                "control_topology_sha256": "2" * 64,
                "candidate_topology_sha256": "3" * 64,
            },
        ),
    )

    assert (
        _check(report, "improved", "perceptible_sharpness_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def _native_sharpen_fixture(
    *, zero_gain: bool = False
) -> tuple[
    dict[str, JsonValue],
    tuple[tuple[int, int, int, float, np.ndarray, np.ndarray], ...],
    tuple[tuple[int, int, int, float, np.ndarray, np.ndarray], ...],
]:
    source: NDArray[np.uint8] = np.zeros((32, 32), dtype=np.uint8)
    source[:, 16:] = 180
    control = cv2.GaussianBlur(source, (5, 5), 1.0)
    sharpened = cv2.GaussianBlur(source, (3, 3), 0.5)
    candidate = control.copy() if zero_gain else sharpened
    scene_baseline = verification_module._sharpen_frame_metrics(
        source.astype(np.float32)
    )[0]
    parameters: dict[str, JsonValue] = {
        "minimum_perceptible_sharpness_gain_ratio": 0.05,
        "minimum_recovered_baseline_ratio": 0.3,
        "minimum_improved_frame_fraction": 0.8,
        "scene_baseline_sharpness": scene_baseline,
        "maximum_noise_increase": 0.1,
        "maximum_sharpness_loss_ratio": 0.5,
        "edge_gradient_threshold": 0.02,
        "edge_neighborhood_radius": 8,
        "edge_overshoot_minimum_amplitude": 0.01,
        "maximum_edge_overshoot_ratio": 0.05,
        "maximum_edge_overshoot_amplitude": 0.05,
        "ringing_minimum_amplitude": 0.02,
        "maximum_ringing_ratio": 0.08,
    }
    source_control_frames = (
        (0, 2, 0, 0.0, source, control),
        (0, 2, 1, 0.041667, source, control),
    )
    control_candidate_frames = (
        (0, 2, 0, 0.0, control, candidate),
        (0, 2, 1, 0.041667, control, candidate),
    )
    return parameters, source_control_frames, control_candidate_frames


def _patch_native_sharpen_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    source_control_frames: tuple[
        tuple[int, int, int, float, np.ndarray, np.ndarray], ...
    ],
    control_candidate_frames: tuple[
        tuple[int, int, int, float, np.ndarray, np.ndarray], ...
    ],
) -> None:
    timestamps = tuple(item[3] for item in source_control_frames)
    monkeypatch.setattr(
        verification_module,
        "_iter_aligned_perceptual_frames",
        lambda first, *_args, **_kwargs: iter(
            source_control_frames
            if first.name == "source.mp4"
            else control_candidate_frames
        ),
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_sharpen_video_topology",
        lambda *_args, **_kwargs: ({"codec_name": "h264"}, "a" * 64),
    )
    monkeypatch.setattr(
        verification_module,
        "_stream_hash",
        lambda path: sha256(path.name.encode("utf-8")).hexdigest(),
    )
    monkeypatch.setattr(
        verification_module,
        "_probe_video_timestamp_inventory",
        lambda *_args, **_kwargs: verification_module._VideoTimestampInventory(
            timestamps=timestamps,
            stream_start_seconds=0.0,
        ),
    )


@pytest.mark.parametrize(
    ("zero_gain", "expected_passing_ranges"),
    [(False, 1.0), (True, 0.0)],
)
def test_native_sharpen_measurement_uses_same_codec_control(
    monkeypatch: pytest.MonkeyPatch,
    zero_gain: bool,
    expected_passing_ranges: float,
) -> None:
    parameters, source_control_frames, control_candidate_frames = (
        _native_sharpen_fixture(zero_gain=zero_gain)
    )
    _patch_native_sharpen_dependencies(
        monkeypatch, source_control_frames, control_candidate_frames
    )

    measured = verification_module._measure_sharpen_improvement(
        Path("source.mp4"),
        Path("faithful.mp4"),
        Path("improved.mp4"),
        ((0.0, 2.0),),
        ((0.0, 2.0),),
        parameters,
        "ffprobe",
        cast(Any, None),
        1.0,
        lambda: False,
    )

    assert measured["range_coverage_ratio"] == pytest.approx(1.0)
    assert measured["passing_range_count"] == pytest.approx(expected_passing_ranges)
    if not zero_gain:
        assert _json_number(measured["minimum_recovered_baseline_ratio"]) >= 0.3
        assert _json_number(measured["maximum_edge_overshoot_amplitude"]) <= 0.05
        assert _json_number(measured["maximum_ringing_ratio"]) <= 0.08


def test_native_sharpen_measurement_rejects_incomplete_frame_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parameters, source_control_frames, control_candidate_frames = (
        _native_sharpen_fixture()
    )
    incomplete_source_control = tuple(
        (range_index, 3, offset, timestamp, control, candidate)
        for range_index, _count, offset, timestamp, control, candidate in (
            source_control_frames
        )
    )
    incomplete_control_candidate = tuple(
        (range_index, 3, offset, timestamp, control, candidate)
        for range_index, _count, offset, timestamp, control, candidate in (
            control_candidate_frames
        )
    )
    _patch_native_sharpen_dependencies(
        monkeypatch, incomplete_source_control, incomplete_control_candidate
    )

    with pytest.raises(ValueError, match="exact frame coverage"):
        verification_module._measure_sharpen_improvement(
            Path("source.mp4"),
            Path("faithful.mp4"),
            Path("improved.mp4"),
            ((0.0, 2.0),),
            ((0.0, 2.0),),
            parameters,
            "ffprobe",
            cast(Any, None),
            1.0,
            lambda: False,
        )


def test_native_sharpen_alternating_edge_halo_fails_independent_ringing_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source: NDArray[np.uint8] = np.zeros((64, 64), dtype=np.uint8)
    source[:, 32:] = 180
    control = cv2.GaussianBlur(source, (7, 7), 1.5)
    candidate = control.astype(np.int16)
    for index, column in enumerate(range(27, 37)):
        candidate[:, column] += 12 if index % 2 == 0 else -12
    candidate = np.clip(candidate, 2, 252).astype(np.uint8)
    source_control_frames = ((0, 1, 0, 0.0, source, control),)
    control_candidate_frames = ((0, 1, 0, 0.0, control, candidate),)
    _patch_native_sharpen_dependencies(
        monkeypatch, source_control_frames, control_candidate_frames
    )
    scene_baseline = verification_module._sharpen_frame_metrics(
        source.astype(np.float32)
    )[0]

    measured = verification_module._measure_sharpen_improvement(
        Path("source.mp4"),
        Path("faithful.mp4"),
        Path("improved.mp4"),
        ((0.0, 1.0),),
        ((0.0, 1.0),),
        {
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "minimum_recovered_baseline_ratio": 0.1,
            "minimum_improved_frame_fraction": 0.8,
            "scene_baseline_sharpness": scene_baseline,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
            "edge_gradient_threshold": 0.02,
            "edge_neighborhood_radius": 8,
            "edge_overshoot_minimum_amplitude": 0.01,
            "maximum_edge_overshoot_ratio": 0.05,
            "maximum_edge_overshoot_amplitude": 0.05,
            "ringing_minimum_amplitude": 0.02,
            "maximum_ringing_ratio": 0.08,
        },
        "ffprobe",
        cast(Any, None),
        1.0,
        lambda: False,
    )

    assert _json_number(measured["minimum_aggregate_gain_ratio"]) > 0.01
    assert _json_number(measured["maximum_noise_increase"]) < 0.02
    assert _json_number(measured["maximum_ringing_ratio"]) > 0.08
    assert measured["passing_range_count"] == pytest.approx(0.0)


@pytest.mark.parametrize("side_effect", ["clip", "flat_noise"])
def test_native_sharpen_independent_edge_and_noise_gates(
    monkeypatch: pytest.MonkeyPatch,
    side_effect: str,
) -> None:
    source: NDArray[np.uint8] = np.zeros((64, 64), dtype=np.uint8)
    source[:, 32:] = 180
    control = cv2.GaussianBlur(source, (7, 7), 1.5)
    candidate = cv2.GaussianBlur(source, (3, 3), 0.5)
    if side_effect == "clip":
        candidate[:, 32] = 255
    else:
        checkerboard = (np.indices(candidate.shape).sum(axis=0) % 2) * 2 - 1
        flat_mask = np.ones(candidate.shape, dtype=bool)
        flat_mask[:, 22:43] = False
        noisy = candidate.astype(np.int16)
        noisy[flat_mask] += checkerboard[flat_mask] * 8
        candidate = np.clip(noisy, 0, 255).astype(np.uint8)
    source_control_frames = ((0, 1, 0, 0.0, source, control),)
    control_candidate_frames = ((0, 1, 0, 0.0, control, candidate),)
    _patch_native_sharpen_dependencies(
        monkeypatch, source_control_frames, control_candidate_frames
    )
    scene_baseline = verification_module._sharpen_frame_metrics(
        source.astype(np.float32)
    )[0]
    parameters, _source_control, _control_candidate = _native_sharpen_fixture()
    parameters.update(
        {
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "minimum_recovered_baseline_ratio": 0.1,
            "scene_baseline_sharpness": scene_baseline,
            "maximum_noise_increase": 0.01,
        }
    )

    measured = verification_module._measure_sharpen_improvement(
        Path("source.mp4"),
        Path("faithful.mp4"),
        Path("improved.mp4"),
        ((0.0, 1.0),),
        ((0.0, 1.0),),
        parameters,
        "ffprobe",
        cast(Any, None),
        1.0,
        lambda: False,
    )

    assert _json_number(measured["minimum_aggregate_gain_ratio"]) > 0.01
    if side_effect == "clip":
        assert _json_number(measured["maximum_edge_overshoot_amplitude"]) > 0.05
        assert _json_number(measured["maximum_noise_increase"]) <= 0.01
    else:
        assert _json_number(measured["maximum_noise_increase"]) > 0.01
        assert _json_number(measured["maximum_edge_overshoot_amplitude"]) <= 0.05
    assert measured["passing_range_count"] == pytest.approx(0.0)


def test_final_improved_sharpness_uses_faithful_control_after_encoding(
    tmp_path: Path,
) -> None:
    """The final candidate must retain lift over its no-SHARPEN control."""
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "radius": 2,
            "amount": 0.8,
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
            "scene_baseline_sharpness": 0.05,
            "minimum_recovered_baseline_ratio": 0.2,
        },
    )
    ranges = action.source_ranges
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        range_updates={
            ("source.mp4", ranges): {
                "luma_p10": 0.1,
                "luma_p50": 0.2,
                "clipping_ratio": 0.0,
                "noise_residual": 0.01,
                "sharpness": 0.01,
            },
            ("faithful-rescue.mp4", ranges): {
                "luma_p10": 0.1,
                "luma_p50": 0.2,
                "clipping_ratio": 0.0,
                "noise_residual": 0.02,
                "sharpness": 0.012,
            },
            ("improved-viewing.mp4", ranges): {
                "luma_p10": 0.1,
                "luma_p50": 0.2,
                "clipping_ratio": 0.0,
                "noise_residual": 0.019,
                "sharpness": 0.0122,
            },
        },
        sharpen_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "range_count": 1.0,
            "passing_range_count": 1.0,
            "minimum_aggregate_gain_ratio": 0.02,
            "minimum_improved_frame_fraction": 1.0,
            "maximum_noise_increase": 0.0,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_ringing_ratio": 0.0,
        },
    )

    check = _check(report, "improved", "perceptible_sharpness_improvement")
    assert check.status is RescueVerificationStatus.PASSED
    assert check.measured["reference"] == "runtime_same_generation_visibility_control"
    assert check.measured["minimum_aggregate_gain_ratio"] == pytest.approx(0.02)


def test_sharpen_zero_gain_against_faithful_control_fails(
    tmp_path: Path,
) -> None:
    """A raw-source baseline cannot hide no lift over the codec control."""
    action = _action(
        RescueActionKind.SHARPEN,
        {
            "radius": 2,
            "amount": 0.8,
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "maximum_noise_increase": 0.02,
            "maximum_sharpness_loss_ratio": 0.1,
            "scene_baseline_sharpness": 0.05,
            "minimum_recovered_baseline_ratio": 0.2,
        },
    )
    ranges = action.source_ranges
    report = _verify(
        tmp_path,
        actions=(action,),
        improved_updates={},
        range_updates={
            ("source.mp4", ranges): {
                "luma_p10": 0.03,
                "luma_p50": 0.04,
                "clipping_ratio": 0.0,
                "noise_residual": 0.01,
                "sharpness": 0.001,
            },
            ("faithful-rescue.mp4", ranges): {
                "luma_p10": 0.08,
                "luma_p50": 0.12,
                "clipping_ratio": 0.0,
                "noise_residual": 0.015,
                "sharpness": 0.005,
            },
            ("improved-viewing.mp4", ranges): {
                "luma_p10": 0.08,
                "luma_p50": 0.12,
                "clipping_ratio": 0.0,
                "noise_residual": 0.015,
                "sharpness": 0.005,
            },
        },
        sharpen_measurement={
            "range_coverage_ratio": 1.0,
            "expected_frames": 96.0,
            "compared_frames": 96.0,
            "range_count": 1.0,
            "passing_range_count": 0.0,
            "minimum_aggregate_gain_ratio": 0.0,
            "minimum_improved_frame_fraction": 0.0,
            "maximum_noise_increase": 0.0,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_ringing_ratio": 0.0,
        },
    )

    check = _check(report, "improved", "perceptible_sharpness_improvement")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    thresholds = cast(dict[str, JsonValue], check.measured["thresholds"])
    assert _json_number(thresholds["minimum_aggregate_gain_ratio"]) == pytest.approx(
        0.01
    )


def test_audio_sample_rate_regression_requires_review(tmp_path: Path) -> None:
    normalize = _action(
        RescueActionKind.NORMALIZE_AUDIO,
        {"output_sample_rate_hz": 48000},
    )
    report = _verify(
        tmp_path,
        actions=(normalize,),
        improved_updates={"audio_sample_rate_hz": 96000},
    )

    check = _check(report, "improved", "audio_sample_rate")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW


def test_both_outputs_require_measurable_confirmed_audio_noise_reduction(
    tmp_path: Path,
) -> None:
    denoise = _action(
        RescueActionKind.DENOISE_AUDIO,
        {
            "noise_floor_dbfs": -32.0,
            "maximum_reduction_db": 12.0,
            "minimum_noise_reduction_db": 3.0,
            "output_sample_rate_hz": 48000,
        },
    )
    observed = (
        AudioNoiseInterval(
            start_seconds=0.0,
            end_seconds=4.0,
            rms_dbfs=-32.0,
            spectral_centroid_hz=120.0,
            tone_frequencies_hz=(60.0, 118.0),
            relative_level_delta_db=9.0,
            confidence=0.9,
        ),
    )
    report = _verify(
        tmp_path,
        actions=(denoise,),
        improved_updates={},
        faithful_render_mode="single_reencode",
        audio_noise_updates={
            "source.mp4": observed,
            "faithful-rescue.mp4": (),
            "improved-viewing.mp4": (),
        },
    )

    assert (
        _check(report, "faithful", "perceptible_audio_noise_reduction").status
        is RescueVerificationStatus.PASSED
    )
    assert (
        _check(report, "improved", "perceptible_audio_noise_reduction").status
        is RescueVerificationStatus.PASSED
    )

    not_reduced = _verify(
        tmp_path / "not-reduced",
        actions=(denoise,),
        improved_updates={},
        faithful_render_mode="single_reencode",
        audio_noise_updates={
            "source.mp4": observed,
            "faithful-rescue.mp4": observed,
            "improved-viewing.mp4": observed,
        },
    )
    assert (
        _check(not_reduced, "faithful", "perceptible_audio_noise_reduction").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(not_reduced, "improved", "perceptible_audio_noise_reduction").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_tonal_only_denoise_skips_generic_gate_but_rejects_window_count_mismatch(
    tmp_path: Path,
) -> None:
    action = _tonal_action()
    mismatched_tonal_measurement = _tonal_measurement(measured_windows=158.0)

    report = _verify(
        tmp_path,
        actions=(action,),
        perceptual_measurements={
            (
                "faithful-rescue.mp4",
                RescueActionKind.DENOISE_AUDIO,
            ): mismatched_tonal_measurement,
        },
    )

    generic = _check(report, "faithful", "perceptible_audio_noise_reduction")
    assert generic.status is RescueVerificationStatus.PASSED
    assert generic.measured == {
        "applicable": False,
        "reason": "tonal_only_action_covered_by_required_checks",
    }
    assert (
        _check(report, "faithful", "tonal_interference_reduction").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(report, "faithful", "tonal_boundary_transient").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert report.faithful_status is RescueVerificationStatus.NEEDS_REVIEW


def test_tonal_final_verifier_accepts_exact_complete_profile_inventory(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        actions=(_tonal_action(),),
        perceptual_measurements={
            (
                "faithful-rescue.mp4",
                RescueActionKind.DENOISE_AUDIO,
            ): _tonal_measurement(),
        },
    )

    assert (
        _check(report, "faithful", "tonal_interference_reduction").status
        is RescueVerificationStatus.PASSED
    )
    assert (
        _check(report, "faithful", "tonal_boundary_transient").status
        is RescueVerificationStatus.PASSED
    )
    assert report.faithful_status is RescueVerificationStatus.PASSED


def test_tonal_final_verifier_rejects_self_consistent_v3_metric_drift(
    tmp_path: Path,
) -> None:
    measured = _tonal_measurement(
        minimum_target_reduction_db=26.0,
        minimum_target_margin_db=2.0,
        maximum_non_target_attenuation_db=0.2,
        maximum_boundary_energy_jump_db=0.2,
        profile_0_minimum_target_reduction_db=26.0,
        profile_0_minimum_target_margin_db=2.0,
        profile_0_maximum_non_target_attenuation_db=0.2,
        profile_0_maximum_boundary_energy_jump_db=0.2,
    )

    report = _verify(
        tmp_path,
        actions=(_tonal_action(),),
        perceptual_measurements={
            (
                "faithful-rescue.mp4",
                RescueActionKind.DENOISE_AUDIO,
            ): measured,
        },
    )

    assert (
        _check(report, "faithful", "tonal_interference_reduction").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(report, "faithful", "tonal_boundary_transient").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


@pytest.mark.parametrize(
    "updates",
    (
        {
            "minimum_target_reduction_db": 26.0,
            "minimum_target_margin_db": 2.0,
        },
        {
            "minimum_target_reduction_db": 24.5,
            "minimum_target_margin_db": 0.5,
        },
        {"maximum_non_target_attenuation_db": 0.2},
        {"maximum_non_target_attenuation_db": 0.05},
        {"maximum_boundary_energy_jump_db": 0.2},
        {"maximum_boundary_energy_jump_db": 0.05},
        {"maximum_boundary_crest_jump_db": 2.0},
        {"maximum_boundary_crest_jump_db": 0.5},
        {"maximum_boundary_adjacent_delta": 0.02},
        {"maximum_boundary_adjacent_delta": 0.005},
    ),
)
def test_tonal_final_verifier_rejects_each_v3_metric_drift(
    tmp_path: Path,
    updates: dict[str, float],
) -> None:
    measured = _tonal_measurement(
        **{
            key: value
            for key, value in (
                *updates.items(),
                *((f"profile_0_{key}", value) for key, value in updates.items()),
            )
        }
    )

    report = _verify(
        tmp_path,
        actions=(_tonal_action(),),
        perceptual_measurements={
            (
                "faithful-rescue.mp4",
                RescueActionKind.DENOISE_AUDIO,
            ): measured,
        },
    )

    assert (
        _check(report, "faithful", "tonal_interference_reduction").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(report, "faithful", "tonal_boundary_transient").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_tonal_final_verifier_rejects_selected_combined_metric_mismatch(
    tmp_path: Path,
) -> None:
    plan = _plan_with_tonal_selected_combined_metric_mismatch(
        sha256(b"source").hexdigest()
    )
    measured = _tonal_measurement(
        maximum_non_target_attenuation_db=0.2,
        profile_0_maximum_non_target_attenuation_db=0.2,
    )

    report = _verify(
        tmp_path,
        plan=plan,
        perceptual_measurements={
            (
                "faithful-rescue.mp4",
                RescueActionKind.DENOISE_AUDIO,
            ): measured,
        },
    )

    assert (
        _check(report, "faithful", "tonal_interference_reduction").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(report, "faithful", "tonal_boundary_transient").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


@pytest.mark.parametrize(
    "mutation",
    ("missing", "extra", "profile_count", "excluded", "nonfinite"),
)
def test_tonal_final_verifier_rejects_incomplete_profile_inventory(
    tmp_path: Path,
    mutation: str,
) -> None:
    measured = _tonal_measurement()
    if mutation == "missing":
        measured.pop("profile_0_maximum_boundary_adjacent_delta")
    elif mutation == "extra":
        measured["profile_1_measured_windows"] = 80.0
    elif mutation == "profile_count":
        measured["profile_0_measured_windows"] = 79.0
    elif mutation == "excluded":
        measured["profile_0_excluded_transition_windows"] = 1.0
    else:
        measured["profile_0_maximum_non_target_attenuation_db"] = math.inf

    report = _verify(
        tmp_path,
        actions=(_tonal_action(),),
        perceptual_measurements={
            (
                "faithful-rescue.mp4",
                RescueActionKind.DENOISE_AUDIO,
            ): measured,
        },
    )

    assert (
        _check(report, "faithful", "tonal_interference_reduction").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(report, "faithful", "tonal_boundary_transient").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


@pytest.mark.parametrize(
    ("updates", "check_id"),
    [
        ({"complete_decode": False}, "decodable"),
        ({"video_stream_count": 0}, "streams"),
        ({"video_stream_count": 2}, "streams"),
        ({"audio_stream_count": 0}, "streams"),
        ({"audio_stream_count": 2}, "streams"),
        ({"duration_seconds": 4.251}, "duration"),
    ],
)
def test_required_output_failures_have_failed_precedence(
    tmp_path: Path, updates: dict[str, object], check_id: str
) -> None:
    report = _verify(tmp_path, faithful_updates=updates)
    assert (
        _check(report, "faithful", check_id).status is RescueVerificationStatus.FAILED
    )
    assert report.faithful_status is RescueVerificationStatus.FAILED
    assert report.outcome is RescueOutcome.FAILED


def test_duration_boundary_is_inclusive_at_configured_tolerance(tmp_path: Path) -> None:
    report = _verify(tmp_path, faithful_updates={"duration_seconds": 4.25})
    assert (
        _check(report, "faithful", "duration").status is RescueVerificationStatus.PASSED
    )


def test_source_hash_is_recomputed_and_not_trusted_from_snapshot(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    source_snapshot = _snapshot(source)
    source.write_bytes(b"changed")
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"faithful")
    provider = _FakeMeasurementProvider(
        {source: source_snapshot, faithful: _snapshot(faithful)}
    )
    report = RescueVerifier(measurement_provider=provider).verify(
        source=source,
        faithful=faithful,
        improved=None,
        plan=_plan(source_snapshot.sha256),
        mappings=(SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
    )
    assert (
        _check(report, "faithful", "source_read_only").status
        is RescueVerificationStatus.FAILED
    )


@pytest.mark.parametrize(
    ("updates", "check_id"),
    [
        ({"black_events": 1}, "black_regression"),
        ({"freeze_events": 1}, "freeze_regression"),
        ({"flicker_events": 1}, "flicker_regression"),
        ({"clipping_ratio": 0.01}, "luma_clipping"),
        ({"noise_residual": 0.1}, "noise_side_effects"),
        ({"sharpness": 0.5}, "sharpness_side_effects"),
    ],
)
def test_visual_regressions_require_review(
    tmp_path: Path, updates: dict[str, object], check_id: str
) -> None:
    source_updates = {"sharpness": 1.0} if check_id == "sharpness_side_effects" else {}
    report = _verify(
        tmp_path,
        faithful_updates={**source_updates, **updates},
    )
    # For sharpness, compare a source baseline of 1.0 against candidate 0.5.
    if check_id == "sharpness_side_effects":
        source = tmp_path / "source2.mp4"
        source.write_bytes(b"source")
        faithful = tmp_path / "faithful2.mp4"
        faithful.write_bytes(b"faithful")
        provider = _FakeMeasurementProvider(
            {
                source: _snapshot(source, sharpness=1.0),
                faithful: _snapshot(
                    faithful, relative_path="faithful-rescue.mp4", sharpness=0.5
                ),
            }
        )
        report = RescueVerifier(measurement_provider=provider).verify(
            source=source,
            faithful=faithful,
            improved=None,
            plan=_plan(sha256(source.read_bytes()).hexdigest()),
            mappings=(SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
        )
    assert (
        _check(report, "faithful", check_id).status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert _check(report, "faithful", check_id).measured["applicable"] is True


def test_codec_noise_quantization_tolerance_is_bounded_and_reported(
    tmp_path: Path,
) -> None:
    one_luma_step = 1.0 / 255.0
    within = _verify(
        tmp_path / "within",
        faithful_updates={"noise_residual": one_luma_step},
    )
    outside = _verify(
        tmp_path / "outside",
        faithful_updates={"noise_residual": one_luma_step + 1e-6},
    )

    within_check = _check(within, "faithful", "noise_side_effects")
    assert within_check.status is RescueVerificationStatus.PASSED
    assert _json_number(
        within_check.measured["maximum_residual_increase"]
    ) == pytest.approx(one_luma_step)
    assert (
        _check(outside, "faithful", "noise_side_effects").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_confirmed_denoise_keeps_its_configured_zero_noise_tolerance(
    tmp_path: Path,
) -> None:
    denoise = _action(
        RescueActionKind.DENOISE_VIDEO,
        {"maximum_residual_increase": 0.0},
    )
    report = _verify(
        tmp_path,
        improved_updates={"noise_residual": 1e-9},
        actions=(denoise,),
    )

    check = _check(report, "improved", "noise_side_effects")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    assert _json_number(check.measured["maximum_residual_increase"]) == 0.0


def test_visual_side_effect_comparison_is_not_applicable_across_removed_ranges(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        faithful_updates={
            "duration_seconds": 3.0,
            "black_events": 1,
            "freeze_events": 1,
            "flicker_events": 1,
            "clipping_ratio": 0.5,
            "noise_residual": 0.5,
            "sharpness": 0.0,
        },
        mappings=(
            SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
        ),
        mapped_reference_updates={"sharpness": 1.0},
        plan=_two_segment_luma_plan(sha256(b"source").hexdigest()),
    )

    for check_id in (
        "black_regression",
        "flicker_regression",
        "freeze_regression",
        "luma_clipping",
        "noise_side_effects",
        "sharpness_side_effects",
    ):
        check = _check(report, "faithful", check_id)
        assert check.status is RescueVerificationStatus.NEEDS_REVIEW
        assert check.measured["applicable"] is True
        assert check.measured["reference"] == "retained_source_ranges"
    assert report.faithful_status is RescueVerificationStatus.NEEDS_REVIEW
    assert report.outcome is RescueOutcome.NEEDS_REVIEW


def test_retained_range_visual_reference_allows_normal_partial_faithful(
    tmp_path: Path,
) -> None:
    mappings = (
        SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
    )
    report = _verify(
        tmp_path,
        faithful_updates={"duration_seconds": 3.0},
        mappings=mappings,
        mapped_reference_updates={},
        plan=_two_segment_luma_plan(sha256(b"source").hexdigest()),
    )

    for check_id in (
        "black_regression",
        "flicker_regression",
        "freeze_regression",
        "luma_clipping",
        "noise_side_effects",
        "sharpness_side_effects",
    ):
        check = _check(report, "faithful", check_id)
        assert check.status is RescueVerificationStatus.PASSED
        assert check.measured["applicable"] is True
        assert check.measured["reference"] == "retained_source_ranges"
    assert report.faithful_status is RescueVerificationStatus.PASSED


def test_unavailable_retained_range_reference_requires_review(tmp_path: Path) -> None:
    report = _verify(
        tmp_path,
        faithful_updates={"duration_seconds": 3.0},
        mappings=(
            SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
        ),
        mapped_reference_updates=RuntimeError("measurement unavailable"),
        plan=_two_segment_luma_plan(sha256(b"source").hexdigest()),
    )

    for check_id in (
        "black_regression",
        "flicker_regression",
        "freeze_regression",
        "luma_clipping",
        "noise_side_effects",
        "sharpness_side_effects",
    ):
        check = _check(report, "faithful", check_id)
        assert check.status is RescueVerificationStatus.NEEDS_REVIEW
        assert check.measured["applicable"] is False
        assert check.measured["reason"] == "retained_source_reference_unavailable"


def test_full_source_reencode_uses_codec_aligned_visual_reference(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        faithful_updates={"black_events": 1},
        mapped_reference_updates={"black_events": 0},
        faithful_render_mode="single_reencode",
    )

    check = _check(report, "faithful", "black_regression")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    assert check.measured["applicable"] is True
    assert check.measured["reference"] == "codec_aligned_source"


def test_reencoded_faithful_allows_only_bounded_codec_quantization(
    tmp_path: Path,
) -> None:
    restoration = _action(
        RescueActionKind.SHARPEN,
        {
            "radius": 2,
            "amount": 0.8,
            "minimum_perceptible_sharpness_gain_ratio": 0.01,
            "maximum_noise_increase": 0.02,
        },
    )
    within = _verify(
        tmp_path / "within",
        actions=(restoration,),
        faithful_updates={
            "freeze_events": 12,
            "clipping_ratio": 1.0 / 255.0,
        },
        mapped_reference_updates={
            "freeze_events": 10,
            "clipping_ratio": 0.0,
        },
        faithful_render_mode="single_reencode",
    )
    outside = _verify(
        tmp_path / "outside",
        actions=(restoration,),
        faithful_updates={
            "freeze_events": 13,
            "clipping_ratio": (1.0 / 255.0) + 1e-6,
        },
        mapped_reference_updates={
            "freeze_events": 10,
            "clipping_ratio": 0.0,
        },
        faithful_render_mode="single_reencode",
    )

    assert (
        _check(within, "faithful", "freeze_regression").status
        is RescueVerificationStatus.PASSED
    )
    assert (
        _check(within, "faithful", "luma_clipping").status
        is RescueVerificationStatus.PASSED
    )
    assert (
        _check(within, "faithful", "freeze_regression").measured[
            "codec_event_tolerance"
        ]
        == 2
    )
    assert _check(within, "faithful", "luma_clipping").measured[
        "maximum_increase"
    ] == pytest.approx(1.0 / 255.0)
    assert (
        _check(outside, "faithful", "freeze_regression").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )
    assert (
        _check(outside, "faithful", "luma_clipping").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_improved_visual_side_effects_use_faithful_restored_baseline(
    tmp_path: Path,
) -> None:
    report = _verify(
        tmp_path,
        faithful_updates={"sharpness": 0.8},
        improved_updates={"sharpness": 0.8},
        mapped_reference_updates={"sharpness": 1.0},
        faithful_render_mode="single_reencode",
    )

    check = _check(report, "improved", "sharpness_side_effects")
    assert check.status is RescueVerificationStatus.PASSED
    assert check.measured["reference"] == "faithful_restored_baseline"


def _fixed_offset_action() -> RescueAction:
    return _action(
        RescueActionKind.CORRECT_FIXED_AV_OFFSET,
        {"offset_seconds": 0.4, "audio_shift_seconds": -0.4},
    )


@pytest.mark.parametrize("output_end", [0.4, 40.0])
def test_fixed_offset_rejects_grossly_time_scaled_retained_mapping(
    output_end: float,
) -> None:
    """Capability admission must reject a forged retained-timeline scale."""
    plan = _plan("a" * 64, _fixed_offset_action())
    stretched_mapping = SourceMapping(
        source_start=0.0,
        source_end=4.0,
        output_start=0.0,
        output_end=output_end,
        output_relative_path="faithful-rescue.mp4",
    )

    with pytest.raises(RescueMediaError):
        require_executable_action_scopes(plan, (stretched_mapping,))


def test_fixed_offset_allows_retained_mapping_within_native_duration_tolerance() -> (
    None
):
    plan = _plan("a" * 64, _fixed_offset_action())
    measured_mapping = SourceMapping(
        source_start=0.0,
        source_end=4.0,
        output_start=0.0,
        output_end=4.25,
        output_relative_path="faithful-rescue.mp4",
    )

    require_executable_action_scopes(plan, (measured_mapping,))


def test_fixed_offset_rejects_accumulated_retained_mapping_drift() -> None:
    """Two individually tolerated segments must not accumulate a global scale."""
    plan = _two_segment_fixed_offset_plan("a" * 64)
    mappings = (
        SourceMapping(0.0, 1.0, 0.0, 1.2, "faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.2, 3.4, "faithful-rescue.mp4"),
    )

    with pytest.raises(RescueMediaError):
        require_executable_action_scopes(plan, mappings)


@pytest.mark.parametrize(
    "mappings",
    (
        (SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),),
        (
            SourceMapping(2.0, 4.0, 0.0, 2.0, "faithful-rescue.mp4"),
            SourceMapping(0.0, 1.0, 2.0, 3.0, "faithful-rescue.mp4"),
        ),
    ),
)
def test_local_improvement_rejects_omitted_or_reordered_retained_ranges(
    mappings: tuple[SourceMapping, ...],
) -> None:
    """Every mapped content change must execute the exact retained plan timeline."""
    plan = _two_segment_luma_plan("a" * 64)

    with pytest.raises(RescueMediaError):
        require_executable_action_scopes(plan, mappings)


@pytest.mark.parametrize(
    ("mappings", "duration"),
    (
        ((SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),), 1.0),
        (
            (
                SourceMapping(2.0, 4.0, 0.0, 2.0, "faithful-rescue.mp4"),
                SourceMapping(0.0, 1.0, 2.0, 3.0, "faithful-rescue.mp4"),
            ),
            3.0,
        ),
    ),
)
def test_verifier_rejects_omitted_or_reordered_plan_retained_ranges(
    tmp_path: Path,
    mappings: tuple[SourceMapping, ...],
    duration: float,
) -> None:
    """A self-consistent artifact mapping cannot replace the plan-bound timeline."""
    input_hash = sha256(b"source").hexdigest()
    report = _verify(
        tmp_path,
        faithful_updates={"duration_seconds": duration},
        mappings=mappings,
        mapped_reference_updates={},
        plan=_two_segment_luma_plan(input_hash),
    )

    assert (
        _check(report, "faithful", "source_mapping").status
        is RescueVerificationStatus.FAILED
    )


def test_faithful_fixed_offset_check_uses_native_residual(tmp_path: Path) -> None:
    """Catches checking the shift plan without measuring the faithful artifact."""
    report = _verify(
        tmp_path,
        faithful_updates={
            "av_offset_seconds": 0.01,
            "av_offset_method": "first_usable_packet_timestamp",
            "av_offset_tool_version": "ffprobe test-version",
        },
        actions=(_fixed_offset_action(),),
    )

    check = _check(report, "faithful", "fixed_av_offset")
    assert check.status is RescueVerificationStatus.PASSED
    assert check.measured == {
        "applicable": True,
        "measurement_method": "first_usable_packet_timestamp",
        "tool_version": "ffprobe test-version",
        "planned_offset_seconds": 0.4,
        "planned_shift_seconds": -0.4,
        "observed_residual_seconds": 0.01,
        "tolerance_seconds": 0.04,
    }


def test_fixed_offset_reference_uses_explicit_packet_origin_rendering(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    faithful = tmp_path / "faithful-rescue.mp4"
    source.write_bytes(b"source")
    faithful.write_bytes(b"faithful")
    mappings = (SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),)
    provider = _FakeMeasurementProvider(
        {source: _snapshot(source), faithful: _snapshot(faithful)}
    )

    RescueVerifier(measurement_provider=provider).verify(
        source=source,
        faithful=faithful,
        improved=None,
        plan=_plan(sha256(source.read_bytes()).hexdigest(), _fixed_offset_action()),
        mappings=mappings,
        faithful_render_mode="single_reencode",
    )

    assert provider.mapped_reference_calls == [
        (source, mappings, ReferenceRenderOptions(preserve_packet_origin=True))
    ]


def test_real_fixed_offset_correction_reduces_packet_residual(
    tmp_path: Path,
) -> None:
    """Catches reporting a planned shift without native output packet evidence."""
    filename = "rescue_fixed_av_offset.mp4"
    source = _real_fixture(filename)
    source_hash = sha256(source.read_bytes()).hexdigest()
    manifest = cast(
        dict[str, dict[str, dict[str, Any]]],
        json.loads(REAL_MANIFEST_PATH.read_text(encoding="utf-8")),
    )
    tolerance = float(
        manifest["rescue"][filename]["acceptance"][
            "fixed_offset_residual_tolerance_seconds"
        ]
    )
    assessment_service = LocalRescueAssessmentService(
        sync_provider=lambda *_args, **_kwargs: SyncEventMeasurements(
            audio_events=((1.4, 0.99), (2.4, 0.99), (3.4, 0.99)),
            video_events=((1.0, 0.99), (2.0, 0.99), (3.0, 0.99)),
        )
    )
    pipeline = VideoRescuePipeline(
        RescueConfig(
            output_directory=tmp_path / "fixed offset 中文",
            strategy=RescueStrategy.BALANCED,
        ),
        dependencies=RescuePipelineDependencies(assessment_service=assessment_service),
    )
    preparation = pipeline.prepare(source)
    correction = next(
        action
        for action in preparation.plan.actions
        if action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
    )
    assert _json_number(correction.parameters["offset_seconds"]) == pytest.approx(0.4)
    assert _json_number(correction.parameters["audio_shift_seconds"]) == pytest.approx(
        -0.4
    )
    confirmation = _real_confirmation(preparation)
    pipeline.confirm(preparation, confirmation)

    result = pipeline.execute(preparation, confirmation)

    assert result.verification is not None
    check = _check(result.verification, "faithful", "fixed_av_offset")
    assert check.status is RescueVerificationStatus.PASSED
    assert result.faithful_path is not None and result.faithful_path.is_file()
    assert result.verification.faithful_status is RescueVerificationStatus.PASSED
    assert check.measured["measurement_method"] == "first_usable_packet_timestamp"
    assert check.measured["tool_version"]
    assert _json_number(check.measured["planned_offset_seconds"]) == pytest.approx(0.4)
    assert _json_number(check.measured["planned_shift_seconds"]) == pytest.approx(-0.4)
    assert _json_number(check.measured["tolerance_seconds"]) == tolerance
    assert abs(_json_number(check.measured["observed_residual_seconds"])) <= tolerance
    assert result.improved_path is None
    assert result.public_root is not None
    assert not (result.public_root / "improved-viewing.mp4").exists()
    luma = _check(result.verification, "improved", "perceptible_luma_improvement")
    noise = _check(result.verification, "improved", "noise_side_effects")
    assert luma.status is RescueVerificationStatus.NEEDS_REVIEW
    assert luma.measured["applicable"] is True
    assert luma.measured["measurement_valid"] is True
    assert luma.message == (
        "Observed luma change or clipping falls outside the configured bounds; "
        "manual review is required."
    )
    assert _json_number(luma.measured["minimum_luma_delta"]) == pytest.approx(
        3.0 / 255.0
    )
    luma_thresholds = cast(dict[str, JsonValue], luma.measured["thresholds"])
    assert _json_number(luma_thresholds["minimum_luma_delta"]) == 0.04
    assert noise.status is RescueVerificationStatus.NEEDS_REVIEW
    assert noise.measured["applicable"] is True
    assert noise.measured["measurement_valid"] is True
    assert noise.message == (
        "Observed noise increase exceeds the configured bound; manual review is "
        "required."
    )
    assert _json_number(noise.measured["maximum_noise_increase"]) == pytest.approx(
        8.7932663187398e-05
    )
    noise_thresholds = cast(dict[str, JsonValue], noise.measured["thresholds"])
    assert _json_number(noise_thresholds["maximum_noise_increase"]) == 0.0
    assert result.verification.improved_status is (
        RescueVerificationStatus.NEEDS_REVIEW
    )
    assert result.verification.outcome is RescueOutcome.NEEDS_REVIEW
    assert result.status is RescueStatus.NEEDS_REVIEW
    assert sha256(source.read_bytes()).hexdigest() == source_hash


class _InjectedMiddleDamageScanner:
    """Bind one exact structural deletion while retaining native media execution."""

    def scan(
        self,
        _source: Path,
        source_hash: str,
        metadata: VideoMetadata,
        _config: object,
    ) -> MediaDamageMap:
        intervals = tuple(
            DamageInterval(
                id=make_damage_id(
                    source_hash,
                    "video:0",
                    kind,
                    start_seconds,
                    end_seconds,
                ),
                stream_id="video:0",
                kind=kind,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                measurements={"fixture_evidence": "injected_middle_damage_v1"},
            )
            for kind, start_seconds, end_seconds in (
                (DamageKind.DECODABLE, 0.0, 2.0),
                (DamageKind.UNDECODABLE, 2.0, 3.0),
                (DamageKind.DECODABLE, 3.0, metadata.duration_seconds),
            )
        )
        return MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=metadata.duration_seconds,
            scan_coverage=((0.0, metadata.duration_seconds),),
            intervals=intervals,
        )


def test_real_fixed_offset_survives_two_segment_salvage_concat(
    tmp_path: Path,
) -> None:
    """Catches concat reintroducing A/V offset after middle-range salvage."""
    filename = "rescue_fixed_av_offset.mp4"
    source = _real_fixture(filename)
    source_hash = sha256(source.read_bytes()).hexdigest()
    manifest = cast(
        dict[str, dict[str, dict[str, Any]]],
        json.loads(REAL_MANIFEST_PATH.read_text(encoding="utf-8")),
    )
    tolerance = float(
        manifest["rescue"][filename]["acceptance"][
            "fixed_offset_residual_tolerance_seconds"
        ]
    )
    assessment_service = LocalRescueAssessmentService(
        sync_provider=lambda *_args, **_kwargs: SyncEventMeasurements(
            audio_events=((1.4, 0.99), (2.4, 0.99), (3.4, 0.99)),
            video_events=((1.0, 0.99), (2.0, 0.99), (3.0, 0.99)),
        )
    )
    pipeline = VideoRescuePipeline(
        RescueConfig(
            output_directory=tmp_path / "fixed offset salvage 中文",
            strategy=RescueStrategy.BALANCED,
        ),
        dependencies=RescuePipelineDependencies(
            scanner=_InjectedMiddleDamageScanner(),
            assessment_service=assessment_service,
        ),
    )
    preparation = pipeline.prepare(source)
    correction = next(
        action
        for action in preparation.plan.actions
        if action.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
    )
    assert _json_number(correction.parameters["offset_seconds"]) == pytest.approx(0.4)
    assert _json_number(correction.parameters["audio_shift_seconds"]) == pytest.approx(
        -0.4
    )
    assert preparation.previews is not None
    assert correction.id in preparation.previews.previewed_action_ids
    with pytest.raises(RescueMediaError):
        require_executable_action_scopes(
            preparation.plan,
            (
                SourceMapping(
                    source_start=0.0,
                    source_end=2.0,
                    output_start=0.0,
                    output_end=2.0,
                    output_relative_path="faithful-rescue.mp4",
                ),
            ),
        )
    confirmation = _real_confirmation(preparation)
    pipeline.confirm(preparation, confirmation)

    result = pipeline.execute(preparation, confirmation)

    assert result.failed_source_ranges == ((2.0, 3.0),)
    assert len(result.source_mappings) == 2
    source_ranges = tuple(
        (mapping.source_start, mapping.source_end) for mapping in result.source_mappings
    )
    assert source_ranges[0] == pytest.approx((0.0, 2.0), abs=0.11)
    assert source_ranges[1] == pytest.approx((3.0, 6.0), abs=0.11)
    assert result.technical_report is not None
    check = _check(
        result.technical_report.verification,
        "faithful",
        "fixed_av_offset",
    )
    assert check.status is RescueVerificationStatus.PASSED
    assert check.measured["measurement_method"] == "first_usable_packet_timestamp"
    assert check.measured["tool_version"]
    assert _json_number(check.measured["planned_offset_seconds"]) == pytest.approx(0.4)
    assert _json_number(check.measured["planned_shift_seconds"]) == pytest.approx(-0.4)
    assert _json_number(check.measured["tolerance_seconds"]) == tolerance
    assert abs(_json_number(check.measured["observed_residual_seconds"])) <= tolerance
    assert sha256(source.read_bytes()).hexdigest() == source_hash


def test_fixed_offset_expectation_is_inherited_by_improved_artifact(
    tmp_path: Path,
) -> None:
    """Catches dropping residual verification after faithful-only correction."""
    evidence = {
        "av_offset_seconds": 0.01,
        "av_offset_method": "first_usable_packet_timestamp",
        "av_offset_tool_version": "ffprobe test-version",
    }
    report = _verify(
        tmp_path,
        faithful_updates=evidence,
        improved_updates=evidence,
        actions=(_fixed_offset_action(),),
    )

    assert _check(report, "faithful", "fixed_av_offset").measured["applicable"]
    assert _check(report, "improved", "fixed_av_offset").measured["applicable"]


@pytest.mark.parametrize(
    ("residual", "method", "tool_version", "expected"),
    [
        (None, None, None, RescueVerificationStatus.NEEDS_REVIEW),
        (
            0.04,
            "first_usable_packet_timestamp",
            "ffprobe test-version",
            RescueVerificationStatus.PASSED,
        ),
        (
            0.040001,
            "first_usable_packet_timestamp",
            "ffprobe test-version",
            RescueVerificationStatus.NEEDS_REVIEW,
        ),
    ],
)
def test_fixed_offset_residual_requires_native_evidence_within_tolerance(
    tmp_path: Path,
    residual: float | None,
    method: str | None,
    tool_version: str | None,
    expected: RescueVerificationStatus,
) -> None:
    """Catches inferred passes and an exclusive residual tolerance boundary."""
    report = _verify(
        tmp_path,
        faithful_updates={
            "av_offset_seconds": residual,
            "av_offset_method": method,
            "av_offset_tool_version": tool_version,
        },
        actions=(_fixed_offset_action(),),
    )

    check = _check(report, "faithful", "fixed_av_offset")
    assert check.status is expected
    assert check.measured["tolerance_seconds"] == 0.04


def test_balanced_stabilization_limits_apply_to_both_delivered_artifacts(
    tmp_path: Path,
) -> None:
    stabilize = _action(RescueActionKind.STABILIZE, {"max_crop_ratio": 0.08})
    normalize = _action(
        RescueActionKind.NORMALIZE_AUDIO,
        {
            "target_lufs": -16.0,
            "loudness_tolerance_lu": 1.0,
            "true_peak_limit_dbtp": -1.5,
        },
    )
    report = _verify(
        tmp_path,
        actions=(stabilize, normalize),
        faithful_updates={
            "crop_ratio": 0.5,
            "integrated_lufs": -18.0,
            "true_peak_dbtp": -1.0,
        },
        improved_updates={
            "crop_ratio": 0.09,
            "integrated_lufs": -18.0,
            "true_peak_dbtp": -1.0,
        },
        stabilization_measurement={
            "crop_ratio": 0.09,
            "source_motion_median_pixels": 10.0,
            "output_motion_median_pixels": 8.0,
            "source_motion_p90_pixels": 15.0,
            "output_motion_p90_pixels": 12.0,
            "source_reliable_transforms": 20.0,
            "output_reliable_transforms": 20.0,
        },
    )
    for artifact in ("faithful", "improved"):
        assert (
            _check(report, artifact, "stabilization_crop").status
            is RescueVerificationStatus.NEEDS_REVIEW
        )
        assert (
            _check(report, artifact, "perceptible_stabilization_improvement").status
            is RescueVerificationStatus.NEEDS_REVIEW
        )
    for check_id in ("audio_loudness", "audio_peak"):
        faithful = _check(report, "faithful", check_id)
        assert faithful.status is RescueVerificationStatus.PASSED
        assert faithful.measured["applicable"] is False
        assert (
            _check(report, "improved", check_id).status
            is RescueVerificationStatus.NEEDS_REVIEW
        )
    assert report.faithful_status is RescueVerificationStatus.NEEDS_REVIEW


def test_audio_denoise_does_not_require_unselected_loudness_normalization(
    tmp_path: Path,
) -> None:
    """Catches denoise evidence accidentally activating a normalization gate."""
    denoise = _action(
        RescueActionKind.DENOISE_AUDIO,
        {
            "target_lufs": -16.0,
            "loudness_tolerance_lu": 1.5,
            "true_peak_limit_dbtp": -1.5,
            "noise_reduction_db": 9.0,
            "output_sample_rate_hz": 48000,
        },
    )

    report = _verify(
        tmp_path,
        actions=(denoise,),
        improved_updates={
            "integrated_lufs": -29.0,
            "true_peak_dbtp": -20.0,
            "audio_sample_rate_hz": 48000,
        },
    )

    for check_id in ("audio_loudness", "audio_peak"):
        check = _check(report, "improved", check_id)
        assert check.status is RescueVerificationStatus.PASSED
        assert check.measured["applicable"] is False


def test_missing_native_stabilization_crop_evidence_requires_review(
    tmp_path: Path,
) -> None:
    """Catches treating unavailable observed crop evidence as a measured zero."""
    stabilize = _action(RescueActionKind.STABILIZE, {"max_crop_ratio": 0.08})

    report = _verify(
        tmp_path,
        actions=(stabilize,),
        improved_updates={"crop_ratio": None},
    )

    check = _check(report, "improved", "stabilization_crop")
    assert check.status is RescueVerificationStatus.NEEDS_REVIEW
    assert check.measured["observed_ratio"] is None
    assert check.measured["reason"] == "native_crop_measurement_unavailable"
    assert report.improved_status is RescueVerificationStatus.NEEDS_REVIEW


def test_native_stabilization_requires_measured_motion_reduction_and_crop(
    tmp_path: Path,
) -> None:
    """Catches accepting a crop-only render that did not reduce measured shake."""
    stabilize = _action(
        RescueActionKind.STABILIZE,
        {"max_crop_ratio": 0.12, "minimum_motion_reduction_ratio": 0.5},
    ).model_copy(update={"source_ranges": ((1.0, 3.0),)})
    passed = _verify(
        tmp_path / "native-stabilization-pass",
        actions=(stabilize,),
        improved_updates={},
        stabilization_measurement={
            "crop_ratio": 0.04,
            "source_motion_median_pixels": 10.0,
            "output_motion_median_pixels": 2.0,
            "source_motion_p90_pixels": 15.0,
            "output_motion_p90_pixels": 3.0,
            "source_reliable_transforms": 20.0,
            "output_reliable_transforms": 20.0,
        },
    )
    failed = _verify(
        tmp_path / "native-stabilization-fail",
        actions=(stabilize,),
        improved_updates={},
        stabilization_measurement={
            "crop_ratio": 0.04,
            "source_motion_median_pixels": 10.0,
            "output_motion_median_pixels": 8.0,
            "source_motion_p90_pixels": 15.0,
            "output_motion_p90_pixels": 12.0,
            "source_reliable_transforms": 20.0,
            "output_reliable_transforms": 20.0,
        },
    )

    faithful_motion = _check(
        passed, "faithful", "perceptible_stabilization_improvement"
    )
    assert faithful_motion.status is RescueVerificationStatus.PASSED
    assert faithful_motion.measured["applicable"] is True
    assert (
        _check(passed, "faithful", "stabilization_crop").status
        is RescueVerificationStatus.PASSED
    )
    assert (
        _check(passed, "improved", "stabilization_crop").status
        is RescueVerificationStatus.PASSED
    )
    assert (
        _check(passed, "improved", "perceptible_stabilization_improvement").status
        is RescueVerificationStatus.PASSED
    )
    assert (
        _check(failed, "improved", "perceptible_stabilization_improvement").status
        is RescueVerificationStatus.NEEDS_REVIEW
    )


def test_unmodified_codec_difference_uses_event_regressions_not_pixel_identity(
    tmp_path: Path,
) -> None:
    """Catches lossless re-encoding being mistaken for a new quality event."""
    stabilize = _action(
        RescueActionKind.STABILIZE, {"max_crop_ratio": 0.12}
    ).model_copy(update={"source_ranges": ((1.0, 3.0),)})
    unchanged = ((0.0, 1.0), (3.0, 4.0))
    report = _verify(
        tmp_path / "event-regression",
        actions=(stabilize,),
        improved_updates={},
        range_updates={
            ("faithful-rescue.mp4", unchanged): {
                "black_events": 0.0,
                "freeze_events": 2.0,
                "flicker_events": 0.0,
            },
            ("improved-viewing.mp4", unchanged): {
                "black_events": 0.0,
                "freeze_events": 2.0,
                "flicker_events": 0.0,
            },
        },
    )

    for check_id in ("black_regression", "freeze_regression", "flicker_regression"):
        assert (
            _check(report, "improved", check_id).status
            is RescueVerificationStatus.PASSED
        )


def test_full_range_visual_action_has_no_unmodified_measurement_obligation(
    tmp_path: Path,
) -> None:
    providers: list[_FakeMeasurementProvider] = []
    report = _verify(
        tmp_path,
        plan=_qualified_sharpen_plan(sha256(b"source").hexdigest()),
        improved_updates={},
        sharpen_measurement=_EXACT_FINAL_SHARPEN_METRICS,
        measurement_providers=providers,
    )

    assert len(providers) == 1
    assert providers[0].range_calls == []
    for check_id in ("black_regression", "flicker_regression", "freeze_regression"):
        check = _check(report, "improved", check_id)
        assert check.status is RescueVerificationStatus.PASSED
        assert check.measured == {
            "applicable": False,
            "reason": "no_unmodified_output_ranges",
        }
    assert report.improved_status is RescueVerificationStatus.PASSED


def test_partial_visual_action_measurement_failure_remains_applicable_review(
    tmp_path: Path,
) -> None:
    stabilize = _action(
        RescueActionKind.STABILIZE, {"max_crop_ratio": 0.12}
    ).model_copy(update={"source_ranges": ((1.0, 3.0),)})
    unchanged = ((0.0, 1.0), (3.0, 4.0))
    providers: list[_FakeMeasurementProvider] = []
    report = _verify(
        tmp_path,
        actions=(stabilize,),
        improved_updates={},
        range_updates={("faithful-rescue.mp4", unchanged): RuntimeError("unavailable")},
        measurement_providers=providers,
    )

    assert len(providers) == 1
    assert providers[0].range_calls == [(tmp_path / "faithful-rescue.mp4", unchanged)]
    for check_id in ("black_regression", "flicker_regression", "freeze_regression"):
        check = _check(report, "improved", check_id)
        assert check.status is RescueVerificationStatus.NEEDS_REVIEW
        assert check.measured == {
            "applicable": True,
            "reason": "measurement_unavailable",
        }


@pytest.mark.parametrize(
    ("additional_freezes", "expected_status"),
    [
        (1.0, RescueVerificationStatus.PASSED),
        (2.0, RescueVerificationStatus.PASSED),
        (3.0, RescueVerificationStatus.NEEDS_REVIEW),
    ],
)
def test_improved_unmodified_freezes_allow_bounded_codec_event_tolerance(
    tmp_path: Path,
    additional_freezes: float,
    expected_status: RescueVerificationStatus,
) -> None:
    stabilize = _action(
        RescueActionKind.STABILIZE, {"max_crop_ratio": 0.12}
    ).model_copy(update={"source_ranges": ((1.0, 3.0),)})
    unchanged = ((0.0, 1.0), (3.0, 4.0))
    report = _verify(
        tmp_path,
        actions=(stabilize,),
        improved_updates={},
        range_updates={
            ("faithful-rescue.mp4", unchanged): {
                "black_events": 0.0,
                "freeze_events": 4.0,
                "flicker_events": 0.0,
            },
            ("improved-viewing.mp4", unchanged): {
                "black_events": 0.0,
                "freeze_events": 4.0 + additional_freezes,
                "flicker_events": 0.0,
            },
        },
    )

    check = _check(report, "improved", "freeze_regression")
    assert check.status is expected_status
    assert check.measured["codec_event_tolerance"] == 2


@pytest.mark.parametrize(
    "relative_path", ["../escape.mp4", "C:/private/file.mp4", "/tmp/file.mp4"]
)
def test_artifact_relative_path_is_required_and_safe(
    tmp_path: Path, relative_path: str
) -> None:
    with pytest.raises(ValueError):
        _verify(tmp_path, faithful_updates={"relative_path": relative_path})


def test_artifact_relative_path_must_match_its_public_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _verify(
            tmp_path,
            faithful_updates={"relative_path": "other-safe-name.mp4"},
            improved_updates={"relative_path": "faithful-rescue.mp4"},
        )


def test_mapping_requires_exact_contiguous_complete_source_trace(
    tmp_path: Path,
) -> None:
    invalid_sets = (
        (SourceMapping(0.0, 2.0, 0.0, 1.5, "faithful-rescue.mp4"),),
        (
            SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 2.5, 4.5, "faithful-rescue.mp4"),
        ),
        (SourceMapping(0.0, 4.0, 0.0, 4.0, "../escape.mp4"),),
        (SourceMapping(0.0, 4.0, 0.0, 4.0, "other-safe-name.mp4"),),
    )
    for index, mappings in enumerate(invalid_sets):
        report = _verify(tmp_path / str(index), mappings=mappings)
        assert (
            _check(report, "faithful", "source_mapping").status
            is RescueVerificationStatus.FAILED
        )


def test_mapping_rejects_accumulated_segment_duration_drift(tmp_path: Path) -> None:
    """Independent verification also rejects cumulative mapping time scaling."""
    mappings = (
        SourceMapping(0.0, 1.0, 0.0, 1.2, "faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.2, 3.4, "faithful-rescue.mp4"),
    )

    report = _verify(
        tmp_path,
        faithful_updates={"duration_seconds": 3.4},
        mappings=mappings,
    )

    assert (
        _check(report, "faithful", "source_mapping").status
        is RescueVerificationStatus.FAILED
    )


def test_improved_uses_faithful_source_mapping_without_false_filename_failure(
    tmp_path: Path,
) -> None:
    report = _verify(tmp_path, improved_updates={})
    assert (
        _check(report, "improved", "source_mapping").status
        is RescueVerificationStatus.PASSED
    )


def test_snapshot_rejects_non_finite_or_out_of_range_metrics(tmp_path: Path) -> None:
    path = tmp_path / "x.mp4"
    path.write_bytes(b"x")
    for updates in (
        {"clipping_ratio": float("nan")},
        {"crop_ratio": -0.1},
        {"black_events": -1},
        {"integrated_lufs": float("inf")},
    ):
        with pytest.raises(ValueError):
            _snapshot(path, **updates)


def test_verifier_measures_every_candidate_and_binds_artifact_hashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    faithful = tmp_path / "faithful-rescue.mp4"
    improved = tmp_path / "improved-viewing.mp4"
    for path, content in (
        (source, b"source"),
        (faithful, b"faithful"),
        (improved, b"improved"),
    ):
        path.write_bytes(content)
    provider = _FakeMeasurementProvider(
        {path: _snapshot(path) for path in (source, faithful, improved)}
    )
    report = RescueVerifier(measurement_provider=provider).verify(
        source=source,
        faithful=faithful,
        improved=improved,
        plan=_plan(sha256(source.read_bytes()).hexdigest()),
        mappings=(SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),),
    )
    assert provider.calls == [
        (source, "source"),
        (faithful, "faithful-rescue.mp4"),
        (improved, "improved-viewing.mp4"),
    ]
    assert [(item.relative_path, item.sha256) for item in report.artifacts] == [
        ("faithful-rescue.mp4", sha256(b"faithful").hexdigest()),
        ("improved-viewing.mp4", sha256(b"improved").hexdigest()),
    ]


def test_native_provider_rejects_arbitrary_bytes_when_tools_are_available(
    tmp_path: Path,
) -> None:
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        pytest.skip("native FFmpeg verification tools are unavailable")
    path = tmp_path / "arbitrary.mp4"
    path.write_bytes(b"not a video")
    measured = NativeMediaMeasurementProvider().measure(
        path, "faithful-rescue.mp4", lambda: False
    )
    assert measured.complete_decode is False
    assert measured.video_stream_count == 0


def test_native_provider_decodes_real_generated_fixture_when_available() -> None:
    if shutil.which("ffprobe") is None or shutil.which("ffmpeg") is None:
        pytest.skip("native FFmpeg verification tools are unavailable")
    fixture = Path("tests/fixtures/generated/clean_motion.mp4")
    if not fixture.is_file():
        pytest.skip("generated native fixture is unavailable")
    measured = NativeMediaMeasurementProvider().measure(
        fixture, "faithful-rescue.mp4", lambda: False
    )
    assert measured.complete_decode is True
    assert measured.video_stream_count == 1
    assert measured.duration_seconds > 0
