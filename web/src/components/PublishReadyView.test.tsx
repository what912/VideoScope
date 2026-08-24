import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type {
  PublishJobEvent,
  PublishJobResponse,
  PublishPlan,
  PublishProfile,
  PublishTechnicalReport,
} from "../types";
import {
  PublishReadyView,
  type PublishReadyApi,
} from "./PublishReadyView";

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
  {
    id: "social_vertical_9_16",
    version: "1.0.0",
    width: 1080,
    height: 1920,
    maximum_fps: 60,
    video_codec: "h264",
    audio_codec: "aac",
    pixel_format: "yuv420p",
    container: "mp4",
  },
  {
    id: "social_horizontal_16_9",
    version: "1.0.0",
    width: 1920,
    height: 1080,
    maximum_fps: 60,
    video_codec: "h264",
    audio_codec: "aac",
    pixel_format: "yuv420p",
    container: "mp4",
  },
];

const PLAN: PublishPlan = {
  schema_version: "0.3",
  task_id: "c".repeat(32),
  input_hash: "a".repeat(64),
  source_metadata: {
    filename: "源 video.mp4",
    container_format: "mov,mp4",
    codec: "h264",
    width: 1280,
    height: 720,
    duration_seconds: 12,
    average_frame_rate: 30,
    estimated_frame_count: 360,
    has_audio: true,
    file_size_bytes: 1200,
    creation_time: null,
    raw_probe: {},
  },
  source_read_only: true,
  profile_id: "compatible_mp4",
  profile_version: "1.0.0",
  backend: "native_local",
  actions: [
    {
      action_id: "transcode",
      kind: "transcode",
      description: "Encode H.264 video and AAC audio.",
      parameters: { codec: "h264" },
      affects: ["video", "audio"],
      changes_content_semantics: false,
      confirmation_required: true,
    },
    {
      action_id: "faststart",
      kind: "faststart",
      description: "Move MP4 metadata to the front.",
      parameters: {},
      affects: ["container"],
      changes_content_semantics: false,
      confirmation_required: true,
    },
  ],
  preview_artifact: "preview/publish-preview.mp4",
  confirmation_required: true,
  expected_artifacts: [
    "plan.json",
    "preview/publish-preview.mp4",
    "publish-ready.mp4",
    "cover.jpg",
    "changes.json",
    "technical-report.json",
    "analysis-before/report.json",
    "analysis-after/report.json",
  ],
  effective_config: {
    preview_seconds: 6,
    keep_workspace: false,
    run_diagnostics: true,
  },
  output_filename: "publish-ready.mp4",
  plan_digest: "d".repeat(64),
};

const TECHNICAL_REPORT: PublishTechnicalReport = {
  schema_version: "0.3",
  plan_digest: PLAN.plan_digest,
  verification: {
    schema_version: "0.3",
    profile_id: "compatible_mp4",
    profile_version: "1.0.0",
    status: "passed",
    checks: [
      {
        check_id: "container",
        status: "passed",
        message: "Output container is MP4.",
        measured: { container: "mp4" },
      },
    ],
    manual_review_reasons: [],
  },
  artifacts: [
    {
      relative_path: "publish-ready.mp4",
      sha256: "e".repeat(64),
      description: "Verified Publish Ready output.",
    },
    {
      relative_path: "cover.jpg",
      sha256: "f".repeat(64),
      description: "Representative cover.",
    },
  ],
};

function job(
  status: PublishJobResponse["status"],
  overrides: Partial<PublishJobResponse> = {},
): PublishJobResponse {
  return {
    job_id: "b".repeat(32),
    status,
    message: `Job is ${status}`,
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:01Z",
    upload_size_bytes: 1200,
    progress_percent: status === "completed" ? 100 : 45,
    profile_id: "compatible_mp4",
    warnings: [],
    error: null,
    links: {},
    ...overrides,
  };
}

function fakeApi(initial = job("awaiting_confirmation")): PublishReadyApi {
  return {
    listPublishProfiles: vi.fn(async () => PROFILES),
    createPublishJob: vi.fn(async () => initial),
    getPublishJob: vi.fn(async () => initial),
    getPublishPlan: vi.fn(async () => PLAN),
    confirmPublishJob: vi.fn(async () => job("processing", { progress_percent: 55 })),
    cancelPublishJob: vi.fn(async () => job("cancelled", { progress_percent: 100 })),
    getPublishTechnicalReport: vi.fn(async () => TECHNICAL_REPORT),
    subscribeToPublishEvents: vi.fn(() => ({ close: vi.fn() })),
    publishArtifactUrl: (jobId, path) => `/publish/${jobId}/${path}`,
  };
}

describe("PublishReadyView", () => {
  it("offers exactly three profiles and explains the local, source-safe boundary", async () => {
    render(<PublishReadyView locale="en" api={fakeApi()} />);

    expect(await screen.findAllByRole("radio")).toHaveLength(3);
    expect(screen.getByText(/loopback local service/i)).toBeVisible();
    expect(screen.getByText(/original is never overwritten/i)).toBeVisible();
    expect(screen.getAllByText(/scale and pad/i)).toHaveLength(2);
    expect(screen.queryByText(/overall quality score/i)).not.toBeInTheDocument();
  });

  it("uploads, shows the ordered plan and six-second preview, confirms once, and cancels", async () => {
    const api = fakeApi();
    const user = userEvent.setup();
    render(<PublishReadyView locale="en" api={api} />);
    await screen.findAllByRole("radio");

    await user.upload(
      screen.getByLabelText(/choose a local video/i),
      new File(["video"], "源 video.mp4", { type: "video/mp4" }),
    );
    await user.click(screen.getByRole("radio", { name: /vertical 9:16/i }));
    await user.click(screen.getByRole("button", { name: /inspect and plan/i }));

    expect(await screen.findByText("Encode H.264 video and AAC audio.")).toBeVisible();
    const actions = screen.getAllByRole("listitem", { name: /plan action/i });
    expect(actions[0]).toHaveTextContent("Encode H.264");
    expect(actions[1]).toHaveTextContent("Move MP4 metadata");
    expect(
      screen.getByText(
        "This six-second preview is prepared locally from the exact plan above.",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText(/source preview/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/planned output preview/i)).toBeInTheDocument();

    const confirm = screen.getByRole("button", { name: /confirm and process/i });
    await user.click(confirm);
    expect(api.confirmPublishJob).toHaveBeenCalledTimes(1);
    expect(confirm).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /cancel publish job/i }));
    expect(api.cancelPublishJob).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/cancelled/i)).toBeVisible();
  });

  it("shows staged text progress while processing and verifying", async () => {
    const api = fakeApi(job("verifying", { progress_percent: 88 }));
    render(
      <PublishReadyView
        locale="en"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );

    expect(await screen.findAllByText(/verifying output/i)).toHaveLength(2);
    expect(screen.getByText(/inspect source/i)).toBeVisible();
    expect(screen.getByText(/build plan/i)).toBeVisible();
    expect(screen.getByText(/process locally/i)).toBeVisible();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "88");
  });

  it.each([
    ["completed", "Publish Ready", "passed"],
    ["needs_review", "Needs review", "needs_review"],
  ] as const)(
    "renders %s as a distinct verified result with download links",
    async (status, heading, verificationStatus) => {
      const report: PublishTechnicalReport = {
        ...TECHNICAL_REPORT,
        verification: {
          ...TECHNICAL_REPORT.verification,
          status: verificationStatus,
          checks: TECHNICAL_REPORT.verification.checks.map((check) => ({
            ...check,
            status: verificationStatus,
          })),
          manual_review_reasons:
            status === "needs_review" ? ["Review detector differences."] : [],
        },
      };
      const api = fakeApi(job(status, { progress_percent: 100 }));
      api.getPublishTechnicalReport = vi.fn(async () => report);
      render(
        <PublishReadyView
          locale="en"
          initialJobId={"b".repeat(32)}
          api={api}
        />,
      );

      expect(await screen.findByText("Output container is MP4.")).toBeVisible();
      expect(screen.getByRole("heading", { name: heading })).toBeVisible();
      expect(screen.getByRole("link", { name: /download publish-ready.mp4/i })).toHaveAttribute(
        "href",
        expect.stringContaining("publish-ready.mp4"),
      );
      expect(screen.getByRole("link", { name: /download cover.jpg/i })).toBeVisible();
    },
  );

  it("renders failure without claiming a result or inventing download links", async () => {
    const api = fakeApi(
      job("failed", {
        progress_percent: 100,
        error: "Local FFmpeg could not process the media.",
      }),
    );
    render(
      <PublishReadyView
        locale="en"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );

    expect(await screen.findByRole("heading", { name: "Publish failed" })).toBeVisible();
    expect(screen.getByText("Local FFmpeg could not process the media.")).toBeVisible();
    expect(screen.queryByRole("link", { name: /download publish-ready/i })).not.toBeInTheDocument();
    expect(api.getPublishTechnicalReport).not.toHaveBeenCalled();
  });

  it("starts a new Publish locally without deleting the completed server job", async () => {
    const api = fakeApi(job("completed", { progress_percent: 100 }));
    const onJobIdChange = vi.fn();
    const user = userEvent.setup();
    render(
      <PublishReadyView
        locale="en"
        initialJobId={"b".repeat(32)}
        api={api}
        onJobIdChange={onJobIdChange}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: "New Publish" }),
    );

    expect(screen.getByLabelText(/choose a local video/i)).toBeVisible();
    expect(onJobIdChange).toHaveBeenCalledWith(null);
    expect(api.cancelPublishJob).not.toHaveBeenCalled();
  });

  it("keeps one subscription per job and ignores out-of-order events", async () => {
    const api = fakeApi(job("inspecting"));
    let emitEvent: ((event: PublishJobEvent) => void) | null = null;
    api.subscribeToPublishEvents = vi.fn((_jobId, onEvent) => {
      emitEvent = onEvent;
      return { close: vi.fn() };
    });
    render(
      <PublishReadyView
        locale="en"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );
    await screen.findAllByText("Inspect source");

    act(() =>
      emitEvent?.({
        sequence: 2,
        status: "processing",
        message: "Event processing",
        progress_percent: 72,
        created_at: "2026-08-01T00:00:03Z",
      }),
    );
    expect(await screen.findByText("Event processing")).toBeVisible();
    act(() =>
      emitEvent?.({
        sequence: 1,
        status: "planning",
        message: "Stale planning replay",
        progress_percent: 30,
        created_at: "2026-08-01T00:00:02Z",
      }),
    );

    expect(screen.getByText("Event processing")).toBeVisible();
    expect(screen.queryByText("Stale planning replay")).not.toBeInTheDocument();
    expect(api.subscribeToPublishEvents).toHaveBeenCalledTimes(1);
    expect(api.getPublishJob).toHaveBeenCalledTimes(1);
  });

  it("ignores a terminal reconciliation rejection after starting a new publish", async () => {
    const api = fakeApi(job("inspecting"));
    let emitEvent: ((event: PublishJobEvent) => void) | null = null;
    let rejectTerminalSnapshot: ((reason?: unknown) => void) | null = null;
    const terminalSnapshot = new Promise<PublishJobResponse>((_resolve, reject) => {
      rejectTerminalSnapshot = reject;
    });
    api.getPublishJob = vi
      .fn()
      .mockResolvedValueOnce(job("inspecting"))
      .mockReturnValueOnce(terminalSnapshot);
    api.subscribeToPublishEvents = vi.fn((_jobId, onEvent) => {
      emitEvent = onEvent;
      return { close: vi.fn() };
    });
    const user = userEvent.setup();
    render(
      <PublishReadyView
        locale="en"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );
    await screen.findAllByText("Inspect source");

    act(() =>
      emitEvent?.({
        sequence: 3,
        status: "completed",
        message: "Completed",
        progress_percent: 100,
        created_at: "2026-08-01T00:00:04Z",
      }),
    );
    await user.click(await screen.findByRole("button", { name: "New Publish" }));
    await act(async () => {
      rejectTerminalSnapshot?.(new Error("Stale terminal reconciliation"));
      await Promise.resolve();
    });

    expect(screen.getByLabelText(/choose a local video/i)).toBeVisible();
    expect(screen.queryByText("Stale terminal reconciliation")).not.toBeInTheDocument();
  });

  it("renders all new workflow text in Simplified Chinese", async () => {
    render(<PublishReadyView locale="zh-CN" api={fakeApi()} />);
    expect(await screen.findByText("发布就绪")).toBeVisible();
    expect(screen.getByText(/环回本地服务/)).toBeVisible();
    expect(screen.getByText(/不会覆盖源文件/)).toBeVisible();
  });

  it("localizes Chinese progress without exposing the backend English message", async () => {
    const api = fakeApi(
      job("verifying", {
        progress_percent: 88,
        message: "Opaque backend verification detail",
      }),
    );
    render(
      <PublishReadyView
        locale="zh-CN"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );

    expect(await screen.findByText("正在按所选 Profile 验证输出。")).toBeVisible();
    expect(screen.queryByText("Opaque backend verification detail")).not.toBeInTheDocument();
  });

  it("localizes Chinese ordered actions by stable kind without English primary text", async () => {
    render(
      <PublishReadyView
        locale="zh-CN"
        initialJobId={"b".repeat(32)}
        api={fakeApi()}
      />,
    );

    expect(await screen.findByText("转码")).toBeVisible();
    expect(screen.getByText("按所选兼容 Profile 编码视频与音频流。")).toBeVisible();
    expect(screen.getByText("优化起播")).toBeVisible();
    expect(screen.queryByText("Encode H.264 video and AAC audio.")).not.toBeInTheDocument();
    expect(screen.queryByText("transcode")).not.toBeInTheDocument();
  });

  it("localizes Chinese verification and review reasons with a safe unknown fallback", async () => {
    const report: PublishTechnicalReport = {
      ...TECHNICAL_REPORT,
      verification: {
        ...TECHNICAL_REPORT.verification,
        status: "needs_review",
        checks: [
          {
            check_id: "container",
            status: "passed",
            message: "Output container is MP4.",
            measured: { container: "mp4" },
          },
          {
            check_id: "private_future_check",
            status: "needs_review",
            message: "Opaque private review detail",
            measured: {},
          },
        ],
        manual_review_reasons: ["Opaque private review detail"],
      },
    };
    const api = fakeApi(job("needs_review", { progress_percent: 100 }));
    api.getPublishTechnicalReport = vi.fn(async () => report);
    render(
      <PublishReadyView
        locale="zh-CN"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );

    expect(await screen.findByText("输出容器符合所选 Profile。")).toBeVisible();
    expect(screen.getAllByText("此项技术检查需要人工复核。")).toHaveLength(2);
    expect(screen.queryByText("Output container is MP4.")).not.toBeInTheDocument();
    expect(screen.queryByText("Opaque private review detail")).not.toBeInTheDocument();
  });

  it("uses Chinese safe fallbacks for unknown backend failures", async () => {
    const failedApi = fakeApi(
      job("failed", {
        progress_percent: 100,
        error: "Opaque native backend failure",
      }),
    );
    const unavailableApi = fakeApi();
    unavailableApi.listPublishProfiles = vi.fn(async () => {
      throw new Error("Opaque profile endpoint failure");
    });
    const failedView = render(
      <PublishReadyView
        locale="zh-CN"
        initialJobId={"b".repeat(32)}
        api={failedApi}
      />,
    );

    expect(await screen.findByText("本地处理未能完成，请查看技术日志。")).toBeVisible();
    expect(screen.queryByText("Opaque native backend failure")).not.toBeInTheDocument();
    failedView.unmount();

    render(<PublishReadyView locale="zh-CN" api={unavailableApi} />);
    expect(await screen.findByText("无法继续本地发布就绪工作流。")).toBeVisible();
    expect(screen.queryByText("Opaque profile endpoint failure")).not.toBeInTheDocument();
  });

  it("re-presents one raw request error when the locale changes", async () => {
    const api = fakeApi();
    let requests = 0;
    api.listPublishProfiles = vi.fn(async () => {
      requests += 1;
      if (requests === 1) throw new Error("Opaque request detail");
      return PROFILES;
    });
    const view = render(<PublishReadyView locale="en" api={api} />);

    expect(await screen.findByText("Opaque request detail")).toBeVisible();
    view.rerender(<PublishReadyView locale="zh-CN" api={api} />);
    expect(await screen.findByText("无法继续本地发布就绪工作流。")).toBeVisible();
    expect(screen.queryByText("Opaque request detail")).not.toBeInTheDocument();

    view.rerender(<PublishReadyView locale="en" api={api} />);
    expect(await screen.findByText("Opaque request detail")).toBeVisible();
  });

  it("re-presents one raw SSE error when the locale changes", async () => {
    const api = fakeApi(job("inspecting"));
    let emitSseError!: (error: Error) => void;
    api.subscribeToPublishEvents = vi.fn((_jobId, _onEvent, onError) => {
      emitSseError = onError;
      return { close: vi.fn() };
    });
    const view = render(
      <PublishReadyView
        locale="en"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );
    await screen.findAllByText("Inspect source");
    await waitFor(() => {
      expect(api.subscribeToPublishEvents).toHaveBeenCalledTimes(1);
      expect(emitSseError).toBeTypeOf("function");
    });

    act(() => emitSseError(new Error("Opaque stream detail")));
    expect(await screen.findByText("Opaque stream detail")).toBeVisible();
    view.rerender(
      <PublishReadyView
        locale="zh-CN"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );
    expect(await screen.findByText("无法继续本地发布就绪工作流。")).toBeVisible();
    expect(screen.queryByText("Opaque stream detail")).not.toBeInTheDocument();

    view.rerender(
      <PublishReadyView
        locale="en"
        initialJobId={"b".repeat(32)}
        api={api}
      />,
    );
    expect(await screen.findByText("Opaque stream detail")).toBeVisible();
  });
});
