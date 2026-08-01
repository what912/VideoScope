import { compactReport } from "../../services/report-store/report-store";
import type { BrowserReport } from "../../types/report";

const MAX_REPORT_BYTES = 8 * 1024 * 1024;
const severities = new Set(["info", "low", "medium", "high", "critical"]);
const signalKinds = new Set(["browser_cpu", "optional_demo"]);
const executionStatuses = new Set(["ok", "skipped", "failed"]);
const evidenceTypes = new Set(["frame", "frame_pair", "metric"]);
const metricUnits = new Set(["ratio", "count", "seconds"]);
const severityRank = new Map([
  ["info", 0],
  ["low", 1],
  ["medium", 2],
  ["high", 3],
  ["critical", 4],
]);

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    (Object.getPrototypeOf(value) === Object.prototype ||
      Object.getPrototypeOf(value) === null)
    ? (value as Record<string, unknown>)
    : undefined;
}

function nonEmptyString(value: unknown) {
  return typeof value === "string" && value.trim().length > 0;
}

function finite(value: unknown) {
  return typeof value === "number" && Number.isFinite(value);
}

function nonNegative(value: unknown) {
  return finite(value) && (value as number) >= 0;
}

function stringArray(value: unknown) {
  return (
    Array.isArray(value) &&
    value.every((item) => typeof item === "string")
  );
}

function validConfiguration(value: unknown) {
  const item = record(value);
  return Boolean(
    item &&
      nonEmptyString(item.detector_id) &&
      nonEmptyString(item.detector_version) &&
      signalKinds.has(String(item.signal_kind)) &&
      typeof item.enabled === "boolean" &&
      record(item.parameters),
  );
}

function validExecution(value: unknown) {
  const item = record(value);
  return Boolean(
    item &&
      nonEmptyString(item.detector_id) &&
      nonEmptyString(item.detector_version) &&
      signalKinds.has(String(item.signal_kind)) &&
      executionStatuses.has(String(item.status)) &&
      nonNegative(item.elapsed_seconds) &&
      Number.isInteger(item.findings_count) &&
      nonNegative(item.findings_count) &&
      (item.error_type === undefined || nonEmptyString(item.error_type)) &&
      (item.error_message === undefined || nonEmptyString(item.error_message)),
  );
}

function validEvidence(value: unknown, duration: number) {
  const item = record(value);
  const thumbnail =
    item?.thumbnail === undefined ? undefined : record(item.thumbnail);
  return Boolean(
    item &&
      evidenceTypes.has(String(item.evidence_type)) &&
      nonNegative(item.timestamp_seconds) &&
      (item.timestamp_seconds as number) <= duration &&
      nonEmptyString(item.description) &&
      record(item.metadata) &&
      (item.thumbnail === undefined ||
        (thumbnail &&
          nonEmptyString(thumbnail.src) &&
          Number.isInteger(thumbnail.width) &&
          (thumbnail.width as number) > 0 &&
          Number.isInteger(thumbnail.height) &&
          (thumbnail.height as number) > 0)),
  );
}

function validFinding(value: unknown, duration: number) {
  const item = record(value);
  const range = record(item?.time_range);
  return Boolean(
    item &&
      nonEmptyString(item.id) &&
      nonEmptyString(item.detector_id) &&
      nonEmptyString(item.detector_version) &&
      signalKinds.has(String(item.signal_kind)) &&
      nonEmptyString(item.title) &&
      nonEmptyString(item.description) &&
      severities.has(String(item.severity)) &&
      finite(item.score) &&
      (item.score as number) >= 0 &&
      (item.score as number) <= 1 &&
      finite(item.confidence) &&
      (item.confidence as number) >= 0 &&
      (item.confidence as number) <= 1 &&
      range &&
      nonNegative(range.start_seconds) &&
      nonNegative(range.end_seconds) &&
      (range.end_seconds as number) >= (range.start_seconds as number) &&
      (range.end_seconds as number) <= duration &&
      Array.isArray(item.evidence) &&
      item.evidence.length > 0 &&
      item.evidence.every((evidence) => validEvidence(evidence, duration)) &&
      stringArray(item.tags) &&
      record(item.parameters) &&
      stringArray(item.limitations),
  );
}

function validMetric(value: unknown) {
  const item = record(value);
  const domain = item?.domain === undefined ? undefined : record(item.domain);
  return Boolean(
    item &&
      nonEmptyString(item.id) &&
      nonEmptyString(item.label) &&
      finite(item.value) &&
      signalKinds.has(String(item.kind)) &&
      (item.detector_id === undefined || nonEmptyString(item.detector_id)) &&
      metricUnits.has(String(item.unit)) &&
      nonEmptyString(item.description) &&
      (item.domain === undefined ||
        (domain &&
          finite(domain.min) &&
          finite(domain.max) &&
          (domain.max as number) > (domain.min as number))),
  );
}

function sameDetectorRecord(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
) {
  return (
    left.detector_id === right.detector_id &&
    left.detector_version === right.detector_version &&
    left.signal_kind === right.signal_kind
  );
}

function validReportShape(value: unknown): value is BrowserReport {
  const report = record(value);
  const metadata = record(report?.metadata);
  const summary = record(report?.summary);
  const severityCounts = record(summary?.severity_counts);
  const runtime = record(report?.runtime);
  const preferences = record(report?.preferences);
  if (
    !report ||
    report.schema_version !== "0.1-browser" ||
    (report.source !== "real" && report.source !== "demo") ||
    (report.source === "demo" && !nonEmptyString(report.demo_label)) ||
    !nonEmptyString(report.tool_version) ||
    !nonEmptyString(report.id) ||
    !nonEmptyString(report.analysis_id) ||
    !nonEmptyString(report.title) ||
    !nonEmptyString(report.created_at) ||
    !nonEmptyString(report.input_hash) ||
    (report.prompt !== undefined && typeof report.prompt !== "string") ||
    !metadata ||
    !nonEmptyString(metadata.filename) ||
    !nonEmptyString(metadata.mime_type) ||
    !Number.isInteger(metadata.width) ||
    (metadata.width as number) <= 0 ||
    !Number.isInteger(metadata.height) ||
    (metadata.height as number) <= 0 ||
    !nonNegative(metadata.duration_seconds) ||
    (metadata.frame_rate !== undefined &&
      (!finite(metadata.frame_rate) ||
        (metadata.frame_rate as number) <= 0)) ||
    (metadata.has_audio !== undefined &&
      typeof metadata.has_audio !== "boolean") ||
    !Number.isInteger(metadata.file_size_bytes) ||
    !nonNegative(metadata.file_size_bytes) ||
    !Array.isArray(report.configuration) ||
    !report.configuration.every(validConfiguration) ||
    !Array.isArray(report.detector_executions) ||
    !report.detector_executions.every(validExecution) ||
    !Array.isArray(report.findings) ||
    !report.findings.every((finding) =>
      validFinding(finding, metadata.duration_seconds as number),
    ) ||
    !Array.isArray(report.metrics) ||
    !report.metrics.every(validMetric) ||
    !summary ||
    !Number.isInteger(summary.review_interval_count) ||
    summary.review_interval_count !== report.findings.length ||
    !severityCounts ||
    ![...severities].every(
      (severity) =>
        Number.isInteger(severityCounts[severity]) &&
        nonNegative(severityCounts[severity]),
    ) ||
    !stringArray(report.warnings) ||
    !runtime ||
    runtime.environment !== "browser" ||
    !nonEmptyString(runtime.user_agent_family) ||
    !nonNegative(runtime.analysis_seconds) ||
    !Number.isInteger(runtime.sample_count) ||
    !nonNegative(runtime.sample_count) ||
    !stringArray(report.reviewed_finding_ids) ||
    !preferences ||
    (preferences.locale !== "en" && preferences.locale !== "zh-CN") ||
    typeof preferences.creator_view !== "boolean" ||
    typeof preferences.reduced_motion !== "boolean"
  ) {
    return false;
  }

  const configurations = report.configuration.map((item) => record(item)!);
  const executions = report.detector_executions.map((item) => record(item)!);
  const findings = report.findings.map((item) => record(item)!);
  const detectorKey = (item: Record<string, unknown>) =>
    String(item.detector_id);
  const actualSeverityCounts = Object.fromEntries(
    [...severities].map((severity) => [
      severity,
      findings.filter((finding) => finding.severity === severity).length,
    ]),
  );
  if (
    new Set(findings.map((finding) => finding.id)).size !== findings.length ||
    new Set(configurations.map(detectorKey)).size !== configurations.length ||
    new Set(executions.map(detectorKey)).size !== executions.length ||
    [...severities].some(
      (severity) =>
        severityCounts[severity] !== actualSeverityCounts[severity],
    ) ||
    executions.some((execution) => {
      const matchingFindings = findings.filter((finding) =>
        sameDetectorRecord(execution, finding),
      );
      const configured = configurations.some((configuration) =>
        sameDetectorRecord(execution, configuration),
      );
      return (
        !configured ||
        execution.findings_count !== matchingFindings.length ||
        (execution.status !== "ok" && matchingFindings.length > 0)
      );
    }) ||
    findings.some(
      (finding) =>
        !executions.some((execution) =>
          sameDetectorRecord(execution, finding),
        ),
    )
  ) {
    return false;
  }

  const sortedFindingIds = [...findings]
    .sort((left, right) => {
      const leftRange = record(left.time_range)!;
      const rightRange = record(right.time_range)!;
      return (
        (leftRange.start_seconds as number) -
          (rightRange.start_seconds as number) ||
        (severityRank.get(String(left.severity)) ?? 0) -
          (severityRank.get(String(right.severity)) ?? 0) ||
        String(left.detector_id).localeCompare(String(right.detector_id)) ||
        String(left.id).localeCompare(String(right.id))
      );
    })
    .map((finding) => finding.id);
  return sortedFindingIds.every((id, index) => id === findings[index]?.id);
}

function readText(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new TypeError("Unable to read report file"));
    reader.onload = () =>
      typeof reader.result === "string"
        ? resolve(reader.result)
        : reject(new TypeError("Unable to read report file"));
    reader.readAsText(file, "utf-8");
  });
}

export async function parseCompatibleBrowserReport(
  file: File,
): Promise<BrowserReport> {
  if (!(file instanceof File) || file.size <= 0 || file.size > MAX_REPORT_BYTES) {
    throw new TypeError("Select a compatible browser report");
  }
  let value: unknown;
  try {
    value = JSON.parse(await readText(file));
  } catch {
    throw new TypeError("Select a compatible browser report");
  }
  if (!validReportShape(value)) {
    throw new TypeError("Select a compatible browser report");
  }
  try {
    return compactReport(value as BrowserReport);
  } catch {
    throw new TypeError("Select a compatible browser report");
  }
}
