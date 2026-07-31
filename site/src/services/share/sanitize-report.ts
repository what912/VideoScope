import type {
  Evidence,
  Finding,
  JsonValue,
} from "../../types/analysis";
import type { BrowserReport } from "../../types/report";
import type {
  SanitizedSharedEvidence,
  SanitizedSharedFinding,
  SanitizedSharedReport,
} from "./contracts";

export interface ShareSanitizationOptions {
  includePrompt: boolean;
  reportTitle?: string;
  selectedEvidence: ReadonlySet<string>;
}

const UNSAFE_REFERENCE =
  /(?:\b[a-z][a-z0-9+.-]*:|(?:^|[\s"'([{=,:;])\/\/|[a-z]:[\\/]|\\\\[^\\\s]+|(?:^|[\s"'([{=,:;])\/(?!\/))/i;
const BARE_WEB_REFERENCE =
  /\b(?:www\.)?[a-z0-9](?:[a-z0-9-]*\.)+[a-z]{2,}(?::\d+)?(?:[/?#][^\s]*)?/i;
const MAX_SHARE_TEXT_LENGTH = 32_768;

function filenameTokens(filename: string) {
  const normalized = filename.normalize("NFKC").trim().toLocaleLowerCase();
  const dot = normalized.lastIndexOf(".");
  const stem = dot > 0 ? normalized.slice(0, dot) : normalized;
  const variants = [normalized, stem];
  for (const token of [normalized, stem]) {
    const words = token.replace(/[^\p{L}\p{N}]+/gu, " ").trim();
    variants.push(
      words,
      words.replace(/\s+/g, "-"),
      words.replace(/\s+/g, "_"),
      words.replace(/\s+/g, "."),
      words.replace(/\s+/g, ""),
      encodeURIComponent(token).toLocaleLowerCase(),
    );
  }
  return [...new Set(variants.filter((token) => token.length >= 3))];
}

function isPrivateRuntimeKey(key: string) {
  const normalized = key.replace(/[^a-z0-9]/gi, "").toLocaleLowerCase();
  return (
    normalized.includes("cache") ||
    normalized.includes("objecturl") ||
    normalized.includes("localpath") ||
    normalized.includes("filepath") ||
    normalized.includes("sourcepath")
  );
}

function safeText(
  value: string | undefined,
  forbiddenTokens: readonly string[] = [],
): string | undefined {
  if (!value) return undefined;
  const trimmed = value.trim();
  if (
    trimmed.length === 0 ||
    trimmed.length > MAX_SHARE_TEXT_LENGTH ||
    UNSAFE_REFERENCE.test(trimmed) ||
    BARE_WEB_REFERENCE.test(trimmed) ||
    forbiddenTokens.some((token) =>
      trimmed.normalize("NFKC").toLocaleLowerCase().includes(token),
    )
  ) {
    return undefined;
  }
  return trimmed;
}

function requiredSafeText(value: string, forbiddenTokens: readonly string[] = []) {
  return safeText(value, forbiddenTokens) ?? "[redacted]";
}

function sanitizeJson(
  value: unknown,
  depth = 0,
  runtime = false,
  forbiddenTokens: readonly string[] = [],
): JsonValue | undefined {
  if (depth > 10) return undefined;
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : undefined;
  }
  if (typeof value === "string") return safeText(value, forbiddenTokens);
  if (!value || typeof value !== "object") return undefined;
  if (
    (typeof Blob !== "undefined" && value instanceof Blob) ||
    (typeof File !== "undefined" && value instanceof File)
  ) {
    return undefined;
  }
  if (Array.isArray(value)) {
    return value.flatMap((item) => {
      const sanitized = sanitizeJson(
        item,
        depth + 1,
        runtime,
        forbiddenTokens,
      );
      return sanitized === undefined ? [] : [sanitized];
    });
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) return undefined;

  const sanitized: Record<string, JsonValue> = {};
  for (const [key, item] of Object.entries(value)) {
    const normalizedKey = key.normalize("NFKC").toLocaleLowerCase();
    if (
      key === "__proto__" ||
      key === "constructor" ||
      key === "prototype" ||
      UNSAFE_REFERENCE.test(key) ||
      forbiddenTokens.some((token) => normalizedKey.includes(token)) ||
      (runtime && isPrivateRuntimeKey(key))
    ) {
      continue;
    }
    const clean = sanitizeJson(item, depth + 1, runtime, forbiddenTokens);
    if (clean !== undefined) sanitized[key] = clean;
  }
  return sanitized;
}

function jsonRecord(
  value: unknown,
  runtime = false,
  forbiddenTokens: readonly string[] = [],
): Record<string, JsonValue> {
  const sanitized = sanitizeJson(value, 0, runtime, forbiddenTokens);
  return sanitized && typeof sanitized === "object" && !Array.isArray(sanitized)
    ? sanitized
    : {};
}

function jsonArray(value: unknown, forbiddenTokens: readonly string[] = []): JsonValue[] {
  const sanitized = sanitizeJson(value, 0, false, forbiddenTokens);
  return Array.isArray(sanitized) ? sanitized : [];
}

export function evidenceSelectionId(findingId: string, evidenceIndex: number) {
  return `${findingId}:${evidenceIndex}`;
}

function sanitizeEvidence(
  evidence: Evidence,
  forbiddenTokens: readonly string[],
): SanitizedSharedEvidence {
  return {
    evidence_type: requiredSafeText(evidence.evidence_type, forbiddenTokens),
    timestamp_seconds: Number.isFinite(evidence.timestamp_seconds)
      ? Math.max(0, evidence.timestamp_seconds)
      : 0,
    description: requiredSafeText(evidence.description, forbiddenTokens),
    metadata: jsonRecord(evidence.metadata, false, forbiddenTokens),
  };
}

function sanitizeFinding(
  finding: Finding,
  selectedEvidence: ReadonlySet<string>,
  forbiddenTokens: readonly string[],
): SanitizedSharedFinding {
  return {
    id: requiredSafeText(finding.id, forbiddenTokens),
    detector_id: requiredSafeText(finding.detector_id, forbiddenTokens),
    detector_version: requiredSafeText(
      finding.detector_version,
      forbiddenTokens,
    ),
    signal_kind: finding.signal_kind,
    title: requiredSafeText(finding.title, forbiddenTokens),
    description: requiredSafeText(finding.description, forbiddenTokens),
    severity: finding.severity,
    score: Number.isFinite(finding.score)
      ? Math.min(1, Math.max(0, finding.score))
      : 0,
    confidence: Number.isFinite(finding.confidence)
      ? Math.min(1, Math.max(0, finding.confidence))
      : 0,
    time_range: {
      start_seconds: Number.isFinite(finding.time_range.start_seconds)
        ? Math.max(0, finding.time_range.start_seconds)
        : 0,
      end_seconds: Number.isFinite(finding.time_range.end_seconds)
        ? Math.max(0, finding.time_range.end_seconds)
        : 0,
    },
    evidence: finding.evidence.flatMap((evidence, index) =>
      selectedEvidence.has(evidenceSelectionId(finding.id, index))
        ? [sanitizeEvidence(evidence, forbiddenTokens)]
        : [],
    ),
    tags: finding.tags.flatMap((tag) => {
      const clean = safeText(tag, forbiddenTokens);
      return clean ? [clean] : [];
    }),
    parameters: jsonRecord(finding.parameters, false, forbiddenTokens),
    limitations: finding.limitations.flatMap((limitation) => {
      const clean = safeText(limitation, forbiddenTokens);
      return clean ? [clean] : [];
    }),
  };
}

export function sanitizeReportForShare(
  report: BrowserReport,
  options: ShareSanitizationOptions,
): SanitizedSharedReport {
  const forbiddenTokens = filenameTokens(report.metadata.filename);
  const title = safeText(options.reportTitle);
  const prompt = options.includePrompt
    ? safeText(report.prompt, forbiddenTokens)
    : undefined;
  const runtime = jsonRecord(report.runtime, true, forbiddenTokens);

  return {
    share_schema_version: "1",
    report_schema_version: report.schema_version,
    tool_version: requiredSafeText(report.tool_version),
    created_at: requiredSafeText(report.created_at),
    ...(title ? { title } : {}),
    ...(prompt ? { prompt } : {}),
    metadata: {
      mime_type: requiredSafeText(report.metadata.mime_type),
      width: Math.max(0, Math.trunc(report.metadata.width)),
      height: Math.max(0, Math.trunc(report.metadata.height)),
      duration_seconds: Math.max(0, report.metadata.duration_seconds),
      file_size_bytes: Math.max(0, Math.trunc(report.metadata.file_size_bytes)),
      ...(report.metadata.frame_rate === undefined
        ? {}
        : { frame_rate: Math.max(0, report.metadata.frame_rate) }),
      ...(report.metadata.has_audio === undefined
        ? {}
        : { has_audio: report.metadata.has_audio }),
    },
    configuration: jsonArray(report.configuration, forbiddenTokens),
    detector_executions: jsonArray(
      report.detector_executions,
      forbiddenTokens,
    ),
    findings: report.findings.map((finding) =>
      sanitizeFinding(finding, options.selectedEvidence, forbiddenTokens),
    ),
    metrics: jsonArray(report.metrics, forbiddenTokens),
    summary: sanitizeJson(report.summary, 0, false, forbiddenTokens) ?? {},
    warnings: report.warnings.flatMap((warning) => {
      const clean = safeText(warning, forbiddenTokens);
      return clean ? [clean] : [];
    }),
    runtime,
  };
}
