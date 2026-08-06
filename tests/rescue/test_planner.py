"""Tests for deterministic Conservative and Balanced Rescue planning."""

from __future__ import annotations

import warnings

import pytest

import videoscope.rescue.capabilities as rescue_capabilities
from videoscope.domain import VideoMetadata
from videoscope.rescue.audio import FixedOffsetAssessment
from videoscope.rescue.capabilities import (
    ActionCapabilityReason,
    evaluate_action_capabilities,
)
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueEffectiveConfig,
    RescueStrategy,
    RescueSymptom,
    make_damage_id,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.stabilization import StabilizationAssessment
from videoscope.rescue.visual import (
    FlickerCorrectionPlan,
    VisualAssessment,
    VisualMetrics,
)


def video_metadata() -> VideoMetadata:
    return VideoMetadata(
        filename="private source.mp4",
        container_format="mp4",
        codec="h264",
        width=1280,
        height=720,
        duration_seconds=20.0,
        average_frame_rate=30.0,
        estimated_frame_count=600,
        has_audio=True,
        file_size_bytes=1024,
    )


def interval(
    kind: DamageKind, start_seconds: float, end_seconds: float
) -> DamageInterval:
    return DamageInterval(
        id=make_damage_id("a" * 64, "video:0", kind, start_seconds, end_seconds),
        stream_id="video:0",
        kind=kind,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )


def damage_map(*intervals: DamageInterval) -> MediaDamageMap:
    return MediaDamageMap(
        input_hash="a" * 64,
        duration_seconds=20.0,
        scan_coverage=((0.0, 20.0),),
        intervals=intervals,
    )


def measured_dark_assessment() -> VisualAssessment:
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
        preview_required=True,
        public_explanation="Measured dark samples support a preview.",
    )


def test_conservative_plan_never_contains_subjective_enhancement() -> None:
    """Catches an enhancement action accidentally leaking into Conservative mode."""
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.DARK, 2.0, 5.0),
            interval(DamageKind.VIDEO_NOISE, 6.0, 8.0),
        ),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(),
    )

    assert {action.kind for action in plan.actions}.isdisjoint(
        {
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
            RescueActionKind.DEFLICKER,
            RescueActionKind.STABILIZE,
            RescueActionKind.NORMALIZE_AUDIO,
            RescueActionKind.DENOISE_AUDIO,
        }
    )


def test_locked_damaged_edge_is_not_trimmed_and_content_changes_need_confirmation() -> (
    None
):
    """Catches any destructive removal being planned through a locked range."""
    locked = interval(DamageKind.UNDECODABLE, 0.0, 2.0)
    removable = interval(DamageKind.UNDECODABLE, 10.0, 12.0)
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(locked, removable),
        strategy=RescueStrategy.BALANCED,
        locked_ranges=((0.0, 2.0),),
        config=RescueEffectiveConfig(locked_ranges=((0.0, 2.0),)),
    )

    assert RescueActionKind.TRIM_DAMAGED_EDGES not in {
        action.kind for action in plan.actions
    }
    salvage = next(
        action
        for action in plan.actions
        if action.kind is RescueActionKind.SALVAGE_SEGMENTS
    )
    assert salvage.parameters["damage_ids"] == [removable.id]
    assert locked.id not in salvage.parameters["damage_ids"]
    assert all(
        action.requires_confirmation
        for action in plan.actions
        if action.changes_content
    )


def test_balanced_actions_exclude_locked_spans_and_bind_strength_limit() -> None:
    """Catches global enhancement filters and ignored strength configuration."""
    config = RescueEffectiveConfig(
        balanced_strength_limit=0.5,
        locked_ranges=((3.0, 4.0),),
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.DARK, 2.0, 5.0)),
        strategy=RescueStrategy.BALANCED,
        locked_ranges=config.locked_ranges,
        config=config,
        visual_assessment=measured_dark_assessment(),
    )

    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )
    assert action.source_ranges == ((2.0, 3.0), (4.0, 5.0))
    assert action.parameters["strength_limit"] == 0.5
    assert action.parameters["brightness"] == pytest.approx(0.02)
    assert action.parameters["contrast"] == pytest.approx(1.01)


def test_preview_ranges_cover_actions_and_ignore_sub_microsecond_noise() -> None:
    """A rounded-zero scan interval cannot displace the action being reviewed."""
    tiny = interval(DamageKind.TIMESTAMP_DISCONTINUITY, 4.2 - 1e-15, 4.2)
    dark = interval(DamageKind.DARK, 7.0, 8.0)
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(tiny, dark),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(max_preview_ranges=1),
        visual_assessment=measured_dark_assessment(),
    )
    action = next(
        item for item in plan.actions if item.kind is RescueActionKind.ADJUST_LUMA
    )

    assert all(end - start >= 1e-6 for start, end in plan.preview_ranges)
    assert any(
        action_start < preview_end and preview_start < action_end
        for action_start, action_end in action.source_ranges
        for preview_start, preview_end in plan.preview_ranges
    )


def test_stabilization_without_a_real_preview_is_review_gated() -> None:
    """Catches issuing stabilization when the preview renderer cannot show it."""
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(interval(DamageKind.SHAKE, 2.0, 4.0)),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="Measured motion supports bounded stabilization review.",
            crop_ratio=0.05,
            parameters={"crop_ratio": 0.05, "max_crop_ratio": 0.12},
        ),
    )

    assert all(action.kind is not RescueActionKind.STABILIZE for action in plan.actions)
    assert "preview_renderer_unavailable" in " ".join(plan.assessment_warnings)


def test_preview_cap_review_gates_every_uncovered_action() -> None:
    """Catches confirming a fourth disjoint action beyond the preview-window cap."""
    visual = VisualAssessment(
        metrics=measured_dark_assessment().metrics,
        recommended_actions=(
            RescueActionKind.ADJUST_LUMA,
            RescueActionKind.DENOISE_VIDEO,
            RescueActionKind.SHARPEN,
        ),
        preview_required=True,
        public_explanation="Measured samples support bounded previews.",
    )
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.DARK, 1.0, 2.0),
            interval(DamageKind.VIDEO_NOISE, 5.0, 6.0),
            interval(DamageKind.SOFT_DETAIL, 9.0, 10.0),
            interval(DamageKind.FLICKER, 13.0, 14.0),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(max_preview_ranges=3),
        visual_assessment=visual,
        flicker_correction=FlickerCorrectionPlan(
            intervals=((13.0, 14.0),),
            gains=((13.0, 1.0), (14.0, 1.01)),
        ),
    )

    confirmable = {action.id for action in plan.actions if action.requires_confirmation}
    assert len(confirmable) == 3
    assert all(
        any(
            start < preview_end and preview_start < end
            for start, end in action.source_ranges
            for preview_start, preview_end in plan.preview_ranges
        )
        for action in plan.actions
        if action.id in confirmable
    )
    assert "preview_range_uncovered" in " ".join(plan.assessment_warnings)


def test_global_actions_conflicting_with_locks_are_review_gated() -> None:
    """Catches whole-stream rotation or offset changes crossing a user lock."""
    metadata = video_metadata().model_copy(update={"raw_probe": {"rotation": 90.0}})
    plan = build_rescue_plan(
        metadata=metadata,
        damage_map=damage_map(),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(locked_ranges=((1.0, 2.0),)),
        locked_ranges=((1.0, 2.0),),
        fixed_offset_assessment=FixedOffsetAssessment(
            offset_seconds=0.1,
            shift_seconds=-0.1,
            correlation=0.95,
            matched_event_count=3,
            agreement_seconds=0.01,
        ),
    )

    assert {action.kind for action in plan.actions}.isdisjoint(
        {
            RescueActionKind.NORMALIZE_ROTATION,
            RescueActionKind.CORRECT_FIXED_AV_OFFSET,
        }
    )
    warnings_text = " ".join(plan.assessment_warnings)
    assert warnings_text.count("locked_range_conflict") == 2


def test_capability_policy_rejects_a_local_range_that_still_overlaps_a_lock() -> None:
    """Catches bypassing upstream lock subtraction before plan issuance."""
    action = RescueAction(
        id="rescue_action_local_lock_regression",
        version="1",
        kind=RescueActionKind.ADJUST_LUMA,
        description="Adjust one measured local range.",
        source_ranges=((1.0, 3.0),),
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )

    decision = evaluate_action_capabilities(
        (action,),
        ((1.0, 3.0),),
        duration_seconds=4.0,
        locked_ranges=((2.0, 2.5),),
    )[0]

    assert decision.range_exact is False
    assert decision.automatic is False
    assert decision.reason is ActionCapabilityReason.LOCKED_RANGE_CONFLICT


def test_action_capability_profiles_cover_every_action_kind() -> None:
    """Catches adding an action kind without an explicit capability decision."""
    profiles = getattr(rescue_capabilities, "_ACTION_CAPABILITY_PROFILES", {})

    assert set(profiles) == set(RescueActionKind)


def test_missing_action_capability_profile_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an unprofiled content action defaulting to automatic eligibility."""
    action = RescueAction(
        id="rescue_action_missing_profile_regression",
        version="1",
        kind=RescueActionKind.ADJUST_LUMA,
        description="Adjust one measured local range.",
        source_ranges=((1.0, 2.0),),
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    monkeypatch.setattr(
        rescue_capabilities,
        "_ACTION_CAPABILITY_PROFILES",
        {},
        raising=False,
    )

    decision = evaluate_action_capabilities(
        (action,),
        ((1.0, 2.0),),
        duration_seconds=4.0,
        locked_ranges=(),
    )[0]

    assert decision.preview_supported is False
    assert decision.verification_mode == "needs_review"
    assert decision.automatic is False
    assert decision.reason is ActionCapabilityReason.PREVIEW_RENDERER_UNAVAILABLE


def test_plan_has_stable_action_order_digest_and_bounded_non_overlapping_previews() -> (
    None
):
    """Catches nondeterministic planning or preview ranges exceeding their cap."""
    map_ = damage_map(
        interval(DamageKind.VIDEO_NOISE, 5.0, 14.0),
        interval(DamageKind.UNDECODABLE, 0.0, 5.0),
        interval(DamageKind.FLICKER, 14.0, 18.0),
    )
    config = RescueEffectiveConfig(max_preview_total_seconds=10.0)

    first = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
    )
    second = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=config,
    )

    assert [action.kind for action in first.actions] == sorted(
        (action.kind for action in first.actions),
        key=lambda kind: list(RescueActionKind).index(kind),
    )
    assert first.plan_digest == second.plan_digest
    assert first.preview_ranges == second.preview_ranges
    assert sum(end - start for start, end in first.preview_ranges) <= (
        config.max_preview_total_seconds
    )
    assert all(
        previous_end <= following_start
        for (_previous_start, previous_end), (following_start, _following_end) in zip(
            first.preview_ranges, first.preview_ranges[1:], strict=False
        )
    )
    salvage = next(
        action
        for action in first.actions
        if action.kind is RescueActionKind.SALVAGE_SEGMENTS
    )
    assert any(
        action_start < preview_end and preview_start < action_end
        for action_start, action_end in salvage.source_ranges
        for preview_start, preview_end in first.preview_ranges
    )
    assert sum(end - start for start, end in first.preview_ranges) <= 10.0
    assert all(
        previous[1] <= current[0]
        for previous, current in zip(first.preview_ranges, first.preview_ranges[1:])
    )


def test_planner_declares_the_complete_faithful_public_bundle() -> None:
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(),
        strategy=RescueStrategy.CONSERVATIVE,
        config=RescueEffectiveConfig(),
    )
    assert plan.public_artifacts == (
        "rescue-plan.json",
        "damaged-segments.json",
        "changes.json",
        "verification-report.json",
        "technical-report.json",
        "report.html",
        "faithful-rescue.mp4",
    )


def test_list_parameters_are_immutable_and_serialize_without_warnings() -> None:
    """Catches frozen JSON lists becoming serializer-invalid tuples in a Rescue plan."""
    map_ = damage_map(interval(DamageKind.DARK, 2.0, 5.0))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        first = build_rescue_plan(
            metadata=video_metadata(),
            damage_map=map_,
            strategy=RescueStrategy.BALANCED,
            config=RescueEffectiveConfig(),
            visual_assessment=measured_dark_assessment(),
        )
        serialized = first.model_dump(mode="json")
    second = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=map_,
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        visual_assessment=measured_dark_assessment(),
    )
    damage_ids = next(
        action.parameters["damage_ids"]
        for action in first.actions
        if action.kind is RescueActionKind.ADJUST_LUMA
    )

    assert serialized["actions"]
    assert damage_ids == [map_.intervals[0].id]
    assert first.plan_digest == second.plan_digest
    with pytest.raises(TypeError):
        damage_ids.append("not-allowed")


def test_planner_omits_unmeasured_flicker_and_stabilization_actions() -> None:
    """Catches generic filters standing in for a reviewed correction curve."""
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(
            interval(DamageKind.FLICKER, 2.0, 4.0),
            interval(DamageKind.SHAKE, 5.0, 7.0),
        ),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
    )

    assert {action.kind for action in plan.actions}.isdisjoint(
        {RescueActionKind.DEFLICKER, RescueActionKind.STABILIZE}
    )
    assert "improved-viewing.mp4" not in plan.public_artifacts


def test_symptom_hint_is_digest_bound_but_cannot_invent_an_action() -> None:
    plan = build_rescue_plan(
        metadata=video_metadata(),
        damage_map=damage_map(),
        strategy=RescueStrategy.BALANCED,
        config=RescueEffectiveConfig(),
        requested_symptoms=(RescueSymptom.DARK,),
    )

    assert plan.requested_symptoms == (RescueSymptom.DARK,)
    assert RescueActionKind.ADJUST_LUMA not in {action.kind for action in plan.actions}
