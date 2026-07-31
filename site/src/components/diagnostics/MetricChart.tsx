import { useId } from "react";

import { useI18n } from "../../i18n/I18nProvider";
import type { QualityMetric } from "../../types/analysis";
import { intervalToPercent } from "./diagnostic-geometry";
import "./diagnostics.css";

export interface MetricSample {
  time: number;
  value: number;
}

interface MetricChartProps {
  metric: QualityMetric;
  samples: MetricSample[];
  currentTime: number;
  duration: number;
}

export function MetricChart({
  metric,
  samples,
  currentTime,
  duration,
}: MetricChartProps) {
  const { t } = useI18n();
  const titleId = useId();
  const finiteSamples = samples.filter(
    (sample) => Number.isFinite(sample.time) && Number.isFinite(sample.value),
  );
  const finiteValues = finiteSamples
    .map((sample) => sample.value)
    .filter(Number.isFinite);
  const suppliedDomain =
    metric.unit !== "ratio" &&
    metric.domain &&
    Number.isFinite(metric.domain.min) &&
    Number.isFinite(metric.domain.max) &&
    metric.domain.max >= metric.domain.min
      ? metric.domain
      : undefined;
  const dataMin = finiteValues.length > 0 ? Math.min(...finiteValues) : 0;
  const dataMax = finiteValues.length > 0 ? Math.max(...finiteValues) : 0;
  const domain =
    metric.unit === "ratio"
      ? { min: 0, max: 1 }
      : suppliedDomain ?? { min: dataMin, max: dataMax };
  const domainWidth = domain.max - domain.min;
  const yForValue = (value: number) => {
    if (!Number.isFinite(value)) return 50;
    if (domainWidth === 0) return 50;
    const normalized = (value - domain.min) / domainWidth;
    return 100 - Math.min(1, Math.max(0, normalized)) * 100;
  };
  const points = finiteSamples
    .map((sample) => {
      const x =
        duration > 0 ? Math.min(100, Math.max(0, sample.time / duration * 100)) : 0;
      const y = yForValue(sample.value);
      return `${x},${y}`;
    })
    .join(" ");
  const cursor = intervalToPercent(currentTime, currentTime, duration).left;
  return (
    <figure className="metric-chart">
      <figcaption id={titleId}>
        <span>{metric.label}</span>
        <span className="metric-chart__domain numeric" data-testid="metric-chart-domain">
          {domain.min === domain.max
            ? domain.min
            : `${domain.min}–${domain.max}`}{" "}
          {t.diagnostics.metricUnits[metric.unit]}
        </span>
      </figcaption>
      <svg
        aria-labelledby={titleId}
        preserveAspectRatio="none"
        role="img"
        viewBox="0 0 100 100"
      >
        <line className="metric-chart__grid" x1="0" x2="100" y1="50" y2="50" />
        <polyline
          className="metric-chart__line"
          data-testid="metric-chart-line"
          points={points}
        />
        <line
          className="metric-chart__cursor"
          data-testid="metric-cursor"
          data-time={String(currentTime)}
          x1={cursor}
          x2={cursor}
          y1="0"
          y2="100"
        />
      </svg>
      <p>{metric.description}</p>
    </figure>
  );
}
