"""Tests for bounded, private, same-range Rescue previews."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from videoscope.domain import VideoMetadata
from videoscope.rescue.errors import RescueArtifactError, RescueMediaError
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
from videoscope.rescue.preview import RescuePreviewBuilder, SubprocessPreviewRunner
from videoscope.rescue.visual import VisualAssessment, VisualMetrics


class FakeRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command: list[str]) -> None:
        self.commands.append(command)
        Path(command[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(command[-1]).write_bytes(b"preview")


def test_subprocess_preview_decodes_non_utf8_stderr_stably(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        arguments: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert arguments == ["ffmpeg", "中文 source.mp4"]
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        stderr = b"\xff\xfe local failure".decode(
            str(kwargs["encoding"]), str(kwargs["errors"])
        )
        return subprocess.CompletedProcess(arguments, 1, "", stderr)

    monkeypatch.setattr("videoscope.rescue.preview.subprocess.run", fake_run)

    with pytest.raises(RescueMediaError) as error:
        SubprocessPreviewRunner().run(["ffmpeg", "中文 source.mp4"])
    assert error.value.internal_message == "ffmpeg preview command failed"


def _plan(strategy: RescueStrategy) -> RescuePlan:
    damage = DamageInterval(
        id=make_damage_id("c" * 64, "video:0", DamageKind.DARK, 2.0, 8.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=2.0,
        end_seconds=8.0,
    )
    visual = VisualAssessment(
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
    )
    return build_rescue_plan(
        metadata=VideoMetadata(
            filename="private-customer-video.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=10.0,
            average_frame_rate=30.0,
            estimated_frame_count=300,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash="c" * 64, duration_seconds=10.0, intervals=(damage,)
        ),
        strategy=strategy,
        config=RescueEffectiveConfig(),
        visual_assessment=visual,
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


def test_preview_ranges_are_identical_across_variants(tmp_path: Path) -> None:
    """Catches an improved preview that compares different content than faithful."""
    runner = FakeRunner()
    source = tmp_path / "private-customer-video.mp4"
    source.write_bytes(b"original")

    previews = RescuePreviewBuilder(runner=runner).build(
        plan=_plan(RescueStrategy.BALANCED),
        source=source,
        private_review_root=tmp_path / "private review",
    )

    assert previews.improved is not None
    assert previews.source.time_ranges == previews.faithful.time_ranges
    assert previews.source.time_ranges == previews.improved.time_ranges
    assert previews.previewed_action_ids == tuple(
        action.id
        for action in _plan(RescueStrategy.BALANCED).actions
        if action.requires_confirmation
    )
    improved_commands = [
        command for command in runner.commands if "improved-" in Path(command[-1]).name
    ]
    assert improved_commands
    for command in improved_commands:
        input_path = Path(command[command.index("-i") + 1])
        assert input_path.name.startswith("faithful-")
        assert input_path.parent == Path(command[-1]).parent
    assert source.read_bytes() == b"original"
    assert all(source.name not in str(path) for path in previews.all_paths())


def test_faithful_preview_records_local_source_mappings(tmp_path: Path) -> None:
    """Catches private faithful media losing its rebased source lineage."""
    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        _plan_deleting_2_to_3(),
        tmp_path / "source.mp4",
        tmp_path / "private review",
    )

    mappings = previews.faithful.source_mappings
    assert [
        (item.source_start, item.source_end, item.output_start, item.output_end)
        for item in mappings
    ] == [
        (1.0, 2.0, 0.0, 1.0),
        (3.0, 4.0, 1.0, 2.0),
    ]
    assert {item.output_relative_path for item in mappings} == {"faithful-00.mp4"}


def test_deleted_only_overlap_does_not_mark_improvement_as_previewed(
    tmp_path: Path,
) -> None:
    """Catches deleted pixels authorizing an unseen retained-content change."""
    plan = _plan_deleting_2_to_3(dark_ranges=((2.0, 3.0), (4.0, 5.0)))

    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan,
        tmp_path / "source.mp4",
        tmp_path / "private review",
    )

    adjust_luma = next(
        action for action in plan.actions if action.kind is RescueActionKind.ADJUST_LUMA
    )
    assert adjust_luma.id not in previews.previewed_action_ids


def test_wholly_removed_preview_is_review_gated_with_bounded_reason(
    tmp_path: Path,
) -> None:
    """Catches an empty comparison authorizing a plan or leaking path details."""
    plan = _plan_deleting_2_to_3(
        undecodable_range=(1.0, 5.0),
        dark_ranges=((1.0, 5.0),),
        max_preview_total_seconds=4.0,
    )

    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan,
        tmp_path / "source.mp4",
        tmp_path / "private review",
    )

    assert previews.all_paths() == ()
    assert previews.previewed_action_ids == ()
    assert previews.review_reasons == (
        "A selected preview window retained no media; confirmation requires a "
        "different representative window.",
    )


def test_preview_records_only_actions_intersecting_an_issued_preview(
    tmp_path: Path,
) -> None:
    """An unrelated bounded preview cannot authorize an unshown action."""
    source_hash = "d" * 64
    structural = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.UNDECODABLE, 1.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=1.0,
        end_seconds=2.0,
    )
    dark = DamageInterval(
        id=make_damage_id(source_hash, "video:0", DamageKind.DARK, 7.0, 8.0),
        stream_id="video:0",
        kind=DamageKind.DARK,
        start_seconds=7.0,
        end_seconds=8.0,
    )
    visual = VisualAssessment(
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
    )
    plan = build_rescue_plan(
        metadata=VideoMetadata(
            filename="source.mp4",
            container_format="mp4",
            codec="h264",
            width=1280,
            height=720,
            duration_seconds=10.0,
            average_frame_rate=30.0,
            estimated_frame_count=300,
            has_audio=True,
            file_size_bytes=1,
        ),
        damage_map=MediaDamageMap(
            input_hash=source_hash,
            duration_seconds=10.0,
            intervals=(structural, dark),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(
            max_preview_ranges=1,
            max_preview_total_seconds=2.0,
        ),
        visual_assessment=visual,
    )

    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan,
        tmp_path / "source.mp4",
        tmp_path / "private review",
    )

    previewed = set(previews.previewed_action_ids)
    salvage = next(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.SALVAGE_SEGMENTS
    )
    assert salvage.id in previewed
    assert RescueActionKind.ADJUST_LUMA not in {action.kind for action in plan.actions}
    assert (
        "Automatic adjust_luma action needs review: preview_range_uncovered."
        in plan.assessment_warnings
    )


def test_preview_omits_improved_when_no_supported_improvement_exists(
    tmp_path: Path,
) -> None:
    """Catches cloning the faithful preview into a misleading improved variant."""
    previews = RescuePreviewBuilder(runner=FakeRunner()).build(
        plan=_plan(RescueStrategy.CONSERVATIVE),
        source=tmp_path / "private-customer-video.mp4",
        private_review_root=tmp_path / "private review",
    )

    assert previews.improved is None


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "reserved_name",
    ("source-00.mp4", "faithful-00.mp4", "improved-00.mp4"),
)
def test_preview_rejects_every_reserved_output_collision_before_running(
    tmp_path: Path,
    reserved_name: str,
) -> None:
    """Catches a private preview command overwriting its read-only source."""
    runner = FakeRunner()
    source = tmp_path / reserved_name
    source.write_bytes(b"original")

    with pytest.raises(RescueArtifactError):
        RescuePreviewBuilder(runner=runner).build(
            plan=_plan(RescueStrategy.BALANCED),
            source=source,
            private_review_root=tmp_path,
        )

    assert runner.commands == []
    assert source.read_bytes() == b"original"


def test_preview_rejects_hard_linked_reserved_output_before_running(
    tmp_path: Path,
) -> None:
    """Catches a distinct preview pathname referring to the read-only source."""
    runner = FakeRunner()
    source = tmp_path / "source.mp4"
    reserved_output = tmp_path / "source-00.mp4"
    source.write_bytes(b"original")
    try:
        reserved_output.hardlink_to(source)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable on this filesystem: {exc}")
    assert os.path.samefile(source, reserved_output)

    with pytest.raises(RescueArtifactError):
        RescuePreviewBuilder(runner=runner).build(
            plan=_plan(RescueStrategy.BALANCED),
            source=source,
            private_review_root=tmp_path,
        )

    assert runner.commands == []
    assert source.read_bytes() == b"original"
