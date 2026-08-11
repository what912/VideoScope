import { useCallback, useRef, useState } from "react";
import { Link } from "react-router";

import { useAuth } from "../../features/auth/AuthProvider";
import { REPOSITORY_URL } from "../../features/growth/growth-copy";
import { useI18n } from "../../i18n/I18nProvider";
import type { Locale } from "../../i18n/types";
import { CreatorBadge } from "../brand/CreatorBadge";
import { ScopeMark } from "../brand/ScopeMark";
import {
  MobileNavigation,
  type NavigationItem,
} from "./MobileNavigation";

type HeaderProps = {
  showSignIn?: boolean;
};

export function Header({ showSignIn = true }: HeaderProps) {
  const { locale, setLocale, t } = useI18n();
  const { session, status } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);
  const mobileTriggerRef = useRef<HTMLButtonElement>(null);
  const closeMobileNavigation = useCallback(() => setMobileOpen(false), []);

  const navigationItems: NavigationItem[] = [
    { href: "/", label: t.navigation.product },
    { href: "/rescue", label: t.navigation.rescue },
    { href: "/examples", label: t.navigation.examples },
    { href: "/download", label: t.navigation.download },
    { href: "/developers", label: t.navigation.developers },
    { href: "/roadmap", label: t.navigation.roadmap },
    { href: "/community", label: t.navigation.community },
    {
      external: true,
      href: REPOSITORY_URL,
      label: t.navigation.github,
    },
  ];

  function handleLocaleChange(localeValue: string) {
    setLocale(localeValue as Locale);
  }

  return (
    <header className="site-header">
      <div className="site-header__inner">
        <Link
          aria-label={t.brand.homeLabel}
          className="brand-lockup"
          to="/"
        >
          <ScopeMark className="brand-lockup__mark" />
          <span>{t.brand.name}</span>
        </Link>

        <nav
          aria-label={t.navigation.primaryLabel}
          className="desktop-navigation"
        >
          <ul>
            {navigationItems.map((item) => (
              <li key={`${item.label}-${item.href}`}>
                {item.external ? (
                  <a href={item.href}>{item.label}</a>
                ) : (
                  <Link to={item.href}>{item.label}</Link>
                )}
              </li>
            ))}
          </ul>
        </nav>

        <div className="site-header__actions">
          <label className="language-control language-control--compact">
            <span className="visually-hidden">{t.navigation.language}</span>
            <select
              aria-label={t.navigation.language}
              onChange={(event) => handleLocaleChange(event.target.value)}
              value={locale}
            >
              <option value="en">{t.navigation.languageEnglish}</option>
              <option value="zh-CN">{t.navigation.languageChinese}</option>
            </select>
          </label>
          {showSignIn ? (
            <Link className="button button--quiet" to="/auth">
              {status === "authenticated"
                ? session?.user.email ?? t.auth.account
                : t.navigation.signIn}
            </Link>
          ) : null}
          <Link className="button button--primary" to="/workspace">
            {t.navigation.analyze}
          </Link>
        </div>

        <CreatorBadge />
        <button
          aria-expanded={mobileOpen}
          aria-label={t.navigation.openMenu}
          className="icon-button mobile-navigation__trigger"
          onClick={() => setMobileOpen(true)}
          ref={mobileTriggerRef}
          type="button"
        >
          <span aria-hidden="true">☰</span>
        </button>
      </div>

      <MobileNavigation
        items={navigationItems}
        onClose={closeMobileNavigation}
        open={mobileOpen}
        showSignIn={showSignIn}
        triggerRef={mobileTriggerRef}
      />
    </header>
  );
}
