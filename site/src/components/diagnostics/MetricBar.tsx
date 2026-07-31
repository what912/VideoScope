import { useI18n } from "../../i18n/I18nProvider";
import type { QualityMetric } from "../../types/analysis";
import "./diagnostics.css";

interface MetricBarProps {
  metric: QualityMetric;
}

export function MetricBar({ metric }: MetricBarProps) {
  const { t } = useI18n();
  const domain =
    metric.unit === "ratio"
      ? { min: 0, max: 1 }
      : metric.domain &&
          Number.isFinite(metric.domain.min) &&
          Number.isFinite(metric.domain.max) &&
          metric.domain.max > metric.domain.min
        ? metric.domain
        : undefined;
  const normalized = domain
    ? Math.min(
        1,
        Math.max(0, (metric.value - domain.min) / (domain.max - domain.min)),
      )
    : undefined;
  return (
    <div className="metric-bar">
      <div className="metric-bar__heading">
        <strong>{metric.label}</strong>
        <span className="numeric">
          {metric.unit === "ratio"
            ? `${Math.round(metric.value * 100)}%`
            : `${metric.value} ${t.diagnostics.metricUnits[metric.unit]}`}
        </span>
      </div>
      {domain && normalized !== undefined ? (
        <div
          aria-label={metric.label}
          aria-valuemax={domain.max}
          aria-valuemin={domain.min}
          aria-valuenow={metric.value}
          className="metric-bar__track"
          role="meter"
        >
          <span
            data-testid="metric-bar-fill"
            style={{ width: `${normalized * 100}%` }}
          />
        </div>
      ) : null}
      <p>{metric.description}</p>
    </div>
  );
}
