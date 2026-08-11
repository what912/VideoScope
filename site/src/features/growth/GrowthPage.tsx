import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import { PublicFunnelCopyState } from "./PublicFunnelCopyState";
import { usePublicFunnelCopy } from "./use-public-funnel-copy";
import "./growth.css";

type GrowthPageName = "download" | "developers" | "roadmap" | "community";

type GrowthPageProps = {
  actionHref: string;
  page: GrowthPageName;
};

export function GrowthPage({ actionHref, page }: GrowthPageProps) {
  const { locale } = useI18n();
  const copyState = usePublicFunnelCopy();

  if (copyState.status !== "ready") {
    return <PublicFunnelCopyState state={copyState} />;
  }

  const copy = copyState.copy[locale].pages[page];

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
