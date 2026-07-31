import { describe, expect, it, vi } from "vitest";
import {
  createJob,
  getJob,
  subscribeToJobEvents,
} from "./api";
import type { JobEvent, JobResponse } from "./types";

const JOB: JobResponse = {
  job_id: "a".repeat(32),
  status: "queued",
  message: "Job queued",
  created_at: "2026-07-29T00:00:00Z",
  updated_at: "2026-07-29T00:00:00Z",
  upload_size_bytes: 10,
  progress_percent: 0,
  current_detector: null,
  warnings: [],
  error: null,
  links: {},
};

describe("typed API client", () => {
  it("uses the centralized API root", async () => {
    const fetcher = vi.fn(async () =>
      new Response(JSON.stringify(JOB), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    await expect(getJob(JOB.job_id, fetcher as typeof fetch)).resolves.toEqual(JOB);
    expect(fetcher).toHaveBeenCalledWith(
      `/api/jobs/${JOB.job_id}`,
      undefined,
    );
  });

  it("uploads video, prompt, and strict configuration as multipart data", async () => {
    const fetcher = vi.fn(async (_url: string, init?: RequestInit) => {
      const form = init?.body as FormData;
      expect(form.get("prompt")).toBe("A local prompt");
      expect(JSON.parse(String(form.get("config")))).toEqual({
        sample_fps: 2,
        thumbnail_max_size: 640,
        enabled_detectors: ["near_black"],
        locale: "en",
      });
      return new Response(JSON.stringify(JOB), {
        status: 202,
        headers: { "content-type": "application/json" },
      });
    });
    const file = new File(["video"], "sample.mp4", { type: "video/mp4" });
    await createJob(
      file,
      "A local prompt",
      {
        sampleFps: 2,
        thumbnailMaxSize: 640,
        locale: "en",
        detectorIds: ["near_black"],
      },
      fetcher as typeof fetch,
    );
    expect(fetcher).toHaveBeenCalledOnce();
  });
});

describe("SSE reconnection", () => {
  it("reconnects from the last delivered event sequence", () => {
    const urls: string[] = [];
    const sources: FakeSource[] = [];
    const timers: Array<() => void> = [];
    const events: JobEvent[] = [];
    const factory = (url: string): FakeSource => {
      urls.push(url);
      const source = new FakeSource();
      sources.push(source);
      return source;
    };
    const subscription = subscribeToJobEvents(
      JOB.job_id,
      (event) => events.push(event),
      vi.fn(),
      {
        sourceFactory: factory,
        setTimer: ((callback: TimerHandler) => {
          timers.push(callback as () => void);
          return 1;
        }) as typeof setTimeout,
        clearTimer: vi.fn() as unknown as typeof clearTimeout,
      },
    );
    sources[0].emit("status", {
      data: JSON.stringify({
        sequence: 4,
        status: "detecting",
        message: "Running detector: near_black",
        created_at: "2026-07-29T00:00:01Z",
      }),
    });
    sources[0].emit("error", {});
    timers[0]();

    expect(events).toHaveLength(1);
    expect(urls).toEqual([
      `/api/jobs/${JOB.job_id}/events`,
      `/api/jobs/${JOB.job_id}/events?after=4`,
    ]);
    subscription.close();
    expect(sources[1].closed).toBe(true);
  });
});

class FakeSource {
  private listeners = new Map<string, EventListener[]>();
  closed = false;

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    const callback =
      typeof listener === "function" ? listener : listener.handleEvent.bind(listener);
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), callback]);
  }

  emit(type: string, payload: object): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(payload as Event);
    }
  }

  close(): void {
    this.closed = true;
  }
}
