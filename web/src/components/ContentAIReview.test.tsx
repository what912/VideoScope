import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AISuggestionBatch, ContentJobResponse } from "../types";
import { ContentAIReview, type ContentAIReviewApi } from "./ContentAIReview";

const SUGGESTION_ID = `suggestion_${"a".repeat(64)}`;
const BATCH: AISuggestionBatch = {
  schema_version: "0.1",
  input_hash: "b".repeat(64),
  transcript_hash: "c".repeat(64),
  duration_seconds: 20,
  provider_id: "fake-semantic",
  model_id: "fake-grounded-v1",
  prompt_contract_version: "0.1",
  effective_parameters: {},
  suggestions: [{
    id: SUGGESTION_ID,
    kind: "highlight",
    content: "Clear explanation of the main result",
    rationale: "The cited cue contains the conclusion.",
    evidence: {
      source_ranges: [{ start_seconds: 4, end_seconds: 9 }],
      transcript_cue_ids: [`ai_cue_${"d".repeat(64)}`],
      frame_timestamps_seconds: [],
    },
    confidence: 0.8,
    limitations: ["Human review is required."],
  }],
  execution: {
    provider_id: "fake-semantic",
    model_id: "fake-grounded-v1",
    operation: "suggest",
    status: "ok",
    elapsed_seconds: 0,
    device: "cpu",
    precision: "fake",
    error_type: null,
  },
  warnings: [],
  batch_digest: "e".repeat(64),
};

function api(): ContentAIReviewApi {
  return {
    prepare: vi.fn(async () => BATCH),
    review: vi.fn(async () => ({ review_digest: "f".repeat(64) })),
    apply: vi.fn(async () => ({ job_id: "1".repeat(32), revision: 2 } as ContentJobResponse)),
    cancel: vi.fn(async () => undefined),
  };
}

describe("ContentAIReview", () => {
  afterEach(() => vi.restoreAllMocks());

  it("keeps suggestions rejected until a human accepts and applies the exact review", async () => {
    const fake = api();
    const onApplied = vi.fn(async () => undefined);
    const user = userEvent.setup();
    render(<ContentAIReview jobId={"1".repeat(32)} revision={1} locale="en" busy={false} api={fake} onApplied={onApplied} />);

    await user.click(screen.getAllByText("Ollama model already installed")[0]);
    await user.type(screen.getAllByLabelText("Ollama model already installed")[0], "qwen2.5:7b");
    await user.click(screen.getByRole("button", { name: "Prepare private AI suggestions" }));
    expect(await screen.findByDisplayValue("Clear explanation of the main result")).toBeVisible();
    expect(screen.getByRole("button", { name: "Reject" })).toHaveClass("active");

    await user.click(screen.getByRole("button", { name: "Accept" }));
    await user.click(screen.getByRole("button", { name: "Save exact review" }));
    expect(fake.review).toHaveBeenCalledWith("1".repeat(32), [{ suggestion_id: SUGGESTION_ID, decision: "accept" }]);
    await user.click(screen.getByRole("button", { name: "Apply accepted ranges to storyboard" }));
    expect(fake.apply).toHaveBeenCalledWith("1".repeat(32), 1);
    expect(onApplied).toHaveBeenCalledOnce();
  });

  it("renders the privacy and no-auto-download disclosure in Simplified Chinese", () => {
    render(<ContentAIReview jobId={"1".repeat(32)} revision={0} locale="zh-CN" busy={false} api={api()} onApplied={vi.fn(async () => undefined)} />);
    expect(screen.getByText(/不会离开这台电脑/)).toBeVisible();
    expect(screen.getByText(/绝不会自动拉取 Ollama 模型/)).toBeVisible();
  });

  it("requires explicit consent before a configured BYOK provider is used", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify([{
        profile_id: "my-ai",
        display_name: "My remote AI",
        provider_id: "custom-openai",
        protocol: "openai_compatible",
        api_base_url: "https://provider.example/v1",
        model_id: "user-paid-model",
        capabilities: ["structured_text"],
        request_json_object: false,
        credential_state: "memory_only",
      }]), { status: 200, headers: { "content-type": "application/json" } }),
    );
    const fake = api();
    const user = userEvent.setup();
    render(<ContentAIReview jobId={"1".repeat(32)} revision={1} locale="en" busy={false} api={fake} onApplied={vi.fn(async () => undefined)} />);

    const providerSelect = await screen.findByLabelText("Content intelligence provider");
    expect(await screen.findByRole("option", { name: "My remote AI · user-paid-model" })).toHaveValue("my-ai");
    await user.selectOptions(providerSelect, "my-ai");
    expect(screen.getByText(/provider charges are paid by your own account/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Prepare private AI suggestions" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Approve the described remote data transfer");
    expect(fake.prepare).not.toHaveBeenCalled();

    await user.click(screen.getByLabelText(/I approve sending the bounded transcript/));
    await user.click(screen.getByRole("button", { name: "Prepare private AI suggestions" }));
    expect(fake.prepare).toHaveBeenCalledWith("1".repeat(32), expect.objectContaining({
      provider_profile_id: "my-ai",
      remote_data_consent: true,
      semantic_model_id: "user-paid-model",
    }));
  });
});
