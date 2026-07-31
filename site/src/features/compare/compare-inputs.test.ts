import { describe, expect, it } from "vitest";

import { createDemoReport } from "../../data/demo-report";
import { parseCompatibleBrowserReport } from "./compare-inputs";

describe("parseCompatibleBrowserReport", () => {
  it("accepts a compatible browser report without retaining a media URL", async () => {
    const report = createDemoReport("en");
    const file = new File([JSON.stringify(report)], "report.json", {
      type: "application/json",
    });

    await expect(parseCompatibleBrowserReport(file)).resolves.toMatchObject({
      id: report.id,
      schema_version: "0.1-browser",
    });
  });

  it("rejects incompatible report schemas", async () => {
    const file = new File(
      [JSON.stringify({ schema_version: "desktop-1", source: "real" })],
      "report.json",
      { type: "application/json" },
    );

    await expect(parseCompatibleBrowserReport(file)).rejects.toThrow(
      "compatible browser report",
    );
  });

  it.each([
    ["severity enum", (report: Record<string, unknown>) => {
      const findings = report.findings as Array<Record<string, unknown>>;
      findings[0]!.severity = "urgent";
    }],
    ["inverted time range", (report: Record<string, unknown>) => {
      const findings = report.findings as Array<Record<string, unknown>>;
      findings[0]!.time_range = {
        start_seconds: 4,
        end_seconds: 3,
      };
    }],
    ["execution status", (report: Record<string, unknown>) => {
      const executions = report.detector_executions as Array<
        Record<string, unknown>
      >;
      executions[0]!.status = "complete";
    }],
    ["finding order", (report: Record<string, unknown>) => {
      report.findings = [...(report.findings as unknown[])].reverse();
    }],
    ["execution count", (report: Record<string, unknown>) => {
      const executions = report.detector_executions as Array<
        Record<string, unknown>
      >;
      executions[0]!.findings_count = 99;
    }],
    ["array shape", (report: Record<string, unknown>) => {
      report.findings = {};
    }],
    ["duplicate execution", (report: Record<string, unknown>) => {
      const executions = report.detector_executions as unknown[];
      executions.push(structuredClone(executions[0]));
    }],
    ["summary mismatch", (report: Record<string, unknown>) => {
      const summary = report.summary as Record<string, unknown>;
      const counts = summary.severity_counts as Record<string, unknown>;
      counts.high = 100;
    }],
    ["real report demo marker", (report: Record<string, unknown>) => {
      report.source = "real";
    }],
    ["duplicate detector ID with another version", (report: Record<string, unknown>) => {
      const configurations = report.configuration as Array<
        Record<string, unknown>
      >;
      configurations.push({
        ...structuredClone(configurations[0]),
        detector_version: "different-version",
      });
    }],
    ["invalid optional frame rate", (report: Record<string, unknown>) => {
      const metadata = report.metadata as Record<string, unknown>;
      metadata.frame_rate = "24";
    }],
    ["invalid optional audio flag", (report: Record<string, unknown>) => {
      const metadata = report.metadata as Record<string, unknown>;
      metadata.has_audio = "no";
    }],
    ["invalid optional prompt", (report: Record<string, unknown>) => {
      report.prompt = { text: "not a string" };
    }],
  ])("rejects malformed browser report %s", async (_label, mutate) => {
    const value = structuredClone(createDemoReport("en")) as unknown as Record<
      string,
      unknown
    >;
    mutate(value);
    const file = new File([JSON.stringify(value)], "report.json", {
      type: "application/json",
    });

    await expect(parseCompatibleBrowserReport(file)).rejects.toThrow(
      "compatible browser report",
    );
  });
});
