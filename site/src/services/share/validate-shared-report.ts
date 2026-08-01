import type { JsonValue } from "../../types/analysis";
import type { SanitizedSharedReport } from "./contracts";

const severityValues = new Set([
  "info",
  "low",
  "medium",
  "high",
  "critical",
]);
const signalValues = new Set(["browser_cpu", "optional_demo"]);
const executionStatusValues = new Set(["ok", "skipped", "failed"]);
const metricUnits = new Set(["ratio", "count", "seconds"]);
const unsafeReference =
  /(?:\b[a-z][a-z0-9+.-]*:|(?:^|[\s"'([{=,:;])\/\/|[a-z]:[\\/]|\\\\[^\\\s]+|(?:^|[\s"'([{=,:;])\/(?!\/))/i;
const bareWebReference =
  /\b(?:www\.)?[a-z0-9](?:[a-z0-9-]*\.)+[a-z]{2,}(?::\d+)?(?:[/?#][^\s]*)?/i;

function fail(): never {
  throw new TypeError("Invalid sanitized shared report.");
}

function record(value: unknown): Record<string, unknown> {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    Object.getPrototypeOf(value) !== Object.prototype
  ) {
    fail();
  }
  return value as Record<string, unknown>;
}

function exactKeys(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
) {
  const allowed = new Set([...required, ...optional]);
  if (
    required.some((key) => !(key in value)) ||
    Object.keys(value).some((key) => !allowed.has(key))
  ) {
    fail();
  }
}

function stringValue(value: unknown) {
  if (
    typeof value !== "string" ||
    value.trim().length === 0 ||
    value.length > 32_768 ||
    unsafeReference.test(value) ||
    bareWebReference.test(value)
  ) {
    fail();
  }
  return value;
}

function finite(value: unknown, minimum = 0, maximum = Number.MAX_VALUE) {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < minimum ||
    value > maximum
  ) {
    fail();
  }
  return value;
}

function integer(value: unknown, minimum = 0) {
  const number = finite(value, minimum);
  if (!Number.isInteger(number)) fail();
  return number;
}

function jsonValue(value: unknown, depth = 0): JsonValue {
  if (depth > 10) fail();
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    return finite(value, -Number.MAX_VALUE, Number.MAX_VALUE);
  }
  if (typeof value === "string") return stringValue(value);
  if (Array.isArray(value)) {
    return value.map((item) => jsonValue(item, depth + 1));
  }
  const source = record(value);
  const clean: Record<string, JsonValue> = {};
  for (const [key, item] of Object.entries(source)) {
    if (
      key === "__proto__" ||
      key === "constructor" ||
      key === "prototype" ||
      unsafeReference.test(key)
    ) {
      fail();
    }
    clean[key] = jsonValue(item, depth + 1);
  }
  return clean;
}

function stringArray(value: unknown) {
  if (!Array.isArray(value)) fail();
  return value.map(stringValue);
}

function validateConfiguration(value: unknown) {
  const item = record(value);
  exactKeys(item, [
    "detector_id",
    "detector_version",
    "signal_kind",
    "enabled",
    "parameters",
  ]);
  stringValue(item.detector_id);
  stringValue(item.detector_version);
  if (!signalValues.has(stringValue(item.signal_kind))) fail();
  if (typeof item.enabled !== "boolean") fail();
  jsonValue(item.parameters);
}

function validateExecution(value: unknown) {
  const item = record(value);
  exactKeys(
    item,
    [
      "detector_id",
      "detector_version",
      "signal_kind",
      "status",
      "elapsed_seconds",
      "findings_count",
    ],
    ["error_type", "error_message"],
  );
  stringValue(item.detector_id);
  stringValue(item.detector_version);
  if (!signalValues.has(stringValue(item.signal_kind))) fail();
  if (!executionStatusValues.has(stringValue(item.status))) fail();
  finite(item.elapsed_seconds);
  integer(item.findings_count);
  if (item.error_type !== undefined) stringValue(item.error_type);
  if (item.error_message !== undefined) stringValue(item.error_message);
}

function validateEvidence(value: unknown) {
  const item = record(value);
  exactKeys(item, [
    "evidence_type",
    "timestamp_seconds",
    "description",
    "metadata",
  ]);
  stringValue(item.evidence_type);
  finite(item.timestamp_seconds);
  stringValue(item.description);
  jsonValue(item.metadata);
}

function validateFinding(value: unknown) {
  const item = record(value);
  exactKeys(item, [
    "id",
    "detector_id",
    "detector_version",
    "signal_kind",
    "title",
    "description",
    "severity",
    "score",
    "confidence",
    "time_range",
    "evidence",
    "tags",
    "parameters",
    "limitations",
  ]);
  stringValue(item.id);
  stringValue(item.detector_id);
  stringValue(item.detector_version);
  if (!signalValues.has(stringValue(item.signal_kind))) fail();
  stringValue(item.title);
  stringValue(item.description);
  if (!severityValues.has(stringValue(item.severity))) fail();
  finite(item.score, 0, 1);
  finite(item.confidence, 0, 1);
  const range = record(item.time_range);
  exactKeys(range, ["start_seconds", "end_seconds"]);
  const start = finite(range.start_seconds);
  const end = finite(range.end_seconds);
  if (end < start) fail();
  if (!Array.isArray(item.evidence)) fail();
  item.evidence.forEach(validateEvidence);
  stringArray(item.tags);
  jsonValue(item.parameters);
  stringArray(item.limitations);
}

function validateMetric(value: unknown) {
  const item = record(value);
  exactKeys(
    item,
    ["id", "label", "value", "kind", "unit", "description"],
    ["detector_id", "domain"],
  );
  stringValue(item.id);
  stringValue(item.label);
  finite(item.value, -Number.MAX_VALUE, Number.MAX_VALUE);
  if (!signalValues.has(stringValue(item.kind))) fail();
  if (item.detector_id !== undefined) stringValue(item.detector_id);
  if (!metricUnits.has(stringValue(item.unit))) fail();
  stringValue(item.description);
  if (item.domain !== undefined) {
    const domain = record(item.domain);
    exactKeys(domain, ["min", "max"]);
    const minimum = finite(
      domain.min,
      -Number.MAX_VALUE,
      Number.MAX_VALUE,
    );
    const maximum = finite(
      domain.max,
      -Number.MAX_VALUE,
      Number.MAX_VALUE,
    );
    if (maximum <= minimum) fail();
  }
}

function validateSummary(value: unknown) {
  const summary = record(value);
  exactKeys(summary, ["review_interval_count", "severity_counts"]);
  integer(summary.review_interval_count);
  const counts = record(summary.severity_counts);
  exactKeys(counts, ["info", "low", "medium", "high", "critical"]);
  for (const severity of severityValues) integer(counts[severity]);
}

export function validateSanitizedSharedReport(
  value: unknown,
): SanitizedSharedReport {
  if (JSON.stringify(value).length > 2_097_152) fail();
  const report = record(value);
  exactKeys(
    report,
    [
      "share_schema_version",
      "report_schema_version",
      "tool_version",
      "created_at",
      "metadata",
      "configuration",
      "detector_executions",
      "findings",
      "metrics",
      "summary",
      "warnings",
      "runtime",
    ],
    ["title", "prompt"],
  );
  if (report.share_schema_version !== "1") fail();
  stringValue(report.report_schema_version);
  stringValue(report.tool_version);
  stringValue(report.created_at);
  if (report.title !== undefined) stringValue(report.title);
  if (report.prompt !== undefined) stringValue(report.prompt);

  const metadata = record(report.metadata);
  exactKeys(
    metadata,
    [
      "mime_type",
      "width",
      "height",
      "duration_seconds",
      "file_size_bytes",
    ],
    ["frame_rate", "has_audio"],
  );
  stringValue(metadata.mime_type);
  integer(metadata.width);
  integer(metadata.height);
  finite(metadata.duration_seconds);
  integer(metadata.file_size_bytes);
  if (metadata.frame_rate !== undefined) finite(metadata.frame_rate);
  if (
    metadata.has_audio !== undefined &&
    typeof metadata.has_audio !== "boolean"
  ) {
    fail();
  }

  if (!Array.isArray(report.configuration)) fail();
  report.configuration.forEach(validateConfiguration);
  if (!Array.isArray(report.detector_executions)) fail();
  report.detector_executions.forEach(validateExecution);
  if (!Array.isArray(report.findings)) fail();
  report.findings.forEach(validateFinding);
  if (!Array.isArray(report.metrics)) fail();
  report.metrics.forEach(validateMetric);
  validateSummary(report.summary);
  stringArray(report.warnings);
  jsonValue(report.runtime);

  return structuredClone(report) as unknown as SanitizedSharedReport;
}
