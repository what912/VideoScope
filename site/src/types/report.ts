import type {
  AnalysisSummary,
  BrowserCpuDetectorConfiguration,
  BrowserCpuDetectorExecution,
  BrowserCpuFinding,
  BrowserCpuQualityMetric,
  BrowserRuntime,
  DetectorConfiguration,
  DetectorExecution,
  Finding,
  QualityMetric,
  ReportPreferences,
  VideoMetadata,
} from "./analysis";

export const BROWSER_REPORT_SCHEMA_VERSION = "0.1-browser" as const;

interface BrowserReportBase {
  schema_version: typeof BROWSER_REPORT_SCHEMA_VERSION;
  tool_version: string;
  id: string;
  analysis_id: string;
  title: string;
  created_at: string;
  input_hash: string;
  prompt?: string;
  metadata: VideoMetadata;
  configuration: DetectorConfiguration[];
  detector_executions: DetectorExecution[];
  findings: Finding[];
  metrics: QualityMetric[];
  summary: AnalysisSummary;
  warnings: string[];
  runtime: BrowserRuntime;
  reviewed_finding_ids: string[];
  preferences: ReportPreferences;
}

export interface RealBrowserReport extends BrowserReportBase {
  source: "real";
  demo_label?: never;
  configuration: BrowserCpuDetectorConfiguration[];
  detector_executions: BrowserCpuDetectorExecution[];
  findings: BrowserCpuFinding[];
  metrics: BrowserCpuQualityMetric[];
}

export interface DemoBrowserReport extends BrowserReportBase {
  source: "demo";
  demo_label: string;
  configuration: DetectorConfiguration[];
  detector_executions: DetectorExecution[];
  findings: Finding[];
  metrics: QualityMetric[];
}

export type BrowserReport = RealBrowserReport | DemoBrowserReport;

export type RealBrowserReportInput = Omit<
  RealBrowserReport,
  "schema_version" | "source" | "demo_label"
>;

const REAL_INPUT_KEYS = new Set([
  "tool_version",
  "id",
  "analysis_id",
  "title",
  "created_at",
  "input_hash",
  "prompt",
  "metadata",
  "configuration",
  "detector_executions",
  "findings",
  "metrics",
  "summary",
  "warnings",
  "runtime",
  "reviewed_finding_ids",
  "preferences",
]);

function hasDemoEvidenceMarker(finding: BrowserCpuFinding) {
  return finding.evidence.some((evidence) => {
    const metadata = evidence.metadata as Record<string, unknown>;
    return (
      metadata.demo === true ||
      metadata.signal_kind === "optional_demo" ||
      metadata.source === "demo"
    );
  });
}

export function createRealBrowserReport(
  input: RealBrowserReportInput,
): RealBrowserReport {
  const rawInput = input as RealBrowserReportInput & Record<string, unknown>;
  const unexpectedKeys = Object.keys(rawInput).filter(
    (key) => !REAL_INPUT_KEYS.has(key),
  );
  if (unexpectedKeys.length > 0) {
    throw new TypeError(
      `Invalid real report input fields: ${unexpectedKeys.join(", ")}`,
    );
  }
  if (
    typeof input.tool_version !== "string" ||
    input.tool_version.trim().length === 0
  ) {
    throw new TypeError("Real report input requires a tool_version");
  }
  if (
    input.configuration.some(
      (configuration) => configuration.signal_kind !== "browser_cpu",
    ) ||
    input.detector_executions.some(
      (execution) => execution.signal_kind !== "browser_cpu",
    ) ||
    input.findings.some(
      (finding) =>
        finding.signal_kind !== "browser_cpu" ||
        finding.tags.some((tag) => /demo/i.test(tag)) ||
        hasDemoEvidenceMarker(finding),
    ) ||
    input.metrics.some((metric) => metric.kind !== "browser_cpu")
  ) {
    throw new TypeError(
      "Real reports accept browser_cpu records without demo markers only",
    );
  }

  return {
    ...structuredClone(input),
    schema_version: BROWSER_REPORT_SCHEMA_VERSION,
    source: "real",
  };
}
