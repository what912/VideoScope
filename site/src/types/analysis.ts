export type JsonPrimitive = boolean | number | string | null;
export type JsonValue =
  | JsonPrimitive
  | JsonValue[]
  | { [key: string]: JsonValue };

export type Severity = "info" | "low" | "medium" | "high" | "critical";
export type SignalKind = "browser_cpu" | "optional_demo";
export type DetectorExecutionStatus = "ok" | "skipped" | "failed";

export interface TimeRange {
  start_seconds: number;
  end_seconds: number;
}

export interface EvidenceThumbnail {
  src: string;
  width: number;
  height: number;
}

export interface Evidence {
  evidence_type: "frame" | "frame_pair" | "metric";
  timestamp_seconds: number;
  description: string;
  thumbnail?: EvidenceThumbnail;
  metadata: Record<string, JsonValue>;
}

export interface FindingBase<TSignalKind extends SignalKind> {
  id: string;
  detector_id: string;
  detector_version: string;
  signal_kind: TSignalKind;
  title: string;
  description: string;
  severity: Severity;
  score: number;
  confidence: number;
  time_range: TimeRange;
  evidence: Evidence[];
  tags: string[];
  parameters: Record<string, JsonValue>;
  limitations: string[];
}

export type BrowserCpuFinding = FindingBase<"browser_cpu">;
export type OptionalDemoFinding = FindingBase<"optional_demo">;
export type Finding = BrowserCpuFinding | OptionalDemoFinding;

export interface VideoMetadata {
  filename: string;
  mime_type: string;
  width: number;
  height: number;
  duration_seconds: number;
  file_size_bytes: number;
  frame_rate?: number;
  has_audio?: boolean;
}

export interface DetectorConfigurationBase<TSignalKind extends SignalKind> {
  detector_id: string;
  detector_version: string;
  signal_kind: TSignalKind;
  enabled: boolean;
  parameters: Record<string, JsonValue>;
}

export type BrowserCpuDetectorConfiguration =
  DetectorConfigurationBase<"browser_cpu">;
export type OptionalDemoDetectorConfiguration =
  DetectorConfigurationBase<"optional_demo">;
export type DetectorConfiguration =
  | BrowserCpuDetectorConfiguration
  | OptionalDemoDetectorConfiguration;

export interface DetectorExecutionBase<TSignalKind extends SignalKind> {
  detector_id: string;
  detector_version: string;
  signal_kind: TSignalKind;
  status: DetectorExecutionStatus;
  elapsed_seconds: number;
  findings_count: number;
  error_type?: string;
  error_message?: string;
}

export type BrowserCpuDetectorExecution =
  DetectorExecutionBase<"browser_cpu">;
export type OptionalDemoDetectorExecution =
  DetectorExecutionBase<"optional_demo">;
export type DetectorExecution =
  | BrowserCpuDetectorExecution
  | OptionalDemoDetectorExecution;

export interface MetricDomain {
  min: number;
  max: number;
}

export interface QualityMetricBase<TSignalKind extends SignalKind> {
  id: string;
  label: string;
  value: number;
  kind: TSignalKind;
  detector_id?: string;
  unit: "ratio" | "count" | "seconds";
  domain?: MetricDomain;
  description: string;
}

export type BrowserCpuQualityMetric = QualityMetricBase<"browser_cpu">;
export type OptionalDemoQualityMetric = QualityMetricBase<"optional_demo">;
export type QualityMetric =
  | BrowserCpuQualityMetric
  | OptionalDemoQualityMetric;

export interface AnalysisSummary {
  review_interval_count: number;
  severity_counts: Record<Severity, number>;
}

export interface BrowserRuntime {
  environment: "browser";
  user_agent_family: string;
  analysis_seconds: number;
  sample_count: number;
}

export interface ReportPreferences {
  locale: "en" | "zh-CN";
  creator_view: boolean;
  reduced_motion: boolean;
}
