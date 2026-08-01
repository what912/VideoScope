import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/AppProviders";
import { createDemoReport } from "../../data/demo-report";
import { I18nProvider } from "../../i18n/I18nProvider";
import { FakeAuthClient } from "../../services/auth";
import { MemoryReportStore } from "../../services/report-store/memory-report-store";
import {
  FakeShareClient,
  sanitizeReportForShare,
} from "../../services/share";
import { createRealBrowserReport } from "../../types/report";
import type {
  BrowserCpuDetectorConfiguration,
  BrowserCpuDetectorExecution,
  BrowserCpuFinding,
  BrowserCpuQualityMetric,
} from "../../types/analysis";
import type { RealBrowserReport } from "../../types/report";
import {
  downloadReportJson,
  ReportPage,
  serializeReportForExport,
} from "./ReportPage";

function makeRealReport(
  overrides: Partial<RealBrowserReport> = {},
): RealBrowserReport {
  const demo = createDemoReport("en");
  const configuration = demo.configuration
    .filter(
      (
        item,
      ): item is BrowserCpuDetectorConfiguration =>
        item.signal_kind === "browser_cpu",
    )
    .map((item) => structuredClone(item));
  const detectorExecutions = demo.detector_executions
    .filter(
      (
        item,
      ): item is BrowserCpuDetectorExecution =>
        item.signal_kind === "browser_cpu",
    )
    .map((item) => structuredClone(item));
  const findings = demo.findings
    .filter(
      (item): item is BrowserCpuFinding =>
        item.signal_kind === "browser_cpu",
    )
    .map((finding) => ({
      ...structuredClone(finding),
      tags: ["browser-cpu"],
      evidence: finding.evidence.map((evidence) => ({
        ...structuredClone(evidence),
        metadata: { residual_peak: 0.31 },
      })),
    }));
  const metrics = demo.metrics
    .filter(
      (item): item is BrowserCpuQualityMetric => item.kind === "browser_cpu",
    )
    .map((item) => structuredClone(item));

  const report = createRealBrowserReport({
    tool_version: "0.2.0",
    id: "local-report",
    analysis_id: "local-analysis",
    title: "Local observatory report",
    created_at: "2026-07-30T08:00:00.000Z",
    input_hash: "a".repeat(64),
    prompt: "一只白色的猫",
    metadata: {
      filename: "local-video.mp4",
      mime_type: "video/mp4",
      width: 1280,
      height: 720,
      duration_seconds: 18,
      file_size_bytes: 1024,
      frame_rate: 24,
      has_audio: false,
    },
    configuration,
    detector_executions: detectorExecutions,
    findings,
    metrics,
    summary: {
      review_interval_count: findings.length,
      severity_counts: {
        info: 0,
        low: 0,
        medium: findings.length,
        high: 0,
        critical: 0,
      },
    },
    warnings: ["Browser metadata is an observable preview."],
    runtime: {
      environment: "browser",
      user_agent_family: "test",
      analysis_seconds: 0.42,
      sample_count: 36,
    },
    reviewed_finding_ids: [],
    preferences: {
      locale: "en",
      creator_view: true,
      reduced_motion: false,
    },
  });
  return { ...report, ...structuredClone(overrides) } as RealBrowserReport;
}

function renderReport(options: {
  reportId: string;
  reportStore?: MemoryReportStore;
  locale?: "en" | "zh-CN";
  printReport?: () => void;
  downloadReport?: (report: RealBrowserReport) => void;
}) {
  return render(
    <I18nProvider initialLocale={options.locale ?? "en"}>
      <MemoryRouter>
        <ReportPage
          downloadReport={options.downloadReport}
          printReport={options.printReport}
          reportId={options.reportId}
          reportStore={options.reportStore}
        />
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("ReportPage", () => {
  it("opens the explicit sanitized sharing consent from report actions", async () => {
    const store = new MemoryReportStore();
    const report = makeRealReport();
    await store.put(report);
    const shareClient = new FakeShareClient();
    const authClient = new FakeAuthClient({
      initialSession: { user: { id: "owner-123" } },
    });

    render(
      <AppProviders authClient={authClient} initialLocale="en">
        <MemoryRouter>
          <ReportPage
            reportId={report.id}
            reportStore={store}
            shareClient={shareClient}
            shareEnabled
          />
        </MemoryRouter>
      </AppProviders>,
    );

    fireEvent.click(
      await screen.findByRole("button", { name: "Share sanitized report" }),
    );
    expect(
      screen.getByRole("dialog", { name: "Share a sanitized report" }),
    ).toBeInTheDocument();
    expect(shareClient.requests).toHaveLength(0);
  });

  it("loads a sanitized public report through RPC mode without consulting local storage", async () => {
    const localStore = new MemoryReportStore();
    const localReport = makeRealReport({ title: "Private local fallback" });
    await localStore.put(localReport);
    const getLocal = vi.spyOn(localStore, "get");
    const shareClient = new FakeShareClient();
    const shared = sanitizeReportForShare(localReport, {
      includePrompt: false,
      reportTitle: "Public team review",
      selectedEvidence: new Set(),
    });
    shareClient.seed("public-report-id", shared);

    render(
      <AppProviders
        authClient={new FakeAuthClient()}
        initialLocale="en"
      >
        <MemoryRouter
          initialEntries={["/report/public-report-id?shared=1"]}
        >
          <ReportPage
            reportId="public-report-id"
            reportStore={localStore}
            shareClient={shareClient}
            shareEnabled
          />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(
      await screen.findByRole("heading", { name: "Public team review" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Shared sanitized report")).toBeInTheDocument();
    expect(screen.queryByText("Private local fallback")).not.toBeInTheDocument();
    expect(getLocal).not.toHaveBeenCalled();
    expect(shareClient.readPublicIds).toEqual(["public-report-id"]);
  });

  it("shows a missing, revoked, or expired public report state with no local fallback", async () => {
    const localStore = new MemoryReportStore();
    await localStore.put(makeRealReport({ id: "missing-shared" }));
    const getLocal = vi.spyOn(localStore, "get");
    const shareClient = new FakeShareClient();

    render(
      <AppProviders
        authClient={new FakeAuthClient()}
        initialLocale="en"
      >
        <MemoryRouter initialEntries={["/report/missing-shared?shared=1"]}>
          <ReportPage
            reportId="missing-shared"
            reportStore={localStore}
            shareClient={shareClient}
            shareEnabled
          />
        </MemoryRouter>
      </AppProviders>,
    );

    expect(
      await screen.findByRole("heading", {
        name: "Shared report unavailable",
      }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/missing, revoked, or expired/i),
    ).toBeInTheDocument();
    expect(getLocal).not.toHaveBeenCalled();
  });

  beforeEach(() => {
    window.localStorage.clear();
  });

  it("loads a saved local report by its local identifier", async () => {
    const store = new MemoryReportStore();
    await store.put(makeRealReport());

    renderReport({ reportId: "local-report", reportStore: store });

    expect(
      await screen.findByRole("heading", { name: "Local observatory report" }),
    ).toBeVisible();
    expect(
      screen.getByLabelText("Temporal Flicker, 00:03.200–00:04.100"),
    ).toBeVisible();
    expect(screen.getByText("Review first")).toBeVisible();
  });

  it("loads only the explicit demo identifier as a visibly labelled demo", async () => {
    const store = new MemoryReportStore();

    renderReport({ reportId: "demo", reportStore: store });

    expect(
      await screen.findByRole("heading", {
        name: "Video Observatory interactive demonstration",
      }),
    ).toBeVisible();
    expect(screen.getByText("INTERACTIVE DEMO")).toBeVisible();
  });

  it("shows a missing-report state instead of falling back to demo data", async () => {
    renderReport({
      reportId: "does-not-exist",
      reportStore: new MemoryReportStore(),
    });

    expect(
      await screen.findByRole("heading", { name: "Report not found" }),
    ).toBeVisible();
    expect(
      screen.queryByText("Interactive VideoScope diagnosis"),
    ).not.toBeInTheDocument();
  });

  it("switches from creator guidance to research diagnostics on the same report", async () => {
    const store = new MemoryReportStore();
    const report = makeRealReport({
      runtime: {
        environment: "browser",
        user_agent_family: "test",
        analysis_seconds: 1.25,
        sample_count: 48,
        detector_diagnostics: {
          global_flicker: { peak_timestamp_seconds: 3.6 },
        },
      } as RealBrowserReport["runtime"],
    });
    await store.put(report);

    renderReport({ reportId: report.id, reportStore: store });

    expect(await screen.findByText("Review order")).toBeVisible();
    expect(screen.getByText("Limitations")).toBeVisible();
    expect(screen.queryByText("Detector ID")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Research View" }));

    expect(screen.getByText("Detector ID")).toBeVisible();
    expect(screen.getByText("Detector version")).toBeVisible();
    expect(screen.getByText("Configuration")).toBeVisible();
    expect(screen.getByText("Confidence")).toBeVisible();
    expect(screen.getByText("Raw diagnostic summaries")).toBeVisible();
    expect(screen.getByText("Runtime")).toBeVisible();
    expect(screen.getByText("Warnings")).toBeVisible();
    expect(screen.getByText("0.1-browser")).toBeVisible();
  });

  it("shows a detector failure separately from a no-Findings result", async () => {
    const store = new MemoryReportStore();
    const report = makeRealReport({
      detector_executions: [
        {
          detector_id: "scene_relative_blur",
          detector_version: "browser-1",
          signal_kind: "browser_cpu",
          status: "failed",
          elapsed_seconds: 0.1,
          findings_count: 0,
          error_type: "DecodeError",
          error_message: "<b>Frame decode stopped</b>",
        },
      ],
      findings: [],
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
    });
    await store.put(report);

    renderReport({ reportId: report.id, reportStore: store });

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("Detector error");
    expect(failure).toHaveTextContent("<b>Frame decode stopped</b>");
    expect(failure.querySelector("b")).toBeNull();
    expect(screen.queryByText("No issues found")).not.toBeInTheDocument();
  });

  it("renders HTML-sensitive report strings as text", async () => {
    const store = new MemoryReportStore();
    const report = makeRealReport({
      title: "<img src=x onerror=alert(1)>",
    });
    await store.put(report);

    renderReport({ reportId: report.id, reportStore: store });

    expect(
      await screen.findByRole("heading", {
        name: "<img src=x onerror=alert(1)>",
      }),
    ).toBeVisible();
    expect(document.querySelector('img[src="x"]')).toBeNull();
  });

  it("downloads the current local report and labels browser printing truthfully", async () => {
    const store = new MemoryReportStore();
    const report = makeRealReport();
    await store.put(report);
    const printReport = vi.fn();
    const downloadReport = vi.fn();

    renderReport({
      reportId: report.id,
      reportStore: store,
      printReport,
      downloadReport,
    });

    fireEvent.click(await screen.findByRole("button", { name: "Download JSON" }));
    expect(downloadReport).toHaveBeenCalledWith(
      expect.objectContaining({ id: report.id }),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Print / Save as PDF" }),
    );
    expect(printReport).toHaveBeenCalledOnce();
    expect(screen.queryByText("PDF export")).not.toBeInTheDocument();
  });

  it("keeps the key report controls and review guidance available in Simplified Chinese", async () => {
    const store = new MemoryReportStore();
    const report = makeRealReport();
    await store.put(report);

    renderReport({
      locale: "zh-CN",
      reportId: report.id,
      reportStore: store,
    });

    expect(
      await screen.findByRole("heading", { name: "Local observatory report" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "创作者视图" })).toBeVisible();
    expect(screen.getByRole("button", { name: "研究视图" })).toBeVisible();
    expect(screen.getByRole("button", { name: "下载 JSON" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: "打印 / 另存为 PDF" }),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "复核顺序" })).toBeVisible();
    expect(
      screen.getByText(
        "浏览器分析是本地预览；元数据和时间定位可能与桌面端 FFmpeg 流程存在差异。",
      ),
    ).toBeVisible();
  });
});

describe("serializeReportForExport", () => {
  it("produces UTF-8 JSON without absolute paths or media object URLs", async () => {
    const unsafe = makeRealReport({
      title: "中文诊断报告",
      metadata: {
        ...makeRealReport().metadata,
        filename: "C:\\Users\\person\\private-video.mp4",
      },
      findings: makeRealReport().findings.map((finding) => ({
        ...finding,
        evidence: finding.evidence.map((evidence) => ({
          ...evidence,
          thumbnail: {
            src: "blob:https://videoscope.example/original-media",
            width: 480,
            height: 270,
          },
        })),
      })),
    });

    const blob = serializeReportForExport(unsafe);
    const json = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => resolve(String(reader.result)));
      reader.addEventListener("error", () => reject(reader.error));
      reader.readAsText(blob, "utf-8");
    });

    expect(blob.type).toBe("application/json;charset=utf-8");
    expect(json).toContain("中文诊断报告");
    expect(json).not.toContain("C:\\\\Users");
    expect(json).not.toContain("blob:");
    expect(JSON.parse(json)).toMatchObject({
      schema_version: "0.1-browser",
      source: "real",
      metadata: { filename: "local-video" },
    });
  });

  it("recursively removes local references from nested keys, values, arrays, and evidence", async () => {
    const report = makeRealReport();
    const finding = report.findings[0];
    expect(finding).toBeDefined();
    const unsafe = {
      ...report,
      findings: [
        {
          ...finding!,
          parameters: {
            safe_value: "keep-me",
            nested: {
              "C:\\Users\\person\\secret-key": "hidden-key-value",
              "/home/person/secret-key": "hidden-posix-key-value",
              "blob:https://local.invalid/secret-key":
                "hidden-blob-key-value",
              "file:///Users/person/secret-key": "hidden-file-key-value",
              "data:image/png;base64,secret-key":
                "hidden-data-key-value",
              "data:text/plain,secret-key": "hidden-text-data-key-value",
              "prefixdata:application/json,secret-key":
                "hidden-application-data-key-value",
              values: [
                "safe-array-value",
                "metadata: verified",
                "prefixblob:https://local.invalid/session-video",
                "prefixfile:///C:/Users/person/private.mp4",
                "private=data:text/plain,private-caption",
                "prefixdata:application/octet-stream;base64,cHJpdmF0ZQ==",
                "prefixdata:video/mp4;base64,cHJpdmF0ZQ==",
                "prefixC:\\Users\\person\\private.mp4",
                "private=/home/person/private.mp4",
              ],
            },
          },
          evidence: finding!.evidence.map((evidence) => ({
            ...evidence,
            thumbnail: {
              src: "data:image/png;base64,iVBORw0KGgo=",
              width: 1,
              height: 1,
            },
            metadata: {
              safe_metric: 0.31,
              deep: {
                object_value:
                  "private=blob:https://local.invalid/evidence-object",
                array_value: [
                  "safe-evidence-value",
                  "private=/Users/person/evidence.png",
                ],
              },
            },
          })),
        },
      ],
    } as RealBrowserReport;

    const blob = serializeReportForExport(unsafe);
    const json = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.addEventListener("load", () => resolve(String(reader.result)));
      reader.addEventListener("error", () => reject(reader.error));
      reader.readAsText(blob, "utf-8");
    });
    const exported = JSON.parse(json) as RealBrowserReport;

    expect(json).not.toMatch(
      /data:(?:image|text|application|video)|blob:|file:/i,
    );
    expect(json).not.toContain("C:\\\\Users");
    expect(json).not.toContain("/home/person");
    expect(json).not.toContain("/Users/person");
    expect(json).not.toContain("secret-key");
    expect(json).not.toContain("private.mp4");
    expect(json).not.toContain("evidence-object");
    expect(exported.findings[0]?.parameters).toMatchObject({
      safe_value: "keep-me",
    });
    expect(json).toContain("safe-array-value");
    expect(json).toContain("safe-evidence-value");
    expect(json).toContain("metadata: verified");
    expect(exported.findings[0]?.evidence[0]?.thumbnail).toBeUndefined();
  });
});

describe("downloadReportJson", () => {
  it("clicks an attached anchor, removes it, and revokes the URL on the next task", () => {
    vi.useFakeTimers();
    const createObjectURL = vi.fn(() => "blob:download-url");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL,
      revokeObjectURL,
    });
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(function (this: HTMLAnchorElement) {
        expect(document.body).toContainElement(this);
      });

    downloadReportJson(makeRealReport());

    expect(click).toHaveBeenCalledOnce();
    expect(document.querySelector('a[download="local-report.json"]')).toBeNull();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    vi.runOnlyPendingTimers();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:download-url");

    click.mockRestore();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });
});
