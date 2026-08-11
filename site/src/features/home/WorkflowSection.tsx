import { useI18n } from "../../i18n/I18nProvider";
import { legacyHomeCopy } from "./legacy-home-copy";

export function WorkflowSection() {
  const { locale } = useI18n();
  const copy = legacyHomeCopy[locale];
  return (
    <section className="home-section workflow-section">
      <div className="home-section__heading">
        <p className="eyebrow">{copy.workflow.eyebrow}</p>
        <h2>{copy.workflow.title}</h2>
      </div>
      <ol>
        {copy.workflow.steps.map((step, index) => (
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
