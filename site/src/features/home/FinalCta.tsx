import { useI18n } from "../../i18n/I18nProvider";
import { repositoryUrl } from "./home-data";

interface FinalCtaProps {
  onAnalyze(): void;
  onDemo(): void;
}
export function FinalCta({ onAnalyze, onDemo }: FinalCtaProps) {
  const { t } = useI18n();
  return (
    <section className="final-cta">
      <div>
        <p className="eyebrow">{t.home.final.eyebrow}</p>
        <h2>{t.home.final.title}</h2>
        <p>{t.home.final.description}</p>
      </div>
      <div className="home-actions">
        <button className="button button--primary" onClick={onAnalyze} type="button">
          {t.home.final.analyze}
        </button>
        <button className="button button--quiet" onClick={onDemo} type="button">
          {t.home.final.demo}
        </button>
        <a className="text-link" href={repositoryUrl}>
          {t.home.final.github}
        </a>
      </div>
    </section>
  );
}
