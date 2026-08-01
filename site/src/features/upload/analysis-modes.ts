import {
  defaultBrowserAnalysisOptions,
  type BrowserAnalysisOptions,
} from "../../services/browser-analysis";
import type { Locale } from "../../i18n/types";

export const CPU_DETECTOR_IDS = [
  "near_black",
  "possible_freeze",
  "scene_relative_blur",
  "global_flicker",
] as const;

export type CpuDetectorId = (typeof CPU_DETECTOR_IDS)[number];
export type BrowserAnalysisModeId = "quick" | "deep" | "research";
export type AnalysisModeId =
  | BrowserAnalysisModeId
  | "compare"
  | "batch";

type BrowserMode = {
  kind: "browser_cpu";
  sampleFps: number;
  maxSamples: number;
  maxDimension: number;
};

export const analysisModes = {
  quick: {
    kind: "browser_cpu",
    sampleFps: 2,
    maxSamples: 600,
    maxDimension: 320,
  },
  deep: {
    kind: "browser_cpu",
    sampleFps: 3,
    maxSamples: 1_200,
    maxDimension: 400,
  },
  research: {
    kind: "browser_cpu",
    sampleFps: 4,
    maxSamples: 1_800,
    maxDimension: 480,
  },
  compare: {
    kind: "navigation",
    destination: "/compare",
  },
  batch: {
    kind: "desktop_only",
    disabled: true,
    documentation: "/docs#batch-analysis",
  },
} as const satisfies Record<
  AnalysisModeId,
  BrowserMode | {
    kind: "navigation";
    destination: string;
  } | {
    kind: "desktop_only";
    disabled: true;
    documentation: string;
  }
>;

export function createModeOptions(
  modeId: BrowserAnalysisModeId,
  enabledDetectorIds: readonly CpuDetectorId[],
  locale: Locale,
  reducedMotion: boolean,
): BrowserAnalysisOptions {
  const mode = analysisModes[modeId];
  const enabled = new Set(enabledDetectorIds);
  const detectors = Object.fromEntries(
    Object.entries(defaultBrowserAnalysisOptions.detectors).map(
      ([detectorId, config]) => [
        detectorId,
        {
          ...config,
          enabled: enabled.has(detectorId as CpuDetectorId),
        },
      ],
    ),
  );
  return {
    ...defaultBrowserAnalysisOptions,
    sample_fps: mode.sampleFps,
    max_samples: mode.maxSamples,
    max_dimension: mode.maxDimension,
    locale,
    reduced_motion: reducedMotion,
    detectors,
  };
}
