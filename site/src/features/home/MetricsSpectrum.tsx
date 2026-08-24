import { MetricBar } from "../../components/diagnostics";
import { useI18n } from "../../i18n/I18nProvider";
import type { QualityMetric } from "../../types/analysis";
import { HomeMedia } from "./HomeMedia";
import { legacyHomeCopy } from "./legacy-home-copy";

export function MetricsSpectrum({ metrics }: { metrics: QualityMetric[] }) {
  const { locale } = useI18n();
  const copy = legacyHomeCopy[locale];
  const cpuMetrics = metrics.filter((metric) => metric.kind === "browser_cpu");
  const optionalMetrics = metrics.filter(
    (metric) => metric.kind === "optional_demo",
  );
  return (
    <section className="home-section metrics-spectrum">
      <div className="home-section__heading">
        <p className="eyebrow">{copy.metrics.eyebrow}</p>
        <h2>{copy.metrics.title}</h2>
        <p>{copy.metrics.description}</p>
        <span className="demo-label">{copy.demoLabel}</span>
      </div>
      <div className="metrics-spectrum__rail">
        <section>
          <h3>{copy.metrics.cpu}</h3>
          {cpuMetrics.map((metric) => <MetricBar key={metric.id} metric={metric} />)}
        </section>
        <section>
          <h3>{copy.metrics.optional}</h3>
          {optionalMetrics.map((metric) => (
            <div className="metrics-spectrum__optional" key={metric.id}>
              <span className="signal-kind">{copy.narrative.optional}</span>
              <MetricBar metric={metric} />
            </div>
          ))}
        </section>
      </div>
      <section className="evidence-atlas">
        <h3>{copy.metrics.evidence}</h3>
        <span className="demo-label">{copy.demoLabel}</span>
        <div>
          <HomeMedia label={copy.metrics.evidenceLabels.a} role="evidence-a" />
          <HomeMedia label={copy.metrics.evidenceLabels.b} role="evidence-b" />
          <HomeMedia label={copy.metrics.evidenceLabels.c} role="evidence-c" />
        </div>
      </section>
    </section>
  );
}
