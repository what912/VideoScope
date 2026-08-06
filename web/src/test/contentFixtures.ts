import type {
  ContentJobResponse,
  ContentMap,
  ContentPlan,
} from "../types";

export const CONTENT_JOB_ID = "d".repeat(32);
export const CONTENT_DIGEST = "e".repeat(64);

export function contentJob(
  status: ContentJobResponse["status"],
  overrides: Partial<ContentJobResponse> = {},
): ContentJobResponse {
  return {
    job_id: CONTENT_JOB_ID,
    status,
    message: status,
    created_at: "2026-08-06T00:00:00Z",
    updated_at: "2026-08-06T00:00:01Z",
    upload_size_bytes: 100,
    progress_percent: status === "completed" ? 100 : 55,
    goal: "faithful_clean",
    revision: 0,
    plan_digest: status === "ready_to_confirm" ? CONTENT_DIGEST : null,
    warnings: [],
    error: null,
    links: {},
    ...overrides,
  };
}

export const CONTENT_MAP: ContentMap = {
  schema_version: "0.1",
  input_hash: "a".repeat(64),
  transcript_hash: null,
  duration_seconds: 20,
  effective_config: { goal: "faithful_clean" },
  provider_executions: [
    {
      provider_id: "scene",
      provider_version: "1",
      status: "ok",
      elapsed_seconds: 0.1,
      warning: null,
    },
  ],
  segments: [
    {
      id: `segment_${"1".repeat(64)}`,
      source_range: { start_seconds: 0, end_seconds: 8 },
      source_order_index: 0,
      signals: [
        {
          signal_type: "scene",
          provider_id: "scene",
          provider_version: "1",
          measurements: { scene_index: 0 },
          parameters: {},
          limitations: [],
        },
      ],
      transcript_cue_ids: [],
      selection_eligibility: "ineligible",
      reason: "Source context is retained.",
      limitations: [],
      private_evidence_paths: [],
      user_range_ids: [],
    },
    {
      id: `segment_${"2".repeat(64)}`,
      source_range: { start_seconds: 8, end_seconds: 12 },
      source_order_index: 1,
      signals: [
        {
          signal_type: "silence",
          provider_id: "silence",
          provider_version: "1",
          measurements: {},
          parameters: {},
          limitations: ["Silence can be meaningful."],
        },
      ],
      transcript_cue_ids: [],
      selection_eligibility: "eligible",
      reason: "Corroborated low-information interval.",
      limitations: ["Review before removal."],
      private_evidence_paths: [],
      user_range_ids: [],
    },
  ],
  user_ranges: [],
  warnings: [],
  map_digest: "b".repeat(64),
};

const STORY_ITEM = {
  id: `story_${"3".repeat(64)}`,
  source_range: { start_seconds: 0, end_seconds: 20 },
  source_order_index: 0,
  output_order_index: 0,
  decision: "keep" as const,
  decision_source: "proposal" as const,
  reason: "Retained",
  label: null,
  segment_ids: [],
};

export const CONTENT_PLAN: ContentPlan = {
  schema_version: "0.1",
  input_hash: "a".repeat(64),
  transcript_hash: null,
  goal: "faithful_clean",
  effective_config: { goal: "faithful_clean" },
  storyboard: {
    schema_version: "0.1",
    input_hash: "a".repeat(64),
    transcript_hash: null,
    goal: "faithful_clean",
    items: [STORY_ITEM],
    chapters: [],
    locked_ranges: [],
    estimated_output_duration_seconds: 20,
    estimated_source_coverage: 1,
    reorder_acknowledged: false,
    storyboard_digest: "c".repeat(64),
  },
  actions: [
    {
      id: `action_${"4".repeat(64)}`,
      version: "1",
      kind: "concatenate",
      description: "Render reviewed ranges.",
      source_ranges: [{ start_seconds: 0, end_seconds: 20 }],
      expected_output_ranges: [{ start_seconds: 0, end_seconds: 20 }],
      parameters: {},
      changes_content: true,
      requires_confirmation: true,
      depends_on: [],
      evidence_segment_ids: [],
    },
  ],
  locked_ranges: [],
  private_artifacts: [],
  public_artifacts: [
    "content-output/useful-content.mp4",
    "content-output/source-map.json",
  ],
  preview_identities: { [`action_${"4".repeat(64)}`]: "preview-id" },
  verification_policy: ["decodable"],
  plan_digest: CONTENT_DIGEST,
};
