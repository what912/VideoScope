import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TestApp } from "../../app/router";
import { I18nProvider } from "../../i18n/I18nProvider";
import { MemoryReportStore } from "../../services/report-store/memory-report-store";
import type {
  ReportIndexEntry,
  ReportStore,
} from "../../services/report-store/report-store";
import type {
  BrowserCpuDetectorExecution,
  BrowserCpuFinding,
} from "../../types/analysis";
import {
  createRealBrowserReport,
  type BrowserReport,
  type RealBrowserReport,
} from "../../types/report";
import { WorkspacePage } from "./WorkspacePage";

const findings: BrowserCpuFinding[] = [
  {
    id: "dark-interval",
    detector_id: "near_black",
    detector_version: "1",
    signal_kind: "browser_cpu",
    title: "Near-black interval detected",
    description: "A sustained low-luminance interval was observed.",
    severity: "medium",
    score: 0.72,
    confidence: 0.78,
    time_range: { start_seconds: 1, end_seconds: 2 },
    evidence: [
      {
        evidence_type: "frame",
        timestamp_seconds: 1.5,
        description: "Dark midpoint",
        thumbnail: {
          src: "media/evidence-dark.webp",
          width: 160,
          height: 90,
        },
        metadata: {},
      },
    ],
    tags: ["browser_cpu"],
    parameters: { mean_luma_threshold: 0.08 },
    limitations: ["This may be an intentional fade or night scene."],
  },
  {
    id: "freeze-interval",
    detector_id: "possible_freeze",
    detector_version: "1",
    signal_kind: "browser_cpu",
    title: "Possible frozen or repeated frames",
    description: "A sustained near-repeat sequence was observed.",
    severity: "high",
    score: 0.86,
    confidence: 0.74,
    time_range: { start_seconds: 5, end_seconds: 7 },
    evidence: [
      {
        evidence_type: "frame",
        timestamp_seconds: 6,
        description: "Freeze midpoint",
        thumbnail: {
          src: "media/evidence-freeze.webp",
          width: 160,
          height: 90,
        },
        metadata: {},
      },
    ],
    tags: ["browser_cpu"],
    parameters: { max_pixel_difference: 0.01 },
    limitations: ["A deliberately static shot can produce this signal."],
  },
];

const detectorExecutions: BrowserCpuDetectorExecution[] = [
  {
    detector_id: "near_black",
    detector_version: "1",
    signal_kind: "browser_cpu",
    status: "ok",
    elapsed_seconds: 0.02,
    findings_count: 1,
  },
  {
    detector_id: "possible_freeze",
    detector_version: "1",
    signal_kind: "browser_cpu",
    status: "ok",
    elapsed_seconds: 0.03,
    findings_count: 1,
  },
  {
    detector_id: "global_flicker",
    detector_version: "1",
    signal_kind: "browser_cpu",
    status: "failed",
    elapsed_seconds: 0.01,
    findings_count: 0,
    error_type: "DetectorError",
    error_message: "Luminance samples were unavailable.",
  },
];

function makeReport(overrides: {
  findings?: BrowserCpuFinding[];
  detectorExecutions?: BrowserCpuDetectorExecution[];
} = {}): RealBrowserReport {
  const reportFindings = overrides.findings ?? findings;
  return createRealBrowserReport({
    tool_version: "0.2.0",
    id: "workspace-report",
    analysis_id: "workspace-analysis",
    title: "Local diagnostic report",
    created_at: "2026-07-30T00:00:00.000Z",
    input_hash: "workspace-input-hash",
    metadata: {
      filename: "local-video.mp4",
      mime_type: "video/mp4",
      width: 320,
      height: 180,
      duration_seconds: 10,
      file_size_bytes: 1024,
      frame_rate: 10,
      has_audio: false,
    },
    configuration: [
      {
        detector_id: "near_black",
        detector_version: "1",
        signal_kind: "browser_cpu",
        enabled: true,
        parameters: {},
      },
      {
        detector_id: "possible_freeze",
        detector_version: "1",
        signal_kind: "browser_cpu",
        enabled: true,
        parameters: {},
      },
    ],
    detector_executions:
      overrides.detectorExecutions ?? detectorExecutions,
    findings: reportFindings,
    metrics: [
      {
        id: "dark-duration",
        label: "Near-black duration",
        value: 1,
        kind: "browser_cpu",
        detector_id: "near_black",
        unit: "seconds",
        domain: { min: 0, max: 10 },
        description: "Duration observed by this detector.",
      },
      {
        id: "freeze-duration",
        label: "Repeated-frame duration",
        value: 2,
        kind: "browser_cpu",
        detector_id: "possible_freeze",
        unit: "seconds",
        domain: { min: 0, max: 10 },
        description: "Duration observed by this detector.",
      },
    ],
    summary: {
      review_interval_count: reportFindings.length,
      severity_counts: {
        info: 0,
        low: 0,
        medium: reportFindings.filter(
          (finding) => finding.severity === "medium",
        ).length,
        high: reportFindings.filter(
          (finding) => finding.severity === "high",
        ).length,
        critical: 0,
      },
    },
    warnings: [],
    runtime: {
      environment: "browser",
      user_agent_family: "test",
      analysis_seconds: 0.12,
      sample_count: 20,
    },
    reviewed_finding_ids: [],
    preferences: {
      locale: "en",
      creator_view: true,
      reduced_motion: false,
    },
  });
}

async function renderWorkspace(
  report = makeReport(),
  options: {
    sessionVideo?: "loaded" | "missing";
    mobile?: boolean;
    hashFile?: (file: File, signal: AbortSignal) => Promise<string>;
    replaceSessionVideo?: (session: {
      reportId: string;
      file: File;
      objectUrl: string;
    }) => void;
    writeClipboard?: (value: string) => Promise<void>;
  } = {},
) {
  const store = new MemoryReportStore();
  await store.put(report);
  const writeClipboard =
    options.writeClipboard ?? vi.fn().mockResolvedValue(undefined);
  const exportReport = vi.fn();
  const clearSession = vi.fn();
  const navigate = vi.fn();
  const replaceSessionVideo = options.replaceSessionVideo ?? vi.fn();
  const sessionVideo =
    options.sessionVideo === "missing"
      ? null
      : {
          reportId: report.id,
          file: new File(["video"], "local-video.mp4", {
            type: "video/mp4",
          }),
          objectUrl: "blob:workspace-video",
        };

  render(
    <MemoryRouter initialEntries={[`/workspace?report=${report.id}`]}>
      <I18nProvider initialLocale="en">
        <WorkspacePage
          clearSession={clearSession}
          exportReport={exportReport}
          getSessionVideo={() => sessionVideo}
          hashFile={options.hashFile}
          isMobile={options.mobile ?? false}
          navigate={navigate}
          replaceSessionVideo={replaceSessionVideo}
          reportStore={store}
          writeClipboard={writeClipboard}
        />
      </I18nProvider>
    </MemoryRouter>,
  );
  await screen.findByRole("heading", { name: report.title });
  return {
    clearSession,
    exportReport,
    navigate,
    replaceSessionVideo,
    report,
    store,
    writeClipboard,
  };
}

function renderWithStore(
  store: ReportStore,
  options: {
    clearSession?: () => void;
    navigate?: (path: string) => void;
    reportId?: string;
  } = {},
) {
  const clearSession = options.clearSession ?? vi.fn();
  const navigate = options.navigate ?? vi.fn();
  render(
    <MemoryRouter
      initialEntries={[
        `/workspace?report=${options.reportId ?? "workspace-report"}`,
      ]}
    >
      <I18nProvider initialLocale="en">
        <WorkspacePage
          clearSession={clearSession}
          getSessionVideo={() => null}
          isMobile={false}
          navigate={navigate}
          reportStore={store}
        />
      </I18nProvider>
    </MemoryRouter>,
  );
  return { clearSession, navigate };
}

function passthroughStore(
  inner: MemoryReportStore,
  overrides: Partial<ReportStore>,
): ReportStore {
  return {
    put: (report: BrowserReport) => inner.put(report),
    get: (id: string) => inner.get(id),
    list: () => inner.list(),
    delete: (id: string) => inner.delete(id),
    clear: () => inner.clear(),
    usage: () => inner.usage(),
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

describe("workspace route", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
  });

  afterEach(() => vi.restoreAllMocks());

  it("distinguishes a missing report selection from an empty analysis", async () => {
    render(<TestApp initialEntries={["/workspace"]} />);

    expect(
      await screen.findByRole("heading", { name: "No report selected" }),
    ).toBeVisible();
    expect(
      screen.queryByText(
        "No observable intervals were found by the enabled detectors.",
      ),
    ).not.toBeInTheDocument();
  });

  it("distinguishes a missing saved report from no report selection", async () => {
    const store = new MemoryReportStore();
    render(
      <MemoryRouter initialEntries={["/workspace?report=missing-report"]}>
        <I18nProvider initialLocale="en">
          <WorkspacePage reportStore={store} />
        </I18nProvider>
      </MemoryRouter>,
    );

    expect(
      await screen.findByRole("heading", {
        name: "Saved report is unavailable",
      }),
    ).toBeVisible();
    expect(screen.queryByText("No report selected")).not.toBeInTheDocument();
  });

  it("synchronizes Finding, video, timeline, overlay, detail, and metric cursor", async () => {
    await renderWorkspace();

    const issueList = screen.getByRole("region", {
      name: "Review intervals",
    });
    fireEvent.click(
      within(issueList).getByRole("button", {
        name: /possible frozen or repeated frames/i,
      }),
    );

    const video = screen.getByLabelText("Selected video");
    await waitFor(() => expect(video).toHaveProperty("currentTime", 5));
    expect(screen.getByTestId("diagnostic-overlay")).toHaveTextContent(
      "Possible frozen or repeated frames",
    );
    expect(
      screen.getByRole("button", {
        name: "High: Possible frozen or repeated frames",
      }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("finding-detail")).toHaveTextContent(
      "max_pixel_difference",
    );
    expect(screen.getAllByTestId("metric-cursor")[0]).toHaveAttribute(
      "data-time",
      "5",
    );

    fireEvent.click(
      within(screen.getByTestId("finding-detail")).getByRole("button", {
        name: /freeze midpoint/i,
      }),
    );

    await waitFor(() => expect(video).toHaveProperty("currentTime", 6));
    expect(screen.getAllByTestId("metric-cursor")[0]).toHaveAttribute(
      "data-time",
      "6",
    );
  });

  it("persists reviewed IDs without adding review state to Findings", async () => {
    const { report, store } = await renderWorkspace();

    fireEvent.click(
      screen.getAllByRole("checkbox", { name: "Mark as reviewed" })[0],
    );

    await waitFor(async () => {
      expect((await store.get(report.id))?.reviewed_finding_ids).toEqual([
        "dark-interval",
      ]);
    });
    expect(report.findings[0]).not.toHaveProperty("reviewed");
  });

  it("serializes reviewed-state writes and preserves newer state after an older write fails", async () => {
    const report = makeReport();
    const inner = new MemoryReportStore();
    await inner.put(report);
    let rejectFirst!: (error: Error) => void;
    const firstWrite = new Promise<void>((_resolve, reject) => {
      rejectFirst = reject;
    });
    let writeCount = 0;
    const put = vi.fn(async (nextReport: BrowserReport) => {
      writeCount += 1;
      if (writeCount === 1) {
        await firstWrite;
        return;
      }
      await inner.put(nextReport);
    });
    const store = passthroughStore(inner, { put });
    renderWithStore(store);
    await screen.findByRole("heading", { name: report.title });

    const checkboxes = screen.getAllByRole("checkbox", {
      name: "Mark as reviewed",
    });
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);

    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    rejectFirst(new Error("older write failed"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2));
    await waitFor(async () =>
      expect((await inner.get(report.id))?.reviewed_finding_ids).toEqual([
        "dark-interval",
        "freeze-interval",
      ]),
    );
    expect(checkboxes[0]).toBeChecked();
    expect(checkboxes[1]).toBeChecked();
    expect(
      screen.getByText(
        "The reviewed state could not be saved on this device.",
      ),
    ).toBeVisible();
  });

  it("filters findings independently by detector and severity", async () => {
    await renderWorkspace();

    fireEvent.change(screen.getByLabelText("Detector filter"), {
      target: { value: "near_black" },
    });
    expect(
      screen.getByText("Near-black interval detected", { selector: "h3" }),
    ).toBeVisible();
    expect(
      screen.queryByText("Possible frozen or repeated frames", {
        selector: "h3",
      }),
    ).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Severity filter"), {
      target: { value: "high" },
    });
    expect(screen.getByText("No review intervals match these filters.")).toBeVisible();
  });

  it("does not claim no Findings when every applicable detector failed", async () => {
    const failedExecution = detectorExecutions[2];
    await renderWorkspace(
      makeReport({
        findings: [],
        detectorExecutions: [failedExecution],
      }),
      { sessionVideo: "missing" },
    );

    expect(
      screen.getByText(
        "Analysis is incomplete because no applicable detector completed successfully.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByText(
        "No observable intervals were found by the enabled detectors.",
      ),
    ).not.toBeInTheDocument();
    const detectorErrors = screen.getByRole("region", {
      name: "Detector errors",
    });
    expect(detectorErrors).toHaveTextContent("Luminance samples were unavailable.");
  });

  it("scopes an empty result to completed detectors when another detector failed", async () => {
    await renderWorkspace(
      makeReport({
        findings: [],
        detectorExecutions: [detectorExecutions[0], detectorExecutions[2]],
      }),
      { sessionVideo: "missing" },
    );

    expect(
      screen.getByText(
        "Completed detectors found no observable intervals; one or more detectors failed.",
      ),
    ).toBeVisible();
  });

  it("uses the normal no-Finding message when applicable detectors completed", async () => {
    await renderWorkspace(
      makeReport({
        findings: [],
        detectorExecutions: [detectorExecutions[0]],
      }),
      { sessionVideo: "missing" },
    );

    expect(
      screen.getByText(
        "No observable intervals were found by the enabled detectors.",
      ),
    ).toBeVisible();
  });

  it("keeps report evidence available when the session video is gone", async () => {
    await renderWorkspace(makeReport(), { sessionVideo: "missing" });

    expect(
      screen.getByRole("heading", {
        name: "Original video is no longer loaded",
      }),
    ).toBeVisible();
    expect(screen.getByText("320 × 180")).toBeVisible();
    expect(screen.getAllByAltText("Dark midpoint")[0]).toBeVisible();
    expect(
      screen.getByLabelText("Reselect original video"),
    ).toHaveAttribute("type", "file");
  });

  it("hashes a reselected video and rejects a file that does not match the report", async () => {
    const hashFile = vi.fn().mockResolvedValue("different-input-hash");
    const replaceSessionVideo = vi.fn();
    await renderWorkspace(makeReport(), {
      hashFile,
      replaceSessionVideo,
      sessionVideo: "missing",
    });
    const file = new File(["different-video"], "candidate.mp4", {
      type: "video/mp4",
    });

    fireEvent.change(screen.getByLabelText("Reselect original video"), {
      target: { files: [file] },
    });

    await waitFor(() => expect(hashFile).toHaveBeenCalledWith(file, expect.any(AbortSignal)));
    expect(replaceSessionVideo).not.toHaveBeenCalled();
    const mismatch = screen.getByRole("alert");
    expect(mismatch).toHaveTextContent(
      "This file does not match the video used for this report.",
    );
    expect(mismatch).toHaveAttribute("data-status", "error");
    expect(
      screen.getByRole("heading", {
        name: "Original video is no longer loaded",
      }),
    ).toBeVisible();
  });

  it("aborts a deferred reselect and cannot attach the old video after switching reports", async () => {
    const firstReport = makeReport();
    const secondReport: RealBrowserReport = {
      ...makeReport(),
      id: "workspace-report-two",
      analysis_id: "workspace-analysis-two",
      input_hash: "workspace-input-hash-two",
      title: "Second local diagnostic report",
    };
    const store = new MemoryReportStore();
    await store.put(firstReport);
    await store.put(secondReport);
    const hash = deferred<string>();
    let hashSignal: AbortSignal | undefined;
    const hashFile = vi.fn((_file: File, signal: AbortSignal) => {
      hashSignal = signal;
      return hash.promise;
    });
    const replaceSessionVideo = vi.fn();
    render(
      <MemoryRouter
        initialEntries={[`/workspace?report=${firstReport.id}`]}
      >
        <I18nProvider initialLocale="en">
          <WorkspacePage
            getSessionVideo={() => null}
            hashFile={hashFile}
            isMobile={false}
            replaceSessionVideo={replaceSessionVideo}
            reportStore={store}
          />
        </I18nProvider>
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { name: firstReport.title });
    fireEvent.change(screen.getByLabelText("Reselect original video"), {
      target: {
        files: [
          new File(["first-video"], "first.mp4", { type: "video/mp4" }),
        ],
      },
    });
    await waitFor(() => expect(hashFile).toHaveBeenCalledOnce());

    fireEvent.click(
      within(
        screen.getByRole("complementary", {
          name: "Saved report drawer",
        }),
      ).getByRole("button", { name: new RegExp(secondReport.title) }),
    );
    await screen.findByRole("heading", { name: secondReport.title });
    hash.resolve(firstReport.input_hash);

    await waitFor(() => expect(hashSignal?.aborted).toBe(true));
    expect(replaceSessionVideo).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("Selected video")).not.toBeInTheDocument();
  });

  it("aborts a deferred reselect and cannot restore a session after local data is cleared", async () => {
    const hash = deferred<string>();
    let hashSignal: AbortSignal | undefined;
    const hashFile = vi.fn((_file: File, signal: AbortSignal) => {
      hashSignal = signal;
      return hash.promise;
    });
    const replaceSessionVideo = vi.fn();
    await renderWorkspace(makeReport(), {
      hashFile,
      replaceSessionVideo,
      sessionVideo: "missing",
    });
    fireEvent.change(screen.getByLabelText("Reselect original video"), {
      target: {
        files: [
          new File(["candidate"], "candidate.mp4", { type: "video/mp4" }),
        ],
      },
    });
    await waitFor(() => expect(hashFile).toHaveBeenCalledOnce());

    fireEvent.click(screen.getByRole("button", { name: "Clear local data" }));
    fireEvent.click(
      within(
        screen.getByRole("dialog", { name: "Clear all local reports?" }),
      ).getByRole("button", { name: "Clear local data" }),
    );
    await screen.findByRole("heading", { name: "Saved report is unavailable" });
    hash.resolve("workspace-input-hash");

    await waitFor(() => expect(hashSignal?.aborted).toBe(true));
    expect(replaceSessionVideo).not.toHaveBeenCalled();
  });

  it("resets the reselect input so the same local file can be retried", async () => {
    const hash = deferred<string>();
    const hashFile = vi.fn(() => hash.promise);
    await renderWorkspace(makeReport(), {
      hashFile,
      sessionVideo: "missing",
    });
    const input = screen.getByLabelText(
      "Reselect original video",
    ) as HTMLInputElement;
    const file = new File(["candidate"], "candidate.mp4", {
      type: "video/mp4",
    });
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [file],
    });
    Object.defineProperty(input, "value", {
      configurable: true,
      value: "candidate.mp4",
      writable: true,
    });

    fireEvent.change(input);

    expect(input.value).toBe("");
    hash.resolve("different-input-hash");
  });

  it("restores playback only after a reselected video's hash matches", async () => {
    const report = makeReport();
    const hashFile = vi.fn().mockResolvedValue(report.input_hash);
    const replaceSessionVideo = vi.fn();
    const createObjectURL = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:matched-video");
    await renderWorkspace(report, {
      hashFile,
      replaceSessionVideo,
      sessionVideo: "missing",
    });
    const file = new File(["same-video"], "candidate.mp4", {
      type: "video/mp4",
    });

    fireEvent.change(screen.getByLabelText("Reselect original video"), {
      target: { files: [file] },
    });

    await waitFor(() =>
      expect(replaceSessionVideo).toHaveBeenCalledWith({
        reportId: report.id,
        file,
        objectUrl: "blob:matched-video",
      }),
    );
    expect(createObjectURL).toHaveBeenCalledWith(file);
    expect(
      screen.queryByRole("heading", {
        name: "Original video is no longer loaded",
      }),
    ).not.toBeInTheDocument();
  });

  it("uses a mobile bottom sheet for the selected Finding", async () => {
    await renderWorkspace(makeReport(), { mobile: true });

    fireEvent.click(
      within(
        screen.getByRole("region", { name: "Review intervals" }),
      ).getByRole("button", {
        name: /possible frozen or repeated frames/i,
      }),
    );

    expect(
      screen.getByRole("dialog", { name: "Finding details" }),
    ).toHaveAttribute("data-presentation", "bottom-sheet");
    expect(document.querySelector(".workspace-modal-backdrop")).not.toBeNull();
    expect(screen.getByTestId("workspace-surface")).toHaveAttribute(
      "inert",
    );
    expect(screen.getByTestId("workspace-surface")).toHaveAttribute(
      "aria-hidden",
      "true",
    );
  });

  it("moves focus into the mobile detail sheet and restores it on Escape", async () => {
    await renderWorkspace(makeReport(), { mobile: true });
    const trigger = within(
      screen.getByRole("region", { name: "Review intervals" }),
    ).getByRole("button", {
      name: /possible frozen or repeated frames/i,
    });
    trigger.focus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole("dialog", { name: "Finding details" });
    const close = within(dialog).getByRole("button", {
      name: "Close Finding details",
    });
    await waitFor(() => expect(close).toHaveFocus());
    const surface = screen.getByTestId("workspace-surface");
    const originalFocus = trigger.focus.bind(trigger);
    let inertWhenFocusWasRestored: boolean | undefined;
    vi.spyOn(trigger, "focus").mockImplementation(() => {
      inertWhenFocusWasRestored = surface.hasAttribute("inert");
      originalFocus();
    });

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(
      screen.queryByRole("dialog", { name: "Finding details" }),
    ).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(inertWhenFocusWasRestored).toBe(false);
  });

  it("opens and closes the mobile project drawer without trapping focus behind it", async () => {
    await renderWorkspace(makeReport(), { mobile: true });
    const trigger = screen.getByRole("button", { name: "Projects" });
    trigger.focus();
    fireEvent.click(trigger);

    const drawer = screen.getByRole("dialog", {
      name: "Saved report drawer",
    });
    const close = within(drawer).getByRole("button", {
      name: "Close projects",
    });
    await waitFor(() => expect(close).toHaveFocus());
    const surface = screen.getByTestId("workspace-surface");
    const originalFocus = trigger.focus.bind(trigger);
    let inertWhenFocusWasRestored: boolean | undefined;
    vi.spyOn(trigger, "focus").mockImplementation(() => {
      inertWhenFocusWasRestored = surface.hasAttribute("inert");
      originalFocus();
    });
    fireEvent.keyDown(drawer, { key: "Escape" });

    expect(
      screen.queryByRole("dialog", {
        name: "Saved report drawer",
      }),
    ).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(inertWhenFocusWasRestored).toBe(false);
  });

  it("closes the mobile project drawer after a report is selected", async () => {
    const { navigate, report } = await renderWorkspace(makeReport(), {
      mobile: true,
    });
    fireEvent.click(screen.getByRole("button", { name: "Projects" }));
    const drawer = screen.getByRole("dialog", {
      name: "Saved report drawer",
    });

    fireEvent.click(
      within(drawer).getByRole("button", { name: new RegExp(report.title) }),
    );

    expect(navigate).toHaveBeenCalledWith(
      `/workspace?report=${encodeURIComponent(report.id)}`,
    );
    expect(
      screen.queryByRole("dialog", { name: "Saved report drawer" }),
    ).not.toBeInTheDocument();
  });

  it("switches to a two-column desktop grid when the project rail closes", async () => {
    await renderWorkspace();
    const grid = screen.getByTestId("workspace-grid");
    expect(grid).toHaveAttribute("data-rail-open", "true");

    fireEvent.click(screen.getByRole("button", { name: "Projects" }));

    expect(grid).toHaveAttribute("data-rail-open", "false");
  });

  it("shows a recoverable storage error and retries loading", async () => {
    const report = makeReport();
    const inner = new MemoryReportStore();
    await inner.put(report);
    const get = vi
      .fn<(id: string) => Promise<BrowserReport | null>>()
      .mockRejectedValueOnce(new Error("storage unavailable"))
      .mockImplementation((id) => inner.get(id));
    const list = vi
      .fn<() => Promise<ReportIndexEntry[]>>()
      .mockRejectedValueOnce(new Error("storage unavailable"))
      .mockImplementation(() => inner.list());
    const store = passthroughStore(inner, { get, list });
    const { navigate } = renderWithStore(store);

    expect(
      await screen.findByRole("heading", {
        name: "Local reports could not be loaded",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "Browser storage could not be read. Retry or start a new analysis.",
      ),
    ).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "New analysis" }));
    expect(navigate).toHaveBeenCalledWith("/");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: report.title })).toBeVisible();
  });

  it("shows clipboard rejection instead of leaving a silent failure", async () => {
    const writeClipboard = vi.fn().mockRejectedValue(new Error("denied"));
    await renderWorkspace(makeReport(), { writeClipboard });

    fireEvent.click(screen.getByRole("button", { name: "Copy timestamp" }));

    const failure = await screen.findByRole("alert");
    expect(failure).toHaveTextContent("The timestamp could not be copied.");
    expect(failure).toHaveAttribute("data-status", "error");
  });

  it("renders each failed detector once", async () => {
    await renderWorkspace();

    expect(screen.getAllByText("global_flicker")).toHaveLength(1);
  });

  it("offers frame, playback, copy, export, new-analysis, and clear actions without an overall score", async () => {
    const { clearSession, exportReport, navigate, report, store, writeClipboard } =
      await renderWorkspace();

    fireEvent.change(screen.getByLabelText("Playback speed"), {
      target: { value: "1.5" },
    });
    expect(
      within(screen.getByTestId("video-player").parentElement!).getByText(
        "1.50×",
      ),
    ).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Next frame" }));
    expect(screen.getAllByTestId("metric-cursor")[0]).toHaveAttribute(
      "data-time",
      "0.1",
    );
    fireEvent.click(screen.getByRole("button", { name: "Copy timestamp" }));
    expect(writeClipboard).toHaveBeenCalledWith("00:00.100");
    const copyStatus = await screen.findByRole("status");
    expect(copyStatus).toHaveTextContent("Timestamp copied.");
    expect(copyStatus).toHaveAttribute("data-status", "success");

    fireEvent.click(screen.getByRole("button", { name: "Export JSON" }));
    expect(exportReport).toHaveBeenCalledWith(
      expect.objectContaining({ id: report.id }),
    );
    fireEvent.click(screen.getByRole("button", { name: "New analysis" }));
    expect(navigate).toHaveBeenCalledWith("/");

    const clearButton = screen.getByRole("button", {
      name: "Clear local data",
    });
    fireEvent.click(clearButton);
    const confirmation = screen.getByRole("dialog", {
      name: "Clear all local reports?",
    });
    const cancel = within(confirmation).getByRole("button", { name: "Cancel" });
    await waitFor(() => expect(cancel).toHaveFocus());
    expect(await store.get(report.id)).not.toBeNull();
    fireEvent.click(cancel);
    expect(
      screen.queryByRole("dialog", { name: "Clear all local reports?" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(clearButton).toHaveFocus());
    expect(await store.get(report.id)).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Clear local data" }));
    fireEvent.click(
      within(
        screen.getByRole("dialog", { name: "Clear all local reports?" }),
      ).getByRole("button", {
        name: "Clear local data",
      }),
    );
    await waitFor(async () => expect(await store.get(report.id)).toBeNull());
    expect(clearSession).toHaveBeenCalled();
    expect(screen.queryByText(/overall score/i)).not.toBeInTheDocument();
  });

  it("uses clear as a mutation barrier so queued reviewed writes cannot reinsert a report", async () => {
    const report = makeReport();
    const inner = new MemoryReportStore();
    await inner.put(report);
    const firstWriteGate = deferred<void>();
    const firstWriteFinished = deferred<void>();
    let putCount = 0;
    const put = vi.fn(async (nextReport: BrowserReport) => {
      putCount += 1;
      if (putCount === 1) {
        await firstWriteGate.promise;
      }
      await inner.put(nextReport);
      if (putCount === 1) firstWriteFinished.resolve();
    });
    const store = passthroughStore(inner, { put });
    renderWithStore(store);
    await screen.findByRole("heading", { name: report.title });
    const checkboxes = screen.getAllByRole("checkbox", {
      name: "Mark as reviewed",
    });
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Clear local data" }));
    fireEvent.click(
      within(
        screen.getByRole("dialog", { name: "Clear all local reports?" }),
      ).getByRole("button", { name: "Clear local data" }),
    );
    firstWriteGate.resolve();
    await firstWriteFinished.promise;
    await screen.findByRole("heading", { name: "Saved report is unavailable" });

    expect(await inner.get(report.id)).toBeNull();
    expect(put).toHaveBeenCalledTimes(2);
  });

  it("persists every preexisting reviewed mutation when a queued clear rejects", async () => {
    const report = makeReport();
    const inner = new MemoryReportStore();
    await inner.put(report);
    const firstWriteGate = deferred<void>();
    let putCount = 0;
    const put = vi.fn(async (nextReport: BrowserReport) => {
      putCount += 1;
      if (putCount === 1) await firstWriteGate.promise;
      await inner.put(nextReport);
    });
    const clear = vi.fn().mockRejectedValue(new Error("quota failure"));
    const store = passthroughStore(inner, { clear, put });
    renderWithStore(store);
    await screen.findByRole("heading", { name: report.title });
    const checkboxes = screen.getAllByRole("checkbox", {
      name: "Mark as reviewed",
    });
    fireEvent.click(checkboxes[0]);
    fireEvent.click(checkboxes[1]);
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Clear local data" }));
    const confirmation = screen.getByRole("dialog", {
      name: "Clear all local reports?",
    });
    fireEvent.click(
      within(confirmation).getByRole("button", { name: "Clear local data" }),
    );
    firstWriteGate.resolve();

    expect(
      await within(confirmation).findByText(
        "Local data could not be cleared. Your saved reports remain available.",
      ),
    ).toBeVisible();
    expect(clear).toHaveBeenCalledOnce();
    expect(put).toHaveBeenCalledTimes(2);
    expect((await inner.get(report.id))?.reviewed_finding_ids).toEqual([
      "dark-interval",
      "freeze-interval",
    ]);
    expect(checkboxes[0]).toBeChecked();
    expect(checkboxes[1]).toBeChecked();
  });

  it("keeps the report and surfaces an error when clearing local data fails", async () => {
    const report = makeReport();
    const inner = new MemoryReportStore();
    await inner.put(report);
    const store = passthroughStore(inner, {
      clear: vi.fn().mockRejectedValue(new Error("quota failure")),
    });
    const { clearSession } = renderWithStore(store);
    await screen.findByRole("heading", { name: report.title });

    fireEvent.click(screen.getByRole("button", { name: "Clear local data" }));
    const confirmation = screen.getByRole("dialog", {
      name: "Clear all local reports?",
    });
    fireEvent.click(
      within(confirmation).getByRole("button", {
        name: "Clear local data",
      }),
    );

    expect(
      await within(confirmation).findByText(
        "Local data could not be cleared. Your saved reports remain available.",
      ),
    ).toBeVisible();
    expect(await inner.get(report.id)).not.toBeNull();
    expect(clearSession).not.toHaveBeenCalled();
  });
});
