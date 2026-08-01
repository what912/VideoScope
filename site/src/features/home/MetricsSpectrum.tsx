import { MetricBar } from "../../components/diagnostics";
import { useI18n } from "../../i18n/I18nProvider";
import type { QualityMetric } from "../../types/analysis";
import { HomeMedia } from "./HomeMedia";

export function MetricsSpectrum({ metrics }: { metrics: QualityMetric[] }) {
  const { t } = useI18n();
  const cpuMetrics = metrics.filter((metric) => metric.kind === "browser_cpu");
  const optionalMetrics = metrics.filter(
    (metric) => metric.kind === "optional_demo",
  );
  return (
    <section className="home-section metrics-spectrum">
      <div className="home-section__heading">
        <p className="eyebrow">{t.home.metrics.eyebrow}</p>
        <h2>{t.home.metrics.title}</h2>
        <p>{t.home.metrics.description}</p>
        <span className="demo-label">{t.home.demoLabel}</span>
      </div>
      <div className="metrics-spectrum__rail">
        <section>
          <h3>{t.home.metrics.cpu}</h3>
          {cpuMetrics.map((metric) => <MetricBar key={metric.id} metric={metric} />)}
        </section>
        <section>
          <h3>{t.home.metrics.optional}</h3>
          {optionalMetrics.map((metric) => (
            <div className="metrics-spectrum__optional" key={metric.id}>
              <span className="signal-kind">{t.home.narrative.optional}</span>
              <MetricBar metric={metric} />
            </div>
          ))}
        </section>
      </div>
      <section className="evidence-atlas">
        <h3>{t.home.metrics.evidence}</h3>
        <span className="demo-label">{t.home.demoLabel}</span>
        <div>
          <HomeMedia label={t.home.metrics.evidenceLabels.a} role="evidence-a" />
          <HomeMedia label={t.home.metrics.evidenceLabels.b} role="evidence-b" />
          <HomeMedia label={t.home.metrics.evidenceLabels.c} role="evidence-c" />
        </div>
      </section>
    </section>
  );
}
