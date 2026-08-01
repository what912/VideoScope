import { useI18n } from "../../i18n/I18nProvider";
import type { DetectorExecution } from "../../types/analysis";
import "./diagnostics.css";

interface DetectorStatusListProps {
  executions: DetectorExecution[];
}

export function DetectorStatusList({ executions }: DetectorStatusListProps) {
  const { t } = useI18n();
  return (
    <section aria-label={t.diagnostics.detectorStatus}>
      <h2>{t.diagnostics.detectorStatus}</h2>
      <ul className="detector-status-list">
        {executions.map((execution) => (
          <li
            data-status={execution.status}
            key={`${execution.detector_id}-${execution.detector_version}`}
          >
            <span aria-hidden="true">
              {execution.status === "ok"
                ? "✓"
                : execution.status === "failed"
                  ? "!"
                  : "–"}
            </span>
            <strong>{execution.detector_id}</strong>
            <span>{t.diagnostics.status[execution.status]}</span>
            {execution.status === "failed" && execution.error_message ? (
              <p>{execution.error_message}</p>
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  );
}
