import {
  DetectorStatusList,
  MetricBar,
  MetricChart,
} from "../../components/diagnostics";
import { useI18n } from "../../i18n/I18nProvider";
import type { BrowserReport } from "../../types/report";

interface WorkspaceSignalsProps {
  currentTime: number;
  open: boolean;
  report: BrowserReport;
  onToggle(): void;
}

export function WorkspaceSignals({
  currentTime,
  open,
  report,
  onToggle,
}: WorkspaceSignalsProps) {
  const { t } = useI18n();
  const failedExecutions = report.detector_executions.filter(
    (execution) => execution.status === "failed",
  );
  const completedExecutions = report.detector_executions.filter(
    (execution) => execution.status !== "failed",
  );
  return (
    <>
      <button
        aria-expanded={open}
        className="workspace__signals-toggle"
        onClick={onToggle}
        type="button"
      >
        {t.workspace.signalPanel}
      </button>
      {open ? (
        <section
          aria-label={t.workspace.signalPanel}
          className="workspace__signals"
        >
          {report.metrics.map((metric) => (
            <div className="workspace__metric" key={metric.id}>
              <MetricBar metric={metric} />
              <MetricChart
                currentTime={currentTime}
                duration={report.metadata.duration_seconds}
                metric={metric}
                samples={[
                  { time: 0, value: metric.value },
                  {
                    time: report.metadata.duration_seconds,
                    value: metric.value,
                  },
                ]}
              />
            </div>
          ))}
        </section>
      ) : null}
      {completedExecutions.length > 0 ? (
        <DetectorStatusList executions={completedExecutions} />
      ) : null}
      {failedExecutions.length > 0 ? (
        <section
          aria-label={t.workspace.detectorErrors}
          className="workspace__detector-errors"
        >
          <h2>{t.workspace.detectorErrors}</h2>
          <ul className="detector-status-list">
            {failedExecutions.map((execution) => (
              <li
                data-status="failed"
                key={`${execution.detector_id}-${execution.detector_version}`}
              >
                <span aria-hidden="true">!</span>
                <strong>{execution.detector_id}</strong>
                <span>{t.diagnostics.status.failed}</span>
                {execution.error_message ? (
                  <p>{execution.error_message}</p>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </>
  );
}
