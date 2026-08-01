import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import { IssueCard } from "./IssueCard";
import "./diagnostics.css";

interface IssueListProps {
  findings: Finding[];
  selectedFindingId?: string;
  reviewedFindingIds?: ReadonlySet<string>;
  onSelectFinding(finding: Finding): void;
  onReviewChange?(finding: Finding, reviewed: boolean): void;
}

export function IssueList({
  findings,
  selectedFindingId,
  reviewedFindingIds,
  onSelectFinding,
  onReviewChange,
}: IssueListProps) {
  const { t } = useI18n();
  return (
    <section aria-label={t.diagnostics.findings} className="issue-list">
      <div className="diagnostic-section-heading">
        <h2>{t.diagnostics.findings}</h2>
        <span className="numeric">{findings.length}</span>
      </div>
      {findings.length === 0 ? (
        <p className="diagnostic-empty">{t.diagnostics.noFindings}</p>
      ) : (
        findings.map((finding) => (
          <IssueCard
            finding={finding}
            key={finding.id}
            onReviewChange={onReviewChange}
            onSelect={onSelectFinding}
            reviewed={reviewedFindingIds?.has(finding.id)}
            selected={finding.id === selectedFindingId}
          />
        ))
      )}
    </section>
  );
}
