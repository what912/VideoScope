"""Pure contracts for the private V15 clarity runtime provenance envelope."""

from __future__ import annotations

import copy
import inspect
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from types import FunctionType
from typing import Any, Literal, TypedDict, cast

import pytest
from pydantic import JsonValue

import tests.rescue.clarity_runtime_provenance as provenance_module
import tests.rescue.test_fixture_rescue as fixture_rescue_module
from tests.rescue.clarity_runtime_provenance import (
    CLARITY_CALL_REPORT_KEY,
    DEFAULT_COMPONENTS,
    EXACT_CLARITY_NODE_ID,
    ClarityEventInput,
    ClarityPytestCallReport,
    ClarityRuntimeEventV1,
    ClarityRuntimeGuard,
    ClarityRuntimeObserver,
    ClarityRuntimeProvenanceV1,
    ClarityToolIdentityV1,
    ProductionComponentSpec,
    build_event_chain,
    canonical_provenance_bytes,
    production_component,
    provenance_digest,
    read_clarity_runtime_provenance,
    verify_clarity_tool_identity,
    write_clarity_runtime_provenance,
)
from videoscope.domain import VideoMetadata
from videoscope.rescue.errors import RescueCancelledError
from videoscope.rescue.executor import (
    CommandResult,
    NativeRescueExecutor,
    RescuedSegment,
    RescueExecutionResult,
    RescueImprovedExecutionResult,
    SourceMapping,
)
from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueActionKind,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    canonical_video_encode_contract,
    make_damage_id,
)
from videoscope.rescue.pipeline import (
    _cleanup_verification_controls,
    _public_source_mappings,
)
from videoscope.rescue.planner import build_rescue_plan
from videoscope.rescue.qualification import (
    SHARPEN_QUALIFICATION_LIMITATION,
    NativeRescueCandidateQualifier,
    SharpenProfileMeasurementV1,
    SharpenQualificationEvidenceV1,
    SharpenQualificationMetricsV1,
    SharpenQualificationThresholdsV1,
    SharpenVerificationControlHandle,
    SharpenVerificationControlRecipeV1,
    build_sharpen_qualification_evidence,
)
from videoscope.rescue.verification import (
    MediaVerificationSnapshot,
    NativeMediaMeasurementProvider,
    RescueVerifier,
)
from videoscope.rescue.visual import VisualAssessment, VisualMetrics

SHA256_PATTERN = r"^[0-9a-f]{64}$"

_PASSED_PHASES = (
    "tool_identity_verified",
    "draft_bound",
    "qualification_returned",
    "qualification_cleanup_verified",
    "final_plan_bound",
    "faithful_returned",
    "improved_returned",
    "verification_returned",
    "controls_cleanup_returned",
    "source_integrity_verified",
    "publication_absence_verified",
)

_PHASE_OUTCOMES = {
    "tool_identity_verified": "verified",
    "draft_bound": "verified",
    "qualification_returned": "returned",
    "qualification_cleanup_verified": "verified",
    "final_plan_bound": "verified",
    "faithful_returned": "returned",
    "improved_returned": "returned",
    "verification_returned": "returned",
    "controls_cleanup_returned": "returned",
    "source_integrity_verified": "verified",
    "publication_absence_verified": "verified",
}


def test_exact_clarity_selector_signature_binds_runtime_guard() -> None:
    selector_name = (
        "test_native_fixed_8_1_2_soft_detail_qualification_matches_final_verifier"
    )
    selector = getattr(fixture_rescue_module, selector_name)
    expected_node_id = (
        f"{fixture_rescue_module.__name__.replace('.', '/')}.py::{selector.__name__}"
    )

    assert selector.__module__ == fixture_rescue_module.__name__
    assert selector.__name__ == selector_name
    assert EXACT_CLARITY_NODE_ID == expected_node_id
    assert "clarity_runtime_provenance_guard" in inspect.signature(selector).parameters


_PHASE_COMPONENTS = {
    "tool_identity_verified": "tool_identity_verifier",
    "draft_bound": "build_rescue_plan",
    "qualification_returned": "NativeRescueCandidateQualifier.qualify",
    "qualification_cleanup_verified": "qualification_cleanup",
    "final_plan_bound": "build_rescue_plan",
    "faithful_returned": "NativeRescueExecutor.execute_faithful",
    "improved_returned": "NativeRescueExecutor.execute_improved_with_controls",
    "verification_returned": "RescueVerifier.verify",
    "controls_cleanup_returned": "_cleanup_verification_controls",
    "source_integrity_verified": "source_integrity",
    "publication_absence_verified": "publication_absence",
}
_ACTION_ID = f"rescue_action_{sha256(b'sharpen-action').hexdigest()}"


def _digest(label: str) -> str:
    return sha256(label.encode("utf-8")).hexdigest()


def _independent_json_value(value: object) -> object:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")  # type: ignore[union-attr]
    if isinstance(value, Mapping):
        return {str(key): _independent_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_independent_json_value(item) for item in value]
    return value


def _independent_canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            _independent_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _independent_digest(value: object) -> str:
    return sha256(_independent_canonical_bytes(value)).hexdigest()


def _event_payloads(
    phases: tuple[str, ...] = _PASSED_PHASES,
) -> tuple[dict[str, object], ...]:
    events: list[dict[str, object]] = []
    previous: str | None = None
    for sequence, phase in enumerate(phases):
        payload: dict[str, object] = {
            "sequence": sequence,
            "phase": phase,
            "component": _PHASE_COMPONENTS[phase],
            "outcome": _PHASE_OUTCOMES[phase],
            "stable_input_digest": None if sequence == 0 else _digest(f"in-{sequence}"),
            "stable_output_digest": _digest(f"out-{sequence}"),
            "previous_event_sha256": previous,
        }
        event_digest = _independent_digest(payload)
        payload["event_sha256"] = event_digest
        events.append(payload)
        previous = event_digest
    return tuple(events)


def _valid_payload(*, metric_override: float | None = None) -> dict[str, object]:
    metric = 0.125 if metric_override is None else metric_override
    events = _event_payloads()
    payload: dict[str, object] = {
        "schema_version": "1",
        "track": "sharpen_clarity",
        "producer_version": "clarity_runtime_provenance_v1",
        "selector_id": "clarity_exact_native_v1",
        "outcome": "passed",
        "component_manifest": (
            {
                "module": "videoscope.rescue.planner",
                "qualname": "build_rescue_plan",
                "source_sha256": _digest("planner-source"),
            },
            {
                "module": "videoscope.rescue.qualification",
                "qualname": "NativeRescueCandidateQualifier.qualify",
                "source_sha256": _digest("qualifier-source"),
            },
            {
                "module": "videoscope.rescue.executor",
                "qualname": "NativeRescueExecutor.execute_faithful",
                "source_sha256": _digest("faithful-source"),
            },
            {
                "module": "videoscope.rescue.executor",
                "qualname": "NativeRescueExecutor.execute_improved_with_controls",
                "source_sha256": _digest("improved-source"),
            },
            {
                "module": "videoscope.rescue.verification",
                "qualname": "RescueVerifier.verify",
                "source_sha256": _digest("verifier-source"),
            },
            {
                "module": "videoscope.rescue.pipeline",
                "qualname": "_cleanup_verification_controls",
                "source_sha256": _digest("cleanup-source"),
            },
        ),
        "tools": (
            {
                "role": "ffmpeg",
                "binary_sha256": _digest("ffmpeg-binary"),
                "reported_version_line": "ffmpeg version 8.1.2",
                "version_stdout_sha256": _digest("ffmpeg-version-stdout"),
                "semantic_version": "8.1.2",
            },
            {
                "role": "ffprobe",
                "binary_sha256": _digest("ffprobe-binary"),
                "reported_version_line": "ffprobe version 8.1.2",
                "version_stdout_sha256": _digest("ffprobe-version-stdout"),
                "semantic_version": "8.1.2",
            },
        ),
        "source": {
            "sha256_before": _digest("source"),
            "sha256_after": _digest("source"),
            "size_bytes": 4096,
        },
        "draft": {
            "input_hash": _digest("source"),
            "plan_digest": _digest("draft-plan"),
            "action_id": _ACTION_ID,
            "config_digest": _digest("config"),
            "encode_contract_digest": _digest("encode-contract"),
            "source_ranges": ((0.0, 6.0),),
        },
        "qualification": {
            "evidence_digest": _digest("qualification-evidence"),
            "profile_order": ("full", "moderate", "gentle"),
            "selected_profile_id": "moderate",
            "selected_identity_digest": _digest("selected-identity"),
            "selected_metrics_digest": _digest("selected-metrics"),
        },
        "final": {
            "plan_digest": _digest("final-plan"),
            "action_id": _ACTION_ID,
            "source_mappings_digest": _digest("source-mappings"),
            "output_ranges_digest": _digest("output-ranges"),
            "faithful_sha256": _digest("faithful"),
            "improved_sha256": _digest("improved"),
        },
        "runtime_recipe": {
            "recipe_digest": _digest("runtime-recipe"),
            "baseline_sha256": _digest("baseline"),
            "visibility_control_sha256": _digest("visibility-control"),
            "candidate_sha256": _digest("candidate"),
            "normalized_pts_digest": _digest("normalized-pts"),
            "stream_topology_digest": _digest("stream-topology"),
            "inventory_frame_count": 60,
            "source_ranges_digest": _digest("source-ranges"),
            "output_ranges_digest": _digest("output-ranges"),
        },
        "verification": {
            "report_digest": _digest("verification-report"),
            "required_check_id": "perceptible_sharpness_improvement",
            "required_check_status": "passed",
            "runtime_control_recipe_valid": True,
            "selected_qualification_binding_valid": True,
            "expected_frames": 60,
            "compared_frames": 60,
            "range_count": 1,
            "passing_range_count": 1,
            "range_coverage_ratio": 1.0,
            "minimum_aggregate_gain_ratio": metric,
            "minimum_recovered_baseline_ratio": 1.0,
            "minimum_improved_frame_fraction": 1.0,
            "maximum_noise_increase": 0.0,
            "maximum_edge_overshoot_ratio": 0.0,
            "maximum_edge_overshoot_amplitude": 0.0,
            "maximum_ringing_ratio": 0.0,
            "metrics_digest": _digest("verification-metrics"),
        },
        "cleanup": {
            "qualification_root_absent": True,
            "control_count": 2,
            "controls_absent": True,
            "source_unchanged": True,
            "public_outputs_absent": True,
        },
        "events": events,
        "events_digest": _independent_digest({"events": events}),
        "error": None,
    }
    payload["envelope_digest"] = _independent_digest(payload)
    return payload


def _valid_passed_envelope(
    *, metric_override: float | None = None
) -> ClarityRuntimeProvenanceV1:
    return ClarityRuntimeProvenanceV1.model_validate(
        _valid_payload(metric_override=metric_override)
    )


def _reseal_payload(payload: dict[str, object]) -> None:
    events = payload["events"]
    payload["events_digest"] = _independent_digest({"events": events})
    payload.pop("envelope_digest", None)
    payload["envelope_digest"] = _independent_digest(payload)


def _no_profile_payload() -> dict[str, object]:
    payload = _valid_payload()
    payload["outcome"] = "no_profile_passed"
    payload["qualification"] = {
        "evidence_digest": _digest("qualification-evidence"),
        "profile_order": ("full", "moderate", "gentle"),
        "selected_profile_id": None,
        "selected_identity_digest": None,
        "selected_metrics_digest": None,
    }
    payload["final"] = None
    payload["runtime_recipe"] = None
    payload["verification"] = None
    payload["cleanup"] = {
        "qualification_root_absent": True,
        "control_count": 0,
        "controls_absent": True,
        "source_unchanged": True,
        "public_outputs_absent": True,
    }
    payload["events"] = _event_payloads(
        (
            "tool_identity_verified",
            "draft_bound",
            "qualification_returned",
            "qualification_cleanup_verified",
            "source_integrity_verified",
            "publication_absence_verified",
        )
    )
    _reseal_payload(payload)
    return payload


def _partial_error_payload(outcome: str) -> dict[str, object]:
    payload = _valid_payload()
    payload["outcome"] = outcome
    payload["final"] = None
    payload["runtime_recipe"] = None
    payload["verification"] = None
    payload["error"] = {
        "phase": "qualification_cleanup",
        "code": "selector_interrupted",
    }
    payload["cleanup"] = {
        "qualification_root_absent": True,
        "control_count": 0,
        "controls_absent": True,
        "source_unchanged": True,
        "public_outputs_absent": True,
    }
    payload["events"] = _event_payloads(
        (
            "tool_identity_verified",
            "draft_bound",
            "qualification_returned",
            "qualification_cleanup_verified",
        )
    )
    _reseal_payload(payload)
    return payload


def _source_integrity_error_payload(
    source: dict[str, object] | None,
) -> dict[str, object]:
    payload = _valid_payload()
    payload["outcome"] = "error"
    payload["source"] = source
    payload["draft"] = None
    payload["qualification"] = None
    payload["final"] = None
    payload["runtime_recipe"] = None
    payload["verification"] = None
    payload["error"] = {
        "phase": "source_integrity",
        "code": "selector_interrupted",
    }
    payload["cleanup"] = {
        "qualification_root_absent": True,
        "control_count": 0,
        "controls_absent": True,
        "source_unchanged": True,
        "public_outputs_absent": True,
    }
    payload["events"] = _event_payloads(("source_integrity_verified",))
    _reseal_payload(payload)
    return payload


def _mutated_payload(
    mutate: Callable[[dict[str, Any]], None],
) -> dict[str, object]:
    payload = copy.deepcopy(_valid_payload())
    mutate(payload)
    return payload


def _remove_required_event(payload: dict[str, Any]) -> None:
    payload["events"] = tuple(payload["events"][:-1])


def _duplicate_event(payload: dict[str, Any]) -> None:
    payload["events"] = (*payload["events"], copy.deepcopy(payload["events"][-1]))


def _reorder_events(payload: dict[str, Any]) -> None:
    events = list(payload["events"])
    events[3], events[4] = events[4], events[3]
    payload["events"] = tuple(events)


def _break_previous_hash(payload: dict[str, Any]) -> None:
    payload["events"][1]["previous_event_sha256"] = "f" * 64


def _change_event_payload_without_rehash(payload: dict[str, Any]) -> None:
    payload["events"][2]["component"] = "different_component"


def _change_envelope_without_rehash(payload: dict[str, Any]) -> None:
    payload["source"]["size_bytes"] = 8192


def test_clarity_runtime_provenance_round_trip_is_canonical_and_path_free(
    tmp_path: Path,
) -> None:
    envelope = _valid_passed_envelope()
    payload = canonical_provenance_bytes(envelope)

    assert payload == _independent_canonical_bytes(envelope)
    assert payload.endswith(b"\n")
    assert b"C:\\" not in payload
    assert b"/tmp/" not in payload

    path = write_clarity_runtime_provenance(tmp_path / "audit", envelope)

    assert path == tmp_path / "audit" / "clarity-runtime-provenance.json"
    assert read_clarity_runtime_provenance(path) == envelope


@pytest.mark.parametrize(
    "manifest_drift",
    ("missing", "extra", "wrong-component-identity"),
)
def test_strict_readback_requires_exact_six_component_manifest(
    tmp_path: Path,
    manifest_drift: str,
) -> None:
    payload = _valid_payload()
    manifest = list(cast(tuple[dict[str, object], ...], payload["component_manifest"]))
    assert len(manifest) == 6
    if manifest_drift == "missing":
        manifest.pop()
    elif manifest_drift == "extra":
        manifest.append(copy.deepcopy(manifest[0]))
    elif manifest_drift == "wrong-component-identity":
        manifest[0] = {
            "module": "videoscope.rescue.executor",
            "qualname": "NativeRescueExecutor.execute_faithful",
            "source_sha256": _digest("wrong component identity"),
        }
    else:
        raise AssertionError(f"unknown manifest drift: {manifest_drift}")
    payload["component_manifest"] = tuple(manifest)
    _reseal_payload(payload)
    path = tmp_path / "clarity-runtime-provenance.json"
    path.write_bytes(_independent_canonical_bytes(payload))

    with pytest.raises(ValueError, match="component manifest|duplicates"):
        read_clarity_runtime_provenance(path)


@pytest.mark.parametrize(
    "mutation",
    [
        _remove_required_event,
        _duplicate_event,
        _reorder_events,
        _break_previous_hash,
        _change_event_payload_without_rehash,
        _change_envelope_without_rehash,
    ],
    ids=(
        "missing-event",
        "duplicate-event",
        "reordered-event",
        "broken-previous-hash",
        "event-payload-drift",
        "envelope-payload-drift",
    ),
)
def test_clarity_runtime_provenance_rejects_event_or_digest_tamper(
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    with pytest.raises(ValueError):
        ClarityRuntimeProvenanceV1.model_validate(_mutated_payload(mutation))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_clarity_runtime_provenance_rejects_nonfinite_metrics(value: float) -> None:
    payload = _valid_payload()
    verification = payload["verification"]
    assert isinstance(verification, dict)
    verification["minimum_aggregate_gain_ratio"] = value

    with pytest.raises(ValueError):
        ClarityRuntimeProvenanceV1.model_validate(payload)


def test_clarity_runtime_provenance_preserves_signed_zero_in_digest() -> None:
    positive = _valid_payload(metric_override=0.0)
    negative = _valid_payload(metric_override=-0.0)

    assert b'"minimum_aggregate_gain_ratio":0.0' in canonical_provenance_bytes(positive)
    assert b'"minimum_aggregate_gain_ratio":-0.0' in canonical_provenance_bytes(
        negative
    )
    assert provenance_digest(positive) != provenance_digest(negative)


@pytest.mark.parametrize(
    "path_bearing",
    [
        r"C:\Users\person\audit.json",
        "/tmp/audit.json",
        r"\\server\share\audit.json",
        "../audit.json",
        "foo/bar",
        r"foo\bar",
        "file:audit.json",
        "https:example.invalid",
    ],
    ids=("windows", "posix", "unc", "parent", "slash", "backslash", "file", "https"),
)
@pytest.mark.parametrize("location", ["nested-key", "nested-value"])
def test_clarity_runtime_provenance_rejects_path_bearing_nested_content(
    path_bearing: str,
    location: str,
) -> None:
    value: dict[str, object]
    if location == "nested-key":
        value = {"outer": {path_bearing: "safe"}}
    else:
        value = {"outer": {"inner": path_bearing}}

    with pytest.raises(ValueError, match="path"):
        canonical_provenance_bytes(value)


def test_clarity_runtime_provenance_rejects_path_content_before_writing(
    tmp_path: Path,
) -> None:
    envelope = _valid_passed_envelope()
    component = envelope.component_manifest[0]
    unsafe_component = type(component).model_construct(
        module=component.module,
        qualname="foo/bar",
        source_sha256=component.source_sha256,
    )
    values = {
        field_name: getattr(envelope, field_name)
        for field_name in ClarityRuntimeProvenanceV1.model_fields
    }
    values["component_manifest"] = (
        unsafe_component,
        *envelope.component_manifest[1:],
    )
    unsafe = ClarityRuntimeProvenanceV1.model_construct(**values)
    root = tmp_path / "must not exist"

    with pytest.raises(ValueError, match="path"):
        write_clarity_runtime_provenance(root, unsafe)

    assert not root.exists()


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda value: value.__setitem__("unexpected", "field"), "extra"),
        (lambda value: value["source"].__setitem__("size_bytes", True), "integer"),
        (lambda value: value.__setitem__("events_digest", "not-a-hash"), "pattern"),
    ],
    ids=("extra-field", "bool-as-integer", "invalid-hash"),
)
def test_clarity_runtime_provenance_is_strict(
    mutation: Callable[[dict[str, Any]], None],
    match: str,
) -> None:
    payload = copy.deepcopy(_valid_payload())
    mutation(payload)

    with pytest.raises(ValueError, match=match):
        ClarityRuntimeProvenanceV1.model_validate(payload)


def test_clarity_event_chain_matches_independently_derived_hashes() -> None:
    inputs = tuple(
        ClarityEventInput(
            phase=phase,
            component=_PHASE_COMPONENTS[phase],
            outcome=_PHASE_OUTCOMES[phase],
            stable_input_digest=None if sequence == 0 else _digest(f"in-{sequence}"),
            stable_output_digest=_digest(f"out-{sequence}"),
        )
        for sequence, phase in enumerate(_PASSED_PHASES)
    )

    events = build_event_chain(inputs)

    assert tuple(item.model_dump(mode="python") for item in events) == _event_payloads()


def test_clarity_event_chain_rejects_duplicate_reordered_or_wrong_outcome() -> None:
    valid = ClarityEventInput(
        phase="tool_identity_verified",
        component="tool_identity_verifier",
        outcome="verified",
        stable_input_digest=None,
        stable_output_digest=_digest("tools"),
    )

    with pytest.raises(ValueError, match="order"):
        build_event_chain((valid, valid))
    with pytest.raises(ValueError, match="order"):
        build_event_chain(
            (
                ClarityEventInput(
                    phase="draft_bound",
                    component="build_rescue_plan",
                    outcome="verified",
                    stable_input_digest=_digest("draft-in"),
                    stable_output_digest=_digest("draft-out"),
                ),
                valid,
            )
        )
    with pytest.raises(ValueError, match="outcome"):
        build_event_chain(
            (
                ClarityEventInput(
                    phase="tool_identity_verified",
                    component="tool_identity_verifier",
                    outcome="returned",
                    stable_input_digest=None,
                    stable_output_digest=_digest("tools"),
                ),
            )
        )


def test_clarity_runtime_provenance_enforces_passed_outcome_sections() -> None:
    for field_name in (
        "source",
        "draft",
        "qualification",
        "final",
        "runtime_recipe",
        "verification",
    ):
        payload = _valid_payload()
        payload[field_name] = None
        with pytest.raises(ValueError):
            ClarityRuntimeProvenanceV1.model_validate(payload)

    payload = _valid_payload()
    payload["error"] = {"phase": "verification", "code": "verification_failed"}
    with pytest.raises(ValueError):
        ClarityRuntimeProvenanceV1.model_validate(payload)


def test_clarity_runtime_provenance_accepts_strict_no_profile_outcome() -> None:
    envelope = ClarityRuntimeProvenanceV1.model_validate(_no_profile_payload())

    assert envelope.outcome == "no_profile_passed"
    assert envelope.qualification is not None
    assert envelope.qualification.selected_profile_id is None
    assert envelope.final is None
    assert envelope.runtime_recipe is None
    assert envelope.verification is None


def test_clarity_runtime_provenance_no_profile_forbids_final_sections() -> None:
    payload = _no_profile_payload()
    payload["final"] = _valid_payload()["final"]
    _reseal_payload(payload)

    with pytest.raises(ValueError, match="forbids final"):
        ClarityRuntimeProvenanceV1.model_validate(payload)


@pytest.mark.parametrize("outcome", ["cancelled", "error"])
def test_clarity_runtime_provenance_partial_outcome_requires_stable_error(
    outcome: str,
) -> None:
    payload = _partial_error_payload(outcome)
    payload["error"] = None
    _reseal_payload(payload)

    with pytest.raises(ValueError, match="stable error"):
        ClarityRuntimeProvenanceV1.model_validate(payload)


@pytest.mark.parametrize("outcome", ["cancelled", "error"])
def test_clarity_runtime_provenance_partial_outcome_rejects_missing_event_section(
    outcome: str,
) -> None:
    payload = _partial_error_payload(outcome)
    payload["draft"] = None
    _reseal_payload(payload)

    with pytest.raises(ValueError, match="draft_bound"):
        ClarityRuntimeProvenanceV1.model_validate(payload)


def test_source_integrity_event_rejects_null_source() -> None:
    payload = _source_integrity_error_payload(None)

    with pytest.raises(ValueError, match="source integrity"):
        ClarityRuntimeProvenanceV1.model_validate(payload)


def test_source_integrity_event_rejects_hash_drift() -> None:
    payload = _source_integrity_error_payload(
        {
            "sha256_before": _digest("source-before"),
            "sha256_after": _digest("source-after"),
            "size_bytes": 4096,
        }
    )

    with pytest.raises(ValueError, match="source integrity"):
        ClarityRuntimeProvenanceV1.model_validate(payload)


def test_source_integrity_event_accepts_matching_hashes() -> None:
    source_digest = _digest("unchanged-source")
    payload = _source_integrity_error_payload(
        {
            "sha256_before": source_digest,
            "sha256_after": source_digest,
            "size_bytes": 4096,
        }
    )

    envelope = ClarityRuntimeProvenanceV1.model_validate(payload)

    assert envelope.source is not None
    assert envelope.source.sha256_before == envelope.source.sha256_after


@pytest.mark.parametrize(
    ("unsafe_class", "mutation"),
    [
        (
            "username",
            lambda payload: payload["component_manifest"][0].__setitem__(
                "module", "alice"
            ),
        ),
        (
            "stderr",
            lambda payload: payload["tools"][0].__setitem__(
                "reported_version_line", "ffmpeg: Invalid argument"
            ),
        ),
        (
            "exception-text",
            lambda payload: payload["component_manifest"][0].__setitem__(
                "qualname", "RuntimeError: qualification failed"
            ),
        ),
        (
            "python-object-address",
            lambda payload: payload["component_manifest"][0].__setitem__(
                "qualname", "<object object at 0x000001ABCDEF1234>"
            ),
        ),
        (
            "temporary-filename",
            lambda payload: payload["draft"].__setitem__("action_id", "tmpa8f1c2.json"),
        ),
    ],
)
def test_clarity_runtime_provenance_rejects_unsafe_non_path_string_classes(
    unsafe_class: str,
    mutation: Callable[[dict[str, Any]], None],
) -> None:
    del unsafe_class
    payload = _valid_payload()
    mutation(payload)
    _reseal_payload(payload)

    with pytest.raises(ValueError):
        ClarityRuntimeProvenanceV1.model_validate(payload)


def test_clarity_runtime_event_rejects_temporary_filename_component() -> None:
    event: dict[str, object] = {
        "sequence": 0,
        "phase": "draft_bound",
        "component": "tmpa8f1c2.tmp",
        "outcome": "verified",
        "stable_input_digest": _digest("draft-input"),
        "stable_output_digest": _digest("draft-output"),
        "previous_event_sha256": None,
    }
    event["event_sha256"] = _independent_digest(event)

    with pytest.raises(ValueError, match="component"):
        ClarityRuntimeEventV1.model_validate(event)


@pytest.mark.parametrize(
    ("role", "reported_version_line"),
    [
        (
            "ffmpeg",
            "ffmpeg version 8.1.2-full_build-www.gyan.dev Copyright (c) "
            "2000-2026 the FFmpeg developers",
        ),
        (
            "ffprobe",
            "ffprobe version 8.1.2 Copyright (c) 2007-2026 the FFmpeg developers",
        ),
    ],
)
def test_clarity_runtime_provenance_accepts_production_shaped_tool_version_lines(
    role: str,
    reported_version_line: str,
) -> None:
    tool = ClarityToolIdentityV1.model_validate(
        {
            "role": role,
            "binary_sha256": _digest(f"{role}-binary"),
            "reported_version_line": reported_version_line,
            "version_stdout_sha256": _digest(f"{role}-version-stdout"),
            "semantic_version": "8.1.2",
        }
    )

    assert tool.reported_version_line == reported_version_line


def _winerror_5() -> PermissionError:
    error = PermissionError("injected Windows sharing violation")
    error.winerror = 5  # type: ignore[attr-defined]
    return error


def test_clarity_windows_no_replace_rename_retries_then_promotes_complete_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partial.bin"
    target = tmp_path / "final.bin"
    source.write_bytes(b"complete-provenance")
    attempts = 0
    delays: list[float] = []

    def rename(observed_source: Path, observed_target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise _winerror_5()
        os.rename(observed_source, observed_target)

    provenance_module._retry_windows_no_replace_rename(
        source,
        target,
        rename=rename,
        sleep=delays.append,
    )

    assert attempts == 2
    assert delays == [0.01]
    assert target.read_bytes() == b"complete-provenance"
    assert not source.exists()


def test_clarity_windows_no_replace_rename_surfaces_exhausted_sharing_violation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partial.bin"
    target = tmp_path / "final.bin"
    source.write_bytes(b"complete-provenance")
    attempts = 0
    delays: list[float] = []
    final_error = _winerror_5()

    def rename(_source: Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise final_error

    with pytest.raises(PermissionError) as caught:
        provenance_module._retry_windows_no_replace_rename(
            source,
            target,
            rename=rename,
            sleep=delays.append,
        )

    assert caught.value is final_error
    assert attempts == 6
    assert delays == [0.01, 0.02, 0.04, 0.08, 0.16]
    assert source.read_bytes() == b"complete-provenance"
    assert not target.exists()


def test_clarity_windows_no_replace_rename_stops_when_target_appears(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partial.bin"
    target = tmp_path / "final.bin"
    source.write_bytes(b"complete-provenance")
    attempts = 0
    delays: list[float] = []
    error = _winerror_5()

    def rename(_source: Path, observed_target: Path) -> None:
        nonlocal attempts
        attempts += 1
        observed_target.write_bytes(b"race-winner")
        raise error

    with pytest.raises(PermissionError) as caught:
        provenance_module._retry_windows_no_replace_rename(
            source,
            target,
            rename=rename,
            sleep=delays.append,
        )

    assert caught.value is error
    assert attempts == 1
    assert delays == []
    assert source.read_bytes() == b"complete-provenance"
    assert target.read_bytes() == b"race-winner"


def test_clarity_windows_no_replace_rename_stops_when_source_disappears(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partial.bin"
    target = tmp_path / "final.bin"
    source.write_bytes(b"complete-provenance")
    attempts = 0
    delays: list[float] = []
    error = _winerror_5()

    def rename(observed_source: Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        observed_source.unlink()
        raise error

    with pytest.raises(PermissionError) as caught:
        provenance_module._retry_windows_no_replace_rename(
            source,
            target,
            rename=rename,
            sleep=delays.append,
        )

    assert caught.value is error
    assert attempts == 1
    assert delays == []
    assert not source.exists()
    assert not target.exists()


def test_clarity_windows_no_replace_rename_does_not_retry_other_os_errors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "partial.bin"
    target = tmp_path / "final.bin"
    source.write_bytes(b"complete-provenance")
    attempts = 0
    delays: list[float] = []
    error = OSError("injected non-Windows error")
    error.winerror = 32  # type: ignore[attr-defined]

    def rename(_source: Path, _target: Path) -> None:
        nonlocal attempts
        attempts += 1
        raise error

    with pytest.raises(OSError) as caught:
        provenance_module._retry_windows_no_replace_rename(
            source,
            target,
            rename=rename,
            sleep=delays.append,
        )

    assert caught.value is error
    assert attempts == 1
    assert delays == []
    assert source.read_bytes() == b"complete-provenance"
    assert not target.exists()


def test_clarity_windows_atomic_promote_preserves_preexisting_final_bytes(
    tmp_path: Path,
) -> None:
    if os.name != "nt":
        return
    partial = tmp_path / "clarity-runtime-provenance.json.partial"
    final = tmp_path / "clarity-runtime-provenance.json"
    partial.write_bytes(b"complete-provenance")
    final.write_bytes(b"race-winner")

    with pytest.raises(FileExistsError):
        provenance_module._atomic_promote(partial, final)

    assert partial.read_bytes() == b"complete-provenance"
    assert final.read_bytes() == b"race-winner"


def test_clarity_runtime_provenance_no_clobber_preserves_existing_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "existing directory"
    root.mkdir()
    sentinel = root / "sentinel.bin"
    sentinel.write_bytes(b"keep-directory")

    with pytest.raises(FileExistsError):
        write_clarity_runtime_provenance(root, _valid_passed_envelope())

    assert sentinel.read_bytes() == b"keep-directory"


def test_clarity_runtime_provenance_no_clobber_preserves_existing_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / "existing-file"
    root.write_bytes(b"keep-file")

    with pytest.raises(FileExistsError):
        write_clarity_runtime_provenance(root, _valid_passed_envelope())

    assert root.read_bytes() == b"keep-file"


def test_clarity_runtime_provenance_no_clobber_preserves_symlink_like_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"keep-external")
    root = tmp_path / "audit-link"
    real_link = True
    try:
        os.symlink(external, root, target_is_directory=True)
    except (NotImplementedError, OSError):
        real_link = False
        original = provenance_module._path_exists_no_follow
        monkeypatch.setattr(
            provenance_module,
            "_path_exists_no_follow",
            lambda path: path == root or original(path),
        )

    with pytest.raises(FileExistsError):
        write_clarity_runtime_provenance(root, _valid_passed_envelope())

    assert sentinel.read_bytes() == b"keep-external"
    if real_link:
        assert root.is_symlink()


def test_clarity_runtime_provenance_ownership_cleans_partial_file_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "partial-collision"
    original = provenance_module._write_exclusive

    def collide(path: Path, payload: bytes) -> None:
        path.write_bytes(b"racing-writer")
        original(path, payload)

    monkeypatch.setattr(provenance_module, "_write_exclusive", collide)

    with pytest.raises(FileExistsError):
        write_clarity_runtime_provenance(root, _valid_passed_envelope())

    assert not root.exists()


def test_clarity_runtime_provenance_promotion_race_preserves_winner_and_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "promotion-race"
    envelope = _valid_passed_envelope()
    expected_partial = canonical_provenance_bytes(envelope)
    collision = FileExistsError("injected promotion collision")

    def lose_promotion_race(partial: Path, final: Path) -> None:
        final.write_bytes(b"race-winner")
        assert partial.read_bytes() == expected_partial
        raise collision

    monkeypatch.setattr(provenance_module, "_atomic_promote", lose_promotion_race)

    with pytest.raises(FileExistsError) as caught:
        write_clarity_runtime_provenance(root, envelope)

    assert caught.value is collision
    assert (root / "clarity-runtime-provenance.json").read_bytes() == b"race-winner"
    assert (
        root / "clarity-runtime-provenance.json.partial"
    ).read_bytes() == expected_partial


@pytest.mark.parametrize(
    ("failure_point", "root_retained"),
    [("write", False), ("promotion", True), ("readback", False)],
)
def test_clarity_runtime_provenance_ownership_preserves_promotion_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
    root_retained: bool,
) -> None:
    root = tmp_path / f"owned-{failure_point}"
    external = tmp_path / "external-sentinel.bin"
    external.write_bytes(b"keep-external")

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError(f"injected {failure_point} failure")

    if failure_point == "write":
        monkeypatch.setattr(provenance_module, "_write_exclusive", fail)
    elif failure_point == "promotion":
        monkeypatch.setattr(provenance_module, "_atomic_promote", fail)
    else:
        monkeypatch.setattr(provenance_module, "read_clarity_runtime_provenance", fail)

    with pytest.raises(OSError, match=failure_point):
        write_clarity_runtime_provenance(root, _valid_passed_envelope())

    if root_retained:
        assert (root / "clarity-runtime-provenance.json.partial").is_file()
    else:
        assert not root.exists()
    assert external.read_bytes() == b"keep-external"


def test_clarity_runtime_provenance_ownership_surfaces_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "owned-cleanup-failure"
    external = tmp_path / "external-sentinel.bin"
    external.write_bytes(b"keep-external")

    def fail_readback(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected readback failure")

    def fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(
        provenance_module, "read_clarity_runtime_provenance", fail_readback
    )
    monkeypatch.setattr(provenance_module, "_remove_owned_root", fail_cleanup)

    with pytest.raises(RuntimeError, match="cleanup") as caught:
        write_clarity_runtime_provenance(root, _valid_passed_envelope())

    assert isinstance(caught.value.__cause__, OSError)
    assert "cleanup" in str(caught.value.__cause__)
    assert (root / "clarity-runtime-provenance.json").is_file()
    assert external.read_bytes() == b"keep-external"


def test_clarity_runtime_provenance_no_clobber_rejects_lexical_path_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "owner" / ".." / "escaped"

    with pytest.raises(ValueError, match="escape"):
        write_clarity_runtime_provenance(root, _valid_passed_envelope())

    assert not (tmp_path / "escaped").exists()


def test_clarity_runtime_provenance_writer_handles_unicode_and_spaces(
    tmp_path: Path,
) -> None:
    root = tmp_path / "审计 evidence"

    path = write_clarity_runtime_provenance(root, _valid_passed_envelope())

    assert path.is_file()
    assert not (root / "clarity-runtime-provenance.json.partial").exists()


def test_clarity_runtime_provenance_read_rejects_noncanonical_bytes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "clarity-runtime-provenance.json"
    path.write_text(
        json.dumps(_valid_payload(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(ValueError, match="canonical"):
        read_clarity_runtime_provenance(path)


def _observed_component(value: object) -> object:
    return value


def _second_observed_component(value: object) -> object:
    return value


class _ObservedBase:
    def observed(self, value: object) -> object:
        return value


class _ObservedOverride(_ObservedBase):
    def observed(self, value: object) -> object:
        return value


class _ObservedInherited(_ObservedBase):
    pass


def test_observer_accepts_exact_code_object_and_return_identity() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    expected = object()
    observer.start()
    try:
        returned = _observed_component(expected)
        observer.require_intact()
    finally:
        observer.stop()
    observed = observer.require_return("observed", returned)
    assert observed.value is returned
    assert observed.object_id == id(returned)


def test_observer_rejects_same_name_fake() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )

    def _same_name(value: object) -> object:
        return value

    expected = object()
    observer.start()
    try:
        _same_name(expected)
    finally:
        observer.stop()
    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", expected)


def test_observer_rejects_equal_but_distinct_code_object() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    cloned_code = _observed_component.__code__.replace()
    assert cloned_code == _observed_component.__code__
    assert cloned_code is not _observed_component.__code__
    equal_clone = FunctionType(
        cloned_code,
        _observed_component.__globals__,
        _observed_component.__name__,
    )
    expected = object()
    observer.start()
    try:
        returned = equal_clone(expected)
    finally:
        observer.stop()

    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", returned)


def test_observer_does_not_record_dead_branch() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    expected = object()
    observer.start()
    try:
        if False:
            _observed_component(expected)
    finally:
        observer.stop()
    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", expected)


def test_observer_rejects_disabled_profile_hook() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    observer.start()
    sys.setprofile(None)
    try:
        with pytest.raises(ValueError, match="observer was replaced"):
            observer.require_intact()
    finally:
        observer.stop()


def test_observer_rejects_subclass_override_code_object() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _ObservedBase.observed),)
    )
    expected = object()
    observer.start()
    try:
        returned = _ObservedOverride().observed(expected)
    finally:
        observer.stop()

    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", returned)


def test_observer_rejects_subclass_receiver_using_registered_base_code() -> None:
    observer = ClarityRuntimeObserver(
        (
            production_component(
                "observed",
                _ObservedBase.observed,
                receiver_type=_ObservedBase,
            ),
        )
    )
    observer.start()
    try:
        returned = _ObservedInherited().observed(object())
    finally:
        observer.stop()

    del returned
    with pytest.raises(ValueError, match="receiver type"):
        observer.require_intact()


def test_observer_rejects_monkeypatched_method_after_registry_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    component = production_component("observed", _ObservedBase.observed)

    def replacement(self: _ObservedBase, value: object) -> object:
        del self
        return value

    monkeypatch.setattr(_ObservedBase, "observed", replacement)
    observer = ClarityRuntimeObserver((component,))
    expected = object()
    observer.start()
    try:
        returned = _ObservedBase().observed(expected)
    finally:
        observer.stop()

    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", returned)


def test_observer_does_not_record_code_after_early_return() -> None:
    def selector(value: object) -> object:
        return value
        _observed_component(value)

    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    expected = object()
    observer.start()
    try:
        returned = selector(expected)
    finally:
        observer.stop()

    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", returned)


def test_observer_chains_and_restores_previous_profile_hook() -> None:
    original = sys.getprofile()
    previous_events: list[tuple[str, object]] = []

    def previous_hook(frame: Any, event: str, arg: object) -> None:
        del arg
        if frame.f_code is _observed_component.__code__:
            previous_events.append((event, frame.f_code))

    sys.setprofile(previous_hook)
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    expected = object()
    try:
        observer.start()
        assert _observed_component(expected) is expected
        observer.stop()
        assert sys.getprofile() is previous_hook
        assert ("call", _observed_component.__code__) in previous_events
        assert ("return", _observed_component.__code__) in previous_events
    finally:
        observer.stop()
        sys.setprofile(original)


@pytest.mark.parametrize(
    ("components", "message"),
    [
        (
            (
                production_component("observed", _observed_component),
                production_component("observed", _second_observed_component),
            ),
            "duplicate component name",
        ),
        (
            (
                production_component("first", _observed_component),
                production_component("second", _observed_component),
            ),
            "duplicate code object",
        ),
    ],
)
def test_observer_rejects_duplicate_registry_identity(
    components: tuple[ProductionComponentSpec, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        ClarityRuntimeObserver(components)


def test_observer_rejects_duplicate_or_reordered_returns() -> None:
    components = (
        production_component("first", _observed_component),
        production_component("second", _second_observed_component),
    )
    duplicate = ClarityRuntimeObserver(components)
    duplicate.start()
    try:
        _observed_component(object())
        _observed_component(object())
        with pytest.raises(ValueError, match="return sequence"):
            duplicate.require_intact()
    finally:
        duplicate.stop()

    reordered = ClarityRuntimeObserver(components)
    reordered.start()
    try:
        _second_observed_component(object())
        with pytest.raises(ValueError, match="return sequence"):
            reordered.require_intact()
    finally:
        reordered.stop()


def test_observer_rejects_return_object_substitution() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    observer.start()
    try:
        _observed_component(object())
    finally:
        observer.stop()

    with pytest.raises(ValueError, match="return identity mismatch"):
        observer.require_return("observed", object())


def test_observer_rejects_cross_thread_component_return() -> None:
    observer = ClarityRuntimeObserver(
        (production_component("observed", _observed_component),)
    )
    expected = object()
    returned: list[object] = []
    observer.start()
    try:
        worker = threading.Thread(
            target=lambda: returned.append(_observed_component(expected))
        )
        worker.start()
        worker.join(timeout=5.0)
        assert not worker.is_alive()
    finally:
        observer.stop()

    assert returned == [expected]
    with pytest.raises(ValueError, match="return is missing"):
        observer.require_return("observed", expected)


def test_code_object_registry_retains_exact_production_identity() -> None:
    expected_names = (
        "build_rescue_plan",
        "qualify",
        "execute_faithful",
        "execute_improved_with_controls",
        "verify",
        "cleanup_controls",
    )

    assert tuple(component.name for component in DEFAULT_COMPONENTS) == expected_names
    assert DEFAULT_COMPONENTS[0].expected_return_count == 2
    assert all("/" not in component.source_sha256 for component in DEFAULT_COMPONENTS)
    assert all("\\" not in component.source_sha256 for component in DEFAULT_COMPONENTS)


def test_observer_suppresses_qualifier_nested_executor_and_keeps_seven_milestones(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = ClarityRuntimeGuard(tmp_path / "nested lifecycle provenance")
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "nested lifecycle",
            monkeypatch,
        )

        guard.observer._require_complete()
    finally:
        guard.observer.stop()

    observed = guard.observer.observed_returns
    assert tuple(item.component for item in observed) == (
        "build_rescue_plan",
        "qualify",
        "build_rescue_plan",
        "execute_faithful",
        "execute_improved_with_controls",
        "verify",
        "cleanup_controls",
    )
    assert tuple(item.value for item in observed) == (
        case.draft,
        case.evidence,
        case.final,
        case.faithful,
        case.improved,
        case.report,
        None,
    )


def test_observer_retains_exact_live_receivers_and_call_arguments_in_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guard = ClarityRuntimeGuard(tmp_path / "live call capture provenance")
    guard.start()
    try:
        case, _inputs, ffmpeg, ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "live call capture",
            monkeypatch,
        )
        guard.observer._require_complete()
    finally:
        guard.observer.stop()

    observed = guard.observer.observed_returns
    qualifier_return = observed[1]
    faithful_return = observed[3]
    improved_return = observed[4]
    verifier_return = observed[5]
    cleanup_return = observed[6]
    qualifier_arguments = dict(qualifier_return.arguments)
    faithful_arguments = dict(faithful_return.arguments)
    improved_arguments = dict(improved_return.arguments)
    verifier_arguments = dict(verifier_return.arguments)
    cleanup_arguments = dict(cleanup_return.arguments)

    assert type(qualifier_return.receiver) is NativeRescueCandidateQualifier
    assert type(faithful_return.receiver) is NativeRescueExecutor
    assert faithful_return.receiver is improved_return.receiver
    assert type(verifier_return.receiver) is RescueVerifier
    assert qualifier_arguments["draft_plan"] is case.draft
    assert qualifier_arguments["source"] == case.source
    assert qualifier_arguments["work_root"] == case.qualification_root
    assert faithful_arguments["plan"] is case.final
    assert faithful_arguments["source"] == case.source
    assert faithful_arguments["work_root"] == case.execution_root
    assert improved_arguments["plan"] is case.final
    assert improved_arguments["faithful"] == case.faithful.output_path
    assert improved_arguments["work_root"] == case.execution_root
    assert verifier_arguments["plan"] is case.final
    assert verifier_arguments["source"] == case.source
    assert cleanup_arguments["private_root"] == case.execution_root
    assert cleanup_arguments["handles"] is case.controls
    qualifier = qualifier_return.receiver
    executor = cast(NativeRescueExecutor, faithful_return.receiver)
    verifier = verifier_return.receiver
    assert qualifier._executor is executor
    assert type(qualifier._measurement_provider) is NativeMediaMeasurementProvider
    assert executor._ffmpeg == str(ffmpeg)
    assert executor._ffprobe == str(ffprobe)
    assert qualifier._measurement_provider._ffmpeg == str(ffmpeg)
    assert qualifier._measurement_provider._ffprobe == str(ffprobe)
    assert verifier._measurement_provider is qualifier._measurement_provider


def _set_call_report(
    item: pytest.Item,
    *,
    outcome: Literal["passed", "failed", "skipped"],
    exception_type: type[BaseException] | None,
) -> None:
    item.stash[CLARITY_CALL_REPORT_KEY] = ClarityPytestCallReport(
        outcome=outcome,
        exception_type=exception_type,
    )


def test_finalizer_rejects_missing_terminal_seal_and_restores_hook(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "missing-seal" / "clarity-runtime-provenance"
    root.parent.mkdir()
    before = sys.getprofile()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    _set_call_report(
        request.node,
        outcome="passed",
        exception_type=None,
    )

    with pytest.raises(ValueError, match="terminal seal is missing"):
        guard.finalize_from_pytest_item(request.node)

    assert sys.getprofile() is before
    partial = read_clarity_runtime_provenance(root / "clarity-runtime-provenance.json")
    assert partial.outcome == "error"
    assert partial.error is not None
    assert partial.error.code == "missing_terminal_seal"


def test_guard_rejects_second_terminal_persistence_after_strict_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "duplicate-seal" / "clarity-runtime-provenance"
    root.parent.mkdir()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "duplicate-seal lifecycle",
            monkeypatch,
            guard=guard,
        )
        first = guard.seal_success(**case.success_kwargs())  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="already recorded"):
            guard.seal_success(**case.success_kwargs())  # type: ignore[arg-type]
    finally:
        guard.observer.stop()

    retained = read_clarity_runtime_provenance(root / "clarity-runtime-provenance.json")
    assert retained == first


def test_finalizer_rejects_public_counter_bypass_without_persisted_readback_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "counter-bypass" / "clarity-runtime-provenance"
    root.parent.mkdir()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    try:
        _run_pure_production_lifecycle(
            tmp_path / "counter-bypass lifecycle",
            monkeypatch,
        )
        bypass = getattr(guard, "record_terminal_seal", lambda: None)
        bypass()
        _set_call_report(request.node, outcome="passed", exception_type=None)

        with pytest.raises(ValueError, match="terminal seal is missing"):
            guard.finalize_from_pytest_item(request.node)
    finally:
        guard.observer.stop()

    partial = read_clarity_runtime_provenance(root / "clarity-runtime-provenance.json")
    assert partial.outcome == "error"
    assert partial.error is not None
    assert partial.error.code == "missing_terminal_seal"


def test_finalizer_rejects_prebuilt_envelope_without_stored_path_and_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "prebuilt" / "clarity-runtime-provenance"
    root.parent.mkdir()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    try:
        _run_pure_production_lifecycle(
            tmp_path / "prebuilt lifecycle",
            monkeypatch,
        )
        prebuilt = _valid_passed_envelope()
        path = write_clarity_runtime_provenance(root, prebuilt)
        _set_call_report(request.node, outcome="passed", exception_type=None)

        with pytest.raises(ValueError, match="terminal seal is missing"):
            guard.finalize_from_pytest_item(request.node)
    finally:
        guard.observer.stop()

    assert read_clarity_runtime_provenance(path) == prebuilt


def test_finalizer_persists_sanitized_partial_error_and_restores_hook(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "failed-call" / "clarity-runtime-provenance"
    root.parent.mkdir()
    before = sys.getprofile()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    _set_call_report(
        request.node,
        outcome="failed",
        exception_type=RuntimeError,
    )

    guard.finalize_from_pytest_item(request.node)

    assert sys.getprofile() is before
    path = root / "clarity-runtime-provenance.json"
    partial = read_clarity_runtime_provenance(path)
    assert partial.outcome == "error"
    assert partial.error is not None
    assert partial.error.phase == "selector_call"
    assert partial.error.code == "pytest_call_failed"
    assert "RuntimeError" not in path.read_text(encoding="utf-8")


def test_finalizer_partial_error_preserves_valid_live_qualification_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "qualification-prefix" / "clarity-runtime-provenance"
    root.parent.mkdir()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "qualification-prefix lifecycle",
            monkeypatch,
            passing_profile=False,
            guard=guard,
        )
        _set_call_report(
            request.node,
            outcome="failed",
            exception_type=RuntimeError,
        )

        guard.finalize_from_pytest_item(request.node)
    finally:
        guard.observer.stop()

    path = root / "clarity-runtime-provenance.json"
    partial = read_clarity_runtime_provenance(path)
    assert partial.outcome == "error"
    assert {tool.role for tool in partial.tools} == {"ffmpeg", "ffprobe"}
    assert partial.source is not None
    assert partial.source.sha256_before == case.source_hash
    assert partial.draft is not None
    assert partial.draft.plan_digest == case.draft.plan_digest
    assert partial.qualification is not None
    assert partial.qualification.evidence_digest == provenance_digest(case.evidence)
    assert partial.final is None
    assert partial.runtime_recipe is None
    assert partial.verification is None
    assert tuple(event.phase for event in partial.events) == (
        "tool_identity_verified",
        "draft_bound",
        "qualification_returned",
        "qualification_cleanup_verified",
        "source_integrity_verified",
    )
    assert partial.error is not None
    assert partial.error.code == "pytest_call_failed"
    retained = path.read_text(encoding="utf-8")
    assert "RuntimeError" not in retained
    assert str(case.source) not in retained


def test_finalizer_rejects_replaced_observer_with_sanitized_partial_error(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "replaced-observer" / "clarity-runtime-provenance"
    root.parent.mkdir()
    before = sys.getprofile()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    _set_call_report(
        request.node,
        outcome="passed",
        exception_type=None,
    )
    sys.setprofile(None)

    with pytest.raises(ValueError, match="observer was replaced"):
        guard.finalize_from_pytest_item(request.node)

    assert sys.getprofile() is before
    path = root / "clarity-runtime-provenance.json"
    partial = read_clarity_runtime_provenance(path)
    assert partial.outcome == "error"
    assert partial.error is not None
    assert partial.error.phase == "observer"
    assert partial.error.code == "observer_replaced"
    assert "clarity runtime observer was replaced" not in path.read_text(
        encoding="utf-8"
    )


def test_finalizer_preserves_rescue_cancelled_behavior_and_sanitizes_error(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "cancelled-call" / "clarity-runtime-provenance"
    root.parent.mkdir()
    before = sys.getprofile()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    _set_call_report(
        request.node,
        outcome="failed",
        exception_type=RescueCancelledError,
    )

    guard.finalize_from_pytest_item(request.node)

    assert sys.getprofile() is before
    path = root / "clarity-runtime-provenance.json"
    partial = read_clarity_runtime_provenance(path)
    assert partial.outcome == "cancelled"
    assert partial.error is not None
    assert partial.error.phase == "selector_call"
    assert partial.error.code == "pytest_call_cancelled"
    assert "RescueCancelledError" not in path.read_text(encoding="utf-8")


def test_finalizer_treats_non_rescue_cancellation_as_sanitized_error(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "non-rescue-cancelled-call" / "clarity-runtime-provenance"
    root.parent.mkdir()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    _set_call_report(
        request.node,
        outcome="failed",
        exception_type=KeyboardInterrupt,
    )

    guard.finalize_from_pytest_item(request.node)

    path = root / "clarity-runtime-provenance.json"
    partial = read_clarity_runtime_provenance(path)
    assert partial.outcome == "error"
    assert partial.error is not None
    assert partial.error.code == "pytest_call_failed"
    assert "KeyboardInterrupt" not in path.read_text(encoding="utf-8")


def test_finalizer_rejects_unrelated_same_named_cancellation_type(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "same-named-cancellation" / "clarity-runtime-provenance"
    root.parent.mkdir()
    unrelated = cast(
        type[BaseException],
        type("RescueCancelledError", (Exception,), {}),
    )
    guard = ClarityRuntimeGuard(root)
    guard.start()
    _set_call_report(
        request.node,
        outcome="failed",
        exception_type=unrelated,
    )

    guard.finalize_from_pytest_item(request.node)

    path = root / "clarity-runtime-provenance.json"
    partial = read_clarity_runtime_provenance(path)
    assert partial.outcome == "error"
    assert partial.error is not None
    assert partial.error.code == "pytest_call_failed"
    assert "RescueCancelledError" not in path.read_text(encoding="utf-8")


def test_finalizer_rejects_incomplete_runtime_sequence_with_sanitized_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    root = tmp_path / "incomplete-sequence" / "clarity-runtime-provenance"
    root.parent.mkdir()
    before = sys.getprofile()
    guard = ClarityRuntimeGuard(root)
    guard.start()
    inputs = _pure_clarity_inputs(tmp_path / "incomplete-sequence lifecycle")
    ffmpeg = tmp_path / "incomplete-ffmpeg.exe"
    ffprobe = tmp_path / "incomplete-ffprobe.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")

    def fake_tool_verifier(
        path: Path,
        role: Literal["ffmpeg", "ffprobe"],
    ) -> ClarityToolIdentityV1:
        return verify_clarity_tool_identity(
            path,
            role,
            runner=_FakeToolRunner(stdout=f"{role} version 8.1.2\n"),
        )

    monkeypatch.setattr(
        provenance_module,
        "verify_clarity_tool_identity",
        fake_tool_verifier,
    )
    guard.bind_tools(ffmpeg, ffprobe)
    guard.bind_source_before(inputs.source, inputs.source_hash)
    draft = build_rescue_plan(**inputs.planner_inputs)
    _set_call_report(
        request.node,
        outcome="passed",
        exception_type=None,
    )

    with pytest.raises(ValueError, match="terminal seal is missing"):
        guard.finalize_from_pytest_item(request.node)

    assert sys.getprofile() is before
    path = root / "clarity-runtime-provenance.json"
    partial = read_clarity_runtime_provenance(path)
    assert partial.outcome == "error"
    assert partial.error is not None
    assert partial.error.phase == "selector_finalize"
    assert partial.error.code == "missing_terminal_seal"
    assert partial.draft is not None
    assert partial.draft.plan_digest == draft.plan_digest


def test_non_exact_node_fixture_has_no_profile_hook_or_audit_root(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    before = sys.getprofile()

    fixture_value = request.getfixturevalue("clarity_runtime_provenance_guard")

    assert request.node.nodeid != EXACT_CLARITY_NODE_ID
    assert fixture_value is None
    assert sys.getprofile() is before
    assert not (tmp_path / "clarity-runtime-provenance").exists()


@dataclass
class _FakeToolRunner:
    stdout: str
    returncode: int = 0
    stderr: str = ""
    error: BaseException | None = None

    def __post_init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, arguments: list[str], **options: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((arguments, options))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(
            arguments,
            self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class _FakeVersionProcess:
    def __init__(self, stdout: bytes, stderr: bytes) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = 0
        self.wait_timeouts: list[float | None] = []
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def test_tool_identity_default_streams_both_outputs_through_bounded_fake_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binary = tmp_path / "fixed ffmpeg 中文.exe"
    binary.write_bytes(b"fixed-ffmpeg")
    process = _FakeVersionProcess(
        b"ffmpeg version 8.1.2\n" + b"x" * (64 * 1024),
        b"private stderr" * (64 * 1024),
    )
    popen_calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(
        arguments: list[str],
        **options: object,
    ) -> _FakeVersionProcess:
        popen_calls.append((arguments, options))
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unbounded subprocess.run must not be used")
        ),
    )

    with pytest.raises(ValueError, match="stdout exceeds the bounded limit"):
        verify_clarity_tool_identity(binary, "ffmpeg")

    assert len(popen_calls) == 1
    arguments, options = popen_calls[0]
    assert arguments == [str(binary), "-version"]
    assert options == {
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    assert process.wait_timeouts == [5.0]
    assert not process.killed


@pytest.mark.parametrize("role", ["ffmpeg", "ffprobe"])
def test_tool_identity_uses_one_bounded_fake_runner_call_and_hashes_complete_stdout(
    tmp_path: Path,
    role: Literal["ffmpeg", "ffprobe"],
) -> None:
    binary = tmp_path / f"fixed {role} 中文.exe"
    binary_bytes = f"{role}-8.1.2-binary".encode()
    binary.write_bytes(binary_bytes)
    stdout = (
        f"  {role}   version  8.1.2-full_build-www.gyan.dev  "
        "Copyright (c) 2000-2026 the FFmpeg developers\r\n"
        "configuration: --disable-network\r\n"
    )
    runner = _FakeToolRunner(stdout=stdout, stderr="private stderr must disappear")

    identity = verify_clarity_tool_identity(binary, role, runner=runner)

    assert identity.role == role
    assert identity.semantic_version == "8.1.2"
    assert identity.binary_sha256 == sha256(binary_bytes).hexdigest()
    assert identity.reported_version_line == (
        f"{role} version 8.1.2-full_build-www.gyan.dev "
        "Copyright (c) 2000-2026 the FFmpeg developers"
    )
    assert identity.version_stdout_sha256 == sha256(stdout.encode()).hexdigest()
    assert len(runner.calls) == 1
    arguments, options = runner.calls[0]
    assert arguments == [str(binary), "-version"]
    assert options == {
        "capture_output": True,
        "check": False,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
        "text": True,
        "timeout": 5.0,
    }
    persisted = identity.model_dump_json()
    assert str(binary) not in persisted
    assert "private stderr" not in persisted


@pytest.mark.parametrize(
    ("case", "stdout", "returncode", "error", "message"),
    [
        (
            "missing",
            "",
            0,
            FileNotFoundError("C:/Users/private/fixed-ffmpeg.exe"),
            "unavailable",
        ),
        (
            "nonzero",
            "ffmpeg version 8.1.2\n",
            7,
            None,
            "failed",
        ),
        (
            "timeout",
            "",
            0,
            subprocess.TimeoutExpired(
                ["C:/Users/private/fixed-ffmpeg.exe", "-version"],
                5.0,
                stderr="secret stderr",
            ),
            "timed out",
        ),
        ("wrong-role", "ffprobe version 8.1.2\n", 0, None, "role"),
        ("malformed", "ffmpeg release 8.1.2\n", 0, None, "version line"),
        ("old-version", "ffmpeg version 8.1.1\n", 0, None, "8.1.2"),
        ("new-version", "ffmpeg version 8.2\n", 0, None, "8.1.2"),
        (
            "oversized",
            "ffmpeg version 8.1.2\n" + "x" * (64 * 1024),
            0,
            None,
            "bounded",
        ),
    ],
    ids=(
        "missing",
        "nonzero",
        "timeout",
        "wrong-role",
        "malformed",
        "old-version",
        "new-version",
        "oversized",
    ),
)
def test_tool_identity_fake_runner_rejects_drift_without_leaking_path_or_stderr(
    tmp_path: Path,
    case: str,
    stdout: str,
    returncode: int,
    error: BaseException | None,
    message: str,
) -> None:
    binary = tmp_path / f"{case} fixed ffmpeg.exe"
    binary.write_bytes(b"binary")
    runner = _FakeToolRunner(
        stdout=stdout,
        returncode=returncode,
        stderr=f"secret stderr at {binary}",
        error=error,
    )

    with pytest.raises((TypeError, ValueError), match=message) as caught:
        verify_clarity_tool_identity(binary, "ffmpeg", runner=runner)

    rendered = str(caught.value)
    assert str(binary) not in rendered
    assert "secret stderr" not in rendered
    assert "private" not in rendered


def test_tool_identity_missing_file_uses_injected_runner_boundary_without_real_tool(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "absent ffprobe.exe"
    runner = _FakeToolRunner(stdout="ffprobe version 8.1.2\n")

    with pytest.raises(ValueError, match="regular file"):
        verify_clarity_tool_identity(binary, "ffprobe", runner=runner)

    assert runner.calls == []


@dataclass
class _LiveClarityCase:
    source: Path
    source_hash: str
    draft: RescuePlan
    evidence: SharpenQualificationEvidenceV1
    final: RescuePlan
    faithful: RescueExecutionResult
    improved: RescueImprovedExecutionResult
    controls: tuple[SharpenVerificationControlHandle, ...]
    report: RescueVerificationReport
    qualification_root: Path
    execution_root: Path

    def success_kwargs(self) -> dict[str, object]:
        return {
            "source": self.source,
            "source_sha256_after": self.source_hash,
            "draft": self.draft,
            "evidence": self.evidence,
            "final": self.final,
            "faithful": self.faithful,
            "improved": self.improved,
            "controls": self.controls,
            "report": self.report,
            "qualification_root": self.qualification_root,
            "execution_root": self.execution_root,
        }


class _PlannerInputs(TypedDict):
    metadata: VideoMetadata
    damage_map: MediaDamageMap
    strategy: RescueStrategy
    config: RescueEffectiveConfig
    visual_assessment: VisualAssessment


@dataclass(frozen=True)
class _PureClarityInputs:
    source: Path
    source_hash: str
    planner_inputs: _PlannerInputs


def _pure_clarity_inputs(
    tmp_path: Path,
    *,
    planner_input_hash: str | None = None,
) -> _PureClarityInputs:
    tmp_path.mkdir(parents=True)
    source = tmp_path / "source input 中文.mp4"
    source.write_bytes(b"source")
    source_hash = sha256(source.read_bytes()).hexdigest()
    plan_hash = planner_input_hash or source_hash
    config = RescueEffectiveConfig()
    damage = DamageInterval(
        id=make_damage_id(
            plan_hash,
            "video:0",
            DamageKind.SOFT_DETAIL,
            0.0,
            4.0,
        ),
        stream_id="video:0",
        kind=DamageKind.SOFT_DETAIL,
        start_seconds=0.0,
        end_seconds=4.0,
    )
    return _PureClarityInputs(
        source=source,
        source_hash=source_hash,
        planner_inputs={
            "metadata": VideoMetadata(
                filename=source.name,
                container_format="mp4",
                codec="h264",
                width=320,
                height=180,
                duration_seconds=4.0,
                average_frame_rate=10.0,
                estimated_frame_count=40,
                has_audio=True,
                file_size_bytes=source.stat().st_size,
            ),
            "damage_map": MediaDamageMap(
                input_hash=plan_hash,
                duration_seconds=4.0,
                scan_coverage=((0.0, 4.0),),
                intervals=(damage,),
            ),
            "strategy": RescueStrategy.BALANCED,
            "config": config,
            "visual_assessment": VisualAssessment(
                metrics=VisualMetrics(
                    luma_p10=0.2,
                    luma_p50=0.45,
                    luma_p90=0.8,
                    low_clip_ratio=0.0,
                    high_clip_ratio=0.0,
                    noise_residual=0.005,
                    sharpness=0.001,
                ),
                recommended_actions=(RescueActionKind.SHARPEN,),
                preview_required=True,
                public_explanation="Measured soft detail supports fake qualification.",
            ),
        },
    )


def _pure_media_probe_json() -> str:
    return json.dumps(
        {
            "format": {"duration": "4.0"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "start_time": "0.0",
                    "duration": "4.0",
                    "avg_frame_rate": "10000/1000",
                    "r_frame_rate": "10000/1000",
                    "nb_frames": "40",
                },
                {
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "start_time": "0.0",
                    "duration": "4.0",
                    "sample_rate": "48000",
                },
            ],
        },
        separators=(",", ":"),
    )


@dataclass
class _PureMediaRunner:
    ffmpeg: Path
    ffprobe: Path

    def __post_init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        **options: object,
    ) -> CommandResult:
        self.calls.append((arguments, options))
        assert arguments[0] in {str(self.ffmpeg), str(self.ffprobe)}
        if arguments[0] == str(self.ffprobe):
            return CommandResult(0, "", _pure_media_probe_json())
        output = arguments[-1]
        if output != "-":
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            if "baseline" in path.name:
                payload = b"sharpen-baseline"
            elif "visibility" in path.name:
                payload = b"sharpen-visibility"
            elif "improved" in path.name or "candidate" in path.name:
                payload = b"improved"
            else:
                payload = b"faithful"
            path.write_bytes(payload)
        return CommandResult(0, "", "")


def _pure_sharpen_measurement(
    baseline: Path,
    visibility: Path,
    candidate: Path,
    *,
    passing_profile: bool,
) -> dict[str, object]:
    metrics = dict(_CLARITY_METRICS)
    if not passing_profile:
        metrics["minimum_aggregate_gain_ratio"] = 0.0
    return {
        "baseline_sha256": sha256(baseline.read_bytes()).hexdigest(),
        "control_sha256": sha256(visibility.read_bytes()).hexdigest(),
        "candidate_sha256": sha256(candidate.read_bytes()).hexdigest(),
        "normalized_pts_digest": "d" * 64,
        "baseline_normalized_pts_digest": "d" * 64,
        "control_normalized_pts_digest": "d" * 64,
        "candidate_normalized_pts_digest": "d" * 64,
        "baseline_topology_sha256": "a" * 64,
        "control_topology_sha256": "a" * 64,
        "candidate_topology_sha256": "a" * 64,
        "decoded_width": 320,
        "decoded_height": 180,
        "inventory_frame_count": 40,
        "baseline_frame_count": 40,
        "control_frame_count": 40,
        "candidate_frame_count": 40,
        **metrics,
    }


def _run_pure_production_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    passing_profile: bool = True,
    guard: ClarityRuntimeGuard | None = None,
    planner_input_hash: str | None = None,
) -> tuple[_LiveClarityCase, _PureClarityInputs, Path, Path, _PureMediaRunner]:
    inputs = _pure_clarity_inputs(
        tmp_path,
        planner_input_hash=planner_input_hash,
    )
    ffmpeg = tmp_path / "fixed ffmpeg 8.1.2.exe"
    ffprobe = tmp_path / "fixed ffprobe 8.1.2.exe"
    ffmpeg.write_bytes(b"fixed-ffmpeg-binary")
    ffprobe.write_bytes(b"fixed-ffprobe-binary")
    runner = _PureMediaRunner(ffmpeg, ffprobe)
    executor = NativeRescueExecutor(
        runner=runner,
        ffmpeg=str(ffmpeg),
        ffprobe=str(ffprobe),
        sharpen_control_inspector=lambda *_args: ("d" * 64, "a" * 64, 40),
    )
    provider = NativeMediaMeasurementProvider(
        ffmpeg=str(ffmpeg),
        ffprobe=str(ffprobe),
        command_runner=runner,
    )

    def fake_single_output(
        _self: NativeRescueExecutor,
        *,
        plan: RescuePlan,
        source: Path,
        work_root: Path,
        final_output: Path,
        partial_output: Path,
        source_range: tuple[float, float],
        stream_copy: bool,
        cancellation_callback: Callable[[], bool],
    ) -> RescueExecutionResult:
        del plan, source, partial_output, cancellation_callback
        final_output.write_bytes(b"faithful")
        start, end = source_range
        private_relative_path = final_output.relative_to(work_root).as_posix()
        segment = RescuedSegment(
            source_start=start,
            source_end=end,
            output_start=0.0,
            output_end=end - start,
            output_relative_path=private_relative_path,
        )
        return RescueExecutionResult(
            output_path=final_output,
            output_relative_path=private_relative_path,
            segments=(segment,),
            source_mappings=(segment.source_mapping,),
            render_mode="stream_copy" if stream_copy else "single_reencode",
        )

    monkeypatch.setattr(
        NativeRescueExecutor,
        "_execute_single_output",
        fake_single_output,
    )
    if planner_input_hash is not None:
        monkeypatch.setattr(
            NativeRescueExecutor,
            "_validate_source",
            staticmethod(lambda _plan, _source: None),
        )

    def fake_sharpen_measurement(
        _self: NativeMediaMeasurementProvider,
        baseline: Path,
        visibility: Path,
        candidate: Path,
        _output_ranges: tuple[tuple[float, float], ...],
        _parameters: object,
        _cancel: object,
    ) -> dict[str, object]:
        return _pure_sharpen_measurement(
            baseline,
            visibility,
            candidate,
            passing_profile=passing_profile,
        )

    def fake_measure(
        _self: NativeMediaMeasurementProvider,
        path: Path,
        relative_path: str,
        _cancel: object,
    ) -> MediaVerificationSnapshot:
        return MediaVerificationSnapshot(
            path=path,
            relative_path=relative_path,
            duration_seconds=4.0,
            video_stream_count=1,
            audio_stream_count=1,
            complete_decode=True,
            sha256=sha256(path.read_bytes()).hexdigest(),
            audio_sample_rate_hz=48000,
            sharpness=0.001,
        )

    def fake_mapped_reference(
        _self: NativeMediaMeasurementProvider,
        source: Path,
        _mappings: object,
        _render_mode: object,
        _options: object,
        _cancel: object,
    ) -> MediaVerificationSnapshot:
        return fake_measure(_self, source, "mapped-source.mp4", _cancel)

    def fake_measure_ranges(
        _self: NativeMediaMeasurementProvider,
        _path: Path,
        _ranges: object,
        _cancel: object,
    ) -> dict[str, float]:
        return {
            "luma_p10": 0.2,
            "luma_p50": 0.45,
            "clipping_ratio": 0.0,
            "noise_residual": 0.005,
            "sharpness": 0.001,
            "black_events": 0.0,
            "freeze_events": 0.0,
            "flicker_events": 0.0,
        }

    def fake_compare_ranges(
        _self: NativeMediaMeasurementProvider,
        _reference: Path,
        _candidate: Path,
        _ranges: object,
        _cancel: object,
    ) -> dict[str, float]:
        return {
            "mean_absolute_pixel_difference": 0.0,
            "p95_frame_difference": 0.0,
            "compared_frames": 40.0,
        }

    monkeypatch.setattr(
        NativeMediaMeasurementProvider,
        "measure_sharpen_qualification",
        fake_sharpen_measurement,
    )
    monkeypatch.setattr(NativeMediaMeasurementProvider, "measure", fake_measure)
    monkeypatch.setattr(
        NativeMediaMeasurementProvider,
        "measure_mapped_reference",
        fake_mapped_reference,
    )
    monkeypatch.setattr(
        NativeMediaMeasurementProvider,
        "measure_ranges",
        fake_measure_ranges,
    )
    monkeypatch.setattr(
        NativeMediaMeasurementProvider,
        "compare_ranges",
        fake_compare_ranges,
    )

    if guard is not None:

        def fake_tool_verifier(
            path: Path,
            role: Literal["ffmpeg", "ffprobe"],
        ) -> ClarityToolIdentityV1:
            return verify_clarity_tool_identity(
                path,
                role,
                runner=_FakeToolRunner(stdout=f"{role} version 8.1.2\nconfiguration\n"),
            )

        monkeypatch.setattr(
            provenance_module,
            "verify_clarity_tool_identity",
            fake_tool_verifier,
        )
        guard.bind_tools(ffmpeg, ffprobe)
        guard.bind_source_before(inputs.source, inputs.source_hash)

    draft = build_rescue_plan(**inputs.planner_inputs)
    qualification_root = tmp_path / "qualification root 中文"
    evidence = NativeRescueCandidateQualifier(
        executor=executor,
        measurement_provider=provider,
    ).qualify(draft, inputs.source, qualification_root, lambda: False)
    execution_root = tmp_path / "execution root 中文"
    if evidence.selected is None:
        final = draft
        return (
            _LiveClarityCase(
                source=inputs.source,
                source_hash=inputs.source_hash,
                draft=draft,
                evidence=evidence,
                final=final,
                faithful=RescueExecutionResult(
                    output_path=execution_root / "unused-faithful.mp4",
                    output_relative_path="unused-faithful.mp4",
                    segments=(),
                    source_mappings=(),
                ),
                improved=RescueImprovedExecutionResult(
                    output_path=execution_root / "unused-improved.mp4"
                ),
                controls=(),
                report=RescueVerificationReport(
                    plan_digest=final.plan_digest,
                    faithful_status=RescueVerificationStatus.PASSED,
                    improved_status=RescueVerificationStatus.PASSED,
                    checks=(
                        *_required_passed_checks("faithful"),
                        *_required_passed_checks("improved"),
                    ),
                    outcome=RescueOutcome.COMPLETED,
                ),
                qualification_root=qualification_root,
                execution_root=execution_root,
            ),
            inputs,
            ffmpeg,
            ffprobe,
            runner,
        )

    final = build_rescue_plan(
        **inputs.planner_inputs,
        sharpen_qualification=evidence,
        require_sharpen_qualification=True,
    )
    faithful = executor.execute_faithful(
        final,
        inputs.source,
        execution_root,
        lambda: False,
    )
    improved = executor.execute_improved_with_controls(
        final,
        faithful.output_path,
        execution_root,
        lambda: False,
        source_mappings=faithful.source_mappings,
        inherited_action_ids=faithful.applied_action_ids,
    )
    controls = cast(
        tuple[SharpenVerificationControlHandle, ...],
        tuple(improved.verification_controls),
    )
    verifier_mappings = _public_source_mappings(faithful.source_mappings)
    report = RescueVerifier(measurement_provider=provider).verify(
        inputs.source,
        faithful.output_path,
        improved.output_path,
        final,
        verifier_mappings,
        lambda: False,
        faithful_render_mode=faithful.render_mode,
        verification_controls=controls,
    )
    _cleanup_verification_controls(execution_root, controls)
    return (
        _LiveClarityCase(
            source=inputs.source,
            source_hash=inputs.source_hash,
            draft=draft,
            evidence=evidence,
            final=final,
            faithful=faithful,
            improved=improved,
            controls=controls,
            report=report,
            qualification_root=qualification_root,
            execution_root=execution_root,
        ),
        inputs,
        ffmpeg,
        ffprobe,
        runner,
    )


_CLARITY_METRICS: dict[str, int | float] = {
    "range_coverage_ratio": 1.0,
    "expected_frames": 40,
    "compared_frames": 40,
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


def _required_passed_checks(
    artifact: Literal["faithful", "improved"],
) -> tuple[RescueVerificationCheck, ...]:
    return tuple(
        RescueVerificationCheck(
            check_id=check_id,
            artifact=artifact,
            status=RescueVerificationStatus.PASSED,
            message="Controlled fake measurement passed.",
        )
        for check_id in RESCUE_REQUIRED_VERIFICATION_CHECK_IDS
    )


def test_public_verifier_mapping_projection_preserves_exact_interval_values() -> None:
    private = (
        SourceMapping(0.0, 1.0, 0.0, 1.0, "staging/faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.0, 3.0, "staging/faithful-rescue.mp4"),
    )
    public = (
        SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
    )

    retained = provenance_module._require_public_verifier_mapping_projection(
        private,
        public,
    )

    assert retained == public
    assert retained is public


@pytest.mark.parametrize(
    "public",
    (
        (SourceMapping(0.0, 1.0, 0.1, 1.1, "faithful-rescue.mp4"),),
        (
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
            SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
        ),
        (
            SourceMapping(0.0, 1.0, 0.0, 1.1, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.1, 3.1, "faithful-rescue.mp4"),
        ),
        (
            SourceMapping(0.0, 1.0, 0.0, 1.0, "staging/faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
        ),
        (SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),),
        (
            SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
            SourceMapping(4.0, 5.0, 3.0, 4.0, "faithful-rescue.mp4"),
        ),
        (
            SourceMapping(float("nan"), 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
        ),
        (
            SourceMapping(0.1, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.0, 3.0, "faithful-rescue.mp4"),
        ),
        (
            SourceMapping(0.0, 1.0, 0.0, 1.0, "faithful-rescue.mp4"),
            SourceMapping(2.0, 4.0, 1.1, 3.1, "faithful-rescue.mp4"),
        ),
    ),
)
def test_public_verifier_mapping_projection_rejects_drift(
    public: tuple[SourceMapping, ...],
) -> None:
    private = (
        SourceMapping(0.0, 1.0, 0.0, 1.0, "staging/faithful-rescue.mp4"),
        SourceMapping(2.0, 4.0, 1.0, 3.0, "staging/faithful-rescue.mp4"),
    )

    with pytest.raises(ValueError, match="public verifier mappings"):
        provenance_module._require_public_verifier_mapping_projection(
            private,
            public,
        )


def _build_live_clarity_case(
    tmp_path: Path,
    *,
    passing_profile: bool = True,
) -> _LiveClarityCase:
    tmp_path.mkdir(parents=True)
    source = tmp_path / "source input 中文.mp4"
    source.write_bytes(b"source")
    source_hash = sha256(source.read_bytes()).hexdigest()
    config = RescueEffectiveConfig()
    damage = DamageInterval(
        id=make_damage_id(
            source_hash,
            "video:0",
            DamageKind.SOFT_DETAIL,
            0.0,
            4.0,
        ),
        stream_id="video:0",
        kind=DamageKind.SOFT_DETAIL,
        start_seconds=0.0,
        end_seconds=4.0,
    )
    metadata = VideoMetadata(
        filename=source.name,
        container_format="mp4",
        codec="h264",
        width=320,
        height=180,
        duration_seconds=4.0,
        average_frame_rate=10.0,
        estimated_frame_count=40,
        has_audio=True,
        file_size_bytes=source.stat().st_size,
    )
    damage_map = MediaDamageMap(
        input_hash=source_hash,
        duration_seconds=4.0,
        scan_coverage=((0.0, 4.0),),
        intervals=(damage,),
    )
    visual_assessment = VisualAssessment(
        metrics=VisualMetrics(
            luma_p10=0.2,
            luma_p50=0.45,
            luma_p90=0.8,
            low_clip_ratio=0.0,
            high_clip_ratio=0.0,
            noise_residual=0.005,
            sharpness=0.001,
        ),
        recommended_actions=(RescueActionKind.SHARPEN,),
        preview_required=True,
        public_explanation="Measured soft detail supports fake qualification.",
    )
    draft = build_rescue_plan(
        metadata=metadata,
        damage_map=damage_map,
        strategy=RescueStrategy.BALANCED,
        config=config,
        visual_assessment=visual_assessment,
    )
    draft_action = next(
        action for action in draft.actions if action.kind is RescueActionKind.SHARPEN
    )
    thresholds = SharpenQualificationThresholdsV1(
        minimum_aggregate_gain_ratio=float(
            cast(
                float,
                draft_action.parameters["minimum_perceptible_sharpness_gain_ratio"],
            )
        ),
        minimum_recovered_baseline_ratio=float(
            cast(float, draft_action.parameters["minimum_recovered_baseline_ratio"])
        ),
        minimum_improved_frame_fraction=float(
            cast(float, draft_action.parameters["minimum_improved_frame_fraction"])
        ),
        maximum_noise_increase=float(
            cast(float, draft_action.parameters["maximum_noise_increase"])
        ),
        maximum_edge_overshoot_ratio=float(
            cast(float, draft_action.parameters["maximum_edge_overshoot_ratio"])
        ),
        maximum_edge_overshoot_amplitude=float(
            cast(
                float,
                draft_action.parameters["maximum_edge_overshoot_amplitude"],
            )
        ),
        maximum_ringing_ratio=float(
            cast(float, draft_action.parameters["maximum_ringing_ratio"])
        ),
    )
    baseline_bytes = b"sharpen-baseline"
    visibility_bytes = b"sharpen-visibility"
    improved_bytes = b"improved"
    measurements: list[SharpenProfileMeasurementV1] = []
    for index, profile in enumerate(config.sharpen_qualification_profiles):
        metrics = dict(_CLARITY_METRICS)
        if not passing_profile or index > 0:
            metrics["minimum_aggregate_gain_ratio"] = 0.0
        measurements.append(
            SharpenProfileMeasurementV1(
                profile=profile,
                baseline_sha256=sha256(baseline_bytes).hexdigest(),
                visibility_control_sha256=(
                    sha256(visibility_bytes).hexdigest()
                    if index == 0
                    else sha256(f"visibility-{index}".encode()).hexdigest()
                ),
                candidate_sha256=(
                    sha256(improved_bytes).hexdigest()
                    if index == 0
                    else sha256(f"candidate-{index}".encode()).hexdigest()
                ),
                normalized_pts_digest="d" * 64,
                stream_topology_digest="a" * 64,
                decoded_width=320,
                decoded_height=180,
                inventory_frame_count=40,
                metrics=SharpenQualificationMetricsV1.model_validate(metrics),
                thresholds=thresholds,
            )
        )
    evidence = build_sharpen_qualification_evidence(
        input_hash=source_hash,
        draft_action_id=draft_action.id,
        draft_parameters=draft_action.parameters,
        source_ranges=draft_action.source_ranges,
        output_ranges=draft_action.source_ranges,
        encode_contract=canonical_video_encode_contract(config),
        configured_profiles=config.sharpen_qualification_profiles,
        measurements=measurements,
    )
    final = build_rescue_plan(
        metadata=metadata,
        damage_map=damage_map,
        strategy=RescueStrategy.BALANCED,
        config=config,
        visual_assessment=visual_assessment,
        sharpen_qualification=evidence,
        require_sharpen_qualification=True,
    )
    execution_root = tmp_path / "execution root 中文"
    execution_root.mkdir()
    staging = execution_root / "staging"
    staging.mkdir()
    faithful_path = staging / "faithful-rescue.mp4"
    faithful_path.write_bytes(b"faithful")
    improved_path = staging / "improved-viewing.mp4"
    improved_path.write_bytes(improved_bytes)
    mapping = SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4")
    faithful = RescueExecutionResult(
        output_path=faithful_path,
        output_relative_path="faithful-rescue.mp4",
        segments=(
            RescuedSegment(
                source_start=0.0,
                source_end=4.0,
                output_start=0.0,
                output_end=4.0,
                output_relative_path="faithful-rescue.mp4",
            ),
        ),
        source_mappings=(mapping,),
        applied_action_ids=frozenset(action.id for action in final.actions),
    )
    controls: tuple[SharpenVerificationControlHandle, ...] = ()
    if evidence.selected is not None:
        selected = evidence.selected
        final_action = next(
            action
            for action in final.actions
            if action.kind is RescueActionKind.SHARPEN
        )
        baseline_path = execution_root / "sharpen-baseline.private.mp4"
        visibility_path = execution_root / "sharpen-visibility.private.mp4"
        baseline_path.write_bytes(baseline_bytes)
        visibility_path.write_bytes(visibility_bytes)
        controls = (
            SharpenVerificationControlHandle(
                baseline_path=baseline_path,
                visibility_path=visibility_path,
                recipe=SharpenVerificationControlRecipeV1(
                    plan_digest=final.plan_digest,
                    action_id=final_action.id,
                    baseline_sha256=selected.baseline_sha256,
                    visibility_control_sha256=selected.visibility_control_sha256,
                    candidate_sha256=selected.candidate_sha256,
                    encode_contract=canonical_video_encode_contract(config),
                    normalized_pts_digest=selected.normalized_pts_digest,
                    stream_topology_digest=selected.stream_topology_digest,
                    source_ranges=final_action.source_ranges,
                    output_ranges=evidence.output_ranges,
                    inventory_frame_count=selected.inventory_frame_count,
                ),
            ),
        )
        for path in controls[0].cleanup_paths:
            path.unlink()
    improved = RescueImprovedExecutionResult(
        output_path=improved_path,
        verification_controls=controls,
    )
    optional_checks: tuple[RescueVerificationCheck, ...] = ()
    if evidence.selected is not None:
        optional_checks = (
            RescueVerificationCheck(
                check_id="perceptible_sharpness_improvement",
                artifact="improved",
                status=RescueVerificationStatus.PASSED,
                message="Exact selected fake metrics passed.",
                measured={
                    "valid": True,
                    "reference": "runtime_same_generation_visibility_control",
                    "source_ranges": [[0.0, 4.0]],
                    "output_ranges": [[0.0, 4.0]],
                    "runtime_control_recipe_valid": True,
                    "selected_qualification_binding_valid": True,
                    **_CLARITY_METRICS,
                },
                required=False,
            ),
        )
    report = RescueVerificationReport(
        plan_digest=final.plan_digest,
        faithful_status=RescueVerificationStatus.PASSED,
        improved_status=RescueVerificationStatus.PASSED,
        checks=(
            *_required_passed_checks("faithful"),
            *_required_passed_checks("improved"),
            *optional_checks,
        ),
        outcome=RescueOutcome.COMPLETED,
    )
    if evidence.selected is None:
        shutil.rmtree(execution_root)
    return _LiveClarityCase(
        source=source,
        source_hash=source_hash,
        draft=draft,
        evidence=evidence,
        final=final,
        faithful=faithful,
        improved=improved,
        controls=controls,
        report=report,
        qualification_root=tmp_path / "qualification root 中文",
        execution_root=execution_root,
    )


def _bind_fake_tools_and_source(
    guard: ClarityRuntimeGuard,
    case: _LiveClarityCase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    ffmpeg = tmp_path / "fixed ffmpeg 8.1.2.exe"
    ffprobe = tmp_path / "fixed ffprobe 8.1.2.exe"
    ffmpeg.write_bytes(b"fixed-ffmpeg-binary")
    ffprobe.write_bytes(b"fixed-ffprobe-binary")

    def fake_verifier(
        path: Path,
        role: Literal["ffmpeg", "ffprobe"],
    ) -> ClarityToolIdentityV1:
        runner = _FakeToolRunner(stdout=f"{role} version 8.1.2\nconfiguration\n")
        return verify_clarity_tool_identity(path, role, runner=runner)

    monkeypatch.setattr(
        provenance_module,
        "verify_clarity_tool_identity",
        fake_verifier,
    )
    guard.bind_tools(ffmpeg, ffprobe)
    guard.bind_source_before(case.source, case.source_hash)
    return ffmpeg, ffprobe


def test_live_object_seal_positive_writes_and_reads_one_passed_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_root = tmp_path / "live positive provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "live positive",
            monkeypatch,
            guard=guard,
        )

        envelope = guard.seal_success(**case.success_kwargs())  # type: ignore[arg-type]
    finally:
        guard.observer.stop()

    retained = read_clarity_runtime_provenance(
        audit_root / "clarity-runtime-provenance.json"
    )
    assert envelope == retained
    assert retained.outcome == "passed"
    assert tuple(event.phase for event in retained.events) == _PASSED_PHASES
    assert {tool.role for tool in retained.tools} == {"ffmpeg", "ffprobe"}
    assert retained.source is not None
    assert retained.source.sha256_before == retained.source.sha256_after
    assert retained.verification is not None
    assert retained.verification.expected_frames == 40
    assert retained.verification.maximum_ringing_ratio == 0.0
    assert str(case.source) not in canonical_provenance_bytes(retained).decode()


def _replace_live_count_metrics(
    case: _LiveClarityCase,
    values: Mapping[str, JsonValue],
) -> None:
    check = _live_clarity_check(case)
    measured = dict(check.measured)
    measured.update(values)
    object.__setattr__(check, "measured", measured)


def test_live_object_seal_canonicalizes_integral_float_counts_to_selected_ints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_root = tmp_path / "integral float count provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "integral float count lifecycle",
            monkeypatch,
            guard=guard,
        )
        _replace_live_count_metrics(
            case,
            {
                "expected_frames": 40.0,
                "compared_frames": 40.0,
                "range_count": 1.0,
                "passing_range_count": 1.0,
            },
        )

        envelope = guard.seal_success(**case.success_kwargs())  # type: ignore[arg-type]
    finally:
        guard.observer.stop()

    retained = read_clarity_runtime_provenance(
        audit_root / "clarity-runtime-provenance.json"
    )
    assert retained == envelope
    assert retained.verification is not None
    integer_metrics = {
        name: getattr(retained.verification, name) for name in _INTEGER_METRIC_DRIFTS
    }
    assert integer_metrics == {
        "expected_frames": 40,
        "compared_frames": 40,
        "range_count": 1,
        "passing_range_count": 1,
    }
    assert all(type(value) is int for value in integer_metrics.values())
    assert retained.verification.metrics_digest == provenance_digest(
        {
            **integer_metrics,
            **{name: _CLARITY_METRICS[name] for name in _FLOAT_METRIC_DRIFTS},
        }
    )
    payload = canonical_provenance_bytes(retained)
    for name, value in integer_metrics.items():
        assert f'"{name}":{value}'.encode() in payload
        assert f'"{name}":{value}.0'.encode() not in payload


def test_integral_int_and_float_counts_have_identical_canonical_metric_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifications = []
    for label, as_float in (("strict int", False), ("integral float", True)):
        audit_root = tmp_path / f"{label} provenance"
        guard = ClarityRuntimeGuard(audit_root)
        guard.start()
        try:
            case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
                tmp_path / f"{label} lifecycle",
                monkeypatch,
                guard=guard,
            )
            if as_float:
                selected = case.evidence.selected
                assert selected is not None
                _replace_live_count_metrics(
                    case,
                    {
                        name: float(getattr(selected.metrics, name))
                        for name in _INTEGER_METRIC_DRIFTS
                    },
                )
            envelope = guard.seal_success(  # type: ignore[arg-type]
                **case.success_kwargs()
            )
        finally:
            guard.observer.stop()
        assert envelope.verification is not None
        verifications.append(envelope.verification)

    strict_int, integral_float = verifications
    assert strict_int.metrics_digest == integral_float.metrics_digest
    strict_payload = strict_int.model_dump(
        mode="python",
        exclude={"report_digest"},
    )
    float_payload = integral_float.model_dump(
        mode="python",
        exclude={"report_digest"},
    )
    assert canonical_provenance_bytes(strict_payload) == canonical_provenance_bytes(
        float_payload
    )
    assert provenance_digest(strict_payload) == provenance_digest(float_payload)


@pytest.mark.parametrize(
    "metric_name",
    (
        "expected_frames",
        "compared_frames",
        "range_count",
        "passing_range_count",
    ),
)
@pytest.mark.parametrize(
    "invalid_kind",
    (
        "boolean",
        "string",
        "null",
        "fractional",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "negative",
        "mismatched-integral",
        "unsafe-float",
        "missing",
    ),
)
def test_live_object_seal_rejects_invalid_count_representation_or_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metric_name: str,
    invalid_kind: str,
) -> None:
    audit_root = tmp_path / f"{metric_name} {invalid_kind} provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / f"{metric_name} {invalid_kind} lifecycle",
            monkeypatch,
            guard=guard,
        )
        selected = case.evidence.selected
        assert selected is not None
        expected = getattr(selected.metrics, metric_name)
        invalid_values: dict[str, JsonValue] = {
            "boolean": bool(expected),
            "string": str(expected),
            "null": None,
            "fractional": float(expected) + 0.5,
            "nan": float("nan"),
            "positive-infinity": float("inf"),
            "negative-infinity": float("-inf"),
            "negative": -1.0,
            "mismatched-integral": float(expected + 1),
            "unsafe-float": float(2**53),
        }
        check = _live_clarity_check(case)
        measured = dict(check.measured)
        if invalid_kind == "missing":
            measured.pop(metric_name)
        else:
            measured[metric_name] = invalid_values[invalid_kind]
        object.__setattr__(check, "measured", measured)

        with pytest.raises(ValueError, match=f"clarity integer metric {metric_name}"):
            guard.seal_success(**case.success_kwargs())  # type: ignore[arg-type]
    finally:
        guard.observer.stop()

    assert not audit_root.exists()


def test_count_metric_negative_zero_canonicalizes_only_to_selected_zero() -> None:
    retained = provenance_module._canonicalize_clarity_count_metric(
        -0.0,
        0,
        name="passing_range_count",
    )

    assert retained == 0
    assert type(retained) is int
    assert canonical_provenance_bytes({"passing_range_count": retained}) == (
        b'{"passing_range_count":0}\n'
    )
    with pytest.raises(
        ValueError,
        match="clarity integer metric passing_range_count",
    ):
        provenance_module._canonicalize_clarity_count_metric(
            -0.0,
            1,
            name="passing_range_count",
        )


def test_count_metric_rejects_float_beyond_exact_json_integer_range() -> None:
    with pytest.raises(ValueError, match="clarity integer metric expected_frames"):
        provenance_module._canonicalize_clarity_count_metric(
            float(2**53),
            2**53,
            name="expected_frames",
        )


def test_live_runtime_verifier_consumes_public_projection_of_private_mappings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_root = tmp_path / "public verifier mapping provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "public verifier mapping",
            monkeypatch,
            guard=guard,
        )
        private_mappings = case.faithful.source_mappings
        verifier_arguments = dict(guard.observer.observed_returns[5].arguments)
        verifier_mappings = verifier_arguments["mappings"]

        assert private_mappings == (
            SourceMapping(
                0.0,
                4.0,
                0.0,
                4.0,
                "staging/faithful-rescue.mp4",
            ),
        )
        assert verifier_mappings == (
            SourceMapping(0.0, 4.0, 0.0, 4.0, "faithful-rescue.mp4"),
        )
        assert verifier_mappings is not private_mappings

        envelope = guard.seal_success(**case.success_kwargs())  # type: ignore[arg-type]
    finally:
        guard.observer.stop()

    assert envelope.final is not None
    assert envelope.final.source_mappings_digest == provenance_digest(
        {
            "source_mappings": (
                {
                    "source_start": 0.0,
                    "source_end": 4.0,
                    "output_start": 0.0,
                    "output_end": 4.0,
                    "output_relative_path": "faithful-rescue.mp4",
                },
            )
        }
    )


@pytest.mark.parametrize(
    "relation_drift",
    (
        "qualification-root",
        "executor-ffmpeg",
        "qualifier-provider-ffprobe",
        "verifier-provider-identity",
    ),
)
def test_live_object_seal_rejects_each_observed_call_relation_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    relation_drift: str,
) -> None:
    audit_root = tmp_path / f"{relation_drift} provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, ffmpeg, ffprobe, runner = _run_pure_production_lifecycle(
            tmp_path / relation_drift,
            monkeypatch,
            guard=guard,
        )
        kwargs = case.success_kwargs()
        observed = guard.observer.observed_returns
        qualifier = cast(NativeRescueCandidateQualifier, observed[1].receiver)
        executor = cast(NativeRescueExecutor, observed[3].receiver)
        verifier = cast(RescueVerifier, observed[5].receiver)
        if relation_drift == "qualification-root":
            kwargs["qualification_root"] = tmp_path / "different absent root"
        elif relation_drift == "executor-ffmpeg":
            executor._ffmpeg = str(tmp_path / "different-ffmpeg.exe")
        elif relation_drift == "qualifier-provider-ffprobe":
            qualifier._measurement_provider._ffprobe = str(
                tmp_path / "different-ffprobe.exe"
            )
        elif relation_drift == "verifier-provider-identity":
            verifier._measurement_provider = NativeMediaMeasurementProvider(
                ffmpeg=str(ffmpeg),
                ffprobe=str(ffprobe),
                command_runner=runner,
            )
        else:
            raise AssertionError(f"unknown relation drift: {relation_drift}")

        with pytest.raises(
            ValueError,
            match="live call|live identity|tool path|qualification root",
        ):
            guard.seal_success(**kwargs)  # type: ignore[arg-type]
    finally:
        guard.observer.stop()

    assert not audit_root.exists()


def test_no_profile_seal_rejects_draft_hash_different_from_bound_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_root = tmp_path / "source draft relation provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "source draft relation",
            monkeypatch,
            passing_profile=False,
            guard=guard,
            planner_input_hash="f" * 64,
        )

        with pytest.raises(ValueError, match="draft.*source binding"):
            guard.seal_no_profile(
                source=case.source,
                source_sha256_after=case.source_hash,
                draft=case.draft,
                evidence=case.evidence,
                qualification_root=case.qualification_root,
                execution_root=case.execution_root,
            )
    finally:
        guard.observer.stop()

    assert not audit_root.exists()


def test_live_object_no_profile_seal_is_honest_and_has_no_final_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_root = tmp_path / "no profile provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "no profile",
            monkeypatch,
            passing_profile=False,
            guard=guard,
        )
        assert case.evidence.selected is None
        assert case.evidence.limitation == SHARPEN_QUALIFICATION_LIMITATION
        envelope = guard.seal_no_profile(
            source=case.source,
            source_sha256_after=case.source_hash,
            draft=case.draft,
            evidence=case.evidence,
            qualification_root=case.qualification_root,
            execution_root=case.execution_root,
        )
    finally:
        guard.observer.stop()

    assert envelope.outcome == "no_profile_passed"
    assert envelope.final is None
    assert envelope.runtime_recipe is None
    assert envelope.verification is None
    assert tuple(event.phase for event in envelope.events) == (
        "tool_identity_verified",
        "draft_bound",
        "qualification_returned",
        "qualification_cleanup_verified",
        "source_integrity_verified",
        "publication_absence_verified",
    )


def test_live_object_no_profile_seal_survives_expected_selector_failure_finalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    audit_root = tmp_path / "no profile finalized provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "no profile finalized",
            monkeypatch,
            passing_profile=False,
            guard=guard,
        )
        guard.seal_no_profile(
            source=case.source,
            source_sha256_after=case.source_hash,
            draft=case.draft,
            evidence=case.evidence,
            qualification_root=case.qualification_root,
            execution_root=case.execution_root,
        )
        _set_call_report(
            request.node,
            outcome="failed",
            exception_type=RuntimeError,
        )

        guard.finalize_from_pytest_item(request.node)
    finally:
        guard.observer.stop()

    retained = read_clarity_runtime_provenance(
        audit_root / "clarity-runtime-provenance.json"
    )
    assert retained.outcome == "no_profile_passed"


def test_finalizer_rejects_passed_call_with_retained_no_profile_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    audit_root = tmp_path / "no profile missing limitation provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "no profile missing limitation",
            monkeypatch,
            passing_profile=False,
            guard=guard,
        )
        terminal = guard.seal_no_profile(
            source=case.source,
            source_sha256_after=case.source_hash,
            draft=case.draft,
            evidence=case.evidence,
            qualification_root=case.qualification_root,
            execution_root=case.execution_root,
        )
        path = audit_root / "clarity-runtime-provenance.json"
        original_bytes = path.read_bytes()
        _set_call_report(request.node, outcome="passed", exception_type=None)

        def reject_second_write(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("retained no-profile terminal must not be rewritten")

        monkeypatch.setattr(
            provenance_module,
            "write_clarity_runtime_provenance",
            reject_second_write,
        )

        with pytest.raises(
            ValueError,
            match="no-profile terminal requires a failed pytest call",
        ):
            guard.finalize_from_pytest_item(request.node)
    finally:
        guard.observer.stop()

    assert path.read_bytes() == original_bytes
    assert read_clarity_runtime_provenance(path) == terminal


def test_finalizer_accepts_passed_call_with_retained_passed_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    audit_root = tmp_path / "passed terminal finalized provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "passed terminal finalized",
            monkeypatch,
            guard=guard,
        )
        terminal = guard.seal_success(**case.success_kwargs())  # type: ignore[arg-type]
        path = audit_root / "clarity-runtime-provenance.json"
        original_bytes = path.read_bytes()
        _set_call_report(request.node, outcome="passed", exception_type=None)

        guard.finalize_from_pytest_item(request.node)
    finally:
        guard.observer.stop()

    assert path.read_bytes() == original_bytes
    assert read_clarity_runtime_provenance(path) == terminal


def test_live_object_tool_and_source_bindings_are_one_shot_and_early(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_live_clarity_case(tmp_path / "one shot")
    guard = ClarityRuntimeGuard(tmp_path / "one shot provenance")
    guard.start()
    try:
        ffmpeg, ffprobe = _bind_fake_tools_and_source(
            guard, case, tmp_path, monkeypatch
        )
        with pytest.raises(ValueError, match="tools.*already bound"):
            guard.bind_tools(ffmpeg, ffprobe)
        with pytest.raises(ValueError, match="source.*already bound"):
            guard.bind_source_before(case.source, case.source_hash)
    finally:
        guard.observer.stop()


def test_live_object_bindings_reject_duplicate_tool_role_and_late_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _build_live_clarity_case(tmp_path / "binding rejection")
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"ffmpeg")
    ffprobe.write_bytes(b"ffprobe")
    duplicate = ClarityToolIdentityV1(
        role="ffmpeg",
        binary_sha256=_digest("binary"),
        reported_version_line="ffmpeg version 8.1.2",
        version_stdout_sha256=_digest("stdout"),
        semantic_version="8.1.2",
    )
    monkeypatch.setattr(
        provenance_module,
        "verify_clarity_tool_identity",
        lambda _path, _role: duplicate,
    )
    guard = ClarityRuntimeGuard(tmp_path / "duplicate role provenance")
    guard.start()
    try:
        with pytest.raises(ValueError, match="duplicate.*role"):
            guard.bind_tools(ffmpeg, ffprobe)
    finally:
        guard.observer.stop()

    guard = ClarityRuntimeGuard(tmp_path / "late source provenance")
    guard.start()
    try:
        inputs = _pure_clarity_inputs(tmp_path / "late source")
        build_rescue_plan(**inputs.planner_inputs)
        with pytest.raises(ValueError, match="source.*before.*draft"):
            guard.bind_source_before(case.source, case.source_hash)
    finally:
        guard.observer.stop()


class _RescuePlanSubclass(RescuePlan):
    pass


_INTEGER_METRIC_DRIFTS = (
    "expected_frames",
    "compared_frames",
    "range_count",
    "passing_range_count",
)
_FLOAT_METRIC_DRIFTS = (
    "range_coverage_ratio",
    "minimum_aggregate_gain_ratio",
    "minimum_recovered_baseline_ratio",
    "minimum_improved_frame_fraction",
    "maximum_noise_increase",
    "maximum_edge_overshoot_ratio",
    "maximum_edge_overshoot_amplitude",
    "maximum_ringing_ratio",
)


def _live_clarity_check(case: _LiveClarityCase) -> RescueVerificationCheck:
    return next(
        check
        for check in case.report.checks
        if check.artifact == "improved"
        and check.check_id == "perceptible_sharpness_improvement"
    )


def _apply_live_drift(
    case: _LiveClarityCase,
    drift: str,
) -> dict[str, object]:
    kwargs = case.success_kwargs()
    if drift == "draft-plan-identity":
        object.__setattr__(case.draft, "plan_digest", "f" * 64)
    elif drift == "final-plan-identity":
        object.__setattr__(case.final, "plan_digest", "f" * 64)
    elif drift == "final-action-identity":
        action = next(
            item for item in case.final.actions if item.kind is RescueActionKind.SHARPEN
        )
        object.__setattr__(action, "id", f"rescue_action_{'f' * 64}")
    elif drift == "selected-profile":
        object.__setattr__(case.evidence, "selected_profile_id", "moderate")
    elif drift == "profile-order":
        object.__setattr__(
            case.evidence,
            "profile_measurements",
            tuple(reversed(case.evidence.profile_measurements)),
        )
    elif drift == "source-ranges":
        object.__setattr__(case.evidence, "source_ranges", ((0.1, 3.9),))
        object.__setattr__(case.evidence, "output_ranges", ((0.1, 3.9),))
    elif drift == "output-ranges":
        object.__setattr__(case.evidence, "output_ranges", ((0.1, 4.1),))
    elif drift == "encode-contract":
        object.__setattr__(
            case.evidence,
            "encode_contract",
            case.evidence.encode_contract.model_copy(update={"crf": 17}),
        )
    elif drift == "mapping":
        mapping = SourceMapping(0.0, 4.0, 0.1, 4.1, "faithful-rescue.mp4")
        object.__setattr__(case.faithful, "source_mappings", (mapping,))
    elif drift in {
        "baseline_sha256",
        "visibility_control_sha256",
        "candidate_sha256",
        "normalized_pts_digest",
        "stream_topology_digest",
    }:
        replacement = {
            "baseline_sha256": "f" * 64,
            "visibility_control_sha256": "e" * 64,
            "candidate_sha256": "c" * 64,
            "normalized_pts_digest": "e" * 64,
            "stream_topology_digest": "b" * 64,
        }[drift]
        object.__setattr__(case.controls[0].recipe, drift, replacement)
    elif drift == "inventory_frame_count":
        object.__setattr__(case.controls[0].recipe, drift, 39)
    elif drift in _INTEGER_METRIC_DRIFTS:
        check = _live_clarity_check(case)
        measured = dict(check.measured)
        measured[drift] = {
            "expected_frames": 41,
            "compared_frames": 39,
            "range_count": 2,
            "passing_range_count": 0,
        }[drift]
        object.__setattr__(check, "measured", measured)
    elif drift in _FLOAT_METRIC_DRIFTS:
        check = _live_clarity_check(case)
        measured = dict(check.measured)
        measured[drift] = {
            "range_coverage_ratio": 0.99,
            "minimum_aggregate_gain_ratio": 0.09,
            "minimum_recovered_baseline_ratio": 0.99,
            "minimum_improved_frame_fraction": 0.99,
            "maximum_noise_increase": 0.001,
            "maximum_edge_overshoot_ratio": 0.001,
            "maximum_edge_overshoot_amplitude": 0.001,
            "maximum_ringing_ratio": 0.001,
        }[drift]
        object.__setattr__(check, "measured", measured)
    elif drift in {
        "runtime_control_recipe_valid",
        "selected_qualification_binding_valid",
    }:
        check = _live_clarity_check(case)
        measured = dict(check.measured)
        measured[drift] = False
        object.__setattr__(check, "measured", measured)
    elif drift == "source-hash":
        kwargs["source_sha256_after"] = "f" * 64
    elif drift == "source-size":
        case.source.write_bytes(case.source.read_bytes() + b"-changed-size")
        kwargs["source_sha256_after"] = sha256(case.source.read_bytes()).hexdigest()
    elif drift == "qualification-cleanup":
        case.qualification_root.mkdir()
    elif drift == "control-cleanup":
        case.controls[0].baseline_path.write_bytes(b"left behind")
    elif drift == "public-rescue-output":
        (case.execution_root / "rescue-output").mkdir()
    elif drift == "public-report-json":
        (case.execution_root / "report.json").write_text("{}", encoding="utf-8")
    elif drift == "public-report-html":
        (case.execution_root / "report.html").write_text("x", encoding="utf-8")
    elif drift == "subclass":
        subclassed = _RescuePlanSubclass.model_validate(
            case.final.model_dump(mode="python")
        )
        kwargs["final"] = subclassed
    elif drift == "model-copy":
        kwargs["final"] = case.final.model_copy()
    elif drift == "prebuilt-json":
        kwargs["final"] = case.final.model_dump(mode="json")
    elif drift == "control-copy":
        kwargs["controls"] = (replace(case.controls[0]),)
    elif drift == "improved-copy":
        kwargs["improved"] = replace(case.improved)
    else:
        raise AssertionError(f"unknown live drift: {drift}")
    return kwargs


@pytest.mark.parametrize(
    "drift",
    (
        "draft-plan-identity",
        "final-plan-identity",
        "final-action-identity",
        "selected-profile",
        "profile-order",
        "source-ranges",
        "output-ranges",
        "encode-contract",
        "mapping",
        "baseline_sha256",
        "visibility_control_sha256",
        "candidate_sha256",
        "normalized_pts_digest",
        "stream_topology_digest",
        "inventory_frame_count",
        *_INTEGER_METRIC_DRIFTS,
        *_FLOAT_METRIC_DRIFTS,
        "runtime_control_recipe_valid",
        "selected_qualification_binding_valid",
        "source-hash",
        "source-size",
        "qualification-cleanup",
        "control-cleanup",
        "public-rescue-output",
        "public-report-json",
        "public-report-html",
        "subclass",
        "model-copy",
        "prebuilt-json",
        "control-copy",
        "improved-copy",
    ),
)
def test_live_object_seal_rejects_each_independent_drift_before_retaining_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    audit_root = tmp_path / f"{drift} provenance"
    guard = ClarityRuntimeGuard(audit_root)
    guard.start()
    try:
        case, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / drift,
            monkeypatch,
            guard=guard,
        )
        kwargs = _apply_live_drift(case, drift)
        with pytest.raises((TypeError, ValueError)):
            guard.seal_success(**kwargs)  # type: ignore[arg-type]
    finally:
        guard.observer.stop()

    assert not audit_root.exists()


def test_live_object_no_profile_rejects_selection_or_later_runtime_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected_guard = ClarityRuntimeGuard(tmp_path / "selected provenance")
    selected_guard.start()
    try:
        selected, _inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "selected",
            monkeypatch,
            guard=selected_guard,
        )
        with pytest.raises(ValueError, match="no-profile|selected"):
            selected_guard.seal_no_profile(
                source=selected.source,
                source_sha256_after=selected.source_hash,
                draft=selected.draft,
                evidence=selected.evidence,
                qualification_root=selected.qualification_root,
                execution_root=selected.execution_root,
            )
    finally:
        selected_guard.observer.stop()

    later_guard = ClarityRuntimeGuard(tmp_path / "later return provenance")
    later_guard.start()
    try:
        no_profile, inputs, _ffmpeg, _ffprobe, _runner = _run_pure_production_lifecycle(
            tmp_path / "later return",
            monkeypatch,
            passing_profile=False,
            guard=later_guard,
        )
        build_rescue_plan(
            **inputs.planner_inputs,
            sharpen_qualification=no_profile.evidence,
            require_sharpen_qualification=True,
        )
        with pytest.raises(ValueError, match="no-profile.*sequence|later"):
            later_guard.seal_no_profile(
                source=no_profile.source,
                source_sha256_after=no_profile.source_hash,
                draft=no_profile.draft,
                evidence=no_profile.evidence,
                qualification_root=no_profile.qualification_root,
                execution_root=no_profile.execution_root,
            )
    finally:
        later_guard.observer.stop()
