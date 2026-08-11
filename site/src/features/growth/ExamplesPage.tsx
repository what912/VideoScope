import { Link } from "react-router";

import { caseStudyManifest } from "../../data/case-studies";
import { useI18n } from "../../i18n/I18nProvider";
import { growthCopy } from "./growth-copy";
import "./growth.css";

export function ExamplesPage() {
  const { locale } = useI18n();
  const copy = growthCopy[locale].pages.examples;

  return (
    <article className="growth-page" aria-labelledby="examples-title">
      <header className="growth-page__header">
        <p className="eyebrow">{copy.eyebrow}</p>
        <h1 id="examples-title">{copy.title}</h1>
        <p>{copy.description}</p>
      </header>
      <ul className="growth-page__case-list">
        {caseStudyManifest.cases.map((caseStudy) => (
          <li key={caseStudy.id}>
            <h2>{caseStudy.title[locale]}</h2>
            <p>{caseStudy.summary[locale]}</p>
            <Link className="text-link" to={`/examples/${caseStudy.slug}`}>
              {copy.action}
            </Link>
          </li>
        ))}
      </ul>
    </article>
  );
}
