import { Link, useParams } from "react-router";

import { findCaseStudy } from "../../data/case-studies";
import { useI18n } from "../../i18n/I18nProvider";
import { growthCopy } from "./growth-copy";
import "./growth.css";

export function CaseStudyPage() {
  const { locale } = useI18n();
  const { slug } = useParams();
  const caseStudy = slug ? findCaseStudy(slug) : undefined;
  const copy = growthCopy[locale].pages;

  if (!caseStudy) {
    return (
      <article className="growth-page" aria-labelledby="missing-case-title">
        <header className="growth-page__header">
          <p className="eyebrow">{copy.missingCase.eyebrow}</p>
          <h1 id="missing-case-title">{copy.missingCase.title}</h1>
          <p>{copy.missingCase.description}</p>
        </header>
        <Link className="button button--primary" to="/examples">
          {copy.missingCase.action}
        </Link>
      </article>
    );
  }

  return (
    <article className="growth-page" aria-labelledby="case-study-title">
      <header className="growth-page__header">
        <p className="eyebrow">{copy.caseStudy.eyebrow}</p>
        <h1 id="case-study-title">{caseStudy.title[locale]}</h1>
        <p>{caseStudy.summary[locale]}</p>
      </header>
      <section className="growth-page__record" aria-label={copy.caseStudy.title}>
        <h2>{copy.caseStudy.title}</h2>
        <p>{caseStudy.observableSymptom[locale]}</p>
        <p>{caseStudy.authorizationSummary[locale]}</p>
        <p>{copy.caseStudy.description}</p>
      </section>
      <Link className="text-link" to="/examples">
        {copy.caseStudy.action}
      </Link>
    </article>
  );
}
