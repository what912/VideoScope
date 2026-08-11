import { useEffect, useState } from "react";
import { Link, useParams } from "react-router";

import { loadCaseStudyManifest } from "../../data/case-studies-runtime";
import type { CaseStudy } from "../../data/case-studies";
import { useI18n } from "../../i18n/I18nProvider";
import { PublicFunnelCopyState } from "./PublicFunnelCopyState";
import { usePublicFunnelCopy } from "./use-public-funnel-copy";
import "./growth.css";

export function CaseStudyPage() {
  const { locale } = useI18n();
  const { slug } = useParams();
  const copyState = usePublicFunnelCopy();
  const [cases, setCases] = useState<readonly CaseStudy[] | null>(null);
  const [unavailable, setUnavailable] = useState(false);

  useEffect(() => {
    if (copyState.status !== "ready") return undefined;
    let active = true;
    setUnavailable(false);
    void loadCaseStudyManifest()
      .then((manifest) => {
        if (active) setCases(manifest.cases);
      })
      .catch(() => {
        if (active) setUnavailable(true);
      });
    return () => {
      active = false;
    };
  }, [copyState.status]);

  if (copyState.status !== "ready") {
    return <PublicFunnelCopyState state={copyState} />;
  }

  const copy = copyState.copy[locale].pages;
  const evidenceCopy = copyState.copy[locale].caseEvidence;

  if (unavailable) {
    return <article className="growth-page" role="alert">{copyState.copy[locale].home.cases.unavailable}</article>;
  }

  if (cases === null) {
    return <article className="growth-page" role="status" aria-live="polite">{copyState.copy[locale].home.cases.loading}</article>;
  }

  const caseStudy = slug ? cases.find((item) => item.slug === slug) : undefined;

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
