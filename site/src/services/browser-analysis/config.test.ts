import { describe, expect, it } from "vitest";

import {
  defaultBrowserAnalysisOptions,
  validateBrowserAnalysisOptions,
} from "./config";

describe("browser analysis configuration", () => {
  it("rejects detector thresholds outside their documented domains", () => {
    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        detectors: {
          ...defaultBrowserAnalysisOptions.detectors,
          near_black: {
            ...defaultBrowserAnalysisOptions.detectors.near_black,
            dark_pixel_ratio: 1.2,
          },
        },
      }),
    ).toThrow("dark_pixel_ratio");

    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        detectors: {
          ...defaultBrowserAnalysisOptions.detectors,
          possible_freeze: {
            ...defaultBrowserAnalysisOptions.detectors.possible_freeze,
            min_duration_seconds: -1,
          },
        },
      }),
    ).toThrow("min_duration_seconds");
  });

  it("rejects non-boolean switches and non-integer discrete budgets", () => {
    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        max_samples: 1.1,
      }),
    ).toThrow("max_samples");
    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        max_dimension: 320.5,
      }),
    ).toThrow("max_dimension");
    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        max_evidence_items: 1.5,
      }),
    ).toThrow("max_evidence_items");
    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        max_evidence_total_bytes: 0,
      }),
    ).toThrow("max_evidence_total_bytes");
    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        detectors: {
          ...defaultBrowserAnalysisOptions.detectors,
          near_black: {
            ...defaultBrowserAnalysisOptions.detectors.near_black,
            enabled: "false" as unknown as boolean,
          },
        },
      }),
    ).toThrow("enabled");
  });

  it("validates configured severity boundaries", () => {
    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        detectors: {
          ...defaultBrowserAnalysisOptions.detectors,
          near_black: {
            ...defaultBrowserAnalysisOptions.detectors.near_black,
            medium_severity_threshold: 0.9,
            high_severity_threshold: 0.8,
          },
        },
      }),
    ).toThrow("severity");
    expect(() =>
      validateBrowserAnalysisOptions({
        ...defaultBrowserAnalysisOptions,
        detectors: {
          ...defaultBrowserAnalysisOptions.detectors,
          global_flicker: {
            ...defaultBrowserAnalysisOptions.detectors.global_flicker,
            boundary_residual_ratio: 1.1,
          },
        },
      }),
    ).toThrow("boundary_residual_ratio");
  });
});
