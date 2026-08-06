import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/AppProviders";
import { createDemoReport } from "../../data/demo-report";
import type { BrowserAnalysisService } from "../../services/browser-analysis";
import {
  createRealBrowserReport,
  type BrowserReport,
  type RealBrowserReport,
} from "../../types/report";
import { ComparePage } from "./ComparePage";

function report(
  side: "a" | "b",
  duration: number,
  findingRange: readonly [number, number],
): BrowserReport {
  const base = createDemoReport("en");
  return {
    ...base,
    id: `${side}-report`,
    title: `Video ${side.toUpperCase()}`,
    metadata: { ...base.metadata, duration_seconds: duration, frame_rate: 10 },
    findings: base.findings.map((finding, index) =>
      index === 0
        ? {
            ...finding,
            time_range: {
              start_seconds: findingRange[0],
              end_seconds: findingRange[1],
            },
          }
        : finding,
    ),
  };
}

function asRealReport(source: BrowserReport): RealBrowserReport {
  return createRealBrowserReport({
    tool_version: source.tool_version,
    id: source.id,
    analysis_id: source.analysis_id,
    title: source.title,
    created_at: source.created_at,
    input_hash: source.input_hash,
    metadata: source.metadata,
    configuration: source.configuration.flatMap((item) =>
      item.signal_kind === "browser_cpu" ? [item] : [],
    ),
    detector_executions: source.detector_executions.flatMap((item) =>
      item.signal_kind === "browser_cpu" ? [item] : [],
    ),
    findings: source.findings
      .flatMap((item) =>
        item.signal_kind === "browser_cpu"
          ? [
              {
                ...item,
                tags: ["browser_cpu"],
                evidence: item.evidence.map((evidence) => ({
                  ...evidence,
                  metadata: {},
                })),
              },
            ]
          : [],
      ),
    metrics: source.metrics.flatMap((item) =>
      item.kind === "browser_cpu" ? [item] : [],
    ),
    summary: source.summary,
    warnings: source.warnings,
    runtime: source.runtime,
    reviewed_finding_ids: source.reviewed_finding_ids,
    preferences: source.preferences,
  });
}

function renderPage(
  props: Partial<React.ComponentProps<typeof ComparePage>> = {},
) {
  return render(
    <AppProviders>
      <MemoryRouter>
        <ComparePage
          initialA={{ report: report("a", 10, [1, 2]), mediaUrl: "blob:a" }}
          initialB={{ report: report("b", 20, [3, 5]), mediaUrl: "blob:b" }}
          {...props}
        />
      </MemoryRouter>
    </AppProviders>,
  );
}

describe("ComparePage", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("shows detector-level observations without a universal ranking", () => {
    renderPage();

    expect(
      screen.getByRole("heading", { name: "Compare videos" }),
    ).toBeVisible();
    expect(
      screen.getByText(/No universal ranking is produced\./),
    ).toBeVisible();
    const table = screen.getByRole("table", {
      name: "Detector differences",
    });
    expect(within(table).getByText("global_flicker")).toBeVisible();
    expect(within(table).getAllByText("Equal").length).toBeGreaterThan(0);
    expect(screen.queryByText(/overall score/i)).not.toBeInTheDocument();
  });

  it("uses absolute or normalized shared seeking for unequal durations", () => {
    renderPage();

    const seek = screen.getByRole("slider", { name: "Shared seek" });
    expect(seek).toHaveAttribute("max", "20");
    fireEvent.change(seek, { target: { value: "15" } });
    expect(screen.getByTestId("compare-time-a")).toHaveTextContent("00:10.0");
    expect(screen.getByTestId("compare-time-b")).toHaveTextContent("00:15.0");

    fireEvent.change(seek, { target: { value: "5" } });
    expect(screen.getByTestId("compare-time-a")).toHaveTextContent("00:05.0");
    expect(screen.getByTestId("compare-time-b")).toHaveTextContent("00:05.0");

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Normalized timeline" }),
    );
    expect(seek).toHaveAttribute("max", "1");
    fireEvent.change(seek, { target: { value: "0.5" } });
    expect(screen.getByTestId("compare-time-a")).toHaveTextContent("00:05.0");
    expect(screen.getByTestId("compare-time-b")).toHaveTextContent("00:10.0");
    expect(screen.getByTestId("comparison-side-a")).toHaveTextContent("0.50");
    expect(screen.getByTestId("comparison-side-b")).toHaveTextContent("1.00");
  });

  it("keeps independent positions when synchronization is disabled", () => {
    renderPage();

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Synchronize playback" }),
    );
    const sideA = screen.getByRole("region", { name: "Video A comparison" });
    fireEvent.click(within(sideA).getByRole("button", { name: "Next frame A" }));

    expect(screen.getByTestId("compare-time-a")).toHaveTextContent("00:00.1");
    expect(screen.getByTestId("compare-time-b")).toHaveTextContent("00:00.0");
    expect(screen.getByTestId("compare-time-a").textContent).not.toBe(
      screen.getByTestId("compare-time-b").textContent,
    );
  });

  it("swaps reports, media, and timeline positions", () => {
    renderPage();
    fireEvent.change(screen.getByRole("slider", { name: "Shared seek" }), {
      target: { value: "4" },
    });

    fireEvent.click(screen.getByRole("button", { name: "Swap A and B" }));

    const sideA = screen.getByRole("region", { name: "Video A comparison" });
    const sideB = screen.getByRole("region", { name: "Video B comparison" });
    expect(within(sideA).getByText("Video B")).toBeVisible();
    expect(within(sideB).getByText("Video A")).toBeVisible();
    expect(within(sideA).getByLabelText("Selected video")).toHaveAttribute(
      "src",
      "blob:b",
    );
  });

  it("reveals paired evidence for the selected detector", () => {
    renderPage();

    fireEvent.click(
      screen.getByRole("button", {
        name: "View evidence for global_flicker",
      }),
    );

    const evidence = screen.getByRole("region", {
      name: "Evidence pair",
    });
    expect(within(evidence).getByRole("heading", { name: "Evidence pair" })).toBeVisible();
    expect(within(evidence).getAllByRole("img")).toHaveLength(2);
  });

  it("preserves the selected Finding ID and shows paired interval details", () => {
    const aReport = report("a", 10, [1, 2]);
    const bReport = report("b", 20, [3, 5]);
    const secondA = {
      ...aReport.findings[0]!,
      id: "a-second",
      title: "Second A interval",
      time_range: { start_seconds: 7, end_seconds: 8 },
      evidence: aReport.findings[0]!.evidence.map((item) => ({
        ...item,
        description: "Second A evidence",
        timestamp_seconds: 7.5,
      })),
    };
    const secondB = {
      ...bReport.findings[0]!,
      id: "b-second",
      title: "Second B interval",
      time_range: { start_seconds: 14, end_seconds: 16 },
      evidence: bReport.findings[0]!.evidence.map((item) => ({
        ...item,
        description: "Second B evidence",
        timestamp_seconds: 15,
      })),
    };
    aReport.findings = [...aReport.findings, secondA];
    bReport.findings = [...bReport.findings, secondB];
    renderPage({
      initialA: { report: aReport, mediaUrl: "blob:a" },
      initialB: { report: bReport, mediaUrl: "blob:b" },
    });

    const sideA = screen.getByRole("region", { name: "Video A comparison" });
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Normalized timeline" }),
    );
    fireEvent.click(
      within(sideA).getByRole("button", { name: /Second A interval/ }),
    );

    expect(within(sideA).getByTestId("finding-detail")).toHaveTextContent(
      "Second A interval",
    );
    const sideB = screen.getByRole("region", { name: "Video B comparison" });
    expect(within(sideB).getByTestId("finding-detail")).toHaveTextContent(
      "Second B interval",
    );
    const evidence = screen.getByRole("region", { name: "Evidence pair" });
    expect(
      within(evidence).getByRole("img", { name: "Second A evidence" }),
    ).toBeVisible();
    expect(
      within(evidence).getByRole("img", { name: "Second B evidence" }),
    ).toBeVisible();
  });

  it("reconciles paired evidence when the timeline mode changes", () => {
    const aReport = report("a", 10, [1, 2]);
    const bReport = report("b", 20, [3, 5]);
    aReport.findings = [
      ...aReport.findings,
      {
        ...aReport.findings[0]!,
        id: "a-late",
        title: "Late A interval",
        time_range: { start_seconds: 7, end_seconds: 8 },
      },
    ];
    bReport.findings = [
      ...bReport.findings,
      {
        ...bReport.findings[0]!,
        id: "b-late",
        title: "Late B interval",
        time_range: { start_seconds: 14, end_seconds: 16 },
      },
    ];
    renderPage({
      initialA: { report: aReport, mediaUrl: "blob:a" },
      initialB: { report: bReport, mediaUrl: "blob:b" },
    });

    const sideA = screen.getByRole("region", { name: "Video A comparison" });
    fireEvent.click(
      within(sideA).getByRole("button", { name: /Late A interval/ }),
    );
    const sideB = screen.getByRole("region", { name: "Video B comparison" });
    expect(within(sideB).getByTestId("finding-detail")).not.toHaveTextContent(
      "Late B interval",
    );

    fireEvent.click(
      screen.getByRole("checkbox", { name: "Normalized timeline" }),
    );
    expect(within(sideB).getByTestId("finding-detail")).toHaveTextContent(
      "Late B interval",
    );
  });

  it("visibly labels demo reports and optional demo signals", () => {
    const demo = report("a", 10, [1, 2]);
    if (demo.source !== "demo") {
      throw new TypeError("Expected a demo fixture");
    }
    const untrusted = {
      ...demo,
      demo_label: "BENCHMARK WINNER",
    };
    renderPage({
      initialA: { report: untrusted, mediaUrl: "blob:a" },
      initialB: { report: untrusted, mediaUrl: "blob:b" },
    });

    expect(screen.getAllByText("INTERACTIVE DEMO")).toHaveLength(2);
    expect(screen.queryByText("BENCHMARK WINNER")).not.toBeInTheDocument();
    const table = screen.getByRole("table", {
      name: "Detector differences",
    });
    const optionalRow = within(table)
      .getByRole("button", {
        name: "View evidence for demo_optional_geometry",
      })
      .closest("tr");
    expect(optionalRow).toHaveTextContent("OPTIONAL / DEMO");
  });

  it("loads two compatible browser reports without creating media URLs", async () => {
    const reportA = {
      ...createDemoReport("en"),
      title: "Imported report A",
    };
    const reportB = {
      ...createDemoReport("en"),
      title: "Imported report B",
    };
    const createObjectURL = vi.fn<(file: File) => string>();
    renderPage({
      initialA: undefined,
      initialB: undefined,
      createObjectURL,
    });

    fireEvent.change(screen.getByLabelText("Browser report A"), {
      target: {
        files: [
          new File([JSON.stringify(reportA)], "report-a.json", {
            type: "application/json",
          }),
        ],
      },
    });
    fireEvent.change(screen.getByLabelText("Browser report B"), {
      target: {
        files: [
          new File([JSON.stringify(reportB)], "report-b.json", {
            type: "application/json",
          }),
        ],
      },
    });

    expect(await screen.findByText("Imported report A")).toBeVisible();
    expect(await screen.findByText("Imported report B")).toBeVisible();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("renders the comparison workflow in Simplified Chinese", () => {
    window.localStorage.setItem("videoscope.locale", "zh-CN");
    renderPage();

    expect(
      screen.getByRole("heading", { name: "视频对比" }),
    ).toBeVisible();
    expect(
      screen.getByRole("checkbox", { name: "同步播放" }),
    ).toBeVisible();
  });

  it("analyzes two local files through the shared browser analysis service and revokes session URLs", async () => {
    const aReport = asRealReport(report("a", 10, [1, 2]));
    const bReport = asRealReport(report("b", 20, [3, 5]));
    const analyzeLocalVideo = vi
      .fn<BrowserAnalysisService["analyzeLocalVideo"]>()
      .mockResolvedValueOnce(aReport)
      .mockResolvedValueOnce(bReport);
    const createObjectURL = vi
      .fn<(file: File) => string>()
      .mockReturnValueOnce("blob:local-a")
      .mockReturnValueOnce("blob:local-b");
    const revokeObjectURL = vi.fn();
    const view = renderPage({
      initialA: undefined,
      initialB: undefined,
      analysisService: { analyzeLocalVideo },
      createObjectURL,
      revokeObjectURL,
    });
    const fileA = new File(["a"], "视频 A.mp4", { type: "video/mp4" });
    const fileB = new File(["b"], "Video B.mp4", { type: "video/mp4" });

    fireEvent.change(screen.getByLabelText("Local video A"), {
      target: { files: [fileA] },
    });
    fireEvent.change(screen.getByLabelText("Local video B"), {
      target: { files: [fileB] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyze both videos" }));

    expect(await screen.findByText("Video A")).toBeVisible();
    expect(analyzeLocalVideo).toHaveBeenCalledTimes(2);
    expect(createObjectURL).toHaveBeenCalledTimes(2);

    view.unmount();
    await waitFor(() => {
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:local-a");
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:local-b");
    });
  });

  it("revokes a partially-created session URL if the second URL cannot be created", async () => {
    const analyzeLocalVideo = vi
      .fn<BrowserAnalysisService["analyzeLocalVideo"]>()
      .mockResolvedValueOnce(asRealReport(report("a", 10, [1, 2])))
      .mockResolvedValueOnce(asRealReport(report("b", 20, [3, 5])));
    const createObjectURL = vi
      .fn<(file: File) => string>()
      .mockReturnValueOnce("blob:partial")
      .mockImplementationOnce(() => {
        throw new Error("Object URL allocation failed");
      });
    const revokeObjectURL = vi.fn();
    renderPage({
      initialA: undefined,
      initialB: undefined,
      analysisService: { analyzeLocalVideo },
      createObjectURL,
      revokeObjectURL,
    });

    fireEvent.change(screen.getByLabelText("Local video A"), {
      target: {
        files: [new File(["a"], "a.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.change(screen.getByLabelText("Local video B"), {
      target: {
        files: [new File(["b"], "b.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyze both videos" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The comparison input could not be opened.",
    );
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:partial");
  });

  it("cancels the peer analysis when either local video fails", async () => {
    let peerSignal: AbortSignal | undefined;
    const analyzeLocalVideo = vi
      .fn<BrowserAnalysisService["analyzeLocalVideo"]>()
      .mockRejectedValueOnce(new Error("decode failed"))
      .mockImplementationOnce((_file, _options, signal) => {
        peerSignal = signal;
        return new Promise((_resolve, reject) => {
          signal.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        });
      });
    renderPage({
      initialA: undefined,
      initialB: undefined,
      analysisService: { analyzeLocalVideo },
    });

    fireEvent.change(screen.getByLabelText("Local video A"), {
      target: {
        files: [new File(["a"], "a.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.change(screen.getByLabelText("Local video B"), {
      target: {
        files: [new File(["b"], "b.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyze both videos" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The comparison input could not be opened.",
    );
    expect(peerSignal?.aborted).toBe(true);
  });

  it("locks input mutations while analysis is running and ignores a superseded run", async () => {
    let resolveA: ((report: RealBrowserReport) => void) | undefined;
    let resolveB: ((report: RealBrowserReport) => void) | undefined;
    const analyzeLocalVideo = vi
      .fn<BrowserAnalysisService["analyzeLocalVideo"]>()
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveA = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveB = resolve;
          }),
      );
    renderPage({
      initialA: undefined,
      initialB: undefined,
      analysisService: { analyzeLocalVideo },
    });
    fireEvent.change(screen.getByLabelText("Local video A"), {
      target: { files: [new File(["a"], "a.mp4", { type: "video/mp4" })] },
    });
    fireEvent.change(screen.getByLabelText("Local video B"), {
      target: { files: [new File(["b"], "b.mp4", { type: "video/mp4" })] },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyze both videos" }));

    expect(screen.getByLabelText("Local video A")).toBeDisabled();
    expect(screen.getByLabelText("Browser report A")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Swap A and B" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel analysis" }));

    resolveA?.(asRealReport(report("a", 10, [1, 2])));
    resolveB?.(asRealReport(report("b", 20, [3, 5])));
    await waitFor(() =>
      expect(screen.queryAllByLabelText("Selected video")).toHaveLength(0),
    );
    expect(screen.getByLabelText("Local video A")).not.toBeDisabled();
  });

  it("invalidates an in-flight report read when that slot receives a local file", async () => {
    let resolveReport:
      | ((report: BrowserReport) => void)
      | undefined;
    const parseReport = vi.fn(
      () =>
        new Promise<BrowserReport>((resolve) => {
          resolveReport = resolve;
        }),
    );
    renderPage({
      initialA: undefined,
      initialB: undefined,
      parseReport,
    });
    fireEvent.change(screen.getByLabelText("Browser report A"), {
      target: {
        files: [
          new File(["{}"], "pending.json", { type: "application/json" }),
        ],
      },
    });
    fireEvent.change(screen.getByLabelText("Local video A"), {
      target: {
        files: [new File(["video"], "new.mp4", { type: "video/mp4" })],
      },
    });
    resolveReport?.({
      ...createDemoReport("en"),
      title: "Stale imported report",
    });

    await waitFor(() =>
      expect(
        screen.queryByText("Stale imported report"),
      ).not.toBeInTheDocument(),
    );
  });

  it("keeps the longer absolute side playing after the shorter side ends", () => {
    renderPage();
    fireEvent.click(screen.getByRole("button", { name: "Play both" }));
    fireEvent.change(screen.getByRole("slider", { name: "Shared seek" }), {
      target: { value: "15" },
    });
    const videos = screen.getAllByLabelText("Selected video");
    fireEvent.pause(videos[0]!);

    const sideA = screen.getByRole("region", { name: "Video A comparison" });
    const sideB = screen.getByRole("region", { name: "Video B comparison" });
    expect(within(sideA).getByRole("button", { name: "Play" })).toBeVisible();
    expect(within(sideB).getByRole("button", { name: "Pause" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Pause both" })).toBeVisible();
  });

  it("finishes local analysis when the application runs in React Strict Mode", async () => {
    const analyzeLocalVideo = vi
      .fn<BrowserAnalysisService["analyzeLocalVideo"]>()
      .mockResolvedValueOnce(asRealReport(report("a", 10, [1, 2])))
      .mockResolvedValueOnce(asRealReport(report("b", 20, [3, 5])));
    const createObjectURL = vi
      .fn<(file: File) => string>()
      .mockReturnValueOnce("blob:strict-a")
      .mockReturnValueOnce("blob:strict-b");
    render(
      <StrictMode>
        <AppProviders>
          <MemoryRouter>
            <ComparePage
              analysisService={{ analyzeLocalVideo }}
              createObjectURL={createObjectURL}
            />
          </MemoryRouter>
        </AppProviders>
      </StrictMode>,
    );

    fireEvent.change(screen.getByLabelText("Local video A"), {
      target: {
        files: [new File(["a"], "a.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.change(screen.getByLabelText("Local video B"), {
      target: {
        files: [new File(["b"], "b.mp4", { type: "video/mp4" })],
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Analyze both videos" }));

    const videos = await screen.findAllByLabelText(
      "Selected video",
      {},
      { timeout: 1_000 },
    );
    expect(videos[0]).toHaveAttribute("src", "blob:strict-a");
  });
});
