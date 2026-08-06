"""Offline traceable useful-content report rendering."""

from __future__ import annotations

from tests.content.test_verification import make_plan, mappings_for, passing_evidence
from videoscope.content.models import (
    ContentActionExecution,
    ContentArtifact,
    ContentArtifactRole,
    ContentExecutionStatus,
    ContentPlan,
    ContentTechnicalReport,
)
from videoscope.content.report import (
    build_content_change_log,
    build_content_technical_report,
    render_content_report,
)
from videoscope.content.verification import verify_content_result


def make_report(
    *,
    limitation: str = "Structural heuristics require user review.",
) -> tuple[ContentPlan, ContentTechnicalReport]:
    plan = make_plan()
    mappings = mappings_for(plan)
    verification = verify_content_result(
        plan=plan,
        mappings=mappings,
        evidence=passing_evidence(),
    )
    artifact = ContentArtifact(
        role=ContentArtifactRole.MEDIA,
        relative_path="content-output/useful-content.mp4",
        sha256="f" * 64,
        description="Verified useful-content media.",
    )
    executions = tuple(
        ContentActionExecution(
            action_id=action.id,
            status=ContentExecutionStatus.SUCCEEDED,
        )
        for action in plan.actions
    )
    change_log = build_content_change_log(
        plan=plan,
        executions=executions,
        artifacts=(artifact,),
    )
    report = build_content_technical_report(
        plan=plan,
        verification=verification,
        mappings=mappings,
        change_log=change_log,
        artifacts=(artifact,),
        limitations=(limitation,),
        runtime={"platform": "test"},
    )
    return plan, report


def test_report_contains_traceable_result_and_no_remote_resources() -> None:
    plan, report = make_report()

    html = render_content_report(plan, report)

    assert plan.plan_digest in html
    assert "faithful_clean" not in html
    assert "chaptered_full" in html
    assert "Exact source mappings" in html
    assert "Independent verification" in html
    assert "useful-content.mp4" in html
    assert "http://" not in html.casefold()
    assert "https://" not in html.casefold()
    assert "content-review-private" not in html


def test_report_escapes_user_controlled_text() -> None:
    plan, report = make_report(limitation='<img src=x onerror=alert("x")>')

    html = render_content_report(plan, report)

    assert "<img src=x" not in html
    assert "&lt;img src=x" in html


def test_report_missing_optional_artifacts_is_nonfatal() -> None:
    plan, report = make_report()
    report = report.model_copy(update={"artifacts": (), "change_log": None})

    html = render_content_report(plan, report)

    assert "No downloadable artifact record was included." in html
    assert "No change log was available." in html
