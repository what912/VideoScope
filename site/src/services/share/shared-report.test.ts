import { describe, expect, it } from "vitest";

import { createDemoReport } from "../../data/demo-report";
import {
  sanitizeReportForShare,
  validateSanitizedSharedReport,
} from "./index";

describe("shared report schema validation", () => {
  it("accepts a sanitizer result and returns an isolated value", () => {
    const source = sanitizeReportForShare(createDemoReport("en"), {
      includePrompt: false,
      selectedEvidence: new Set(),
    });
    const validated = validateSanitizedSharedReport(source);

    expect(validated).toEqual(source);
    expect(validated).not.toBe(source);
  });

  it("rejects malformed nested report data instead of rendering it", () => {
    const source = sanitizeReportForShare(createDemoReport("en"), {
      includePrompt: false,
      selectedEvidence: new Set(),
    }) as unknown as Record<string, unknown>;
    source.findings = [
      {
        id: "unsafe",
        detector_id: "detector",
        detector_version: "1",
        signal_kind: "browser_cpu",
        title: "Unsafe",
        description: "Unsafe",
        severity: "high",
        score: 2,
        confidence: 0.5,
        time_range: { start_seconds: 4, end_seconds: 2 },
        evidence: [],
        tags: [],
        parameters: {},
        limitations: [],
      },
    ];

    expect(() => validateSanitizedSharedReport(source)).toThrow(
      /shared report/i,
    );
  });
});
