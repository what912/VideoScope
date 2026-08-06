import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { RescueApi } from "./RescueView";
import { RescueView } from "./RescueView";
import type {
  RescueDamageMap,
  RescueJobResponse,
  RescuePlan,
  RescueTechnicalReport,
} from "../types";

const ID = "a".repeat(32);
const DIGEST = "b".repeat(64);
const DAMAGE_ID = `damage_${"c".repeat(64)}`;

function job(status: RescueJobResponse["status"]): RescueJobResponse {
  return {
    job_id: ID,
    status,
    message: `server ${status}`,
    created_at: "2026-08-04T00:00:00Z",
    updated_at: "2026-08-04T00:00:01Z",
    upload_size_bytes: 10,
    progress_percent: status === "awaiting_confirmation" ? 70 : 100,
    strategy: "balanced",
    symptoms: ["dark"],
    locked_ranges: [[1, 2]],
    balanced_strength_limit: 0.6,
    private_artifacts: ["source-00.mp4", "faithful-00.mp4"],
    plan_digest: DIGEST,
    warnings: [],
    error: status === "failed" ? "Observed input could not be processed." : null,
    links: {},
  };
}

const damageMap: RescueDamageMap = {
  schema_version: "0.2",
  input_hash: "d".repeat(64),
  duration_seconds: 10,
  scanner_version: "1",
  scan_coverage: [[0, 8]],
  intervals: [{
    id: DAMAGE_ID,
    stream_id: "v:0",
    kind: "undecodable",
    start_seconds: 0,
    end_seconds: 1,
    description: "Observed undecodable edge.",
    measurements: {},
  }],
};

const plan: RescuePlan = {
  schema_version: "0.2",
  input_hash: damageMap.input_hash,
  strategy: "balanced",
  requested_symptoms: ["dark"],
  assessment_parameters: {},
  assessment_limitations: ["Measurements describe only observed local media."],
  assessment_warnings: [],
  effective_config: { source_read_only: true, balanced_strength_limit: 1, locked_ranges: [] },
  actions: [
    {
      id: "action_trim",
      version: "1",
      kind: "trim_damaged_edges",
      description: "Trim observed undecodable edge.",
      source_ranges: [[0, 1]],
      parameters: { damage_ids: [DAMAGE_ID] },
      changes_content: true,
      requires_confirmation: true,
      depends_on: [],
      fallback: null,
      strategy: "balanced",
    },
    {
      id: "action_luma",
      version: "1",
      kind: "adjust_luma",
      description: "Apply bounded luma adjustment.",
      source_ranges: [[2, 4]],
      parameters: { strength: 0.25 },
      changes_content: true,
      requires_confirmation: true,
      depends_on: [],
      fallback: null,
      strategy: "balanced",
    },
  ],
  preview_ranges: [[0, 5]],
  private_artifacts: ["source-00.mp4", "faithful-00.mp4"],
  public_artifacts: ["faithful-rescue.mp4", "improved-viewing.mp4"],
  damage_intervals: damageMap.intervals,
  plan_digest: DIGEST,
};

const report = (outcome: RescueTechnicalReport["outcome"], includeImproved = false): RescueTechnicalReport => ({
  schema_version: "0.2",
  plan_digest: DIGEST,
  outcome,
  damage_map: damageMap,
  verification: {
    schema_version: "0.2",
    checks: [],
    faithful_status: "passed",
    improved_status: includeImproved ? "passed" : null,
    artifacts: [
      { artifact_role: "faithful", relative_path: "faithful-rescue.mp4", sha256: "e".repeat(64), description: "Faithful rescue" },
      ...(includeImproved ? [{ artifact_role: "improved" as const, relative_path: "improved-viewing.mp4", sha256: "2".repeat(64), description: "Improved viewing" }] : []),
    ],
    outcome,
  },
  requested_symptoms: ["dark"],
  artifacts: [
    { artifact_role: "faithful", relative_path: "faithful-rescue.mp4", sha256: "e".repeat(64), description: "Faithful rescue" },
    ...(includeImproved ? [{ artifact_role: "improved" as const, relative_path: "improved-viewing.mp4", sha256: "2".repeat(64), description: "Improved viewing" }] : []),
    { artifact_role: "document", relative_path: "technical-report.json", sha256: "f".repeat(64), description: "Technical report" },
    { artifact_role: "document", relative_path: "report.html", sha256: "1".repeat(64), description: "HTML report" },
  ],
  action_executions: [],
  limitations: ["Manual review remains important."],
  manual_review_reasons: outcome === "needs_review" ? ["Improved output needs review."] : [],
});

function fakeApi(overrides: Partial<RescueApi> = {}): RescueApi {
  return {
    createJob: vi.fn(async () => job("queued")),
    getJob: vi.fn(async () => job("awaiting_confirmation")),
    getDamageMap: vi.fn(async () => damageMap),
    getPlan: vi.fn(async () => plan),
    getTechnicalReport: vi.fn(async () => report("completed")),
    confirm: vi.fn(async () => job("processing")),
    deleteJob: vi.fn(async () => null),
    subscribeToEvents: vi.fn(() => ({ close: vi.fn() })),
    publicArtifactUrl: (jobId, path) => `/public/${jobId}/${path}`,
    privateArtifactUrl: (jobId, path) => `/private/${jobId}/${path}`,
    ...overrides,
  };
}

it("keeps what912 visible while switching Rescue language", async () => {
  const user = userEvent.setup();
  render(<RescueView initialLocale="en" onJobChange={() => undefined} api={{} as never} />);
  expect(screen.getByText("what912")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "切换到简体中文" }));
  expect(screen.getByText("what912")).toBeVisible();
  expect(screen.getByText("视频抢救")).toBeVisible();
  expect(screen.getByText("选择视频")).toBeVisible();
});

describe("Rescue workbench contract", () => {
  it("restores a plan, shows only manifest-listed previews, and confirms the exact plan", async () => {
    const api = fakeApi();
    const user = userEvent.setup();
    render(<RescueView initialLocale="en" initialJobId={ID} onJobChange={() => undefined} api={api} />);

    expect(await screen.findByText("Review exact plan")).toBeVisible();
    expect(screen.getByText(DIGEST)).toBeVisible();
    expect(screen.getByText("Source preview")).toBeVisible();
    expect(screen.getByText("Faithful rescue")).toBeVisible();
    expect(screen.queryByLabelText("Improved viewing", { selector: "video" })).toBeNull();
    expect(screen.getByText(/No improved result was published/i)).toBeVisible();

    expect(screen.queryByRole("checkbox", { name: /Apply action:/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Confirm exact local rescue plan" }));
    await waitFor(() => expect(api.confirm).toHaveBeenCalledTimes(1));
    expect(api.confirm).toHaveBeenCalledWith(ID, {
      plan_digest: DIGEST,
      publish_faithful: true,
      publish_improved: true,
      accepted_action_ids: ["action_trim", "action_luma"],
      accepted_trim_damage_ids: [DAMAGE_ID],
    });
  });

  it("binds protected ranges and bounded strength before the plan is issued", async () => {
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:local") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    const api = fakeApi();
    const user = userEvent.setup();
    render(<RescueView initialLocale="en" onJobChange={() => undefined} api={api} />);

    await user.upload(screen.getByLabelText("Choose a video"), new File(["video"], "clip.mp4", { type: "video/mp4" }));
    await user.clear(screen.getByLabelText("Protected range start (seconds)"));
    await user.type(screen.getByLabelText("Protected range start (seconds)"), "1.25");
    await user.clear(screen.getByLabelText("Protected range end (seconds)"));
    await user.type(screen.getByLabelText("Protected range end (seconds)"), "2.5");
    await user.click(screen.getByRole("button", { name: "Protect range" }));
    await user.click(screen.getByRole("checkbox", { name: "Picture is too dark" }));
    const strength = screen.getByRole("slider", { name: /^Maximum balanced improvement strength/ });
    fireEvent.change(strength, { target: { value: "0.4" } });
    await user.click(screen.getByRole("button", { name: "Scan locally" }));

    await waitFor(() => expect(api.createJob).toHaveBeenCalledTimes(1));
    expect(api.createJob).toHaveBeenCalledWith(expect.any(File), "balanced", ["dark"], {
      lockedRanges: [[1.25, 2.5]],
      balancedStrengthLimit: 0.4,
    });
  });

  it("never offers an improved download that is absent from the verified artifact manifest", async () => {
    const completedJob = job("completed");
    completedJob.private_artifacts.push("improved-00.mp4");
    const api = fakeApi({
      getJob: vi.fn(async () => completedJob),
      getTechnicalReport: vi.fn(async () => report("completed")),
    });
    render(<RescueView initialJobId={ID} onJobChange={() => undefined} api={api} />);
    expect(await screen.findByRole("link", { name: "Download faithful rescue" })).toBeVisible();
    expect(screen.queryByRole("link", { name: /Download improved viewing/i })).toBeNull();
    expect(screen.getByRole("link", { name: "Open HTML report" })).toBeVisible();
    expect(screen.getByText("Source preview")).toBeVisible();
    expect(screen.getByText("Faithful rescue")).toBeVisible();
    expect(screen.getByLabelText("Faithful rescue", { selector: "video" })).toHaveAttribute("src", `/public/${ID}/faithful-rescue.mp4`);
    expect(screen.queryByLabelText("Improved viewing", { selector: "video" })).toBeNull();
    expect(screen.getByRole("img", { name: "Observable damage and protected ranges timeline" })).toBeVisible();
  });

  it("maps verified faithful and improved result roles without using private candidates", async () => {
    const api = fakeApi({
      getJob: vi.fn(async () => job("completed")),
      getTechnicalReport: vi.fn(async () => report("completed", true)),
    });
    render(<RescueView initialJobId={ID} onJobChange={() => undefined} api={api} />);

    expect(await screen.findByLabelText("Faithful rescue", { selector: "video" })).toHaveAttribute("src", `/public/${ID}/faithful-rescue.mp4`);
    expect(screen.getByLabelText("Improved viewing", { selector: "video" })).toHaveAttribute("src", `/public/${ID}/improved-viewing.mp4`);
  });

  it("renders needs-review as its own terminal Web result", async () => {
    const api = fakeApi({
      getJob: vi.fn(async () => job("needs_review")),
      getTechnicalReport: vi.fn(async () => report("needs_review")),
    });
    render(<RescueView initialLocale="zh-CN" initialJobId={ID} onJobChange={() => undefined} api={api} />);
    expect(await screen.findByRole("heading", { name: "需要复核" })).toBeVisible();
    expect(document.querySelector(".status-needs_review")).not.toBeNull();
    expect(api.getTechnicalReport).toHaveBeenCalledWith(ID);
    expect(screen.queryByText(/100%.*恢复/)).toBeNull();
    expect(screen.getByText("what912")).toBeVisible();
  });

  it("shows per-artifact review status without calling the candidate verified", async () => {
    const reviewReport = report("needs_review", true);
    reviewReport.verification.improved_status = "needs_review";
    const api = fakeApi({
      getJob: vi.fn(async () => job("needs_review")),
      getTechnicalReport: vi.fn(async () => reviewReport),
    });

    render(<RescueView initialLocale="en" initialJobId={ID} onJobChange={() => undefined} api={api} />);

    expect(await screen.findByRole("heading", { name: "Needs review" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "Result comparison" })).toBeVisible();
    expect(screen.getByText("Faithful rescue verification: passed")).toBeVisible();
    expect(screen.getByText("Improved viewing verification: needs review")).toBeVisible();
    expect(screen.queryByText("Verified result comparison")).toBeNull();
  });

  it("keeps a partial outcome even when manual review reasons are present", async () => {
    const partialReport = report("partial");
    partialReport.manual_review_reasons = ["Review the retained segment boundary."];
    const api = fakeApi({
      getJob: vi.fn(async () => job("partial")),
      getTechnicalReport: vi.fn(async () => partialReport),
    });
    render(<RescueView initialLocale="zh-CN" initialJobId={ID} onJobChange={() => undefined} api={api} />);
    expect(await screen.findByRole("heading", { name: "部分抢救" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: "需要复核" })).toBeNull();
    expect(screen.getByRole("img", { name: "可观察损坏与保护区间时间轴" })).toBeVisible();
  });

  it("surfaces failed jobs and exposes explicit local data deletion", async () => {
    const deleteJob = vi.fn(async () => null);
    const api = fakeApi({ getJob: vi.fn(async () => job("failed")), deleteJob });
    const user = userEvent.setup();
    const onJobChange = vi.fn();
    render(<RescueView initialJobId={ID} onJobChange={onJobChange} api={api} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Observed input could not be processed.");
    await user.click(screen.getByRole("button", { name: "Delete local task data" }));
    await waitFor(() => expect(deleteJob).toHaveBeenCalledWith(ID));
    expect(onJobChange).toHaveBeenCalledWith(null);
  });
});
