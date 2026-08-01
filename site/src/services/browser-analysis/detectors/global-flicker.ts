import type { GlobalFlickerConfig } from "../config";
import type {
  BrowserFindingDraft,
  BrowserSample,
  BrowserScene,
} from "../contracts";
import { samplesInScene } from "../scene-segmentation";
import { getDetectorCopy, type BrowserAnalysisLocale } from "../messages";
import {
  clampRatio,
  frameEvidence,
  makeDraft,
  severityFromScore,
} from "./detector-utils";

interface ResidualPoint {
  timestamp: number;
  residual: number;
}

function trimWeakBoundaryResiduals(
  run: readonly ResidualPoint[],
  ratio: number,
): ResidualPoint[] {
  let start = 0;
  let end = run.length - 1;
  if (
    run.length >= 3 &&
    Math.abs(run[start].residual) <
      Math.abs(run[start + 1].residual) * ratio
  ) {
    start += 1;
  }
  if (
    end - start >= 2 &&
    Math.abs(run[end].residual) <
      Math.abs(run[end - 1].residual) * ratio
  ) {
    end -= 1;
  }
  return run.slice(start, end + 1);
}

function residualRuns(
  samples: readonly BrowserSample[],
  scene: BrowserScene,
  config: GlobalFlickerConfig,
): ResidualPoint[][] {
  const sceneSamples = samplesInScene(samples, scene);
  const points: ResidualPoint[] = [];
  for (let index = 1; index < sceneSamples.length - 1; index += 1) {
    const current = sceneSamples[index];
    if (
      current.timestamp_seconds <=
        scene.start_seconds + config.scene_boundary_guard_seconds ||
      current.timestamp_seconds >=
        scene.end_seconds - config.scene_boundary_guard_seconds
    ) {
      continue;
    }
    const trend =
      (sceneSamples[index - 1].median_luma +
        sceneSamples[index + 1].median_luma) /
      2;
    points.push({
      timestamp: current.timestamp_seconds,
      residual: current.median_luma - trend,
    });
  }
  const runs: ResidualPoint[][] = [];
  let active: ResidualPoint[] = [];
  for (const point of points) {
    if (Math.abs(point.residual) < config.residual_threshold) {
      if (active.length > 0) runs.push(active);
      active = [];
      continue;
    }
    const previous = active.at(-1);
    if (
      previous &&
      Math.sign(previous.residual) === Math.sign(point.residual)
    ) {
      if (active.length > 0) runs.push(active);
      active = [point];
    } else {
      active.push(point);
    }
  }
  if (active.length > 0) runs.push(active);
  return runs;
}

export function detectGlobalFlicker(
  samples: readonly BrowserSample[],
  scenes: readonly BrowserScene[],
  config: GlobalFlickerConfig,
  locale: BrowserAnalysisLocale = "en",
): BrowserFindingDraft[] {
  if (!config.enabled || samples.length < 3) return [];
  const copy = getDetectorCopy(locale, "global_flicker");
  return scenes
    .flatMap((scene) => residualRuns(samples, scene, config))
    .map((run) =>
      trimWeakBoundaryResiduals(run, config.boundary_residual_ratio),
    )
    .filter((run) => {
      const cycles = Math.floor((run.length - 1) / 2);
      return (
        cycles >= config.minimum_cycles &&
        run.at(-1)!.timestamp - run[0].timestamp >=
          config.min_duration_seconds
      );
    })
    .map((run) => {
      const peak = run.reduce((largest, point) =>
        Math.abs(point.residual) > Math.abs(largest.residual) ? point : largest,
      );
      const interval = {
        start_seconds: run[0].timestamp,
        end_seconds: run.at(-1)!.timestamp,
      };
      const score = clampRatio(
        Math.abs(peak.residual) /
          Math.max(config.residual_threshold * 2, 0.001),
      );
      return makeDraft({
        detector_id: "global_flicker",
        detector_version: "browser-1",
        title: copy.title,
        description: copy.description,
        severity: severityFromScore(score, config),
        score,
        confidence: clampRatio(0.5 + run.length * 0.05),
        time_range: interval,
        evidence: frameEvidence(
          samples,
          [peak.timestamp],
          copy.evidence,
          {
            peak_residual: Number(peak.residual.toFixed(4)),
            residual_count: run.length,
          },
        ),
        tags: ["luminance", "temporal", "flicker"],
        parameters: { ...config },
        limitations: [...copy.limitations],
      });
    })
    .sort(
      (left, right) =>
        left.time_range.start_seconds - right.time_range.start_seconds,
    );
}
