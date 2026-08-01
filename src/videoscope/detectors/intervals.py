"""Deterministic interval operations shared by quality detectors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IntervalCandidate:
    """One detector candidate with frame indices and an isolation group."""

    start_seconds: float
    end_seconds: float
    evidence_indices: tuple[int, ...]
    group_index: int = 0

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds


def merge_intervals(
    candidates: list[IntervalCandidate],
    *,
    merge_gap_seconds: float,
    min_duration_seconds: float,
) -> list[IntervalCandidate]:
    """Merge nearby candidates within a group and reject short intervals."""
    if merge_gap_seconds < 0:
        raise ValueError("merge_gap_seconds must not be negative")
    if min_duration_seconds < 0:
        raise ValueError("min_duration_seconds must not be negative")
    if not candidates:
        return []

    ordered = sorted(
        candidates,
        key=lambda candidate: (
            candidate.group_index,
            candidate.start_seconds,
            candidate.end_seconds,
            candidate.evidence_indices,
        ),
    )
    merged: list[IntervalCandidate] = []
    current = ordered[0]
    for candidate in ordered[1:]:
        gap = candidate.start_seconds - current.end_seconds
        if candidate.group_index == current.group_index and gap <= merge_gap_seconds:
            current = IntervalCandidate(
                start_seconds=current.start_seconds,
                end_seconds=max(current.end_seconds, candidate.end_seconds),
                evidence_indices=tuple(
                    sorted(set(current.evidence_indices + candidate.evidence_indices))
                ),
                group_index=current.group_index,
            )
            continue
        if current.duration_seconds >= min_duration_seconds:
            merged.append(current)
        current = candidate
    if current.duration_seconds >= min_duration_seconds:
        merged.append(current)
    return sorted(
        merged,
        key=lambda candidate: (
            candidate.start_seconds,
            candidate.end_seconds,
            candidate.group_index,
        ),
    )


def expand_to_sample_boundary(
    candidate: IntervalCandidate,
    *,
    timestamps: tuple[float, ...],
    duration_seconds: float,
    group_end_seconds: float | None = None,
) -> IntervalCandidate:
    """Expand a sampled state through its following half-open sample boundary."""
    if not candidate.evidence_indices:
        return candidate
    last_position = candidate.evidence_indices[-1]
    next_timestamp = (
        timestamps[last_position + 1]
        if last_position + 1 < len(timestamps)
        else duration_seconds
    )
    boundary = min(
        next_timestamp,
        duration_seconds,
        group_end_seconds if group_end_seconds is not None else duration_seconds,
    )
    return IntervalCandidate(
        start_seconds=candidate.start_seconds,
        end_seconds=max(candidate.end_seconds, boundary),
        evidence_indices=candidate.evidence_indices,
        group_index=candidate.group_index,
    )


def select_representative_indices(
    evidence_indices: tuple[int, ...],
    *,
    count: int = 3,
) -> tuple[int, ...]:
    """Select deterministic first, middle, and final evidence indices."""
    if count <= 0:
        raise ValueError("count must be greater than zero")
    ordered = tuple(sorted(set(evidence_indices)))
    if len(ordered) <= count:
        return ordered
    if count == 1:
        return (ordered[len(ordered) // 2],)
    positions = [
        round(position * (len(ordered) - 1) / (count - 1)) for position in range(count)
    ]
    return tuple(ordered[position] for position in positions)
