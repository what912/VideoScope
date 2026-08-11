import { useEffect, useState } from "react";
import { Link } from "react-router";

import { loadCaseStudyManifest } from "../../data/case-studies-runtime";
import type { CaseStudy } from "../../data/case-studies";
import { useI18n } from "../../i18n/I18nProvider";
import { PublicFunnelCopyState } from "./PublicFunnelCopyState";
import { usePublicFunnelCopy } from "./use-public-funnel-copy";
import "./growth.css";

export function ExamplesPage() {
  const { locale } = useI18n();
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

  const copy = copyState.copy[locale].pages.examples;
  const caseCopy = copyState.copy[locale].home.cases;

  return (
    <article className="growth-page" aria-labelledby="examples-title">
      <header className="growth-page__header">
        <p className="eyebrow">{copy.eyebrow}</p>
        <h1 id="examples-title">{copy.title}</h1>
        <p>{copy.description}</p>
      </header>
      {cases === null ? <p role="status" aria-live="polite">{caseCopy.loading}</p> : null}
      {unavailable ? <p role="alert">{caseCopy.unavailable}</p> : null}
      {cases !== null && !unavailable ? <ul className="growth-page__case-list">
        {cases.map((caseStudy) => (
          <li key={caseStudy.id}>
            <h2>{caseStudy.title[locale]}</h2>
            <p>{caseStudy.summary[locale]}</p>
            <Link className="text-link" to={`/examples/${caseStudy.slug}`}>
              {copy.action}
            </Link>
          </li>
        ))}
      </ul> : null}
    </article>
  );
}
