import { afterEach, describe, expect, it, vi } from "vitest";

import { createRealBrowserReport } from "../../types/report";
import { exportWorkspaceReport } from "./workspace-export";

describe("workspace report export", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("keeps the object URL alive through the click and then removes every temporary resource", () => {
    vi.useFakeTimers();
    const report = createRealBrowserReport({
      tool_version: "0.2.0",
      id: "export-report",
      analysis_id: "export-analysis",
      title: "Export report",
      created_at: "2026-07-30T00:00:00.000Z",
      input_hash: "export-input-hash",
      metadata: {
        filename: "video.mp4",
        mime_type: "video/mp4",
        width: 320,
        height: 180,
        duration_seconds: 1,
        file_size_bytes: 1,
      },
      configuration: [],
      detector_executions: [],
      findings: [],
      metrics: [],
      summary: {
        review_interval_count: 0,
        severity_counts: {
          info: 0,
          low: 0,
          medium: 0,
          high: 0,
          critical: 0,
        },
      },
      warnings: [],
      runtime: {
        environment: "browser",
        user_agent_family: "test",
        analysis_seconds: 0,
        sample_count: 0,
      },
      reviewed_finding_ids: [],
      preferences: {
        locale: "en",
        creator_view: true,
        reduced_motion: false,
      },
    });
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:export");
    const revokeObjectURL = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => undefined);
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    exportWorkspaceReport(report);

    const anchor = document.querySelector<HTMLAnchorElement>(
      'a[download="videoscope-export-report.json"]',
    );
    expect(createObjectURL).toHaveBeenCalledOnce();
    expect(click).toHaveBeenCalledOnce();
    expect(anchor).not.toBeNull();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    vi.runAllTimers();

    expect(anchor?.isConnected).toBe(false);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:export");
  });
});
