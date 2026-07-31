import { describe, expect, it } from "vitest";

import {
  computeFrameMetrics,
  hammingDistance,
  meanAbsoluteDifference,
} from "./metrics";

describe("pure browser frame metrics", () => {
  it("computes a statistical median and configured dark-pixel ratio", () => {
    const data = new Uint8ClampedArray([
      0, 0, 0, 255,
      10, 10, 10, 255,
      20, 20, 20, 255,
      30, 30, 30, 255,
    ]);
    const metrics = computeFrameMetrics(
      { data, width: 2, height: 2 } as ImageData,
      10,
    );

    expect(metrics.meanLuma).toBe(15);
    expect(metrics.medianLuma).toBe(15);
    expect(metrics.darkPixelRatio).toBe(0.5);
    expect(metrics.sharpness).toBe(0);
  });

  it("computes independent adjacent-frame and hash differences", () => {
    expect(
      meanAbsoluteDifference(
        new Uint8Array([0, 10, 30]),
        new Uint8Array([0, 20, 10]),
      ),
    ).toBe(10);
    expect(hammingDistance(0b1010n, 0b0011n)).toBe(2);
  });

  it("keeps zero as a valid lower median value", () => {
    const data = new Uint8ClampedArray([
      0, 0, 0, 255,
      10, 10, 10, 255,
    ]);

    expect(
      computeFrameMetrics(
        { data, width: 2, height: 1 } as ImageData,
        5,
      ).medianLuma,
    ).toBe(5);
  });
});
