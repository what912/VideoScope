import { Outlet } from "react-router";

import { Footer } from "../components/layout/Footer";
import { Header } from "../components/layout/Header";
import { PageTransition } from "../components/layout/PageTransition";
import { useI18n } from "../i18n/I18nProvider";

export function App() {
  const { t } = useI18n();

  return (
    <>
      <a className="skip-link" href="#main-content">
        {t.shell.skipToContent}
      </a>
      <div className="app-shell">
        <Header showSignIn />
        <main id="main-content" tabIndex={-1}>
          <PageTransition>
            <Outlet />
          </PageTransition>
        </main>
        <Footer />
      </div>
      <span
        aria-hidden="true"
        className="print-attribution numeric"
        data-attribution={t.brand.creator}
        data-testid="print-attribution"
      />
    </>
  );
}
