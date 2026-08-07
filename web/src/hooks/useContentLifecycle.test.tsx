import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CONTENT_MAP, CONTENT_PLAN, contentJob } from "../test/contentFixtures";
import { mergeContentSnapshot, useContentLifecycle, type ContentLifecycleApi } from "./useContentLifecycle";

function api(status: ReturnType<typeof contentJob>["status"]): ContentLifecycleApi {
  return {
    getJob: vi.fn(async () => contentJob(status)),
    getMap: vi.fn(async () => CONTENT_MAP),
    getPlan: vi.fn(async () => CONTENT_PLAN),
    getPreviews: vi.fn(async () => []),
    subscribeToEvents: vi.fn(() => ({ close: vi.fn() })),
  };
}

describe("useContentLifecycle", () => {
  it("rejects older revisions and stage regressions", () => {
    const current = contentJob("rendering", { revision: 2 });
    expect(mergeContentSnapshot(current, contentJob("awaiting_review", { revision: 1 }))).toBe(current);
    expect(mergeContentSnapshot(current, contentJob("planning", { revision: 2 }))).toBe(current);
  });

  it("recovers a refreshable plan and its private preview manifest", async () => {
    const lifecycleApi = api("ready_to_confirm");
    vi.mocked(lifecycleApi.getPreviews).mockResolvedValueOnce([
      {
        action_id: CONTENT_PLAN.actions[0].id,
        action_ranges: CONTENT_PLAN.actions[0].source_ranges,
        context_ranges: [{ start_seconds: 7, end_seconds: 9 }],
        relative_paths: ["preview/action-000-joined.mp4"],
        artifact_hashes: ["f".repeat(64)],
        encoding_parameters: {},
        identity: "preview-id",
      },
    ]);
    const { result } = renderHook(() =>
      useContentLifecycle({ api: lifecycleApi, jobId: "d".repeat(32) }),
    );

    await waitFor(() => expect(result.current.plan).toEqual(CONTENT_PLAN));
    expect(result.current.contentMap).toEqual(CONTENT_MAP);
    expect(result.current.previews).toHaveLength(1);
  });

  it("refreshes after an ordered SSE notification", async () => {
    const lifecycleApi = api("awaiting_review");
    let notify: ((event: Parameters<ContentLifecycleApi["subscribeToEvents"]>[1] extends (event: infer T) => void ? T : never) => void) | null = null;
    lifecycleApi.subscribeToEvents = vi.fn((_jobId, onEvent) => {
      notify = onEvent;
      return { close: vi.fn() };
    });
    const { result } = renderHook(() =>
      useContentLifecycle({ api: lifecycleApi, jobId: "d".repeat(32) }),
    );
    await waitFor(() => expect(result.current.job?.status).toBe("awaiting_review"));
    await act(async () => {
      notify?.({
        sequence: 1,
        status: "awaiting_review",
        message: "ready",
        progress_percent: 55,
        revision: 0,
        created_at: "2026-08-06T00:00:02Z",
      });
    });
    await waitFor(() => expect(lifecycleApi.getJob).toHaveBeenCalledTimes(2));
  });
});
