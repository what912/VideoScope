"""Validated public documents and offline useful-content report rendering."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from pydantic import JsonValue

from videoscope import __version__
from videoscope.content.models import (
    ContentActionExecution,
    ContentArtifact,
    ContentChangeLog,
    ContentPlan,
    ContentSourceMapping,
    ContentTechnicalReport,
    ContentVerificationReport,
)


def build_content_change_log(
    *,
    plan: ContentPlan,
    executions: tuple[ContentActionExecution, ...],
    artifacts: tuple[ContentArtifact, ...],
) -> ContentChangeLog:
    """Build an exact, source-read-only execution ledger."""
    return ContentChangeLog(
        plan_digest=plan.plan_digest,
        source_modified=False,
        actions=plan.actions,
        executions=executions,
        artifacts=artifacts,
    )


def build_content_technical_report(
    *,
    plan: ContentPlan,
    verification: ContentVerificationReport,
    mappings: tuple[ContentSourceMapping, ...],
    change_log: ContentChangeLog | None,
    artifacts: tuple[ContentArtifact, ...],
    warnings: tuple[str, ...] = (),
    limitations: tuple[str, ...] = (),
    runtime: dict[str, JsonValue] | None = None,
) -> ContentTechnicalReport:
    """Create the validated public envelope without private review material."""
    return ContentTechnicalReport(
        input_hash=plan.input_hash,
        transcript_hash=plan.transcript_hash,
        goal=plan.goal,
        outcome=verification.outcome,
        plan_digest=plan.plan_digest,
        artifacts=artifacts,
        chapters=plan.storyboard.chapters,
        source_mappings=mappings,
        change_log=change_log,
        verification=verification,
        warnings=warnings,
        limitations=limitations,
        runtime={"tool_version": __version__, **(runtime or {})},
    )


def render_content_report(
    plan: ContentPlan,
    report: ContentTechnicalReport,
) -> str:
    """Render only validated models into a self-contained offline HTML page."""
    validated_plan = ContentPlan.model_validate(plan.model_dump(mode="python"))
    validated_report = ContentTechnicalReport.model_validate(
        report.model_dump(mode="python")
    )
    if validated_report.plan_digest != validated_plan.plan_digest:
        raise ValueError("content report and plan digests do not match")
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "reporting" / "templates"),
        autoescape=select_autoescape(default_for_string=True, default=True),
        undefined=StrictUndefined,
    )
    rendered = cast(
        str,
        environment.get_template("content_report.html.j2").render(
            plan=validated_plan,
            report=validated_report,
        ),
    )
    lowered = rendered.casefold()
    if "http://" in lowered or "https://" in lowered:
        raise ValueError("content report must not contain remote resources")
    if "content-review-private" in lowered or "content-pending" in lowered:
        raise ValueError("content report must not reference private artifacts")
    return rendered


__all__ = [
    "build_content_change_log",
    "build_content_technical_report",
    "render_content_report",
]
