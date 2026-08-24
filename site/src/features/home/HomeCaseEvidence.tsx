import { useEffect, useState } from "react";
import { Link } from "react-router";

import { loadFeaturedCaseStudies } from "../../data/case-studies-runtime";
import type { CaseStudy } from "../../data/case-studies";
import { useI18n } from "../../i18n/I18nProvider";
import type { HomeCopy } from "../growth/growth-copy-runtime";
import { FeaturedCaseComparison } from "./FeaturedCaseComparison";

type CaseLoader = () => Promise<readonly CaseStudy[]>;

export function HomeCaseEvidence({
  copy,
  loadCases = loadFeaturedCaseStudies,
}: {
  copy: HomeCopy;
  loadCases?: CaseLoader;
}) {
  const { locale } = useI18n();
  const [cases, setCases] = useState<readonly CaseStudy[] | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const labels = copy.cases;

  useEffect(() => {
    let active = true;
    void loadCases()
      .then((loadedCases) => {
        if (active) setCases(loadedCases);
      })
      .catch(() => {
        if (active) setUnavailable(true);
      });
    return () => {
      active = false;
    };
  }, [loadCases]);

  if (unavailable || cases === null || cases[0] === undefined) {
    return (
      <section className="home-section funnel-case-loading" role="status" aria-live="polite">
        <p>{unavailable ? labels.unavailable : labels.loading}</p>
        {unavailable ? <Link className="text-link" to="/examples">{labels.casesAction}</Link> : null}
      </section>
    );
  }

  return (
    <>
      <FeaturedCaseComparison copy={copy.comparison} item={cases[0]} />
      <section className="home-section funnel-cases" aria-labelledby="funnel-cases-title">
        <p className="eyebrow">{labels.casesEyebrow}</p>
        <h2 id="funnel-cases-title">{labels.casesTitle}</h2>
        <ul>
          {cases.map((item) => (
            <li key={item.id}>
              <h3>{item.title[locale]}</h3>
              <p>{item.summary[locale]}</p>
              <p className="funnel-case__status">{item.verification.status}</p>
              <Link className="text-link" to={`/examples/${item.slug}`}>
                {item.title[locale]}
              </Link>
            </li>
          ))}
        </ul>
        <Link className="text-link" to="/examples">{labels.casesAction}</Link>
      </section>
    </>
  );
}
