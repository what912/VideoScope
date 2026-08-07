import type {
  PrivacyJobResponse,
  PrivacyPlan,
  PrivacyRiskMap,
  PrivacyTechnicalReport,
  ShareAudienceProfile,
} from "../types";

export const PRIVACY_JOB_ID = "c".repeat(32);
export const PRIVACY_PLAN_DIGEST = "d".repeat(64);

export const PRIVACY_PROFILES: ShareAudienceProfile[] = [
  {
    id: "public",
    version: "1",
    forbidden_metadata_categories: ["author", "location", "filename"],
    required_manual_review_categories: [
      "metadata",
      "visual",
      "qr_barcode",
      "text",
      "audio",
    ],
    default_visual_style: "blur",
    qr_handling: "redact_by_default",
    final_human_review_required: true,
  },
  {
    id: "family",
    version: "1",
    forbidden_metadata_categories: ["location", "filename"],
    required_manual_review_categories: ["metadata", "visual", "audio"],
    default_visual_style: "blur",
    qr_handling: "review",
    final_human_review_required: true,
  },
];

export function privacyJob(
  status: PrivacyJobResponse["status"],
  overrides: Partial<PrivacyJobResponse> = {},
): PrivacyJobResponse {
  return {
    job_id: PRIVACY_JOB_ID,
    status,
    message: `Safe Sharing ${status}`,
    created_at: "2026-08-03T00:00:00Z",
    updated_at: "2026-08-03T00:00:01Z",
    upload_size_bytes: 2048,
    progress_percent:
      status === "completed" || status === "needs_review" ? 100 : 35,
    profile_id: "public",
    plan_digest:
      status === "awaiting_confirmation" ? PRIVACY_PLAN_DIGEST : null,
    warnings: [],
    error: null,
    links: {},
    ...overrides,
  };
}

export const PRIVACY_RISK_MAP: PrivacyRiskMap = {
  schema_version: "0.1",
  input_hash: "f".repeat(64),
  profile: "public",
  duration_seconds: 8,
  is_private: true,
  risks: [
    {
      id: `privacy_risk_${"1".repeat(64)}`,
      scanner_id: "anonymous_face",
      scanner_version: "1.0.0",
      risk_type: "face_region",
      title: "Face-like region",
      public_description: "An anonymous face-like region was observed.",
      severity: "high",
      confidence: 0.88,
      start_seconds: 1,
      end_seconds: 2.5,
      box: { x_min: 0.1, y_min: 0.2, x_max: 0.4, y_max: 0.65 },
      track_id: "face_track_01",
      metadata_scope: null,
      metadata_key: null,
      recommended_style: "blur",
      decision: "unreviewed",
      style: null,
      limitations: ["This is an anonymous region proposal, not identity recognition."],
      evidence: [{ timestamp_seconds: 1.5 }],
      private_evidence: [
        { relative_path: "evidence/risk_01.png", timestamp_seconds: 1.5 },
      ],
    },
    {
      id: `privacy_risk_${"2".repeat(64)}`,
      scanner_id: "metadata_privacy",
      scanner_version: "1.0.0",
      risk_type: "metadata",
      title: "Author metadata",
      public_description: "Author metadata can reveal information when shared.",
      severity: "medium",
      confidence: 1,
      start_seconds: 0,
      end_seconds: 0,
      box: null,
      track_id: null,
      metadata_scope: "global",
      metadata_key: "author",
      recommended_style: "remove_metadata",
      decision: "unreviewed",
      style: null,
      limitations: ["Only supported container metadata is inspected."],
      evidence: [],
      private_evidence: [],
    },
  ],
};

export const PRIVACY_PLAN: PrivacyPlan = {
  schema_version: "0.1",
  input_hash: PRIVACY_RISK_MAP.input_hash,
  profile: "public",
  duration_seconds: 8,
  effective_config: {
    preview_seconds: 5,
    guard_pixels: 0,
    blur_kernel_size: 21,
    pixelate_block_size: 12,
    solid_fill_color: [0, 0, 0],
    interpolation_guard_ratio: 0.05,
    expand_track_gaps: true,
    profile_version: "1",
    qr_handling: "redact_by_default",
    default_visual_style: "blur",
    preview_identity: "preview/privacy-preview.mp4",
    expected_artifacts: ["share-safe.mp4", "technical-report.json"],
    source_read_only: true,
    verification_policy: ["decodable", "public_artifact_privacy"],
  },
  risks: PRIVACY_RISK_MAP.risks.map((risk) => ({
    ...risk,
    private_evidence: [],
    decision: "redact",
    style: risk.risk_type === "metadata" ? "remove_metadata" : "blur",
  })),
  actions: [
    {
      id: "visual-01",
      version: "1.0.0",
      kind: "visual_redaction",
      start_seconds: 1,
      end_seconds: 2.5,
      box: PRIVACY_RISK_MAP.risks[0].box,
      parameters: { style: "blur" },
      changes_semantics: true,
      requires_confirmation: true,
    },
  ],
  artifacts: [],
  digest: PRIVACY_PLAN_DIGEST,
};

export const PRIVACY_TECHNICAL_REPORT: PrivacyTechnicalReport = {
  schema_version: "0.1",
  plan_digest: PRIVACY_PLAN_DIGEST,
  verification: {
    schema_version: "0.1",
    plan_digest: PRIVACY_PLAN_DIGEST,
    status: "completed",
    checks: [
      {
        check_id: "decodable",
        status: "passed",
        message: "The sharing copy is decodable.",
        measured: {},
        required: true,
      },
    ],
  },
  artifacts: [
    {
      relative_path: "share-safe.mp4",
      sha256: "a".repeat(64),
      description: "Verified local sharing copy",
    },
    {
      relative_path: "technical-report.json",
      sha256: "b".repeat(64),
      description: "Public technical report",
    },
  ],
};
