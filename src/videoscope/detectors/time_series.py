"""Reusable deterministic conversions from time series to intervals."""

from __future__ import annotations

from dataclasses import dataclass

from videoscope.detectors.intervals import (
    IntervalCandidate,
    merge_intervals,
)


@dataclass(frozen=True, slots=True)
class TimeSeriesPoint:
    """One ordered sample and its detector-specific anomaly decision."""

    timestamp_seconds: float
    sample_position: int
    is_anomalous: bool
    group_index: int = 0


def anomalous_points_to_intervals(
    points: list[TimeSeriesPoint],
    *,
    merge_gap_seconds: float,
    min_duration_seconds: float,
) -> list[IntervalCandidate]:
    """Convert consecutive anomalous points into merged time intervals."""
    if not points:
        return []
    ordered = sorted(
        points,
        key=lambda point: (point.sample_position, point.timestamp_seconds),
    )
    raw: list[IntervalCandidate] = []
    run: list[TimeSeriesPoint] = []
    for point in ordered:
        continues_run = (
            bool(run)
            and point.is_anomalous
            and point.group_index == run[-1].group_index
            and point.sample_position == run[-1].sample_position + 1
        )
        if continues_run:
            run.append(point)
            continue
        if run:
            raw.append(_series_run_candidate(run))
            run = []
        if point.is_anomalous:
            run = [point]
    if run:
        raw.append(_series_run_candidate(run))
    return merge_intervals(
        raw,
        merge_gap_seconds=merge_gap_seconds,
        min_duration_seconds=min_duration_seconds,
    )


def centered_moving_average(
    timestamps: tuple[float, ...],
    values: tuple[float, ...],
    group_indices: tuple[int, ...],
    *,
    window_seconds: float,
) -> tuple[float, ...]:
    """Estimate a low-frequency trend without crossing group boundaries."""
    if not (len(timestamps) == len(values) == len(group_indices)):
        raise ValueError("time series inputs must have matching lengths")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be greater than zero")
    half_window = window_seconds / 2.0
    trend: list[float] = []
    for position, timestamp in enumerate(timestamps):
        local_values = [
            value
            for other_timestamp, value, group_index in zip(
                timestamps,
                values,
                group_indices,
                strict=True,
            )
            if group_index == group_indices[position]
            and abs(other_timestamp - timestamp) <= half_window
        ]
        trend.append(sum(local_values) / len(local_values))
    return tuple(trend)


def _series_run_candidate(run: list[TimeSeriesPoint]) -> IntervalCandidate:
    return IntervalCandidate(
        start_seconds=run[0].timestamp_seconds,
        end_seconds=run[-1].timestamp_seconds,
        evidence_indices=tuple(point.sample_position for point in run),
        group_index=run[0].group_index,
    )
