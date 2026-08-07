"""Tests for strict, deterministic Video Rescue models."""

from __future__ import annotations

from typing import cast

import pytest
from pydantic import JsonValue

from videoscope.rescue.models import (
    RESCUE_REQUIRED_VERIFICATION_CHECK_IDS,
    DamageInterval,
    DamageKind,
    RescueAction,
    RescueActionExecution,
    RescueActionExecutionStatus,
    RescueActionKind,
    RescueArtifact,
    RescueChangeLog,
    RescueEffectiveConfig,
    RescueOutcome,
    RescuePlan,
    RescueStrategy,
    RescueTechnicalReport,
    RescueVerificationCheck,
    RescueVerificationReport,
    RescueVerificationStatus,
    make_damage_id,
    make_rescue_plan_digest,
)


def make_damage_payload(
    *,
    start_seconds: float = 2.0,
    end_seconds: float = 3.5,
) -> dict[str, JsonValue]:
    """Build one hand-specified observable interval."""
    return {
        "id": make_damage_id(
            "a" * 64,
            "video:0",
            DamageKind.UNDECODABLE,
            start_seconds,
            end_seconds,
        ),
        "stream_id": "video:0",
        "kind": DamageKind.UNDECODABLE,
        "start_seconds": start_seconds,
        "end_seconds": end_seconds,
        "description": "A local decoder could not read this interval.",
    }


def make_plan_payload() -> dict[str, JsonValue]:
    """Build a confirmation-bound plan without deriving its expected digest."""
    action = RescueAction(
        id="remux",
        version="1.0.0",
        kind=RescueActionKind.REMUX,
        description="Write a new locally remuxed copy.",
        source_ranges=((0.0, 4.0),),
        parameters={},
        changes_content=False,
        requires_confirmation=False,
        depends_on=(),
        fallback=None,
    )
    payload: dict[str, JsonValue] = {
        "input_hash": "a" * 64,
        "strategy": RescueStrategy.CONSERVATIVE,
        "effective_config": RescueEffectiveConfig().model_dump(mode="json"),
        "actions": [action.model_dump(mode="json")],
        "preview_ranges": [[0.0, 4.0]],
        "private_artifacts": ["preview/source-0.mp4"],
        "public_artifacts": ["faithful-rescue.mp4"],
        "damage_intervals": [],
    }
    payload["plan_digest"] = make_rescue_plan_digest(payload)
    return payload


def test_damage_id_is_deterministic() -> None:
    """Changing damage identity inputs must be required to change the ID."""
    first = make_damage_id("a" * 64, "video:0", DamageKind.UNDECODABLE, 2.0, 3.5)
    second = make_damage_id("a" * 64, "video:0", DamageKind.UNDECODABLE, 2.0, 3.5)

    assert first == second
    assert first.startswith("damage_")


def test_action_execution_ledger_requires_truthful_terminal_reason() -> None:
    with pytest.raises(ValueError, match="requires a reason"):
        RescueActionExecution(
            action_id="action",
            kind=RescueActionKind.ADJUST_LUMA,
            status=RescueActionExecutionStatus.FAILED,
            artifact_role="improved",
        )

    record = RescueActionExecution(
        action_id="action",
        kind=RescueActionKind.ADJUST_LUMA,
        status=RescueActionExecutionStatus.FAILED,
        artifact_role="improved",
        reason="The improved candidate could not be completed.",
    )
    assert record.model_dump(mode="json")["status"] == "failed"


def test_legacy_v02_change_log_without_action_ledger_is_unknown() -> None:
    """A missing additive ledger must not be mistaken for a known empty ledger."""
    parsed = RescueChangeLog.model_validate({"plan_digest": "a" * 64})

    assert parsed.action_executions == ()
    assert parsed.action_execution_state_known is False


def test_explicit_empty_action_ledger_is_known() -> None:
    """Newly emitted empty ledgers have a distinct, truthful meaning."""
    parsed = RescueChangeLog.model_validate(
        {"plan_digest": "a" * 64, "action_executions": []}
    )

    assert parsed.action_executions == ()
    assert parsed.action_execution_state_known is True


def test_change_log_rejects_unknown_fields_while_accepting_legacy_ledger_absence() -> (
    None
):
    """Compatibility must not weaken the strict public contract."""
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        RescueChangeLog.model_validate(
            {"plan_digest": "a" * 64, "unrecognized": "value"}
        )


def test_damage_interval_rejects_reverse_time() -> None:
    """An interval must not represent time flowing backwards."""
    with pytest.raises(ValueError):
        DamageInterval.model_validate(
            make_damage_payload(start_seconds=4.0, end_seconds=3.0)
        )


def test_plan_rejects_stale_digest() -> None:
    """Changing effective plan content without its digest must be rejected."""
    payload = make_plan_payload()
    payload["plan_digest"] = "0" * 64

    with pytest.raises(ValueError, match="plan_digest"):
        RescuePlan.model_validate(payload)


def test_plan_digest_binds_the_exact_public_artifact_declaration() -> None:
    payload = make_plan_payload()
    payload["public_artifacts"] = [
        "rescue-plan.json",
        "damaged-segments.json",
        "changes.json",
        "verification-report.json",
        "technical-report.json",
        "report.html",
        "faithful-rescue.mp4",
    ]
    with pytest.raises(ValueError, match="plan_digest"):
        RescuePlan.model_validate(payload)


def test_conservative_plan_rejects_subjective_enhancement() -> None:
    """A Conservative plan must not silently include an enhancement action."""
    payload = make_plan_payload()
    enhancement = RescueAction(
        id="adjust-luma",
        version="1.0.0",
        kind=RescueActionKind.ADJUST_LUMA,
        description="Adjust local luminance for viewing.",
        source_ranges=((0.0, 4.0),),
        parameters={},
        changes_content=True,
        requires_confirmation=True,
    )
    payload["actions"] = [enhancement.model_dump(mode="json")]
    payload["plan_digest"] = make_rescue_plan_digest(payload)

    with pytest.raises(ValueError, match="Conservative"):
        RescuePlan.model_validate(payload)


def test_artifact_rejects_absolute_windows_path() -> None:
    """Public report artifacts must remain portable output-relative paths."""
    with pytest.raises(ValueError, match="normalized relative POSIX path"):
        RescueArtifact(
            artifact_role="faithful",
            relative_path="C:/Users/example/faithful-rescue.mp4",
            sha256="b" * 64,
            description="Faithful rescue copy",
        )


def test_verification_rejects_completed_without_required_checks() -> None:
    """A caller cannot represent an unverified faithful copy as completed."""
    with pytest.raises(ValueError, match="required rescue verification checks"):
        RescueVerificationReport(
            plan_digest="a" * 64,
            faithful_status=RescueVerificationStatus.PASSED,
            improved_status=None,
            checks=(),
            outcome=RescueOutcome.COMPLETED,
        )


def test_verification_derives_failed_status_from_required_check() -> None:
    """A failed required check must override caller-supplied completed status."""
    checks = (
        RescueVerificationCheck(
            check_id="decodable",
            artifact="faithful",
            status=RescueVerificationStatus.PASSED,
            message="The output is decodable.",
        ),
        RescueVerificationCheck(
            check_id="duration",
            artifact="faithful",
            status=RescueVerificationStatus.FAILED,
            message="The measured duration does not match the retained ranges.",
        ),
        RescueVerificationCheck(
            check_id="streams",
            artifact="faithful",
            status=RescueVerificationStatus.PASSED,
            message="Expected streams are present.",
        ),
        RescueVerificationCheck(
            check_id="source_read_only",
            artifact="faithful",
            status=RescueVerificationStatus.PASSED,
            message="The source hash is unchanged.",
        ),
    )

    report = RescueVerificationReport(
        plan_digest="a" * 64,
        faithful_status=RescueVerificationStatus.PASSED,
        improved_status=None,
        checks=checks,
        outcome=RescueOutcome.COMPLETED,
    )

    assert report.faithful_status is RescueVerificationStatus.FAILED
    assert report.outcome == "failed"


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "policy",
    [
        RESCUE_REQUIRED_VERIFICATION_CHECK_IDS[:1],
        tuple(reversed(RESCUE_REQUIRED_VERIFICATION_CHECK_IDS)),
    ],
)
def test_effective_config_rejects_subset_or_reordered_verification_policy(
    policy: tuple[str, ...],
) -> None:
    """A confirmation-bound plan cannot weaken or reorder v0.1 checks."""
    with pytest.raises(ValueError, match="canonical v0.1 verification policy"):
        RescueEffectiveConfig(verification_policy=policy)


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "policy",
    [
        RESCUE_REQUIRED_VERIFICATION_CHECK_IDS[:1],
        tuple(reversed(RESCUE_REQUIRED_VERIFICATION_CHECK_IDS)),
    ],
)
def test_verification_report_rejects_subset_or_reordered_required_policy(
    policy: tuple[str, ...],
) -> None:
    """A report cannot claim completion after replacing the fixed v0.1 policy."""
    checks = tuple(
        RescueVerificationCheck(
            check_id=check_id,
            artifact="faithful",
            status=RescueVerificationStatus.PASSED,
            message="The local check passed.",
        )
        for check_id in policy
    )

    with pytest.raises(ValueError, match="canonical v0.1 verification policy"):
        RescueVerificationReport(
            plan_digest="a" * 64,
            faithful_status=RescueVerificationStatus.PASSED,
            checks=checks,
            outcome=RescueOutcome.COMPLETED,
            required_check_ids=policy,
        )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "measurements",
    [
        {"source": "C:/Users/Alice/private.mp4"},
        {"C:/Users/Alice/private.mp4": "local source"},
    ],
)
def test_technical_report_rejects_absolute_path_in_nested_public_measurement(
    measurements: dict[str, JsonValue],
) -> None:
    """Nested public JSON must not expose a local personal source path."""
    interval = DamageInterval(
        id=make_damage_id("a" * 64, "video:0", DamageKind.UNDECODABLE, 1.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=1.0,
        end_seconds=2.0,
        measurements=measurements,
    )
    damage_map = {
        "input_hash": "a" * 64,
        "duration_seconds": 4.0,
        "intervals": [interval.model_dump(mode="json")],
    }
    checks = tuple(
        RescueVerificationCheck(
            check_id=check_id,
            artifact="faithful",
            status=RescueVerificationStatus.PASSED,
            message="Local verification completed.",
        )
        for check_id in ("decodable", "duration", "streams", "source_read_only")
    )
    verification = RescueVerificationReport(
        plan_digest="b" * 64,
        faithful_status=RescueVerificationStatus.PASSED,
        checks=checks,
        outcome=RescueOutcome.COMPLETED,
    )

    with pytest.raises(ValueError, match="absolute path"):
        RescueTechnicalReport.model_validate(
            {
                "plan_digest": "b" * 64,
                "outcome": "completed",
                "damage_map": damage_map,
                "verification": verification.model_dump(mode="json"),
            }
        )


def test_plan_rejects_damage_interval_from_another_input() -> None:
    """A trim confirmation cannot target an interval observed for another video."""
    payload = make_plan_payload()
    foreign_interval = DamageInterval(
        id=make_damage_id("b" * 64, "video:0", DamageKind.UNDECODABLE, 1.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=1.0,
        end_seconds=2.0,
    )
    payload["damage_intervals"] = [foreign_interval.model_dump(mode="json")]
    payload["plan_digest"] = make_rescue_plan_digest(payload)

    with pytest.raises(ValueError, match="plan input"):
        RescuePlan.model_validate(payload)


def test_plan_rejects_duplicate_action_kind() -> None:
    """Two actions of one kind would make action ordering nondeterministic."""
    payload = make_plan_payload()
    second_remux = RescueAction(
        id="remux-again",
        version="1.0.0",
        kind=RescueActionKind.REMUX,
        description="A second remux is not a distinct stable action kind.",
        source_ranges=((0.0, 4.0),),
        parameters={},
        changes_content=False,
        requires_confirmation=False,
    )
    existing_actions = cast(list[JsonValue], payload["actions"])
    payload["actions"] = [
        existing_actions[0],
        second_remux.model_dump(mode="json"),
    ]
    payload["plan_digest"] = make_rescue_plan_digest(payload)

    with pytest.raises(ValueError, match="duplicate rescue action kind"):
        RescuePlan.model_validate(payload)


def test_plan_digest_normalizes_damage_interval_order() -> None:
    """Equivalent plans must produce one digest and one published interval order."""
    first = DamageInterval(
        id=make_damage_id("a" * 64, "video:0", DamageKind.DECODABLE, 0.0, 1.0),
        stream_id="video:0",
        kind=DamageKind.DECODABLE,
        start_seconds=0.0,
        end_seconds=1.0,
    )
    second = DamageInterval(
        id=make_damage_id("a" * 64, "video:0", DamageKind.UNDECODABLE, 1.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=1.0,
        end_seconds=2.0,
    )
    forward = make_plan_payload()
    reverse = make_plan_payload()
    forward["damage_intervals"] = [
        first.model_dump(mode="json"),
        second.model_dump(mode="json"),
    ]
    reverse["damage_intervals"] = [
        second.model_dump(mode="json"),
        first.model_dump(mode="json"),
    ]
    forward["plan_digest"] = make_rescue_plan_digest(forward)
    reverse["plan_digest"] = make_rescue_plan_digest(reverse)

    first_plan = RescuePlan.model_validate(forward)
    second_plan = RescuePlan.model_validate(reverse)

    assert first_plan.plan_digest == second_plan.plan_digest
    assert first_plan.damage_intervals == second_plan.damage_intervals
