from __future__ import annotations

import itertools
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import JsonValue, ValidationError

from videoscope.rescue.errors import RescueCancelledError, RescueMediaError
from videoscope.rescue.executor import SourceMapping
from videoscope.rescue.models import (
    CanonicalVideoEncodeContract,
    RescueAction,
    RescueActionKind,
    RescueEffectiveConfig,
    RescueStrategy,
    SharpenQualificationProfile,
    make_rescue_action_id,
)
from videoscope.rescue.planner import _apply_sharpen_qualification
from videoscope.rescue.qualification import (
    SHARPEN_QUALIFICATION_LIMITATION,
    NativeRescueCandidateQualifier,
    SharpenProfileMeasurementV1,
    SharpenQualificationMetricsV1,
    SharpenQualificationThresholdsV1,
    VerificationControlRecipeV1,
    _map_exact_qualification_ranges,
    _profile_measurement_from_raw,
    apply_qualified_sharpen_profile,
    build_sharpen_qualification_evidence,
    qualification_action_parameters,
    validate_plan_sharpen_qualification_contracts,
)

_A = "a" * 64
_B = "b" * 64
_C = "c" * 64
_D = "d" * 64
_E = "e" * 64
_F = "f" * 64


def _thresholds() -> SharpenQualificationThresholdsV1:
    return SharpenQualificationThresholdsV1(
        minimum_aggregate_gain_ratio=0.08,
        minimum_recovered_baseline_ratio=0.8,
        minimum_improved_frame_fraction=0.8,
        maximum_noise_increase=0.04,
        maximum_edge_overshoot_ratio=0.05,
        maximum_edge_overshoot_amplitude=0.05,
        maximum_ringing_ratio=0.08,
    )


def _measurement(
    profile: SharpenQualificationProfile,
    *,
    recovered: float = 0.9,
    coverage: float = 1.0,
    sha: str = _C,
    range_count: int = 1,
) -> SharpenProfileMeasurementV1:
    return SharpenProfileMeasurementV1(
        profile=profile,
        baseline_sha256=_A,
        visibility_control_sha256=_B,
        candidate_sha256=sha,
        normalized_pts_digest=_D,
        stream_topology_digest=_E,
        decoded_width=1280,
        decoded_height=720,
        generation_count=1,
        inventory_frame_count=132,
        metrics=SharpenQualificationMetricsV1(
            range_coverage_ratio=coverage,
            expected_frames=132,
            compared_frames=132,
            range_count=range_count,
            passing_range_count=range_count,
            minimum_aggregate_gain_ratio=0.1,
            minimum_recovered_baseline_ratio=recovered,
            minimum_improved_frame_fraction=0.9,
            maximum_noise_increase=0.01,
            maximum_edge_overshoot_ratio=0.02,
            maximum_edge_overshoot_amplitude=0.03,
            maximum_ringing_ratio=0.04,
        ),
        thresholds=_thresholds(),
    )


def test_verification_control_recipe_is_strict_and_path_free() -> None:
    payload = {
        "plan_digest": _A,
        "action_id": "action_stabilize",
        "parent_sha256": _B,
        "control_sha256": _C,
        "candidate_sha256": _D,
        "encode_contract": CanonicalVideoEncodeContract().model_dump(mode="json"),
        "normalized_pts_digest": _D,
        "stream_topology_digest": _E,
        "parent_normalized_pts_digest": _D,
        "parent_stream_topology_digest": _E,
        "candidate_normalized_pts_digest": _D,
        "candidate_stream_topology_digest": _E,
        "source_ranges": [[3.0, 4.0]],
        "frame_count": 24,
        "parent_frame_count": 24,
        "candidate_frame_count": 24,
    }
    recipe = VerificationControlRecipeV1.model_validate_json(json.dumps(payload))
    assert recipe.source_ranges == ((3.0, 4.0),)
    with pytest.raises(ValidationError):
        VerificationControlRecipeV1.model_validate({**payload, "path": "private.mp4"})
    with pytest.raises(ValidationError):
        VerificationControlRecipeV1.model_validate({**payload, "frame_count": True})
    with pytest.raises(ValidationError):
        VerificationControlRecipeV1.model_validate(
            {**payload, "source_ranges": ((3.0, float("nan")),)}
        )
    with pytest.raises(ValidationError):
        VerificationControlRecipeV1.model_validate({**payload, "control_sha256": _B})


def test_profile_axis_is_finite_ordered_and_strict() -> None:
    config = RescueEffectiveConfig()
    assert tuple(item.profile_id for item in config.sharpen_qualification_profiles) == (
        "full",
        "moderate",
        "gentle",
    )
    with pytest.raises(ValidationError):
        RescueEffectiveConfig.model_validate(
            {**config.model_dump(mode="python"), "sharpen_qualification_profiles": ()}
        )
    with pytest.raises(ValidationError):
        SharpenQualificationProfile.model_validate(
            {
                "profile_id": "bad",
                "cas_strength_scale": "0.5",
                "unsharp_amount_scale": 0.5,
                "pass_count": 1,
            }
        )


def test_profile_measurement_binds_full_inventory_frame_count() -> None:
    """A whole-generation PTS digest needs its whole inventory cardinality."""
    profile = RescueEffectiveConfig().sharpen_qualification_profiles[0]
    payload = _measurement(profile).model_dump(mode="python")
    payload["inventory_frame_count"] = 132

    measured = SharpenProfileMeasurementV1.model_validate(payload)

    assert measured.inventory_frame_count == 132
    del payload["inventory_frame_count"]
    with pytest.raises(ValidationError, match="inventory_frame_count"):
        SharpenProfileMeasurementV1.model_validate(payload)

    impossible = _measurement(profile).model_dump(mode="python")
    impossible["inventory_frame_count"] = 131
    with pytest.raises(ValidationError, match="bounded frame coverage"):
        SharpenProfileMeasurementV1.model_validate(impossible)


def test_profile_measurement_separates_full_inventory_from_bounded_range() -> None:
    """A full 20-second generation inventory is not a 5.5-second range count."""
    profile = RescueEffectiveConfig().sharpen_qualification_profiles[0]
    raw: dict[str, JsonValue] = {
        "baseline_sha256": _A,
        "control_sha256": _B,
        "candidate_sha256": _C,
        "baseline_normalized_pts_digest": _D,
        "control_normalized_pts_digest": _D,
        "candidate_normalized_pts_digest": _D,
        "baseline_topology_sha256": _E,
        "control_topology_sha256": _E,
        "candidate_topology_sha256": _E,
        "baseline_frame_count": 480,
        "control_frame_count": 480,
        "candidate_frame_count": 480,
        "decoded_width": 1280,
        "decoded_height": 720,
        "range_coverage_ratio": 1.0,
        "expected_frames": 132,
        "compared_frames": 132,
        "range_count": 1,
        "passing_range_count": 1,
        "minimum_aggregate_gain_ratio": 0.1,
        "minimum_recovered_baseline_ratio": 0.9,
        "minimum_improved_frame_fraction": 0.9,
        "maximum_noise_increase": 0.01,
        "maximum_edge_overshoot_ratio": 0.02,
        "maximum_edge_overshoot_amplitude": 0.03,
        "maximum_ringing_ratio": 0.04,
    }
    parameters = {
        "minimum_perceptible_sharpness_gain_ratio": 0.08,
        "minimum_recovered_baseline_ratio": 0.8,
        "minimum_improved_frame_fraction": 0.8,
        "maximum_noise_increase": 0.04,
        "maximum_edge_overshoot_ratio": 0.05,
        "maximum_edge_overshoot_amplitude": 0.05,
        "maximum_ringing_ratio": 0.08,
    }

    measured = _profile_measurement_from_raw(profile, parameters, raw)
    evidence = build_sharpen_qualification_evidence(
        input_hash=_A,
        draft_action_id="draft-sharpen",
        draft_parameters={},
        source_ranges=((4.75, 10.25),),
        output_ranges=((4.75, 10.25),),
        encode_contract=CanonicalVideoEncodeContract(),
        configured_profiles=(profile,),
        measurements=(measured,),
    )

    assert measured.inventory_frame_count == 480
    assert measured.metrics.expected_frames == 132
    assert measured.metrics.compared_frames == 132
    assert evidence.source_ranges == ((4.75, 10.25),)


def test_profile_measurement_rejects_wrong_generation() -> None:
    """Qualification artifacts must be one encode generation from one parent."""
    profile = RescueEffectiveConfig().sharpen_qualification_profiles[0]
    payload = _measurement(profile).model_dump(mode="python")
    payload["generation_count"] = 2

    with pytest.raises(ValidationError, match="generation_count"):
        SharpenProfileMeasurementV1.model_validate(payload)


@pytest.mark.parametrize(
    ("field_name", "replacement", "message"),
    (
        ("control_normalized_pts_digest", _F, "PTS inventory differs"),
        ("control_topology_sha256", _F, "topology inventory differs"),
        ("candidate_frame_count", 131, "frame inventory differs"),
    ),
)
def test_raw_profile_measurement_rejects_generation_inventory_drift(
    field_name: str, replacement: JsonValue, message: str
) -> None:
    profile = RescueEffectiveConfig().sharpen_qualification_profiles[0]
    raw: dict[str, JsonValue] = {
        "baseline_sha256": _A,
        "control_sha256": _B,
        "candidate_sha256": _C,
        "normalized_pts_digest": _D,
        "baseline_normalized_pts_digest": _D,
        "control_normalized_pts_digest": _D,
        "candidate_normalized_pts_digest": _D,
        "baseline_topology_sha256": _E,
        "control_topology_sha256": _E,
        "candidate_topology_sha256": _E,
        "inventory_frame_count": 132,
        "baseline_frame_count": 132,
        "control_frame_count": 132,
        "candidate_frame_count": 132,
        "decoded_width": 1280,
        "decoded_height": 720,
        "range_coverage_ratio": 1.0,
        "expected_frames": 132,
        "compared_frames": 132,
        "range_count": 1,
        "passing_range_count": 1,
        "minimum_aggregate_gain_ratio": 0.1,
        "minimum_recovered_baseline_ratio": 0.9,
        "minimum_improved_frame_fraction": 0.9,
        "maximum_noise_increase": 0.01,
        "maximum_edge_overshoot_ratio": 0.02,
        "maximum_edge_overshoot_amplitude": 0.03,
        "maximum_ringing_ratio": 0.04,
    }
    parameters = {
        "minimum_perceptible_sharpness_gain_ratio": 0.08,
        "minimum_recovered_baseline_ratio": 0.8,
        "minimum_improved_frame_fraction": 0.8,
        "maximum_noise_increase": 0.04,
        "maximum_edge_overshoot_ratio": 0.05,
        "maximum_edge_overshoot_amplitude": 0.05,
        "maximum_ringing_ratio": 0.08,
    }
    raw[field_name] = replacement

    with pytest.raises(ValueError, match=message):
        _profile_measurement_from_raw(profile, parameters, raw)


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    (
        ("baseline_sha256", _F),
        ("normalized_pts_digest", _F),
        ("stream_topology_digest", _F),
        ("decoded_width", 1920),
    ),
)
def test_profile_measurements_require_one_common_parent_inventory(
    field_name: str, replacement: object
) -> None:
    """Every profile must be measured against one exact baseline inventory."""
    profiles = RescueEffectiveConfig().sharpen_qualification_profiles
    measurements = [
        _measurement(profile, sha=f"{index + 1:x}" * 64)
        for index, profile in enumerate(profiles)
    ]
    measurements[1] = measurements[1].model_copy(update={field_name: replacement})

    with pytest.raises(ValueError, match="common parent inventory"):
        build_sharpen_qualification_evidence(
            input_hash=_A,
            draft_action_id="draft-sharpen",
            draft_parameters={},
            source_ranges=((4.75, 10.25),),
            output_ranges=((4.75, 10.25),),
            encode_contract=CanonicalVideoEncodeContract(),
            configured_profiles=profiles,
            measurements=measurements,
        )


def test_first_full_range_passing_profile_is_selected_deterministically() -> None:
    profiles = RescueEffectiveConfig().sharpen_qualification_profiles
    measurements = (
        _measurement(profiles[0], recovered=0.79),
        _measurement(profiles[1], recovered=0.9, sha=_F),
        _measurement(profiles[2], recovered=0.95, sha="1" * 64),
    )
    evidence = build_sharpen_qualification_evidence(
        input_hash=_A,
        draft_action_id="draft-sharpen",
        draft_parameters={},
        source_ranges=((4.75, 10.25),),
        output_ranges=((4.75, 10.25),),
        encode_contract=CanonicalVideoEncodeContract(),
        configured_profiles=profiles,
        measurements=measurements,
    )
    assert evidence.selected_profile_id == "moderate"
    assert evidence.limitation is None
    assert qualification_action_parameters(evidence)["qualification_profile_id"] == (
        "moderate"
    )


def test_partial_coverage_cannot_qualify_and_no_pass_is_truthful() -> None:
    profiles = RescueEffectiveConfig().sharpen_qualification_profiles
    measurements = tuple(
        _measurement(profile, coverage=0.6, sha=f"{index + 1:x}" * 64)
        for index, profile in enumerate(profiles)
    )
    evidence = build_sharpen_qualification_evidence(
        input_hash=_A,
        draft_action_id="draft-sharpen",
        draft_parameters={},
        source_ranges=((4.75, 10.25),),
        output_ranges=((4.75, 10.25),),
        encode_contract=CanonicalVideoEncodeContract(),
        configured_profiles=profiles,
        measurements=measurements,
    )
    assert evidence.selected_profile_id is None
    assert evidence.limitation == SHARPEN_QUALIFICATION_LIMITATION
    with pytest.raises(ValueError):
        qualification_action_parameters(evidence)


@pytest.mark.parametrize(
    ("metric_name", "failing_value"),
    (
        ("minimum_aggregate_gain_ratio", 0.079),
        ("minimum_recovered_baseline_ratio", 0.79),
        ("minimum_improved_frame_fraction", 0.79),
        ("maximum_noise_increase", 0.041),
        ("maximum_edge_overshoot_ratio", 0.051),
        ("maximum_edge_overshoot_amplitude", 0.051),
        ("maximum_ringing_ratio", 0.081),
    ),
)
def test_each_unchanged_clarity_gate_rejects_independently(
    metric_name: str, failing_value: float
) -> None:
    profiles = RescueEffectiveConfig().sharpen_qualification_profiles
    measurements: list[SharpenProfileMeasurementV1] = []
    for index, profile in enumerate(profiles):
        measurement = _measurement(profile, sha=f"{index + 1:x}" * 64)
        measurements.append(
            measurement.model_copy(
                update={
                    "metrics": measurement.metrics.model_copy(
                        update={metric_name: failing_value}
                    )
                }
            )
        )

    evidence = build_sharpen_qualification_evidence(
        input_hash=_A,
        draft_action_id="draft-sharpen",
        draft_parameters={},
        source_ranges=((4.75, 10.25),),
        output_ranges=((4.75, 10.25),),
        encode_contract=CanonicalVideoEncodeContract(),
        configured_profiles=profiles,
        measurements=measurements,
    )

    assert evidence.selected_profile_id is None
    assert evidence.limitation == SHARPEN_QUALIFICATION_LIMITATION


def test_each_profile_must_measure_every_retained_range() -> None:
    """An aggregate pass cannot replace one result per retained action range."""
    profiles = RescueEffectiveConfig().sharpen_qualification_profiles
    measurements = tuple(
        _measurement(profile, sha=f"{index + 1:x}" * 64, range_count=2)
        for index, profile in enumerate(profiles)
    )

    with pytest.raises(ValueError, match="retained range inventory"):
        build_sharpen_qualification_evidence(
            input_hash=_A,
            draft_action_id="draft-sharpen",
            draft_parameters={},
            source_ranges=((4.75, 10.25),),
            output_ranges=((4.75, 10.25),),
            encode_contract=CanonicalVideoEncodeContract(),
            configured_profiles=profiles,
            measurements=measurements,
        )


def test_qualification_binds_exact_output_range_mapping() -> None:
    profiles = RescueEffectiveConfig().sharpen_qualification_profiles
    measurements = tuple(
        _measurement(profile, sha=f"{index + 1:x}" * 64, range_count=2)
        for index, profile in enumerate(profiles)
    )
    source_ranges = ((1.0, 2.0), (4.0, 5.0))
    output_ranges = ((0.0, 1.0), (2.0, 3.0))

    evidence = build_sharpen_qualification_evidence(
        input_hash=_A,
        draft_action_id="draft-sharpen",
        draft_parameters={},
        source_ranges=source_ranges,
        output_ranges=output_ranges,
        encode_contract=CanonicalVideoEncodeContract(),
        configured_profiles=profiles,
        measurements=measurements,
    )

    assert evidence.output_ranges == output_ranges
    with pytest.raises(ValueError, match="output range inventory"):
        build_sharpen_qualification_evidence(
            input_hash=_A,
            draft_action_id="draft-sharpen",
            draft_parameters={},
            source_ranges=source_ranges,
            output_ranges=output_ranges[:1],
            encode_contract=CanonicalVideoEncodeContract(),
            configured_profiles=profiles,
            measurements=measurements,
        )


@pytest.mark.parametrize(
    "private_value",
    (
        "C:/private/control.mp4",
        "../private.mp4",
        "foo/bar.mp4",
        "foo/bar",
        "foo\\bar",
        ".\\control.mp4",
        "https://example.invalid/control.mp4",
    ),
)
def test_qualification_evidence_rejects_path_bearing_draft_parameters(
    private_value: str,
) -> None:
    """A private render path must never cross the qualification wire boundary."""
    profiles = RescueEffectiveConfig().sharpen_qualification_profiles
    measurements = tuple(
        _measurement(profile, sha=f"{index + 1:x}" * 64)
        for index, profile in enumerate(profiles)
    )

    with pytest.raises(ValueError, match="path"):
        build_sharpen_qualification_evidence(
            input_hash=_A,
            draft_action_id="draft-sharpen",
            draft_parameters={"private_control": private_value},
            source_ranges=((4.75, 10.25),),
            output_ranges=((4.75, 10.25),),
            encode_contract=CanonicalVideoEncodeContract(),
            configured_profiles=profiles,
            measurements=measurements,
        )


def test_profile_measurement_order_is_not_reordered_by_input_identity() -> None:
    profiles = RescueEffectiveConfig().sharpen_qualification_profiles
    measurements = tuple(
        _measurement(profile, recovered=0.9, sha=f"{index + 1:x}" * 64)
        for index, profile in enumerate(profiles)
    )
    for permutation in itertools.permutations(measurements):
        if permutation == measurements:
            continue
        with pytest.raises(ValueError, match="configured order"):
            build_sharpen_qualification_evidence(
                input_hash=_A,
                draft_action_id="draft-sharpen",
                draft_parameters={},
                source_ranges=((4.75, 10.25),),
                output_ranges=((4.75, 10.25),),
                encode_contract=CanonicalVideoEncodeContract(),
                configured_profiles=profiles,
                measurements=permutation,
            )

    duplicate_measurements = (measurements[0], measurements[0], measurements[2])
    with pytest.raises(ValueError, match="configured order"):
        build_sharpen_qualification_evidence(
            input_hash=_A,
            draft_action_id="draft-sharpen",
            draft_parameters={},
            source_ranges=((4.75, 10.25),),
            output_ranges=((4.75, 10.25),),
            encode_contract=CanonicalVideoEncodeContract(),
            configured_profiles=profiles,
            measurements=duplicate_measurements,
        )


def test_selected_profile_changes_only_bounded_sharpen_strength_axis() -> None:
    profile = RescueEffectiveConfig().sharpen_qualification_profiles[1]
    original = {
        "adaptive_strength": 0.32,
        "amount": 1.6,
        "detail_passes": 3,
        "visibility_brightness": 0.15,
    }
    selected = apply_qualified_sharpen_profile(original, profile)
    assert selected == {
        "adaptive_strength": 0.24,
        "amount": pytest.approx(1.2),
        "detail_passes": 2,
        "visibility_brightness": 0.15,
    }


def test_final_action_identity_binds_full_qualification_and_no_pass_omits() -> None:
    config = RescueEffectiveConfig()
    contract = CanonicalVideoEncodeContract()
    parameters: dict[str, JsonValue] = {
        "adaptive_strength": 0.32,
        "amount": 1.6,
        "detail_passes": 3,
        "visibility_brightness": 0.15,
        "minimum_perceptible_sharpness_gain_ratio": 0.08,
        "minimum_recovered_baseline_ratio": 0.8,
        "minimum_improved_frame_fraction": 0.8,
        "maximum_noise_increase": 0.04,
        "maximum_edge_overshoot_ratio": 0.05,
        "maximum_edge_overshoot_amplitude": 0.05,
        "maximum_ringing_ratio": 0.08,
        "video_encode_contract": contract.model_dump(mode="json"),
    }
    ranges = ((4.75, 10.25),)
    draft_id = make_rescue_action_id(
        kind=RescueActionKind.SHARPEN,
        parameters=parameters,
        source_ranges=ranges,
        strategy=RescueStrategy.BALANCED,
        version="1",
    )
    draft = RescueAction(
        id=draft_id,
        version="1",
        kind=RescueActionKind.SHARPEN,
        description="Sharpen measured soft detail.",
        source_ranges=ranges,
        parameters=parameters,
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    passing_measurements = tuple(
        _measurement(profile, recovered=0.9, sha=f"{index + 1:x}" * 64)
        for index, profile in enumerate(config.sharpen_qualification_profiles)
    )
    passing = build_sharpen_qualification_evidence(
        input_hash=_A,
        draft_action_id=draft.id,
        draft_parameters=parameters,
        source_ranges=ranges,
        output_ranges=ranges,
        encode_contract=contract,
        configured_profiles=config.sharpen_qualification_profiles,
        measurements=passing_measurements,
    )
    actions = _apply_sharpen_qualification(
        (draft,), passing, input_hash=_A, config=config
    )
    assert len(actions) == 1
    assert actions[0].id != draft.id
    assert actions[0].parameters["qualification_profile_id"] == "full"
    validate_plan_sharpen_qualification_contracts(
        SimpleNamespace(
            actions=actions,
            input_hash=_A,
            effective_config=config,
        )
    )
    with pytest.raises(ValueError, match="qualification is missing"):
        validate_plan_sharpen_qualification_contracts(
            SimpleNamespace(
                actions=(draft,),
                input_hash=_A,
                effective_config=config,
            )
        )

    tampered_parameters = actions[0].parameters.copy()
    tampered_qualification = cast(
        dict[str, JsonValue], tampered_parameters["qualification"]
    ).copy()
    measurements_wire = list(
        cast(list[JsonValue], tampered_qualification["profile_measurements"])
    )
    first_measurement = cast(dict[str, JsonValue], measurements_wire[0]).copy()
    first_thresholds = cast(
        dict[str, JsonValue], first_measurement["thresholds"]
    ).copy()
    first_thresholds["maximum_noise_increase"] = 0.4
    first_measurement["thresholds"] = first_thresholds
    measurements_wire[0] = first_measurement
    tampered_qualification["profile_measurements"] = measurements_wire
    tampered_parameters["qualification"] = tampered_qualification
    tampered = actions[0].model_copy(
        update={
            "parameters": tampered_parameters,
            "id": make_rescue_action_id(
                kind=actions[0].kind,
                parameters=tampered_parameters,
                source_ranges=actions[0].source_ranges,
                strategy=actions[0].strategy,
                version=actions[0].version,
            ),
        }
    )
    with pytest.raises(ValueError, match="thresholds"):
        validate_plan_sharpen_qualification_contracts(
            SimpleNamespace(
                actions=(tampered,),
                input_hash=_A,
                effective_config=config,
            )
        )

    failing_measurements = tuple(
        _measurement(
            profile,
            recovered=0.79,
            sha=f"{index + 4:x}" * 64,
        )
        for index, profile in enumerate(config.sharpen_qualification_profiles)
    )
    failing = build_sharpen_qualification_evidence(
        input_hash=_A,
        draft_action_id=draft.id,
        draft_parameters=parameters,
        source_ranges=ranges,
        output_ranges=ranges,
        encode_contract=contract,
        configured_profiles=config.sharpen_qualification_profiles,
        measurements=failing_measurements,
    )
    assert (
        _apply_sharpen_qualification((draft,), failing, input_hash=_A, config=config)
        == ()
    )


def test_exact_qualification_range_mapping_rejects_scaled_or_partial_time() -> None:
    ranges = ((1.0, 2.0),)
    with pytest.raises(ValueError, match="not exact"):
        _map_exact_qualification_ranges(
            ranges,
            (SourceMapping(0.0, 3.0, 0.0, 2.0, "faithful.mp4"),),
        )
    with pytest.raises(ValueError, match="not exactly retained"):
        _map_exact_qualification_ranges(
            ranges,
            (SourceMapping(0.0, 1.5, 0.0, 1.5, "faithful.mp4"),),
        )


def test_native_qualifier_renders_full_same_generation_profile_inventory_and_cleans(
    tmp_path: Path,
) -> None:
    config = RescueEffectiveConfig()
    contract = CanonicalVideoEncodeContract()
    ranges = ((4.75, 10.25),)
    parameters: dict[str, JsonValue] = {
        "adaptive_strength": 0.32,
        "amount": 1.6,
        "detail_passes": 3,
        "visibility_brightness": 0.15,
        "boundary_transition_seconds": 0.25,
        "minimum_perceptible_sharpness_gain_ratio": 0.08,
        "minimum_recovered_baseline_ratio": 0.8,
        "minimum_improved_frame_fraction": 0.8,
        "maximum_noise_increase": 0.02,
        "maximum_edge_overshoot_ratio": 0.05,
        "maximum_edge_overshoot_amplitude": 0.05,
        "maximum_ringing_ratio": 0.08,
        "video_encode_contract": contract.model_dump(mode="json"),
    }
    draft_id = make_rescue_action_id(
        kind=RescueActionKind.SHARPEN,
        parameters=parameters,
        source_ranges=ranges,
        strategy=RescueStrategy.BALANCED,
        version="1",
    )
    action = RescueAction(
        id=draft_id,
        version="1",
        kind=RescueActionKind.SHARPEN,
        description="Sharpen measured soft detail.",
        source_ranges=ranges,
        parameters=parameters,
        changes_content=True,
        requires_confirmation=True,
        strategy=RescueStrategy.BALANCED,
    )
    draft = SimpleNamespace(
        actions=(action,),
        effective_config=config,
        input_hash=_A,
    )
    calls: list[tuple[str, str, tuple[tuple[float, float], ...]]] = []

    class Executor:
        def execute_faithful(
            self,
            _plan: object,
            _source: Path,
            root: Path,
            _cancel: object,
            **_kwargs: object,
        ) -> object:
            parent = root / "staging" / "faithful-rescue.mp4"
            parent.parent.mkdir(parents=True)
            parent.write_bytes(b"faithful-parent")
            return SimpleNamespace(
                output_path=parent,
                source_mappings=(
                    SourceMapping(0.0, 20.0, 0.0, 20.0, "faithful-rescue.mp4"),
                ),
            )

        def render_sharpen_qualification_candidate(self, **kwargs: object) -> None:
            output = Path(kwargs["output"])  # type: ignore[arg-type]
            mode = str(kwargs["mode"])
            raw_parameters = kwargs["parameters"]
            assert isinstance(raw_parameters, dict)
            profile = str(raw_parameters.get("detail_passes", "control"))
            source_ranges = kwargs["source_ranges"]
            assert isinstance(source_ranges, tuple)
            calls.append((mode, profile, source_ranges))
            output.write_bytes((mode + profile).encode("ascii"))

    class Provider:
        def measure_sharpen_qualification(
            self,
            baseline: Path,
            visibility: Path,
            candidate: Path,
            output_ranges: tuple[tuple[float, float], ...],
            _parameters: object,
            _cancel: object,
        ) -> dict[str, object]:
            assert baseline.is_file() and visibility.is_file() and candidate.is_file()
            assert output_ranges == ranges
            passes = not candidate.name.endswith("full.mp4")
            return {
                "baseline_sha256": _A,
                "control_sha256": _B,
                "candidate_sha256": sha256(candidate.read_bytes()).hexdigest(),
                "normalized_pts_digest": _D,
                "baseline_normalized_pts_digest": _D,
                "control_normalized_pts_digest": _D,
                "candidate_normalized_pts_digest": _D,
                "baseline_topology_sha256": _E,
                "control_topology_sha256": _E,
                "candidate_topology_sha256": _E,
                "decoded_width": 1280,
                "decoded_height": 720,
                "inventory_frame_count": 132,
                "baseline_frame_count": 132,
                "control_frame_count": 132,
                "candidate_frame_count": 132,
                "range_coverage_ratio": 1.0,
                "expected_frames": 132,
                "compared_frames": 132,
                "range_count": 1,
                "passing_range_count": 1,
                "minimum_aggregate_gain_ratio": 0.1,
                "minimum_recovered_baseline_ratio": 0.9 if passes else 0.79,
                "minimum_improved_frame_fraction": 0.9,
                "maximum_noise_increase": 0.01,
                "maximum_edge_overshoot_ratio": 0.02,
                "maximum_edge_overshoot_amplitude": 0.03,
                "maximum_ringing_ratio": 0.04,
            }

    root = tmp_path / "qualification"
    evidence = NativeRescueCandidateQualifier(
        executor=Executor(), measurement_provider=Provider()
    ).qualify(draft, tmp_path / "source.mp4", root, lambda: False)

    assert evidence.selected_profile_id == "moderate"
    assert [item[0] for item in calls] == [
        "baseline",
        "visibility",
        "candidate",
        "visibility",
        "candidate",
        "visibility",
        "candidate",
    ]
    assert [item[1] for item in calls[1:]] == ["3", "3", "2", "2", "1", "1"]
    assert all(item[2] == ranges for item in calls)
    assert not root.exists()

    class FailingExecutor(Executor):
        def render_sharpen_qualification_candidate(self, **kwargs: object) -> None:
            super().render_sharpen_qualification_candidate(**kwargs)
            if kwargs["mode"] == "candidate":
                raise RescueMediaError("injected qualification render failure")

    failed_root = tmp_path / "failed-qualification"
    with pytest.raises(RescueMediaError):
        NativeRescueCandidateQualifier(
            executor=FailingExecutor(), measurement_provider=Provider()
        ).qualify(draft, tmp_path / "source.mp4", failed_root, lambda: False)
    assert not failed_root.exists()

    cancelled = False

    class CancellingExecutor(Executor):
        def render_sharpen_qualification_candidate(self, **kwargs: object) -> None:
            nonlocal cancelled
            if cancelled:
                raise RescueCancelledError("injected qualification cancellation")
            super().render_sharpen_qualification_candidate(**kwargs)
            cancelled = True

    cancelled_root = tmp_path / "cancelled-qualification"
    with pytest.raises(RescueCancelledError):
        NativeRescueCandidateQualifier(
            executor=CancellingExecutor(), measurement_provider=Provider()
        ).qualify(draft, tmp_path / "source.mp4", cancelled_root, lambda: cancelled)
    assert not cancelled_root.exists()
