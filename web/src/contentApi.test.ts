import { describe, expect, it, vi } from "vitest";
import {
  confirmContentJob,
  contentArtifactUrl,
  createContentJob,
  deleteContentJob,
  reviseContentStoryboard,
  subscribeToContentEvents,
  prepareContentAI,
  reviewContentAI,
  applyContentAI,
  cancelContentAI,
} from "./api";
import { CONTENT_PLAN } from "./test/contentFixtures";

describe("useful-content API client", () => {
  it("encodes local video, transcript, goal, and strict config as multipart", async () => {
    let request: RequestInit | undefined;
    await createContentJob(
      new File(["video"], "中文 source.mp4"),
      {
        goal: "selected_clips",
        transcript: new File(["cue"], "字幕.srt"),
        config: { allow_reorder: true },
      },
      async (_url, init) => {
        request = init;
        return new Response(JSON.stringify({ job_id: "d".repeat(32) }), { status: 202 });
      },
    );
    const form = request?.body as FormData;
    expect(form.get("goal")).toBe("selected_clips");
    expect((form.get("video") as File).name).toBe("中文 source.mp4");
    expect((form.get("transcript") as File).name).toBe("字幕.srt");
    expect(JSON.parse(String(form.get("config_json")))).toEqual({ allow_reorder: true, goal: "selected_clips" });
  });

  it("sends revision and exact digest confirmation payloads", async () => {
    const bodies: unknown[] = [];
    const fetcher: typeof fetch = async (_url, init) => {
      bodies.push(JSON.parse(String(init?.body)));
      return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
    };
    await reviseContentStoryboard("job", {
      expected_revision: 3,
      ranges: [{ kind: "keep", start_seconds: 1, end_seconds: 2 }],
      selected_range_order: [],
      reorder_acknowledged: false,
      chapter_titles: {},
    }, fetcher);
    await confirmContentJob("job", CONTENT_PLAN, 3, fetcher);
    expect(bodies[0]).toMatchObject({ expected_revision: 3 });
    expect(bodies[1]).toEqual({
      plan_digest: CONTENT_PLAN.plan_digest,
      revision: 3,
      accepted_action_ids: [CONTENT_PLAN.actions[0].id],
    });
  });

  it("handles terminal deletion and strictly encodes artifact segments", async () => {
    await expect(deleteContentJob("job", async () => new Response(null, { status: 204 }))).resolves.toBeNull();
    expect(contentArtifactUrl("job id", "reports/../来源.json")).toBe(
      "/api/content/jobs/job%20id/artifacts/reports/%2E%2E/%E6%9D%A5%E6%BA%90%2Ejson",
    );
  });

  it("reconnects SSE from the last accepted sequence and ignores replay", () => {
    const urls: string[] = [];
    const listeners: Array<Record<string, EventListenerOrEventListenerObject>> = [];
    const received: number[] = [];
    const subscription = subscribeToContentEvents(
      "d".repeat(32),
      (event) => received.push(event.sequence),
      vi.fn(),
      {
        sourceFactory: (url) => {
          urls.push(url);
          const handlers: Record<string, EventListenerOrEventListenerObject> = {};
          listeners.push(handlers);
          return {
            addEventListener: (name, handler) => { handlers[name] = handler; },
            close: vi.fn(),
          };
        },
        reconnectDelayMs: 0,
        setTimer: (handler) => {
          if (typeof handler === "function") handler();
          return 0 as unknown as ReturnType<typeof setTimeout>;
        },
      },
    );
    (listeners[0].status as EventListener)(new MessageEvent("status", { data: JSON.stringify({ sequence: 2, status: "mapping", message: "map", progress_percent: 30, revision: 0, created_at: "x" }) }));
    (listeners[0].error as EventListener)(new Event("error"));
    expect(urls[1]).toContain("after=2");
    (listeners[1].status as EventListener)(new MessageEvent("status", { data: JSON.stringify({ sequence: 1, status: "mapping", message: "old", progress_percent: 30, revision: 0, created_at: "x" }) }));
    expect(received).toEqual([2]);
    subscription.close();
  });

  it("uses typed private AI prepare, review, and optimistic apply payloads", async () => {
    const calls: Array<{ url: string; body: unknown }> = [];
    const fetcher: typeof fetch = async (url, init) => {
      calls.push({ url: String(url), body: init?.body ? JSON.parse(String(init.body)) : null });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    };
    await prepareContentAI("job id", {
      semantic_model_id: "qwen2.5:7b",
      asr_model_id: "small",
      asr_language: "zh",
      ollama_endpoint: "http://127.0.0.1:11434",
      locale: "zh-CN",
      device: "cpu",
      allow_model_download: false,
      maximum_suggestions: 12,
    }, fetcher);
    await reviewContentAI("job id", [{ suggestion_id: `suggestion_${"a".repeat(64)}`, decision: "reject" }], fetcher);
    await applyContentAI("job id", 4, fetcher);
    await cancelContentAI("job id", fetcher);
    expect(calls.map((item) => item.url)).toEqual([
      "/api/content/jobs/job%20id/ai/prepare",
      "/api/content/jobs/job%20id/ai/review",
      "/api/content/jobs/job%20id/ai/apply",
      "/api/content/jobs/job%20id/ai",
    ]);
    expect(calls[2].body).toEqual({ expected_revision: 4 });
  });
});
