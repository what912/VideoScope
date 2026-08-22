from __future__ import annotations

import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, ValidationError

import videoscope.rescue.stabilization as stabilization_module
from videoscope.domain import VideoMetadata
from videoscope.rescue.commands import build_preview_commands
from videoscope.rescue.errors import RescueArtifactError, RescueCancelledError
from videoscope.rescue.models import (
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
    RescueActionKind,
    RescueEffectiveConfig,
    RescuePlan,
    RescueStrategy,
    StabilizationQualificationProfile,
    canonical_video_encode_contract,
    make_damage_id,
    make_rescue_action_id,
    make_rescue_plan_digest,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.stabilization import (
    MotionTransform,
    StabilizationAssessment,
    StabilizationConfig,
)

_INPUT_SHA = "a" * 64
_TOPOLOGY_SHA = "d" * 64


def _profiles() -> tuple[StabilizationQualificationProfile, ...]:
    return (
        StabilizationQualificationProfile(profile_id="transition_anchor_v1"),
        StabilizationQualificationProfile(profile_id="transition_anchor_v1_dense"),
        StabilizationQualificationProfile(profile_id="transition_anchor_v1_lk"),
    )


def _actual_pts() -> tuple[float, ...]:
    return tuple(32.0 + index / 24.0 for index in range(96))


def _transforms(*, offset: float = 0.0) -> tuple[MotionTransform, ...]:
    return tuple(
        MotionTransform(
            timestamp_seconds=timestamp,
            translation_x=(0.25 if index % 2 else -0.25) + offset,
            translation_y=0.1,
            rotation_degrees=0.0,
            scale=1.0,
            inlier_ratio=0.95,
            residual_pixels=0.2,
            semantics="frame_correction",
        )
        for index, timestamp in enumerate(_actual_pts())
    )


def _action_parameters(
    config: RescueEffectiveConfig, *, offset: float = 0.0
) -> dict[str, JsonValue]:
    stabilization_config = StabilizationConfig(
        frame_width=1280,
        frame_height=720,
        accepted_ranges=((32.0, 36.0),),
    )
    transforms = _transforms(offset=offset)
    return {
        "method": "transition_anchor_v1",
        "algorithm_version": "1",
        "estimator_algorithm_version": "transition_anchor_v1",
        "transition_range": [32.0, 33.0],
        "following_anchor_range": [33.0, 36.0],
        "transition_correction_count": len(transforms),
        "motion_transforms": [item.model_dump(mode="json") for item in transforms],
        "config": stabilization_config.model_dump(mode="json"),
        "crop_ratio": 0.05,
        "affected_ranges": [[32.0, 36.0]],
        "video_encode_contract": canonical_video_encode_contract(config).model_dump(
            mode="json"
        ),
    }


def _draft_plan(
    profiles: tuple[StabilizationQualificationProfile, ...] | None = None,
    *,
    input_hash: str = _INPUT_SHA,
) -> RescuePlan:
    config = RescueEffectiveConfig(
        stabilization_qualification_profiles=profiles or _profiles()
    )
    parameters = _action_parameters(config)
    action = RescueAction(
        id=make_rescue_action_id(
            kind=RescueActionKind.STABILIZE,
            parameters=parameters,
            source_ranges=((32.0, 36.0),),
            strategy=RescueStrategy.BALANCED,
            version="1",
        ),
        version="1",
        kind=RescueActionKind.STABILIZE,
        description="Apply exact transition-anchor corrections.",
        source_ranges=((32.0, 36.0),),
        parameters=parameters,
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    draft = RescuePlan.model_construct(
        input_hash=input_hash,
        strategy=RescueStrategy.BALANCED,
        effective_config=config,
        actions=(action,),
        preview_ranges=((32.0, 36.0),),
        plan_digest="0" * 64,
    )
    raw = draft.model_dump(mode="python", exclude={"plan_digest"})
    return RescuePlan(**raw, plan_digest=make_rescue_plan_digest(raw))


def _parent_handle(draft: RescuePlan, path: Path) -> Any:
    handle_type = getattr(stabilization_module, "StabilizationImmediateParentHandle")
    return handle_type(
        root=path.parent,
        path=path,
        draft_plan_digest=draft.plan_digest,
        stabilization_action_id=draft.actions[0].id,
        preceding_action_ids=(),
        sha256=sha256(path.read_bytes()).hexdigest(),
        encode_contract=canonical_video_encode_contract(draft.effective_config),
        actual_pts=_actual_pts(),
        normalized_pts_digest=stabilization_module.stabilization_actual_pts_digest(
            _actual_pts()
        ),
        stream_topology_digest=_TOPOLOGY_SHA,
        frame_count=96,
        cleanup_paths=(path,),
    )


def _measurement(
    draft: RescuePlan,
    profile: StabilizationQualificationProfile,
    index: int,
    *,
    metric_updates: dict[str, float] | None = None,
    parameter_offset: float = 0.0,
) -> Any:
    measurement_type = getattr(
        stabilization_module, "StabilizationProfileMeasurementV1"
    )
    thresholds_for = getattr(
        stabilization_module, "stabilization_qualification_thresholds"
    )
    pts_digest = getattr(stabilization_module, "stabilization_actual_pts_digest")(
        _actual_pts()
    )
    config = StabilizationConfig.model_validate_json(
        json.dumps(_action_parameters(draft.effective_config)["config"])
    )
    metrics: dict[str, float] = {
        "range_coverage_ratio": 1.0,
        "expected_frames": 96.0,
        "reliable_transforms": 96.0,
        "residual_median_pixels": 0.1,
        "residual_p90_pixels": 0.2,
        "crop_ratio": 0.05,
        "transition_consensus_coverage_ratio": 1.0,
        "transition_consensus_p90_pixels": 0.1,
        "transition_seam_residual_pixels": 0.1,
        "transition_expected_frames": 24.0,
        "transition_reliable_frames": 24.0,
        "transition_boundary_path_residual_pixels": 0.1,
    }
    metrics.update(metric_updates or {})
    return measurement_type(
        profile=profile,
        parent_sha256=draft.input_hash,
        control_sha256=f"{index + 1:064x}",
        candidate_sha256=f"{index + 11:064x}",
        encode_contract=canonical_video_encode_contract(draft.effective_config),
        source_ranges=((32.0, 36.0),),
        actual_pts=_actual_pts(),
        parent_normalized_pts_digest=pts_digest,
        control_normalized_pts_digest=pts_digest,
        candidate_normalized_pts_digest=pts_digest,
        parent_stream_topology_digest=_TOPOLOGY_SHA,
        control_stream_topology_digest=_TOPOLOGY_SHA,
        candidate_stream_topology_digest=_TOPOLOGY_SHA,
        parent_frame_count=96,
        control_frame_count=96,
        candidate_frame_count=96,
        control_recipe="same_parent_identity_v1",
        action_parameters=_action_parameters(
            draft.effective_config, offset=parameter_offset
        ),
        metrics=metrics,
        thresholds=thresholds_for(config),
    )


def _evidence(
    draft: RescuePlan,
    measurements: tuple[Any, ...],
) -> Any:
    builder = getattr(
        stabilization_module, "build_stabilization_qualification_evidence"
    )
    return builder(draft, measurements)


def test_stabilization_qualification_selects_first_all_pass_profile() -> None:
    draft = _draft_plan()
    profiles = draft.effective_config.stabilization_qualification_profiles
    measurements = (
        _measurement(
            draft,
            profiles[0],
            0,
            metric_updates={"residual_p90_pixels": 1.01},
        ),
        _measurement(draft, profiles[1], 1, parameter_offset=0.01),
        _measurement(draft, profiles[2], 2, parameter_offset=0.02),
    )

    evidence = _evidence(draft, measurements)

    assert evidence.selected is measurements[1]
    assert evidence.selected_profile_id == profiles[1].profile_id
    assert evidence.actual_profile_order == tuple(
        profile.profile_id for profile in profiles
    )
    assert evidence.source_ranges == ((32.0, 36.0),)
    assert len(evidence.selected.actual_pts) == 96
    assert json.dumps(evidence.model_dump(mode="json"), allow_nan=False)


@pytest.mark.parametrize(
    ("field", "mutate", "message"),
    [
        ("actual_pts", lambda value: value[:-1], "PTS"),
        (
            "actual_pts",
            lambda value: (*value[:3], value[2], *value[4:]),
            "PTS",
        ),
        (
            "actual_pts",
            lambda value: (value[1], value[0], *value[2:]),
            "PTS",
        ),
        ("source_ranges", lambda _value: ((32.0, 35.5),), "PTS|range"),
        ("control_stream_topology_digest", lambda _value: "e" * 64, "topology"),
        ("candidate_sha256", lambda payload: payload["control_sha256"], "distinct"),
    ],
)
def test_stabilization_profile_rejects_structural_drift(
    field: str,
    mutate: Callable[[Any], Any],
    message: str,
) -> None:
    draft = _draft_plan()
    measurement = _measurement(
        draft, draft.effective_config.stabilization_qualification_profiles[0], 0
    )
    raw = measurement.model_dump(mode="python")
    raw[field] = mutate(raw if field == "candidate_sha256" else raw[field])

    with pytest.raises(ValidationError, match=message):
        type(measurement).model_validate(raw)


def test_stabilization_evidence_rejects_parent_generation_drift() -> None:
    profiles = (StabilizationQualificationProfile(profile_id="transition_anchor_v1"),)
    draft = _draft_plan(profiles)
    measurement = _measurement(draft, profiles[0], 0)

    with pytest.raises(ValidationError, match="parent"):
        stabilization_module.StabilizationQualificationEvidenceV1(
            input_hash=draft.input_hash,
            draft_plan_digest=draft.plan_digest,
            draft_action_id=draft.actions[0].id,
            draft_parameters=draft.actions[0].parameters,
            source_ranges=draft.actions[0].source_ranges,
            encode_contract=canonical_video_encode_contract(draft.effective_config),
            parent_sha256=draft.input_hash,
            parent_encode_contract=canonical_video_encode_contract(
                draft.effective_config
            ),
            parent_actual_pts=_actual_pts(),
            parent_normalized_pts_digest=(
                stabilization_module.stabilization_actual_pts_digest(_actual_pts())
            ),
            parent_frame_count=96,
            parent_stream_topology_digest=_TOPOLOGY_SHA,
            preceding_action_ids=(),
            authoritative_actual_pts=_actual_pts(),
            authoritative_actual_pts_digest=(
                stabilization_module.stabilization_actual_pts_digest(_actual_pts())
            ),
            authoritative_frame_count=96,
            authoritative_parent_stream_topology_digest=_TOPOLOGY_SHA,
            configured_profiles=profiles,
            actual_profile_order=(profiles[0].profile_id,),
            profile_measurements=(
                measurement.model_copy(update={"parent_sha256": "f" * 64}),
            ),
            selected_profile_id=profiles[0].profile_id,
        )


@pytest.mark.parametrize(
    "metric_updates",
    [
        {"range_coverage_ratio": 0.99},
        {"residual_median_pixels": 0.51},
        {"residual_p90_pixels": 1.01},
        {"crop_ratio": 0.121},
        {"transition_consensus_coverage_ratio": 0.99},
        {"transition_consensus_p90_pixels": 4.01},
        {"transition_seam_residual_pixels": 0.251},
        {"transition_boundary_path_residual_pixels": 0.251},
        {"transition_reliable_frames": 23.0},
    ],
)
def test_stabilization_profile_gate_failure_rejects_only_that_profile(
    metric_updates: dict[str, float],
) -> None:
    draft = _draft_plan()
    profile = draft.effective_config.stabilization_qualification_profiles[0]

    assert (
        _measurement(draft, profile, 0, metric_updates=metric_updates).passed is False
    )


def test_stabilization_no_pass_preserves_existing_action_and_plan_identity() -> None:
    profiles = _profiles()
    draft = _planned_transition(profiles)
    measurements = tuple(
        _measurement(
            draft,
            profile,
            index,
            metric_updates={"range_coverage_ratio": 0.5},
        )
        for index, profile in enumerate(profiles)
    )
    evidence = _evidence(draft, measurements)

    final = _planned_transition(profiles, qualification=evidence)

    assert evidence.selected is None
    assert final == draft
    assert final.actions == draft.actions
    assert final.plan_digest == draft.plan_digest


def test_stale_stabilization_evidence_is_rejected_after_identity_recompute() -> None:
    draft = _draft_plan(
        (StabilizationQualificationProfile(profile_id="transition_anchor_v1"),)
    )
    profile = draft.effective_config.stabilization_qualification_profiles[0]
    evidence = _evidence(draft, (_measurement(draft, profile, 0),))
    final = _qualified_plan(draft, evidence)
    raw = final.model_dump(mode="python")
    action = raw["actions"][0]
    action["parameters"]["stabilization_qualification"]["draft_action_id"] = (
        "rescue_action_stale"
    )
    action["id"] = make_rescue_action_id(
        kind=RescueActionKind.STABILIZE,
        parameters=action["parameters"],
        source_ranges=tuple(action["source_ranges"]),
        strategy=RescueStrategy.BALANCED,
        version="1",
    )
    raw["plan_digest"] = make_rescue_plan_digest(
        {key: value for key, value in raw.items() if key != "plan_digest"}
    )

    with pytest.raises(ValidationError, match="qualification"):
        RescuePlan.model_validate(raw)


def test_preview_boundary_rejects_profile_order_tamper_before_command_output() -> None:
    profiles = (
        StabilizationQualificationProfile(profile_id="transition_anchor_v1"),
        StabilizationQualificationProfile(profile_id="transition_anchor_v1_dense"),
    )
    draft = _draft_plan(profiles)
    evidence = _evidence(
        draft,
        tuple(
            _measurement(draft, profile, index)
            for index, profile in enumerate(profiles)
        ),
    )
    final = _qualified_plan(draft, evidence)
    raw = final.model_dump(mode="python")
    action = raw["actions"][0]
    qualification = action["parameters"]["stabilization_qualification"]
    qualification["actual_profile_order"] = list(
        reversed(qualification["actual_profile_order"])
    )
    action["id"] = make_rescue_action_id(
        kind=RescueActionKind.STABILIZE,
        parameters=action["parameters"],
        source_ranges=tuple(action["source_ranges"]),
        strategy=RescueStrategy.BALANCED,
        version="1",
    )
    raw["plan_digest"] = make_rescue_plan_digest(
        {key: value for key, value in raw.items() if key != "plan_digest"}
    )
    tampered = RescuePlan.model_construct(
        **{
            **final.__dict__,
            "actions": (RescueAction.model_validate(action),),
            "plan_digest": raw["plan_digest"],
        }
    )

    with pytest.raises(ValueError, match="qualification"):
        build_preview_commands(tampered, Path("source.mp4"), Path("private"))


def test_qualified_stabilization_method_tamper_cannot_hide_evidence() -> None:
    profiles = (StabilizationQualificationProfile(profile_id="transition_anchor_v1"),)
    draft = _draft_plan(profiles)
    evidence = _evidence(draft, (_measurement(draft, profiles[0], 0),))
    final = _qualified_plan(draft, evidence)
    raw = final.model_dump(mode="python")
    action = raw["actions"][0]
    action["parameters"]["method"] = "anchor_v1"
    action["id"] = make_rescue_action_id(
        kind=RescueActionKind.STABILIZE,
        parameters=action["parameters"],
        source_ranges=tuple(action["source_ranges"]),
        strategy=RescueStrategy.BALANCED,
        version="1",
    )
    raw["plan_digest"] = make_rescue_plan_digest(
        {key: value for key, value in raw.items() if key != "plan_digest"}
    )

    with pytest.raises(ValidationError, match="qualification"):
        RescuePlan.model_validate(raw)


def test_recomputed_95_pts_profile_cannot_replace_authoritative_96_pts() -> None:
    profiles = (StabilizationQualificationProfile(profile_id="transition_anchor_v1"),)
    draft = _draft_plan(profiles)
    evidence = _evidence(draft, (_measurement(draft, profiles[0], 0),))
    final = _qualified_plan(draft, evidence)
    raw = final.model_dump(mode="python")
    action = raw["actions"][0]
    qualification = action["parameters"]["stabilization_qualification"]
    measurement = qualification["profile_measurements"][0]
    shortened_pts = tuple(measurement["actual_pts"][:-1])
    shortened_digest = stabilization_module.stabilization_actual_pts_digest(
        shortened_pts
    )
    measurement["actual_pts"] = list(shortened_pts)
    for key in (
        "parent_normalized_pts_digest",
        "control_normalized_pts_digest",
        "candidate_normalized_pts_digest",
    ):
        measurement[key] = shortened_digest
    for key in ("parent_frame_count", "control_frame_count", "candidate_frame_count"):
        measurement[key] = 95
    measurement["action_parameters"]["motion_transforms"] = measurement[
        "action_parameters"
    ]["motion_transforms"][:-1]
    measurement["action_parameters"]["transition_correction_count"] = 95
    measurement["metrics"]["expected_frames"] = 95.0
    measurement["metrics"]["reliable_transforms"] = 95.0
    action["parameters"]["motion_transforms"] = action["parameters"][
        "motion_transforms"
    ][:-1]
    action["parameters"]["transition_correction_count"] = 95
    action["id"] = make_rescue_action_id(
        kind=RescueActionKind.STABILIZE,
        parameters=action["parameters"],
        source_ranges=tuple(action["source_ranges"]),
        strategy=RescueStrategy.BALANCED,
        version="1",
    )
    raw["plan_digest"] = make_rescue_plan_digest(
        {key: value for key, value in raw.items() if key != "plan_digest"}
    )

    with pytest.raises(ValidationError, match="authoritative|qualification"):
        RescuePlan.model_validate(raw)


def test_callback_stabilization_qualifier_cleans_private_generations(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source-generation")
    profiles = (
        StabilizationQualificationProfile(profile_id="transition_anchor_v1"),
        StabilizationQualificationProfile(profile_id="transition_anchor_v1_dense"),
    )
    draft = _draft_plan(
        profiles,
        input_hash=sha256(source.read_bytes()).hexdigest(),
    )
    rendered: list[Path] = []

    def render(
        _source: Path,
        output: Path,
        _action: RescueAction,
        profile: StabilizationQualificationProfile,
        identity_control: bool,
    ) -> Path:
        output.write_bytes(
            ("control" if identity_control else profile.profile_id).encode("ascii")
        )
        rendered.append(output)
        return output

    def measure(
        _source: Path,
        control: Path,
        candidate: Path,
        plan: RescuePlan,
        _action: RescueAction,
        profile: StabilizationQualificationProfile,
        index: int,
    ) -> Any:
        return _measurement(plan, profile, index).model_copy(
            update={
                "control_sha256": sha256(control.read_bytes()).hexdigest(),
                "candidate_sha256": sha256(candidate.read_bytes()).hexdigest(),
            }
        )

    qualifier_type = getattr(
        stabilization_module, "CallbackStabilizationCandidateQualifier"
    )
    evidence = qualifier_type(renderer=render, measurement_provider=measure).qualify(
        draft,
        _parent_handle(draft, source),
        tmp_path / "private" / "stabilization-qualification",
        lambda: False,
    )

    assert evidence.selected_profile_id == profiles[0].profile_id
    assert rendered
    assert all(not path.exists() for path in rendered)
    assert not (tmp_path / "private" / "stabilization-qualification").exists()


def test_callback_qualifier_renders_from_bound_immediate_parent(
    tmp_path: Path,
) -> None:
    raw_source = tmp_path / "raw-source.bin"
    raw_source.write_bytes(b"raw-source")
    immediate_parent = tmp_path / "immediate-parent.bin"
    immediate_parent.write_bytes(b"preceding-action-generation")
    profiles = (StabilizationQualificationProfile(profile_id="transition_anchor_v1"),)
    draft = _draft_plan(
        profiles,
        input_hash=sha256(raw_source.read_bytes()).hexdigest(),
    )
    pts_digest = stabilization_module.stabilization_actual_pts_digest(_actual_pts())
    handle_type = getattr(stabilization_module, "StabilizationImmediateParentHandle")
    parent = handle_type(
        root=immediate_parent.parent,
        path=immediate_parent,
        draft_plan_digest=draft.plan_digest,
        stabilization_action_id=draft.actions[0].id,
        preceding_action_ids=(),
        sha256=sha256(immediate_parent.read_bytes()).hexdigest(),
        encode_contract=canonical_video_encode_contract(draft.effective_config),
        actual_pts=_actual_pts(),
        normalized_pts_digest=pts_digest,
        stream_topology_digest=_TOPOLOGY_SHA,
        frame_count=96,
        cleanup_paths=(immediate_parent,),
    )
    seen_parents: list[Path] = []

    def render(
        source: Path,
        output: Path,
        _action: RescueAction,
        profile: StabilizationQualificationProfile,
        identity_control: bool,
    ) -> Path:
        seen_parents.append(source)
        output.write_bytes(
            ("control" if identity_control else profile.profile_id).encode("ascii")
        )
        return output

    def measure(
        _source: Path,
        control: Path,
        candidate: Path,
        plan: RescuePlan,
        _action: RescueAction,
        profile: StabilizationQualificationProfile,
        index: int,
    ) -> Any:
        return _measurement(plan, profile, index).model_copy(
            update={
                "parent_sha256": parent.sha256,
                "control_sha256": sha256(control.read_bytes()).hexdigest(),
                "candidate_sha256": sha256(candidate.read_bytes()).hexdigest(),
            }
        )

    qualifier_type = getattr(
        stabilization_module, "CallbackStabilizationCandidateQualifier"
    )
    evidence = qualifier_type(renderer=render, measurement_provider=measure).qualify(
        draft,
        parent,
        tmp_path / "private-parent-qualification",
        lambda: False,
    )

    assert seen_parents == [immediate_parent, immediate_parent]
    assert evidence.parent_sha256 == parent.sha256
    assert evidence.preceding_action_ids == ()


def test_immediate_parent_handle_rejects_external_and_unowned_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "stabilization-parent"
    root.mkdir()
    inside = root / "parent.private"
    inside.write_bytes(b"owned parent")
    outside = tmp_path / "raw-source.bin"
    outside.write_bytes(b"raw source")
    draft = _draft_plan(input_hash=sha256(outside.read_bytes()).hexdigest())
    handle_type = getattr(stabilization_module, "StabilizationImmediateParentHandle")
    common = {
        "root": root,
        "draft_plan_digest": draft.plan_digest,
        "stabilization_action_id": draft.actions[0].id,
        "preceding_action_ids": (),
        "encode_contract": canonical_video_encode_contract(draft.effective_config),
        "actual_pts": _actual_pts(),
        "normalized_pts_digest": (
            stabilization_module.stabilization_actual_pts_digest(_actual_pts())
        ),
        "stream_topology_digest": _TOPOLOGY_SHA,
        "frame_count": 96,
    }

    with pytest.raises(ValueError, match="private root|owned"):
        handle_type(
            **common,
            path=outside,
            sha256=sha256(outside.read_bytes()).hexdigest(),
            cleanup_paths=(outside,),
        )
    with pytest.raises(ValueError, match="cleanup|owned"):
        handle_type(
            **common,
            path=inside,
            sha256=sha256(inside.read_bytes()).hexdigest(),
            cleanup_paths=(),
        )


def test_immediate_parent_handle_rejects_symlink_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "stabilization-parent"
    root.mkdir()
    target = root / "target.private"
    target.write_bytes(b"owned parent")
    linked = root / "linked.private"
    try:
        linked.symlink_to(target)
    except OSError:
        linked.write_bytes(target.read_bytes())
        original_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path,
            "is_symlink",
            lambda value: value == linked or original_is_symlink(value),
        )
    draft = _draft_plan(input_hash=sha256(target.read_bytes()).hexdigest())
    handle_type = getattr(stabilization_module, "StabilizationImmediateParentHandle")

    with pytest.raises(ValueError, match="symlink|regular"):
        handle_type(
            root=root,
            path=linked,
            draft_plan_digest=draft.plan_digest,
            stabilization_action_id=draft.actions[0].id,
            preceding_action_ids=(),
            sha256=sha256(target.read_bytes()).hexdigest(),
            encode_contract=canonical_video_encode_contract(draft.effective_config),
            actual_pts=_actual_pts(),
            normalized_pts_digest=(
                stabilization_module.stabilization_actual_pts_digest(_actual_pts())
            ),
            stream_topology_digest=_TOPOLOGY_SHA,
            frame_count=96,
            cleanup_paths=(linked,),
        )


def test_callback_qualifier_revalidates_parent_before_renderer(tmp_path: Path) -> None:
    root = tmp_path / "stabilization-parent"
    root.mkdir()
    parent_path = root / "parent.private"
    parent_path.write_bytes(b"owned parent")
    outside = tmp_path / "raw-source.bin"
    outside.write_bytes(b"raw source")
    draft = _draft_plan(input_hash=sha256(outside.read_bytes()).hexdigest())
    parent = _parent_handle(draft, parent_path)
    object.__setattr__(parent, "path", outside)
    object.__setattr__(parent, "cleanup_paths", (outside,))
    render_calls = 0

    def render(*_args: object) -> Path:
        nonlocal render_calls
        render_calls += 1
        raise AssertionError("unowned parent reached renderer")

    qualifier_type = getattr(
        stabilization_module, "CallbackStabilizationCandidateQualifier"
    )
    with pytest.raises(RescueArtifactError):
        qualifier_type(
            renderer=render, measurement_provider=lambda *_args: None
        ).qualify(
            draft,
            parent,
            tmp_path / "stabilization-qualification",
            lambda: False,
        )
    assert render_calls == 0


def test_callback_stabilization_qualifier_path_escape_fails_closed(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source-generation")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must remain")
    profiles = (StabilizationQualificationProfile(profile_id="transition_anchor_v1"),)
    draft = _draft_plan(
        profiles,
        input_hash=sha256(source.read_bytes()).hexdigest(),
    )

    def escape(
        _source: Path,
        output: Path,
        _action: RescueAction,
        _profile: StabilizationQualificationProfile,
        _identity_control: bool,
    ) -> Path:
        output.write_bytes(b"private")
        return outside

    qualifier_type = getattr(
        stabilization_module, "CallbackStabilizationCandidateQualifier"
    )
    with pytest.raises(RescueArtifactError):
        qualifier_type(
            renderer=escape, measurement_provider=lambda *_args: None
        ).qualify(
            draft,
            _parent_handle(draft, source),
            tmp_path / "private" / "stabilization-qualification",
            lambda: False,
        )

    assert outside.read_bytes() == b"must remain"
    assert not (tmp_path / "private" / "stabilization-qualification").exists()


def test_callback_stabilization_qualifier_error_and_cancellation_clean_up(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"source-generation")
    profiles = (StabilizationQualificationProfile(profile_id="transition_anchor_v1"),)
    draft = _draft_plan(
        profiles,
        input_hash=sha256(source.read_bytes()).hexdigest(),
    )
    qualifier_type = getattr(
        stabilization_module, "CallbackStabilizationCandidateQualifier"
    )

    def render(
        _source: Path,
        output: Path,
        _action: RescueAction,
        _profile: StabilizationQualificationProfile,
        _identity_control: bool,
    ) -> Path:
        output.write_bytes(b"private")
        return output

    root = tmp_path / "error-root"
    with pytest.raises(RuntimeError, match="measurement failed"):
        qualifier_type(
            renderer=render,
            measurement_provider=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("measurement failed")
            ),
        ).qualify(draft, _parent_handle(draft, source), root, lambda: False)
    assert not root.exists()

    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    root = tmp_path / "cancel-root"
    with pytest.raises(RescueCancelledError):
        qualifier_type(
            renderer=render, measurement_provider=lambda *_args: None
        ).qualify(draft, _parent_handle(draft, source), root, cancelled)
    assert not root.exists()


def _qualified_plan(draft: RescuePlan, evidence: Any) -> RescuePlan:
    parameter_builder = getattr(
        stabilization_module, "stabilization_qualification_action_parameters"
    )
    selected = evidence.selected
    assert selected is not None
    parameters = dict(selected.action_parameters)
    parameters.update(parameter_builder(evidence))
    old = draft.actions[0]
    action = old.model_copy(
        update={
            "id": make_rescue_action_id(
                kind=old.kind,
                parameters=parameters,
                source_ranges=old.source_ranges,
                strategy=old.strategy,
                version=old.version,
            ),
            "parameters": parameters,
        }
    )
    raw = draft.model_dump(mode="python", exclude={"plan_digest"})
    raw["actions"] = [action.model_dump(mode="python")]
    return RescuePlan(**raw, plan_digest=make_rescue_plan_digest(raw))


def _planned_transition(
    profiles: tuple[StabilizationQualificationProfile, ...],
    *,
    qualification: Any | None = None,
) -> RescuePlan:
    metadata = VideoMetadata(
        filename="source.mp4",
        container_format="mp4",
        codec="h264",
        width=1280,
        height=720,
        duration_seconds=40.0,
        average_frame_rate=24.0,
        estimated_frame_count=960,
        has_audio=True,
        file_size_bytes=1,
    )
    interval = DamageInterval(
        id=make_damage_id(_INPUT_SHA, "video:0", DamageKind.SHAKE, 32.0, 36.0),
        stream_id="video:0",
        kind=DamageKind.SHAKE,
        start_seconds=32.0,
        end_seconds=36.0,
    )
    damage_map = MediaDamageMap(
        input_hash=_INPUT_SHA,
        duration_seconds=40.0,
        scan_coverage=((0.0, 40.0),),
        intervals=(interval,),
    )
    config = RescueEffectiveConfig(stabilization_qualification_profiles=profiles)
    stabilization_config = StabilizationConfig(
        frame_width=1280,
        frame_height=720,
        accepted_ranges=((32.0, 36.0),),
    )
    return build_rescue_plan(
        metadata=metadata,
        damage_map=damage_map,
        strategy=RescueStrategy.BALANCED,
        config=config,
        stabilization_assessment=StabilizationAssessment(
            recommended=True,
            reason="measured_transition_anchor_motion",
            crop_ratio=0.05,
            transforms=_transforms(),
            parameters={
                "affected_ranges": [[32.0, 36.0]],
                "method": "transition_anchor_v1",
                "algorithm_version": "1",
                "estimator_algorithm_version": "transition_anchor_v1",
                "transition_range": [32.0, 33.0],
                "following_anchor_range": [33.0, 36.0],
                "transition_correction_count": 96,
                "config": stabilization_config.model_dump(mode="json"),
                "crop_ratio": 0.05,
            },
        ),
        stabilization_qualification=qualification,
    )
