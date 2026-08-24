"""Shared faithful/restoration artifact roles for confirmed Rescue actions."""

from __future__ import annotations

from typing import Literal

from videoscope.rescue.models import (
    FAITHFUL_RESTORATION_ACTION_KINDS,
    REMAINING_IMPROVEMENT_ACTION_KINDS,
    RescueActionKind,
    RescuePlan,
)


def action_artifact_role(
    kind: RescueActionKind,
) -> Literal["faithful", "improved"] | None:
    """Return the media artifact that is allowed to render this action kind."""
    if kind in FAITHFUL_RESTORATION_ACTION_KINDS:
        return "faithful"
    if kind in REMAINING_IMPROVEMENT_ACTION_KINDS:
        return "improved"
    return None


def faithful_restoration_action_ids(plan: RescuePlan) -> frozenset[str]:
    """Return confirmed restoration IDs that execute on faithful media."""
    return frozenset(
        action.id
        for action in plan.actions
        if action.kind in FAITHFUL_RESTORATION_ACTION_KINDS
    )


def remaining_improvement_action_ids(plan: RescuePlan) -> frozenset[str]:
    """Return viewing-only IDs that require a distinct improved artifact."""
    return frozenset(
        action.id
        for action in plan.actions
        if action.kind in REMAINING_IMPROVEMENT_ACTION_KINDS
    )


__all__ = [
    "FAITHFUL_RESTORATION_ACTION_KINDS",
    "REMAINING_IMPROVEMENT_ACTION_KINDS",
    "action_artifact_role",
    "faithful_restoration_action_ids",
    "remaining_improvement_action_ids",
]
