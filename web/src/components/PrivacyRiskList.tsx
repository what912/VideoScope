import type { PrivacyDecision, PrivacyRisk } from "../types";
import { privacyIdentifierText, privacyServerText } from "../privacyI18n";
import type { WorkbenchLocale } from "./PublishReadyView";

interface PrivacyRiskListProps {
  locale: WorkbenchLocale;
  risks: PrivacyRisk[];
  selectedRiskId: string | null;
  decisions: Record<string, PrivacyDecision>;
  onSelect: (risk: PrivacyRisk) => void;
}

const severitySymbol: Record<PrivacyRisk["severity"], string> = {
  info: "●",
  low: "●",
  medium: "▲",
  high: "▲",
  critical: "◆",
};

export function PrivacyRiskList({
  locale,
  risks,
  selectedRiskId,
  decisions,
  onSelect,
}: PrivacyRiskListProps): React.JSX.Element {
  const zh = locale === "zh-CN";
  return (
    <section className="privacy-risk-panel" aria-labelledby="privacy-risk-heading">
      <div className="privacy-section-heading">
        <div>
          <p className="step-label">{zh ? "02 / 人工复核" : "02 / HUMAN REVIEW"}</p>
          <h2 id="privacy-risk-heading">{zh ? "隐私风险" : "Privacy risks"}</h2>
        </div>
        <span>{risks.length}</span>
      </div>
      <div className="privacy-risk-list">
        {risks.map((risk) => {
          const decision = decisions[risk.id] ?? risk.decision;
          const riskTitle = zh
            ? privacyIdentifierText("risk", risk.risk_type, locale)
            : risk.title;
          return (
            <button
              key={risk.id}
              type="button"
              className={`privacy-risk-card severity-${risk.severity} ${
                selectedRiskId === risk.id ? "is-selected" : ""
              }`}
              aria-label={`${zh ? "复核" : "Review"} ${riskTitle}`}
              aria-pressed={selectedRiskId === risk.id}
              onClick={() => onSelect(risk)}
            >
              <span className="privacy-risk-symbol" aria-hidden="true">
                {severitySymbol[risk.severity]}
              </span>
              <span className="privacy-risk-copy">
                <strong>{riskTitle}</strong>
                <small>
                  {risk.start_seconds.toFixed(1)}–{risk.end_seconds.toFixed(1)} s ·{" "}
                  {privacyIdentifierText("risk", risk.risk_type, locale)}
                </small>
                <span>{privacyServerText(risk.public_description, locale, "risk_description", risk.scanner_id)}</span>
              </span>
              <span className={`privacy-decision-badge decision-${decision}`}>
                {decision === "unreviewed"
                  ? zh
                    ? "未复核"
                    : "Unreviewed"
                  : decision === "allow"
                    ? zh
                      ? "允许"
                      : "Allowed"
                    : zh
                      ? "将脱敏"
                      : "Redact"}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
