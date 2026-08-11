import { Link } from "react-router";

import {
  CREATOR_ATTRIBUTION,
  CREATOR_URL,
  REPOSITORY_URL,
} from "../../features/growth/growth-constants";
import { useI18n } from "../../i18n/I18nProvider";
import { ScopeMark } from "../brand/ScopeMark";

export function Footer() {
  const { t } = useI18n();

  return (
    <footer aria-label={t.footer.label} className="site-footer">
      <div className="site-footer__brand">
        <ScopeMark className="site-footer__mark" />
        <div>
          <strong>{t.brand.name}</strong>
          <p>{t.footer.description}</p>
        </div>
      </div>
      <p>{t.footer.localFirst}</p>
      <a className="text-link" href={CREATOR_URL}>
        {CREATOR_ATTRIBUTION}
      </a>
      <nav aria-label={t.footer.linksLabel} className="site-footer__links">
        <Link className="text-link" to="/rescue">
          {t.navigation.rescue}
        </Link>
        <Link className="text-link" to="/examples">
          {t.navigation.examples}
        </Link>
        <Link className="text-link" to="/download">{t.navigation.download}</Link>
        <a className="text-link" href={REPOSITORY_URL}>{t.navigation.github}</a>
      </nav>
    </footer>
  );
}
