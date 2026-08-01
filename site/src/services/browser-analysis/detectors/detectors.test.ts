import { describe, expect, it } from "vitest";

import {
  defaultGlobalFlickerConfig,
  defaultNearBlackConfig,
  defaultPossibleFreezeConfig,
  defaultSceneRelativeBlurConfig,
} from "../config";
import type { BrowserSample, BrowserScene } from "../contracts";
import { detectGlobalFlicker } from "./global-flicker";
import { detectNearBlack } from "./near-black";
import { detectPossibleFreeze } from "./possible-freeze";
import { detectSceneRelativeBlur } from "./scene-relative-blur";

const scene = (
  start: number,
  end: number,
  sceneIndex = 0,
): BrowserScene => ({
  scene_index: sceneIndex,
  start_seconds: start,
  end_seconds: end,
  representative_timestamp: (start + end) / 2,
});

const sample = (
  timestamp: number,
  overrides: Partial<BrowserSample> = {},
): BrowserSample => ({
  sample_index: Math.round(timestamp * 2),
  timestamp_seconds: timestamp,
  width: 160,
  height: 90,
  mean_luma: 120,
  median_luma: 120,
  dark_pixel_ratio: 0,
  sharpness: 120,
  pixel_difference: 20,
  hash_distance: 12,
  ...overrides,
});

describe("browser CPU detector functions", () => {
  it("detects only a sustained near-black run with neutral limitations", () => {
    const samples = [
      sample(0),
      sample(1),
      sample(2, {
        mean_luma: 4,
        median_luma: 2,
        dark_pixel_ratio: 0.99,
      }),
      sample(3, {
        mean_luma: 5,
        median_luma: 3,
        dark_pixel_ratio: 0.98,
      }),
      sample(4, {
        mean_luma: 6,
        median_luma: 4,
        dark_pixel_ratio: 0.97,
      }),
      sample(5),
    ];

    const findings = detectNearBlack(
      samples,
      [scene(0, 6)],
      defaultNearBlackConfig,
    );

    expect(findings).toHaveLength(1);
    expect(findings[0].time_range).toEqual({
      start_seconds: 2,
      end_seconds: 4,
    });
    expect(findings[0].title).toBe("Near-black interval detected");
    expect(findings[0].limitations.join(" ")).toMatch(
      /intentional|fade|night/i,
    );
  });

  it("resets possible-freeze runs at scene boundaries", () => {
    const samples = [
      sample(0),
      sample(1, { pixel_difference: 0.2, hash_distance: 0 }),
      sample(2, { pixel_difference: 0.2, hash_distance: 0 }),
      sample(3, { pixel_difference: 0.2, hash_distance: 0 }),
      sample(4, { pixel_difference: 0.2, hash_distance: 0 }),
    ];

    const split = detectPossibleFreeze(
      samples,
      [scene(0, 2.5, 0), scene(2.5, 5, 1)],
      { ...defaultPossibleFreezeConfig, min_duration_seconds: 2.5 },
    );
    const oneScene = detectPossibleFreeze(
      samples,
      [scene(0, 5)],
      { ...defaultPossibleFreezeConfig, min_duration_seconds: 2.5 },
    );

    expect(split).toEqual([]);
    expect(oneScene).toHaveLength(1);
    expect(oneScene[0].title).toBe(
      "Possible frozen or repeated frames",
    );
    expect(oneScene[0].time_range).toEqual({
      start_seconds: 0,
      end_seconds: 4,
    });
  });

  it("reports a freeze through the first changed sample boundary", () => {
    const samples = [
      sample(1.5),
      sample(2),
      sample(2.5, { pixel_difference: 0.2, hash_distance: 0 }),
      sample(3, { pixel_difference: 0.2, hash_distance: 0 }),
      sample(3.5, { pixel_difference: 0.2, hash_distance: 0 }),
      sample(4, { pixel_difference: 30, hash_distance: 16 }),
    ];

    const findings = detectPossibleFreeze(samples, [scene(0, 5)], {
      ...defaultPossibleFreezeConfig,
      min_duration_seconds: 1.5,
    });

    expect(findings).toHaveLength(1);
    expect(findings[0].time_range).toEqual({
      start_seconds: 2,
      end_seconds: 4,
    });
    expect(findings[0].score).toBeGreaterThan(0.8);
  });

  it("finds a sustained scene-relative sharpness drop", () => {
    const samples = [
      sample(0, { sharpness: 100 }),
      sample(1, { sharpness: 110 }),
      sample(2, { sharpness: 18 }),
      sample(3, { sharpness: 16 }),
      sample(4, { sharpness: 105 }),
    ];

    const findings = detectSceneRelativeBlur(
      samples,
      [scene(0, 5)],
      {
        ...defaultSceneRelativeBlurConfig,
        min_duration_seconds: 1,
        relative_ratio_threshold: 0.45,
      },
    );

    expect(findings).toHaveLength(1);
    expect(findings[0].title).toBe("Relative sharpness drop");
    expect(findings[0].time_range).toEqual({
      start_seconds: 2,
      end_seconds: 3,
    });
    expect(findings[0].evidence[0].metadata).toMatchObject({
      scene_baseline: 100,
    });
  });

  it("records an absolute-floor trigger for an entirely blurry scene", () => {
    const samples = [
      sample(0, { sharpness: 5 }),
      sample(1, { sharpness: 4 }),
      sample(2, { sharpness: 5 }),
    ];

    const findings = detectSceneRelativeBlur(
      samples,
      [scene(0, 3)],
      {
        ...defaultSceneRelativeBlurConfig,
        absolute_floor: 10,
        min_duration_seconds: 1,
      },
    );

    expect(findings).toHaveLength(1);
    expect(findings[0].description).toMatch(/absolute/i);
    expect(findings[0].evidence[0].metadata).toMatchObject({
      trigger_reason: "absolute_floor",
    });
  });

  it("describes a mixed relative and absolute blur interval without claiming every sample crosses both screens", () => {
    const samples = [
      sample(0, { sharpness: 100 }),
      sample(1, { sharpness: 100 }),
      sample(2, { sharpness: 30 }),
      sample(3, { sharpness: 5 }),
      sample(4, { sharpness: 100 }),
    ];

    const [finding] = detectSceneRelativeBlur(
      samples,
      [scene(0, 5)],
      {
        ...defaultSceneRelativeBlurConfig,
        relative_ratio_threshold: 0.45,
        absolute_floor: 12,
        min_duration_seconds: 1,
      },
    );

    expect(finding.evidence[0].metadata).toMatchObject({
      scene_baseline: 100,
      trigger_reason: "relative_and_absolute",
    });
    expect(finding.description).toBe(
      "This interval contains sampled frames below the scene-relative threshold and sampled frames below the configured absolute screening floor.",
    );
  });

  it("uses configured severity boundaries and records them as parameters", () => {
    const samples = [
      sample(0),
      sample(1, { pixel_difference: 0.2, hash_distance: 0 }),
      sample(2, { pixel_difference: 0.2, hash_distance: 0 }),
    ];
    const config = {
      ...defaultPossibleFreezeConfig,
      min_duration_seconds: 1,
      medium_severity_threshold: 0.4,
      high_severity_threshold: 0.8,
    };

    const [finding] = detectPossibleFreeze(samples, [scene(0, 3)], config);

    expect(finding.severity).toBe("high");
    expect(finding.parameters).toMatchObject({
      medium_severity_threshold: 0.4,
      high_severity_threshold: 0.8,
    });
  });

  it("detects alternating luminance residuals but not a smooth fade", () => {
    const flicker = [100, 150, 90, 155, 85, 150, 100].map((luma, index) =>
      sample(index * 0.5, { mean_luma: luma, median_luma: luma }),
    );
    const fade = [50, 60, 70, 80, 90, 100, 110].map((luma, index) =>
      sample(index * 0.5, { mean_luma: luma, median_luma: luma }),
    );
    const config = {
      ...defaultGlobalFlickerConfig,
      residual_threshold: 20,
      minimum_cycles: 2,
      min_duration_seconds: 1,
      scene_boundary_guard_seconds: 0,
    };

    const findings = detectGlobalFlicker(flicker, [scene(0, 3.5)], config);

    expect(findings).toHaveLength(1);
    expect(findings[0].title).toBe("Potential global luminance flicker");
    expect(findings[0].time_range.start_seconds).toBe(0.5);
    expect(findings[0].time_range.end_seconds).toBe(2.5);
    expect(detectGlobalFlicker(fade, [scene(0, 3.5)], config)).toEqual([]);
    expect(
      detectGlobalFlicker(
        flicker,
        [scene(0, 1.75, 0), scene(1.75, 3.5, 1)],
        config,
      ),
    ).toEqual([]);
  });

  it("trims weak trend-removal residuals at flicker interval boundaries", () => {
    const samples = [100, 100, 100, 50, 200, 50, 200, 100, 100, 100].map(
      (luma, index) =>
        sample(index * 0.5, { mean_luma: luma, median_luma: luma }),
    );
    const findings = detectGlobalFlicker(samples, [scene(0, 5)], {
      ...defaultGlobalFlickerConfig,
      boundary_residual_ratio: 0.5,
      minimum_cycles: 1,
      scene_boundary_guard_seconds: 0,
    });

    expect(findings).toHaveLength(1);
    expect(findings[0].time_range).toEqual({
      start_seconds: 1.5,
      end_seconds: 3,
    });
  });

  it("drops a trimmed flicker run that no longer meets cycle requirements", () => {
    const samples = [100, 100, 100, 50, 200, 50, 200, 100, 100, 100].map(
      (luma, index) =>
        sample(index * 0.5, { mean_luma: luma, median_luma: luma }),
    );

    expect(
      detectGlobalFlicker(samples, [scene(0, 5)], {
        ...defaultGlobalFlickerConfig,
        boundary_residual_ratio: 0.5,
        minimum_cycles: 2,
        scene_boundary_guard_seconds: 0,
      }),
    ).toEqual([]);
  });

  it("does not create high findings for clean motion, empty, or short series", () => {
    const clean = Array.from({ length: 6 }, (_, index) =>
      sample(index, {
        mean_luma: 90 + index * 3,
        median_luma: 90 + index * 3,
        sharpness: 100 + index,
        pixel_difference: 18,
        hash_distance: 10,
      }),
    );
    const scenes = [scene(0, 6)];
    const findings = [
      ...detectNearBlack(clean, scenes, defaultNearBlackConfig),
      ...detectPossibleFreeze(clean, scenes, defaultPossibleFreezeConfig),
      ...detectSceneRelativeBlur(
        clean,
        scenes,
        defaultSceneRelativeBlurConfig,
      ),
      ...detectGlobalFlicker(clean, scenes, defaultGlobalFlickerConfig),
    ];

    expect(findings.filter((finding) => finding.severity === "high")).toEqual(
      [],
    );
    expect(detectNearBlack([], [], defaultNearBlackConfig)).toEqual([]);
    expect(
      detectPossibleFreeze([sample(0)], [scene(0, 0.1)], {
        ...defaultPossibleFreezeConfig,
        min_duration_seconds: 0.5,
      }),
    ).toEqual([]);
  });
});
