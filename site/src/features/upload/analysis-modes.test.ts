import { describe, expect, it } from "vitest";

import { analysisModes, createModeOptions } from "./analysis-modes";

describe("analysis modes", () => {
  it("keeps browser modes CPU-only and distinguishes their sampling budgets", () => {
    expect(analysisModes.quick.kind).toBe("browser_cpu");
    expect(analysisModes.deep.kind).toBe("browser_cpu");
    expect(analysisModes.research.kind).toBe("browser_cpu");

    const quick = createModeOptions("quick", ["near_black"], "en", false);
    const deep = createModeOptions("deep", ["near_black"], "en", false);
    const research = createModeOptions(
      "research",
      ["near_black"],
      "en",
      false,
    );

    expect(deep.sample_fps).toBeGreaterThan(quick.sample_fps);
    expect(research.max_samples).toBeGreaterThan(deep.max_samples);
    expect(quick.detectors.near_black.enabled).toBe(true);
    expect(quick.detectors.global_flicker.enabled).toBe(false);
  });

  it("marks compare as navigation and batch as an unavailable desktop workflow", () => {
    expect(analysisModes.compare).toMatchObject({
      kind: "navigation",
      destination: "/compare",
    });
    expect(analysisModes.batch).toMatchObject({
      kind: "desktop_only",
      disabled: true,
    });
  });
});
