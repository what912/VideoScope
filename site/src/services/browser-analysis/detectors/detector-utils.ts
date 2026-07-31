import type { Evidence, Severity, TimeRange } from "../../../types/analysis";
import type {
  BrowserFindingDraft,
  BrowserSample,
} from "../contracts";

export function clampRatio(value: number): number {
  return Math.max(0, Math.min(1, value));
}

export interface SeverityThresholds {
  medium_severity_threshold: number;
  high_severity_threshold: number;
}

export function severityFromScore(
  score: number,
  thresholds: SeverityThresholds,
): Severity {
  if (score >= thresholds.high_severity_threshold) return "high";
  if (score >= thresholds.medium_severity_threshold) return "medium";
  return "low";
}

export function nearestSample(
  samples: readonly BrowserSample[],
  timestamp: number,
): BrowserSample | undefined {
  return samples.reduce<BrowserSample | undefined>((nearest, sample) => {
    if (!nearest) return sample;
    return Math.abs(sample.timestamp_seconds - timestamp) <
      Math.abs(nearest.timestamp_seconds - timestamp)
      ? sample
      : nearest;
  }, undefined);
}

export function frameEvidence(
  samples: readonly BrowserSample[],
  timestamps: readonly number[],
  description: string,
  metadata: Evidence["metadata"],
): Evidence[] {
  const unique = new Map<number, BrowserSample>();
  timestamps.forEach((timestamp) => {
    const sample = nearestSample(samples, timestamp);
    if (sample) unique.set(sample.timestamp_seconds, sample);
  });
  return [...unique.values()].map((sample) => ({
    evidence_type: "frame",
    timestamp_seconds: sample.timestamp_seconds,
    description,
    metadata: {
      ...metadata,
      mean_luma: Number(sample.mean_luma.toFixed(4)),
      sharpness: Number(sample.sharpness.toFixed(4)),
    },
  }));
}

export function intervalEvidenceTimes(interval: TimeRange): number[] {
  return [
    interval.start_seconds,
    (interval.start_seconds + interval.end_seconds) / 2,
    interval.end_seconds,
  ];
}

export function makeDraft(
  input: Omit<BrowserFindingDraft, "signal_kind">,
): BrowserFindingDraft {
  return {
    ...input,
    signal_kind: "browser_cpu",
  };
}
