import {
  useEffect,
  useState,
  type CSSProperties,
} from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { EmptyState } from "../../components/feedback/EmptyState";
import { ErrorState } from "../../components/feedback/ErrorState";
import { LoadingState } from "../../components/feedback/LoadingState";
import { createDemoReport } from "../../data/demo-report";
import { useI18n } from "../../i18n/I18nProvider";
import {
  compactReport,
  createReportStore,
  type ReportStore,
} from "../../services/report-store/report-store";
import {
  createShareClient,
  isShareEnvironmentEnabled,
  readShareEnvironment,
  type SanitizedSharedReport,
  type ShareClient,
} from "../../services/share";
import type {
  AnalysisSummary,
  DetectorConfiguration,
  DetectorExecution,
  Finding,
  JsonValue,
  QualityMetric,
  Severity,
  VideoMetadata,
} from "../../types/analysis";
import type { BrowserReport } from "../../types/report";
import type { Locale } from "../../i18n/types";
import { ShareDialog } from "./ShareDialog";
import "./report.css";

type ReportView = "creator" | "research";

interface SharedReportDisplay {
  source: "shared";
  schema_version: string;
  tool_version: string;
  id: string;
  title: string;
  created_at: string;
  prompt?: string;
  metadata: VideoMetadata;
  configuration: DetectorConfiguration[];
  detector_executions: DetectorExecution[];
  findings: Finding[];
  metrics: QualityMetric[];
  summary: AnalysisSummary;
  warnings: string[];
  runtime: Record<string, JsonValue>;
  preferences: {
    locale: Locale;
    creator_view: boolean;
    reduced_motion: boolean;
  };
  sharedPayload: SanitizedSharedReport;
}

type ReportDisplay = BrowserReport | SharedReportDisplay;

type LoadState =
  | { status: "loading" }
  | { status: "missing" }
  | { status: "shared-missing" }
  | { status: "error" }
  | { status: "ready"; report: ReportDisplay };

interface ReportCopy {
  eyebrow: string;
  demo: string;
  optionalDemo: string;
  creatorView: string;
  researchView: string;
  localReport: string;
  sharedReport: string;
  metadata: string;
  duration: string;
  dimensions: string;
  frameRate: string;
  audio: string;
  yes: string;
  no: string;
  unknown: string;
  reviewIntervals: string;
  detectorStatus: string;
  complete: string;
  skipped: string;
  detectorError: string;
  noFindings: string;
  noFindingsAfterSuccess: string;
  incompleteNoFindings: string;
  reviewOrder: string;
  reviewFirst: string;
  interval: string;
  observableFinding: string;
  limitations: string;
  evidence: string;
  evidenceAlt: string;
  score: string;
  confidence: string;
  detectorId: string;
  detectorVersion: string;
  parameters: string;
  configuration: string;
  rawDiagnostics: string;
  runtime: string;
  warnings: string;
  schemaVersion: string;
  toolVersion: string;
  browserBoundary: string;
  downloadJson: string;
  share: string;
  print: string;
  reportActions: string;
  loadingTitle: string;
  loadingMessage: string;
  missingTitle: string;
  missingMessage: string;
  sharedMissingTitle: string;
  sharedMissingMessage: string;
  loadErrorTitle: string;
  loadErrorMessage: string;
  analyzeVideo: string;
  severity: Record<Severity, string>;
}

const copyByLocale: Record<Locale, ReportCopy> = {
  en: {
    eyebrow: "Report · Local evidence",
    demo: "INTERACTIVE DEMO",
    optionalDemo: "OPTIONAL / DEMO",
    creatorView: "Creator View",
    researchView: "Research View",
    localReport: "Local browser report",
    sharedReport: "Shared sanitized report",
    metadata: "Video metadata",
    duration: "Duration",
    dimensions: "Dimensions",
    frameRate: "Frame rate",
    audio: "Audio",
    yes: "Yes",
    no: "No",
    unknown: "Unavailable",
    reviewIntervals: "Review intervals",
    detectorStatus: "Detector execution",
    complete: "Completed",
    skipped: "Skipped",
    detectorError: "Detector error",
    noFindings: "No observable review intervals",
    noFindingsAfterSuccess:
      "The completed detectors produced no observable review intervals with this configuration.",
    incompleteNoFindings:
      "No review intervals are shown because one or more detector checks did not complete.",
    reviewOrder: "Review order",
    reviewFirst: "Review first",
    interval: "Observable interval",
    observableFinding: "Observable finding",
    limitations: "Limitations",
    evidence: "Evidence",
    evidenceAlt: "Evidence frame",
    score: "Detector-local score",
    confidence: "Confidence",
    detectorId: "Detector ID",
    detectorVersion: "Detector version",
    parameters: "Parameters",
    configuration: "Configuration",
    rawDiagnostics: "Raw diagnostic summaries",
    runtime: "Runtime",
    warnings: "Warnings",
    schemaVersion: "Schema version",
    toolVersion: "Tool version",
    browserBoundary:
      "Browser analysis is a local preview. Metadata and timing can differ from the desktop FFmpeg workflow.",
    downloadJson: "Download JSON",
    share: "Share sanitized report",
    print: "Print / Save as PDF",
    reportActions: "Report actions",
    loadingTitle: "Opening local report",
    loadingMessage: "Reading the saved diagnostic data on this device.",
    missingTitle: "Report not found",
    missingMessage:
      "This report is not stored on this device. Start a local analysis or open another saved report.",
    sharedMissingTitle: "Shared report unavailable",
    sharedMissingMessage:
      "This public report is missing, revoked, or expired. No local report was substituted.",
    loadErrorTitle: "Report could not be opened",
    loadErrorMessage:
      "The local report store could not be read. No diagnostic result was substituted.",
    analyzeVideo: "Analyze a video",
    severity: {
      info: "Info",
      low: "Low",
      medium: "Medium",
      high: "High",
      critical: "Critical",
    },
  },
  "zh-CN": {
    eyebrow: "报告 · 本地证据",
    demo: "交互演示 · INTERACTIVE DEMO",
    optionalDemo: "可选能力 / 演示",
    creatorView: "创作者视图",
    researchView: "研究视图",
    localReport: "本地浏览器报告",
    sharedReport: "公开脱敏报告",
    metadata: "视频元数据",
    duration: "时长",
    dimensions: "画面尺寸",
    frameRate: "帧率",
    audio: "音频",
    yes: "有",
    no: "无",
    unknown: "不可用",
    reviewIntervals: "待复核区间",
    detectorStatus: "检测器执行状态",
    complete: "已完成",
    skipped: "已跳过",
    detectorError: "检测器错误",
    noFindings: "没有可观察的待复核区间",
    noFindingsAfterSuccess:
      "已完成的检测器在当前配置下没有产生可观察的待复核区间。",
    incompleteNoFindings:
      "一个或多个检测器未完成，因此当前不能把空结果理解为没有问题。",
    reviewOrder: "复核顺序",
    reviewFirst: "优先复核",
    interval: "可观察区间",
    observableFinding: "可观察 Finding",
    limitations: "局限性",
    evidence: "证据",
    evidenceAlt: "证据帧",
    score: "检测器内分数",
    confidence: "置信度",
    detectorId: "检测器 ID",
    detectorVersion: "检测器版本",
    parameters: "参数",
    configuration: "配置",
    rawDiagnostics: "原始诊断摘要",
    runtime: "运行时",
    warnings: "警告",
    schemaVersion: "模式版本",
    toolVersion: "工具版本",
    browserBoundary:
      "浏览器分析是本地预览；元数据和时间定位可能与桌面端 FFmpeg 流程存在差异。",
    downloadJson: "下载 JSON",
    share: "分享脱敏报告",
    print: "打印 / 另存为 PDF",
    reportActions: "报告操作",
    loadingTitle: "正在打开本地报告",
    loadingMessage: "正在读取这台设备上保存的诊断数据。",
    missingTitle: "未找到报告",
    missingMessage:
      "这份报告未存储在当前设备上。请开始本地分析，或打开另一份已保存报告。",
    sharedMissingTitle: "公开报告不可用",
    sharedMissingMessage:
      "这份公开报告不存在、已被撤销或已经过期；页面没有用本地报告替代它。",
    loadErrorTitle: "无法打开报告",
    loadErrorMessage:
      "无法读取本地报告存储；页面没有用其他诊断结果替代它。",
    analyzeVideo: "分析视频",
    severity: {
      info: "信息",
      low: "低",
      medium: "中",
      high: "高",
      critical: "严重",
    },
  },
};

const defaultReportStore = createReportStore().then(({ store }) => store);
const defaultShareEnvironment = readShareEnvironment();
const defaultShareClient = createShareClient(defaultShareEnvironment);
const defaultShareEnabled = isShareEnvironmentEnabled(
  defaultShareEnvironment,
);

function toSharedReportDisplay(
  report: SanitizedSharedReport,
  publicId: string,
  locale: Locale,
): SharedReportDisplay {
  return {
    source: "shared",
    schema_version: report.report_schema_version,
    tool_version: report.tool_version,
    id: publicId,
    title:
      report.title ??
      (locale === "zh-CN" ? "公开 VideoScope 报告" : "Shared VideoScope report"),
    created_at: report.created_at,
    ...(report.prompt ? { prompt: report.prompt } : {}),
    metadata: {
      filename: "shared-report",
      ...report.metadata,
    },
    configuration:
      report.configuration as unknown as DetectorConfiguration[],
    detector_executions:
      report.detector_executions as unknown as DetectorExecution[],
    findings: report.findings as unknown as Finding[],
    metrics: report.metrics as unknown as QualityMetric[],
    summary: report.summary as unknown as AnalysisSummary,
    warnings: report.warnings,
    runtime: report.runtime,
    preferences: {
      locale,
      creator_view: true,
      reduced_motion: false,
    },
    sharedPayload: report,
  };
}

const severityPriority: Record<Severity, number> = {
  critical: 5,
  high: 4,
  medium: 3,
  low: 2,
  info: 1,
};

function formatTimestamp(seconds: number) {
  const safe = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(safe / 60);
  const remainder = safe - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remainder
    .toFixed(3)
    .padStart(6, "0")}`;
}

function formatInterval(finding: Finding) {
  return `${formatTimestamp(finding.time_range.start_seconds)}–${formatTimestamp(
    finding.time_range.end_seconds,
  )}`;
}

function jsonSummary(value: unknown) {
  return JSON.stringify(value, null, 2);
}

function detectorStatus(
  execution: DetectorExecution,
  copy: ReportCopy,
) {
  if (execution.status === "failed") return copy.detectorError;
  if (execution.status === "skipped") return copy.skipped;
  return copy.complete;
}

function reviewOrder(findings: Finding[]) {
  return [...findings].sort(
    (left, right) =>
      severityPriority[right.severity] - severityPriority[left.severity] ||
      left.time_range.start_seconds - right.time_range.start_seconds ||
      left.id.localeCompare(right.id),
  );
}

function severityStyle(finding: Finding): CSSProperties {
  return {
    "--report-start": `${finding.time_range.start_seconds}`,
    "--report-duration": `${Math.max(
      0,
      finding.time_range.end_seconds - finding.time_range.start_seconds,
    )}`,
  } as CSSProperties;
}

const REDACTED_LOCAL_REFERENCE = "[redacted local reference]";
const exportReferencePatterns = [
  /data:(?=(?:[a-z][a-z0-9.+-]*(?:\/|;|,|$))|[;,])/i,
  /blob:/i,
  /file:/i,
  /[a-z][a-z0-9+.-]*:\/\//i,
  /[a-z]:[\\/]/i,
  /\\\\[^\\\s]+/,
  /(?:^|[\s"'([{=,:;])\/(?!\/)[^\s"'<>]+/,
];

function containsExportReference(value: string) {
  return exportReferencePatterns.some((pattern) => pattern.test(value));
}

function sanitizeExportValue(value: unknown): unknown {
  if (
    value === null ||
    typeof value === "boolean" ||
    typeof value === "number"
  ) {
    return value;
  }
  if (typeof value === "string") {
    return containsExportReference(value)
      ? REDACTED_LOCAL_REFERENCE
      : value;
  }
  if (Array.isArray(value)) {
    return value.map(sanitizeExportValue);
  }
  if (!value || typeof value !== "object") {
    return undefined;
  }

  const record = value as Record<string, unknown>;
  if (
    typeof record.src === "string" &&
    containsExportReference(record.src)
  ) {
    return undefined;
  }

  const sanitized: Record<string, unknown> = {};
  for (const [key, nestedValue] of Object.entries(record)) {
    if (containsExportReference(key)) continue;
    const cleanValue = sanitizeExportValue(nestedValue);
    if (cleanValue !== undefined) sanitized[key] = cleanValue;
  }
  return sanitized;
}

export function serializeReportForExport(report: BrowserReport) {
  const compact = compactReport(report);
  const sanitized = sanitizeExportValue(compact);
  return new Blob([`${JSON.stringify(sanitized, null, 2)}\n`], {
    type: "application/json;charset=utf-8",
  });
}

export function downloadReportJson(report: BrowserReport) {
  const blob = serializeReportForExport(report);
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const safeId = report.id.replace(/[^a-z0-9._-]/gi, "-");
  anchor.download = `${safeId || "videoscope-report"}.json`;
  anchor.href = objectUrl;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }
}

function downloadSharedReportJson(report: SharedReportDisplay) {
  const blob = new Blob(
    [`${JSON.stringify(report.sharedPayload, null, 2)}\n`],
    { type: "application/json;charset=utf-8" },
  );
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  const safeId = report.id.replace(/[^a-z0-9._-]/gi, "-");
  anchor.download = `${safeId || "videoscope-shared-report"}.json`;
  anchor.href = objectUrl;
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
  }
}

function ReportHeader({
  copy,
  onDownload,
  onPrint,
  onShare,
  report,
  setView,
  view,
}: {
  copy: ReportCopy;
  onDownload(): void;
  onPrint(): void;
  onShare?(): void;
  report: ReportDisplay;
  setView(view: ReportView): void;
  view: ReportView;
}) {
  return (
    <header className="report-page__header">
      <div>
        <p className="eyebrow">{copy.eyebrow}</p>
        {report.source === "demo" ? (
          <p className="report-demo-label">{copy.demo}</p>
        ) : report.source === "shared" ? (
          <p className="report-source-label">{copy.sharedReport}</p>
        ) : (
          <p className="report-source-label">{copy.localReport}</p>
        )}
        <h1 id="report-title">{report.title}</h1>
        <p className="report-page__created numeric">
          {new Date(report.created_at).toLocaleString()}
        </p>
      </div>
      <div className="report-page__controls" data-print="hide">
        <div
          aria-label={copy.reportActions}
          className="report-view-switch"
          role="group"
        >
          <button
            aria-pressed={view === "creator"}
            onClick={() => setView("creator")}
            type="button"
          >
            {copy.creatorView}
          </button>
          <button
            aria-pressed={view === "research"}
            onClick={() => setView("research")}
            type="button"
          >
            {copy.researchView}
          </button>
        </div>
        <div className="report-page__actions">
          {onShare ? (
            <button className="button button--quiet" onClick={onShare} type="button">
              {copy.share}
            </button>
          ) : null}
          <button className="button button--quiet" onClick={onDownload} type="button">
            {copy.downloadJson}
          </button>
          <button className="button button--primary" onClick={onPrint} type="button">
            {copy.print}
          </button>
        </div>
      </div>
    </header>
  );
}

function MetadataPanel({
  copy,
  report,
}: {
  copy: ReportCopy;
  report: ReportDisplay;
}) {
  const metadata = report.metadata;
  return (
    <section aria-labelledby="report-metadata-title" className="report-metadata">
      <h2 id="report-metadata-title">{copy.metadata}</h2>
      <dl>
        <div>
          <dt>{copy.duration}</dt>
          <dd className="numeric">
            {metadata.duration_seconds.toFixed(3)} s
          </dd>
        </div>
        <div>
          <dt>{copy.dimensions}</dt>
          <dd className="numeric">
            {metadata.width} × {metadata.height}
          </dd>
        </div>
        <div>
          <dt>{copy.frameRate}</dt>
          <dd className="numeric">
            {metadata.frame_rate === undefined
              ? copy.unknown
              : metadata.frame_rate.toFixed(3)}
          </dd>
        </div>
        <div>
          <dt>{copy.audio}</dt>
          <dd>
            {metadata.has_audio === undefined
              ? copy.unknown
              : metadata.has_audio
                ? copy.yes
                : copy.no}
          </dd>
        </div>
      </dl>
    </section>
  );
}

function DetectorExecutions({
  copy,
  report,
}: {
  copy: ReportCopy;
  report: ReportDisplay;
}) {
  return (
    <section
      aria-labelledby="detector-status-title"
      className="report-detectors"
    >
      <h2 id="detector-status-title">{copy.detectorStatus}</h2>
      <ul>
        {report.detector_executions.map((execution) => {
          const failed = execution.status === "failed";
          return (
            <li
              className="report-detector"
              data-status={execution.status}
              key={`${execution.detector_id}:${execution.detector_version}`}
              role={failed ? "alert" : undefined}
            >
              <span aria-hidden="true" className="report-detector__symbol">
                {failed ? "!" : execution.status === "skipped" ? "–" : "✓"}
              </span>
              <span>
                <strong>{execution.detector_id}</strong>
                <small>
                  {detectorStatus(execution, copy)}
                  {execution.error_message
                    ? ` · ${execution.error_message}`
                    : ""}
                </small>
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function TemporalMap({
  copy,
  report,
}: {
  copy: ReportCopy;
  report: ReportDisplay;
}) {
  const duration = Math.max(report.metadata.duration_seconds, 0.001);
  return (
    <section aria-labelledby="report-map-title" className="report-map">
      <div className="report-section-heading">
        <h2 id="report-map-title">{copy.reviewIntervals}</h2>
        <span className="numeric">{report.findings.length}</span>
      </div>
      <div aria-label={copy.reviewIntervals} className="report-map__track">
        {report.findings.map((finding) => (
          <span
            aria-label={`${finding.title}, ${formatInterval(finding)}`}
            className="report-map__interval"
            data-severity={finding.severity}
            key={finding.id}
            style={{
              left: `${(finding.time_range.start_seconds / duration) * 100}%`,
              width: `${Math.max(
                0.7,
                ((finding.time_range.end_seconds -
                  finding.time_range.start_seconds) /
                  duration) *
                  100,
              )}%`,
            }}
            title={`${finding.title} · ${formatInterval(finding)}`}
          />
        ))}
      </div>
      <div aria-hidden="true" className="report-map__scale numeric">
        <span>00:00.000</span>
        <span>{formatTimestamp(duration)}</span>
      </div>
    </section>
  );
}

function EvidenceList({
  copy,
  finding,
}: {
  copy: ReportCopy;
  finding: Finding;
}) {
  if (finding.evidence.length === 0) return null;
  return (
    <section aria-label={copy.evidence} className="report-evidence">
      <h4>{copy.evidence}</h4>
      <div className="report-evidence__grid">
        {finding.evidence.map((evidence, index) => (
          <figure key={`${finding.id}:evidence:${index}`}>
            {evidence.thumbnail ? (
              <img
                alt={`${copy.evidenceAlt}: ${evidence.description}`}
                height={evidence.thumbnail.height}
                loading="lazy"
                src={evidence.thumbnail.src}
                width={evidence.thumbnail.width}
              />
            ) : (
              <div aria-hidden="true" className="report-evidence__missing">
                ○
              </div>
            )}
            <figcaption>
              <span>{evidence.description}</span>
              <span className="numeric">
                {formatTimestamp(evidence.timestamp_seconds)}
              </span>
            </figcaption>
          </figure>
        ))}
      </div>
    </section>
  );
}

function CreatorFindings({
  copy,
  report,
}: {
  copy: ReportCopy;
  report: ReportDisplay;
}) {
  const findings = reviewOrder(report.findings);
  if (findings.length === 0) {
    const incomplete = report.detector_executions.some(
      (execution) => execution.status === "failed",
    );
    return (
      <section className="report-no-findings">
        <h2>{copy.noFindings}</h2>
        <p>
          {incomplete
            ? copy.incompleteNoFindings
            : copy.noFindingsAfterSuccess}
        </p>
      </section>
    );
  }

  return (
    <section aria-labelledby="review-order-title" className="report-findings">
      <div className="report-section-heading">
        <h2 id="review-order-title">{copy.reviewOrder}</h2>
        <span className="numeric">{findings.length}</span>
      </div>
      <ol>
        {findings.map((finding, index) => (
          <li
            className="report-finding"
            data-severity={finding.severity}
            key={finding.id}
            style={severityStyle(finding)}
          >
            <article>
              <header>
                <div>
                  <p className="report-finding__priority">
                    {index === 0 ? copy.reviewFirst : `${copy.reviewOrder} ${index + 1}`}
                  </p>
                  <h3>{finding.title}</h3>
                </div>
                <div className="report-finding__badges">
                  {finding.signal_kind === "optional_demo" ? (
                    <span className="report-optional-label">
                      {copy.optionalDemo}
                    </span>
                  ) : null}
                  <span
                    className="report-severity"
                    data-severity={finding.severity}
                  >
                    {copy.severity[finding.severity]}
                  </span>
                </div>
              </header>
              <p className="report-finding__interval numeric">
                {copy.interval}: {formatInterval(finding)}
              </p>
              <p>{finding.description}</p>
              <section className="report-limitations">
                <h4>{copy.limitations}</h4>
                {finding.limitations.length > 0 ? (
                  <ul>
                    {finding.limitations.map((limitation) => (
                      <li key={limitation}>{limitation}</li>
                    ))}
                  </ul>
                ) : (
                  <p>{copy.unknown}</p>
                )}
              </section>
              <EvidenceList copy={copy} finding={finding} />
            </article>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ResearchView({
  copy,
  report,
}: {
  copy: ReportCopy;
  report: ReportDisplay;
}) {
  return (
    <div className="report-research">
      <section className="report-research__identity">
        <h2>{copy.researchView}</h2>
        <dl>
          <div>
            <dt>{copy.schemaVersion}</dt>
            <dd className="numeric">{report.schema_version}</dd>
          </div>
          <div>
            <dt>{copy.toolVersion}</dt>
            <dd className="numeric">{report.tool_version}</dd>
          </div>
        </dl>
      </section>

      <section aria-labelledby="research-findings-title">
        <h2 id="research-findings-title">{copy.observableFinding}</h2>
        {report.findings.length === 0 ? (
          <p>{copy.noFindings}</p>
        ) : (
          <div className="report-research__grid">
            {report.findings.map((finding) => (
              <article className="report-research-card" key={finding.id}>
                <header>
                  <h3>{finding.title}</h3>
                  {finding.signal_kind === "optional_demo" ? (
                    <span className="report-optional-label">
                      {copy.optionalDemo}
                    </span>
                  ) : null}
                </header>
                <dl>
                  <div>
                    <dt>{copy.detectorId}</dt>
                    <dd className="numeric">{finding.detector_id}</dd>
                  </div>
                  <div>
                    <dt>{copy.detectorVersion}</dt>
                    <dd className="numeric">{finding.detector_version}</dd>
                  </div>
                  <div>
                    <dt>{copy.score}</dt>
                    <dd className="numeric">{finding.score.toFixed(4)}</dd>
                  </div>
                  <div>
                    <dt>{copy.confidence}</dt>
                    <dd className="numeric">{finding.confidence.toFixed(4)}</dd>
                  </div>
                </dl>
                <p className="numeric">{formatInterval(finding)}</p>
                <h4>{copy.parameters}</h4>
                <pre>{jsonSummary(finding.parameters)}</pre>
                <h4>{copy.rawDiagnostics}</h4>
                <pre>
                  {jsonSummary(
                    finding.evidence.map((evidence) => ({
                      timestamp_seconds: evidence.timestamp_seconds,
                      metadata: evidence.metadata,
                    })),
                  )}
                </pre>
                <h4>{copy.limitations}</h4>
                <ul>
                  {finding.limitations.map((limitation) => (
                    <li key={limitation}>{limitation}</li>
                  ))}
                </ul>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="report-research__payload">
        <div>
          <h2>{copy.configuration}</h2>
          <pre>{jsonSummary(report.configuration)}</pre>
        </div>
        <div>
          <h2>{copy.runtime}</h2>
          <pre>{jsonSummary(report.runtime)}</pre>
        </div>
        <div>
          <h2>{copy.warnings}</h2>
          {report.warnings.length > 0 ? (
            <ul>
              {report.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          ) : (
            <p>{copy.unknown}</p>
          )}
        </div>
      </section>
    </div>
  );
}

export interface ReportPageProps {
  reportId?: string;
  reportStore?: ReportStore;
  printReport?(): void;
  downloadReport?(report: BrowserReport): void;
  shareClient?: ShareClient;
  shareEnabled?: boolean;
}

export function ReportPage({
  reportId: reportIdOverride,
  reportStore,
  printReport = () => window.print(),
  downloadReport = downloadReportJson,
  shareClient = defaultShareClient,
  shareEnabled = defaultShareEnabled,
}: ReportPageProps = {}) {
  const params = useParams();
  const [searchParams] = useSearchParams();
  const { locale } = useI18n();
  const copy = copyByLocale[locale];
  const reportId = reportIdOverride ?? params.reportId;
  const sharedMode = searchParams.get("shared") === "1";
  const [revision, setRevision] = useState(0);
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [view, setView] = useState<ReportView>("creator");
  const [shareOpen, setShareOpen] = useState(false);

  useEffect(() => {
    let active = true;
    setState({ status: "loading" });

    if (!reportId) {
      setState({ status: sharedMode ? "shared-missing" : "missing" });
      return () => {
        active = false;
      };
    }

    if (!sharedMode && reportId === "demo") {
      const report = createDemoReport(locale);
      setView(report.preferences.creator_view ? "creator" : "research");
      setState({ status: "ready", report });
      return () => {
        active = false;
      };
    }

    if (sharedMode) {
      if (!shareEnabled) {
        setState({ status: "shared-missing" });
        return () => {
          active = false;
        };
      }
      void shareClient
        .getSharedReport(reportId)
        .then((report) => {
          if (!active) return;
          if (!report) {
            setState({ status: "shared-missing" });
            return;
          }
          const display = toSharedReportDisplay(report, reportId, locale);
          setView("creator");
          setState({ status: "ready", report: display });
        })
        .catch(() => {
          if (active) setState({ status: "error" });
        });
      return () => {
        active = false;
      };
    }

    const storePromise = reportStore
      ? Promise.resolve(reportStore)
      : defaultReportStore;
    void storePromise
      .then((store) => store.get(reportId))
      .then((report) => {
        if (!active) return;
        if (!report) {
          setState({ status: "missing" });
          return;
        }
        setView(report.preferences.creator_view ? "creator" : "research");
        setState({ status: "ready", report });
      })
      .catch(() => {
        if (active) setState({ status: "error" });
      });

    return () => {
      active = false;
    };
  }, [
    locale,
    reportId,
    reportStore,
    revision,
    shareClient,
    shareEnabled,
    sharedMode,
  ]);

  if (state.status === "loading") {
    return (
      <LoadingState
        message={copy.loadingMessage}
        title={copy.loadingTitle}
      />
    );
  }
  if (state.status === "missing") {
    return (
      <EmptyState
        action={
          <Link className="button button--primary" to="/">
            {copy.analyzeVideo}
          </Link>
        }
        message={copy.missingMessage}
        title={copy.missingTitle}
      />
    );
  }
  if (state.status === "shared-missing") {
    return (
      <EmptyState
        action={
          <Link className="button button--primary" to="/">
            {copy.analyzeVideo}
          </Link>
        }
        message={copy.sharedMissingMessage}
        title={copy.sharedMissingTitle}
      />
    );
  }
  if (state.status === "error") {
    return (
      <ErrorState
        message={copy.loadErrorMessage}
        onRetry={() => setRevision((value) => value + 1)}
        title={copy.loadErrorTitle}
      />
    );
  }

  const report = state.report;
  const severityCounts = Object.entries(
    report.summary.severity_counts,
  ) as [Severity, number][];

  return (
    <article
      aria-labelledby="report-title"
      className="report-page"
      data-report-source={report.source}
    >
      <ReportHeader
        copy={copy}
        onDownload={() =>
          report.source === "shared"
            ? downloadSharedReportJson(report)
            : downloadReport(report)
        }
        onPrint={printReport}
        onShare={
          report.source === "shared"
            ? undefined
            : () => setShareOpen(true)
        }
        report={report}
        setView={setView}
        view={view}
      />

      <p className="report-page__boundary" role="note">
        {copy.browserBoundary}
      </p>

      <div className="report-page__overview">
        <MetadataPanel copy={copy} report={report} />
        <section
          aria-labelledby="severity-summary-title"
          className="report-severity-summary"
        >
          <div className="report-section-heading">
            <h2 id="severity-summary-title">{copy.reviewIntervals}</h2>
            <strong className="numeric">
              {report.summary.review_interval_count}
            </strong>
          </div>
          <ul>
            {severityCounts.map(([severity, count]) => (
              <li data-severity={severity} key={severity}>
                <span>{copy.severity[severity]}</span>
                <strong className="numeric">{count}</strong>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <TemporalMap copy={copy} report={report} />
      <DetectorExecutions copy={copy} report={report} />

      {view === "creator" ? (
        <CreatorFindings copy={copy} report={report} />
      ) : (
        <ResearchView copy={copy} report={report} />
      )}
      {shareOpen && report.source !== "shared" ? (
        <ShareDialog
          onClose={() => setShareOpen(false)}
          report={report}
          shareClient={shareClient}
          shareEnabled={shareEnabled}
        />
      ) : null}
    </article>
  );
}
