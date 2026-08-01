import type { ChangeEvent } from "react";

import { useI18n } from "../../i18n/I18nProvider";
import type { Severity } from "../../types/analysis";
import type { BrowserReport } from "../../types/report";

const severityOrder: Severity[] = [
  "critical",
  "high",
  "medium",
  "low",
  "info",
];

interface WorkspaceHeaderProps {
  report: BrowserReport;
  sessionLoaded: boolean;
  onReselect(file: File): void;
}

export function WorkspaceHeader({
  report,
  sessionLoaded,
  onReselect,
}: WorkspaceHeaderProps) {
  const { t } = useI18n();
  const handleReselect = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (file) onReselect(file);
  };

  return (
    <>
      <header className="workspace__heading">
        <div>
          <p className="eyebrow">{t.workspace.eyebrow}</p>
          <h1 id="workspace-title">{report.title}</h1>
          <p>
            <span className="numeric">
              {report.metadata.width} × {report.metadata.height}
            </span>{" "}
            · <span className="numeric">{report.metadata.duration_seconds}s</span>
          </p>
        </div>
        <div className="workspace__summary" aria-label={t.workspace.summary}>
          <strong className="numeric">{report.findings.length}</strong>
          <span>{t.workspace.reviewIntervals}</span>
          {severityOrder.map((severity) =>
            report.summary.severity_counts[severity] > 0 ? (
              <span
                className="severity-label"
                data-severity={severity}
                key={severity}
              >
                {t.diagnostics.severity[severity]}{" "}
                <span className="numeric">
                  {report.summary.severity_counts[severity]}
                </span>
              </span>
            ) : null,
          )}
        </div>
      </header>

      {!sessionLoaded ? (
        <section
          className="workspace__video-missing"
          aria-labelledby="workspace-video-missing"
        >
          <div>
            <h2 id="workspace-video-missing">
              {t.workspace.videoMissingTitle}
            </h2>
            <p>{t.workspace.videoMissingMessage}</p>
          </div>
          <label className="button button--quiet">
            {t.workspace.reselectVideo}
            <input
              accept="video/*"
              aria-label={t.workspace.reselectVideo}
              className="visually-hidden"
              onChange={handleReselect}
              type="file"
            />
          </label>
        </section>
      ) : null}
    </>
  );
}
