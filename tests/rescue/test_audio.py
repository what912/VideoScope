"""Tests for bounded, evidence-led Rescue audio processing."""

from __future__ import annotations

import math
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from videoscope.rescue.audio import (
    AudioDenoiseConfig,
    FixedOffsetConfig,
    LoudnessConfig,
    assess_audio,
    audio_filter_fragment_from_actions,
    loudnorm_apply_filter,
    measure_fixed_av_offset,
    parse_loudnorm_measurement,
)
from videoscope.rescue.commands import (
    build_audio_improvement_command,
    build_loudnorm_measurement_command,
)
from videoscope.rescue.models import RescueActionKind


def test_low_loudness_proposes_normalization_with_peak_guard() -> None:
    """Catches normalizing without a measured loudnorm pass or peak guard."""
    assessment = assess_audio(
        {
            "input_i": "-27.0",
            "input_tp": "-3.0",
            "input_lra": "5.0",
            "input_thresh": "-37.0",
            "target_offset": "0.0",
        },
        LoudnessConfig(),
    )

    assert assessment.recommended_actions == (RescueActionKind.NORMALIZE_AUDIO,)
    assert assessment.parameters["true_peak_limit_dbtp"] == -1.5
    assert "measured_I=-27" in loudnorm_apply_filter(
        parse_loudnorm_measurement(
            '{"input_i":"-27.0","input_tp":"-3.0","input_lra":"5.0","input_thresh":"-37.0","target_offset":"0.0"}'
        ),
        LoudnessConfig(),
    )


def test_clipping_is_detected_independently_of_loudness() -> None:
    """Catches treating a near-full-scale peak as a low-loudness condition."""
    assessment = assess_audio(
        {
            "input_i": -16.0,
            "input_tp": -0.01,
            "input_lra": 5.0,
            "input_thresh": -26.0,
            "target_offset": 0.0,
        },
        LoudnessConfig(),
    )

    assert assessment.clipping_detected is True
    assert assessment.recommended_actions == (RescueActionKind.NORMALIZE_AUDIO,)
    assert assessment.parameters["loudness_deviation_lu"] == 0.0


def test_clean_and_ambiguous_noise_measurements_do_not_propose_processing() -> None:
    """Catches default audio filtering for clean or weak isolated noise evidence."""
    clean = assess_audio(
        {
            "input_i": -16.0,
            "input_tp": -2.0,
            "input_lra": 5.0,
            "input_thresh": -26.0,
            "target_offset": 0.0,
            "noise_floor_dbfs": -75.0,
            "noise_confidence": 0.99,
            "noise_event_count": 8,
        },
        LoudnessConfig(),
        AudioDenoiseConfig(),
    )
    ambiguous = assess_audio(
        {
            "input_i": -16.0,
            "input_tp": -2.0,
            "input_lra": 5.0,
            "input_thresh": -26.0,
            "target_offset": 0.0,
            "noise_floor_dbfs": -35.0,
            "noise_confidence": 0.4,
            "noise_event_count": 1,
        },
        LoudnessConfig(),
        AudioDenoiseConfig(),
    )

    assert clean.recommended_actions == ()
    assert ambiguous.recommended_actions == ()
    assert ambiguous.limitations == ("noise_evidence_is_not_reliable",)


def test_repeated_high_confidence_noise_has_bounded_denoise_parameters() -> None:
    """Catches unbounded denoise or reacting to an isolated noise estimate."""
    assessment = assess_audio(
        {
            "input_i": -16.0,
            "input_tp": -2.0,
            "input_lra": 5.0,
            "input_thresh": -26.0,
            "target_offset": 0.0,
            "noise_floor_dbfs": -34.0,
            "noise_confidence": 0.9,
            "noise_event_count": 4,
        },
        LoudnessConfig(),
        AudioDenoiseConfig(maximum_reduction_db=9.0),
    )

    assert assessment.recommended_actions == (RescueActionKind.DENOISE_AUDIO,)
    assert assessment.parameters["maximum_reduction_db"] == 9.0
    assert (
        audio_filter_fragment_from_actions(
            (RescueActionKind.DENOISE_AUDIO,), assessment.parameters
        )
        == "afftdn=nr=9:nf=-34"
    )


@pytest.mark.parametrize(
    "payload", ["not-json", '{"input_i":"NaN"}', '{"input_i":"Infinity"}']
)
def test_loudnorm_json_rejects_invalid_or_nonfinite_values(payload: str) -> None:
    """Catches invalid FFmpeg measurement output reaching a filter argument."""
    with pytest.raises(ValueError):
        parse_loudnorm_measurement(payload)


def test_fixed_positive_and_negative_offsets_are_single_constant_shifts() -> None:
    """Catches offset direction reversal or applying a varying correction curve."""
    config = FixedOffsetConfig()
    audio = ((1.2, 0.95), (2.2, 0.94), (3.2, 0.96), (4.2, 0.93))
    video = ((1.0, 0.95), (2.0, 0.94), (3.0, 0.96), (4.0, 0.93))
    positive = measure_fixed_av_offset(audio, video, config)
    negative = measure_fixed_av_offset(video, audio, config)

    assert positive.offset_seconds == pytest.approx(0.2)
    assert positive.shift_seconds == pytest.approx(-0.2)
    assert negative.offset_seconds == pytest.approx(-0.2)
    assert negative.shift_seconds == pytest.approx(0.2)
    assert positive.reason is None


@pytest.mark.parametrize(
    ("audio", "video", "reason"),
    [
        (
            ((1.2, 0.95), (2.2, 0.95)),
            ((1.0, 0.95), (2.0, 0.95)),
            "insufficient_event_count",
        ),
        (
            ((1.2, 0.4), (2.2, 0.4), (3.2, 0.4)),
            ((1.0, 0.4), (2.0, 0.4), (3.0, 0.4)),
            "insufficient_correlation",
        ),
        (
            ((1.2, 0.95), (2.3, 0.95), (3.5, 0.95)),
            ((1.0, 0.95), (2.0, 0.95), (3.0, 0.95)),
            "offset_not_constant",
        ),
    ],
)
def test_ambiguous_drifting_or_insufficient_offsets_need_manual_review(
    audio: tuple[tuple[float, float], ...],
    video: tuple[tuple[float, float], ...],
    reason: str,
) -> None:
    """Catches guessing an offset from weak, sparse, or drifting observations."""
    assessment = measure_fixed_av_offset(audio, video, FixedOffsetConfig())

    assert assessment.offset_seconds is None
    assert assessment.reason == reason


def test_audio_configs_are_strict_finite_and_immutable() -> None:
    """Catches unsafe thresholds or mutable config changing a confirmed filter."""
    with pytest.raises(ValidationError):
        LoudnessConfig(true_peak_limit_dbtp=math.nan)
    with pytest.raises(ValidationError):
        AudioDenoiseConfig(maximum_reduction_db=20.0)
    with pytest.raises(ValidationError):
        FixedOffsetConfig(minimum_event_count=1)
    config = LoudnessConfig()
    with pytest.raises(ValidationError):
        config.target_integrated_lufs = -18.0
    assessment = assess_audio(
        {
            "input_i": -16.0,
            "input_tp": -2.0,
            "input_lra": 5.0,
            "input_thresh": -26.0,
            "target_offset": 0.0,
        },
        config,
    )
    with pytest.raises(TypeError):
        assessment.parameters["true_peak_limit_dbtp"] = -2.0


def test_audio_commands_are_argument_vectors_with_bound_measurements(
    tmp_path: Path,
) -> None:
    """Catches a shell-shaped audio command or dropping the measured second pass."""
    source = tmp_path / "输入 source.mp4"
    output = tmp_path / "输出 improved.mp4"
    config = LoudnessConfig()
    measurement = parse_loudnorm_measurement(
        '{"input_i":"-27","input_tp":"-3","input_lra":"5","input_thresh":"-37","target_offset":"0"}'
    )

    first = build_loudnorm_measurement_command(source, config)
    second = build_audio_improvement_command(
        source,
        output,
        (RescueActionKind.NORMALIZE_AUDIO,),
        assess_audio(measurement.model_dump(), config).parameters,
    )

    assert first[first.index("-af") + 1].endswith("print_format=json")
    assert str(source) in first
    assert ";" not in " ".join(first)
    assert second[second.index("-filter:a:0") + 1].startswith("loudnorm=I=-16")
    assert ("-c:v", "copy") in tuple(zip(second, second[1:]))


def test_planner_uses_only_assessed_audio_and_reliable_fixed_offset() -> None:
    """Catches planning audio filters from a generic damage label or guessed offset."""
    from videoscope.domain import VideoMetadata
    from videoscope.rescue.models import (
        MediaDamageMap,
        RescueEffectiveConfig,
        RescueStrategy,
    )
    from videoscope.rescue.planner import build_rescue_plan

    metadata = VideoMetadata(
        filename="source.mp4",
        container_format="mp4",
        codec="h264",
        width=16,
        height=16,
        duration_seconds=4.0,
        average_frame_rate=4.0,
        estimated_frame_count=16,
        has_audio=True,
        file_size_bytes=1,
    )
    damage_map = MediaDamageMap(
        input_hash="a" * 64, duration_seconds=4.0, scan_coverage=((0.0, 4.0),)
    )
    audio = assess_audio(
        {
            "input_i": -27.0,
            "input_tp": -3.0,
            "input_lra": 5.0,
            "input_thresh": -37.0,
            "target_offset": 0.0,
        },
        LoudnessConfig(),
    )
    offset = measure_fixed_av_offset(
        ((1.2, 0.95), (2.2, 0.95), (3.2, 0.95)),
        ((1.0, 0.95), (2.0, 0.95), (3.0, 0.95)),
        FixedOffsetConfig(),
    )

    plan = build_rescue_plan(
        metadata=metadata,
        damage_map=damage_map,
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        audio_assessment=audio,
        fixed_offset_assessment=offset,
    )

    actions = {action.kind: action for action in plan.actions}
    assert (
        actions[RescueActionKind.NORMALIZE_AUDIO].parameters["true_peak_limit_dbtp"]
        == -1.5
    )
    assert actions[RescueActionKind.CORRECT_FIXED_AV_OFFSET].parameters[
        "audio_shift_seconds"
    ] == pytest.approx(-0.2)
    assert (
        actions[RescueActionKind.CORRECT_FIXED_AV_OFFSET].parameters[
            "minimum_correlation"
        ]
        == 0.85
    )


def test_conservative_fixed_offset_is_rendered_into_faithful_output(
    tmp_path: Path,
) -> None:
    """Catches making a Conservative correction reachable only as improved media."""
    from videoscope.domain import VideoMetadata
    from videoscope.rescue.executor import CommandResult, NativeRescueExecutor
    from videoscope.rescue.models import (
        MediaDamageMap,
        RescueEffectiveConfig,
        RescueStrategy,
    )
    from videoscope.rescue.planner import build_rescue_plan

    source = tmp_path / "source.mp4"
    source_bytes = b"conservative fixed offset"
    source.write_bytes(source_bytes)
    assessment = measure_fixed_av_offset(
        ((1.2, 0.95), (2.2, 0.95), (3.2, 0.95)),
        ((1.0, 0.95), (2.0, 0.95), (3.0, 0.95)),
        FixedOffsetConfig(),
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=16,
            height=16,
            duration_seconds=4.0,
            average_frame_rate=4.0,
            estimated_frame_count=16,
            has_audio=True,
            file_size_bytes=len(source_bytes),
        ),
        damage_map=MediaDamageMap(
            input_hash=sha256(source_bytes).hexdigest(),
            duration_seconds=4.0,
            scan_coverage=((0.0, 4.0),),
        ),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(),
        fixed_offset_assessment=assessment,
    )
    renders: list[tuple[str, ...]] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffprobe":
            return CommandResult(
                0,
                "",
                '{"format":{"duration":"4.0"},"streams":['
                '{"codec_type":"video"},{"codec_type":"audio"}]}',
            )
        if "null" in arguments:
            return CommandResult(0, "", "")
        renders.append(arguments)
        Path(arguments[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(arguments[-1]).write_bytes(b"faithful corrected")
        return CommandResult(0, "", "")

    result = NativeRescueExecutor(runner=runner).execute_faithful(
        plan, source, tmp_path / "work", lambda: False
    )

    assert result.output_path.name == "faithful-rescue.mp4"
    assert len(renders) == 1
    assert renders[0][renders[0].index("-filter:a:0") + 1] == "asetpts=PTS-0.2/TB"


def test_executor_loudnorm_measurement_uses_sanitized_first_pass_json(
    tmp_path: Path,
) -> None:
    """Catches skipping the first pass or accepting a non-finite measured value."""
    from videoscope.rescue.executor import CommandResult, NativeRescueExecutor

    source = tmp_path / "输入 source.mp4"
    source.write_bytes(b"source")

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        assert arguments[arguments.index("-af") + 1].endswith("print_format=json")
        return CommandResult(
            returncode=0,
            stderr_summary=(
                '[Parsed_loudnorm] {"input_i":"-27","input_tp":"-3",'
                '"input_lra":"5","input_thresh":"-37",'
                '"target_offset":"0"}'
            ),
        )

    result = NativeRescueExecutor(runner=runner).measure_loudness(
        source, tmp_path / "工作", LoudnessConfig(), lambda: False
    )

    assert result.input_i == -27.0


def test_custom_fixed_offset_limit_is_bound_into_and_rendered_from_plan(
    tmp_path: Path,
) -> None:
    """Catches replacing a confirmed 3-second offset bound with the default."""
    from videoscope.domain import VideoMetadata
    from videoscope.rescue.executor import CommandResult, NativeRescueExecutor
    from videoscope.rescue.models import (
        MediaDamageMap,
        RescueEffectiveConfig,
        RescueStrategy,
    )
    from videoscope.rescue.planner import build_rescue_plan

    source = tmp_path / "source.mp4"
    source_bytes = b"custom fixed offset source"
    source.write_bytes(source_bytes)
    offset_config = FixedOffsetConfig(maximum_absolute_offset_seconds=3.0)
    assessment = measure_fixed_av_offset(
        ((3.5, 0.95), (4.5, 0.95), (5.5, 0.95)),
        ((1.0, 0.95), (2.0, 0.95), (3.0, 0.95)),
        offset_config,
    )
    assert assessment.offset_seconds == pytest.approx(2.5)
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=16,
            height=16,
            duration_seconds=4.0,
            average_frame_rate=4.0,
            estimated_frame_count=16,
            has_audio=True,
            file_size_bytes=len(source_bytes),
        ),
        damage_map=MediaDamageMap(
            input_hash=sha256(source_bytes).hexdigest(),
            duration_seconds=4.0,
            scan_coverage=((0.0, 4.0),),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        fixed_offset_assessment=assessment,
    )
    action = next(
        item
        for item in plan.actions
        if item.kind is RescueActionKind.CORRECT_FIXED_AV_OFFSET
    )
    filters: list[str] = []

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        if arguments[0] == "ffmpeg" and arguments[-1] != "-":
            filters.append(arguments[arguments.index("-filter:a:0") + 1])
            Path(arguments[-1]).write_bytes(b"rendered")
            return CommandResult(returncode=0, stderr_summary="")
        return CommandResult(
            returncode=0,
            stderr_summary="",
            stdout_summary=(
                '{"format":{"duration":"4.0"},"streams":['
                '{"codec_type":"video"},{"codec_type":"audio"}]}'
            ),
        )

    result = NativeRescueExecutor(runner=runner).execute_audio_improved(
        plan, source, tmp_path / "work", lambda: False
    )

    assert action.parameters["maximum_absolute_offset_seconds"] == 3.0
    assert filters == ["asetpts=PTS-2.5/TB"]
    assert result.output_path.is_file()


def test_forged_fixed_offset_action_parameters_cannot_bypass_bound() -> None:
    """Catches rendering a larger shift than the action's bound permits."""
    assert (
        audio_filter_fragment_from_actions(
            (RescueActionKind.CORRECT_FIXED_AV_OFFSET,),
            {
                "offset_seconds": 2.5,
                "audio_shift_seconds": -2.5,
                "correlation": 0.95,
                "matched_event_count": 3,
                "agreement_seconds": 0.0,
                "minimum_correlation": 0.85,
                "minimum_event_count": 3,
                "maximum_agreement_seconds": 0.04,
                "maximum_absolute_offset_seconds": 2.0,
            },
        )
        is None
    )


def test_audio_executor_cancels_atomically_without_touching_unicode_source(
    tmp_path: Path,
) -> None:
    """Catches publication of partial audio after cancellation or source aliasing."""
    from videoscope.domain import VideoMetadata
    from videoscope.rescue.errors import RescueCancelledError
    from videoscope.rescue.executor import CommandResult, NativeRescueExecutor
    from videoscope.rescue.models import (
        MediaDamageMap,
        RescueEffectiveConfig,
        RescueStrategy,
    )
    from videoscope.rescue.planner import build_rescue_plan

    source = tmp_path / "输入" / "原始 source.mp4"
    source.parent.mkdir()
    source_bytes = b"audio source"
    source.write_bytes(source_bytes)
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=16,
            height=16,
            duration_seconds=4.0,
            average_frame_rate=4.0,
            estimated_frame_count=16,
            has_audio=True,
            file_size_bytes=len(source_bytes),
        ),
        damage_map=MediaDamageMap(
            input_hash=sha256(source_bytes).hexdigest(),
            duration_seconds=4.0,
            scan_coverage=((0.0, 4.0),),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        audio_assessment=assess_audio(
            {
                "input_i": -27.0,
                "input_tp": -3.0,
                "input_lra": 5.0,
                "input_thresh": -37.0,
                "target_offset": 0.0,
            },
            LoudnessConfig(),
        ),
    )
    cancelled = False

    def runner(arguments: tuple[str, ...], **_kwargs: object) -> CommandResult:
        nonlocal cancelled
        if arguments[0] == "ffmpeg" and arguments[-1] != "-":
            Path(arguments[-1]).write_bytes(b"partial output")
            cancelled = True
            return CommandResult(returncode=0, stderr_summary="")
        return CommandResult(
            returncode=0,
            stderr_summary="",
            stdout_summary='{"format":{"duration":"4.0"},"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
        )

    with pytest.raises(RescueCancelledError):
        NativeRescueExecutor(runner=runner).execute_audio_improved(
            plan, source, tmp_path / "工作", lambda: cancelled
        )

    assert source.read_bytes() == source_bytes
    assert not (tmp_path / "工作" / "staging" / "improved-viewing.mp4").exists()
    assert not (tmp_path / "工作" / "staging" / "improved-viewing.partial.mp4").exists()
