import { useI18n } from "../../i18n/I18nProvider";
import { HomeMedia } from "./HomeMedia";
import { repositoryUrl } from "./home-data";

interface HeroProps {
  intervalCount: number;
  onAnalyze(): void;
  onDemo(): void;
}
export function Hero({ intervalCount, onAnalyze, onDemo }: HeroProps) {
  const { t } = useI18n();
  return (
    <section className="home-hero" data-testid="home-hero">
      <HomeMedia
        className="home-hero__atmosphere"
        eager
        label={t.home.hero.mediaLabel}
        role="hero"
      />
      <div className="home-hero__grid" aria-hidden="true" />
      <div className="home-hero__copy">
        <p className="eyebrow">{t.home.hero.eyebrow}</p>
        <h1>{t.home.hero.title}</h1>
        <p className="home-hero__description">{t.home.hero.description}</p>
        <div className="home-actions">
          <button className="button button--primary" onClick={onAnalyze} type="button">
            {t.home.hero.analyze}
          </button>
          <button className="button button--quiet" onClick={onDemo} type="button">
            {t.home.hero.demo}
          </button>
          <a className="text-link" href={repositoryUrl}>
            {t.home.hero.github}
          </a>
        </div>
        <div className="home-hero__trust">
          <span>{t.home.hero.local}</span>
          <strong className="numeric">
            {intervalCount} {t.home.hero.intervals}
          </strong>
          <span className="demo-label">{t.home.demoLabel}</span>
        </div>
      </div>
      <div aria-hidden="true" className="home-hero__scan" data-decorative-motion />
    </section>
  );
}
