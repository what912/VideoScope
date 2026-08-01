import { useI18n } from "../../i18n/I18nProvider";
import type { Finding, JsonValue } from "../../types/analysis";
import { formatTimestamp } from "./diagnostic-geometry";
import { severitySymbols } from "./severity";
import "./diagnostics.css";

function formatValue(value: JsonValue) {
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

interface IssueDetailPanelProps {
  finding?: Finding;
  onEvidenceSeek(timestampSeconds: number): void;
}

export function IssueDetailPanel({
  finding,
  onEvidenceSeek,
}: IssueDetailPanelProps) {
  const { t } = useI18n();
  if (!finding) {
    return <p className="diagnostic-empty">{t.diagnostics.selectFinding}</p>;
  }
  return (
    <aside
      aria-label={t.diagnostics.findingDetails}
      className="issue-detail"
      data-testid="finding-detail"
    >
      <div>
        <span className="severity-label" data-severity={finding.severity}>
          <span aria-hidden="true">{severitySymbols[finding.severity]}</span>{" "}
          {t.diagnostics.severity[finding.severity]}
        </span>
        <span className="numeric">
          {formatTimestamp(finding.time_range.start_seconds)}–
          {formatTimestamp(finding.time_range.end_seconds)}
        </span>
        <h2>{finding.title}</h2>
        <p>{finding.description}</p>
      </div>
      <dl className="issue-detail__stats">
        <div>
          <dt>{t.diagnostics.detector}</dt>
          <dd className="numeric">{finding.detector_id}</dd>
        </div>
        <div>
          <dt>{t.diagnostics.detectorScore}</dt>
          <dd className="numeric">{finding.score.toFixed(3)}</dd>
        </div>
        <div>
          <dt>{t.diagnostics.confidence}</dt>
          <dd className="numeric">{finding.confidence.toFixed(3)}</dd>
        </div>
      </dl>
      <section>
        <h3>{t.diagnostics.evidence}</h3>
        <div className="evidence-strip">
          {finding.evidence.map((evidence, index) => (
            <button
              className="evidence-button"
              key={`${evidence.timestamp_seconds}-${index}`}
              onClick={() => onEvidenceSeek(evidence.timestamp_seconds)}
              type="button"
            >
              {evidence.thumbnail ? (
                <img
                  alt={evidence.description}
                  height={evidence.thumbnail.height}
                  loading="lazy"
                  src={evidence.thumbnail.src}
                  width={evidence.thumbnail.width}
                />
              ) : (
                <span aria-hidden="true" className="evidence-button__empty">
                  ◫
                </span>
              )}
              <span>{evidence.description}</span>
              <span className="numeric">
                {formatTimestamp(evidence.timestamp_seconds)}
              </span>
            </button>
          ))}
        </div>
      </section>
      <section>
        <h3>{t.diagnostics.limitations}</h3>
        <ul>
          {finding.limitations.map((limitation) => (
            <li key={limitation}>{limitation}</li>
          ))}
        </ul>
      </section>
      <section>
        <h3>{t.diagnostics.parameters}</h3>
        <dl className="parameter-list">
          {Object.entries(finding.parameters).map(([key, value]) => (
            <div key={key}>
              <dt className="numeric">{key}</dt>
              <dd className="numeric">{formatValue(value)}</dd>
            </div>
          ))}
        </dl>
      </section>
    </aside>
  );
}
