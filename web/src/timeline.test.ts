import { describe, expect, it } from "vitest";
import { containsTime, formatTime, intervalPosition } from "./timeline";

describe("timeline calculations", () => {
  it("positions and clamps an interval against video duration", () => {
    expect(
      intervalPosition({ start_seconds: 2, end_seconds: 5 }, 10),
    ).toEqual({ leftPercent: 20, widthPercent: 30 });
    expect(
      intervalPosition({ start_seconds: -2, end_seconds: 12 }, 10),
    ).toEqual({ leftPercent: 0, widthPercent: 100 });
  });

  it("handles missing duration and half-open ranges", () => {
    expect(
      intervalPosition({ start_seconds: 1, end_seconds: 2 }, 0),
    ).toEqual({ leftPercent: 0, widthPercent: 0 });
    expect(containsTime({ start_seconds: 1, end_seconds: 2 }, 1)).toBe(true);
    expect(containsTime({ start_seconds: 1, end_seconds: 2 }, 2)).toBe(false);
  });

  it("formats timestamps consistently", () => {
    expect(formatTime(65.25)).toBe("1:05.25");
  });
});
