import { describe, expect, it, vi } from "vitest";
import {
  cancelPublishJob,
  confirmPublishJob,
  createPublishJob,
  createJob,
  createPrivacyJob,
  confirmPrivacyJob,
  deletePrivacyJob,
  getPrivacyJob,
  getPrivacyPlan,
  getPrivacyRiskMap,
  getPublishJob,
  getPublishPlan,
  getJob,
  listPublishProfiles,
  listPrivacyProfiles,
  preparePrivacyJob,
  privacyArtifactUrl,
  privacyPrivateArtifactUrl,
  publishArtifactUrl,
  subscribeToPublishEvents,
  subscribeToJobEvents,
  subscribeToPrivacyEvents,
  reviewPrivacyJob,
  createRescueJob,
  rescueArtifactUrl,
  subscribeToRescueEvents,
} from "./api";
import type {
  JobEvent,
  JobResponse,
  PublishJobEvent,
  PublishJobResponse,
  PublishProfile,
  PrivacyJobEvent,
  PrivacyJobResponse,
  PrivacyReviewPayload,
  ShareAudienceProfile,
} from "./types";

describe("Video Rescue API", () => {
  it("encodes a local rescue upload as multipart fields", async () => {
    let request: RequestInit | undefined;
    await createRescueJob(new File(["x"], "录制 clip.mp4"), "balanced", ["dark", "flicker"], { lockedRanges: [[1.25, 2.5]], balancedStrengthLimit: 0.4 }, async (_url, init) => {
      request = init;
      return new Response(JSON.stringify({ job_id: "a".repeat(32) }), { status: 202 });
    });
    const form = request?.body as FormData;
    expect(form.get("strategy")).toBe("balanced");
    expect(form.getAll("symptoms")).toEqual(["dark", "flicker"]);
    expect(form.get("locked_ranges")).toBe("[[1.25,2.5]]");
    expect(form.get("balanced_strength_limit")).toBe("0.4");
    expect((form.get("video") as File).name).toBe("录制 clip.mp4");
  });

  it("encodes artifact path segments without retaining traversal separators", () => {
    expect(rescueArtifactUrl("job id", "reports/../报告.json")).toBe("/api/rescue/jobs/job%20id/artifacts/reports/%2E%2E/%E6%8A%A5%E5%91%8A%2Ejson");
  });

  it("orders rescue SSE events and reconnects from the last sequence", () => {
    const urls: string[] = [];
    const listeners: Array<Record<string, EventListenerOrEventListenerObject>> = [];
    const received: number[] = [];
    const subscription = subscribeToRescueEvents("a".repeat(32), (event) => received.push(event.sequence), () => undefined, {
      sourceFactory: (url) => { urls.push(url); const map: Record<string, EventListenerOrEventListenerObject> = {}; listeners.push(map); return { addEventListener: (name, handler) => { map[name] = handler; }, close: () => undefined }; },
      reconnectDelayMs: 0,
      setTimer: (handler) => { if (typeof handler === "function") handler(); return 0 as unknown as ReturnType<typeof setTimeout>; },
    });
    (listeners[0].status as EventListener)(new MessageEvent("status", { data: JSON.stringify({ sequence: 2, status: "scanning", message: "scan", progress_percent: 1, created_at: "x" }) }));
    (listeners[0].error as EventListener)(new Event("error"));
    expect(urls[1]).toContain("after=2");
    (listeners[1].status as EventListener)(new MessageEvent("status", { data: JSON.stringify({ sequence: 1, status: "scanning", message: "old", progress_percent: 1, created_at: "x" }) }));
    expect(received).toEqual([2]);
    subscription.close();
  });

  it("closes the rescue SSE stream when review is required", () => {
    const source = new FakeSource();
    subscribeToRescueEvents(
      "a".repeat(32),
      vi.fn(),
      vi.fn(),
      { sourceFactory: () => source },
    );

    source.emit("status", {
      data: JSON.stringify({
        sequence: 3,
        status: "needs_review",
        message: "Output needs review",
        progress_percent: 100,
        created_at: "2026-08-04T00:00:03Z",
      }),
    });

    expect(source.closed).toBe(true);
  });
});

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

const PUBLISH_JOB: PublishJobResponse = {
  job_id: "b".repeat(32),
  status: "awaiting_confirmation",
  message: "Plan ready for confirmation",
  created_at: "2026-08-01T00:00:00Z",
  updated_at: "2026-08-01T00:00:01Z",
  upload_size_bytes: 10,
  progress_percent: 45,
  profile_id: "compatible_mp4",
  warnings: [],
  error: null,
  links: { plan: "/api/publish/jobs/example/plan" },
};

const PROFILES: PublishProfile[] = [
  {
    id: "compatible_mp4",
    version: "1.0.0",
    width: null,
    height: null,
    maximum_fps: 60,
    video_codec: "h264",
    audio_codec: "aac",
    pixel_format: "yuv420p",
    container: "mp4",
  },
];

const PRIVACY_JOB: PrivacyJobResponse = {
  job_id: "c".repeat(32),
  status: "awaiting_review",
  message: "Review privacy risks",
  created_at: "2026-08-03T00:00:00Z",
  updated_at: "2026-08-03T00:00:01Z",
  upload_size_bytes: 10,
  progress_percent: 35,
  profile_id: "public",
  plan_digest: null,
  warnings: [],
  error: null,
  links: { risk_map: "/api/privacy/jobs/example/risk-map" },
};

const PRIVACY_PROFILES: ShareAudienceProfile[] = [
  {
    id: "public",
    version: "1",
    forbidden_metadata_categories: ["author", "location"],
    required_manual_review_categories: ["visual", "audio"],
    default_visual_style: "blur",
    qr_handling: "redact_by_default",
    final_human_review_required: true,
  },
];

const PRIVACY_REVIEW: PrivacyReviewPayload = {
  reviews: [],
  manual_visual_regions: [
    {
      start_seconds: 0.5,
      end_seconds: 1.5,
      box: { x_min: 0.1, y_min: 0.2, x_max: 0.4, y_max: 0.6 },
      style: "pixelate",
    },
  ],
  manual_audio_intervals: [
    { start_seconds: 1.5, end_seconds: 2.25, style: "mute" },
  ],
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

  it("uses encoded Publish Ready job and artifact paths", async () => {
    const fetcher = vi.fn(async (_url: string, _init?: RequestInit) =>
      new Response(JSON.stringify(PUBLISH_JOB), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    await getPublishJob("job/id 中文", fetcher as typeof fetch);
    await getPublishPlan("job/id 中文", fetcher as typeof fetch);
    await cancelPublishJob("job/id 中文", fetcher as typeof fetch);

    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      "/api/publish/jobs/job%2Fid%20%E4%B8%AD%E6%96%87",
      "/api/publish/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/plan",
      "/api/publish/jobs/job%2Fid%20%E4%B8%AD%E6%96%87",
    ]);
    expect(fetcher.mock.calls[2]?.[1]).toEqual({ method: "DELETE" });
    expect(publishArtifactUrl("job/id 中文", "preview/片段 six.mp4")).toBe(
      "/api/publish/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/artifacts/preview/%E7%89%87%E6%AE%B5%20six.mp4",
    );
  });

  it("lists profiles and uploads the selected profile as multipart data", async () => {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/profiles")) {
        return new Response(JSON.stringify(PROFILES), { status: 200 });
      }
      const form = init?.body as FormData;
      expect(form.get("profile_id")).toBe("social_vertical_9_16");
      expect(form.get("video")).toBeInstanceOf(File);
      return new Response(JSON.stringify(PUBLISH_JOB), { status: 202 });
    });

    await expect(listPublishProfiles(fetcher as typeof fetch)).resolves.toEqual(
      PROFILES,
    );
    await createPublishJob(
      new File(["video"], "源 video.mp4", { type: "video/mp4" }),
      "social_vertical_9_16",
      fetcher as typeof fetch,
    );
    expect(fetcher).toHaveBeenLastCalledWith(
      "/api/publish/jobs",
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );
  });

  it("confirms an exact digest using JSON", async () => {
    const fetcher = vi.fn(async () =>
      new Response(JSON.stringify(PUBLISH_JOB), { status: 202 }),
    );
    const digest = "d".repeat(64);

    await confirmPublishJob(PUBLISH_JOB.job_id, digest, fetcher as typeof fetch);

    expect(fetcher).toHaveBeenCalledWith(
      `/api/publish/jobs/${PUBLISH_JOB.job_id}/confirm`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ plan_digest: digest }),
      },
    );
  });

  it("surfaces the local API detail message", async () => {
    const fetcher = vi.fn(async () =>
      new Response(JSON.stringify({ detail: "Plan digest does not match." }), {
        status: 409,
        headers: { "content-type": "application/json" },
      }),
    );

    await expect(
      confirmPublishJob(PUBLISH_JOB.job_id, "0".repeat(64), fetcher as typeof fetch),
    ).rejects.toMatchObject({
      message: "Plan digest does not match.",
      status: 409,
    });
  });

  it("formats FastAPI validation detail arrays as readable messages", async () => {
    const fetcher = vi.fn(async () =>
      new Response(
        JSON.stringify({
          detail: [
            {
              type: "missing",
              loc: ["body", "profile_id"],
              msg: "Field required",
              input: null,
            },
          ],
        }),
        { status: 422, headers: { "content-type": "application/json" } },
      ),
    );

    await expect(
      createPublishJob(
        new File(["video"], "source.mp4", { type: "video/mp4" }),
        "compatible_mp4",
        fetcher as typeof fetch,
      ),
    ).rejects.toMatchObject({
      message: "body.profile_id: Field required",
      status: 422,
    });
  });

  it("uses every Safe Sharing route with encoded job and artifact paths", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      if (url.endsWith("/profiles")) {
        return new Response(JSON.stringify(PRIVACY_PROFILES), { status: 200 });
      }
      if (url.endsWith("/risk-map")) {
        return new Response(
          JSON.stringify({
            schema_version: "0.1",
            input_hash: "f".repeat(64),
            profile: "public",
            duration_seconds: 4,
            risks: [],
            is_private: true,
          }),
          { status: 200 },
        );
      }
      if (url.endsWith("/plan")) {
        return new Response(
          JSON.stringify({
            schema_version: "0.1",
            input_hash: "f".repeat(64),
            profile: "public",
            duration_seconds: 4,
            effective_config: {},
            risks: [],
            actions: [],
            artifacts: [],
            digest: "d".repeat(64),
          }),
          { status: 200 },
        );
      }
      if (init?.method === "DELETE") return new Response(null, { status: 204 });
      return new Response(JSON.stringify(PRIVACY_JOB), {
        status: init?.method === "POST" ? 202 : 200,
      });
    });
    const file = new File(["video"], "源 video.mp4", { type: "video/mp4" });

    await listPrivacyProfiles(fetcher as typeof fetch);
    await createPrivacyJob(file, "public", true, fetcher as typeof fetch);
    await getPrivacyJob("job/id 中文", fetcher as typeof fetch);
    await getPrivacyRiskMap("job/id 中文", fetcher as typeof fetch);
    await reviewPrivacyJob("job/id 中文", PRIVACY_REVIEW, fetcher as typeof fetch);
    await preparePrivacyJob("job/id 中文", fetcher as typeof fetch);
    await getPrivacyPlan("job/id 中文", fetcher as typeof fetch);
    await confirmPrivacyJob("job/id 中文", "d".repeat(64), fetcher as typeof fetch);
    await expect(
      deletePrivacyJob("job/id 中文", fetcher as typeof fetch),
    ).resolves.toBeNull();

    expect(calls.map(([url]) => url)).toEqual([
      "/api/privacy/profiles",
      "/api/privacy/jobs",
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87",
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/risk-map",
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/review",
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/prepare",
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/plan",
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/confirm",
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87",
    ]);
    const createForm = calls[1]?.[1]?.body as FormData;
    expect(createForm.get("profile_id")).toBe("public");
    expect(createForm.get("enable_ocr")).toBe("true");
    expect(JSON.parse(String(calls[4]?.[1]?.body))).toEqual(PRIVACY_REVIEW);
    expect(privacyArtifactUrl("job/id 中文", "share-safe.mp4")).toBe(
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/artifacts/share-safe.mp4",
    );
    expect(privacyPrivateArtifactUrl("job/id 中文", "evidence/证据 one.png")).toBe(
      "/api/privacy/jobs/job%2Fid%20%E4%B8%AD%E6%96%87/private-artifacts/evidence/%E8%AF%81%E6%8D%AE%20one.png",
    );
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

  it("reconnects Publish Ready events and treats needs_review as terminal", () => {
    const urls: string[] = [];
    const sources: FakeSource[] = [];
    const timers: Array<() => void> = [];
    const events: PublishJobEvent[] = [];
    const factory = (url: string): FakeSource => {
      urls.push(url);
      const source = new FakeSource();
      sources.push(source);
      return source;
    };
    subscribeToPublishEvents(
      "publish/id",
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
        sequence: 7,
        status: "processing",
        message: "Processing locally",
        progress_percent: 72,
        created_at: "2026-08-01T00:00:02Z",
      }),
    });
    sources[0].emit("status", {
      data: JSON.stringify({
        sequence: 6,
        status: "planning",
        message: "Stale replay",
        progress_percent: 40,
        created_at: "2026-08-01T00:00:01Z",
      }),
    });
    sources[0].emit("error", {});
    timers[0]();
    sources[1].emit("status", {
      data: JSON.stringify({
        sequence: 8,
        status: "needs_review",
        message: "Output needs review",
        progress_percent: 100,
        created_at: "2026-08-01T00:00:03Z",
      }),
    });

    expect(urls).toEqual([
      "/api/publish/jobs/publish%2Fid/events",
      "/api/publish/jobs/publish%2Fid/events?after=7",
    ]);
    expect(events.map((event) => event.status)).toEqual([
      "processing",
      "needs_review",
    ]);
    expect(sources[1].closed).toBe(true);
  });

  it("keeps one privacy event stream, ignores stale sequences, and resumes once", () => {
    const urls: string[] = [];
    const sources: FakeSource[] = [];
    const timers: Array<() => void> = [];
    const events: PrivacyJobEvent[] = [];
    const subscription = subscribeToPrivacyEvents(
      "privacy/job",
      (event) => events.push(event),
      vi.fn(),
      {
        sourceFactory: (url) => {
          urls.push(url);
          const source = new FakeSource();
          sources.push(source);
          return source;
        },
        setTimer: ((callback: TimerHandler) => {
          timers.push(callback as () => void);
          return 1;
        }) as typeof setTimeout,
        clearTimer: vi.fn() as unknown as typeof clearTimeout,
      },
    );
    sources[0].emit("status", {
      data: JSON.stringify({
        sequence: 3,
        status: "scanning",
        message: "Scanning locally",
        progress_percent: 35,
        created_at: "2026-08-03T00:00:02Z",
      }),
    });
    sources[0].emit("status", {
      data: JSON.stringify({
        sequence: 2,
        status: "inspecting",
        message: "Stale replay",
        progress_percent: 10,
        created_at: "2026-08-03T00:00:01Z",
      }),
    });
    sources[0].emit("error", {});
    sources[0].emit("error", {});
    expect(timers).toHaveLength(1);
    timers[0]();
    sources[1].emit("status", {
      data: JSON.stringify({
        sequence: 4,
        status: "needs_review",
        message: "Output needs review",
        progress_percent: 100,
        created_at: "2026-08-03T00:00:03Z",
      }),
    });

    expect(urls).toEqual([
      "/api/privacy/jobs/privacy%2Fjob/events",
      "/api/privacy/jobs/privacy%2Fjob/events?after=3",
    ]);
    expect(events.map((event) => event.sequence)).toEqual([3, 4]);
    expect(sources[1].closed).toBe(true);
    subscription.close();
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
