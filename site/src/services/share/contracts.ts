import type { JsonValue } from "../../types/analysis";

export interface SanitizedSharedEvidence {
  evidence_type: string;
  timestamp_seconds: number;
  description: string;
  metadata: Record<string, JsonValue>;
}

export interface SanitizedSharedFinding {
  id: string;
  detector_id: string;
  detector_version: string;
  signal_kind: string;
  title: string;
  description: string;
  severity: string;
  score: number;
  confidence: number;
  time_range: {
    start_seconds: number;
    end_seconds: number;
  };
  evidence: SanitizedSharedEvidence[];
  tags: string[];
  parameters: Record<string, JsonValue>;
  limitations: string[];
}

export interface SanitizedSharedReport {
  share_schema_version: "1";
  report_schema_version: string;
  tool_version: string;
  created_at: string;
  title?: string;
  prompt?: string;
  metadata: {
    mime_type: string;
    width: number;
    height: number;
    duration_seconds: number;
    file_size_bytes: number;
    frame_rate?: number;
    has_audio?: boolean;
  };
  configuration: JsonValue[];
  detector_executions: JsonValue[];
  findings: SanitizedSharedFinding[];
  metrics: JsonValue[];
  summary: JsonValue;
  warnings: string[];
  runtime: Record<string, JsonValue>;
}

export interface CreateShareRequest {
  ownerId: string;
  report: SanitizedSharedReport;
  expiresAt?: string;
}

export interface CreateShareResult {
  publicId: string;
  createdAt: string;
  expiresAt?: string;
}

export interface ShareClient {
  readonly availability: "configured" | "unavailable";
  createShare(request: CreateShareRequest): Promise<CreateShareResult>;
  getSharedReport(publicId: string): Promise<SanitizedSharedReport | null>;
  revokeShare(publicId: string): Promise<void>;
}
