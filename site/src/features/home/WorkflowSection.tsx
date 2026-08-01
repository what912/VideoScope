import { useI18n } from "../../i18n/I18nProvider";

export function WorkflowSection() {
  const { t } = useI18n();
  return (
    <section className="home-section workflow-section">
      <div className="home-section__heading">
        <p className="eyebrow">{t.home.workflow.eyebrow}</p>
        <h2>{t.home.workflow.title}</h2>
      </div>
      <ol>
        {t.home.workflow.steps.map((step, index) => (
          <li key={step.title}>
            <span className="numeric">0{index + 1}</span>
            <h3>{step.title}</h3>
            <p>{step.description}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}
