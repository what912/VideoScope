import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { createRealBrowserReport } from "../../types/report";
import type {
  BrowserAnalysisOptions,
  BrowserAnalysisService,
} from "../../services/browser-analysis";
import type { ReportStore } from "../../services/report-store/report-store";
import {
  clearSessionVideo,
  getSessionVideo,
} from "./session-video-store";
import { useAnalysisJob } from "./useAnalysisJob";

function report(id = "browser-report", title = `${id}.mp4`) {
  return createRealBrowserReport({
    tool_version: "0.2.0",
    id,
    analysis_id: `analysis-${id}`,
    title,
    created_at: "2026-07-30T00:00:00.000Z",
    input_hash: "abc123",
    metadata: {
      filename: "clip.mp4",
      mime_type: "video/mp4",
      width: 320,
      height: 180,
      duration_seconds: 2,
      file_size_bytes: 5,
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
      analysis_seconds: 0.1,
      sample_count: 4,
    },
    reviewed_finding_ids: [],
    preferences: {
      locale: "en",
      creator_view: true,
      reduced_motion: false,
    },
  });
}

const options = {
  sample_fps: 2,
  max_samples: 10,
  max_dimension: 320,
  evidence_max_dimension: 320,
  evidence_quality: 0.7,
  max_evidence_items: 3,
  max_evidence_total_bytes: 100_000,
  dark_pixel_threshold: 16,
  scene_cut_difference_threshold: 34,
  retain_prompt: false,
  locale: "en",
  reduced_motion: false,
  detectors: { near_black: { enabled: true } },
} satisfies BrowserAnalysisOptions;

function createStore(): ReportStore {
  return {
    put: vi.fn().mockResolvedValue(undefined),
    get: vi.fn(),
    list: vi.fn(),
    delete: vi.fn(),
    clear: vi.fn(),
    usage: vi.fn(),
  };
}

describe("useAnalysisJob", () => {
  it("publishes ordered progress, persists the compact report, and navigates", async () => {
    clearSessionVideo();
    const finalReport = report();
    let continueToSampling: (() => void) | undefined;
    let continueToComplete: (() => void) | undefined;
    const samplingGate = new Promise<void>((resolve) => {
      continueToSampling = resolve;
    });
    const completionGate = new Promise<void>((resolve) => {
      continueToComplete = resolve;
    });
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi.fn(
        async (_file, _options, _signal, onProgress) => {
          onProgress({ stage: "validating", progress: 0.02 });
          await samplingGate;
          onProgress({ stage: "sampling_frames", progress: 0.3 });
          await completionGate;
          onProgress({ stage: "complete", progress: 1 });
          return finalReport;
        },
      ),
    };
    const store = createStore();
    const navigate = vi.fn();
    const revokeObjectURL = vi.fn();
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService,
        reportStore: store,
        navigate,
        createObjectURL: () => "blob:session-video",
        revokeObjectURL,
      }),
    );

    let pending: Promise<void>;
    act(() => {
      pending = result.current.start(
        new File(["video"], "clip.mp4", { type: "video/mp4" }),
        options,
      );
    });

    await waitFor(() =>
      expect(result.current.state).toMatchObject({
        status: "running",
        progress: { stage: "validating" },
      }),
    );
    act(() => continueToSampling?.());
    await waitFor(() =>
      expect(result.current.state).toMatchObject({
        status: "running",
        progress: { stage: "sampling_frames" },
      }),
    );
    act(() => continueToComplete?.());
    await act(async () => {
      await pending;
    });

    expect(result.current.state).toMatchObject({
      status: "completed",
      report: finalReport,
    });
    expect(store.put).toHaveBeenCalledWith(finalReport);
    expect(navigate).toHaveBeenCalledWith(
      "/workspace?report=browser-report",
    );
    expect(getSessionVideo()).toMatchObject({
      reportId: "browser-report",
      objectUrl: "blob:session-video",
    });
    expect(revokeObjectURL).not.toHaveBeenCalled();
  });

  it("cancels an active job and revokes its temporary object URL", async () => {
    clearSessionVideo();
    let rejectAnalysis: ((error: unknown) => void) | undefined;
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi.fn(
        (_file, _options, signal) =>
          new Promise<never>((_resolve, reject) => {
            rejectAnalysis = reject;
            signal.addEventListener("abort", () => {
              reject(new DOMException("cancelled", "AbortError"));
            });
          }),
      ),
    };
    const revokeObjectURL = vi.fn();
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService,
        reportStore: createStore(),
        navigate: vi.fn(),
        createObjectURL: () => "blob:pending",
        revokeObjectURL,
      }),
    );
    let pending: Promise<void>;

    act(() => {
      pending = result.current.start(
        new File(["video"], "clip.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await waitFor(() =>
      expect(result.current.state.status).toBe("running"),
    );
    act(() => result.current.cancel());
    await act(async () => {
      await pending;
    });

    expect(result.current.state.status).toBe("cancelled");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:pending");
    expect(getSessionVideo()).toBeNull();
    expect(rejectAnalysis).toBeTypeOf("function");
  });

  it("ignores an analysis result that resolves after cancellation", async () => {
    clearSessionVideo();
    let resolveAnalysis: ((value: ReturnType<typeof report>) => void) | undefined;
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi.fn(
        () =>
          new Promise<ReturnType<typeof report>>((resolve) => {
            resolveAnalysis = resolve;
          }),
      ),
    };
    const store = createStore();
    const navigate = vi.fn();
    const revokeObjectURL = vi.fn();
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService,
        reportStore: store,
        navigate,
        createObjectURL: () => "blob:late-analysis",
        revokeObjectURL,
      }),
    );

    let pending: Promise<void>;
    act(() => {
      pending = result.current.start(
        new File(["video"], "clip.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await waitFor(() => expect(result.current.state.status).toBe("running"));
    act(() => result.current.cancel());
    await act(async () => {
      resolveAnalysis?.(report());
      await pending;
    });

    expect(result.current.state.status).toBe("cancelled");
    expect(store.put).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    expect(getSessionVideo()).toBeNull();
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
  });

  it("removes a report whose persistence finishes after cancellation", async () => {
    clearSessionVideo();
    let resolvePut: (() => void) | undefined;
    const store = createStore();
    vi.mocked(store.put).mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          resolvePut = resolve;
        }),
    );
    const navigate = vi.fn();
    const revokeObjectURL = vi.fn();
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService: {
          analyzeLocalVideo: vi.fn().mockResolvedValue(report()),
        },
        reportStore: store,
        navigate,
        createObjectURL: () => "blob:persisting",
        revokeObjectURL,
      }),
    );

    let pending: Promise<void>;
    act(() => {
      pending = result.current.start(
        new File(["video"], "clip.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await waitFor(() => expect(store.put).toHaveBeenCalledTimes(1));
    act(() => result.current.cancel());
    await act(async () => {
      resolvePut?.();
      await pending;
    });

    expect(store.delete).toHaveBeenCalledWith("browser-report");
    expect(result.current.state.status).toBe("cancelled");
    expect(navigate).not.toHaveBeenCalled();
    expect(getSessionVideo()).toBeNull();
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
  });

  it("lets a new job own state when the replaced job finishes late", async () => {
    clearSessionVideo();
    let resolveOld: ((value: ReturnType<typeof report>) => void) | undefined;
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi.fn((file) => {
        if (file.name === "old.mp4") {
          return new Promise<ReturnType<typeof report>>((resolve) => {
            resolveOld = resolve;
          });
        }
        return Promise.resolve(report("new-report"));
      }),
    };
    const store = createStore();
    const navigate = vi.fn();
    const revokeObjectURL = vi.fn();
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService,
        reportStore: store,
        navigate,
        createObjectURL: (file) => `blob:${file.name}`,
        revokeObjectURL,
      }),
    );

    let oldPending: Promise<void>;
    let newPending: Promise<void>;
    act(() => {
      oldPending = result.current.start(
        new File(["old"], "old.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await waitFor(() => expect(result.current.state.status).toBe("running"));
    act(() => {
      newPending = result.current.start(
        new File(["new"], "new.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await act(async () => {
      await newPending;
      resolveOld?.(report("old-report"));
      await oldPending;
    });

    expect(store.put).toHaveBeenCalledTimes(1);
    expect(store.put).toHaveBeenCalledWith(
      expect.objectContaining({ id: "new-report" }),
    );
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith(
      "/workspace?report=new-report",
    );
    expect(getSessionVideo()).toMatchObject({
      reportId: "new-report",
      objectUrl: "blob:new.mp4",
    });
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:old.mp4");
  });

  it("serializes same-ID persistence so stale cleanup cannot delete the newer report", async () => {
    clearSessionVideo();
    const events: string[] = [];
    let resolveOldPut: (() => void) | undefined;
    let putCount = 0;
    let storedReport: ReturnType<typeof report> | null = null;
    const store: ReportStore = {
      put: vi.fn((nextReport) => {
        putCount += 1;
        if (putCount === 1) {
          return new Promise<void>((resolve) => {
            resolveOldPut = () => {
              storedReport = nextReport as ReturnType<typeof report>;
              events.push("put-old");
              resolve();
            };
          });
        }
        storedReport = nextReport as ReturnType<typeof report>;
        events.push("put-new");
        return Promise.resolve();
      }),
      get: vi.fn(async () => storedReport),
      list: vi.fn(),
      delete: vi.fn(async () => {
        events.push("delete-old");
        storedReport = null;
      }),
      clear: vi.fn(),
      usage: vi.fn(),
    };
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi.fn((file) =>
        Promise.resolve(
          report(
            "deterministic-report",
            file.name === "old.mp4" ? "old result" : "new result",
          ),
        ),
      ),
    };
    const navigate = vi.fn();
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService,
        reportStore: store,
        navigate,
        createObjectURL: (file) => `blob:${file.name}`,
        revokeObjectURL: vi.fn(),
      }),
    );

    let oldPending: Promise<void>;
    let newPending: Promise<void>;
    act(() => {
      oldPending = result.current.start(
        new File(["old"], "old.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await waitFor(() => expect(store.put).toHaveBeenCalledTimes(1));
    act(() => {
      newPending = result.current.start(
        new File(["new"], "new.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await waitFor(() =>
      expect(analysisService.analyzeLocalVideo).toHaveBeenCalledTimes(2),
    );

    await act(async () => {
      resolveOldPut?.();
      await Promise.all([oldPending, newPending]);
    });

    expect(events).toEqual(["put-old", "delete-old", "put-new"]);
    expect(await store.get("deterministic-report")).toMatchObject({
      id: "deterministic-report",
      title: "new result",
    });
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith(
      "/workspace?report=deterministic-report",
    );
  });

  it("restores a preexisting same-ID report when reanalysis is cancelled during put", async () => {
    clearSessionVideo();
    const existing = report("deterministic-report", "existing result");
    let storedReport: ReturnType<typeof report> | null = existing;
    let resolveCancelledPut: (() => void) | undefined;
    let putCount = 0;
    const events: string[] = [];
    const store: ReportStore = {
      get: vi.fn(async () => storedReport),
      put: vi.fn((nextReport) => {
        putCount += 1;
        if (putCount === 1) {
          return new Promise<void>((resolve) => {
            resolveCancelledPut = () => {
              storedReport = nextReport as ReturnType<typeof report>;
              events.push("put-cancelled");
              resolve();
            };
          });
        }
        storedReport = nextReport as ReturnType<typeof report>;
        events.push(`put-${nextReport.title}`);
        return Promise.resolve();
      }),
      list: vi.fn(),
      delete: vi.fn(async () => {
        events.push("delete");
        storedReport = null;
      }),
      clear: vi.fn(),
      usage: vi.fn(),
    };
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService: {
          analyzeLocalVideo: vi
            .fn()
            .mockResolvedValue(
              report("deterministic-report", "cancelled result"),
            ),
        },
        reportStore: store,
        navigate: vi.fn(),
        createObjectURL: () => "blob:cancelled",
        revokeObjectURL: vi.fn(),
      }),
    );

    let pending: Promise<void>;
    act(() => {
      pending = result.current.start(
        new File(["video"], "cancelled.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await waitFor(() => expect(store.put).toHaveBeenCalledTimes(1));
    act(() => result.current.cancel());
    await act(async () => {
      resolveCancelledPut?.();
      await pending;
    });

    expect(events).toEqual(["put-cancelled", "put-existing result"]);
    expect(storedReport).toMatchObject({
      id: "deterministic-report",
      title: "existing result",
    });
    expect(store.delete).not.toHaveBeenCalled();
    expect(result.current.state.status).toBe("cancelled");
  });

  it("lets a newer same-ID run win after restoring the preexisting report", async () => {
    clearSessionVideo();
    const existing = report("deterministic-report", "existing result");
    let storedReport: ReturnType<typeof report> | null = existing;
    let resolveOldPut: (() => void) | undefined;
    let putCount = 0;
    const events: string[] = [];
    const store: ReportStore = {
      get: vi.fn(async () => {
        events.push(`get-${storedReport?.title ?? "none"}`);
        return storedReport;
      }),
      put: vi.fn((nextReport) => {
        putCount += 1;
        if (putCount === 1) {
          return new Promise<void>((resolve) => {
            resolveOldPut = () => {
              storedReport = nextReport as ReturnType<typeof report>;
              events.push("put-old");
              resolve();
            };
          });
        }
        storedReport = nextReport as ReturnType<typeof report>;
        events.push(`put-${nextReport.title}`);
        return Promise.resolve();
      }),
      list: vi.fn(),
      delete: vi.fn(async () => {
        events.push("delete");
        storedReport = null;
      }),
      clear: vi.fn(),
      usage: vi.fn(),
    };
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi.fn((file) =>
        Promise.resolve(
          report(
            "deterministic-report",
            file.name === "old.mp4" ? "old result" : "new result",
          ),
        ),
      ),
    };
    const navigate = vi.fn();
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService,
        reportStore: store,
        navigate,
        createObjectURL: (file) => `blob:${file.name}`,
        revokeObjectURL: vi.fn(),
      }),
    );

    let oldPending: Promise<void>;
    let newPending: Promise<void>;
    act(() => {
      oldPending = result.current.start(
        new File(["old"], "old.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await waitFor(() => expect(store.put).toHaveBeenCalledTimes(1));
    act(() => {
      newPending = result.current.start(
        new File(["new"], "new.mp4", { type: "video/mp4" }),
        options,
      );
    });
    await act(async () => {
      resolveOldPut?.();
      await Promise.all([oldPending, newPending]);
    });

    expect(events).toEqual([
      "get-existing result",
      "put-old",
      "put-existing result",
      "get-existing result",
      "put-new result",
    ]);
    expect(storedReport).toMatchObject({
      id: "deterministic-report",
      title: "new result",
    });
    expect(store.delete).not.toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledTimes(1);
  });

  it("fails safely without writing when the pre-write snapshot cannot be read", async () => {
    clearSessionVideo();
    const store = createStore();
    vi.mocked(store.get).mockRejectedValue(new Error("storage unavailable"));
    const navigate = vi.fn();
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService: {
          analyzeLocalVideo: vi.fn().mockResolvedValue(report()),
        },
        reportStore: store,
        navigate,
        createObjectURL: () => "blob:snapshot-error",
        revokeObjectURL: vi.fn(),
      }),
    );

    await act(() =>
      result.current.start(
        new File(["video"], "clip.mp4", { type: "video/mp4" }),
        options,
      ),
    );

    expect(store.put).not.toHaveBeenCalled();
    expect(store.delete).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    expect(result.current.state).toEqual({
      status: "failed",
      error: { code: "storage_failed" },
    });
  });

  it("returns a stable public error code without exposing thrown details", async () => {
    const analysisService: BrowserAnalysisService = {
      analyzeLocalVideo: vi
        .fn()
        .mockRejectedValue(new Error("C:\\private\\video.mp4 secret")),
    };
    const { result } = renderHook(() =>
      useAnalysisJob({
        analysisService,
        reportStore: createStore(),
        navigate: vi.fn(),
        createObjectURL: () => "blob:error",
        revokeObjectURL: vi.fn(),
      }),
    );

    await act(() =>
      result.current.start(
        new File(["video"], "clip.mp4", { type: "video/mp4" }),
        options,
      ),
    );

    expect(result.current.state).toEqual({
      status: "failed",
      error: { code: "analysis_failed" },
    });
    expect(JSON.stringify(result.current.state)).not.toContain("private");
  });
});
