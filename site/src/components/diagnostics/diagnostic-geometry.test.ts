import { describe, expect, it } from "vitest";

import {
  containedMediaRect,
  intervalToPercent,
} from "./diagnostic-geometry";

describe("intervalToPercent", () => {
  it("maps a valid interval to timeline percentages", () => {
    expect(intervalToPercent(2, 5, 10)).toEqual({ left: 20, width: 30 });
  });

  it.each([
    [Number.NaN, 5, 10, { left: 0, width: 50 }],
    [-4, 14, 10, { left: 0, width: 100 }],
    [8, 3, 10, { left: 80, width: 0 }],
    [2, 5, 0, { left: 0, width: 0 }],
    [2, Number.POSITIVE_INFINITY, 10, { left: 20, width: 0 }],
  ])(
    "clamps unsafe geometry without producing invalid CSS",
    (start, end, duration, expected) => {
      expect(intervalToPercent(start, end, duration)).toEqual(expected);
    },
  );
});

describe("containedMediaRect", () => {
  it.each([
    [1600, 900, 1920, 1080, { left: 0, top: 0, width: 1600, height: 900 }],
    [1600, 900, 1080, 1920, { left: 546.875, top: 0, width: 506.25, height: 900 }],
    [1600, 900, 1000, 1000, { left: 350, top: 0, width: 900, height: 900 }],
    [900, 1600, 1920, 1080, { left: 0, top: 546.875, width: 900, height: 506.25 }],
  ])(
    "contains %sx%s media inside a %sx%s shell",
    (containerWidth, containerHeight, mediaWidth, mediaHeight, expected) => {
      expect(
        containedMediaRect(
          containerWidth,
          containerHeight,
          mediaWidth,
          mediaHeight,
        ),
      ).toEqual(expected);
    },
  );

  it("falls back to the whole finite container for invalid media dimensions", () => {
    expect(containedMediaRect(640, 360, Number.NaN, 0)).toEqual({
      left: 0,
      top: 0,
      width: 640,
      height: 360,
    });
  });
});
