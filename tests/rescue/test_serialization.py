"""Tests for canonical Video Rescue JSON serialization."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import JsonValue, ValidationError

from videoscope.rescue.models import (
    RESCUE_SCHEMA_VERSION,
    DamageInterval,
    DamageKind,
    MediaDamageMap,
    RescueAction,
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
from videoscope.rescue.serialization import (
    read_damage_map_json,
    read_rescue_plan_json,
    read_rescue_technical_report_json,
    rescue_change_log_to_json,
    rescue_technical_report_from_json,
    rescue_technical_report_to_json,
    write_damage_map_json,
    write_rescue_plan_json,
    write_rescue_technical_report_json,
)


def make_damage_map() -> MediaDamageMap:
    """Build a real path-free scan result with unicode public text."""
    interval = DamageInterval(
        id=make_damage_id("a" * 64, "video:0", DamageKind.UNDECODABLE, 1.0, 2.0),
        stream_id="video:0",
        kind=DamageKind.UNDECODABLE,
        start_seconds=1.0,
        end_seconds=2.0,
        description="本地解码器未能读取此时间段。",
    )
    return MediaDamageMap(
        input_hash="a" * 64,
        duration_seconds=4.0,
        scan_coverage=((0.0, 4.0),),
        intervals=(interval,),
    )


def make_plan() -> RescuePlan:
    """Build a digest-bound conservative plan with literal artifacts."""
    action = RescueAction(
        id="remux",
        version="1.0.0",
        kind=RescueActionKind.REMUX,
        description="Write a new locally remuxed copy.",
        source_ranges=((0.0, 4.0),),
        parameters={},
        changes_content=False,
        requires_confirmation=False,
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
    return RescuePlan.model_validate(
        payload | {"plan_digest": make_rescue_plan_digest(payload)}
    )


def make_report() -> RescueTechnicalReport:
    """Build a report whose verification binds to one canonical plan."""
    plan = make_plan()
    artifact = RescueArtifact(
        artifact_role="faithful",
        relative_path="faithful-rescue.mp4",
        sha256="b" * 64,
        description="Faithful rescue copy",
    )
    verification = RescueVerificationReport(
        plan_digest=plan.plan_digest,
        faithful_status=RescueVerificationStatus.PASSED,
        improved_status=None,
        checks=tuple(
            RescueVerificationCheck(
                check_id=check_id,
                artifact="faithful",
                status=RescueVerificationStatus.PASSED,
                message="The local check passed.",
            )
            for check_id in ("decodable", "duration", "streams", "source_read_only")
        ),
        outcome=RescueOutcome.COMPLETED,
    )
    return RescueTechnicalReport(
        plan_digest=plan.plan_digest,
        outcome=RescueOutcome.COMPLETED,
        damage_map=make_damage_map(),
        verification=verification,
        artifacts=(artifact,),
        limitations=("This record contains no overall recovery score.",),
    )


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    ("writer", "reader", "factory"),
    [
        (write_damage_map_json, read_damage_map_json, make_damage_map),
        (write_rescue_plan_json, read_rescue_plan_json, make_plan),
        (
            write_rescue_technical_report_json,
            read_rescue_technical_report_json,
            make_report,
        ),
    ],
)
def test_atomic_json_round_trip_in_unicode_directory(
    tmp_path: Path,
    writer: Callable[[object, Path], None],
    reader: Callable[[Path], object],
    factory: Callable[[], object],
) -> None:
    """Writers must replace stale output atomically without ASCII escaping."""
    destination = tmp_path / "中文 目录" / "result.json"
    destination.parent.mkdir()
    destination.write_text("old", encoding="utf-8")
    value = factory()

    writer(value, destination)

    assert reader(destination) == value
    assert list(destination.parent.glob("*.tmp")) == []


def test_rescue_v02_schema_requires_typed_artifact_roles_and_round_trips() -> None:
    report = make_report()
    payload = json.loads(rescue_technical_report_to_json(report))
    artifact_schema = RescueTechnicalReport.model_json_schema()["$defs"][
        "RescueArtifact"
    ]

    assert RESCUE_SCHEMA_VERSION == "0.2"
    assert report.schema_version == "0.2"
    assert payload["schema_version"] == "0.2"
    assert payload["artifacts"][0]["artifact_role"] == "faithful"
    assert "artifact_role" in artifact_schema["required"]
    assert rescue_technical_report_from_json(json.dumps(payload)) == report


def test_canonical_change_log_serialization_emits_explicit_empty_ledger() -> None:
    """A new canonical writer turns a legacy absence into a known empty ledger."""
    legacy = RescueChangeLog.model_validate({"plan_digest": "a" * 64})
    payload = json.loads(rescue_change_log_to_json(legacy))

    assert legacy.action_execution_state_known is False
    assert payload["action_executions"] == []


def test_rescue_v01_public_report_is_not_accepted_as_v02() -> None:
    payload = json.loads(rescue_technical_report_to_json(make_report()))
    payload["schema_version"] = "0.1"
    for artifact in payload["artifacts"]:
        artifact.pop("artifact_role", None)

    with pytest.raises(ValidationError):
        rescue_technical_report_from_json(json.dumps(payload))
