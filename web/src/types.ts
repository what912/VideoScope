export type JobStatus =
  | "queued"
  | "probing"
  | "sampling"
  | "detecting"
  | "rendering"
  | "completed"
  | "failed"
  | "cancelled";

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
