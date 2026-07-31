import type { TimeRange } from "./types";

export interface TimelinePosition {
  leftPercent: number;
  widthPercent: number;
}

export function intervalPosition(
  range: TimeRange,
  durationSeconds: number,
): TimelinePosition {
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    return { leftPercent: 0, widthPercent: 0 };
  }
  const start = Math.max(0, Math.min(durationSeconds, range.start_seconds));
  const end = Math.max(start, Math.min(durationSeconds, range.end_seconds));
  return {
    leftPercent: (start / durationSeconds) * 100,
    widthPercent: Math.max(((end - start) / durationSeconds) * 100, 0.35),
  };
}

export function containsTime(range: TimeRange, time: number): boolean {
  return time >= range.start_seconds && time < range.end_seconds;
}

export function formatTime(seconds: number): string {
  const safe = Math.max(0, Number.isFinite(seconds) ? seconds : 0);
  const minutes = Math.floor(safe / 60);
  return `${minutes}:${(safe % 60).toFixed(2).padStart(5, "0")}`;
}
