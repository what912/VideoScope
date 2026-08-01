import type { SceneRelativeBlurConfig } from "../config";
import type {
  BrowserFindingDraft,
  BrowserSample,
  BrowserScene,
} from "../contracts";
import { flaggedRuns, mergeIntervals } from "../intervals";
import { getDetectorCopy, type BrowserAnalysisLocale } from "../messages";
import { samplesInScene } from "../scene-segmentation";
import {
  clampRatio,
  frameEvidence,
  intervalEvidenceTimes,
  makeDraft,
  severityFromScore,
} from "./detector-utils";

function median(values: readonly number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function detectSceneRelativeBlur(
  samples: readonly BrowserSample[],
  scenes: readonly BrowserScene[],
  config: SceneRelativeBlurConfig,
  locale: BrowserAnalysisLocale = "en",
): BrowserFindingDraft[] {
  if (!config.enabled || samples.length === 0) return [];
  const copy = getDetectorCopy(locale, "scene_relative_blur");
  const candidates = scenes.flatMap((scene) => {
    const sceneSamples = samplesInScene(samples, scene);
    const baseline = median(sceneSamples.map((sample) => sample.sharpness));
    const intervals = mergeIntervals(
      flaggedRuns(
        sceneSamples.map((sample) => sample.timestamp_seconds),
        sceneSamples.map(
          (sample) =>
            sample.sharpness <= config.absolute_floor ||
            sample.sharpness <= baseline * config.relative_ratio_threshold,
        ),
        config.min_duration_seconds,
      ),
      config.merge_gap_seconds,
    );
    return intervals.map((interval) => ({ interval, baseline }));
  });

  return candidates
    .sort(
      (left, right) =>
        left.interval.start_seconds - right.interval.start_seconds,
    )
    .map(({ interval, baseline }) => {
      const affected = samples.filter(
        (sample) =>
          sample.timestamp_seconds >= interval.start_seconds &&
          sample.timestamp_seconds <= interval.end_seconds,
      );
      const minimum = Math.min(
        ...affected.map((sample) => sample.sharpness),
      );
      const score = clampRatio(
        1 -
          minimum /
            Math.max(
              0.001,
              baseline * config.relative_ratio_threshold,
              config.absolute_floor,
            ),
      );
      const relativeTriggered = affected.some(
        (sample) =>
          sample.sharpness <= baseline * config.relative_ratio_threshold,
      );
      const absoluteTriggered = affected.some(
        (sample) => sample.sharpness <= config.absolute_floor,
      );
      const triggerReason =
        relativeTriggered && absoluteTriggered
          ? "relative_and_absolute"
          : absoluteTriggered
            ? "absolute_floor"
            : "relative_drop";
      const description =
        triggerReason === "relative_and_absolute"
          ? copy.description_both
          : triggerReason === "absolute_floor"
            ? copy.description_absolute
            : copy.description_relative;
      return makeDraft({
        detector_id: "scene_relative_blur",
        detector_version: "browser-1",
        title: copy.title,
        description,
        severity: severityFromScore(score, config),
        score,
        confidence: clampRatio(0.5 + affected.length * 0.06),
        time_range: interval,
        evidence: frameEvidence(
          samples,
          intervalEvidenceTimes(interval),
          copy.evidence,
          {
            scene_baseline: Number(baseline.toFixed(4)),
            minimum_sharpness: Number(minimum.toFixed(4)),
            trigger_reason: triggerReason,
          },
        ),
        tags: ["sharpness", "scene-relative"],
        parameters: { ...config },
        limitations: [...copy.limitations],
      });
    });
}
