"""Tests for shared detector interval operations."""

from __future__ import annotations

import pytest

from videoscope.detectors.intervals import (
    IntervalCandidate,
    expand_to_sample_boundary,
    merge_intervals,
    select_representative_indices,
)


def test_merge_intervals_merges_gap_but_not_different_groups() -> None:
    candidates = [
        IntervalCandidate(0.0, 1.0, (0, 1), group_index=0),
        IntervalCandidate(1.1, 2.0, (2, 3), group_index=0),
        IntervalCandidate(2.0, 3.0, (4, 5), group_index=1),
    ]

    merged = merge_intervals(
        candidates,
        merge_gap_seconds=0.2,
        min_duration_seconds=1.0,
    )

    assert merged == [
        IntervalCandidate(0.0, 2.0, (0, 1, 2, 3), group_index=0),
        IntervalCandidate(2.0, 3.0, (4, 5), group_index=1),
    ]


def test_merge_intervals_rejects_short_candidates_and_bad_config() -> None:
    assert (
        merge_intervals(
            [IntervalCandidate(0.0, 0.5, (0, 1))],
            merge_gap_seconds=0,
            min_duration_seconds=1.0,
        )
        == []
    )
    with pytest.raises(ValueError, match="merge_gap_seconds"):
        merge_intervals([], merge_gap_seconds=-0.1, min_duration_seconds=0)


def test_representative_indices_are_first_middle_last() -> None:
    assert select_representative_indices((8, 2, 6, 4), count=3) == (2, 6, 8)
    assert select_representative_indices((2, 2, 3), count=3) == (2, 3)


def test_sampled_state_expands_to_next_boundary_and_respects_group_end() -> None:
    candidate = IntervalCandidate(2.0, 3.0, (4, 5, 6), group_index=1)

    expanded = expand_to_sample_boundary(
        candidate,
        timestamps=(0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5),
        duration_seconds=4.0,
        group_end_seconds=3.4,
    )

    assert expanded == IntervalCandidate(2.0, 3.4, (4, 5, 6), group_index=1)
