import { describe, expect, it, vi } from "vitest";

import { createBrowserVideoSampler } from "./sampler";

describe("browser video sampler resource handling", () => {
  it("revokes the object URL when metadata loading fails", async () => {
    const revokeObjectURL = vi.fn();
    const video = {
      addEventListener: vi.fn(
        (event: string, callback: EventListenerOrEventListenerObject) => {
          if (event === "error" && typeof callback === "function") {
            queueMicrotask(() => callback(new Event("error")));
          }
        },
      ),
      removeEventListener: vi.fn(),
      load: vi.fn(),
      pause: vi.fn(),
      removeAttribute: vi.fn(),
      src: "",
    };
    const sampler = createBrowserVideoSampler({
      createObjectURL: () => "blob:analysis",
      revokeObjectURL,
      createVideo: () => video as unknown as HTMLVideoElement,
      createCanvas: () => document.createElement("canvas"),
    });

    await expect(
      sampler.withSession(
        new File(["bad"], "bad.mp4", { type: "video/mp4" }),
        {
          sample_fps: 2,
          max_samples: 20,
          max_dimension: 320,
          evidence_max_dimension: 320,
          evidence_quality: 0.72,
          max_evidence_items: 24,
          max_evidence_total_bytes: 3 * 1024 * 1024,
          dark_pixel_threshold: 16,
          scene_cut_difference_threshold: 35,
        },
        new AbortController().signal,
        () => undefined,
        async () => "unused",
      ),
    ).rejects.toThrow("metadata");

    expect(revokeObjectURL).toHaveBeenCalledOnce();
    expect(video.pause).toHaveBeenCalled();
    expect(video.removeAttribute).toHaveBeenCalledWith("src");
  });

  it("reports unavailable duration separately from a generic decode error", async () => {
    const video = document.createElement("video");
    Object.defineProperties(video, {
      duration: { configurable: true, value: Number.NaN },
      readyState: {
        configurable: true,
        value: HTMLMediaElement.HAVE_CURRENT_DATA,
      },
      videoHeight: { configurable: true, value: 180 },
      videoWidth: { configurable: true, value: 320 },
    });
    video.load = vi.fn(() => queueMicrotask(() => {
      video.dispatchEvent(new Event("loadedmetadata"));
    }));
    video.pause = vi.fn();
    const sampler = createBrowserVideoSampler({
      createObjectURL: () => "blob:duration",
      revokeObjectURL: vi.fn(),
      createVideo: () => video,
      createCanvas: () => document.createElement("canvas"),
    });

    await expect(
      sampler.withSession(
        new File(["video"], "duration.mp4", { type: "video/mp4" }),
        {
          sample_fps: 2,
          max_samples: 20,
          max_dimension: 320,
          evidence_max_dimension: 320,
          evidence_quality: 0.72,
          max_evidence_items: 24,
          max_evidence_total_bytes: 3 * 1024 * 1024,
          dark_pixel_threshold: 16,
          scene_cut_difference_threshold: 35,
        },
        new AbortController().signal,
        () => undefined,
        async () => "unused",
      ),
    ).rejects.toMatchObject({
      code: "duration_unavailable",
    });
  });

  it("turns frame-buffer allocation failure into a public memory-pressure state", async () => {
    const video = document.createElement("video");
    Object.defineProperties(video, {
      duration: { configurable: true, value: 1 },
      readyState: {
        configurable: true,
        value: HTMLMediaElement.HAVE_CURRENT_DATA,
      },
      videoHeight: { configurable: true, value: 180 },
      videoWidth: { configurable: true, value: 320 },
    });
    video.load = vi.fn(() => queueMicrotask(() => {
      video.dispatchEvent(new Event("loadedmetadata"));
    }));
    video.pause = vi.fn();
    const context = {
      drawImage: vi.fn(),
      getImageData: vi.fn(() => {
        throw new RangeError("allocation failed");
      }),
    };
    const canvas = document.createElement("canvas");
    canvas.getContext = vi.fn(() => context) as unknown as typeof canvas.getContext;
    const sampler = createBrowserVideoSampler({
      createObjectURL: () => "blob:memory",
      revokeObjectURL: vi.fn(),
      createVideo: () => video,
      createCanvas: () => canvas,
    });

    await expect(
      sampler.withSession(
        new File(["video"], "memory.mp4", { type: "video/mp4" }),
        {
          sample_fps: 2,
          max_samples: 20,
          max_dimension: 320,
          evidence_max_dimension: 320,
          evidence_quality: 0.72,
          max_evidence_items: 24,
          max_evidence_total_bytes: 3 * 1024 * 1024,
          dark_pixel_threshold: 16,
          scene_cut_difference_threshold: 35,
        },
        new AbortController().signal,
        () => undefined,
        async () => "unused",
      ),
    ).rejects.toMatchObject({
      code: "memory_pressure",
    });
  });
});
