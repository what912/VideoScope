import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import { formatTimestamp } from "./diagnostic-geometry";
import { severitySymbols } from "./severity";

interface IssueCardProps {
  finding: Finding;
  selected: boolean;
  reviewed?: boolean;
  onSelect(finding: Finding): void;
  onReviewChange?(finding: Finding, reviewed: boolean): void;
}

export function IssueCard({
  finding,
  selected,
  reviewed = false,
  onSelect,
  onReviewChange,
}: IssueCardProps) {
  const { t } = useI18n();
  const evidence = finding.evidence.find((item) => item.thumbnail);
  return (
    <article
      className="issue-card"
      data-selected={selected || undefined}
      data-severity={finding.severity}
    >
      {evidence?.thumbnail ? (
        <img
          alt={evidence.description}
          className="issue-card__thumbnail"
          height={evidence.thumbnail.height}
          loading="lazy"
          src={evidence.thumbnail.src}
          width={evidence.thumbnail.width}
        />
      ) : (
        <span aria-hidden="true" className="issue-card__signal">
          {severitySymbols[finding.severity]}
        </span>
      )}
      <div className="issue-card__body">
        <div className="issue-card__meta">
          <span className="severity-label" data-severity={finding.severity}>
            <span aria-hidden="true">{severitySymbols[finding.severity]}</span>{" "}
            {t.diagnostics.severity[finding.severity]}
          </span>
          <span className="numeric">
            {formatTimestamp(finding.time_range.start_seconds)}–
            {formatTimestamp(finding.time_range.end_seconds)}
          </span>
        </div>
        <h3>{finding.title}</h3>
        <p>{finding.description}</p>
        <div className="issue-card__meta">
          <span>
            {t.diagnostics.confidence}:{" "}
            <strong className="numeric">
              {Math.round(finding.confidence * 100)}%
            </strong>
          </span>
          <button
            aria-pressed={selected}
            className="text-button"
            onClick={() => onSelect(finding)}
            type="button"
          >
            {t.diagnostics.viewDetails}: {finding.title}
          </button>
        </div>
        {onReviewChange ? (
          <label className="issue-card__review">
            <input
              checked={reviewed}
              onChange={(event) =>
                onReviewChange(finding, event.currentTarget.checked)
              }
              type="checkbox"
            />
            {t.diagnostics.reviewed}
          </label>
        ) : null}
      </div>
    </article>
  );
}
