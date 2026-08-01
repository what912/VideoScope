import { describe, expect, it, vi } from "vitest";

import { demoReport } from "../../data/demo-report";
import type {
  BrowserCpuDetectorConfiguration,
  BrowserCpuDetectorExecution,
  BrowserCpuFinding,
  Finding,
  QualityMetric,
} from "../../types/analysis";
import {
  createRealBrowserReport,
  type BrowserReport,
  type RealBrowserReportInput,
} from "../../types/report";
import {
  MAX_PERSISTED_THUMBNAIL_BYTES,
  MAX_PERSISTED_THUMBNAILS,
  type ReportDatabase,
  type ReportStore,
  createReportStore,
} from "./report-store";
import { IndexedDBReportStore } from "./indexeddb-report-store";
import { MemoryReportStore } from "./memory-report-store";

class FakeIndexedDB implements ReportDatabase {
  readonly #reports = new Map<string, BrowserReport>();

  async put(report: BrowserReport) {
    this.#reports.set(report.id, structuredClone(report));
  }

  async get(id: string) {
    const report = this.#reports.get(id);
    return report ? structuredClone(report) : null;
  }

  async getAll() {
    return [...this.#reports.values()].map((report) =>
      structuredClone(report),
    );
  }

  async delete(id: string) {
    this.#reports.delete(id);
  }

  async clear() {
    this.#reports.clear();
  }
}

function reportAt(
  id: string,
  createdAt: string,
  title = `Report ${id}`,
): BrowserReport {
  return createRealBrowserReport({
    ...realReportInput(),
    id,
    analysis_id: `analysis-${id}`,
    title,
    created_at: createdAt,
  });
}

function realReportInput(): RealBrowserReportInput {
  const finding = structuredClone(demoReport.findings[0]);
  if (finding.signal_kind !== "browser_cpu") {
    throw new TypeError("Expected the first demo finding to be browser CPU");
  }
  finding.tags = ["browser_cpu"];
  finding.evidence = finding.evidence.map((evidence) => ({
    ...evidence,
    metadata: {},
  }));
  const findings: BrowserCpuFinding[] = [finding];
  const detectorIds = new Set(findings.map((finding) => finding.detector_id));
  return {
    tool_version: demoReport.tool_version,
    id: "real-browser-report",
    analysis_id: "real-browser-analysis",
    title: "Real browser report",
    created_at: "2026-07-30T10:00:00.000Z",
    input_hash: "real-input-hash",
    metadata: structuredClone(demoReport.metadata),
    findings,
    metrics: demoReport.metrics.filter(
      (metric) => metric.kind === "browser_cpu",
    ),
    configuration: demoReport.configuration.filter(
      (
        configuration,
      ): configuration is BrowserCpuDetectorConfiguration =>
        configuration.signal_kind === "browser_cpu" &&
        detectorIds.has(configuration.detector_id),
    ),
    detector_executions: demoReport.detector_executions.filter(
      (execution): execution is BrowserCpuDetectorExecution =>
        execution.signal_kind === "browser_cpu" &&
        detectorIds.has(execution.detector_id),
    ),
    summary: {
      review_interval_count: findings.length,
      severity_counts: {
        info: 0,
        low: 0,
        medium: 1,
        high: 0,
        critical: 0,
      },
    },
    warnings: [
      "Browser analysis has limited codec and timestamp visibility.",
    ],
    runtime: {
      environment: "browser",
      user_agent_family: "test",
      analysis_seconds: 0.18,
      sample_count: 36,
    },
    reviewed_finding_ids: [],
    preferences: {
      locale: "en",
      creator_view: true,
      reduced_motion: false,
    },
  };
}

interface FakeNativeOptions {
  open?: "success" | "blocked" | "error";
  lateAbort?: boolean;
}

function fakeNativeIndexedDB(options: FakeNativeOptions = {}) {
  const state = {
    objectStoreCreated: false,
    transactionCalls: 0,
  };
  const openMode = options.open ?? "success";

  const database = {
    objectStoreNames: {
      contains: () => state.objectStoreCreated,
    } as unknown as DOMStringList,
    createObjectStore: vi.fn(() => {
      state.objectStoreCreated = true;
      return {} as IDBObjectStore;
    }),
    transaction: vi.fn(() => {
      state.transactionCalls += 1;
      const transaction = {
        error: null as DOMException | null,
        onabort: null as ((event: Event) => void) | null,
        oncomplete: null as ((event: Event) => void) | null,
        onerror: null as ((event: Event) => void) | null,
        objectStore: () => {
          const mutate = () => {
            const request = {
              result: undefined,
              error: null as DOMException | null,
              onsuccess: null as ((event: Event) => void) | null,
              onerror: null as ((event: Event) => void) | null,
            };
            queueMicrotask(() => {
              request.onsuccess?.(new Event("success"));
              queueMicrotask(() => {
                if (options.lateAbort) {
                  transaction.error = new DOMException(
                    "Quota exhausted after request success",
                    "QuotaExceededError",
                  );
                  transaction.onabort?.(new Event("abort"));
                } else {
                  transaction.oncomplete?.(new Event("complete"));
                }
              });
            });
            return request as unknown as IDBRequest<IDBValidKey>;
          };
          return {
            put: mutate,
            delete: mutate,
            clear: mutate,
          } as unknown as IDBObjectStore;
        },
      };
      return transaction as unknown as IDBTransaction;
    }),
  } as unknown as IDBDatabase;

  const openRequest = {
    result: database,
    error:
      openMode === "error"
        ? new DOMException("Database open failed", "UnknownError")
        : null,
    onupgradeneeded: null as ((event: Event) => void) | null,
    onsuccess: null as ((event: Event) => void) | null,
    onerror: null as ((event: Event) => void) | null,
    onblocked: null as ((event: Event) => void) | null,
  };
  const factory = {
    open: vi.fn(() => {
      queueMicrotask(() => {
        if (openMode === "blocked") {
          openRequest.onblocked?.(new Event("blocked"));
          return;
        }
        if (openMode === "error") {
          openRequest.onerror?.(new Event("error"));
          return;
        }
        openRequest.onupgradeneeded?.(new Event("upgradeneeded"));
        openRequest.onsuccess?.(new Event("success"));
      });
      return openRequest as unknown as IDBOpenDBRequest;
    }),
  } as unknown as IDBFactory;

  return { factory, state };
}

function stores(): Array<[string, () => ReportStore]> {
  return [
    ["memory", () => new MemoryReportStore()],
    [
      "fake IndexedDB",
      () =>
        new IndexedDBReportStore({
          openDatabase: async () => new FakeIndexedDB(),
        }),
    ],
  ];
}

describe.each(stores())("%s report-store contract", (_name, createStore) => {
  it("lists reports newest first using stable local index data", async () => {
    const store = createStore();
    await store.put(reportAt("older", "2026-07-29T10:00:00.000Z"));
    await store.put(reportAt("newer", "2026-07-30T10:00:00.000Z"));

    expect(await store.list()).toEqual([
      expect.objectContaining({
        id: "newer",
        title: "Report newer",
        source: "real",
        finding_count: 1,
      }),
      expect.objectContaining({
        id: "older",
        title: "Report older",
        source: "real",
        finding_count: 1,
      }),
    ]);
  });

  it("retrieves a report by local ID and deterministically replaces that ID", async () => {
    const store = createStore();
    await store.put(reportAt("same-id", "2026-07-29T10:00:00.000Z", "Before"));
    await store.put(reportAt("same-id", "2026-07-30T10:00:00.000Z", "After"));

    expect(await store.get("same-id")).toMatchObject({
      id: "same-id",
      title: "After",
      created_at: "2026-07-30T10:00:00.000Z",
    });
    expect(await store.list()).toHaveLength(1);
  });

  it("deletes one report and clears all reports", async () => {
    const store = createStore();
    await store.put(reportAt("one", "2026-07-29T10:00:00.000Z"));
    await store.put(reportAt("two", "2026-07-30T10:00:00.000Z"));

    await store.delete("one");
    expect(await store.get("one")).toBeNull();
    expect(await store.list()).toHaveLength(1);

    await store.clear();
    expect(await store.list()).toEqual([]);
    expect(await store.usage()).toMatchObject({
      report_count: 0,
      bytes_used: 0,
      thumbnail_count: 0,
    });
  });

  it("persists reviewed IDs and preferences while removing files and object URLs", async () => {
    const store = createStore();
    const report = reportAt("private-input", "2026-07-30T10:00:00.000Z");
    report.reviewed_finding_ids = ["demo-flicker"];
    report.preferences = {
      locale: "zh-CN",
      creator_view: false,
      reduced_motion: true,
    };
    const unsafe = report as BrowserReport & {
      original_file?: File;
      active_video_url?: string;
      arbitrary_secret?: string;
    };
    unsafe.original_file = new File(["private video"], "私密 视频.mp4");
    unsafe.active_video_url = "BlOb:https://example.test/private-video";
    unsafe.arbitrary_secret = "must not enter the report store";
    unsafe.findings[0].evidence[0].metadata = {
      object_url: "BLOB:https://example.test/evidence",
      remote_url: "HTTPS://example.test/private-frame.png",
      windows_path: "C:\\Users\\person\\private-frame.png",
      posix_path: "/Users/person/private-frame.png",
      full_resolution_frame: `data:image/png;base64,${"YQ==".repeat(2048)}`,
      safe_note: "kept",
    };
    (
      unsafe.findings[0] as (typeof unsafe.findings)[number] & {
        raw_frame?: Uint8Array;
      }
    ).raw_frame = new Uint8Array([1, 2, 3]);

    await store.put(unsafe);

    const persisted = await store.get(report.id);
    expect(persisted?.reviewed_finding_ids).toEqual(["demo-flicker"]);
    expect(persisted?.preferences).toEqual({
      locale: "zh-CN",
      creator_view: false,
      reduced_motion: true,
    });
    expect(JSON.stringify(persisted)).not.toContain("私密 视频.mp4");
    expect(JSON.stringify(persisted).toLowerCase()).not.toContain("blob:");
    expect(JSON.stringify(persisted).toLowerCase()).not.toContain("https:");
    expect(JSON.stringify(persisted)).not.toContain("C:\\Users");
    expect(JSON.stringify(persisted)).not.toContain("/Users/person");
    expect(JSON.stringify(persisted)).not.toContain("arbitrary_secret");
    expect(JSON.stringify(persisted)).not.toContain("raw_frame");
    expect(persisted?.findings[0].evidence[0].metadata).toEqual({
      safe_note: "kept",
    });
  });

  it("caps persisted evidence thumbnails by count and encoded byte size", async () => {
    const store = createStore();
    const report = reportAt("thumbnail-cap", "2026-07-30T10:00:00.000Z");
    const acceptedData = `data:image/webp;base64,${"YWFh".repeat(16)}`;
    const oversizedData = `data:image/webp;base64,${"YWFh".repeat(
      Math.ceil(MAX_PERSISTED_THUMBNAIL_BYTES / 3) + 1,
    )}`;
    report.findings[0].evidence = Array.from(
      { length: MAX_PERSISTED_THUMBNAILS + 2 },
      (_, index) => ({
        evidence_type: "frame" as const,
        timestamp_seconds: 3.2 + index / 10,
        description: `Evidence ${index}`,
        metadata: {},
        thumbnail:
          index === 0
            ? {
                src: oversizedData,
                width: 1920,
                height: 1080,
              }
            : {
                src: acceptedData,
                width: 320,
                height: 180,
              },
      }),
    );

    await store.put(report);

    const persisted = await store.get(report.id);
    const thumbnails = persisted?.findings.flatMap((finding) =>
      finding.evidence.flatMap((evidence) =>
        evidence.thumbnail ? [evidence.thumbnail] : [],
      ),
    );
    expect(thumbnails).toHaveLength(MAX_PERSISTED_THUMBNAILS);
    expect(thumbnails?.every((thumbnail) => thumbnail.width <= 480)).toBe(true);
    expect(JSON.stringify(persisted)).not.toContain(oversizedData);
  });

  it("keeps bounded relative thumbnails but rejects remote and private-path thumbnails", async () => {
    const store = createStore();
    const report = reportAt("thumbnail-paths", "2026-07-30T10:00:00.000Z");
    report.findings[0].evidence = [
      {
        evidence_type: "frame",
        timestamp_seconds: 3.2,
        description: "Safe local thumbnail",
        metadata: {},
        thumbnail: {
          src: "media/evidence-lake.webp",
          width: 480,
          height: 270,
        },
      },
      {
        evidence_type: "frame",
        timestamp_seconds: 3.3,
        description: "Remote thumbnail",
        metadata: {},
        thumbnail: {
          src: "HTTPS://example.test/private.webp",
          width: 320,
          height: 180,
        },
      },
      {
        evidence_type: "frame",
        timestamp_seconds: 3.4,
        description: "Private path thumbnail",
        metadata: {},
        thumbnail: {
          src: "C:\\Users\\person\\private.webp",
          width: 320,
          height: 180,
        },
      },
    ];

    await store.put(report);

    const persisted = await store.get(report.id);
    expect(
      persisted?.findings[0].evidence.flatMap((evidence) =>
        evidence.thumbnail ? [evidence.thumbnail.src] : [],
      ),
    ).toEqual(["media/evidence-lake.webp"]);
  });

  it("reports deterministic compact storage usage", async () => {
    const store = createStore();
    await store.put(reportAt("usage", "2026-07-30T10:00:00.000Z"));

    const usage = await store.usage();
    expect(usage.report_count).toBe(1);
    expect(usage.bytes_used).toBeGreaterThan(0);
    expect(usage.thumbnail_count).toBe(1);
  });
});

describe("report-store fallback", () => {
  it("returns a memory store and a non-fatal warning when IndexedDB is unavailable", async () => {
    const result = await createReportStore({ indexedDB: undefined });

    expect(result.storage).toBe("memory");
    expect(result.warning).toMatch(/IndexedDB.*memory/i);
    await result.store.put(reportAt("fallback", "2026-07-30T10:00:00.000Z"));
    expect(await result.store.get("fallback")).not.toBeNull();
  });

  it("falls back when opening IndexedDB fails", async () => {
    const result = await createReportStore({
      indexedDB: {} as IDBFactory,
      openDatabase: async () => {
        throw new Error("private mode denied");
      },
    });

    expect(result.storage).toBe("memory");
    expect(result.warning).toMatch(/IndexedDB.*memory/i);
  });
});

describe("native IndexedDB boundary", () => {
  it("creates the report object store during upgrade", async () => {
    const native = fakeNativeIndexedDB();
    const store = new IndexedDBReportStore({ indexedDB: native.factory });

    await store.ready();

    expect(native.state.objectStoreCreated).toBe(true);
  });

  it.each(["blocked", "error"] as const)(
    "rejects when database opening is %s",
    async (open) => {
      const native = fakeNativeIndexedDB({ open });
      const store = new IndexedDBReportStore({ indexedDB: native.factory });

      await expect(store.ready()).rejects.toMatchObject({
        name: open === "error" ? "UnknownError" : "Error",
      });
    },
  );

  it("rejects a write when its transaction aborts after request success", async () => {
    const native = fakeNativeIndexedDB({ lateAbort: true });
    const store = new IndexedDBReportStore({ indexedDB: native.factory });
    await store.ready();

    await expect(
      store.put(reportAt("late-abort", "2026-07-30T10:00:00.000Z")),
    ).rejects.toMatchObject({ name: "QuotaExceededError" });
    expect(native.state.transactionCalls).toBe(1);
  });
});

describe("centralized demo report", () => {
  it("uses the browser schema, visible demo label, and approved five intervals", () => {
    expect(demoReport).toMatchObject({
      schema_version: "0.1-browser",
      tool_version: "0.2.0",
      source: "demo",
      demo_label: "INTERACTIVE DEMO",
      summary: { review_interval_count: 5 },
    });
    expect(
      demoReport.findings.map(({ title, time_range }) => ({
        title,
        time_range,
      })),
    ).toEqual([
      {
        title: "Temporal Flicker",
        time_range: { start_seconds: 3.2, end_seconds: 4.1 },
      },
      {
        title: "Hand Geometry Distortion",
        time_range: { start_seconds: 6.8, end_seconds: 7.5 },
      },
      {
        title: "Background Warping",
        time_range: { start_seconds: 9, end_seconds: 10.4 },
      },
      {
        title: "Text Instability",
        time_range: { start_seconds: 12.1, end_seconds: 12.8 },
      },
      {
        title: "Motion Jitter",
        time_range: { start_seconds: 15.2, end_seconds: 16 },
      },
    ]);
  });

  it("keeps CPU findings distinct from optional demo signals and has no overall score", () => {
    expect(demoReport.findings[0].signal_kind).toBe("browser_cpu");
    expect(
      demoReport.findings.slice(1).every(({ signal_kind }) => {
        return signal_kind === "optional_demo";
      }),
    ).toBe(true);
    expect(
      demoReport.metrics.every(({ label }) => !/overall|综合|总质量/i.test(label)),
    ).toBe(true);
    expect(JSON.stringify(demoReport)).not.toMatch(
      /overall[_ ]?score|aggregate[_ ]?quality/i,
    );
  });

  it("creates only real reports from structurally real CPU input", () => {
    const report = createRealBrowserReport(realReportInput());

    expect(report).toMatchObject({
      schema_version: "0.1-browser",
      tool_version: "0.2.0",
      source: "real",
    });
    expect("demo_label" in report).toBe(false);
    expect(
      report.findings.every((finding) => finding.signal_kind === "browser_cpu"),
    ).toBe(true);
  });

  it("rejects a demo report passed through an untyped JavaScript boundary", () => {
    expect(() =>
      createRealBrowserReport(demoReport as unknown as ReturnType<
        typeof realReportInput
      >),
    ).toThrow(/demo|real report input/i);
  });

  it("rejects optional-demo findings and metrics at runtime", () => {
    const findingInput = realReportInput();
    (
      findingInput as unknown as { findings: Finding[] }
    ).findings.push(structuredClone(demoReport.findings[1]));
    const metricInput = realReportInput();
    (
      metricInput as unknown as { metrics: QualityMetric[] }
    ).metrics.push(structuredClone(demoReport.metrics[4]));

    expect(() =>
      createRealBrowserReport(
        findingInput as unknown as RealBrowserReportInput,
      ),
    ).toThrow(
      /optional|browser_cpu/i,
    );
    expect(() =>
      createRealBrowserReport(
        metricInput as unknown as RealBrowserReportInput,
      ),
    ).toThrow(
      /optional|browser_cpu/i,
    );
  });

  it("rejects demo tags and evidence metadata at runtime", () => {
    const taggedInput = realReportInput();
    taggedInput.findings[0].tags = ["interactive-demo"];
    const evidenceInput = realReportInput();
    evidenceInput.findings[0].evidence[0].metadata = { demo: true };

    expect(() => createRealBrowserReport(taggedInput)).toThrow(/demo/i);
    expect(() => createRealBrowserReport(evidenceInput)).toThrow(/demo/i);
  });
});
