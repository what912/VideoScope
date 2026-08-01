import type { PossibleFreezeConfig } from "../config";
import type {
  BrowserFindingDraft,
  BrowserSample,
  BrowserScene,
} from "../contracts";
import { mergeIntervals } from "../intervals";
import { getDetectorCopy, type BrowserAnalysisLocale } from "../messages";
import { samplesInScene } from "../scene-segmentation";
import {
  clampRatio,
  frameEvidence,
  intervalEvidenceTimes,
  makeDraft,
  severityFromScore,
} from "./detector-utils";

function freezeIntervals(
  samples: readonly BrowserSample[],
  scenes: readonly BrowserScene[],
  config: PossibleFreezeConfig,
) {
  const intervals = scenes.flatMap((scene) => {
    const sceneSamples = samplesInScene(samples, scene);
    const result = [];
    let start: number | undefined;
    let end: number | undefined;
    for (let index = 1; index <= sceneSamples.length; index += 1) {
      const sample = sceneSamples[index];
      const similar =
        sample !== undefined &&
        sample.pixel_difference <= config.max_pixel_difference &&
        sample.hash_distance <= config.max_hash_distance;
      if (similar) {
        start ??= sceneSamples[index - 1].timestamp_seconds;
        end = sample.timestamp_seconds;
      } else if (start !== undefined && end !== undefined) {
        if (end - start >= config.min_duration_seconds) {
          result.push({
            start_seconds: start,
            end_seconds: sample?.timestamp_seconds ?? end,
          });
        }
        start = undefined;
        end = undefined;
      }
    }
    return mergeIntervals(result, config.merge_gap_seconds);
  });
  return intervals.sort(
    (left, right) => left.start_seconds - right.start_seconds,
  );
}

export function detectPossibleFreeze(
  samples: readonly BrowserSample[],
  scenes: readonly BrowserScene[],
  config: PossibleFreezeConfig,
  locale: BrowserAnalysisLocale = "en",
): BrowserFindingDraft[] {
  if (!config.enabled || samples.length < 2) return [];
  const copy = getDetectorCopy(locale, "possible_freeze");
  return freezeIntervals(samples, scenes, config).map((interval) => {
    const affected = samples.filter(
      (sample) =>
        sample.timestamp_seconds > interval.start_seconds &&
        sample.timestamp_seconds <= interval.end_seconds &&
        sample.pixel_difference <= config.max_pixel_difference &&
        sample.hash_distance <= config.max_hash_distance,
    );
    const averageDifference =
      affected.reduce(
        (total, sample) => total + sample.pixel_difference,
        0,
      ) / Math.max(1, affected.length);
    const score = clampRatio(
      1 - averageDifference / Math.max(0.001, config.max_pixel_difference),
    );
    return makeDraft({
      detector_id: "possible_freeze",
      detector_version: "browser-1",
      title: copy.title,
      description: copy.description,
      severity: severityFromScore(score, config),
      score,
      confidence: clampRatio(0.5 + affected.length * 0.07),
      time_range: interval,
      evidence: frameEvidence(
        samples,
        intervalEvidenceTimes(interval),
        copy.evidence,
        {
          average_pixel_difference: Number(averageDifference.toFixed(4)),
          max_hash_distance: config.max_hash_distance,
        },
      ),
      tags: ["temporal", "repeated-frames"],
      parameters: { ...config },
      limitations: [...copy.limitations],
    });
  });
}
