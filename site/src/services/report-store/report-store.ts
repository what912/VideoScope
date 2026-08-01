import type {
  BrowserCpuDetectorConfiguration,
  BrowserCpuDetectorExecution,
  BrowserCpuFinding,
  BrowserCpuQualityMetric,
  DetectorConfiguration,
  DetectorExecution,
  Evidence,
  EvidenceThumbnail,
  Finding,
  JsonValue,
  QualityMetric,
} from "../../types/analysis";
import type {
  BrowserReport,
  DemoBrowserReport,
  RealBrowserReport,
} from "../../types/report";

export const MAX_PERSISTED_THUMBNAILS = 12;
export const MAX_PERSISTED_THUMBNAIL_BYTES = 160 * 1024;
export const MAX_PERSISTED_THUMBNAIL_EDGE = 480;

export interface ReportIndexEntry {
  id: string;
  title: string;
  created_at: string;
  source: BrowserReport["source"];
  demo_label?: string;
  duration_seconds: number;
  finding_count: number;
}

export interface StorageUsage {
  report_count: number;
  bytes_used: number;
  thumbnail_count: number;
}

export interface ReportStore {
  put(report: BrowserReport): Promise<void>;
  get(id: string): Promise<BrowserReport | null>;
  list(): Promise<ReportIndexEntry[]>;
  delete(id: string): Promise<void>;
  clear(): Promise<void>;
  usage(): Promise<StorageUsage>;
}

export interface ReportDatabase {
  put(report: BrowserReport): Promise<void>;
  get(id: string): Promise<BrowserReport | null>;
  getAll(): Promise<BrowserReport[]>;
  delete(id: string): Promise<void>;
  clear(): Promise<void>;
}

export interface ReportStoreResolution {
  store: ReportStore;
  storage: "indexeddb" | "memory";
  warning: string | null;
}

function encodedDataBytes(src: string) {
  const comma = src.indexOf(",");
  if (comma === -1) return Number.POSITIVE_INFINITY;
  const payload = src.slice(comma + 1);
  if (src.slice(0, comma).includes(";base64")) {
    const padding = payload.endsWith("==") ? 2 : payload.endsWith("=") ? 1 : 0;
    return Math.max(0, Math.floor((payload.length * 3) / 4) - padding);
  }
  return new TextEncoder().encode(decodeURIComponent(payload)).byteLength;
}

const MAX_PERSISTED_TEXT_LENGTH = 32_768;
const SAFE_RELATIVE_MEDIA_PATH = /^[a-z0-9][a-z0-9._/-]*$/i;
const UNSAFE_PERSISTED_STRING =
  /(?:^|\s)(?:[a-z][a-z0-9+.-]*:\/\/|(?:blob|file|data):|[a-z]:[\\/]|\\\\|\/[a-z0-9._-])/i;

function isSafePersistedString(value: string) {
  return (
    value.length <= MAX_PERSISTED_TEXT_LENGTH &&
    !UNSAFE_PERSISTED_STRING.test(value)
  );
}

function requiredString(value: string, field: string) {
  if (!value || !isSafePersistedString(value)) {
    throw new TypeError(`Unsafe or empty persisted ${field}`);
  }
  return value;
}

function optionalString(value: string | undefined) {
  return value && isSafePersistedString(value) ? value : undefined;
}

function finiteNumber(value: number, fallback = 0) {
  return Number.isFinite(value) ? value : fallback;
}

function sanitizeLocale(locale: string): "en" | "zh-CN" {
  return locale === "zh-CN" ? "zh-CN" : "en";
}

function sanitizeJsonValue(value: unknown, depth = 0): JsonValue | undefined {
  if (depth > 8) return undefined;
  if (value === null || typeof value === "boolean") return value;
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : undefined;
  }
  if (typeof value === "string") {
    return isSafePersistedString(value) ? value : undefined;
  }
  if (
    (typeof File !== "undefined" && value instanceof File) ||
    (typeof Blob !== "undefined" && value instanceof Blob) ||
    !value ||
    typeof value !== "object"
  ) {
    return undefined;
  }
  if (Array.isArray(value)) {
    return value.flatMap((item) => {
      const sanitized = sanitizeJsonValue(item, depth + 1);
      return sanitized === undefined ? [] : [sanitized];
    });
  }
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) {
    return undefined;
  }
  const sanitized: Record<string, JsonValue> = {};
  for (const [key, item] of Object.entries(value)) {
    if (key === "__proto__" || key === "constructor" || key === "prototype") {
      continue;
    }
    const sanitizedItem = sanitizeJsonValue(item, depth + 1);
    if (sanitizedItem !== undefined) {
      sanitized[key] = sanitizedItem;
    }
  }
  return sanitized;
}

function sanitizeJsonRecord(value: Record<string, JsonValue>) {
  const sanitized = sanitizeJsonValue(value);
  return sanitized && !Array.isArray(sanitized) && typeof sanitized === "object"
    ? sanitized
    : {};
}

function isPersistableThumbnail(thumbnail: EvidenceThumbnail) {
  if (
    thumbnail.width <= 0 ||
    thumbnail.height <= 0 ||
    thumbnail.width > MAX_PERSISTED_THUMBNAIL_EDGE ||
    thumbnail.height > MAX_PERSISTED_THUMBNAIL_EDGE
  ) {
    return false;
  }
  if (thumbnail.src.startsWith("data:image/")) {
    return (
      /^data:image\/(?:jpeg|png|webp);base64,[a-z0-9+/]*={0,2}$/i.test(
        thumbnail.src,
      ) && encodedDataBytes(thumbnail.src) <= MAX_PERSISTED_THUMBNAIL_BYTES
    );
  }
  return (
    SAFE_RELATIVE_MEDIA_PATH.test(thumbnail.src) &&
    !thumbnail.src.split("/").includes("..") &&
    !thumbnail.src.includes("\\") &&
    !thumbnail.src.includes(":")
  );
}

function sanitizeConfiguration<T extends DetectorConfiguration>(
  configuration: T,
): T {
  return {
    detector_id: requiredString(configuration.detector_id, "detector ID"),
    detector_version: requiredString(
      configuration.detector_version,
      "detector version",
    ),
    signal_kind: configuration.signal_kind,
    enabled: Boolean(configuration.enabled),
    parameters: sanitizeJsonRecord(configuration.parameters),
  } as T;
}

function sanitizeExecution<T extends DetectorExecution>(execution: T): T {
  return {
    detector_id: requiredString(execution.detector_id, "execution detector ID"),
    detector_version: requiredString(
      execution.detector_version,
      "execution detector version",
    ),
    signal_kind: execution.signal_kind,
    status: execution.status,
    elapsed_seconds: Math.max(0, finiteNumber(execution.elapsed_seconds)),
    findings_count: Math.max(0, Math.trunc(execution.findings_count)),
    ...(optionalString(execution.error_type)
      ? { error_type: optionalString(execution.error_type) }
      : {}),
    ...(optionalString(execution.error_message)
      ? { error_message: optionalString(execution.error_message) }
      : {}),
  } as T;
}

function sanitizeEvidence(
  evidence: Evidence,
  acceptThumbnail: (thumbnail: EvidenceThumbnail) => boolean,
): Evidence {
  const thumbnail =
    evidence.thumbnail && acceptThumbnail(evidence.thumbnail)
      ? {
          src: evidence.thumbnail.src,
          width: evidence.thumbnail.width,
          height: evidence.thumbnail.height,
        }
      : undefined;
  return {
    evidence_type: evidence.evidence_type,
    timestamp_seconds: Math.max(
      0,
      finiteNumber(evidence.timestamp_seconds),
    ),
    description: requiredString(evidence.description, "evidence description"),
    ...(thumbnail ? { thumbnail } : {}),
    metadata: sanitizeJsonRecord(evidence.metadata),
  };
}

function sanitizeFinding<T extends Finding>(
  finding: T,
  acceptThumbnail: (thumbnail: EvidenceThumbnail) => boolean,
): T {
  return {
    id: requiredString(finding.id, "finding ID"),
    detector_id: requiredString(finding.detector_id, "finding detector ID"),
    detector_version: requiredString(
      finding.detector_version,
      "finding detector version",
    ),
    signal_kind: finding.signal_kind,
    title: requiredString(finding.title, "finding title"),
    description: requiredString(finding.description, "finding description"),
    severity: finding.severity,
    score: Math.min(1, Math.max(0, finiteNumber(finding.score))),
    confidence: Math.min(1, Math.max(0, finiteNumber(finding.confidence))),
    time_range: {
      start_seconds: Math.max(
        0,
        finiteNumber(finding.time_range.start_seconds),
      ),
      end_seconds: Math.max(
        0,
        finiteNumber(finding.time_range.end_seconds),
      ),
    },
    evidence: finding.evidence.map((evidence) =>
      sanitizeEvidence(evidence, acceptThumbnail),
    ),
    tags: finding.tags.flatMap((tag) =>
      isSafePersistedString(tag) ? [tag] : [],
    ),
    parameters: sanitizeJsonRecord(finding.parameters),
    limitations: finding.limitations.flatMap((limitation) =>
      isSafePersistedString(limitation) ? [limitation] : [],
    ),
  } as T;
}

function sanitizeMetric<T extends QualityMetric>(metric: T): T {
  const domain =
    metric.domain &&
    Number.isFinite(metric.domain.min) &&
    Number.isFinite(metric.domain.max) &&
    metric.domain.max > metric.domain.min
      ? { min: metric.domain.min, max: metric.domain.max }
      : undefined;
  return {
    id: requiredString(metric.id, "metric ID"),
    label: requiredString(metric.label, "metric label"),
    value: finiteNumber(metric.value),
    kind: metric.kind,
    ...(optionalString(metric.detector_id)
      ? { detector_id: optionalString(metric.detector_id) }
      : {}),
    unit: metric.unit,
    ...(domain ? { domain } : {}),
    description: requiredString(metric.description, "metric description"),
  } as T;
}

function compactSharedFields(report: BrowserReport) {
  let thumbnailCount = 0;
  const acceptThumbnail = (thumbnail: EvidenceThumbnail) => {
    if (
      thumbnailCount >= MAX_PERSISTED_THUMBNAILS ||
      !isPersistableThumbnail(thumbnail)
    ) {
      return false;
    }
    thumbnailCount += 1;
    return true;
  };
  const filename = optionalString(report.metadata.filename) ?? "local-video";
  const prompt = optionalString(report.prompt);
  return {
    schema_version: report.schema_version,
    tool_version: requiredString(report.tool_version, "tool version"),
    id: requiredString(report.id, "report ID"),
    analysis_id: requiredString(report.analysis_id, "analysis ID"),
    title: requiredString(report.title, "report title"),
    created_at: requiredString(report.created_at, "creation timestamp"),
    input_hash: requiredString(report.input_hash, "input hash"),
    ...(prompt ? { prompt } : {}),
    metadata: {
      filename,
      mime_type: requiredString(report.metadata.mime_type, "MIME type"),
      width: Math.max(0, Math.trunc(report.metadata.width)),
      height: Math.max(0, Math.trunc(report.metadata.height)),
      duration_seconds: Math.max(
        0,
        finiteNumber(report.metadata.duration_seconds),
      ),
      file_size_bytes: Math.max(
        0,
        Math.trunc(report.metadata.file_size_bytes),
      ),
      ...(report.metadata.frame_rate === undefined
        ? {}
        : { frame_rate: Math.max(0, finiteNumber(report.metadata.frame_rate)) }),
      ...(report.metadata.has_audio === undefined
        ? {}
        : { has_audio: Boolean(report.metadata.has_audio) }),
    },
    configuration: report.configuration.map(sanitizeConfiguration),
    detector_executions:
      report.detector_executions.map(sanitizeExecution),
    findings: report.findings.map((finding) =>
      sanitizeFinding(finding, acceptThumbnail),
    ),
    metrics: report.metrics.map(sanitizeMetric),
    summary: {
      review_interval_count: Math.max(
        0,
        Math.trunc(report.summary.review_interval_count),
      ),
      severity_counts: {
        info: Math.max(0, Math.trunc(report.summary.severity_counts.info)),
        low: Math.max(0, Math.trunc(report.summary.severity_counts.low)),
        medium: Math.max(0, Math.trunc(report.summary.severity_counts.medium)),
        high: Math.max(0, Math.trunc(report.summary.severity_counts.high)),
        critical: Math.max(
          0,
          Math.trunc(report.summary.severity_counts.critical),
        ),
      },
    },
    warnings: report.warnings.flatMap((warning) =>
      isSafePersistedString(warning) ? [warning] : [],
    ),
    runtime: {
      environment: "browser" as const,
      user_agent_family:
        optionalString(report.runtime.user_agent_family) ?? "unknown",
      analysis_seconds: Math.max(
        0,
        finiteNumber(report.runtime.analysis_seconds),
      ),
      sample_count: Math.max(0, Math.trunc(report.runtime.sample_count)),
    },
    reviewed_finding_ids: report.reviewed_finding_ids.flatMap((id) =>
      isSafePersistedString(id) ? [id] : [],
    ),
    preferences: {
      locale: sanitizeLocale(report.preferences.locale),
      creator_view: Boolean(report.preferences.creator_view),
      reduced_motion: Boolean(report.preferences.reduced_motion),
    },
  };
}

export function compactReport(report: BrowserReport): BrowserReport {
  if (report.schema_version !== "0.1-browser") {
    throw new TypeError("Unsupported browser report schema");
  }
  const fields = compactSharedFields(report);
  if (report.source === "demo") {
    const demoReport: DemoBrowserReport = {
      ...fields,
      source: "demo",
      demo_label: requiredString(report.demo_label, "demo label"),
    };
    return demoReport;
  }
  const configuration = fields.configuration.filter(
    (
      item,
    ): item is BrowserCpuDetectorConfiguration =>
      item.signal_kind === "browser_cpu",
  );
  const detectorExecutions = fields.detector_executions.filter(
    (item): item is BrowserCpuDetectorExecution =>
      item.signal_kind === "browser_cpu",
  );
  const findings = fields.findings.filter(
    (item): item is BrowserCpuFinding => item.signal_kind === "browser_cpu",
  );
  const metrics = fields.metrics.filter(
    (item): item is BrowserCpuQualityMetric => item.kind === "browser_cpu",
  );
  if (
    configuration.length !== fields.configuration.length ||
    detectorExecutions.length !== fields.detector_executions.length ||
    findings.length !== fields.findings.length ||
    metrics.length !== fields.metrics.length
  ) {
    throw new TypeError("Real reports may persist browser_cpu records only");
  }
  const realReport: RealBrowserReport = {
    ...fields,
    source: "real",
    configuration,
    detector_executions: detectorExecutions,
    findings,
    metrics,
  };
  return realReport;
}

export function reportToIndex(report: BrowserReport): ReportIndexEntry {
  return {
    id: report.id,
    title: report.title,
    created_at: report.created_at,
    source: report.source,
    ...(report.demo_label ? { demo_label: report.demo_label } : {}),
    duration_seconds: report.metadata.duration_seconds,
    finding_count: report.findings.length,
  };
}

export function calculateStorageUsage(reports: BrowserReport[]): StorageUsage {
  return {
    report_count: reports.length,
    bytes_used: reports.reduce(
      (total, report) =>
        total + new TextEncoder().encode(JSON.stringify(report)).byteLength,
      0,
    ),
    thumbnail_count: reports.reduce(
      (total, report) =>
        total +
        report.findings.reduce(
          (findingTotal, finding) =>
            findingTotal +
            finding.evidence.filter((evidence) => evidence.thumbnail).length,
          0,
        ),
      0,
    ),
  };
}

export function sortReportIndexes(entries: ReportIndexEntry[]) {
  return entries.sort(
    (left, right) =>
      right.created_at.localeCompare(left.created_at) ||
      left.id.localeCompare(right.id),
  );
}

export async function createReportStore(options?: {
  indexedDB?: IDBFactory;
  openDatabase?: () => Promise<ReportDatabase>;
}): Promise<ReportStoreResolution> {
  const factory =
    options && "indexedDB" in options ? options.indexedDB : globalThis.indexedDB;
  const warning =
    "IndexedDB is unavailable; reports will remain in memory for this session.";
  if (!factory && !options?.openDatabase) {
    const { MemoryReportStore } = await import("./memory-report-store");
    return {
      store: new MemoryReportStore(),
      storage: "memory",
      warning,
    };
  }

  try {
    const { IndexedDBReportStore } = await import(
      "./indexeddb-report-store"
    );
    const store = new IndexedDBReportStore({
      indexedDB: factory,
      openDatabase: options?.openDatabase,
    });
    await store.ready();
    return { store, storage: "indexeddb", warning: null };
  } catch {
    const { MemoryReportStore } = await import("./memory-report-store");
    return {
      store: new MemoryReportStore(),
      storage: "memory",
      warning,
    };
  }
}
