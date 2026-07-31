import { describe, expect, it } from "vitest";

import { mergeIntervals, validateInterval } from "./intervals";

describe("interval utilities", () => {
  it("rejects reversed or non-finite intervals", () => {
    expect(() =>
      validateInterval({ start_seconds: 2, end_seconds: 1 }),
    ).toThrow("end_seconds");
    expect(() =>
      validateInterval({ start_seconds: Number.NaN, end_seconds: 1 }),
    ).toThrow("finite");
  });

  it("sorts and deterministically merges intervals within the configured gap", () => {
    expect(
      mergeIntervals(
        [
          { start_seconds: 3.1, end_seconds: 4 },
          { start_seconds: 1, end_seconds: 2 },
          { start_seconds: 2.2, end_seconds: 2.8 },
        ],
        0.25,
      ),
    ).toEqual([
      { start_seconds: 1, end_seconds: 2.8 },
      { start_seconds: 3.1, end_seconds: 4 },
    ]);
  });
});
