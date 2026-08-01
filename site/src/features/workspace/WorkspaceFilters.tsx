import { useI18n } from "../../i18n/I18nProvider";
import type { Severity } from "../../types/analysis";

interface WorkspaceFiltersProps {
  detectorIds: string[];
  detectorFilter: string;
  severityFilter: Severity | "all";
  onDetectorChange(value: string): void;
  onSeverityChange(value: Severity | "all"): void;
}

export function WorkspaceFilters({
  detectorIds,
  detectorFilter,
  severityFilter,
  onDetectorChange,
  onSeverityChange,
}: WorkspaceFiltersProps) {
  const { t } = useI18n();
  const severities: Severity[] = [
    "critical",
    "high",
    "medium",
    "low",
    "info",
  ];

  return (
    <div className="workspace-filters">
      <label>
        <span>{t.workspace.detectorFilter}</span>
        <select
          aria-label={t.workspace.detectorFilter}
          onChange={(event) => onDetectorChange(event.currentTarget.value)}
          value={detectorFilter}
        >
          <option value="all">{t.workspace.allDetectors}</option>
          {detectorIds.map((detectorId) => (
            <option key={detectorId} value={detectorId}>
              {detectorId}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>{t.workspace.severityFilter}</span>
        <select
          aria-label={t.workspace.severityFilter}
          onChange={(event) =>
            onSeverityChange(event.currentTarget.value as Severity | "all")
          }
          value={severityFilter}
        >
          <option value="all">{t.workspace.allSeverities}</option>
          {severities.map((severity) => (
            <option key={severity} value={severity}>
              {t.diagnostics.severity[severity]}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}
