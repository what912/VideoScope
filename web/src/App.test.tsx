import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import App from "./App";
import type { JobResponse, PublishJobResponse, PublishProfile } from "./types";
import {
  PRIVACY_JOB_ID,
  PRIVACY_PROFILES,
  PRIVACY_RISK_MAP,
  privacyJob,
} from "./test/privacyFixtures";

const ANALYSIS_JOB_ID = "a".repeat(32);
const PUBLISH_JOB_ID = "b".repeat(32);

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

beforeEach(() => {
  window.history.replaceState(null, "", "/?mode=publish");
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      clear: () => values.clear(),
      getItem: (key: string) => values.get(key) ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, value),
    },
  });
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:local-analysis"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      return new Response(
        JSON.stringify(url.endsWith("/publish/profiles") ? PROFILES : []),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

function analysisJob(): JobResponse {
  return {
    job_id: ANALYSIS_JOB_ID,
    status: "cancelled",
    message: "Analysis cancelled",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:01Z",
    upload_size_bytes: 5,
    progress_percent: 100,
    current_detector: null,
    warnings: [],
    error: null,
    links: {},
  };
}

function publishJob(): PublishJobResponse {
  return {
    job_id: PUBLISH_JOB_ID,
    status: "inspecting",
    message: "Inspecting the local source",
    created_at: "2026-08-01T00:00:00Z",
    updated_at: "2026-08-01T00:00:01Z",
    upload_size_bytes: 5,
    progress_percent: 10,
    profile_id: "compatible_mp4",
    warnings: [],
    error: null,
    links: {},
  };
}

function installRecoveryApi(): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.endsWith("/publish/profiles")
        ? PROFILES
        : url.includes(`/publish/jobs/${PUBLISH_JOB_ID}`)
          ? publishJob()
          : url.endsWith("/detectors")
            ? []
            : analysisJob();
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  vi.stubGlobal(
    "EventSource",
    vi.fn(() => ({ addEventListener: vi.fn(), close: vi.fn() })),
  );
}

it("switches workbench modes and locale while keeping the what912 mark literal", async () => {
  const user = userEvent.setup();
  const view = render(<App />);

  expect(await screen.findByRole("heading", { name: "Publish Ready" })).toBeVisible();
  expect(screen.getByText("what912")).toBeVisible();

  await user.click(screen.getByRole("button", { name: "切换到简体中文" }));
  expect(screen.getByRole("heading", { name: "发布就绪" })).toBeVisible();
  expect(screen.getByText("what912")).toHaveTextContent("what912");

  await user.click(screen.getByRole("button", { name: "D 安全分享" }));
  expect(screen.getByRole("heading", { name: "分享前，看清并处理风险" })).toBeVisible();
  expect(screen.getByText("what912")).toHaveTextContent("what912");

  window.history.replaceState(
    null,
    "",
    `/?mode=publish&publishJob=${"b".repeat(32)}`,
  );
  await user.click(screen.getByRole("button", { name: "检查" }));
  expect(screen.getByRole("heading", { name: "New analysis" })).toBeVisible();
  const updatedQuery = new URLSearchParams(window.location.search);
  expect(updatedQuery.get("mode")).toBe("analyze");
  expect(updatedQuery.get("publishJob")).toBe("b".repeat(32));

  view.unmount();
  render(<App />);
  expect(screen.getByRole("heading", { name: "New analysis" })).toBeVisible();
});

it("restores Safe Sharing from privacyJob and preserves the query across modes", async () => {
  window.history.replaceState(
    null,
    "",
    `/?mode=privacy&privacyJob=${PRIVACY_JOB_ID}&keep=retained`,
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.endsWith("/privacy/profiles")
        ? PRIVACY_PROFILES
        : url.endsWith("/risk-map")
          ? PRIVACY_RISK_MAP
          : url.includes(`/privacy/jobs/${PRIVACY_JOB_ID}`)
            ? privacyJob("awaiting_review")
            : url.endsWith("/detectors")
              ? []
              : PROFILES;
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }),
  );
  vi.stubGlobal(
    "EventSource",
    vi.fn(() => ({ addEventListener: vi.fn(), close: vi.fn() })),
  );
  const user = userEvent.setup();
  render(<App />);

  expect(await screen.findByRole("heading", { name: "Privacy risk review" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Check" }));
  expect(new URLSearchParams(window.location.search).get("privacyJob")).toBe(
    PRIVACY_JOB_ID,
  );
  await user.click(screen.getByRole("button", { name: "D · Safe Sharing" }));
  expect(await screen.findByRole("heading", { name: "Privacy risk review" })).toBeVisible();
});

it("retains a Publish job when starting Analyze and restores it after remount", async () => {
  installRecoveryApi();
  window.history.replaceState(
    null,
    "",
    `/?mode=analyze&publishJob=${PUBLISH_JOB_ID}&keep=retained`,
  );
  const user = userEvent.setup();
  const view = render(<App />);
  const input = view.container.querySelector<HTMLInputElement>('input[type="file"]');
  expect(input).not.toBeNull();

  await user.upload(input!, new File(["video"], "source.mp4", { type: "video/mp4" }));
  await user.click(screen.getByRole("button", { name: /start local analysis/i }));

  await waitFor(() => {
    const query = new URLSearchParams(window.location.search);
    expect(query.get("job")).toBe(ANALYSIS_JOB_ID);
    expect(query.get("publishJob")).toBe(PUBLISH_JOB_ID);
    expect(query.get("keep")).toBe("retained");
  });

  view.unmount();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "A · Publish Ready" }));
  expect(await screen.findAllByText("Inspect source")).not.toHaveLength(0);
});

it("removes only the Analyze job on reset and restores Publish after remount", async () => {
  installRecoveryApi();
  window.history.replaceState(
    null,
    "",
    `/?mock=1&mode=analyze&job=mock-dashboard&publishJob=${PUBLISH_JOB_ID}&keep=retained`,
  );
  const user = userEvent.setup();
  const view = render(<App />);

  await user.click(await screen.findByRole("button", { name: "New analysis" }));
  const query = new URLSearchParams(window.location.search);
  expect(query.get("job")).toBeNull();
  expect(query.get("publishJob")).toBe(PUBLISH_JOB_ID);
  expect(query.get("mode")).toBe("analyze");
  expect(query.get("mock")).toBe("1");
  expect(query.get("keep")).toBe("retained");

  view.unmount();
  render(<App />);
  await user.click(screen.getByRole("button", { name: "A · Publish Ready" }));
  expect(await screen.findAllByText("Inspect source")).not.toHaveLength(0);
});
