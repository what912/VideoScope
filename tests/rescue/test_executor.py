"""Tests for staged, source-traceable faithful Rescue execution."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import wave
from collections.abc import Callable, Mapping
from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from time import monotonic, sleep
from typing import Any, cast

import numpy as np
import pytest
from pydantic import JsonValue

import videoscope.rescue as rescue
from videoscope.domain import VideoMetadata
from videoscope.rescue.audio import AudioDenoiseConfig
from videoscope.rescue.commands import build_decode_verification_command
from videoscope.rescue.deblur import (
    BlurKernelEstimate,
    DeblurConfig,
    _boundary_weight,
)
from videoscope.rescue.errors import (
    RescueArtifactError,
    RescueCancelledError,
    RescueInputError,
    RescueMediaError,
)
from videoscope.rescue.executor import (
    CommandResult,
    NativeRescueExecutor,
    RescuedSegment,
    RescueExecutionResult,
    SourceMapping,
    _deblur_operations,
    _measure_audio_noise_windows,
    _stabilization_operation,
    _tonal_operation,
    run_external_command,
)
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    canonical_video_encode_contract,
    make_damage_id,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.planner import _apply_sharpen_qualification, build_rescue_plan
from videoscope.rescue.qualification import (
    SharpenProfileMeasurementV1,
    SharpenQualificationEvidenceV1,
    SharpenQualificationMetricsV1,
    SharpenQualificationThresholdsV1,
    build_sharpen_qualification_evidence,
)
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationAssessment,
    StabilizationConfig,
)
from videoscope.rescue.timeline import timestamp_in_half_open_range
from videoscope.rescue.tonal import (
    InterferenceTone,
    TonalInterferenceConfig,
    TonalRenderQualification,
)
from videoscope.rescue.visual import (
    FlickerCorrectionPlan,
    VisualAssessment,
    VisualEvidence,
    VisualMetrics,
    derive_visual_action_parameters,
)


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _passing_sharpen_qualification(
    draft: RescuePlan,
    config: RescueEffectiveConfig,
    *,
    expected_frames: int,
    decoded_width: int,
    decoded_height: int,
) -> SharpenQualificationEvidenceV1:
    action = next(
        item for item in draft.actions if item.kind is RescueActionKind.SHARPEN
    )
    parameters = action.parameters
    thresholds = SharpenQualificationThresholdsV1(
        minimum_aggregate_gain_ratio=float(
            cast(float, parameters["minimum_perceptible_sharpness_gain_ratio"])
        ),
        minimum_recovered_baseline_ratio=float(
            cast(float, parameters["minimum_recovered_baseline_ratio"])
        ),
        minimum_improved_frame_fraction=float(
            cast(float, parameters["minimum_improved_frame_fraction"])
        ),
        maximum_noise_increase=float(cast(float, parameters["maximum_noise_increase"])),
        maximum_edge_overshoot_ratio=float(
            cast(float, parameters["maximum_edge_overshoot_ratio"])
        ),
        maximum_edge_overshoot_amplitude=float(
            cast(float, parameters["maximum_edge_overshoot_amplitude"])
        ),
        maximum_ringing_ratio=float(cast(float, parameters["maximum_ringing_ratio"])),
    )
    measurements = tuple(
        SharpenProfileMeasurementV1(
            profile=profile,
            baseline_sha256="a" * 64,
            visibility_control_sha256="b" * 64,
            candidate_sha256=character * 64,
            normalized_pts_digest="f" * 64,
            stream_topology_digest="0" * 64,
            decoded_width=decoded_width,
            decoded_height=decoded_height,
            generation_count=1,
            inventory_frame_count=expected_frames,
            metrics=SharpenQualificationMetricsV1(
                range_coverage_ratio=1.0,
                expected_frames=expected_frames,
                compared_frames=expected_frames,
                range_count=1,
                passing_range_count=1,
                minimum_aggregate_gain_ratio=max(
                    1.0, thresholds.minimum_aggregate_gain_ratio
                ),
                minimum_recovered_baseline_ratio=max(
                    1.0, thresholds.minimum_recovered_baseline_ratio
                ),
                minimum_improved_frame_fraction=1.0,
                maximum_noise_increase=0.0,
                maximum_edge_overshoot_ratio=0.0,
                maximum_edge_overshoot_amplitude=0.0,
                maximum_ringing_ratio=0.0,
            ),
            thresholds=thresholds,
        )
        for profile, character in zip(
            config.sharpen_qualification_profiles,
            ("c", "d", "e"),
            strict=True,
        )
    )
    return build_sharpen_qualification_evidence(
        input_hash=draft.input_hash,
        draft_action_id=action.id,
        draft_parameters=action.parameters,
        source_ranges=action.source_ranges,
        output_ranges=action.source_ranges,
        encode_contract=canonical_video_encode_contract(config),
        configured_profiles=config.sharpen_qualification_profiles,
        measurements=measurements,
    )


def _media_probe_json(
    *,
    container_duration: float,
    video_duration: float | None = None,
    video_start: float = 0.0,
    fps: float = 10.0,
    frame_count: int | None = None,
    audio_duration: float | None = None,
    video_codec: str = "h264",
) -> str:
    resolved_video_duration = (
        container_duration if video_duration is None else video_duration
    )
    resolved_frame_count = (
        round(resolved_video_duration * fps) if frame_count is None else frame_count
    )
    rate = f"{round(fps * 1000)}/1000"
    streams: list[dict[str, object]] = [
        {
            "codec_type": "video",
            "codec_name": video_codec,
            "start_time": str(video_start),
            "duration": str(resolved_video_duration),
            "avg_frame_rate": rate,
            "r_frame_rate": rate,
            "nb_frames": str(resolved_frame_count),
        }
    ]
    if audio_duration is not None:
        streams.append(
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "start_time": "0.0",
                "duration": str(audio_duration),
                "sample_rate": "48000",
            }
        )
    return json.dumps(
        {"format": {"duration": str(container_duration)}, "streams": streams},
        separators=(",", ":"),
    )


def test_transition_anchor_operation_maps_every_exact_correction() -> None:
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=2.0 + index / 24.0,
            rotation_degrees=0.0,
            scale=1.0,
            translation_x=float(index % 3 - 1),
            translation_y=0.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            semantics="frame_correction",
        )
        for index in range(96)
    )
    config = StabilizationConfig(accepted_ranges=((2.0, 6.0),))
    mapped, mapped_config = _stabilization_operation(
        {
            "method": "transition_anchor_v1",
            "algorithm_version": "1",
            "estimator_algorithm_version": "transition_anchor_v1",
            "transition_range": [2.0, 3.0],
            "following_anchor_range": [3.0, 6.0],
            "transition_correction_count": 96,
            "motion_transforms": [item.model_dump(mode="json") for item in transforms],
            "config": config.model_dump(mode="json"),
        },
        ((2.0, 6.0),),
        (SourceMapping(2.0, 6.0, 0.0, 4.0, "faithful-rescue.mp4"),),
        expected_version="1",
    )

    assert len(mapped) == 96
    assert tuple(item.timestamp_seconds for item in mapped) == pytest.approx(
        tuple(index / 24.0 for index in range(96)), abs=1e-12
    )
    assert tuple(item.translation_x for item in mapped) == tuple(
        item.translation_x for item in transforms
    )
    assert mapped_config.accepted_ranges == ((0.0, 4.0),)


def test_half_open_membership_never_moves_a_legal_pts_across_a_boundary() -> None:
    config = StabilizationConfig(exact_timestamp_tolerance_seconds=0.001)
    assert config.exact_timestamp_tolerance_seconds == 0.001
    assert timestamp_in_half_open_range(0.9995, 0.0, 1.0)
    assert not timestamp_in_half_open_range(0.9995, 1.0, 2.0)
    assert not timestamp_in_half_open_range(1.0, 0.0, 1.0)
    assert timestamp_in_half_open_range(1.0, 1.0, 2.0)


def test_planner_direct_stabilization_plan_is_accepted_by_executor() -> None:
    source_hash = "8" * 64
    source_range = (0.0, 1.0)
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            rotation_degrees=0.0,
            scale=1.0,
            translation_x=2.0,
            translation_y=0.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            semantics="frame_correction",
        )
        for timestamp in (0.5, 0.9995)
    )
    config = StabilizationConfig(
        frame_width=16,
        frame_height=16,
        accepted_ranges=(source_range,),
        exact_timestamp_tolerance_seconds=0.001,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=16,
            height=16,
            duration_seconds=1.0,
            average_frame_rate=2.0,
            estimated_frame_count=2,
            has_audio=False,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=1.0,
            scan_coverage=((0.0, 1.0),),
            intervals=(
                DamageInterval(
                    id=make_damage_id(
                        source_hash, "video:0", DamageKind.SHAKE, 0.0, 1.0
                    ),
                    stream_id="video:0",
                    kind=DamageKind.SHAKE,
                    start_seconds=0.0,
                    end_seconds=1.0,
                ),
            ),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(max_preview_total_seconds=1.0),
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="Measured direct corrections.",
            crop_ratio=0.02,
            transforms=transforms,
            parameters={
                "method": "transition_anchor_v1",
                "config": config.model_dump(mode="json"),
                "affected_ranges": [[0.0, 1.0]],
            },
        ),
    )
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.STABILIZE
    )
    assert plan.preview_ranges == ((0.5, 1.0),)
    mappings = rescue.preview_source_mappings(
        plan, plan.preview_ranges[0], "faithful-00.mp4"
    )

    mapped, mapped_config = _stabilization_operation(
        action.parameters,
        action.source_ranges,
        mappings,
        expected_version=plan.effective_config.anchor_stabilization_algorithm_version,
    )

    assert tuple(item.timestamp_seconds for item in mapped) == pytest.approx(
        (0.0, 0.4995), abs=1e-12
    )
    assert mapped_config.accepted_ranges == ((0.0, 0.5),)


def _mapped_transition_corrections(
    timestamps: tuple[float, ...],
    mapping: SourceMapping,
    *,
    source_range: tuple[float, float],
) -> tuple[tuple[MotionTransform, ...], StabilizationConfig]:
    transforms = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            rotation_degrees=0.0,
            scale=1.0,
            translation_x=2.0 if index % 2 else -2.0,
            translation_y=0.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            semantics="frame_correction",
        )
        for index, timestamp in enumerate(timestamps)
    )
    config = StabilizationConfig(
        frame_width=16,
        frame_height=16,
        accepted_ranges=(source_range,),
    )
    return _stabilization_operation(
        {
            "method": "transition_anchor_v1",
            "algorithm_version": "1",
            "estimator_algorithm_version": "transition_anchor_v1",
            "motion_transforms": [item.model_dump(mode="json") for item in transforms],
            "config": config.model_dump(mode="json"),
        },
        (source_range,),
        (mapping,),
        expected_version="1",
    )


def test_transition_anchor_preview_excludes_quantized_half_open_end_correction(
    tmp_path: Path,
) -> None:
    """The planner-snapped r4 endpoint binds exactly 80 decoded corrections."""
    import cv2

    source_timestamps = tuple(32.0 + round(index / 24.0, 6) for index in range(96))
    budget_end = 35.333333333333336
    preview_end = 35.333333
    assert preview_end <= budget_end
    mapping = SourceMapping(
        32.0,
        preview_end,
        0.0,
        preview_end - 32.0,
        "faithful-02.mp4",
    )

    mapped, config = _mapped_transition_corrections(
        source_timestamps,
        mapping,
        source_range=(32.0, 36.0),
    )

    assert len(mapped) == 80
    assert mapped[-1].timestamp_seconds == pytest.approx(79 / 24.0, abs=1e-6)
    assert all(item.timestamp_seconds < mapping.output_end for item in mapped)

    source = tmp_path / "eighty-frames.mp4"
    writer = cv2.VideoWriter(
        str(source), int(getattr(cv2, "VideoWriter_fourcc")(*"mp4v")), 24.0, (16, 16)
    )
    assert writer.isOpened()
    try:
        for index in range(80):
            writer.write(np.full((16, 16, 3), index % 255, dtype=np.uint8))
    finally:
        writer.release()

    output = tmp_path / "stabilized.mp4"

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        shutil.copyfile(arguments[arguments.index("-i") + 1], arguments[-1])
        return CommandResult(returncode=0, stderr_summary="")

    rescue.render_stabilized_video(
        source,
        output,
        mapped,
        config,
        runner=runner,
        cancellation_callback=lambda: False,
        frame_timestamps=tuple(index / 24.0 for index in range(80)),
    )
    assert output.is_file()


def test_transition_anchor_half_open_mapping_handles_non_aligned_and_vfr_pts() -> None:
    non_aligned, _ = _mapped_transition_corrections(
        (1.083333, 1.125, 1.166667, 1.208333, 1.25, 1.291667, 1.333333),
        SourceMapping(1.1, 1.3, 0.0, 0.2, "non-aligned.mp4"),
        source_range=(1.0, 2.0),
    )
    assert tuple(item.timestamp_seconds for item in non_aligned) == pytest.approx(
        (0.025, 0.066667, 0.108333, 0.15, 0.191667), abs=1e-6
    )

    vfr, _ = _mapped_transition_corrections(
        (2.0, 2.041, 2.125, 2.2, 2.333, 2.55, 2.9),
        SourceMapping(2.125, 2.5500004, 0.0, 0.4250004, "vfr.mp4"),
        source_range=(2.0, 3.0),
    )
    assert tuple(item.timestamp_seconds for item in vfr) == pytest.approx(
        (0.0, 0.075, 0.208, 0.425), abs=1e-9
    )


def test_transition_anchor_adjacent_half_open_windows_assign_boundary_once() -> None:
    timestamps = (
        1.25,
        1.291667,
        1.333333,
        1.3333333333333333,
        1.375,
        1.416667,
    )
    boundary = 1.3333333333333333
    left, _ = _mapped_transition_corrections(
        timestamps,
        SourceMapping(1.25, boundary, 0.0, boundary - 1.25, "left.mp4"),
        source_range=(1.0, 2.0),
    )
    right, _ = _mapped_transition_corrections(
        timestamps,
        SourceMapping(boundary, 1.5, 0.0, 1.5 - boundary, "right.mp4"),
        source_range=(1.0, 2.0),
    )

    assert len(left) == 3
    assert len(right) == 3
    assert left[-1].translation_x == -2.0
    assert right[0].translation_x == 2.0


def test_transition_anchor_final_boundary_is_exclusive_and_deterministic() -> None:
    timestamps = (35.916667, 35.958333, 36.0)
    mapping = SourceMapping(35.9, 36.0000004, 0.0, 0.1000004, "final.mp4")

    first, first_config = _mapped_transition_corrections(
        timestamps,
        mapping,
        source_range=(35.0, 37.0),
    )
    second, second_config = _mapped_transition_corrections(
        timestamps,
        mapping,
        source_range=(35.0, 37.0),
    )

    assert len(first) == 3
    assert first == second
    assert first_config == second_config
    assert first[-1].timestamp_seconds == pytest.approx(0.1, abs=1e-9)


def _mapped_tonal_profiles(
    tones: tuple[InterferenceTone, ...],
    source_ranges: tuple[tuple[float, float], ...],
    mappings: tuple[SourceMapping, ...],
) -> tuple[InterferenceTone, ...]:
    config = TonalInterferenceConfig()
    mapped, mapped_config = _tonal_operation(
        {
            "algorithm_version": "1",
            "interference_profiles": [tone.model_dump(mode="json") for tone in tones],
            "config": config.model_dump(mode="json"),
        },
        source_ranges,
        mappings,
        expected_version="1",
    )
    assert mapped_config == config
    return mapped


def _tonal_profile(start: float, end: float, frequency_hz: float) -> InterferenceTone:
    return InterferenceTone(
        start_seconds=start,
        end_seconds=end,
        center_frequency_hz=frequency_hz,
        confidence=0.95,
        baseline_before_dbfs=-55.0,
        baseline_after_dbfs=-54.0,
        peak_dbfs=-18.0,
        local_peak_over_baseline_db=36.0,
        persistence_window_count=20,
        frequency_standard_deviation_hz=0.5,
        channel_indices=(0, 1),
        attenuation_target_db=24.0,
        render_qualification=TonalRenderQualification(
            boundary_mode="full_interval_v1",
            notch_q=8.0,
            complete_window_count=(math.floor((end - start) / 0.05 + 1e-9) * 2),
            minimum_target_reduction_db=25.0,
            maximum_non_target_attenuation_db=0.1,
            maximum_boundary_energy_jump_db=0.1,
            maximum_boundary_crest_jump_db=0.1,
            maximum_boundary_adjacent_delta=0.01,
        ),
    )


def test_tonal_mapping_clips_only_profiles_with_positive_preview_intersection() -> None:
    first = _tonal_profile(1.0, 2.0, 440.0)
    second = _tonal_profile(3.0, 4.0, 880.0)
    mapping = SourceMapping(1.25, 1.75, 0.0, 0.5, "faithful-00.mp4")

    mapped = _mapped_tonal_profiles(
        (first, second),
        ((1.0, 2.0), (3.0, 4.0)),
        (mapping,),
    )

    assert len(mapped) == 1
    assert mapped[0].center_frequency_hz == 440.0
    assert (mapped[0].start_seconds, mapped[0].end_seconds) == (0.0, 0.5)


def test_tonal_mapping_preserves_adjacent_locked_segments_and_determinism() -> None:
    first = _tonal_profile(1.0, 2.0, 440.0)
    second = _tonal_profile(2.25, 3.0, 880.0)
    mappings = (
        SourceMapping(1.0, 2.0, 0.0, 1.0, "faithful.mp4"),
        SourceMapping(2.25, 3.0, 1.0, 1.75, "faithful.mp4"),
    )

    first_result = _mapped_tonal_profiles(
        (first, second),
        ((1.0, 2.0), (2.25, 3.0)),
        mappings,
    )
    second_result = _mapped_tonal_profiles(
        (first, second),
        ((1.0, 2.0), (2.25, 3.0)),
        mappings,
    )

    assert first_result == second_result
    assert tuple((tone.start_seconds, tone.end_seconds) for tone in first_result) == (
        (0.0, 1.0),
        (1.0, 1.75),
    )


def test_tonal_mapping_rejects_touching_only_preview_without_claiming_profile() -> None:
    tones = (
        _tonal_profile(1.0, 2.0, 440.0),
        _tonal_profile(3.0, 4.0, 880.0),
    )

    with pytest.raises(RescueMediaError) as caught:
        _mapped_tonal_profiles(
            tones,
            ((1.0, 2.0), (3.0, 4.0)),
            (SourceMapping(2.0, 3.0, 0.0, 1.0, "touching.mp4"),),
        )
    assert caught.value.internal_message == (
        "confirmed tonal reduction has no retained interval"
    )


def test_audio_noise_detector_locates_sustained_tonal_interference(
    tmp_path: Path,
) -> None:
    """Catches missing either low hum or a sustained audible narrow-band tone."""
    sample_rate = 8000
    seconds = 42
    timestamps = np.arange(sample_rate * seconds, dtype=np.float64) / sample_rate
    signal = 0.012 * np.sin(2 * np.pi * 220.0 * timestamps)
    hum = (timestamps >= 5.0) & (timestamps < 10.0)
    signal[hum] += 0.025 * np.sin(2 * np.pi * 60.0 * timestamps[hum])
    signal[hum] += 0.020 * np.sin(2 * np.pi * 118.0 * timestamps[hum])
    private_tone = (timestamps >= 25.0) & (timestamps < 32.0)
    signal[private_tone] += 0.080 * np.sin(2 * np.pi * 880.0 * timestamps[private_tone])
    pcm = np.clip(signal * 32767.0, -32768, 32767).astype("<i2")
    path = tmp_path / "bounded-noise.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())

    intervals = _measure_audio_noise_windows(path, AudioDenoiseConfig())

    assert len(intervals) == 2
    assert intervals[0].start_seconds == pytest.approx(5.0, abs=0.5)
    assert intervals[0].end_seconds == pytest.approx(10.0, abs=0.5)
    assert intervals[0].confidence >= 0.8
    assert intervals[0].spectral_centroid_hz < 350.0
    assert intervals[0].tone_frequencies_hz == pytest.approx((60.0, 118.0))
    assert intervals[1].start_seconds == pytest.approx(25.0, abs=0.5)
    assert intervals[1].end_seconds == pytest.approx(32.25, abs=0.01)
    assert intervals[1].confidence >= 0.8
    assert intervals[1].tone_frequencies_hz[0] == pytest.approx(880.0)


def _measured_dark_assessment() -> VisualAssessment:
    return VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.05,
            luma_p50=0.08,
            luma_p90=0.12,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.0,
            sharpness=0.1,
        ),
        recommended_actions=(RescueActionKind.ADJUST_LUMA,),
        evidence=(
            VisualEvidence(
                action=RescueActionKind.ADJUST_LUMA,
                timestamp_seconds=0.75,
                metric="luma_p10",
                observed=0.05,
                threshold=0.18,
                context_luma_p50=0.08,
            ),
        ),
        preview_required=True,
        public_explanation="Measured dark samples support a preview.",
    )


def _plan(
    source_bytes: bytes,
    *,
    duration_seconds: float = 6.0,
    damage_ranges: tuple[tuple[float, float], ...] = (),
    timestamp_discontinuity: bool = False,
    input_hash_override: str | None = None,
    file_size_bytes: int | None = None,
    locked_ranges: tuple[tuple[float, float], ...] = (),
) -> RescuePlan:
    input_hash = input_hash_override or _sha256_bytes(source_bytes)
    intervals = [
        DamageInterval(
            id=make_damage_id(
                input_hash,
                "video:0",
                DamageKind.UNDECODABLE,
                start,
                end,
            ),
            stream_id="video:0",
            kind=DamageKind.UNDECODABLE,
            start_seconds=start,
            end_seconds=end,
        )
        for start, end in damage_ranges
    ]
    if timestamp_discontinuity:
        intervals.append(
            DamageInterval(
                id=make_damage_id(
                    input_hash,
                    "video:0",
                    DamageKind.TIMESTAMP_DISCONTINUITY,
                    2.0,
                    2.1,
                ),
                stream_id="video:0",
                kind=DamageKind.TIMESTAMP_DISCONTINUITY,
                start_seconds=2.0,
                end_seconds=2.1,
            )
        )
    return build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=duration_seconds,
            average_frame_rate=10.0,
            estimated_frame_count=int(duration_seconds * 10),
            has_audio=False,
            file_size_bytes=(
                len(source_bytes) if file_size_bytes is None else file_size_bytes
            ),
        ),
        damage_map=MediaDamageMap(
            input_hash=input_hash,
            duration_seconds=duration_seconds,
            scan_coverage=((0.0, duration_seconds),),
            intervals=tuple(intervals),
        ),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(locked_ranges=locked_ranges),
        locked_ranges=locked_ranges,
    )


def _bound_content_action(
    plan: RescuePlan,
    *,
    kind: RescueActionKind,
    description: str,
    source_ranges: tuple[tuple[float, float], ...],
    parameters: Mapping[str, JsonValue],
    strategy: RescueStrategy,
) -> RescueAction:
    bound_parameters: dict[str, JsonValue] = {
        **parameters,
        "video_encode_contract": canonical_video_encode_contract(
            plan.effective_config
        ).model_dump(mode="json"),
    }
    return RescueAction(
        id=make_rescue_action_id(
            kind=kind,
            parameters=bound_parameters,
            source_ranges=source_ranges,
            strategy=strategy,
            version="1",
        ),
        version="1",
        kind=kind,
        description=description,
        source_ranges=source_ranges,
        parameters=bound_parameters,
        changes_content=True,
        requires_confirmation=True,
        strategy=strategy,
    )


def _rebuild_internal_draft_plan(plan: RescuePlan, **updates: object) -> RescuePlan:
    """Mirror planner draft construction while recomputing its identity."""
    values: dict[str, object] = {
        field_name: getattr(plan, field_name)
        for field_name in RescuePlan.model_fields
        if field_name != "plan_digest"
    }
    values.update(updates)
    provisional = RescuePlan.model_construct(plan_digest="0" * 64, **cast(Any, values))
    values["plan_digest"] = make_rescue_plan_digest(
        provisional.model_dump(mode="json", exclude={"plan_digest"})
    )
    return RescuePlan.model_construct(**cast(Any, values))


def test_execute_faithful_rejects_tampered_plan_digest_before_runner(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source_bytes = b"original source"
    source.write_bytes(source_bytes)
    plan = _plan(source_bytes)
    object.__setattr__(plan, "plan_digest", "b" * 64)
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        raise AssertionError("tampered plan reached the media runner")

    with pytest.raises(RescueMediaError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            plan, source, tmp_path / "work", lambda: False
        )
    assert runner_calls == []


def test_execute_faithful_draft_flag_rejects_tampered_plan_digest_before_runner(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.mp4"
    source_bytes = b"original source"
    source.write_bytes(source_bytes)
    plan = _plan(source_bytes)
    object.__setattr__(plan, "plan_digest", "b" * 64)
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        raise AssertionError("tampered draft plan reached the media runner")

    with pytest.raises(RescueMediaError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            plan,
            source,
            tmp_path / "work",
            lambda: False,
            _allow_unqualified_sharpen_draft=True,
        )
    assert runner_calls == []


def test_executor_rejects_stale_sharpen_output_range_mapping_before_runner(
    tmp_path: Path,
) -> None:
    source_bytes = b"original source"
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"faithful media")
    base = _plan(
        source_bytes,
        duration_seconds=2.0,
        damage_ranges=((0.0, 0.25),),
    )
    sharpen = _bound_content_action(
        base,
        kind=RescueActionKind.SHARPEN,
        description="Measured bounded sharpening.",
        source_ranges=((0.5, 1.5),),
        parameters={
            "adaptive_strength": 0.32,
            "amount": 0.8,
            "detail_passes": 1,
            "minimum_perceptible_sharpness_gain_ratio": 0.08,
            "minimum_recovered_baseline_ratio": 0.8,
            "minimum_improved_frame_fraction": 0.8,
            "maximum_noise_increase": 0.04,
            "maximum_edge_overshoot_ratio": 0.05,
            "maximum_edge_overshoot_amplitude": 0.05,
            "maximum_ringing_ratio": 0.08,
        },
        strategy=RescueStrategy.BALANCED,
    )
    draft = _rebuild_internal_draft_plan(
        base,
        strategy=RescueStrategy.BALANCED,
        actions=(*base.actions, sharpen),
    )
    evidence = _passing_sharpen_qualification(
        draft,
        draft.effective_config,
        expected_frames=10,
        decoded_width=64,
        decoded_height=64,
    )
    qualified = _apply_sharpen_qualification(
        (sharpen,),
        evidence,
        input_hash=draft.input_hash,
        config=draft.effective_config,
    )[0]
    plan = _rebuild_internal_draft_plan(
        draft,
        actions=tuple(
            qualified if action.id == sharpen.id else action for action in draft.actions
        ),
    )
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        raise AssertionError("stale qualification reached the media runner")

    with pytest.raises(RescueMediaError) as exc_info:
        NativeRescueExecutor(runner=runner).execute_improved_with_controls(
            plan,
            faithful,
            tmp_path / "work",
            lambda: False,
            source_mappings=(
                SourceMapping(0.25, 2.0, 0.0, 1.75, "faithful-rescue.mp4"),
            ),
        )

    assert exc_info.value.internal_message == (
        "confirmed SHARPEN qualification output ranges differ"
    )
    assert runner_calls == []


class WritingRunner:
    """Write controlled command outputs while preserving command ordering."""

    def __init__(
        self,
        *,
        fail_segment: int | None = None,
        reject_probe_for_segment: int | None = None,
        fail_decode_for_segment: int | None = None,
        cancel_during_first_write: bool = False,
        mutate_source: Path | None = None,
        source_codec: str = "h264",
        keyframe_advance_seconds: float = 0.0,
        probe_durations_by_name: dict[str, float] | None = None,
        decode_error_names: set[str] | None = None,
        decode_error_summary: str = "fatal decode error",
        cancel_after_final_decode: bool = False,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_segment = fail_segment
        self.reject_probe_for_segment = reject_probe_for_segment
        self.fail_decode_for_segment = fail_decode_for_segment
        self.cancel_during_first_write = cancel_during_first_write
        self.mutate_source = mutate_source
        self.source_codec = source_codec
        self.keyframe_advance_seconds = keyframe_advance_seconds
        self.probe_durations_by_name = probe_durations_by_name or {}
        self.decode_error_names = decode_error_names or set()
        self.decode_error_summary = decode_error_summary
        self.cancel_after_final_decode = cancel_after_final_decode
        self.cancelled = False
        self.processed_segments = 0
        self.probed_segments = 0
        self.segment_durations: dict[int, float] = {}
        self.media_durations: dict[str, float] = {}

    def __call__(
        self,
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del timeout_seconds, sensitive_paths
        self.calls.append(arguments)
        if "-skip_frame" in arguments:
            interval = arguments[arguments.index("-read_intervals") + 1]
            requested_start = interval.split("%", 1)[0]
            keyframe_start = float(requested_start) + self.keyframe_advance_seconds
            return CommandResult(
                returncode=0,
                stdout_summary=(
                    '{"frames":[{"best_effort_timestamp_time":"'
                    + str(keyframe_start)
                    + '"}]}'
                ),
                stderr_summary="",
            )
        if arguments[0].endswith("ffprobe") or arguments[0] == "ffprobe":
            self.probed_segments += 1
            if self.probed_segments == self.reject_probe_for_segment:
                return CommandResult(
                    returncode=1,
                    stdout_summary="",
                    stderr_summary="sanitized probe failure",
                )
            candidate_name = Path(arguments[-1]).name
            duration = self.probe_durations_by_name.get(
                candidate_name,
                self.media_durations.get(candidate_name, 6.0),
            )
            return CommandResult(
                returncode=0,
                stdout_summary=_media_probe_json(
                    container_duration=duration,
                    video_codec=self.source_codec,
                ),
                stderr_summary="",
            )
        if ("-f", "null") in tuple(zip(arguments, arguments[1:])):
            segment_name = Path(arguments[arguments.index("-i") + 1]).name
            if segment_name in self.decode_error_names:
                return CommandResult(9, self.decode_error_summary, "")
            if segment_name.startswith("segment-"):
                segment_index = int(segment_name.split("-")[1].split(".")[0]) + 1
                if segment_index == self.fail_decode_for_segment:
                    return CommandResult(9, "sanitized decode failure", "")
            if (
                self.cancel_after_final_decode
                and segment_name == "faithful-rescue.partial.mp4"
            ):
                self.cancelled = True
            return CommandResult(0, "", "")
        output = Path(arguments[-1])
        if "segment-" in output.name:
            self.processed_segments += 1
            if self.processed_segments == self.fail_segment:
                return CommandResult(7, "sanitized segment failure", "")
            duration = float(arguments[arguments.index("-t") + 1])
            self.segment_durations[self.processed_segments - 1] = duration
        elif ("-f", "concat") in tuple(zip(arguments, arguments[1:])):
            manifest = Path(arguments[arguments.index("-i") + 1])
            retained_indexes = [
                int(Path(line[6:-1]).name.split("-")[1].split(".")[0])
                for line in manifest.read_text(encoding="utf-8").splitlines()
                if line.startswith("file '")
            ]
            duration = sum(self.segment_durations[index] for index in retained_indexes)
        else:
            duration = 6.0
        self.media_durations[output.name] = duration
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"verified media")
        if self.mutate_source is not None:
            self.mutate_source.write_bytes(b"source changed during execution")
            self.mutate_source = None
        if self.cancel_during_first_write:
            self.cancel_during_first_write = False
            raise RescueCancelledError("cancelled by fake runner")
        assert cancellation_callback() is False
        return CommandResult(0, "", "")


def test_middle_damage_yields_two_traceable_segments(tmp_path: Path) -> None:
    """Catches merging across a damaged middle or losing source/output mapping."""
    source_bytes = b"source remains read only"
    source = tmp_path / "源 视频.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner()

    result = NativeRescueExecutor(runner=runner).execute_faithful(
        plan=_plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source=source,
        work_root=tmp_path / "工作区",
        cancellation_callback=lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (0.0, 2.0),
        (3.0, 6.0),
    ]
    assert [
        (item.output_start, item.output_end) for item in result.source_mappings
    ] == [(0.0, 2.0), (2.0, 5.0)]
    assert all(
        segment.output_relative_path.startswith("staging/")
        for segment in result.segments
    )
    assert result.output_relative_path == "staging/faithful-rescue.mp4"
    assert result.output_path.is_file()
    assert result.failed_source_ranges == ()
    assert source.read_bytes() == source_bytes
    segment_commands = [
        call
        for call in runner.calls
        if call[0] == "ffmpeg" and "segment-" in Path(call[-1]).name
    ]
    assert len(segment_commands) == 2
    assert all("libx264" in call and "-c" not in call for call in segment_commands)


def test_preview_mapping_removes_middle_damage_and_rebases_output() -> None:
    """Catches preview lineage preserving a deleted middle source interval."""
    mappings = rescue.preview_source_mappings(
        _plan(b"source", damage_ranges=((2.0, 3.0),)),
        (1.0, 4.0),
        "faithful-00.mp4",
    )

    assert [
        (item.source_start, item.source_end, item.output_start, item.output_end)
        for item in mappings
    ] == [
        (1.0, 2.0, 0.0, 1.0),
        (3.0, 4.0, 1.0, 2.0),
    ]


def test_locked_undecodable_range_is_retained_while_authorized_peer_is_removed(
    tmp_path: Path,
) -> None:
    source_bytes = b"locked source remains"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)

    result = NativeRescueExecutor(runner=WritingRunner()).execute_faithful(
        plan=_plan(
            source_bytes,
            damage_ranges=((2.0, 3.0), (4.0, 5.0)),
            locked_ranges=((2.0, 3.0),),
        ),
        source=source,
        work_root=tmp_path / "work",
        cancellation_callback=lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (0.0, 4.0),
        (5.0, 6.0),
    ]


def test_clean_remux_uses_stream_copy_only(tmp_path: Path) -> None:
    """Catches unnecessary transcoding on the one safe remux-only path."""
    source_bytes = b"clean source"
    source = tmp_path / "clean.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner()

    result = NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes), source, tmp_path / "work", lambda: False
    )

    ffmpeg_calls = [
        call for call in runner.calls if call[0] == "ffmpeg" and call[-1] != "-"
    ]
    assert len(ffmpeg_calls) == 1
    assert ("-c", "copy") in tuple(zip(ffmpeg_calls[0], ffmpeg_calls[0][1:]))
    assert "libx264" not in ffmpeg_calls[0]
    assert result.segments[0].source_start == 0.0
    assert result.segments[0].source_end == 6.0


def test_single_output_visual_mapping_ignores_longer_audio_tail_for_deblur(
    tmp_path: Path,
) -> None:
    """A container/AAC tail must not shift confirmed visual frame ownership."""
    source_bytes = b"forty two second source"
    source = tmp_path / "非零起点 source.mp4"
    source.write_bytes(source_bytes)
    probe_payload = _media_probe_json(
        container_duration=42.021333,
        video_duration=42.0,
        video_start=0.083008,
        fps=24.0,
        frame_count=1008,
        audio_duration=42.021333,
    )

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(0, "", probe_payload)
        if ("-f", "null") in tuple(zip(arguments, arguments[1:])):
            return CommandResult(0, "", "")
        Path(arguments[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments[-1]).write_bytes(b"verified media")
        return CommandResult(0, "", "")

    execution = NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes, duration_seconds=42.0),
        source,
        tmp_path / "工作区",
        lambda: False,
    )
    estimate = BlurKernelEstimate(
        kernel_kind="box",
        radius=2,
        regularization=0.003,
        confidence=0.93,
        edge_width_before=4.0,
        predicted_edge_width_after=2.0,
        edge_continuity_ratio=0.9,
        reblur_error_ratio=0.01,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )
    config = DeblurConfig(candidate_radii=(2,))
    operations = _deblur_operations(
        {
            "algorithm_version": "1",
            "estimate": estimate.model_dump(mode="json"),
            "config": config.model_dump(mode="json"),
        },
        ((4.75, 10.25),),
        execution.source_mappings,
        expected_version="1",
    )
    mapped_ranges = operations[0][0]
    actual_pts = tuple(index / 24.0 for index in range(1008))
    selected = tuple(
        index
        for index, timestamp in enumerate(actual_pts)
        if mapped_ranges[0][0] <= timestamp < mapped_ranges[0][1]
    )
    weights = tuple(
        _boundary_weight(timestamp, *mapped_ranges[0], 0.15)
        for timestamp in actual_pts
        if mapped_ranges[0][0] <= timestamp < mapped_ranges[0][1]
    )
    expected_weights = tuple(
        _boundary_weight(timestamp, 4.75, 10.25, 0.15)
        for timestamp in actual_pts
        if 4.75 <= timestamp < 10.25
    )

    assert execution.source_mappings == (
        SourceMapping(0.0, 42.0, 0.0, 42.0, "staging/faithful-rescue.mp4"),
    )
    assert mapped_ranges == ((4.75, 10.25),)
    assert selected == tuple(range(114, 246))
    assert weights == pytest.approx(expected_weights, abs=1e-12)


@pytest.mark.parametrize(
    ("container_duration", "video_duration", "audio_duration", "video_start"),
    (
        (42.021333, 42.0, 42.021333, 0.083008),
        (42.0, 42.0, 41.978667, 0.0),
    ),
)
def test_media_probe_uses_cfr_video_timeline_when_audio_or_video_is_longer(
    tmp_path: Path,
    container_duration: float,
    video_duration: float,
    audio_duration: float,
    video_start: float,
) -> None:
    payload = _media_probe_json(
        container_duration=container_duration,
        video_duration=video_duration,
        video_start=video_start,
        fps=24.0,
        frame_count=1008,
        audio_duration=audio_duration,
    )

    def runner(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del arguments, timeout_seconds, sensitive_paths, cancellation_callback
        return CommandResult(0, "", payload)

    duration, sample_rate = NativeRescueExecutor(runner=runner)._probe_media(
        tmp_path / "候选.mp4",
        tmp_path / "源.mp4",
        tmp_path,
        lambda: False,
    )

    assert duration == 42.0
    assert sample_rate == 48000


@pytest.mark.parametrize(
    ("update_stream", "remove_key"),
    (
        ({"avg_frame_rate": "24000/1001", "r_frame_rate": "24/1"}, None),
        ({"duration": "42.05"}, None),
        ({"nb_frames": "1007"}, None),
        ({"start_time": "-0.001"}, None),
        ({}, "duration"),
        ({}, "nb_frames"),
    ),
)
def test_media_probe_rejects_unverified_video_timeline_inventory(
    tmp_path: Path,
    update_stream: dict[str, str],
    remove_key: str | None,
) -> None:
    payload = json.loads(
        _media_probe_json(
            container_duration=42.021333,
            video_duration=42.0,
            video_start=0.083008,
            fps=24.0,
            frame_count=1008,
            audio_duration=42.021333,
        )
    )
    video = payload["streams"][0]
    video.update(update_stream)
    if remove_key is not None:
        video.pop(remove_key)

    def runner(
        arguments: tuple[str, ...],
        *,
        timeout_seconds: float,
        sensitive_paths: tuple[Path, ...],
        cancellation_callback: Callable[[], bool],
    ) -> CommandResult:
        del arguments, timeout_seconds, sensitive_paths, cancellation_callback
        return CommandResult(0, "", json.dumps(payload))

    with pytest.raises(RescueMediaError) as exc_info:
        NativeRescueExecutor(runner=runner)._probe_media(
            tmp_path / "candidate.mp4",
            tmp_path / "source.mp4",
            tmp_path,
            lambda: False,
        )

    assert exc_info.value.internal_message == (
        "media timing probe returned invalid timing"
    )


def test_multiple_deblur_operations_preserve_locked_and_removed_timeline() -> None:
    estimate = BlurKernelEstimate(
        kernel_kind="box",
        radius=2,
        regularization=0.003,
        confidence=0.93,
        edge_width_before=4.0,
        predicted_edge_width_after=2.0,
        edge_continuity_ratio=0.9,
        reblur_error_ratio=0.01,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )
    config = DeblurConfig(candidate_radii=(2,))
    serialized_estimate = estimate.model_dump(mode="json")
    serialized_config = config.model_dump(mode="json")
    mappings = (
        SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),
        SourceMapping(5.0, 10.0, 4.0, 9.0, "faithful-rescue.mp4"),
    )
    parameters: dict[str, JsonValue] = {
        "algorithm_version": "1",
        "operations": [
            {
                "source_ranges": [[2.0, 3.0], [3.5, 4.0]],
                "estimate": serialized_estimate,
                "config": serialized_config,
            },
            {
                "source_ranges": [[5.0, 5.5]],
                "estimate": serialized_estimate,
                "config": serialized_config,
            },
        ],
    }
    source_ranges = ((2.0, 3.0), (3.5, 4.0), (5.0, 5.5))

    operations = _deblur_operations(
        parameters,
        source_ranges,
        mappings,
        expected_version="1",
    )

    assert tuple(ranges for ranges, _estimate, _config in operations) == (
        ((2.0, 3.0), (3.5, 4.0)),
        ((4.0, 4.5),),
    )

    forged = cast(dict[str, JsonValue], json.loads(json.dumps(parameters)))
    forged_operations = cast(list[dict[str, JsonValue]], forged["operations"])
    forged_operations[1]["source_ranges"] = [[5.0, 5.6]]
    with pytest.raises(RescueMediaError) as exc_info:
        _deblur_operations(
            forged,
            source_ranges,
            mappings,
            expected_version="1",
        )
    assert exc_info.value.internal_message == "confirmed deblur parameters are invalid"


def test_remux_only_reencodes_when_source_codec_is_not_mp4_copy_safe(
    tmp_path: Path,
) -> None:
    """Catches stream-copying a codec outside the conservative MP4 set."""
    source_bytes = b"clean but incompatible source"
    source = tmp_path / "clean.webm"
    source.write_bytes(source_bytes)
    runner = WritingRunner(source_codec="vp9")

    NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes), source, tmp_path / "work", lambda: False
    )

    ffmpeg_call = next(call for call in runner.calls if call[0] == "ffmpeg")
    assert "libx264" in ffmpeg_call
    assert ("-c", "copy") not in tuple(zip(ffmpeg_call, ffmpeg_call[1:]))


def test_timestamp_rebuild_reencodes_instead_of_stream_copy(tmp_path: Path) -> None:
    """Catches copying timestamps when the plan explicitly requires rebuilding."""
    source_bytes = b"timestamp source"
    source = tmp_path / "timeline.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner()

    NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes, timestamp_discontinuity=True),
        source,
        tmp_path / "work",
        lambda: False,
    )

    ffmpeg_call = next(call for call in runner.calls if call[0] == "ffmpeg")
    assert "libx264" in ffmpeg_call
    assert ("-c", "copy") not in tuple(zip(ffmpeg_call, ffmpeg_call[1:]))


def test_failed_segment_is_not_retained_but_verified_independent_segment_is(
    tmp_path: Path,
) -> None:
    """Catches retaining an unverified segment or discarding a verified peer."""
    source_bytes = b"partially salvageable source"
    source = tmp_path / "partial.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    result = NativeRescueExecutor(
        runner=WritingRunner(fail_segment=2)
    ).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        work,
        lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (0.0, 2.0)
    ]
    assert result.failed_source_ranges == ((3.0, 6.0),)
    assert (work / "staging/segments/segment-000.mp4").is_file()
    assert not (work / "staging/segments/segment-001.mp4").exists()
    assert not (work / "staging/segments/segment-001.partial.mp4").exists()
    assert result.output_path.is_file()


def test_failed_segment_verification_never_retains_that_segment(tmp_path: Path) -> None:
    """Catches retaining a nonempty but unverified media fragment."""
    source_bytes = b"verification matters"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    result = NativeRescueExecutor(
        runner=WritingRunner(reject_probe_for_segment=1)
    ).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        work,
        lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (3.0, 6.0)
    ]
    assert not (work / "staging/segments/segment-000.mp4").exists()
    assert result.failed_source_ranges == ((0.0, 2.0),)


def test_segment_that_fails_full_decode_is_not_retained(tmp_path: Path) -> None:
    """Catches trusting a structurally valid segment that cannot fully decode."""
    source_bytes = b"decode verification matters"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    result = NativeRescueExecutor(
        runner=WritingRunner(fail_decode_for_segment=1)
    ).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        work,
        lambda: False,
    )

    assert [(item.source_start, item.source_end) for item in result.segments] == [
        (3.0, 6.0)
    ]
    assert result.failed_source_ranges == ((0.0, 2.0),)
    assert not (work / "staging/segments/segment-000.mp4").exists()


def test_strict_decode_error_is_fatal_and_preserves_sanitized_diagnostic(
    tmp_path: Path,
) -> None:
    """Catches FFmpeg decode errors being logged but accepted as verified."""
    source_bytes = b"decode error source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner(
        decode_error_names={"faithful-rescue.partial.mp4"},
        decode_error_summary="invalid packet while decoding",
    )

    with pytest.raises(RescueMediaError) as error:
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes), source, tmp_path / "work", lambda: False
        )

    decode_call = next(
        call for call in runner.calls if ("-f", "null") in tuple(zip(call, call[1:]))
    )
    assert "-xerror" in decode_call
    assert ("-err_detect", "explode") in tuple(zip(decode_call, decode_call[1:]))
    assert ("-max_error_rate", "0") in tuple(zip(decode_call, decode_call[1:]))
    assert error.value.internal_message == "invalid packet while decoding"


def test_source_mappings_use_measured_final_timing_and_final_path(
    tmp_path: Path,
) -> None:
    """Catches requested durations or segment paths leaking into final mappings."""
    source_bytes = b"measured timing source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner(
        probe_durations_by_name={
            "segment-000.partial.mp4": 1.9,
            "segment-001.partial.mp4": 3.1,
            "faithful-rescue.partial.mp4": 5.0,
        }
    )

    result = NativeRescueExecutor(runner=runner).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        tmp_path / "work",
        lambda: False,
    )

    assert [
        (mapping.output_start, mapping.output_end) for mapping in result.source_mappings
    ] == pytest.approx([(0.0, 1.9), (1.9, 5.0)])
    assert {mapping.output_relative_path for mapping in result.source_mappings} == {
        "staging/faithful-rescue.mp4"
    }
    assert [
        (segment.output_start, segment.output_end) for segment in result.segments
    ] == [
        (0.0, 1.9),
        (0.0, 3.1),
    ]


def test_final_duration_outside_tolerance_is_rejected(tmp_path: Path) -> None:
    """Catches publishing a concat whose measured duration contradicts its segments."""
    source_bytes = b"truncated concat source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    runner = WritingRunner(probe_durations_by_name={"faithful-rescue.partial.mp4": 4.0})

    with pytest.raises(RescueMediaError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
            source,
            tmp_path / "work",
            lambda: False,
        )


def test_keyframe_advance_records_the_omitted_prefix_as_failed(tmp_path: Path) -> None:
    """Catches silently dropping valid source time before a later keyframe."""
    source_bytes = b"ordinary gop source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)

    result = NativeRescueExecutor(
        runner=WritingRunner(keyframe_advance_seconds=0.4)
    ).execute_faithful(
        _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
        source,
        tmp_path / "work",
        lambda: False,
    )

    assert result.is_partial is True
    assert result.failed_source_ranges == ((0.0, 0.4), (3.0, 3.4))
    assert [
        (segment.source_start, segment.source_end) for segment in result.segments
    ] == [
        (0.4, 2.0),
        (3.4, 6.0),
    ]


def test_late_cancellation_before_atomic_rename_never_publishes(tmp_path: Path) -> None:
    """Catches cancellation becoming true after final decode but before rename."""
    source_bytes = b"late cancellation source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"
    runner = WritingRunner(cancel_after_final_decode=True)

    with pytest.raises(RescueCancelledError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes),
            source,
            work,
            lambda: runner.cancelled,
        )

    assert not (work / "staging/faithful-rescue.mp4").exists()
    assert not (work / "staging/faithful-rescue.partial.mp4").exists()


def test_source_hash_mismatch_is_rejected_before_any_command(tmp_path: Path) -> None:
    """Catches executing a plan confirmed for different source bytes."""
    source = tmp_path / "source.mp4"
    source.write_bytes(b"different source")
    runner = WritingRunner()

    with pytest.raises(RescueInputError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(b"confirmed source"), source, tmp_path / "work", lambda: False
        )

    assert runner.calls == []
    assert source.read_bytes() == b"different source"


def test_reserved_output_collision_is_rejected_without_touching_source(
    tmp_path: Path,
) -> None:
    """Catches a same-path output replacing the read-only source."""
    source_bytes = b"must survive"
    work = tmp_path / "work"
    source = work / "staging" / "faithful-rescue.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(source_bytes)
    runner = WritingRunner()

    with pytest.raises(RescueArtifactError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes), source, work, lambda: False
        )

    assert runner.calls == []
    assert source.read_bytes() == source_bytes


def test_staging_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """Catches writing through a staging symlink outside the validated work root."""
    source_bytes = b"source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (work / "staging").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    runner = WritingRunner()

    with pytest.raises(RescueArtifactError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes), source, work, lambda: False
        )

    assert runner.calls == []
    assert list(outside.iterdir()) == []


def test_source_change_during_execution_blocks_publication(tmp_path: Path) -> None:
    """Catches publishing a result after the source identity changed mid-run."""
    source_bytes = b"source before"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    with pytest.raises(RescueArtifactError):
        NativeRescueExecutor(
            runner=WritingRunner(mutate_source=source)
        ).execute_faithful(_plan(source_bytes), source, work, lambda: False)

    assert not (work / "staging/faithful-rescue.mp4").exists()
    assert not (work / "staging/faithful-rescue.partial.mp4").exists()


def test_cancellation_keeps_verified_segment_but_never_publishes_partial_final(
    tmp_path: Path,
) -> None:
    """Catches cancellation deleting verified work or publishing an incomplete file."""
    source_bytes = b"source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"
    runner = WritingRunner(cancel_during_first_write=True)

    with pytest.raises(RescueCancelledError):
        NativeRescueExecutor(runner=runner).execute_faithful(
            _plan(source_bytes, damage_ranges=((2.0, 3.0),)),
            source,
            work,
            lambda: False,
        )

    assert not (work / "staging/faithful-rescue.mp4").exists()
    assert not (work / "staging/faithful-rescue.partial.mp4").exists()
    assert not (work / "staging/segments/segment-000.partial.mp4").exists()


def test_external_runner_terminates_child_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches cooperative cancellation leaving an FFmpeg child running."""
    real_popen = subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def tracking_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("videoscope.rescue.executor.subprocess.Popen", tracking_popen)
    started = monotonic()

    with pytest.raises(RescueCancelledError):
        run_external_command(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            timeout_seconds=10.0,
            sensitive_paths=(),
            cancellation_callback=lambda: monotonic() - started > 0.15,
        )

    assert len(processes) == 1
    for _ in range(50):
        if processes[0].poll() is not None:
            break
        sleep(0.02)
    assert processes[0].poll() is not None
    assert monotonic() - started < 3.0


def test_external_runner_uses_shell_false_and_sanitizes_bounded_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Catches shell execution or exposing an input path through stderr."""
    real_popen = subprocess.Popen
    calls: list[dict[str, Any]] = []
    sensitive = tmp_path / "秘密 source.mp4"

    def tracking_popen(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        calls.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("videoscope.rescue.executor.subprocess.Popen", tracking_popen)
    result = run_external_command(
        (
            sys.executable,
            "-c",
            "import sys; sys.stderr.write(sys.argv[1] + 'x' * 20000)",
            str(sensitive),
        ),
        timeout_seconds=5.0,
        sensitive_paths=(sensitive,),
        cancellation_callback=lambda: False,
    )

    assert result.returncode == 0
    assert calls[0]["shell"] is False
    assert str(sensitive) not in result.stderr_summary
    assert len(result.stderr_summary) <= 2003


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("stderr", "expected_returncode"),
    (("", 0), ("fatal decoder report", 1)),
)
def test_external_runner_normalizes_strict_decode_error_output(
    stderr: str, expected_returncode: int
) -> None:
    """A strict decode cannot trust a zero child status when FFmpeg logged errors."""
    result = run_external_command(
        (
            sys.executable,
            "-c",
            "import sys; sys.stderr.write(sys.argv[1])",
            stderr,
            "-xerror",
            "-loglevel",
            "error",
            "-f",
            "null",
        ),
        timeout_seconds=5.0,
        sensitive_paths=(),
        cancellation_callback=lambda: False,
    )

    assert result.returncode == expected_returncode


def _local_video_tools() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        pytest.skip("local FFmpeg and ffprobe are required for Rescue integration")
    assert ffmpeg is not None
    assert ffprobe is not None
    return ffmpeg, ffprobe


def _run_checked(arguments: list[str]) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        arguments,
        shell=False,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        pytest.fail(completed.stderr.decode("utf-8", errors="replace")[:1000])
    return completed


def _corrupted_unicode_fixture(tmp_path: Path, ffmpeg: str) -> Path:
    source = tmp_path / "损坏 媒体" / "源 视频.mp4"
    source.parent.mkdir(parents=True)
    _run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=10:duration=6",
            "-c:v",
            "libx264",
            "-g",
            "10",
            "-keyint_min",
            "10",
            "-sc_threshold",
            "0",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(source),
        ]
    )
    size = source.stat().st_size
    corruption_start = int(size * 0.42)
    corruption_bytes = max(256, int(size * 0.02))
    with source.open("r+b") as handle:
        handle.seek(corruption_start)
        handle.write(b"\0" * corruption_bytes)
    return source


def test_real_unicode_corrupted_media_is_playable_mapped_and_source_unchanged(
    tmp_path: Path,
) -> None:
    """Catches a fake-only executor that cannot salvage real local media."""
    ffmpeg, ffprobe = _local_video_tools()
    source = _corrupted_unicode_fixture(tmp_path, ffmpeg)
    source_hash = _sha256_file(source)
    result = NativeRescueExecutor(ffmpeg=ffmpeg, ffprobe=ffprobe).execute_faithful(
        _plan(
            b"",
            damage_ranges=((2.0, 3.0),),
            input_hash_override=source_hash,
            file_size_bytes=source.stat().st_size,
        ),
        source,
        tmp_path / "Unicode 工作区",
        lambda: False,
    )

    probe = _run_checked(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(result.output_path),
        ]
    )
    payload = json.loads(probe.stdout.decode("utf-8"))
    streams = payload["streams"]
    mapped_duration = sum(
        mapping.source_end - mapping.source_start for mapping in result.source_mappings
    )
    output_duration = float(payload["format"]["duration"])

    assert any(stream["codec_type"] == "video" for stream in streams)
    assert _sha256_file(source) == source_hash
    assert mapped_duration == pytest.approx(5.0)
    assert output_duration == pytest.approx(mapped_duration, abs=0.25)
    assert result.output_relative_path == "staging/faithful-rescue.mp4"


def test_real_corrupted_media_fails_strict_full_decode(tmp_path: Path) -> None:
    """Catches real FFmpeg logging damaged frames but returning success."""
    ffmpeg, _ffprobe = _local_video_tools()
    source = _corrupted_unicode_fixture(tmp_path, ffmpeg)

    result = run_external_command(
        tuple(build_decode_verification_command(source, ffmpeg=ffmpeg)),
        timeout_seconds=30.0,
        sensitive_paths=(source,),
        cancellation_callback=lambda: False,
    )

    assert result.returncode != 0


@pytest.mark.parametrize(
    ("source_profile", "source_rate", "expected_rate"),
    (
        ("baseline", "24", "24/1"),
        ("high", "24", "24/1"),
        ("high", "24000/1001", "24000/1001"),
    ),
)
def test_native_faithful_and_improved_share_canonical_video_topology(
    tmp_path: Path,
    source_profile: str,
    source_rate: str,
    expected_rate: str,
) -> None:
    """Catches CRF-dependent x264 profile drift between one Rescue bundle."""
    fixed_tools = (
        Path(__file__).parents[4]
        / ".release-audit"
        / "tools"
        / "ffmpeg"
        / "ffmpeg-8.1.2-essentials_build"
        / "bin"
    )
    ffmpeg = shutil.which("ffmpeg") or str(fixed_tools / "ffmpeg.exe")
    ffprobe = shutil.which("ffprobe") or str(fixed_tools / "ffprobe.exe")
    if not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        pytest.skip("fixed FFmpeg 8.1.2 is required for the topology regression")
    source = tmp_path / "输入 profile-high 视频.mp4"
    _run_checked(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=96x64:rate={source_rate}:duration=1",
            "-c:v",
            "libx264",
            "-profile:v",
            source_profile,
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(source),
        ]
    )
    source_hash = _sha256_file(source)
    soft = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SOFT_DETAIL, 0.0, 1.0),
        stream_id="video:0",
        kind=DamageKind.SOFT_DETAIL,
        start_seconds=0.0,
        end_seconds=1.0,
    )
    config = RescueEffectiveConfig()
    metadata = VideoMetadata(
        filename=source.name,
        container_format="mp4",
        codec="h264",
        width=96,
        height=64,
        duration_seconds=1.0,
        average_frame_rate=float(Fraction(source_rate)),
        estimated_frame_count=24,
        has_audio=False,
        file_size_bytes=source.stat().st_size,
    )
    damage_map = MediaDamageMap(
        input_hash=source_hash,
        duration_seconds=1.0,
        scan_coverage=((0.0, 1.0),),
        intervals=(soft,),
    )
    visual_assessment = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.1,
            luma_p50=0.4,
            luma_p90=0.9,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.005,
            sharpness=0.04,
        ),
        recommended_actions=(RescueActionKind.SHARPEN,),
        preview_required=True,
        public_explanation="Measured soft detail supports bounded sharpening.",
    )
    draft = build_rescue_plan(
        metadata=metadata,
        damage_map=damage_map,
        strategy=RescueStrategy.BALANCED,
        config=config,
        visual_assessment=visual_assessment,
    )
    qualification = _passing_sharpen_qualification(
        draft,
        config,
        expected_frames=24,
        decoded_width=96,
        decoded_height=64,
    )
    plan = build_rescue_plan(
        metadata=metadata,
        damage_map=damage_map,
        strategy=RescueStrategy.BALANCED,
        config=config,
        visual_assessment=visual_assessment,
        sharpen_qualification=qualification,
        require_sharpen_qualification=True,
    )
    faithful = tmp_path / "faithful stabilized.mp4"
    source_fps = float(Fraction(source_rate))
    timestamps = tuple(index / source_fps for index in range(24))
    corrections = tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            rotation_degrees=0.0,
            scale=1.0,
            translation_x=0.0,
            translation_y=0.0,
            inlier_ratio=0.95,
            residual_pixels=0.1,
            semantics="frame_correction",
        )
        for timestamp in timestamps
    )
    rescue.render_stabilized_video(
        source,
        faithful,
        corrections,
        StabilizationConfig(
            frame_width=96,
            frame_height=64,
            accepted_ranges=((0.0, 1.0),),
        ),
        runner=run_external_command,
        cancellation_callback=lambda: False,
        ffmpeg=ffmpeg,
        frame_timestamps=timestamps,
    )
    improved = NativeRescueExecutor(ffmpeg=ffmpeg, ffprobe=ffprobe).execute_improved(
        plan,
        faithful,
        tmp_path / "工作区",
        lambda: False,
        source_mappings=(SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),),
    )

    def topology(path: Path) -> dict[str, object]:
        result = _run_checked(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                (
                    "stream=codec_name,profile,pix_fmt,level,time_base,"
                    "r_frame_rate,avg_frame_rate"
                ),
                "-of",
                "json",
                str(path),
            ]
        )
        payload = json.loads(result.stdout.decode("utf-8"))
        return cast(dict[str, object], payload["streams"][0])

    faithful_topology = topology(faithful)
    improved_topology = topology(improved)
    assert faithful_topology == improved_topology
    assert faithful_topology == {
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "level": 31,
        "r_frame_rate": expected_rate,
        "avg_frame_rate": expected_rate,
        "time_base": "1/120000",
    }
    assert _sha256_file(source) == source_hash


def test_all_failed_segments_raise_without_final_output(tmp_path: Path) -> None:
    """Catches producing an empty rescue after every independent segment failed."""
    source_bytes = b"unusable"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    work = tmp_path / "work"

    with pytest.raises(RescueMediaError):
        NativeRescueExecutor(runner=WritingRunner(fail_segment=1)).execute_faithful(
            _plan(source_bytes, damage_ranges=((2.0, 6.0),)),
            source,
            work,
            lambda: False,
        )

    assert not (work / "staging/faithful-rescue.mp4").exists()
    assert not any((work / "staging/segments").glob("*.partial.mp4"))


def test_native_executor_renders_bound_balanced_improvement_from_faithful(
    tmp_path: Path,
) -> None:
    """Catches a pipeline fake being the only available improved executor."""
    source_bytes = b"original source"
    source_hash = _sha256_bytes(source_bytes)
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    interval = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.DARK, 0.5, 1.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=0.5,
        end_seconds=1.0,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=True,
            file_size_bytes=len(source_bytes),
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            scan_coverage=((0.0, 2.0),),
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=_measured_dark_assessment(),
    )
    commands: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(arguments)
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                _media_probe_json(container_duration=2.0, audio_duration=2.0),
            )
        if "null" in arguments:
            return CommandResult(0, "", "")
        Path(arguments[-1]).write_bytes(b"improved pixels")
        return CommandResult(0, "", "")

    output = NativeRescueExecutor(runner=runner).execute_improved(
        plan, faithful, tmp_path / "work", lambda: False
    )

    assert output.name == "improved-viewing.mp4"
    assert output.read_bytes() == b"improved pixels"
    render = next(command for command in commands if "-filter:v:0" in command)
    assert any(
        "eq=brightness=0.05:contrast=1.02:gamma=1.25:gamma_weight=0.85" in value
        for value in render
    )
    assert render[render.index("-preset:v:0") + 1] == "medium"
    assert render[render.index("-crf:v:0") + 1] == "16"
    assert any("enable='gte(t,0.5)*lt(t,1)'" in value for value in render)
    assert faithful.read_bytes() == b"verified faithful"


@pytest.mark.parametrize("failure_mode", ("unsupported", "error", "cancel"))
def test_chroma_qp_render_failure_or_cancellation_cleans_partial_output(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    source_hash = _sha256_bytes(b"original source")
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    dark = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.DARK, 0.5, 1.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=0.5,
        end_seconds=1.0,
    )
    assessment = _measured_dark_assessment()
    assessment = assessment.model_copy(
        update={
            "metrics": assessment.metrics.model_copy(update={"noise_residual": 0.03})
        }
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            scan_coverage=((0.0, 2.0),),
            intervals=(dark,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=assessment,
    )
    renders: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "fixed-ffprobe":
            return CommandResult(
                0,
                "",
                _media_probe_json(container_duration=2.0, audio_duration=2.0),
            )
        assert arguments[0] == "fixed-ffmpeg"
        renders.append(arguments)
        partial = Path(arguments[-1])
        partial.write_bytes(b"partial chroma qp output")
        if failure_mode == "cancel":
            raise RescueCancelledError("cancelled by fixed FFmpeg runner")
        return CommandResult(
            1,
            (
                "Unrecognized option 'chromaoffset'"
                if failure_mode == "unsupported"
                else "injected fixed FFmpeg encode error"
            ),
            "",
        )

    executor = NativeRescueExecutor(
        ffmpeg="fixed-ffmpeg",
        ffprobe="fixed-ffprobe",
        runner=runner,
    )
    expected_error: type[Exception] = (
        RescueCancelledError if failure_mode == "cancel" else RescueMediaError
    )
    with pytest.raises(expected_error):
        executor.execute_improved(
            plan,
            faithful,
            tmp_path / "work",
            lambda: False,
            source_mappings=(SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
        )

    assert len(renders) == 1
    assert renders[0][renders[0].index("-chromaoffset") + 1] == "-6"
    assert not (tmp_path / "work/staging/improved-viewing.partial.mp4").exists()
    assert not (tmp_path / "work/staging/improved-viewing.mp4").exists()
    assert faithful.read_bytes() == b"verified faithful"


def test_sharpen_only_improved_output_is_distinct_from_faithful(
    tmp_path: Path,
) -> None:
    source_bytes = b"original source"
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified restored media")
    plan = _plan(source_bytes, duration_seconds=2.0)
    sharpen = _bound_content_action(
        plan,
        kind=RescueActionKind.SHARPEN,
        description="Measured local restoration.",
        source_ranges=((0.5, 1.5),),
        parameters={"radius": 2, "adaptive_strength": 0.32, "amount": 0.8},
        strategy=RescueStrategy.BALANCED,
    )
    plan = _rebuild_internal_draft_plan(
        plan,
        strategy=RescueStrategy.BALANCED,
        actions=(*plan.actions, sharpen),
    )
    commands: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(arguments)
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                _media_probe_json(container_duration=2.0, audio_duration=2.0),
            )
        if "null" in arguments:
            return CommandResult(0, "", "")
        Path(arguments[-1]).write_bytes(b"distinct sharpened media")
        return CommandResult(0, "", "")

    output = (
        NativeRescueExecutor(runner=runner)
        .execute_improved_with_controls(
            plan,
            faithful,
            tmp_path / "work",
            lambda: False,
            source_mappings=(SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
            generate_verification_controls=False,
            _allow_unqualified_sharpen_draft=True,
        )
        .output_path
    )

    assert output.read_bytes() == b"distinct sharpened media"
    assert any("-filter:v:0" in command for command in commands)
    assert faithful.read_bytes() == b"verified restored media"


@pytest.mark.parametrize("failure_mode", (None, "error", "cancel"))
def test_runtime_sharpen_controls_use_exact_three_way_commands_and_cleanup(
    tmp_path: Path,
    failure_mode: str | None,
) -> None:
    source_bytes = b"original source"
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified restored media")
    plan = _plan(source_bytes, duration_seconds=2.0)
    sharpen = _bound_content_action(
        plan,
        kind=RescueActionKind.SHARPEN,
        description="Measured local restoration.",
        source_ranges=((0.5, 1.5),),
        parameters={
            "radius": 2,
            "adaptive_strength": 0.32,
            "amount": 0.8,
            "visibility_brightness": 0.12,
            "boundary_transition_seconds": 0.2,
        },
        strategy=RescueStrategy.BALANCED,
    )
    plan = _rebuild_internal_draft_plan(
        plan,
        strategy=RescueStrategy.BALANCED,
        actions=(*plan.actions, sharpen),
    )
    render_commands: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(
                0, "", _media_probe_json(container_duration=2.0, audio_duration=2.0)
            )
        if "-filter:v:0" in arguments and arguments[-1].endswith(".mp4"):
            render_commands.append(arguments)
            output = Path(arguments[-1])
            output.write_bytes(f"generation-{len(render_commands)}".encode())
            if failure_mode == "error" and len(render_commands) == 2:
                return CommandResult(1, "injected control error", "")
            if failure_mode == "cancel" and len(render_commands) == 3:
                raise RescueCancelledError("injected candidate cancellation")
        return CommandResult(0, "", "")

    executor = NativeRescueExecutor(
        runner=runner,
        sharpen_control_inspector=lambda _baseline, _visibility, _candidate, _cancel: (
            "d" * 64,
            "e" * 64,
            20,
        ),
    )
    if failure_mode is not None:
        expected = (
            RescueCancelledError if failure_mode == "cancel" else RescueMediaError
        )
        with pytest.raises(expected):
            executor.execute_improved_with_controls(
                plan,
                faithful,
                tmp_path / "work",
                lambda: False,
                source_mappings=(
                    SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),
                ),
                _allow_unqualified_sharpen_draft=True,
            )
        assert not list((tmp_path / "work/staging").glob("*.private.mp4"))
        return

    result = executor.execute_improved_with_controls(
        plan,
        faithful,
        tmp_path / "work",
        lambda: False,
        source_mappings=(SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
        _allow_unqualified_sharpen_draft=True,
    )
    assert len(render_commands) == 3
    filters = tuple(
        command[command.index("-filter:v:0") + 1] for command in render_commands
    )
    assert filters[0] == "null"
    assert "eq=brightness=" in filters[1]
    assert "cas=strength=0" in filters[1]
    assert "unsharp=5:5:0:5:5:0" in filters[1]
    assert "eq=brightness=" in filters[2] and "cas=" in filters[2]
    assert filters[1].count("cas=") == filters[2].count("cas=") == 1
    assert filters[1].count("unsharp=") == filters[2].count("unsharp=") == 1
    assert len(result.verification_controls) == 1
    recipe = result.verification_controls[0].recipe
    assert recipe.candidate_sha256 == _sha256_file(result.output_path)
    assert recipe.output_ranges == ((0.5, 1.5),)
    for path in result.verification_controls[0].cleanup_paths:
        path.unlink()


def test_executor_rejects_tampered_action_encode_contract_before_runner(
    tmp_path: Path,
) -> None:
    source_bytes = b"original source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    plan = _plan(
        source_bytes,
        duration_seconds=3.0,
        damage_ranges=((1.0, 2.0),),
    )
    action = next(item for item in plan.actions if item.changes_content)
    raw_contract = action.parameters["video_encode_contract"]
    assert isinstance(raw_contract, dict)
    tampered_contract = dict(raw_contract)
    tampered_contract["profile"] = "high444"
    object.__setattr__(
        action,
        "parameters",
        {**action.parameters, "video_encode_contract": tampered_contract},
    )
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError) as exc:
        NativeRescueExecutor(runner=runner).execute_faithful(
            plan, source, tmp_path / "work", lambda: False
        )

    assert exc.value.internal_message == (
        "confirmed action video encode contract is invalid"
    )
    assert runner_calls == []
    assert source.read_bytes() == source_bytes


def test_native_executor_applies_confirmed_deflicker_curve_frame_by_frame(
    tmp_path: Path,
) -> None:
    """Catches replacing an alternating sampled curve with midpoint constants."""
    source_bytes = b"original source"
    source_hash = _sha256_bytes(source_bytes)
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    interval = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.FLICKER, 0.5, 1.5),
        stream_id="video:0",
        kind=DamageKind.FLICKER,
        start_seconds=0.5,
        end_seconds=1.5,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=True,
            file_size_bytes=len(source_bytes),
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            scan_coverage=((0.0, 2.0),),
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        flicker_correction=FlickerCorrectionPlan(
            intervals=((0.5, 1.5),),
            gains=((0.5, 1.08), (1.0, 1.0 / 1.08), (1.5, 1.08)),
        ),
    )
    captured: dict[str, object] = {}

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                _media_probe_json(container_duration=2.0, audio_duration=2.0),
            )
        if "null" in arguments:
            return CommandResult(0, "", "")
        raise AssertionError(
            "deflicker-only execution must not use a midpoint FFmpeg render"
        )

    class Executor(NativeRescueExecutor):
        def execute_deflickered(self, **kwargs: object) -> None:
            captured.update(kwargs)
            Path(kwargs["output"]).write_bytes(b"deflickered pixels")  # type: ignore[arg-type]

    output = Executor(runner=runner).execute_improved(
        plan,
        faithful,
        tmp_path / "work",
        lambda: False,
        source_mappings=(SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
    )

    correction = captured["correction"]
    assert isinstance(correction, FlickerCorrectionPlan)
    assert correction.intervals == ((0.5, 1.5),)
    assert output.read_bytes() == b"deflickered pixels"
    assert faithful.read_bytes() == b"verified faithful"


def test_native_executor_dispatches_native_stabilization(
    tmp_path: Path,
) -> None:
    source_hash = _sha256_bytes(b"original source")
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    interval = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SHAKE, 0.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.SHAKE,
        start_seconds=0.0,
        end_seconds=2.0,
    )
    transform = MotionTransform(
        timestamp_seconds=0.0,
        rotation_degrees=0.0,
        scale=1.0,
        translation_x=1.0,
        translation_y=0.0,
        inlier_ratio=0.9,
        residual_pixels=0.5,
        semantics="frame_correction",
    )
    assessment = StabilizationAssessment(
        recommended=True,
        reason="Measured stable correction.",
        crop_ratio=0.02,
        transforms=(transform,),
        parameters={
            "crop_ratio": 0.02,
            "frame_width": 64,
            "frame_height": 64,
            "maximum_timeline_gap_seconds": 1.0,
            "smoothing_window_samples": 5,
        },
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        stabilization_assessment=assessment,
    )
    commands: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        commands.append(arguments)
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                _media_probe_json(container_duration=2.0),
            )
        return CommandResult(0, "", "")

    class Executor(NativeRescueExecutor):
        dispatched: tuple[MotionTransform, ...] = ()

        def execute_stabilized(self, **kwargs: object) -> None:
            self.dispatched = tuple(kwargs["transforms"])  # type: ignore[arg-type]
            Path(kwargs["output"]).write_bytes(b"stabilized")  # type: ignore[arg-type]

    executor = Executor(
        runner=runner,
        verification_control_inspector=lambda _parent, _control, _candidate, _cancel: (
            "d" * 64,
            "e" * 64,
            20,
            "d" * 64,
            "e" * 64,
            20,
            "d" * 64,
            "e" * 64,
            20,
        ),
    )
    assert RescueActionKind.STABILIZE in {action.kind for action in plan.actions}
    executor.execute_improved(plan, faithful, tmp_path / "work", lambda: False)

    assert executor.dispatched == (transform,)
    assert all(
        "deshake" not in command and "deflicker" not in command for command in commands
    )


def test_faithful_restoration_applies_confirmed_native_stabilization(
    tmp_path: Path,
) -> None:
    """Catches delivering a faithful file that still contains confirmed shake."""
    source_hash = _sha256_bytes(b"original source")
    faithful = tmp_path / "work" / "staging" / "faithful-rescue.mp4"
    faithful.parent.mkdir(parents=True)
    faithful.write_bytes(b"verified faithful")
    segment = RescuedSegment(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4")
    execution = RescueExecutionResult(
        faithful,
        "faithful-rescue.mp4",
        (segment,),
        (segment.source_mapping,),
    )
    interval = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SHAKE, 0.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.SHAKE,
        start_seconds=0.0,
        end_seconds=2.0,
    )
    transform = MotionTransform(
        timestamp_seconds=0.0,
        rotation_degrees=0.0,
        scale=1.0,
        translation_x=1.0,
        translation_y=0.0,
        inlier_ratio=0.9,
        residual_pixels=0.5,
        semantics="frame_correction",
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=False,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="Measured stable correction.",
            crop_ratio=0.02,
            transforms=(transform,),
            parameters={
                "crop_ratio": 0.02,
                "frame_width": 64,
                "frame_height": 64,
                "maximum_timeline_gap_seconds": 1.0,
                "smoothing_window_samples": 5,
            },
        ),
    )

    class Executor(NativeRescueExecutor):
        dispatched: tuple[MotionTransform, ...] = ()

        def execute_stabilized(self, **kwargs: object) -> None:
            self.dispatched = tuple(kwargs["transforms"])  # type: ignore[arg-type]
            Path(kwargs["output"]).write_bytes(b"stabilized faithful")  # type: ignore[arg-type]

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                _media_probe_json(container_duration=2.0),
            )
        return CommandResult(0, "", "")

    executor = Executor(
        runner=runner,
        verification_control_inspector=lambda _parent, _control, _candidate, _cancel: (
            "d" * 64,
            "e" * 64,
            20,
            "d" * 64,
            "e" * 64,
            20,
            "d" * 64,
            "e" * 64,
            20,
        ),
    )
    restored = executor.execute_faithful_restoration(
        plan, execution, tmp_path / "work", lambda: False
    )

    assert executor.dispatched == (transform,)
    assert restored.output_path.read_bytes() == b"stabilized faithful"
    assert restored.render_mode == "single_reencode"
    assert plan.actions[-2].id in restored.applied_action_ids
    assert len(restored.verification_controls) == 1
    control_handle = cast(Any, restored.verification_controls[0])
    assert control_handle.path.is_file()
    assert control_handle.recipe.candidate_sha256 == _sha256_bytes(
        restored.output_path.read_bytes()
    )
    assert control_handle.recipe.candidate_frame_count == 20


def test_improved_output_does_not_repeat_inherited_stabilization(
    tmp_path: Path,
) -> None:
    """Catches applying the same motion correction twice to the second output."""
    source_hash = _sha256_bytes(b"original source")
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"already stabilized faithful")
    interval = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SHAKE, 0.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.SHAKE,
        start_seconds=0.0,
        end_seconds=2.0,
    )
    transform = MotionTransform(
        timestamp_seconds=0.0,
        rotation_degrees=0.0,
        scale=1.0,
        translation_x=1.0,
        translation_y=0.0,
        inlier_ratio=0.9,
        residual_pixels=0.5,
        semantics="frame_correction",
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=10.0,
            estimated_frame_count=20,
            has_audio=False,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            intervals=(interval,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="Measured stable correction.",
            crop_ratio=0.02,
            transforms=(transform,),
            parameters={
                "crop_ratio": 0.02,
                "frame_width": 64,
                "frame_height": 64,
                "maximum_timeline_gap_seconds": 1.0,
                "smoothing_window_samples": 5,
            },
        ),
    )
    stabilize_id = next(
        action.id
        for action in plan.actions
        if action.kind is RescueActionKind.STABILIZE
    )

    class Executor(NativeRescueExecutor):
        def execute_stabilized(self, **_kwargs: object) -> None:
            raise AssertionError("inherited stabilization must not run twice")

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                _media_probe_json(container_duration=2.0),
            )
        if "null" in arguments:
            return CommandResult(0, "", "")
        raise AssertionError("no second media render is required")

    with pytest.raises(RescueMediaError):
        Executor(runner=runner).execute_improved(
            plan,
            faithful,
            tmp_path / "work",
            lambda: False,
            inherited_action_ids=frozenset((stabilize_id,)),
        )

    assert not (tmp_path / "work" / "staging" / "improved-viewing.mp4").exists()
    assert faithful.read_bytes() == b"already stabilized faithful"


def test_native_perceptual_restorers_chain_once_and_record_applied_ids(
    tmp_path: Path,
) -> None:
    source_hash = _sha256_bytes(b"original source")
    estimate = BlurKernelEstimate(
        kernel_kind="box",
        radius=2,
        regularization=0.003,
        confidence=0.93,
        edge_width_before=4.0,
        predicted_edge_width_after=2.0,
        edge_continuity_ratio=0.9,
        reblur_error_ratio=0.01,
        ringing_ratio=0.01,
        noise_gain_ratio=1.1,
        temporal_change_ratio=0.02,
    )
    deblur_config = DeblurConfig(candidate_radii=(2,))
    second_estimate = estimate.model_copy(update={"radius": 3})
    second_deblur_config = DeblurConfig(candidate_radii=(3,))
    tone = InterferenceTone(
        start_seconds=0.25,
        end_seconds=1.75,
        center_frequency_hz=913.0,
        confidence=0.91,
        baseline_before_dbfs=-58.0,
        baseline_after_dbfs=-57.0,
        peak_dbfs=-13.0,
        local_peak_over_baseline_db=44.0,
        persistence_window_count=24,
        frequency_standard_deviation_hz=1.25,
        channel_indices=(0,),
        attenuation_target_db=24.0,
        render_qualification=TonalRenderQualification(
            boundary_mode="full_interval_v1",
            notch_q=8.0,
            complete_window_count=30,
            minimum_target_reduction_db=25.0,
            maximum_non_target_attenuation_db=0.1,
            maximum_boundary_energy_jump_db=0.1,
            maximum_boundary_crest_jump_db=0.1,
            maximum_boundary_adjacent_delta=0.01,
        ),
    )
    tonal_config = TonalInterferenceConfig(render_attenuation_headroom_db=4.0)
    transform = MotionTransform(
        timestamp_seconds=1.5,
        rotation_degrees=0.0,
        scale=1.0,
        translation_x=1.0,
        translation_y=0.0,
        inlier_ratio=0.9,
        residual_pixels=0.5,
        semantics="frame_correction",
    )
    first_transform = transform.model_copy(update={"timestamp_seconds": 0.5})
    first_boundary_transform = transform.model_copy(update={"timestamp_seconds": 0.75})
    second_boundary_transform = transform.model_copy(update={"timestamp_seconds": 1.75})
    stabilization_config = StabilizationConfig(
        frame_width=64,
        frame_height=64,
        accepted_ranges=((0.0, 2.0),),
    )
    locked_ranges = ((0.8, 1.0),)
    intervals = tuple(
        DamageInterval(
            id=make_damage_id(source_hash, stream, kind, 0.0, 2.0),
            stream_id=stream,
            kind=kind,
            start_seconds=0.0,
            end_seconds=2.0,
        )
        for stream, kind in (
            ("video:0", DamageKind.SOFT_DETAIL),
            ("audio:0", DamageKind.AUDIO_NOISE),
            ("video:0", DamageKind.SHAKE),
        )
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=64,
            height=64,
            duration_seconds=2.0,
            average_frame_rate=2.0,
            estimated_frame_count=4,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            intervals=intervals,
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(locked_ranges=locked_ranges),
        locked_ranges=locked_ranges,
        assessment_parameters={
            "deblur_measurements": [
                {
                    "algorithm_version": "1",
                    "source_ranges": [[0.0, 0.75]],
                    "estimate": estimate.model_dump(mode="json"),
                    "config": deblur_config.model_dump(mode="json"),
                },
                {
                    "algorithm_version": "1",
                    "source_ranges": [[1.25, 2.0]],
                    "estimate": second_estimate.model_dump(mode="json"),
                    "config": second_deblur_config.model_dump(mode="json"),
                },
            ],
            "tonal_interference_measurements": [
                {
                    "algorithm_version": "1",
                    "source_ranges": [[0.25, 1.75]],
                    "interference_profiles": [tone.model_dump(mode="json")],
                    "config": tonal_config.model_dump(mode="json"),
                }
            ],
        },
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="measured_affine_motion",
            crop_ratio=0.02,
            transforms=(
                first_transform,
                first_boundary_transform,
                transform,
                second_boundary_transform,
            ),
            parameters={
                "method": "anchor_v1",
                "algorithm_version": "1",
                "config": stabilization_config.model_dump(mode="json"),
                "affected_ranges": [[0.0, 2.0]],
            },
        ),
    )
    faithful = tmp_path / "工作 空间" / "staging" / "faithful-rescue.mp4"
    faithful.parent.mkdir(parents=True)
    faithful.write_bytes(b"structural")
    segment = RescuedSegment(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4")
    execution = RescueExecutionResult(
        faithful,
        "faithful-rescue.mp4",
        (segment,),
        (segment.source_mapping,),
    )
    calls: list[tuple[str, object]] = []

    def publish(kind: str, source: Path, output: Path, payload: object) -> None:
        calls.append((kind, payload))
        output.write_bytes(source.read_bytes() + kind.encode("ascii"))

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                _media_probe_json(
                    container_duration=2.0,
                    fps=2.0,
                    audio_duration=2.0,
                ),
            )
        if "null" in arguments:
            return CommandResult(0, "", "")
        raise AssertionError(f"unexpected generic media render: {arguments}")

    executor = NativeRescueExecutor(
        runner=runner,
        deblur_renderer=lambda source, output, ranges, estimate, config, **_kwargs: (
            publish("deblur", source, output, (ranges, estimate, config))
        ),
        tonal_renderer=lambda source, output, tones, config, **_kwargs: publish(
            "tonal", source, output, (tones, config)
        ),
        stabilization_renderer=lambda source, output, transforms, config, **_kwargs: (
            publish("anchor", source, output, (transforms, config))
        ),
        verification_control_inspector=lambda _parent, _control, _candidate, _cancel: (
            "d" * 64,
            "e" * 64,
            4,
            "d" * 64,
            "e" * 64,
            4,
            "d" * 64,
            "e" * 64,
            4,
        ),
    )

    restored = executor.execute_faithful_restoration(
        plan, execution, tmp_path / "工作 空间", lambda: False
    )
    with pytest.raises(RescueMediaError):
        executor.execute_improved(
            plan,
            restored.output_path,
            tmp_path / "工作 空间",
            lambda: False,
            source_mappings=restored.source_mappings,
            inherited_action_ids=restored.applied_action_ids,
        )

    assert [kind for kind, _payload in calls] == [
        "deblur",
        "deblur",
        "anchor",
        "anchor",
    ]
    assert calls[0][1] == (((0.0, 0.75),), estimate, deblur_config)
    assert calls[1][1] == (
        ((1.25, 2.0),),
        second_estimate,
        second_deblur_config,
    )
    assert calls[2][1] == (
        tuple(
            transform.model_copy(
                update={
                    "rotation_degrees": 0.0,
                    "scale": 1.0,
                    "translation_x": 0.0,
                    "translation_y": 0.0,
                }
            )
            for transform in (
                first_transform,
                first_boundary_transform,
                transform,
                second_boundary_transform,
            )
        ),
        stabilization_config.model_copy(
            update={"accepted_ranges": ((0.0, 0.8), (1.0, 2.0))}
        ),
    )
    assert calls[3][1] == (
        (
            first_transform,
            first_boundary_transform,
            transform,
            second_boundary_transform,
        ),
        stabilization_config.model_copy(
            update={"accepted_ranges": ((0.0, 0.8), (1.0, 2.0))}
        ),
    )
    expected_ids = {
        action.id
        for action in plan.actions
        if action.kind
        in {
            RescueActionKind.DEBLUR,
            RescueActionKind.DENOISE_AUDIO,
            RescueActionKind.STABILIZE,
        }
    }
    assert restored.applied_action_ids == expected_ids
    assert len(restored.verification_controls) == 1
    assert "improved-viewing.mp4" not in plan.public_artifacts
    assert not (tmp_path / "工作 空间" / "staging" / "improved-viewing.mp4").exists()

    failed_work = tmp_path / "失败 工作"
    failed_faithful = failed_work / "staging" / "faithful-rescue.mp4"
    failed_faithful.parent.mkdir(parents=True)
    failed_faithful.write_bytes(b"unchanged staged source")
    failed_hash = _sha256_file(failed_faithful)
    failed_execution = replace(execution, output_path=failed_faithful)

    def fail_deblur(
        source: Path, output: Path, *_args: object, **_kwargs: object
    ) -> None:
        output.write_bytes(source.read_bytes() + b"partial")
        raise RescueMediaError("injected native restoration failure")

    with pytest.raises(RescueMediaError):
        NativeRescueExecutor(
            runner=runner,
            deblur_renderer=fail_deblur,
        ).execute_faithful_restoration(
            plan, failed_execution, failed_work, lambda: False
        )

    assert _sha256_file(failed_faithful) == failed_hash
    assert not tuple((failed_work / "staging").glob("*.partial.mp4"))

    collision_work = tmp_path / "collision 工作"
    collision_faithful = collision_work / "staging" / "faithful-rescue.mp4"
    collision_faithful.parent.mkdir(parents=True)
    collision_faithful.write_bytes(b"collision staged source")
    reserved = collision_work / "staging" / "faithful-deblurred.partial.mp4"
    reserved.write_bytes(b"do not clobber")
    collision_execution = replace(execution, output_path=collision_faithful)
    with pytest.raises(RescueArtifactError):
        executor.execute_faithful_restoration(
            plan, collision_execution, collision_work, lambda: False
        )
    assert reserved.read_bytes() == b"do not clobber"
    assert collision_faithful.read_bytes() == b"collision staged source"


@pytest.mark.parametrize(
    ("kind", "source_ranges", "locked_ranges", "source_mappings"),
    (
        (
            RescueActionKind.STABILIZE,
            ((0.5, 1.5),),
            (),
            (SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
        ),
        (
            RescueActionKind.NORMALIZE_ROTATION,
            ((0.0, 2.0),),
            ((0.5, 1.0),),
            (SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),),
        ),
        (
            RescueActionKind.CORRECT_FIXED_AV_OFFSET,
            ((0.0, 2.0),),
            (),
            (SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),),
        ),
    ),
)
def test_forged_action_scope_fails_before_media_runner(
    tmp_path: Path,
    kind: RescueActionKind,
    source_ranges: tuple[tuple[float, float], ...],
    locked_ranges: tuple[tuple[float, float], ...],
    source_mappings: tuple[SourceMapping, ...],
) -> None:
    """Catches bypassing planner scope gates with a forged confirmed plan."""
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    plan = _plan(b"original source", duration_seconds=2.0)
    action = _bound_content_action(
        plan,
        kind=kind,
        description="Forged content-changing action.",
        source_ranges=source_ranges,
        parameters={},
        strategy=RescueStrategy.BALANCED,
    )
    effective_config = plan.effective_config.model_copy(
        update={"locked_ranges": locked_ranges}
    )
    object.__setattr__(plan, "strategy", RescueStrategy.BALANCED)
    object.__setattr__(plan, "effective_config", effective_config)
    object.__setattr__(plan, "actions", (*plan.actions, action))
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError) as exc_info:
        NativeRescueExecutor(runner=runner).execute_improved(
            plan,
            faithful,
            tmp_path / "work",
            lambda: False,
            source_mappings=source_mappings,
        )

    assert str(exc_info.value) == "The selected media could not be processed locally."
    assert (
        exc_info.value.internal_message
        == "confirmed Rescue action scope is not executable"
    )
    assert runner_calls == []


@pytest.mark.parametrize(
    ("kind", "damage_ranges", "locked_ranges"),
    ((RescueActionKind.NORMALIZE_ROTATION, (), ((0.5, 1.0),)),),
)
def test_forged_global_faithful_action_fails_before_media_runner(
    tmp_path: Path,
    kind: RescueActionKind,
    damage_ranges: tuple[tuple[float, float], ...],
    locked_ranges: tuple[tuple[float, float], ...],
) -> None:
    """Catches a locked global rotation action bypassing faithful scope checks."""
    source_bytes = b"original source"
    source = tmp_path / "source.mp4"
    source.write_bytes(source_bytes)
    plan = _plan(source_bytes, duration_seconds=2.0, damage_ranges=damage_ranges)
    action = _bound_content_action(
        plan,
        kind=kind,
        description="Forged global faithful action.",
        source_ranges=((0.0, 2.0),),
        parameters={},
        strategy=RescueStrategy.CONSERVATIVE,
    )
    effective_config = plan.effective_config.model_copy(
        update={"locked_ranges": locked_ranges}
    )
    object.__setattr__(plan, "effective_config", effective_config)
    object.__setattr__(plan, "actions", (*plan.actions, action))
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError) as exc_info:
        NativeRescueExecutor(runner=runner).execute_faithful(
            plan, source, tmp_path / "work", lambda: False
        )

    assert (
        exc_info.value.internal_message
        == "confirmed Rescue action scope is not executable"
    )
    assert runner_calls == []


@pytest.mark.parametrize(
    ("kind", "parameters"),
    (
        (
            RescueActionKind.ADJUST_LUMA,
            {"brightness": 0.04, "contrast": 1.02},
        ),
        (
            RescueActionKind.DEFLICKER,
            {
                "affected_ranges": [[0.0, 2.0]],
                "gain_curve": [[0.0, 1.0], [1.0, 1.1], [2.0, 1.0]],
                "excluded_fade_ranges": [],
            },
        ),
    ),
)
def test_missing_mappings_after_faithful_deletion_fail_before_media_runner(
    tmp_path: Path,
    kind: RescueActionKind,
    parameters: dict[str, object],
) -> None:
    """Catches fabricating identity mapping after faithful timeline compaction."""
    faithful = tmp_path / "faithful-rescue.mp4"
    faithful.write_bytes(b"verified faithful")
    plan = _plan(
        b"original source", duration_seconds=2.0, damage_ranges=((0.75, 1.25),)
    )
    if kind is RescueActionKind.ADJUST_LUMA:
        metrics = _measured_dark_assessment().metrics
        parameters = {
            **derive_visual_action_parameters(kind, metrics),
            "strength_limit": 1.0,
            "assessment_metrics": metrics.model_dump(mode="json"),
            "assessment_evidence": [
                VisualEvidence(
                    action=RescueActionKind.ADJUST_LUMA,
                    timestamp_seconds=1.0,
                    metric="luma_p10",
                    observed=0.05,
                    threshold=0.18,
                    context_luma_p50=0.08,
                ).model_dump(mode="json")
            ],
            "assessment_limitations": [],
        }
    action = _bound_content_action(
        plan,
        kind=kind,
        description="Forged local improvement without a retained mapping.",
        source_ranges=((0.0, 2.0),),
        parameters=cast(dict[str, JsonValue], parameters),
        strategy=RescueStrategy.BALANCED,
    )
    object.__setattr__(plan, "strategy", RescueStrategy.BALANCED)
    object.__setattr__(plan, "actions", (*plan.actions, action))
    runner_calls: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        runner_calls.append(arguments)
        return CommandResult(0, "", "")

    with pytest.raises(RescueMediaError) as exc_info:
        NativeRescueExecutor(runner=runner).execute_improved(
            plan, faithful, tmp_path / "work", lambda: False
        )

    assert (
        exc_info.value.internal_message
        == "confirmed faithful source mapping is required"
    )
    assert runner_calls == []
