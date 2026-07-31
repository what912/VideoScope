import { describe, expect, it } from "vitest";

import type { BrowserFindingDraft } from "./contracts";
import {
  dataUrlByteSize,
  selectEvidenceTimestamps,
} from "./evidence";

function draft(
  detectorId: string,
  start: number,
  timestamps: number[],
): BrowserFindingDraft {
  return {
    detector_id: detectorId,
    detector_version: "test-1",
    signal_kind: "browser_cpu",
    title: "Observable signal",
    description: "Observable signal description.",
    severity: "low",
    score: 0.5,
    confidence: 0.5,
    time_range: { start_seconds: start, end_seconds: start + 1 },
    evidence: timestamps.map((timestamp) => ({
      evidence_type: "frame",
      timestamp_seconds: timestamp,
      description: "Frame evidence",
      metadata: {},
    })),
    tags: ["signal"],
    parameters: {},
    limitations: ["Sampling limitation."],
  };
}

describe("evidence budgets", () => {
  it("selects a deterministic bounded set across findings", () => {
    const selection = selectEvidenceTimestamps(
      [
        draft("later", 4, [4, 4.5, 5]),
        draft("earlier", 1, [1, 1.5, 2]),
        draft("middle", 2, [2, 2.5, 3]),
      ],
      2,
    );

    expect(selection.timestamps).toEqual([1, 2]);
    expect(selection.requested_count).toBe(8);
    expect(selection.capped_by_count).toBe(true);
  });

  it("measures retained data URL bytes independently of item count", () => {
    expect(dataUrlByteSize("data:image/jpeg;base64,YWJj")).toBeGreaterThan(3);
  });
});
