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
  const evidenceCopy = growthCopy[locale].caseEvidence;

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
        <p>{copy.caseStudy.description}</p>
      </section>
      <section className="growth-page__evidence" aria-labelledby="case-provenance-title">
        <h2 id="case-provenance-title">{evidenceCopy.provenance}</h2>
        <dl className="growth-page__details">
          <div>
            <dt>{evidenceCopy.provenance}</dt>
            <dd>{caseStudy.provenance}</dd>
          </div>
          <div>
            <dt>{evidenceCopy.source}</dt>
            <dd>{caseStudy.authorizationSummary[locale]}</dd>
          </div>
        </dl>
      </section>
      <section className="growth-page__evidence" aria-labelledby="case-actions-title">
        <h2 id="case-actions-title">{evidenceCopy.actions}</h2>
        <ol className="growth-page__evidence-list">
          {caseStudy.actions.map((action) => (
            <li key={action.actionId}>{action.description[locale]}</li>
          ))}
        </ol>
      </section>
      <section className="growth-page__evidence" aria-labelledby="case-verification-title">
        <h2 id="case-verification-title">{evidenceCopy.verification}</h2>
        <dl className="growth-page__details">
          <div>
            <dt>{evidenceCopy.verificationStatus}</dt>
            <dd><output>{caseStudy.verification.status}</output></dd>
          </div>
        </dl>
        <ul className="growth-page__evidence-list">
          {caseStudy.verification.checks.map((check) => (
            <li key={check.checkId}>
              <span>{check.summary[locale]}</span>
              <output className="growth-page__check-status">{check.status}</output>
            </li>
          ))}
        </ul>
      </section>
      <section className="growth-page__evidence" aria-labelledby="case-limitations-title">
        <h2 id="case-limitations-title">{evidenceCopy.limitations}</h2>
        <ul className="growth-page__evidence-list">
          {caseStudy.limitations.map((limitation) => (
            <li key={limitation.en}>{limitation[locale]}</li>
          ))}
        </ul>
      </section>
      <Link className="text-link" to="/examples">
        {copy.caseStudy.action}
      </Link>
    </article>
  );
}
