import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import { getStaticCopy } from "./static-copy";
import "./static.css";

export function NotFoundPage() {
  const { locale } = useI18n();
  const copy = getStaticCopy(locale).notFound;

  return (
    <section className="not-found-page" aria-labelledby="not-found-title">
      <div aria-hidden="true" className="not-found-page__scope">
        <span />
        <span />
        <span />
      </div>
      <div>
        <p className="eyebrow">{copy.eyebrow}</p>
        <h1 id="not-found-title">{copy.title}</h1>
        <p>{copy.description}</p>
        <Link className="button button--primary" to="/">
          {copy.action}
        </Link>
      </div>
    </section>
  );
}
