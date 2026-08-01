import { useMemo, useRef, useState } from "react";
import { artifactUrl, reportDownloadUrl } from "../api";
import { containsTime, formatTime } from "../timeline";
import type { AnalysisReport, Finding, Severity } from "../types";
import { Timeline } from "./Timeline";

interface Props {
  jobId: string;
  report: AnalysisReport;
  videoSource: string | null;
  mockMode: boolean;
  onNewAnalysis: () => void;
}

const SEVERITIES: Severity[] = ["critical", "high", "medium", "low", "info"];

export function ReportView({
  jobId,
  report,
  videoSource,
  mockMode,
  onNewAnalysis,
}: Props): React.JSX.Element {
  const videoRef = useRef<HTMLVideoElement>(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(
    report.findings[0]?.id ?? null,
  );
  const [severityFilter, setSeverityFilter] = useState<string>("all");
  const [detectorFilter, setDetectorFilter] = useState<string>("all");

  const filtered = useMemo(
    () =>
      report.findings.filter(
        (finding) =>
          (severityFilter === "all" || finding.severity === severityFilter) &&
          (detectorFilter === "all" || finding.detector_id === detectorFilter),
      ),
    [report.findings, severityFilter, detectorFilter],
  );
  const selected =
    filtered.find((finding) => finding.id === selectedId) ??
    report.findings.find((finding) => finding.id === selectedId) ??
    filtered[0] ??
    null;
  const active =
    filtered.find((finding) => containsTime(finding.time_range, currentTime)) ??
    null;
  const detectorIds = [...new Set(report.findings.map((item) => item.detector_id))];
  const errors = report.detector_executions.filter(
    (execution) => execution.status === "detector_error",
  );

  const seek = (seconds: number): void => {
    setCurrentTime(seconds);
    if (videoRef.current) videoRef.current.currentTime = seconds;
  };
  const selectFinding = (finding: Finding): void => {
    setSelectedId(finding.id);
    seek(finding.time_range.start_seconds);
  };

  return (
    <main className="report-shell">
      <section className="report-topbar">
        <div>
          <p className="eyebrow">Analysis complete</p>
          <h1>{report.metadata.filename}</h1>
          <p className="report-subtitle">
            {report.metadata.width}×{report.metadata.height} ·{" "}
            {report.metadata.codec.toUpperCase()} ·{" "}
            {formatTime(report.metadata.duration_seconds)}
          </p>
        </div>
        <div className="report-actions">
          <a
            className="secondary-button"
            href={
              mockMode
                ? `data:application/json;charset=utf-8,${encodeURIComponent(
                    JSON.stringify(report, null, 2),
                  )}`
                : reportDownloadUrl(jobId)
            }
            download="report.json"
          >
            JSON
          </a>
          <a
            className="secondary-button"
            href={
              mockMode
                ? "#mock-html-unavailable"
                : artifactUrl(jobId, "report.html")
            }
            download={!mockMode ? "report.html" : undefined}
            aria-disabled={mockMode}
          >
            HTML report
          </a>
          <button className="primary-button compact" type="button" onClick={onNewAnalysis}>
            New analysis
          </button>
        </div>
      </section>

      <section className="report-summary-grid">
        <div className="summary-card finding-total">
          <small>Findings</small>
          <strong>{report.findings.length.toString().padStart(2, "0")}</strong>
          <span>observable intervals</span>
        </div>
        {SEVERITIES.slice(0, 4).map((severity) => {
          const count = report.findings.filter(
            (finding) => finding.severity === severity,
          ).length;
          return (
            <div className="summary-card" key={severity}>
              <small>
                <span className={`severity-symbol severity-${severity}`} aria-hidden="true">
                  {severity === "critical" || severity === "high" ? "!" : "●"}
                </span>{" "}
                {severity}
              </small>
              <strong>{count}</strong>
              <span>{count === 1 ? "interval" : "intervals"}</span>
            </div>
          );
        })}
      </section>

      <section className="review-grid">
        <div className="viewer-column">
          <div className="video-frame">
            {videoSource ? (
              <video
                ref={videoRef}
                src={videoSource}
                controls
                preload="metadata"
                onTimeUpdate={(event) =>
                  setCurrentTime(event.currentTarget.currentTime)
                }
                aria-label="Analyzed local video"
              />
            ) : (
              <div className="video-placeholder">
                <span aria-hidden="true">▶</span>
                <p>Video preview is unavailable after this mock-page refresh.</p>
              </div>
            )}
            {active && (
              <div className={`active-indicator severity-${active.severity}`}>
                {active.severity.toUpperCase()} · {active.title}
              </div>
            )}
          </div>
          <Timeline
            duration={report.metadata.duration_seconds}
            findings={filtered}
            currentTime={currentTime}
            selectedId={selected?.id ?? null}
            onSeek={seek}
            onSelect={selectFinding}
          />
        </div>

        <aside className="finding-browser">
          <div className="finding-browser-header">
            <div>
              <p className="step-label">Review queue</p>
              <h2>Findings</h2>
            </div>
            <span>{filtered.length} shown</span>
          </div>
          <div className="filter-row">
            <label>
              <span className="visually-hidden">Filter by severity</span>
              <select
                value={severityFilter}
                onChange={(event) => setSeverityFilter(event.target.value)}
              >
                <option value="all">All severity</option>
                {SEVERITIES.map((severity) => (
                  <option key={severity} value={severity}>
                    {severity}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="visually-hidden">Filter by detector</span>
              <select
                value={detectorFilter}
                onChange={(event) => setDetectorFilter(event.target.value)}
              >
                <option value="all">All detectors</option>
                {detectorIds.map((detector) => (
                  <option key={detector} value={detector}>
                    {detector}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="finding-list">
            {filtered.length === 0 ? (
              <div className="empty-state">
                <strong>No findings match these filters.</strong>
                <p>Adjust the severity or detector filter.</p>
              </div>
            ) : (
              filtered.map((finding, index) => (
                <button
                  type="button"
                  className={`finding-row ${
                    selected?.id === finding.id ? "is-selected" : ""
                  }`}
                  key={finding.id}
                  onClick={() => selectFinding(finding)}
                >
                  <span className={`finding-index severity-${finding.severity}`}>
                    {(index + 1).toString().padStart(2, "0")}
                  </span>
                  <span>
                    <strong>{finding.title}</strong>
                    <small>
                      {formatTime(finding.time_range.start_seconds)} —{" "}
                      {formatTime(finding.time_range.end_seconds)}
                    </small>
                  </span>
                  <span className={`severity-label severity-${finding.severity}`}>
                    {finding.severity}
                  </span>
                </button>
              ))
            )}
          </div>
        </aside>
      </section>

      {selected && (
        <section className="finding-detail">
          <div className="detail-copy">
            <p className="step-label">{selected.detector_id}</p>
            <h2>{selected.title}</h2>
            <p>{selected.description}</p>
            <div className="score-row">
              <span>
                Score <strong>{selected.score.toFixed(2)}</strong>
              </span>
              <span>
                Confidence <strong>{selected.confidence.toFixed(2)}</strong>
              </span>
              <span>
                Range{" "}
                <strong>
                  {formatTime(selected.time_range.start_seconds)}–
                  {formatTime(selected.time_range.end_seconds)}
                </strong>
              </span>
            </div>
            {selected.limitations.length > 0 && (
              <div className="limitations">
                <strong>Interpretation limits</strong>
                <ul>
                  {selected.limitations.map((limitation) => (
                    <li key={limitation}>{limitation}</li>
                  ))}
                </ul>
              </div>
            )}
            <details className="parameters">
              <summary>Detector parameters</summary>
              <pre>{JSON.stringify(selected.parameters, null, 2)}</pre>
            </details>
          </div>
          <div className="evidence-grid">
            {selected.evidence.map((evidence, index) => (
              <button
                type="button"
                className="evidence-card"
                key={`${evidence.timestamp_seconds}-${index}`}
                onClick={() => seek(evidence.timestamp_seconds)}
              >
                {evidence.relative_path ? (
                  <img
                    src={
                      mockMode
                        ? `/${evidence.relative_path}`
                        : artifactUrl(jobId, evidence.relative_path)
                    }
                    alt={`${evidence.description} at ${formatTime(
                      evidence.timestamp_seconds,
                    )}`}
                  />
                ) : (
                  <span className="missing-evidence">No image</span>
                )}
                <span>
                  <strong>{formatTime(evidence.timestamp_seconds)}</strong>
                  <small>{evidence.description}</small>
                </span>
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="execution-panel">
        <div className="timeline-header">
          <div>
            <p className="step-label">Execution record</p>
            <h2>Detector status</h2>
          </div>
          <span>{report.detector_executions.length} detectors</span>
        </div>
        {errors.length > 0 && (
          <div className="detector-error-banner" role="alert">
            <strong>
              {errors.length} detector{errors.length === 1 ? "" : "s"} did not
              complete
            </strong>
            <p>
              Successful findings are preserved. A detector error is not treated
              as “no issue.”
            </p>
          </div>
        )}
        <div className="execution-table">
          {report.detector_executions.map((execution) => (
            <div className="execution-row" key={execution.detector_id}>
              <strong>{execution.detector_id}</strong>
              <span className={`execution-status status-${execution.status}`}>
                {execution.status === "detector_error" ? "Error" : execution.status}
              </span>
              <span>{execution.findings_count} findings</span>
              <span>{execution.elapsed_seconds.toFixed(2)} s</span>
              {execution.error_message && (
                <small>
                  {execution.error_type
                    ? `${execution.error_type}: ${execution.error_message}`
                    : execution.error_message}
                </small>
              )}
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}
