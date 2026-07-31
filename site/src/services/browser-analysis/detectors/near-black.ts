import type { NearBlackConfig } from "../config";
import type {
  BrowserFindingDraft,
  BrowserSample,
  BrowserScene,
} from "../contracts";
import { flaggedRuns, mergeIntervals } from "../intervals";
import { getDetectorCopy, type BrowserAnalysisLocale } from "../messages";
import {
  clampRatio,
  frameEvidence,
  intervalEvidenceTimes,
  makeDraft,
  severityFromScore,
} from "./detector-utils";

export function detectNearBlack(
  samples: readonly BrowserSample[],
  _scenes: readonly BrowserScene[],
  config: NearBlackConfig,
  locale: BrowserAnalysisLocale = "en",
): BrowserFindingDraft[] {
  if (!config.enabled || samples.length === 0) return [];
  const copy = getDetectorCopy(locale, "near_black");
  const intervals = mergeIntervals(
    flaggedRuns(
      samples.map((sample) => sample.timestamp_seconds),
      samples.map(
        (sample) =>
          sample.mean_luma <= config.mean_luma_threshold &&
          sample.dark_pixel_ratio >= config.dark_pixel_ratio,
      ),
      config.min_duration_seconds,
    ),
    config.merge_gap_seconds,
  );

  return intervals.map((interval) => {
    const affected = samples.filter(
      (sample) =>
        sample.timestamp_seconds >= interval.start_seconds &&
        sample.timestamp_seconds <= interval.end_seconds,
    );
    const meanRatio =
      affected.reduce(
        (total, sample) => total + sample.dark_pixel_ratio,
        0,
      ) / Math.max(1, affected.length);
    const score = clampRatio(
      (meanRatio - config.dark_pixel_ratio) /
        Math.max(0.01, 1 - config.dark_pixel_ratio),
    );
    return makeDraft({
      detector_id: "near_black",
      detector_version: "browser-1",
      title: copy.title,
      description: copy.description,
      severity: severityFromScore(score, config),
      score,
      confidence: clampRatio(0.55 + affected.length * 0.06),
      time_range: interval,
      evidence: frameEvidence(
        samples,
        intervalEvidenceTimes(interval),
        copy.evidence,
        {
          mean_luma_threshold: config.mean_luma_threshold,
          dark_pixel_threshold: config.dark_pixel_threshold,
          dark_pixel_ratio_threshold: config.dark_pixel_ratio,
        },
      ),
      tags: ["luminance", "near-black"],
      parameters: { ...config },
      limitations: [...copy.limitations],
    });
  });
}
