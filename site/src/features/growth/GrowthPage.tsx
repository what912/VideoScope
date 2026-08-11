import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import { growthCopy } from "./growth-copy";
import "./growth.css";

type GrowthPageName = keyof typeof growthCopy.en.pages;

type GrowthPageProps = {
  actionHref: string;
  page: GrowthPageName;
};

export function GrowthPage({ actionHref, page }: GrowthPageProps) {
  const { locale } = useI18n();
  const copy = growthCopy[locale].pages[page];

  return (
    <article className="growth-page" aria-labelledby={`${page}-title`}>
      <header className="growth-page__header">
        <p className="eyebrow">{copy.eyebrow}</p>
        <h1 id={`${page}-title`}>{copy.title}</h1>
        <p>{copy.description}</p>
      </header>
      <Link className="button button--primary" to={actionHref}>
        {copy.action}
      </Link>
    </article>
  );
}
