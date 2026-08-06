import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import {
  mergePrivacySnapshot,
  PrivacyView,
  type PrivacyApi,
} from "./PrivacyView";
import {
  PRIVACY_JOB_ID,
  PRIVACY_PLAN,
  PRIVACY_PLAN_DIGEST,
  PRIVACY_PROFILES,
  PRIVACY_RISK_MAP,
  PRIVACY_TECHNICAL_REPORT,
  privacyJob,
} from "../test/privacyFixtures";

function fakePrivacyApi(
  initialStatus: Parameters<typeof privacyJob>[0] = "awaiting_review",
): PrivacyApi {
  return {
    listProfiles: vi.fn(async () => PRIVACY_PROFILES),
    createJob: vi.fn(async () => privacyJob("queued")),
    getJob: vi.fn(async () => privacyJob(initialStatus)),
    getRiskMap: vi.fn(async () => PRIVACY_RISK_MAP),
    review: vi.fn(async () => privacyJob("awaiting_review")),
    prepare: vi.fn(async () =>
      privacyJob("awaiting_confirmation", { plan_digest: PRIVACY_PLAN_DIGEST }),
    ),
    getPlan: vi.fn(async () => PRIVACY_PLAN),
    confirm: vi.fn(async () => privacyJob("processing")),
    deleteJob: vi.fn(async () => null),
    getTechnicalReport: vi.fn(async () => PRIVACY_TECHNICAL_REPORT),
    subscribeToEvents: vi.fn(() => ({ close: vi.fn() })),
    publicArtifactUrl: vi.fn((_jobId, path) => `/public/${path}`),
    privateArtifactUrl: vi.fn((_jobId, path) => `/private/${path}`),
  };
}

beforeEach(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:privacy-source"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

it("does not let a delayed privacy snapshot regress the current stage", () => {
  const processing = privacyJob("processing", {
    updated_at: "2026-08-03T00:00:08Z",
  });
  const delayedPlanning = privacyJob("planning", {
    updated_at: "2026-08-03T00:00:09Z",
  });

  expect(mergePrivacySnapshot(processing, delayedPlanning)).toBe(processing);
});

it("uploads a local source with the selected audience profile", async () => {
  const api = fakePrivacyApi();
  const onJobChange = vi.fn();
  const user = userEvent.setup();
  const view = render(
    <PrivacyView api={api} locale="en" onJobChange={onJobChange} />,
  );
  const fileInput = view.container.querySelector<HTMLInputElement>(
    'input[type="file"]',
  );
  expect(fileInput).not.toBeNull();

  await user.click(await screen.findByRole("radio", { name: /family/i }));
  await user.upload(
    fileInput!,
    new File(["video"], "family clip.mp4", { type: "video/mp4" }),
  );
  await user.click(screen.getByRole("button", { name: "Scan before sharing" }));

  expect(api.createJob).toHaveBeenCalledWith(
    expect.any(File),
    "family",
    false,
  );
  expect(onJobChange).toHaveBeenCalledWith(PRIVACY_JOB_ID);
  expect(api.listProfiles).toHaveBeenCalledOnce();
});

it("plays the current local source and keeps video, timeline, risk, and overlay synchronized", async () => {
  const api = fakePrivacyApi("awaiting_review");
  const user = userEvent.setup();
  const view = render(
    <PrivacyView api={api} locale="en" onJobChange={vi.fn()} />,
  );
  const fileInput = view.container.querySelector<HTMLInputElement>(
    'input[type="file"]',
  );
  await user.upload(
    fileInput!,
    new File(["video"], "private source.mp4", { type: "video/mp4" }),
  );
  await user.click(screen.getByRole("button", { name: "Scan before sharing" }));

  const video = (await screen.findByLabelText(
    "Local source video",
  )) as HTMLVideoElement;
  expect(video).toHaveAttribute("src", "blob:privacy-source");
  await user.click(screen.getByRole("button", { name: "Review Face-like region" }));
  expect(video.currentTime).toBe(1);
  expect(screen.getByRole("slider", { name: "Selected privacy region" })).toBeVisible();

  video.currentTime = 4.2;
  fireEvent.timeUpdate(video);
  expect(screen.getByRole("slider", { name: "Privacy timeline" })).toHaveAttribute(
    "aria-valuenow",
    "4.2",
  );
  expect(
    screen.queryByRole("slider", { name: "Selected privacy region" }),
  ).not.toBeInTheDocument();
});

it("fails closed when a recovered review no longer has a browser source URL", async () => {
  const api = fakePrivacyApi("awaiting_review");
  render(
    <PrivacyView
      api={api}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );

  expect(
    await screen.findByText(
      "Source playback is unavailable after recovery. Re-select the source before revising or use private evidence for review.",
    ),
  ).toBeVisible();
  expect(screen.queryByLabelText("Local source video")).not.toBeInTheDocument();
});

it.each([
  {
    status: "awaiting_review" as const,
    stage: "risk map",
    context: "等待人工复核隐私风险",
    finalHeading: "隐私风险复核",
  },
  {
    status: "awaiting_confirmation" as const,
    stage: "plan",
    context: "等待确认精确处理计划",
    finalHeading: "检查脱敏预览",
  },
  {
    status: "completed" as const,
    stage: "technical report",
    context: "安全分享包已完成",
    finalHeading: "分享副本已通过本地验证",
  },
])(
  "keeps $status recovery controls available when the $stage cannot load",
  async ({ status, stage, context, finalHeading }) => {
    const api = fakePrivacyApi(status);
    const stageFailure = new Error(`${stage} private implementation detail`);
    const stageLoader =
      status === "awaiting_review"
        ? api.getRiskMap
        : status === "awaiting_confirmation"
          ? api.getPlan
          : api.getTechnicalReport;
    vi.mocked(stageLoader).mockRejectedValueOnce(stageFailure);
    const user = userEvent.setup();
    render(
      <PrivacyView
        api={api}
        locale="zh-CN"
        initialJobId={PRIVACY_JOB_ID}
        onJobChange={vi.fn()}
      />,
    );

    expect(await screen.findByRole("heading", { name: context })).toBeVisible();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("本地任务未能完成（safe_sharing）");
    expect(alert).not.toHaveTextContent("private implementation detail");
    expect(screen.getByRole("button", { name: "取消任务" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "安全重试" }));

    expect(stageLoader).toHaveBeenCalledTimes(2);
    expect(await screen.findByRole("heading", { name: finalHeading })).toBeVisible();
  },
);

it("requests cancellation for an active local privacy task", async () => {
  const api = fakePrivacyApi("scanning");
  api.deleteJob = vi.fn(async () =>
    privacyJob("scanning", { message: "Cancellation requested" }),
  );
  const user = userEvent.setup();
  render(
    <PrivacyView
      api={api}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );

  await user.click(await screen.findByRole("button", { name: "Cancel task" }));
  expect(api.deleteJob).toHaveBeenCalledWith(PRIVACY_JOB_ID);
  expect(await screen.findByRole("heading", { name: "Cancellation requested" })).toBeVisible();
});

it("reviews risks, edits a box, adds manual intervals, previews, and confirms the exact digest", async () => {
  const api = fakePrivacyApi();
  const user = userEvent.setup();
  render(
    <PrivacyView
      api={api}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );

  await user.click(
    await screen.findByRole("button", { name: "Review Face-like region" }),
  );
  const timeline = screen.getByRole("slider", { name: "Privacy timeline" });
  timeline.focus();
  await user.keyboard("{ArrowRight}");
  expect(timeline).toHaveAttribute("aria-valuenow", "1.1");
  await user.click(screen.getByRole("button", { name: "Redact" }));
  const overlay = screen.getByRole("slider", { name: "Selected privacy region" });
  overlay.focus();
  await user.keyboard("{ArrowRight}{ArrowDown}");

  await user.click(screen.getByRole("button", { name: "Add visual region" }));
  const manualOverlay = screen.getByRole("slider", {
    name: "Selected privacy region",
  });
  manualOverlay.focus();
  await user.keyboard("{ArrowRight}");
  await user.clear(screen.getByLabelText("Audio start (seconds)"));
  await user.type(screen.getByLabelText("Audio start (seconds)"), "2.5");
  await user.clear(screen.getByLabelText("Audio end (seconds)"));
  await user.type(screen.getByLabelText("Audio end (seconds)"), "3.5");
  await user.click(screen.getByRole("button", { name: "Add audio mute interval" }));
  await user.click(screen.getByRole("button", { name: "Generate preview" }));

  await waitFor(() => expect(api.review).toHaveBeenCalledOnce());
  const payload = vi.mocked(api.review).mock.calls[0]?.[1];
  expect(payload?.reviews).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        risk_id: PRIVACY_RISK_MAP.risks[0].id,
        decision: "redact",
        style: "blur",
        edited_box: expect.objectContaining({ x_min: 0.11, y_min: 0.21 }),
      }),
    ]),
  );
  expect(payload?.manual_visual_regions).toHaveLength(1);
  expect(payload?.manual_visual_regions[0]?.box).toEqual(
    expect.objectContaining({ x_min: 0.31 }),
  );
  expect(payload?.manual_audio_intervals).toEqual([
    expect.objectContaining({ start_seconds: 2.5, end_seconds: 3.5 }),
  ]);
  expect(await screen.findByText("Review the redaction preview")).toBeVisible();

  await user.click(
    screen.getByRole("button", { name: "Confirm and create share copy" }),
  );
  expect(api.confirm).toHaveBeenCalledWith(PRIVACY_JOB_ID, PRIVACY_PLAN_DIGEST);
  expect(
    await screen.findByRole("heading", { name: "Rendering the confirmed sharing copy" }),
  ).toBeVisible();
  expect(
    screen.queryByRole("heading", { name: "Privacy risk review" }),
  ).not.toBeInTheDocument();
});

it("edits, selects, resizes, and removes stable manual visual and audio entries", async () => {
  const api = fakePrivacyApi();
  const user = userEvent.setup();
  render(
    <PrivacyView
      api={api}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );
  await screen.findByRole("heading", { name: "Privacy risk review" });

  await user.click(screen.getByRole("button", { name: "Add visual region" }));
  await user.click(screen.getByRole("button", { name: "Add visual region" }));
  const first = screen.getByRole("button", { name: "Edit manual visual region 1" });
  const second = screen.getByRole("button", { name: "Edit manual visual region 2" });
  const secondId = second.getAttribute("data-manual-id");
  await user.click(first);
  await user.clear(screen.getByLabelText("Visual start (seconds)"));
  await user.type(screen.getByLabelText("Visual start (seconds)"), "0.5");
  await user.clear(screen.getByLabelText("Visual end (seconds)"));
  await user.type(screen.getByLabelText("Visual end (seconds)"), "2.5");
  await user.selectOptions(screen.getByLabelText("Visual style"), "pixelate");

  const stage = screen.getByLabelText("Local video review area");
  vi.spyOn(stage, "getBoundingClientRect").mockReturnValue({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 1000,
    bottom: 500,
    width: 1000,
    height: 500,
    toJSON: () => ({}),
  });
  const handle = screen.getByRole("button", {
    name: "Resize selected privacy region",
  });
  fireEvent.pointerDown(handle, { pointerId: 1, clientX: 700, clientY: 350 });
  fireEvent.pointerMove(handle, { pointerId: 1, clientX: 750, clientY: 400 });
  fireEvent.pointerUp(handle, { pointerId: 1, clientX: 750, clientY: 400 });

  await user.click(second);
  await user.click(screen.getByRole("button", { name: "Remove selected visual region" }));
  expect(screen.getByRole("button", { name: "Edit manual visual region 1" })).toHaveAttribute(
    "data-manual-id",
    expect.not.stringMatching(secondId ?? ""),
  );

  await user.clear(screen.getByLabelText("New mute start (seconds)"));
  await user.type(screen.getByLabelText("New mute start (seconds)"), "3");
  await user.clear(screen.getByLabelText("New mute end (seconds)"));
  await user.type(screen.getByLabelText("New mute end (seconds)"), "4");
  await user.click(screen.getByRole("button", { name: "Add audio mute interval" }));
  await user.clear(screen.getByLabelText("Mute interval 1 start (seconds)"));
  await user.type(screen.getByLabelText("Mute interval 1 start (seconds)"), "3.2");

  await user.click(screen.getByRole("button", { name: "Remove mute interval 1" }));
  expect(screen.queryByLabelText("Mute interval 1 start (seconds)")).not.toBeInTheDocument();
  await user.clear(screen.getByLabelText("New mute start (seconds)"));
  await user.type(screen.getByLabelText("New mute start (seconds)"), "3.2");
  await user.click(screen.getByRole("button", { name: "Add audio mute interval" }));

  await user.click(screen.getByRole("button", { name: "Generate preview" }));
  const payload = vi.mocked(api.review).mock.calls[0]?.[1];
  expect(payload?.manual_visual_regions).toEqual([
    expect.objectContaining({
      start_seconds: 0.5,
      end_seconds: 2.5,
      style: "pixelate",
      box: expect.objectContaining({ x_max: 0.75, y_max: 0.8 }),
    }),
  ]);
  expect(payload?.manual_audio_intervals).toEqual([
    expect.objectContaining({ start_seconds: 3.2, end_seconds: 4 }),
  ]);
});

it("shows scanner warnings and a useful no-risk review state", async () => {
  const api = fakePrivacyApi();
  api.getJob = vi.fn(async () =>
    privacyJob("awaiting_review", {
      warnings: ["suspicious_text scanner unavailable; use manual regions"],
    }),
  );
  api.getRiskMap = vi.fn(async () => ({ ...PRIVACY_RISK_MAP, risks: [] }));
  render(
    <PrivacyView
      api={api}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );

  expect(
    await screen.findByText("No automatic risks were proposed"),
  ).toBeVisible();
  expect(screen.getByRole("alert")).toHaveTextContent("scanner unavailable");
  expect(screen.getByRole("button", { name: "Add visual region" })).toBeVisible();
});

it("offers verified artifacts, starts a new task without deletion, and deletes explicitly", async () => {
  const api = fakePrivacyApi("completed");
  const onJobChange = vi.fn();
  const user = userEvent.setup();
  const view = render(
    <PrivacyView
      api={api}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={onJobChange}
    />,
  );

  const downloads = await screen.findByRole("list", { name: "Share package" });
  expect(within(downloads).getByRole("link", { name: /sharing copy/i })).toHaveAttribute(
    "href",
    "/public/share-safe.mp4",
  );
  await user.click(screen.getByRole("button", { name: "New Safe Sharing task" }));
  expect(api.deleteJob).not.toHaveBeenCalled();
  expect(onJobChange).toHaveBeenCalledWith(null);

  view.unmount();
  render(
    <PrivacyView
      api={api}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={onJobChange}
    />,
  );
  await user.click(await screen.findByRole("button", { name: "Delete local task data" }));
  expect(api.deleteJob).toHaveBeenCalledWith(PRIVACY_JOB_ID);
});

it("cancels at confirmation and conservatively restarts a needs-review task", async () => {
  const confirmApi = fakePrivacyApi("awaiting_confirmation");
  confirmApi.deleteJob = vi.fn(async () => null);
  const user = userEvent.setup();
  const confirmation = render(
    <PrivacyView
      api={confirmApi}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );
  await user.click(await screen.findByRole("button", { name: "Cancel task" }));
  expect(confirmApi.deleteJob).toHaveBeenCalledWith(PRIVACY_JOB_ID);
  confirmation.unmount();

  const recoveredApi = fakePrivacyApi("needs_review");
  recoveredApi.deleteJob = vi.fn(async () => null);
  render(
    <PrivacyView
      api={recoveredApi}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );
  await user.click(await screen.findByRole("button", { name: "Revise and rerun" }));
  expect(recoveredApi.deleteJob).not.toHaveBeenCalled();
  expect(await screen.findByText("Re-select the source video to revise this task.")).toBeVisible();
  expect(screen.getByRole("button", { name: "Scan before sharing" })).toBeVisible();
});

it("creates a new job from the retained local source when revising needs-review output", async () => {
  const api = fakePrivacyApi("needs_review");
  const secondJobId = "e".repeat(32);
  api.createJob = vi
    .fn()
    .mockResolvedValueOnce(privacyJob("queued"))
    .mockResolvedValueOnce(
      privacyJob("queued", { job_id: secondJobId, updated_at: "2026-08-03T00:00:02Z" }),
    );
  const user = userEvent.setup();
  const view = render(
    <PrivacyView api={api} locale="en" onJobChange={vi.fn()} />,
  );
  const fileInput = view.container.querySelector<HTMLInputElement>('input[type="file"]');
  await user.upload(fileInput!, new File(["video"], "retained.mp4", { type: "video/mp4" }));
  await user.click(screen.getByRole("button", { name: "Scan before sharing" }));
  await user.click(await screen.findByRole("button", { name: "Revise and rerun" }));

  expect(api.createJob).toHaveBeenCalledTimes(2);
  expect(api.createJob).toHaveBeenLastCalledWith(expect.any(File), "public", false);
});

it("labels failed and cancelled terminal tasks without implying a share copy exists", async () => {
  const cancelled = fakePrivacyApi("cancelled");
  const view = render(
    <PrivacyView
      api={cancelled}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );
  expect(
    await screen.findByRole("heading", { name: "Safe Sharing task cancelled" }),
  ).toBeVisible();

  view.unmount();
  const failed = fakePrivacyApi("failed");
  render(
    <PrivacyView
      api={failed}
      locale="en"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );
  expect(
    await screen.findByRole("heading", { name: "Safe Sharing task failed" }),
  ).toBeVisible();
});

it("renders Simplified Chinese review controls and the desktop precision notice", async () => {
  const api = fakePrivacyApi();
  render(
    <PrivacyView
      api={api}
      locale="zh-CN"
      initialJobId={PRIVACY_JOB_ID}
      onJobChange={vi.fn()}
    />,
  );

  expect(await screen.findByRole("heading", { name: "隐私风险复核" })).toBeVisible();
  expect(screen.getByText("精确逐帧区域编辑建议使用桌面端")).toBeVisible();
});
