import { describe, expect, it } from "vitest";

import { createBrowserAnalysisService } from "./analyze-local-video";
import { defaultBrowserAnalysisOptions } from "./config";
import type {
  BrowserDetector,
  BrowserSampler,
  BrowserSamplingSession,
} from "./contracts";

const samples = [
  {
    sample_index: 0,
    timestamp_seconds: 0,
    width: 160,
    height: 90,
    mean_luma: 120,
    median_luma: 120,
    dark_pixel_ratio: 0,
    sharpness: 100,
    pixel_difference: 20,
    hash_distance: 10,
  },
  {
    sample_index: 1,
    timestamp_seconds: 1,
    width: 160,
    height: 90,
    mean_luma: 4,
    median_luma: 3,
    dark_pixel_ratio: 0.99,
    sharpness: 100,
    pixel_difference: 20,
    hash_distance: 10,
  },
  {
    sample_index: 2,
    timestamp_seconds: 2,
    width: 160,
    height: 90,
    mean_luma: 4,
    median_luma: 3,
    dark_pixel_ratio: 0.99,
    sharpness: 100,
    pixel_difference: 20,
    hash_distance: 10,
  },
  {
    sample_index: 3,
    timestamp_seconds: 3,
    width: 160,
    height: 90,
    mean_luma: 120,
    median_luma: 120,
    dark_pixel_ratio: 0,
    sharpness: 100,
    pixel_difference: 20,
    hash_distance: 10,
  },
];

function fakeSampler(): BrowserSampler {
  const session: BrowserSamplingSession = {
    metadata: {
      filename: "safe.mp4",
      mime_type: "video/mp4",
      width: 320,
      height: 180,
      duration_seconds: 4,
      file_size_bytes: 20,
    },
    samples,
    scenes: [
      {
        scene_index: 0,
        start_seconds: 0,
        end_seconds: 4,
        representative_timestamp: 2,
      },
    ],
    captureEvidence: async (timestamps) =>
      ({
        thumbnails: new Map(
          timestamps.map((timestamp) => [
            timestamp,
            {
              src: `data:image/jpeg;base64,frame-${timestamp}`,
              width: 160,
              height: 90,
            },
          ]),
        ),
        capped_by_count: false,
        capped_by_bytes: false,
        retained_bytes: 100,
      }),
  };
  return {
    withSession: async (_file, _options, signal, _onProgress, consume) => {
      if (signal.aborted) {
        throw new DOMException("Analysis cancelled", "AbortError");
      }
      return consume(session);
    },
  };
}

describe("browser analysis orchestrator", () => {
  it("emits ordered stages and produces deterministic real-only findings", async () => {
    const service = createBrowserAnalysisService({
      sampler: fakeSampler(),
      hashFile: async () => "b".repeat(64),
      now: () => new Date("2026-01-01T00:00:00.000Z"),
      randomId: () => "analysis-envelope",
    });
    const stages: string[] = [];
    const file = new File(["browser-video"], "C:\\Users\\name\\clip.mp4", {
      type: "video/mp4",
    });
    const run = () =>
      service.analyzeLocalVideo(
        file,
        {
          ...defaultBrowserAnalysisOptions,
          detectors: {
            ...defaultBrowserAnalysisOptions.detectors,
            near_black: {
              ...defaultBrowserAnalysisOptions.detectors.near_black,
              min_duration_seconds: 1,
            },
          },
        },
        new AbortController().signal,
        (event) => stages.push(event.stage),
      );

    const first = await run();
    const second = await run();

    const stageTransitions = stages.filter(
      (stage, index) => stage !== stages[index - 1],
    );
    expect(stageTransitions.slice(0, 8)).toEqual([
      "validating",
      "hashing",
      "reading_metadata",
      "sampling_frames",
      "segmenting_scenes",
      "running_detectors",
      "selecting_evidence",
      "assembling_report",
    ]);
    expect(stageTransitions.at(-1)).toBe("complete");
    expect(first.source).toBe("real");
    expect(first.findings).toHaveLength(1);
    expect(first.findings).toEqual(second.findings);
    expect(first.findings[0].evidence[0].thumbnail?.src).toContain(
      "data:image/jpeg",
    );
    const serialized = JSON.stringify(first);
    expect(serialized).not.toMatch(/INTERACTIVE DEMO|optional_demo|demo-/i);
    expect(serialized).not.toContain("C:\\Users");
    expect(first.metadata.filename).toBe("clip.mp4");
  });

  it("records one detector failure and preserves another detector result", async () => {
    const successful: BrowserDetector = {
      id: "successful",
      version: "test-1",
      defaultConfig: { enabled: true },
      analyze: () => [
        {
          detector_id: "successful",
          detector_version: "test-1",
          signal_kind: "browser_cpu",
          title: "Observable interval",
          description: "A controlled detector result.",
          severity: "low",
          score: 0.5,
          confidence: 0.5,
          time_range: { start_seconds: 1, end_seconds: 2 },
          evidence: [
            {
              evidence_type: "metric",
              timestamp_seconds: 1.5,
              description: "Metric evidence",
              metadata: {},
            },
          ],
          tags: ["test-signal"],
          parameters: {},
          limitations: ["Controlled test detector."],
        },
      ],
    };
    const failed: BrowserDetector = {
      id: "failed",
      version: "test-1",
      defaultConfig: { enabled: true },
      analyze: () => {
        throw new Error("C:\\Users\\private\\secret.mp4 exploded");
      },
    };
    const service = createBrowserAnalysisService({
      sampler: fakeSampler(),
      detectors: [failed, successful],
      hashFile: async () => "c".repeat(64),
      now: () => new Date("2026-01-01T00:00:00.000Z"),
      randomId: () => "analysis-envelope",
    });

    const report = await service.analyzeLocalVideo(
      new File(["x"], "clip.mp4", { type: "video/mp4" }),
      defaultBrowserAnalysisOptions,
      new AbortController().signal,
      () => undefined,
    );

    expect(report.findings).toHaveLength(1);
    expect(report.detector_executions).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ detector_id: "failed", status: "failed" }),
        expect.objectContaining({ detector_id: "successful", status: "ok" }),
      ]),
    );
    expect(
      report.metrics.some((metric) => metric.detector_id === "failed"),
    ).toBe(false);
    expect(JSON.stringify(report)).not.toContain("C:\\Users");
  });

  it("records when the configured sample cap reduces requested coverage", async () => {
    const service = createBrowserAnalysisService({
      sampler: fakeSampler(),
      hashFile: async () => "d".repeat(64),
      now: () => new Date("2026-01-01T00:00:00.000Z"),
      randomId: () => "sample-cap-envelope",
    });

    const report = await service.analyzeLocalVideo(
      new File(["x"], "clip.mp4", { type: "video/mp4" }),
      {
        ...defaultBrowserAnalysisOptions,
        sample_fps: 4,
        max_samples: 4,
      },
      new AbortController().signal,
      () => undefined,
    );

    expect(report.warnings).toContain(
      "The configured sample cap was reached; sampling density was reduced to stay within the browser memory budget.",
    );
  });

  it("stops promptly when cancelled", async () => {
    const controller = new AbortController();
    controller.abort();
    const service = createBrowserAnalysisService({
      sampler: fakeSampler(),
      hashFile: async (_file, signal) => {
        if (signal.aborted) {
          throw new DOMException("Analysis cancelled", "AbortError");
        }
        return "d".repeat(64);
      },
    });

    await expect(
      service.analyzeLocalVideo(
        new File(["x"], "clip.mp4"),
        defaultBrowserAnalysisOptions,
        controller.signal,
        () => undefined,
      ),
    ).rejects.toMatchObject({ name: "AbortError" });
  });

  it("uses the effective near-black pixel threshold while sampling", async () => {
    let sampledThreshold: number | undefined;
    const baseSampler = fakeSampler();
    const sampler: BrowserSampler = {
      withSession: async (
        file,
        options,
        signal,
        onProgress,
        consume,
      ) => {
        sampledThreshold = options.dark_pixel_threshold;
        return baseSampler.withSession(
          file,
          options,
          signal,
          onProgress,
          consume,
        );
      },
    };
    const service = createBrowserAnalysisService({
      sampler,
      hashFile: async () => "e".repeat(64),
    });

    await service.analyzeLocalVideo(
      new File(["x"], "clip.mp4", { type: "video/mp4" }),
      {
        ...defaultBrowserAnalysisOptions,
        dark_pixel_threshold: 16,
        detectors: {
          ...defaultBrowserAnalysisOptions.detectors,
          near_black: {
            ...defaultBrowserAnalysisOptions.detectors.near_black,
            dark_pixel_threshold: 7,
          },
        },
      },
      new AbortController().signal,
      () => undefined,
    );

    expect(sampledThreshold).toBe(7);
  });

  it("forwards per-frame sampler progress without percentage regression", async () => {
    const baseSampler = fakeSampler();
    const sampler: BrowserSampler = {
      withSession: async (
        file,
        options,
        signal,
        onProgress,
        consume,
      ) => {
        onProgress({ stage: "sampling_frames", progress: 0.31 });
        onProgress({ stage: "sampling_frames", progress: 0.42 });
        return baseSampler.withSession(
          file,
          options,
          signal,
          onProgress,
          consume,
        );
      },
    };
    const progress: Array<{ stage: string; progress: number }> = [];
    const service = createBrowserAnalysisService({
      sampler,
      hashFile: async () => "f".repeat(64),
    });

    await service.analyzeLocalVideo(
      new File(["x"], "clip.mp4", { type: "video/mp4" }),
      defaultBrowserAnalysisOptions,
      new AbortController().signal,
      (event) => progress.push(event),
    );

    expect(progress).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ stage: "sampling_frames", progress: 0.31 }),
        expect.objectContaining({ stage: "sampling_frames", progress: 0.42 }),
      ]),
    );
    expect(
      progress.every(
        (event, index) =>
          index === 0 || event.progress >= progress[index - 1].progress,
      ),
    ).toBe(true);
  });

  it("localizes real detector copy from the selected locale", async () => {
    const service = createBrowserAnalysisService({
      sampler: fakeSampler(),
      hashFile: async () => "1".repeat(64),
    });

    const report = await service.analyzeLocalVideo(
      new File(["x"], "clip.mp4", { type: "video/mp4" }),
      {
        ...defaultBrowserAnalysisOptions,
        locale: "zh-CN",
        detectors: {
          ...defaultBrowserAnalysisOptions.detectors,
          near_black: {
            ...defaultBrowserAnalysisOptions.detectors.near_black,
            min_duration_seconds: 1,
          },
        },
      },
      new AbortController().signal,
      () => undefined,
    );

    expect(report.findings[0].title).toBe("检测到近黑区间");
    expect(report.findings[0].description).toMatch(/采样帧/);
    expect(report.findings[0].limitations.join(" ")).toMatch(/夜景|淡出/);
  });
});
