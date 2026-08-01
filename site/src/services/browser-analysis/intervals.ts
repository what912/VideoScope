import type { TimeRange } from "../../types/analysis";

export function validateInterval(interval: TimeRange): TimeRange {
  if (
    !Number.isFinite(interval.start_seconds) ||
    !Number.isFinite(interval.end_seconds)
  ) {
    throw new TypeError("Interval endpoints must be finite");
  }
  if (interval.start_seconds < 0) {
    throw new TypeError("start_seconds must not be negative");
  }
  if (interval.end_seconds < interval.start_seconds) {
    throw new TypeError("end_seconds must not precede start_seconds");
  }
  return interval;
}

export function mergeIntervals(
  intervals: readonly TimeRange[],
  mergeGapSeconds: number,
): TimeRange[] {
  if (!Number.isFinite(mergeGapSeconds) || mergeGapSeconds < 0) {
    throw new TypeError("mergeGapSeconds must be a non-negative finite number");
  }
  const sorted = intervals
    .map((interval) => ({ ...validateInterval(interval) }))
    .sort(
      (left, right) =>
        left.start_seconds - right.start_seconds ||
        left.end_seconds - right.end_seconds,
    );
  const merged: TimeRange[] = [];
  for (const interval of sorted) {
    const previous = merged.at(-1);
    if (
      previous &&
      interval.start_seconds <= previous.end_seconds + mergeGapSeconds
    ) {
      previous.end_seconds = Math.max(
        previous.end_seconds,
        interval.end_seconds,
      );
    } else {
      merged.push({ ...interval });
    }
  }
  return merged;
}

export function flaggedRuns(
  timestamps: readonly number[],
  flags: readonly boolean[],
  minimumDurationSeconds: number,
): TimeRange[] {
  const result: TimeRange[] = [];
  let startIndex: number | undefined;
  for (let index = 0; index <= flags.length; index += 1) {
    if (flags[index]) {
      startIndex ??= index;
      continue;
    }
    if (startIndex !== undefined) {
      const endIndex = index - 1;
      const start = timestamps[startIndex];
      const end = timestamps[endIndex];
      if (
        start !== undefined &&
        end !== undefined &&
        end - start >= minimumDurationSeconds
      ) {
        result.push({ start_seconds: start, end_seconds: end });
      }
      startIndex = undefined;
    }
  }
  return result;
}
