export type JobStatus =
  | "queued"
  | "probing"
  | "sampling"
  | "detecting"
  | "rendering"
  | "completed"
  | "failed"
  | "cancelled";

export type ContentGoal =
  | "faithful_clean"
  | "chaptered_full"
  | "selected_clips";

export type ContentJobStatus =
  | "queued"
  | "probing"
  | "mapping"
  | "planning"
  | "awaiting_review"
  | "previewing"
  | "ready_to_confirm"
  | "rendering"
  | "verifying"
  | "completed"
  | "partial"
  | "needs_review"
  | "failed"
  | "cancelled";

export type ContentRangeKind =
  | "keep"
  | "exclude"
  | "locked_keep"
  | "locked_exclude"
  | "chapter";

export interface ContentTimeRange {
  start_seconds: number;
  end_seconds: number;
}

export interface ContentRangeInput extends ContentTimeRange {
  kind: ContentRangeKind;
  label?: string | null;
}

export interface ContentJobEvent {
  sequence: number;
  status: ContentJobStatus;
  message: string;
  progress_percent: number;
  revision: number;
  created_at: string;
}

export interface ContentJobResponse {
  job_id: string;
  status: ContentJobStatus;
  message: string;
  created_at: string;
  updated_at: string;
  upload_size_bytes: number;
  progress_percent: number;
  goal: ContentGoal;
  revision: number;
  plan_digest: string | null;
  warnings: string[];
  error: string | null;
  links: Record<string, string>;
}

export interface ContentSignal {
  signal_type: string;
  provider_id: string;
  provider_version: string;
  measurements: Record<string, unknown>;
  parameters: Record<string, unknown>;
  limitations: string[];
}

export interface ContentSegment {
  id: string;
  source_range: ContentTimeRange;
  source_order_index: number;
  signals: ContentSignal[];
  transcript_cue_ids: string[];
  selection_eligibility: "eligible" | "ineligible" | "manual_only";
  reason: string;
  limitations: string[];
  private_evidence_paths: string[];
  user_range_ids: string[];
}

export interface ContentUserRange {
  id: string;
  kind: ContentRangeKind;
  source_range: ContentTimeRange;
  label: string | null;
}

export interface ContentMap {
  schema_version: "0.1";
  input_hash: string;
  transcript_hash: string | null;
  duration_seconds: number;
  effective_config: Record<string, unknown> & { goal: ContentGoal };
  provider_executions: Array<{
    provider_id: string;
    provider_version: string;
    status: "ok" | "failed" | "skipped";
    elapsed_seconds: number;
    warning: string | null;
  }>;
  segments: ContentSegment[];
  user_ranges: ContentUserRange[];
  warnings: string[];
  map_digest: string;
}

export interface ContentStoryboardItem {
  id: string;
  source_range: ContentTimeRange;
  source_order_index: number;
  output_order_index: number | null;
  decision: "keep" | "remove";
  decision_source: "proposal" | "user" | "lock";
  reason: string;
  label: string | null;
  segment_ids: string[];
}

export interface ContentChapter {
  id: string;
  source_range: ContentTimeRange;
  output_range: ContentTimeRange | null;
  title: string;
  title_source: "neutral" | "user" | "transcript";
  order_index: number;
}

export interface ContentStoryboard {
  schema_version: "0.1";
  input_hash: string;
  transcript_hash: string | null;
  goal: ContentGoal;
  items: ContentStoryboardItem[];
  chapters: ContentChapter[];
  locked_ranges: ContentUserRange[];
  estimated_output_duration_seconds: number;
  estimated_source_coverage: number;
  reorder_acknowledged: boolean;
  storyboard_digest: string;
}

export interface ContentAction {
  id: string;
  version: string;
  kind: string;
  description: string;
  source_ranges: ContentTimeRange[];
  expected_output_ranges: ContentTimeRange[];
  parameters: Record<string, unknown>;
  changes_content: boolean;
  requires_confirmation: boolean;
  depends_on: string[];
  evidence_segment_ids: string[];
}

export interface ContentJoinPreview {
  action_id: string;
  action_ranges: ContentTimeRange[];
  context_ranges: ContentTimeRange[];
  relative_paths: string[];
  artifact_hashes: string[];
  encoding_parameters: Record<string, unknown>;
  identity: string;
}

export interface ContentPlan {
  schema_version: "0.1";
  input_hash: string;
  transcript_hash: string | null;
  goal: ContentGoal;
  effective_config: Record<string, unknown> & { goal: ContentGoal };
  storyboard: ContentStoryboard;
  actions: ContentAction[];
  locked_ranges: ContentUserRange[];
  private_artifacts: string[];
  public_artifacts: string[];
  preview_identities: Record<string, string>;
  verification_policy: string[];
  plan_digest: string;
}

export interface ContentRevisionPayload {
  expected_revision: number;
  ranges: ContentRangeInput[];
  selected_range_order: string[];
  reorder_acknowledged: boolean;
  chapter_titles: Record<string, string>;
}

export interface ContentCreateOptions {
  goal: ContentGoal;
  transcript?: File | null;
  config?: Record<string, unknown>;
}

export type AISuggestionKind = "chapter" | "highlight" | "summary" | "title";
export type AIReviewDecisionKind = "accept" | "reject" | "edit";

export interface AISourceRange extends ContentTimeRange {}

export interface AISuggestion {
  id: string;
  kind: AISuggestionKind;
  content: string;
  rationale: string;
  evidence: {
    source_ranges: AISourceRange[];
    transcript_cue_ids: string[];
    frame_timestamps_seconds: number[];
  };
  confidence: number | null;
  limitations: string[];
}

export interface AISuggestionBatch {
  schema_version: "0.1";
  input_hash: string;
  transcript_hash: string;
  duration_seconds: number;
  provider_id: string;
  model_id: string;
  prompt_contract_version: string;
  effective_parameters: Record<string, unknown>;
  suggestions: AISuggestion[];
  execution: {
    provider_id: string;
    model_id: string;
    operation: string;
    status: "ok" | "failed" | "skipped";
    elapsed_seconds: number;
    device: string;
    precision: string;
    error_type: string | null;
  };
  warnings: string[];
  batch_digest: string;
}

export interface AIReviewDecision {
  suggestion_id: string;
  decision: AIReviewDecisionKind;
  edited_content?: string | null;
  edited_source_range?: AISourceRange | null;
}

export interface AIReviewManifest {
  schema_version: "0.1";
  input_hash: string;
  batch_digest: string;
  decisions: AIReviewDecision[];
  review_digest: string;
}

export interface AdvancedAIPrepareOptions {
  semantic_model_id: string;
  asr_model_id: string;
  asr_language: string | null;
  ollama_endpoint: string;
  locale: "en" | "zh-CN";
  device: "auto" | "cpu" | "cuda";
  allow_model_download: boolean;
  maximum_suggestions: number;
}

export type PublishProfileId =
  | "compatible_mp4"
  | "social_vertical_9_16"
  | "social_horizontal_16_9";

export type PublishJobStatus =
  | "queued"
  | "inspecting"
  | "planning"
  | "awaiting_confirmation"
  | "processing"
  | "verifying"
  | "completed"
  | "needs_review"
  | "failed"
  | "cancelled";

export type PrivacyJobStatus =
  | "queued"
  | "inspecting"
  | "scanning"
  | "awaiting_review"
  | "planning"
  | "previewing"
  | "awaiting_confirmation"
  | "processing"
  | "verifying"
  | "completed"
  | "needs_review"
  | "partial"
  | "failed"
  | "cancelled";

export type PrivacyRiskType =
  | "metadata"
  | "face_region"
  | "qr_code"
  | "barcode"
  | "suspicious_text"
  | "manual_visual"
  | "manual_audio";

export type PrivacyDecision = "unreviewed" | "allow" | "redact";

export type RedactionStyle =
  | "blur"
  | "pixelate"
  | "solid_fill"
  | "crop"
  | "mute"
  | "remove_metadata";

export type Severity = "info" | "low" | "medium" | "high" | "critical";

export interface DetectorManifest {
  id: string;
  display_name: string;
  version: string;
  description: string;
  default_enabled: boolean;
  requires_prompt: boolean;
  requires_gpu: boolean;
  requires_network: boolean;
  optional_packages: string[];
  estimated_cost: string;
  category: "cpu" | "ai" | "ocr";
  available: boolean;
  unavailable_reason: string | null;
}

export interface JobEvent {
  sequence: number;
  status: JobStatus;
  message: string;
  created_at: string;
}

export interface JobResponse {
  job_id: string;
  status: JobStatus;
  message: string;
  created_at: string;
  updated_at: string;
  upload_size_bytes: number;
  progress_percent: number;
  current_detector: string | null;
  warnings: string[];
  error: string | null;
  links: Record<string, string>;
}

export interface PublishProfile {
  id: PublishProfileId;
  version: string;
  width: number | null;
  height: number | null;
  maximum_fps: number;
  video_codec: string;
  audio_codec: string;
  pixel_format: string;
  container: string;
}

export interface PublishJobEvent {
  sequence: number;
  status: PublishJobStatus;
  message: string;
  progress_percent: number;
  created_at: string;
}

export interface PublishJobResponse {
  job_id: string;
  status: PublishJobStatus;
  message: string;
  created_at: string;
  updated_at: string;
  upload_size_bytes: number;
  progress_percent: number;
  profile_id: PublishProfileId;
  warnings: string[];
  error: string | null;
  links: Record<string, string>;
}

export interface ShareAudienceProfile {
  id: string;
  version: string;
  forbidden_metadata_categories: string[];
  required_manual_review_categories: string[];
  default_visual_style: RedactionStyle;
  qr_handling: "review" | "redact_by_default";
  final_human_review_required: boolean;
}

export interface PrivacyJobEvent {
  sequence: number;
  status: PrivacyJobStatus;
  message: string;
  progress_percent: number;
  created_at: string;
}

export interface PrivacyJobResponse {
  job_id: string;
  status: PrivacyJobStatus;
  message: string;
  created_at: string;
  updated_at: string;
  upload_size_bytes: number;
  progress_percent: number;
  profile_id: string;
  plan_digest: string | null;
  warnings: string[];
  error: string | null;
  links: Record<string, string>;
}

export interface NormalizedBox {
  x_min: number;
  y_min: number;
  x_max: number;
  y_max: number;
}

export interface PrivacyRisk {
  id: string;
  scanner_id: string;
  scanner_version: string;
  risk_type: PrivacyRiskType;
  title: string;
  public_description: string;
  severity: Severity;
  confidence: number;
  start_seconds: number;
  end_seconds: number;
  box: NormalizedBox | null;
  track_id: string | null;
  metadata_scope: string | null;
  metadata_key: string | null;
  recommended_style: RedactionStyle | null;
  decision: PrivacyDecision;
  style: RedactionStyle | null;
  limitations: string[];
  evidence: Array<Record<string, unknown>>;
  private_evidence: Array<Record<string, unknown>>;
}

export interface PrivacyRiskMap {
  schema_version: string;
  input_hash: string;
  profile: string;
  duration_seconds: number;
  risks: PrivacyRisk[];
  is_private: boolean;
}

export interface PrivacyReviewDecision {
  risk_id: string;
  decision: PrivacyDecision;
  style: RedactionStyle | null;
  edited_box: NormalizedBox | null;
  reviewed_at: string;
}

export interface ManualVisualRegion {
  start_seconds: number;
  end_seconds: number;
  box: NormalizedBox;
  style: "blur" | "pixelate" | "solid_fill" | "crop";
  source_duration_seconds?: number | null;
}

export interface ManualAudioInterval {
  start_seconds: number;
  end_seconds: number;
  style: "mute";
  source_duration_seconds?: number | null;
}

export interface PrivacyReviewPayload {
  reviews: PrivacyReviewDecision[];
  manual_visual_regions: ManualVisualRegion[];
  manual_audio_intervals: ManualAudioInterval[];
}

export interface PrivacyEffectiveConfig {
  preview_seconds: number;
  guard_pixels: number;
  blur_kernel_size: number;
  pixelate_block_size: number;
  solid_fill_color: [number, number, number];
  interpolation_guard_ratio: number;
  expand_track_gaps: boolean;
  profile_version: string;
  qr_handling: "review" | "redact_by_default";
  default_visual_style: RedactionStyle;
  preview_identity: string;
  expected_artifacts: string[];
  source_read_only: true;
  verification_policy: string[];
}

export interface PrivacyAction {
  id: string;
  version: string;
  kind:
    | "remove_metadata"
    | "crop"
    | "visual_redaction"
    | "audio_mute"
    | "remux"
    | "verify";
  start_seconds: number;
  end_seconds: number;
  box: NormalizedBox | null;
  parameters: Record<string, unknown>;
  changes_semantics: boolean;
  requires_confirmation: boolean;
}

export interface PrivacyArtifact {
  relative_path: string;
  sha256: string;
  description: string;
}

export interface PrivacyPlan {
  schema_version: string;
  input_hash: string;
  profile: string;
  duration_seconds: number | null;
  effective_config: PrivacyEffectiveConfig;
  risks: PrivacyRisk[];
  actions: PrivacyAction[];
  artifacts: PrivacyArtifact[];
  digest: string;
}

export interface PrivacyVerificationCheck {
  check_id: string;
  status: "passed" | "needs_review" | "failed";
  message: string;
  measured: Record<string, unknown>;
  required: boolean;
}

export interface PrivacyTechnicalReport {
  schema_version: string;
  plan_digest: string;
  verification: {
    schema_version: string;
    plan_digest: string;
    status: "completed" | "needs_review" | "partial" | "failed";
    checks: PrivacyVerificationCheck[];
  };
  artifacts: PrivacyArtifact[];
}

export interface TimeRange {
  start_seconds: number;
  end_seconds: number;
  start_frame?: number | null;
  end_frame?: number | null;
}

export interface Evidence {
  evidence_type: string;
  timestamp_seconds: number;
  relative_path: string | null;
  description: string;
  metadata: Record<string, unknown>;
}

export interface Finding {
  id: string;
  detector_id: string;
  detector_version: string;
  title: string;
  description: string;
  severity: Severity;
  score: number;
  confidence: number;
  time_range: TimeRange;
  evidence: Evidence[];
  tags: string[];
  parameters: Record<string, unknown>;
  limitations: string[];
}

export interface VideoMetadata {
  filename: string;
  container_format: string;
  codec: string;
  width: number;
  height: number;
  duration_seconds: number;
  average_frame_rate: number;
  estimated_frame_count: number;
  has_audio: boolean;
  file_size_bytes: number;
  creation_time: string | null;
  raw_probe: Record<string, unknown>;
}

export type PublishActionKind =
  | "remux"
  | "transcode"
  | "scale_pad"
  | "strip_metadata"
  | "faststart"
  | "extract_cover";

export interface PublishAction {
  action_id: string;
  kind: PublishActionKind;
  description: string;
  parameters: Record<string, unknown>;
  affects: string[];
  changes_content_semantics: boolean;
  confirmation_required: boolean;
}

export interface PublishPlan {
  schema_version: string;
  task_id: string;
  input_hash: string;
  source_metadata: VideoMetadata;
  source_read_only: true;
  profile_id: PublishProfileId;
  profile_version: string;
  backend: "native_local";
  actions: PublishAction[];
  preview_artifact: string;
  confirmation_required: true;
  expected_artifacts: string[];
  effective_config: {
    preview_seconds: number;
    keep_workspace: boolean;
    run_diagnostics: boolean;
  };
  output_filename: string;
  plan_digest: string;
}

export type VerificationStatus = "passed" | "needs_review" | "failed";

export interface VerificationCheck {
  check_id: string;
  status: VerificationStatus;
  message: string;
  measured: Record<string, unknown>;
}

export interface VerificationReport {
  schema_version: string;
  profile_id: PublishProfileId;
  profile_version: string;
  status: VerificationStatus;
  checks: VerificationCheck[];
  manual_review_reasons: string[];
}

export interface PublishArtifact {
  relative_path: string;
  sha256: string;
  description: string;
}

export interface PublishTechnicalReport {
  schema_version: string;
  plan_digest: string;
  verification: VerificationReport;
  artifacts: PublishArtifact[];
}

export interface DetectorExecution {
  detector_id: string;
  status: "ok" | "detector_error" | "skipped";
  elapsed_seconds: number;
  findings_count: number;
  error_type: string | null;
  error_message: string | null;
}

export interface AnalysisReport {
  schema_version: string;
  tool_version: string;
  analysis_id: string;
  created_at: string;
  input_hash: string;
  prompt: string | null;
  metadata: VideoMetadata;
  configuration: Record<string, unknown>;
  detector_executions: DetectorExecution[];
  findings: Finding[];
  warnings: string[];
  runtime: Record<string, unknown>;
}

export interface AnalysisOptions {
  sampleFps: number;
  thumbnailMaxSize: number;
  locale: string;
  detectorIds: string[];
}

export type RescueStrategy = "conservative" | "balanced";
export type RescueJobStatus = "queued" | "scanning" | "planning" | "previewing" | "awaiting_confirmation" | "processing" | "verifying" | "completed" | "needs_review" | "partial" | "failed" | "cancelled";
export type RescueSymptom = "unplayable" | "timeline_discontinuity" | "missing_audio" | "audio_video_offset" | "dark" | "video_noise" | "soft_detail" | "flicker" | "shake" | "low_loudness" | "audio_noise" | "audio_clipping";
export type RescueActionKind = "remux" | "rebuild_timestamps" | "select_tracks" | "normalize_rotation" | "salvage_segments" | "trim_damaged_edges" | "correct_fixed_av_offset" | "adjust_luma" | "denoise_video" | "sharpen" | "deflicker" | "stabilize" | "normalize_audio" | "denoise_audio" | "verify";

export interface RescueJobEvent { sequence: number; status: RescueJobStatus; message: string; progress_percent: number; created_at: string; }
export interface RescueJobResponse { job_id: string; status: RescueJobStatus; message: string; created_at: string; updated_at: string; upload_size_bytes: number; progress_percent: number; strategy: RescueStrategy; symptoms: RescueSymptom[]; locked_ranges: Array<[number, number]>; balanced_strength_limit: number; private_artifacts: string[]; plan_digest: string | null; warnings: string[]; error: string | null; links: Record<string, string>; }
export interface RescuePrepareOptions { lockedRanges: Array<[number, number]>; balancedStrengthLimit: number; }
export interface RescueDamageInterval { id: string; stream_id: string; kind: string; start_seconds: number; end_seconds: number; description: string; measurements: Record<string, unknown>; }
export interface RescueDamageMap { schema_version: "0.2"; input_hash: string; duration_seconds: number; scanner_version: string; scan_coverage: Array<[number, number]>; intervals: RescueDamageInterval[]; }
export interface RescueAction { id: string; version: string; kind: RescueActionKind; description: string; source_ranges: Array<[number, number]>; parameters: Record<string, unknown>; changes_content: boolean; requires_confirmation: boolean; depends_on: string[]; fallback: RescueActionKind | null; strategy: RescueStrategy; }
export interface RescuePlan { schema_version: "0.2"; input_hash: string; strategy: RescueStrategy; requested_symptoms: RescueSymptom[]; assessment_parameters: Record<string, unknown>; assessment_limitations: string[]; assessment_warnings: string[]; effective_config: { source_read_only: true; balanced_strength_limit: number; locked_ranges: Array<[number, number]>; [key: string]: unknown }; actions: RescueAction[]; preview_ranges: Array<[number, number]>; private_artifacts: string[]; public_artifacts: string[]; damage_intervals: RescueDamageInterval[]; plan_digest: string; }
export interface RescueArtifact { artifact_role: "faithful" | "improved" | "document"; relative_path: string; sha256: string; description: string; }
export type RescueVerificationStatus = "passed" | "needs_review" | "failed";
export interface RescueActionExecution { action_id: string; kind: RescueActionKind; status: "attempted" | "succeeded" | "failed" | "skipped"; artifact_role: "faithful" | "improved" | "document"; reason: string | null; }
export interface RescueTechnicalReport { schema_version: "0.2"; plan_digest: string; outcome: "completed" | "partial" | "needs_review" | "failed"; damage_map: RescueDamageMap; verification: { schema_version: "0.2"; checks: Array<{ check_id: string; status: RescueVerificationStatus; message: string; measured: Record<string, unknown>; required: boolean }>; faithful_status: RescueVerificationStatus; improved_status: RescueVerificationStatus | null; artifacts: RescueArtifact[]; outcome: string }; requested_symptoms: RescueSymptom[]; artifacts: RescueArtifact[]; action_executions: RescueActionExecution[]; limitations: string[]; manual_review_reasons: string[]; }
export interface RescueConfirmation { plan_digest: string; publish_faithful: true; publish_improved: boolean; accepted_action_ids: string[]; accepted_trim_damage_ids: string[]; }
