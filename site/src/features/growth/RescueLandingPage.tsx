import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import { growthCopy } from "./growth-copy";
import "./growth.css";

export function RescueLandingPage() {
  const { locale } = useI18n();
  const copy = growthCopy[locale];

  return (
    <article className="growth-page" aria-labelledby="rescue-title">
      <header className="growth-page__header growth-page__header--signal">
        <p className="eyebrow">{copy.pages.rescue.eyebrow}</p>
        <h1 id="rescue-title">{copy.positioning}</h1>
        <p>{copy.pages.rescue.description}</p>
      </header>
      <ul className="growth-page__boundaries">
        <li>{copy.sourcePreserved}</li>
        <li>{copy.localBoundary}</li>
      </ul>
      <Link className="button button--primary" to="/connect">
        {copy.pages.rescue.action}
      </Link>
    </article>
  );
}
