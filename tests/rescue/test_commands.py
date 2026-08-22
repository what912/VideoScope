"""Tests for safe, argument-vector Rescue preview commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import JsonValue

from videoscope.domain import VideoMetadata
from videoscope.rescue.audio import (
    AudioDenoiseConfig,
    AudioNoiseInterval,
    FixedOffsetAssessment,
    LoudnessConfig,
    assess_audio,
)
from videoscope.rescue.commands import (
    build_audio_improvement_command,
    build_faithful_concat_command,
    build_faithful_remux_command,
    build_ffprobe_version_command,
    build_improved_viewing_command,
    build_packet_timestamp_probe_command,
    build_preview_commands,
    build_sharpen_qualification_command,
    previewed_improvement_action_ids,
)
from videoscope.rescue.executor import SourceMapping
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    make_damage_id,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.visual import (
    FlickerCorrectionPlan,
    VisualAssessment,
    VisualEvidence,
    VisualMetrics,
)


def _dark_visual_assessment(
    *ranges: tuple[float, float], noise_residual: float = 0.0
) -> VisualAssessment:
    return VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.05,
            luma_p50=0.08,
            luma_p90=0.12,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=noise_residual,
            sharpness=0.1,
        ),
        recommended_actions=(RescueActionKind.ADJUST_LUMA,),
        evidence=tuple(
            VisualEvidence(
                action=RescueActionKind.ADJUST_LUMA,
                timestamp_seconds=(start + end) / 2.0,
                metric="luma_p10",
                observed=0.05,
                threshold=0.18,
                context_luma_p50=0.08,
            )
            for start, end in ranges
        ),
        preview_required=True,
        public_explanation="Measured dark samples support a preview.",
    )


def _plan_deleting_2_to_3(
    *,
    undecodable_range: tuple[float, float] = (2.0, 3.0),
    dark_ranges: tuple[tuple[float, float], ...] = ((1.0, 4.0),),
    max_preview_total_seconds: float = 3.0,
    noise_residual: float = 0.0,
) -> RescuePlan:
    source_hash = "a" * 64
    undecodable_start, undecodable_end = undecodable_range
    undecodable = DamageInterval(
        id=make_damage_id(
            source_hash,
            "video:0",
            DamageKind.UNDECODABLE,
            undecodable_start,
            undecodable_end,
        ),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=undecodable_start,
        end_seconds=undecodable_end,
    )
    dark = tuple(
        DamageInterval(
            id=make_damage_id(source_hash, "video:0", DamageKind.DARK, start, end),
            stream_id="video:0",
            kind=DamageKind.DARK,
            start_seconds=start,
            end_seconds=end,
        )
        for start, end in dark_ranges
    )
    return build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=6.0,
            average_frame_rate=30.0,
            estimated_frame_count=180,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=6.0,
            intervals=(undecodable, *dark),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(
            max_preview_total_seconds=max_preview_total_seconds
        ),
        visual_assessment=_dark_visual_assessment(
            *dark_ranges, noise_residual=noise_residual
        ),
    )


def test_preview_command_rejects_stale_plan_digest(
    tmp_path: Path,
) -> None:
    """A command cannot be built from a plan whose digest no longer matches it."""
    plan = _plan_deleting_2_to_3()
    tampered = RescuePlan.model_construct(
        **{
            **{
                field_name: getattr(plan, field_name)
                for field_name in RescuePlan.model_fields
            },
            "plan_digest": "f" * 64,
        }
    )

    with pytest.raises(ValueError, match="plan digest"):
        build_preview_commands(tampered, tmp_path / "source.mp4", tmp_path / "preview")


def test_direct_command_boundaries_reject_stale_plan_digest(tmp_path: Path) -> None:
    plan = _plan_deleting_2_to_3()
    tampered = RescuePlan.model_construct(
        **{
            **{
                field_name: getattr(plan, field_name)
                for field_name in RescuePlan.model_fields
            },
            "plan_digest": "f" * 64,
        }
    )
    mapping = (SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),)
    with pytest.raises(ValueError, match="plan digest"):
        build_improved_viewing_command(
            tampered,
            tmp_path / "faithful.mp4",
            tmp_path / "improved.mp4",
            source_mappings=mapping,
            force_video_encode=True,
        )
    with pytest.raises(ValueError, match="plan digest"):
        build_improved_viewing_command(
            tampered,
            tmp_path / "faithful.mp4",
            tmp_path / "draft-improved.mp4",
            source_mappings=mapping,
            force_video_encode=True,
            _allow_unqualified_sharpen_draft=True,
        )
    with pytest.raises(ValueError, match="plan digest"):
        build_sharpen_qualification_command(
            tampered,
            tmp_path / "faithful.mp4",
            tmp_path / "candidate.mp4",
            source_ranges=((1.0, 2.0),),
            parameters={"amount": 1.0},
            mode="baseline",
            source_mappings=mapping,
        )
    with pytest.raises(ValueError, match="plan digest"):
        build_sharpen_qualification_command(
            tampered,
            tmp_path / "faithful.mp4",
            tmp_path / "draft-candidate.mp4",
            source_ranges=((1.0, 2.0),),
            parameters={"amount": 1.0},
            mode="baseline",
            source_mappings=mapping,
            _allow_unqualified_sharpen_draft=True,
        )
    with pytest.raises(ValueError, match="plan digest"):
        previewed_improvement_action_ids(tampered, mapping)


def test_audio_offset_render_preserves_the_source_packet_time_origin(
    tmp_path: Path,
) -> None:
    """Catches the muxer shifting video timestamps for AAC encoder priming."""
    command = build_audio_improvement_command(
        tmp_path / "source.mp4",
        tmp_path / "corrected.mp4",
        (RescueActionKind.CORRECT_FIXED_AV_OFFSET,),
        {
            "offset_seconds": 0.4,
            "audio_shift_seconds": -0.4,
            "minimum_correlation": 0.85,
            "minimum_event_count": 3,
            "maximum_agreement_seconds": 0.04,
            "maximum_absolute_offset_seconds": 2.0,
            "correlation": 0.99,
            "matched_event_count": 3,
            "agreement_seconds": 0.01,
        },
    )

    assert command[command.index("-avoid_negative_ts") + 1] == "disabled"


def test_sharpen_qualification_commands_share_encode_and_exact_range(
    tmp_path: Path,
) -> None:
    source_hash = "a" * 64
    soft = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SOFT_DETAIL, 0.5, 1.5),
        stream_id="video:0",
        kind=DamageKind.SOFT_DETAIL,
        start_seconds=0.5,
        end_seconds=1.5,
    )
    assessment = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.1,
            luma_p50=0.05,
            luma_p90=0.4,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.01,
            sharpness=0.01,
        ),
        recommended_actions=(RescueActionKind.SHARPEN,),
        evidence=(
            VisualEvidence(
                action=RescueActionKind.SHARPEN,
                timestamp_seconds=1.0,
                metric="scene_relative_sharpness",
                observed=0.01,
                threshold=0.03,
                scene_baseline_sharpness=0.04,
            ),
        ),
        preview_required=True,
        public_explanation="Measured soft detail supports qualification.",
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=2.0,
            average_frame_rate=24.0,
            estimated_frame_count=48,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=2.0,
            intervals=(soft,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=assessment,
    )
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.SHARPEN
    )
    mapping = (SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),)
    commands = {
        mode: build_sharpen_qualification_command(
            plan,
            tmp_path / "faithful.mp4",
            tmp_path / f"{mode}.mp4",
            source_ranges=action.source_ranges,
            parameters=action.parameters,
            mode=mode,
            source_mappings=mapping,
        )
        for mode in ("baseline", "visibility", "candidate")
    }
    filters = {
        mode: command[command.index("-filter:v:0") + 1]
        for mode, command in commands.items()
    }
    assert filters["baseline"] == "null"
    assert "eq=brightness=" in filters["visibility"]
    assert "cas=strength=0" in filters["visibility"]
    assert filters["visibility"].count("unsharp=5:5:0:5:5:0") == 3
    assert "between(t\\,0.5\\,1.5)" in filters["visibility"]
    assert "min(1\\,max(0\\," in filters["visibility"]
    assert "eval=frame" in filters["visibility"]
    assert "eq=brightness=" in filters["candidate"]
    assert "cas=" in filters["candidate"]
    assert "unsharp=" in filters["candidate"]
    assert "between(t\\,0.5\\,1.5)" in filters["candidate"]
    assert "min(1\\,max(0\\," in filters["candidate"]
    assert "between(t,0.5,1.5)" not in filters["candidate"]
    runtime_visibility = build_improved_viewing_command(
        plan,
        tmp_path / "faithful.mp4",
        tmp_path / "runtime-visibility.mp4",
        source_mappings=mapping,
        sharpen_mode="visibility",
        force_video_encode=True,
        _allow_unqualified_sharpen_draft=True,
    )
    runtime_filter = runtime_visibility[runtime_visibility.index("-filter:v:0") + 1]
    assert "between(t\\,0.5\\,1.5)" in runtime_filter
    assert "cas=strength=0" in runtime_filter
    assert runtime_filter.count("unsharp=5:5:0:5:5:0") == 3
    for command in commands.values():
        assert command[command.index("-c:v:0") + 1] == "libx264"
        assert command[command.index("-crf:v:0") + 1] == "16"
        assert command[command.index("-pix_fmt:v:0") + 1] == "yuv420p"
        assert command[command.index("-c:a") + 1] == "copy"


def test_ordinary_faithful_reencode_preserves_default_b_frame_behavior(
    tmp_path: Path,
) -> None:
    command = build_faithful_remux_command(
        tmp_path / "source.mp4",
        tmp_path / "faithful.mp4",
        stream_copy=False,
    )

    assert "-bf" not in command


def test_fixed_offset_faithful_remux_disables_b_frame_timestamp_reordering(
    tmp_path: Path,
) -> None:
    command = build_faithful_remux_command(
        tmp_path / "source.mp4",
        tmp_path / "faithful.mp4",
        stream_copy=False,
        audio_filter="asetpts=PTS-0.4/TB",
    )

    assert command[command.index("-bf:v:0") + 1] == "0"


def test_faithful_concat_applies_fixed_offset_without_a_third_audio_encode(
    tmp_path: Path,
) -> None:
    command = build_faithful_concat_command(
        tmp_path / "segments.txt",
        tmp_path / "faithful.mp4",
        audio_filter="asetpts=PTS-0.4/TB",
    )

    assert command[command.index("-filter:a:0") + 1] == "asetpts=PTS-0.4/TB"
    assert command[command.index("-avoid_negative_ts") + 1] == "disabled"
    assert command[command.index("-bf:v:0") + 1] == "0"


def test_ordinary_faithful_concat_preserves_default_b_frame_behavior(
    tmp_path: Path,
) -> None:
    command = build_faithful_concat_command(
        tmp_path / "segments.txt",
        tmp_path / "faithful.mp4",
    )

    assert "-bf" not in command


def test_fixed_offset_codec_reference_uses_the_same_video_timestamp_encoding(
    tmp_path: Path,
) -> None:
    remux = build_faithful_remux_command(
        tmp_path / "source.mp4",
        tmp_path / "reference-remux.mp4",
        stream_copy=False,
        preserve_packet_origin=True,
    )
    concat = build_faithful_concat_command(
        tmp_path / "segments.txt",
        tmp_path / "reference-concat.mp4",
        preserve_packet_origin=True,
    )

    for command in (remux, concat):
        assert command[command.index("-avoid_negative_ts") + 1] == "disabled"
        assert command[command.index("-bf:v:0") + 1] == "0"


def _plan_with_locked_middle_deflicker() -> RescuePlan:
    source_hash = "c" * 64
    flicker = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.FLICKER, 1.0, 4.0),
        stream_id="video:0",
        kind=DamageKind.FLICKER,
        start_seconds=1.0,
        end_seconds=4.0,
    )
    locked_ranges = ((2.0, 3.0),)
    return build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=6.0,
            average_frame_rate=30.0,
            estimated_frame_count=180,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=6.0,
            intervals=(flicker,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(locked_ranges=locked_ranges),
        locked_ranges=locked_ranges,
        flicker_correction=FlickerCorrectionPlan(
            intervals=((1.0, 4.0),),
            gains=((1.0, 1.0), (2.0, 1.1), (3.0, 1.2), (4.0, 1.0)),
        ),
    )


def test_preview_commands_keep_unicode_source_path_as_one_argument(
    tmp_path: Path,
) -> None:
    """Catches shell-like command construction that splits a user path."""
    source = tmp_path / "private folder" / "中文 source name.mp4"
    damage = DamageInterval(
        id=make_damage_id("b" * 64, "video:0", DamageKind.DARK, 2.0, 5.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=2.0,
        end_seconds=5.0,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename=source.name,
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=8.0,
            average_frame_rate=30.0,
            estimated_frame_count=240,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash="b" * 64, duration_seconds=8.0, intervals=(damage,)
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=_dark_visual_assessment((2.0, 5.0)),
    )

    commands = build_preview_commands(plan, source, tmp_path / "private review")

    assert commands
    assert all(isinstance(command, list) for command in commands)
    source_commands = [command for command in commands if str(source) in command]
    assert len(source_commands) == 2
    assert all(command.count(str(source)) == 1 for command in source_commands)
    assert all(" ".join(command).count(str(source)) == 1 for command in source_commands)


def test_preview_commands_decode_the_same_non_keyframe_interval_for_every_variant(
    tmp_path: Path,
) -> None:
    """Catches a stream-copy variant silently expanding a requested preview range."""
    source = tmp_path / "source.mp4"
    damage = DamageInterval(
        id=make_damage_id("d" * 64, "video:0", DamageKind.DARK, 2.375, 5.625),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=2.375,
        end_seconds=5.625,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=8.0,
            average_frame_rate=30.0,
            estimated_frame_count=240,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash="d" * 64, duration_seconds=8.0, intervals=(damage,)
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=_dark_visual_assessment((2.375, 5.625)),
    )

    commands = build_preview_commands(plan, source, tmp_path / "private review")

    source_windows = {
        (command[command.index("-ss") + 1], command[command.index("-t") + 1])
        for command in commands
        if str(source) in command
    }
    derived_windows = {
        (command[command.index("-ss") + 1], command[command.index("-t") + 1])
        for command in commands
        if str(source) not in command
    }
    assert source_windows == {("2.375", "3.25")}
    assert derived_windows == {("0", "3.25")}
    assert all(command.index("-i") < command.index("-ss") for command in commands)
    assert all("copy" not in command for command in commands)
    assert all("libx264" in command and "aac" in command for command in commands)


def test_improvement_filter_uses_mapped_authorized_ranges_only(
    tmp_path: Path,
) -> None:
    """Catches applying a reviewed improvement globally after faithful salvage."""
    source = tmp_path / "faithful-rescue.mp4"
    output = tmp_path / "improved-viewing.mp4"
    damage = DamageInterval(
        id=make_damage_id("e" * 64, "video:0", DamageKind.DARK, 2.5, 5.5),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=2.5,
        end_seconds=5.5,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=8.0,
            average_frame_rate=30.0,
            estimated_frame_count=240,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash="e" * 64, duration_seconds=8.0, intervals=(damage,)
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=_dark_visual_assessment((2.5, 5.5)),
    )

    command = build_improved_viewing_command(
        plan,
        source,
        output,
        source_mappings=(
            SourceMapping(0.0, 2.0, 0.0, 2.0, "faithful-rescue.mp4"),
            SourceMapping(4.0, 8.0, 2.0, 6.0, "faithful-rescue.mp4"),
        ),
    )

    video_filter = command[command.index("-filter:v:0") + 1]
    assert "gte(t,2)*lt(t,3.5)" in video_filter
    assert "gte(t,0)*lt(t,6)" not in video_filter


def test_locked_deflicker_gap_is_not_present_in_filter(tmp_path: Path) -> None:
    """Catches an embedded deflicker curve crossing a confirmed locked range."""
    command = build_improved_viewing_command(
        _plan_with_locked_middle_deflicker(),
        tmp_path / "faithful-rescue.mp4",
        tmp_path / "improved-viewing.mp4",
        source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
    )

    filter_text = command[command.index("-filter:v:0") + 1]
    assert "gte(t,1)*lt(t,2)" in filter_text
    assert "gte(t,2)*lt(t,3)" not in filter_text
    assert "gte(t,3)*lt(t,4)" in filter_text


def test_improved_render_uses_explicit_high_quality_encoding_contract(
    tmp_path: Path,
) -> None:
    plan = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0))
    command = build_improved_viewing_command(
        plan,
        tmp_path / "faithful-rescue.mp4",
        tmp_path / "improved-viewing.mp4",
        source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
        audio_sample_rate_hz=48000,
    )

    assert command[command.index("-preset:v:0") + 1] == "medium"
    assert command[command.index("-crf:v:0") + 1] == "16"
    assert command[command.index("-pix_fmt:v:0") + 1] == "yuv420p"
    assert command[command.index("-profile:v:0") + 1] == "high"
    assert command[command.index("-level:v:0") + 1] == "3.1"
    assert command[command.index("-fps_mode:v:0") + 1] == "cfr"
    assert command[command.index("-video_track_timescale") + 1] == "120000"
    assert command[command.index("-g:v:0") + 1] == "48"
    assert command[command.index("-keyint_min:v:0") + 1] == "24"
    assert command[command.index("-bf:v:0") + 1] == "0"
    assert command[command.index("-refs:v:0") + 1] == "3"
    assert command[command.index("-sc_threshold:v:0") + 1] == "0"
    assert "-chromaoffset" not in command
    if "-filter:a:0" in command:
        assert command[command.index("-ar:a:0") + 1] == "48000"
        assert command[command.index("-b:a:0") + 1] == "192k"


def test_noise_guarded_luma_command_uses_bound_y_offset_without_gamma(
    tmp_path: Path,
) -> None:
    plan = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0), noise_residual=0.03)
    command = build_improved_viewing_command(
        plan,
        tmp_path / "faithful-rescue.mp4",
        tmp_path / "improved-viewing.mp4",
        source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    video_filter = command[command.index("-filter:v:0") + 1]
    assert action.parameters["filter_mode"] == "noise_guarded_y_offset"
    brightness = action.parameters["brightness"]
    minimum_delta = action.parameters["minimum_perceptible_luma_delta"]
    assert isinstance(brightness, (int, float)) and not isinstance(brightness, bool)
    assert isinstance(minimum_delta, (int, float)) and not isinstance(
        minimum_delta, bool
    )
    expected_lift_steps = round(
        max(
            float(brightness),
            float(minimum_delta),
        )
        * 255
        * 2
    )
    assert action.parameters["luma_lift_steps"] == expected_lift_steps
    assert action.parameters["noise_guard_video_crf"] == 23
    assert action.parameters["noise_guard_chroma_qp_offset"] == -6
    assert video_filter.startswith(f"lutyuv=y='val+{expected_lift_steps}'")
    assert "hqdn3d" not in video_filter
    assert "lutrgb" not in video_filter
    assert ":enable='gte(t,1)*lt(t,4)'" in video_filter
    assert "eq=" not in video_filter
    assert command[command.index("-crf:v:0") + 1] == "23"
    assert command[command.index("-chromaoffset") + 1] == "-6"
    assert command.count("-chromaoffset") == 1
    assert command.index("-chromaoffset") > command.index("-video_track_timescale")

    preview_commands = build_preview_commands(
        plan,
        tmp_path / "输入 source.mp4",
        tmp_path / "private review",
    )
    improved_preview = next(
        item
        for item in preview_commands
        if Path(item[-1]).name.startswith("improved-") and "-vf" in item
    )
    preview_filter = improved_preview[improved_preview.index("-vf") + 1]
    assert preview_filter.startswith(f"lutyuv=y='val+{expected_lift_steps}'")
    assert "hqdn3d" not in preview_filter
    assert improved_preview[improved_preview.index("-crf:v:0") + 1] == "23"
    assert improved_preview[improved_preview.index("-chromaoffset") + 1] == "-6"
    assert improved_preview.count("-chromaoffset") == 1
    assert improved_preview.index("-chromaoffset") > improved_preview.index(
        "-video_track_timescale"
    )
    for preview_command in preview_commands:
        if preview_command is not improved_preview:
            assert "-chromaoffset" not in preview_command


def test_preview_and_final_visual_commands_share_canonical_video_contract(
    tmp_path: Path,
) -> None:
    plan = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0))
    config = plan.effective_config
    final_commands = (
        build_faithful_remux_command(
            tmp_path / "输入 source.mp4",
            tmp_path / "faithful-rescue.mp4",
            stream_copy=False,
            encode_config=config,
        ),
        build_improved_viewing_command(
            plan,
            tmp_path / "faithful-rescue.mp4",
            tmp_path / "improved-viewing.mp4",
            source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
            audio_sample_rate_hz=48000,
        ),
    )
    preview_commands = build_preview_commands(
        plan, tmp_path / "输入 source.mp4", tmp_path / "private review"
    )

    for command in (*final_commands, *preview_commands):
        assert command[command.index("-c:v:0") + 1] == config.video_encoder
        assert command[command.index("-preset:v:0") + 1] == (
            config.improved_video_preset
        )
        assert command[command.index("-crf:v:0") + 1] == str(config.improved_video_crf)
        assert command[command.index("-pix_fmt:v:0") + 1] == (
            config.improved_pixel_format
        )
        assert command[command.index("-profile:v:0") + 1] == config.video_profile
        assert command[command.index("-level:v:0") + 1] == config.video_level
        assert command[command.index("-fps_mode:v:0") + 1] == config.video_fps_mode
        assert command[command.index("-video_track_timescale") + 1] == str(
            config.video_track_timescale
        )


def test_command_builder_rejects_tampered_action_encode_contract(
    tmp_path: Path,
) -> None:
    plan = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0))
    action = next(item for item in plan.actions if item.changes_content)
    raw_contract = action.parameters["video_encode_contract"]
    assert isinstance(raw_contract, dict)
    tampered_contract = dict(raw_contract)
    tampered_contract["crf"] = 18
    object.__setattr__(
        action,
        "parameters",
        {**action.parameters, "video_encode_contract": tampered_contract},
    )

    with pytest.raises(ValueError, match="video encode contract"):
        build_improved_viewing_command(
            plan,
            tmp_path / "faithful-rescue.mp4",
            tmp_path / "improved-viewing.mp4",
            source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
            audio_sample_rate_hz=48000,
        )


def test_command_builder_rejects_semantic_luma_tamper_with_recomputed_ids(
    tmp_path: Path,
) -> None:
    plan = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0), noise_residual=0.03)
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    parameters = dict(action.parameters)
    lift_steps = parameters["luma_lift_steps"]
    assert isinstance(lift_steps, int) and not isinstance(lift_steps, bool)
    parameters["luma_lift_steps"] = lift_steps + 1
    tampered_action = action.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=action.kind,
                parameters=parameters,
                source_ranges=action.source_ranges,
                strategy=action.strategy,
                version=action.version,
            ),
        }
    )
    payload = cast(
        dict[str, JsonValue], plan.model_dump(mode="json", exclude={"plan_digest"})
    )
    payload["actions"] = [
        tampered_action.model_dump(mode="json")
        if item.id == action.id
        else item.model_dump(mode="json")
        for item in plan.actions
    ]
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    tampered_plan = plan
    object.__setattr__(
        tampered_plan,
        "actions",
        tuple(
            tampered_action if item.id == action.id else item for item in plan.actions
        ),
    )
    object.__setattr__(tampered_plan, "plan_digest", payload["plan_digest"])

    with pytest.raises(ValueError, match="ADJUST_LUMA action wire"):
        build_improved_viewing_command(
            tampered_plan,
            tmp_path / "faithful-rescue.mp4",
            tmp_path / "improved-viewing.mp4",
            source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
        )


def _plan_with_luma_evidence(
    evidence: dict[str, JsonValue] | list[dict[str, JsonValue]],
) -> RescuePlan:
    plan = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0), noise_residual=0.03)
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    normalized_evidence: list[JsonValue] = (
        cast(list[JsonValue], evidence) if isinstance(evidence, list) else [evidence]
    )
    parameters: dict[str, JsonValue] = {
        **action.parameters,
        "assessment_evidence": normalized_evidence,
    }
    rebound = action.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=action.kind,
                parameters=parameters,
                source_ranges=action.source_ranges,
                strategy=action.strategy,
                version=action.version,
            ),
        }
    )
    payload = cast(
        dict[str, JsonValue], plan.model_dump(mode="json", exclude={"plan_digest"})
    )
    payload["actions"] = [
        rebound.model_dump(mode="json")
        if item.id == action.id
        else item.model_dump(mode="json")
        for item in plan.actions
    ]
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return RescuePlan.model_validate_json(json.dumps(payload))


def test_command_builder_accepts_canonical_json_luma_evidence_enum(
    tmp_path: Path,
) -> None:
    plan = _plan_with_luma_evidence(
        {
            "action": "adjust_luma",
            "timestamp_seconds": 1.0,
            "metric": "luma_p10",
            "observed": 0.05,
            "threshold": 0.18,
            "context_luma_p50": 0.08,
            "scene_baseline_sharpness": None,
        }
    )

    command = build_improved_viewing_command(
        plan,
        tmp_path / "faithful-rescue.mp4",
        tmp_path / "improved-viewing.mp4",
        source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
    )

    assert command[command.index("-crf:v:0") + 1] == "23"


@pytest.mark.parametrize(
    "assessment_evidence",
    (
        [],
        [
            {
                "action": "adjust_luma",
                "timestamp_seconds": 1.0,
                "metric": "noise_residual",
                "observed": 0.05,
                "threshold": 0.18,
                "context_luma_p50": 0.08,
                "scene_baseline_sharpness": None,
            }
        ],
        [
            {
                "action": "adjust_luma",
                "timestamp_seconds": 1.0,
                "metric": "luma_p10",
                "observed": 0.05,
                "threshold": 0.99,
                "context_luma_p50": 0.08,
                "scene_baseline_sharpness": None,
            }
        ],
        [
            {
                "action": "adjust_luma",
                "timestamp_seconds": 4.0,
                "metric": "luma_p10",
                "observed": 0.05,
                "threshold": 0.18,
                "context_luma_p50": 0.08,
                "scene_baseline_sharpness": None,
            }
        ],
        [
            {
                "action": "adjust_luma",
                "timestamp_seconds": 1.0,
                "metric": "luma_p10",
                "observed": 0.05,
                "threshold": 0.18,
                "context_luma_p50": 0.08,
                "scene_baseline_sharpness": None,
            },
            {
                "action": "adjust_luma",
                "timestamp_seconds": 1.0,
                "metric": "luma_p10",
                "observed": 0.05,
                "threshold": 0.18,
                "context_luma_p50": 0.08,
                "scene_baseline_sharpness": None,
            },
        ],
        [
            {
                "action": "adjust_luma",
                "timestamp_seconds": 1.0,
                "metric": "luma_p10",
                "observed": 0.19,
                "threshold": 0.18,
                "context_luma_p50": 0.08,
                "scene_baseline_sharpness": None,
            }
        ],
        [
            {
                "action": "adjust_luma",
                "timestamp_seconds": 1.0,
                "metric": "luma_p10",
                "observed": 0.05,
                "threshold": 0.18,
                "context_luma_p50": 0.09,
                "scene_baseline_sharpness": None,
            }
        ],
    ),
    ids=(
        "empty",
        "wrong-metric",
        "wrong-threshold",
        "half-open-end",
        "duplicate",
        "trigger-false",
        "context-mismatch",
    ),
)
def test_plan_parser_rejects_semantically_false_luma_evidence_with_rebound_ids(
    assessment_evidence: list[dict[str, JsonValue]],
) -> None:
    with pytest.raises(ValueError, match="ADJUST_LUMA assessment evidence"):
        _plan_with_luma_evidence(assessment_evidence)


@pytest.mark.parametrize(
    "evidence_update",
    (
        {"action": "sharpen"},
        {"action": "unknown_action"},
        {"unexpected": 1},
        {"timestamp_seconds": "1.0"},
    ),
)
def test_command_builder_rejects_noncanonical_luma_evidence(
    evidence_update: dict[str, JsonValue],
) -> None:
    evidence: dict[str, JsonValue] = {
        "action": "adjust_luma",
        "timestamp_seconds": 1.0,
        "metric": "luma_p10",
        "observed": 0.05,
        "threshold": 0.18,
        "context_luma_p50": 0.08,
        "scene_baseline_sharpness": None,
        **evidence_update,
    }
    with pytest.raises(ValueError, match="ADJUST_LUMA assessment evidence"):
        _plan_with_luma_evidence(evidence)


def test_faithful_and_improved_previews_share_one_confirmed_noise_cleanup(
    tmp_path: Path,
) -> None:
    """Catches omitting denoise from faithful or applying it twice to improved."""
    source_hash = "9" * 64
    audio = assess_audio(
        {
            "input_i": -29.5,
            "input_tp": -20.8,
            "input_lra": 17.2,
            "input_thresh": -39.5,
            "target_offset": -1.6,
            "noise_floor_dbfs": -32.0,
            "noise_confidence": 0.9,
            "noise_event_count": 10,
            "noise_intervals": (
                AudioNoiseInterval(
                    start_seconds=5.0,
                    end_seconds=10.0,
                    rms_dbfs=-32.0,
                    spectral_centroid_hz=122.0,
                    tone_frequencies_hz=(60.0, 118.0),
                    confidence=0.9,
                ),
                AudioNoiseInterval(
                    start_seconds=25.0,
                    end_seconds=32.0,
                    rms_dbfs=-24.0,
                    spectral_centroid_hz=880.0,
                    tone_frequencies_hz=(880.0,),
                    confidence=0.95,
                ),
            ),
        },
        LoudnessConfig(),
        AudioDenoiseConfig(),
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=320,
            height=180,
            duration_seconds=42.0,
            average_frame_rate=24.0,
            estimated_frame_count=1008,
            has_audio=True,
            file_size_bytes=1,
            raw_probe={"audio_sample_rate_hz": 48000},
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=42.0,
            scan_coverage=((0.0, 42.0),),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        audio_assessment=audio,
    )

    commands = build_preview_commands(plan, tmp_path / "source.mp4", tmp_path)
    faithful_render = build_improved_viewing_command(
        plan,
        tmp_path / "faithful-source.mp4",
        tmp_path / "faithful-restored.mp4",
        source_mappings=(SourceMapping(0.0, 42.0, 0.0, 42.0, "faithful-source.mp4"),),
        excluded_action_ids=frozenset(
            action.id
            for action in plan.actions
            if action.kind is not RescueActionKind.DENOISE_AUDIO
        ),
        audio_sample_rate_hz=48000,
    )

    faithful_audio = faithful_render[faithful_render.index("-filter:a:0") + 1]
    improved_audio = ",".join(
        command[command.index("-af") + 1]
        for command in commands
        if Path(command[-1]).name.startswith("improved-") and "-af" in command
    )

    assert "afftdn=" in faithful_audio
    assert "bandreject=f=60" in faithful_audio
    assert "bandreject=f=118" in faithful_audio
    assert "bandreject=f=880" in faithful_audio
    assert faithful_audio.count(":enable=") == 5
    assert "gte(t,4.75)*lt(t,10.25)" in faithful_audio
    assert "gte(t,24.75)*lt(t,32.25)" in faithful_audio
    first_interval = next(
        item for item in faithful_audio.split(",") if item.startswith("bandreject=f=60")
    )
    second_interval = next(
        item
        for item in faithful_audio.split(",")
        if item.startswith("bandreject=f=880")
    )
    assert "gte(t,24.75)*lt(t,32.25)" not in first_interval
    assert "gte(t,4.75)*lt(t,10.25)" not in second_interval
    assert faithful_render[faithful_render.index("-ar:a:0") + 1] == "48000"
    assert "loudnorm=" in improved_audio
    assert "afftdn=" not in improved_audio


def test_command_builder_rejects_chroma_qp_tamper_with_recomputed_ids(
    tmp_path: Path,
) -> None:
    plan = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0), noise_residual=0.03)
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    assert action.parameters["noise_guard_chroma_qp_offset"] == -6
    parameters = {**action.parameters, "noise_guard_chroma_qp_offset": -8}
    tampered_action = action.model_copy(
        update={
            "parameters": parameters,
            "id": make_rescue_action_id(
                kind=action.kind,
                parameters=parameters,
                source_ranges=action.source_ranges,
                strategy=action.strategy,
                version=action.version,
            ),
        }
    )
    payload = cast(
        dict[str, JsonValue], plan.model_dump(mode="json", exclude={"plan_digest"})
    )
    payload["actions"] = [
        tampered_action.model_dump(mode="json")
        if item.id == action.id
        else item.model_dump(mode="json")
        for item in plan.actions
    ]
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    object.__setattr__(
        plan,
        "actions",
        tuple(
            tampered_action if item.id == action.id else item for item in plan.actions
        ),
    )
    object.__setattr__(plan, "plan_digest", payload["plan_digest"])

    with pytest.raises(ValueError, match="ADJUST_LUMA action wire"):
        build_improved_viewing_command(
            plan,
            tmp_path / "faithful-rescue.mp4",
            tmp_path / "improved-viewing.mp4",
            source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
            audio_sample_rate_hz=48000,
        )


def test_multistage_visual_restoration_is_bounded_to_confirmed_range(
    tmp_path: Path,
) -> None:
    """Catches eq/CAS leaking globally while only unsharp has an enable gate."""
    source_hash = "7" * 64
    soft = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.SOFT_DETAIL, 4.0, 10.0),
        stream_id="video:0",
        kind=DamageKind.SOFT_DETAIL,
        start_seconds=4.0,
        end_seconds=10.0,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=320,
            height=180,
            duration_seconds=42.0,
            average_frame_rate=24.0,
            estimated_frame_count=1008,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=42.0,
            intervals=(soft,),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=VisualAssessment(
            metrics=VisualMetrics(
                luma_p10=0.03,
                luma_p50=0.04,
                luma_p90=0.12,
                low_clip_ratio=0.0,
                high_clip_ratio=0.0,
                noise_residual=0.01,
                sharpness=0.04,
            ),
            recommended_actions=(RescueActionKind.SHARPEN,),
            preview_required=True,
            public_explanation="Measured local softness supports private restoration.",
        ),
    )

    command = build_improved_viewing_command(
        plan,
        tmp_path / "faithful-rescue.mp4",
        tmp_path / "improved-viewing.mp4",
        source_mappings=(SourceMapping(0.0, 42.0, 0.0, 42.0, "faithful-rescue.mp4"),),
        _allow_unqualified_sharpen_draft=True,
    )
    video_filter = command[command.index("-filter:v:0") + 1]

    assert video_filter.startswith("eq=brightness=")
    assert "eval=frame" in video_filter
    assert "between(t\\,4\\,10)" in video_filter
    assert "min(1\\,max(0\\," in video_filter
    assert "between(t,4,10)" not in video_filter
    assert video_filter.count(":enable='gte(t,4)*lt(t,10)'") == 2

    with pytest.raises(ValueError, match="qualification is missing"):
        build_preview_commands(plan, tmp_path / "source.mp4", tmp_path)


def test_improved_audio_filter_requires_a_valid_source_sample_rate(
    tmp_path: Path,
) -> None:
    base = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0))
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=6.0,
            average_frame_rate=30.0,
            estimated_frame_count=180,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=base.input_hash,
            duration_seconds=6.0,
            intervals=base.damage_intervals,
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=_dark_visual_assessment((1.0, 4.0)),
        audio_assessment=assess_audio(
            {
                "input_i": -29.5,
                "input_tp": -20.8,
                "input_lra": 17.2,
                "input_thresh": -39.5,
                "target_offset": -1.6,
            },
            LoudnessConfig(),
        ),
    )
    with pytest.raises(ValueError, match="audio sample rate"):
        build_improved_viewing_command(
            plan,
            tmp_path / "faithful-rescue.mp4",
            tmp_path / "improved-viewing.mp4",
            source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
            audio_sample_rate_hz=0,
        )


def test_fixed_offset_is_not_applied_again_to_improved_candidate(
    tmp_path: Path,
) -> None:
    """Catches duplicating a faithful-only A/V correction in the improved view."""
    plan = _plan_deleting_2_to_3(undecodable_range=(5.0, 6.0))
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=6.0,
            average_frame_rate=30.0,
            estimated_frame_count=180,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=plan.input_hash,
            duration_seconds=6.0,
            intervals=tuple(
                item for item in plan.damage_intervals if item.kind is DamageKind.DARK
            ),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=_dark_visual_assessment((1.0, 4.0)),
        fixed_offset_assessment=FixedOffsetAssessment(
            offset_seconds=0.1,
            shift_seconds=-0.1,
            correlation=0.95,
            matched_event_count=3,
            agreement_seconds=0.01,
        ),
    )

    command = build_improved_viewing_command(
        plan,
        tmp_path / "faithful-rescue.mp4",
        tmp_path / "improved-viewing.mp4",
        source_mappings=(SourceMapping(0.0, 6.0, 0.0, 6.0, "faithful-rescue.mp4"),),
    )

    assert all("asetpts" not in argument for argument in command)


def test_faithful_preview_represents_structural_removal_and_rotation(
    tmp_path: Path,
) -> None:
    damage = DamageInterval(
        id=make_damage_id("f" * 64, "video:0", DamageKind.UNDECODABLE, 2.0, 3.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=2.0,
        end_seconds=3.0,
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=8.0,
            average_frame_rate=30.0,
            estimated_frame_count=240,
            has_audio=True,
            file_size_bytes=1,
            raw_probe={"rotation": 90},
        ),
        damage_map=MediaDamageMap(
            input_hash="f" * 64, duration_seconds=8.0, intervals=(damage,)
        ),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(),
    )

    commands = build_preview_commands(plan, tmp_path / "source.mp4", tmp_path)
    source_command, faithful_command = commands[:2]

    assert "-vf" not in source_command
    faithful_filter = faithful_command[faithful_command.index("-vf") + 1]
    preview_start = plan.preview_ranges[0][0]
    assert (
        "select='not(gte(t,"
        f"{2.0 - preview_start:g})*lt(t,{3.0 - preview_start:g}))'" in faithful_filter
    )
    assert "transpose=clock" in faithful_filter


def test_improved_preview_duration_uses_retained_duration(tmp_path: Path) -> None:
    """Catches an improved preview retaining the deleted interval as blank time."""
    source = tmp_path / "source.mp4"

    commands = build_preview_commands(
        _plan_deleting_2_to_3(), source, tmp_path / "private review"
    )

    improved = commands[2]
    assert improved[improved.index("-t") + 1] == "2"


def test_explicit_empty_mappings_do_not_fallback_to_source_time(
    tmp_path: Path,
) -> None:
    """Catches an explicitly empty retained timeline enabling source-time filters."""
    with pytest.raises(
        ValueError, match="confirmed plan contains no executable improvement filter"
    ):
        build_improved_viewing_command(
            _plan_deleting_2_to_3(),
            tmp_path / "faithful-rescue.mp4",
            tmp_path / "improved-viewing.mp4",
            source_mappings=(),
        )


def test_missing_mappings_after_faithful_deletion_fail_closed(
    tmp_path: Path,
) -> None:
    """Catches fabricating source-time identity after retained-range deletion."""
    with pytest.raises(
        ValueError, match="confirmed faithful source mapping is required"
    ):
        build_improved_viewing_command(
            _plan_deleting_2_to_3(),
            tmp_path / "faithful-rescue.mp4",
            tmp_path / "improved-viewing.mp4",
        )


def test_structural_preview_window_emits_retained_context_commands(
    tmp_path: Path,
) -> None:
    """Structural preview renders a positive retained boundary comparison."""
    plan = _plan_deleting_2_to_3(
        undecodable_range=(1.0, 5.0),
        dark_ranges=((1.0, 5.0),),
        max_preview_total_seconds=4.0,
    )

    commands = build_preview_commands(
        plan, tmp_path / "source.mp4", tmp_path / "private review"
    )

    assert tuple(Path(command[-1]).name for command in commands) == (
        "source-00.mp4",
        "faithful-00.mp4",
    )


def test_packet_timestamp_probe_is_packet_count_bounded_and_stream_indexed(
    tmp_path: Path,
) -> None:
    """Catches reverting residual verification to unbounded stream metadata."""
    source = tmp_path / "输入 packet source.mp4"

    command = build_packet_timestamp_probe_command(
        source, ffprobe="local-ffprobe", maximum_packets=96
    )

    assert command == [
        "local-ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_packets",
        "-read_intervals",
        "%+#96",
        "-show_entries",
        (
            "stream=index,codec_type,sample_rate:"
            "packet=stream_index,pts_time,dts_time,side_data_list"
        ),
        "-of",
        "json",
        str(source),
    ]
    assert build_ffprobe_version_command(ffprobe="local-ffprobe") == [
        "local-ffprobe",
        "-version",
    ]
