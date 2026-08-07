import { Link } from "react-router";

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
      <nav aria-label={t.footer.linksLabel} className="site-footer__links">
        <Link className="text-link" to="/workspace">
          {t.navigation.analyze}
        </Link>
        <Link className="text-link" to="/docs">
          {t.navigation.docs}
        </Link>
        <Link className="text-link" to="/privacy">
          {t.footer.privacy}
        </Link>
      </nav>
    </footer>
  );
}
