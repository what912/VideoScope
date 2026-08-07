import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CONTENT_JOB_ID, CONTENT_MAP, CONTENT_PLAN, contentJob } from "../test/contentFixtures";
import type { ContentApi } from "./ContentView";
import { ContentView } from "./ContentView";

function fakeApi(status: ReturnType<typeof contentJob>["status"] = "awaiting_review"): ContentApi {
  return {
    createJob: vi.fn(async (_video, options) => contentJob("queued", { goal: options.goal })),
    getJob: vi.fn(async () => contentJob(status, { plan_digest: status === "ready_to_confirm" ? CONTENT_PLAN.plan_digest : null })),
    getMap: vi.fn(async () => CONTENT_MAP),
    getPlan: vi.fn(async () => CONTENT_PLAN),
    getPreviews: vi.fn(async () => [
      {
        action_id: CONTENT_PLAN.actions[0].id,
        action_ranges: CONTENT_PLAN.actions[0].source_ranges,
        context_ranges: [{ start_seconds: 7, end_seconds: 9 }],
        relative_paths: ["preview/action-000-joined.mp4"],
        artifact_hashes: ["f".repeat(64)],
        encoding_parameters: {},
        identity: "preview-id",
      },
    ]),
    revise: vi.fn(async (_jobId, payload) => contentJob("awaiting_review", { revision: payload.expected_revision + 1 })),
    createPreviews: vi.fn(async () => contentJob("ready_to_confirm", { plan_digest: CONTENT_PLAN.plan_digest })),
    confirm: vi.fn(async () => contentJob("rendering")),
    deleteJob: vi.fn(async () => null),
    subscribeToEvents: vi.fn(() => ({ close: vi.fn() })),
    artifactUrl: vi.fn((_jobId, path) => `/content/${path}`),
    previewUrl: vi.fn((_jobId, path) => `/private/${path}`),
  };
}

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:content-source"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

describe("ContentView", () => {
  it("offers all three goals and uploads the chosen goal with a private transcript", async () => {
    const api = fakeApi();
    const onJobChange = vi.fn();
    const user = userEvent.setup();
    const view = render(<ContentView locale="en" api={api} onJobChange={onJobChange} />);
    const files = view.container.querySelectorAll<HTMLInputElement>('input[type="file"]');
    await user.click(screen.getByRole("radio", { name: /Selected Clips/i }));
    await user.upload(files[0], new File(["video"], "source.mp4", { type: "video/mp4" }));
    await user.upload(files[1], new File(["cue"], "notes.srt", { type: "text/plain" }));
    await user.click(screen.getByRole("button", { name: "Build content map" }));

    expect(api.createJob).toHaveBeenCalledWith(
      expect.any(File),
      expect.objectContaining({
        goal: "selected_clips",
        transcript: expect.any(File),
        config: expect.objectContaining({ allow_reorder: true, export_clips: true }),
      }),
    );
    expect(onJobChange).toHaveBeenCalledWith(CONTENT_JOB_ID);
    expect(await screen.findByRole("heading", { name: "Source structure" })).toBeVisible();
  });

  it("edits an exact locked range using labelled keyboard-capable inputs", async () => {
    const api = fakeApi();
    const user = userEvent.setup();
    render(<ContentView locale="en" api={api} initialJobId={CONTENT_JOB_ID} onJobChange={vi.fn()} />);
    await screen.findByRole("heading", { name: "Exact source ranges" });
    await user.selectOptions(screen.getByLabelText("Range purpose"), "locked_keep");
    await user.clear(screen.getByLabelText("Start (seconds)"));
    await user.type(screen.getByLabelText("Start (seconds)"), "2.5");
    await user.type(screen.getByLabelText("End (seconds)"), "6.5");
    await user.type(screen.getByLabelText("Label (optional)"), "Do not cut");
    await user.click(screen.getByRole("button", { name: "Add range" }));
    await user.click(screen.getByRole("button", { name: "Apply revision" }));

    expect(api.revise).toHaveBeenCalledWith(
      CONTENT_JOB_ID,
      expect.objectContaining({
        expected_revision: 0,
        ranges: [expect.objectContaining({ kind: "locked_keep", start_seconds: 2.5, end_seconds: 6.5 })],
      }),
    );
  });

  it("shows private join evidence and confirms the exact digest-bound plan", async () => {
    const api = fakeApi("ready_to_confirm");
    const user = userEvent.setup();
    render(<ContentView locale="en" api={api} initialJobId={CONTENT_JOB_ID} onJobChange={vi.fn()} />);
    expect(await screen.findByRole("heading", { name: "Local private join preview" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Confirm exact edit plan" })).toBeVisible();
    expect(screen.getByText(CONTENT_PLAN.plan_digest)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Confirm and render locally" }));
    expect(api.confirm).toHaveBeenCalledWith(CONTENT_JOB_ID, CONTENT_PLAN, 0);
  });

  it.each(["completed", "partial", "needs_review", "failed"] as const)(
    "renders a truthful %s terminal state without inventing success",
    async (status) => {
      const api = fakeApi(status);
      render(<ContentView locale="zh-CN" api={api} initialJobId={CONTENT_JOB_ID} onJobChange={vi.fn()} />);
      expect(await screen.findByRole("heading", { name: "有用内容结果" })).toBeVisible();
      const expected = {
        completed: "已完成",
        partial: "部分完成",
        needs_review: "需要复核",
        failed: "失败",
      }[status];
      expect(screen.getByText(new RegExp(expected))).toBeVisible();
    },
  );
});
