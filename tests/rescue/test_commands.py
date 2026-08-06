"""Tests for safe, argument-vector Rescue preview commands."""

from __future__ import annotations

from pathlib import Path

import pytest

from videoscope.domain import VideoMetadata
from videoscope.rescue.audio import FixedOffsetAssessment
from videoscope.rescue.commands import (
    build_audio_improvement_command,
    build_faithful_concat_command,
    build_faithful_remux_command,
    build_ffprobe_version_command,
    build_improved_viewing_command,
    build_packet_timestamp_probe_command,
    build_preview_commands,
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
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.visual import (
    FlickerCorrectionPlan,
    VisualAssessment,
    VisualMetrics,
)


def _plan_deleting_2_to_3(
    *,
    undecodable_range: tuple[float, float] = (2.0, 3.0),
    dark_ranges: tuple[tuple[float, float], ...] = ((1.0, 4.0),),
    max_preview_total_seconds: float = 3.0,
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
        visual_assessment=VisualAssessment(
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
            preview_required=True,
            public_explanation="Measured dark samples support a preview.",
        ),
    )


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

    assert command[command.index("-bf") + 1] == "0"


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
    assert command[command.index("-bf") + 1] == "0"


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
        assert command[command.index("-bf") + 1] == "0"


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
    )

    commands = build_preview_commands(plan, source, tmp_path / "private review")

    assert commands
    assert all(isinstance(command, list) for command in commands)
    assert all(command.count(str(source)) == 1 for command in commands)
    assert all(" ".join(command).count(str(source)) == 1 for command in commands)


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
    )

    commands = build_preview_commands(plan, source, tmp_path / "private review")

    windows = {
        (command[command.index("-ss") + 1], command[command.index("-t") + 1])
        for command in commands
    }
    assert windows == {("2.375", "3.25")}
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
        visual_assessment=VisualAssessment(
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
            preview_required=True,
            public_explanation="Measured dark samples support a preview.",
        ),
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
        visual_assessment=VisualAssessment(
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
            preview_required=True,
            public_explanation="Measured dark samples support a preview.",
        ),
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


def test_wholly_removed_preview_window_emits_no_media_command(
    tmp_path: Path,
) -> None:
    """Catches issuing a zero-duration faithful or improved preview artifact."""
    plan = _plan_deleting_2_to_3(
        undecodable_range=(1.0, 5.0),
        dark_ranges=((1.0, 5.0),),
        max_preview_total_seconds=4.0,
    )

    commands = build_preview_commands(
        plan, tmp_path / "source.mp4", tmp_path / "private review"
    )

    assert commands == ()


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
