import {
  type RefObject,
  useCallback,
  useEffect,
  useId,
  useRef,
} from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router-dom";

import { useAuth } from "../../features/auth/AuthProvider";
import { useI18n } from "../../i18n/I18nProvider";
import type { Locale } from "../../i18n/types";
import { CreatorBadge } from "../brand/CreatorBadge";

export type NavigationItem = {
  external?: boolean;
  href: string;
  label: string;
};

type MobileNavigationProps = {
  items: NavigationItem[];
  onClose(): void;
  open: boolean;
  showSignIn?: boolean;
  triggerRef: RefObject<HTMLButtonElement | null>;
};

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function MobileNavigation({
  items,
  onClose,
  open,
  showSignIn = true,
  triggerRef,
}: MobileNavigationProps) {
  const { locale, setLocale, t } = useI18n();
  const { session, status } = useAuth();
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeAndRestoreFocus = useCallback(() => {
    onClose();
    triggerRef.current?.focus();
  }, [onClose, triggerRef]);

  useEffect(() => {
    if (!open) {
      return;
    }

    const panel = panelRef.current;
    const focusable = panel?.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
    focusable?.item(0).focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeAndRestoreFocus();
        return;
      }

      if (event.key !== "Tab" || !panel) {
        return;
      }

      const elements = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)];
      const first = elements[0];
      const last = elements.at(-1);
      if (!first || !last) {
        return;
      }

      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [closeAndRestoreFocus, open]);

  if (!open) {
    return null;
  }

  function handleLocaleChange(localeValue: string) {
    setLocale(localeValue as Locale);
  }

  return createPortal(
    <div className="mobile-navigation__backdrop">
      <div
        aria-labelledby={titleId}
        aria-modal="true"
        className="mobile-navigation"
        ref={panelRef}
        role="dialog"
      >
        <div className="mobile-navigation__heading">
          <span className="eyebrow" id={titleId}>
            {t.navigation.mobileLabel}
          </span>
          <button
            aria-label={t.navigation.closeMenu}
            className="icon-button"
            onClick={closeAndRestoreFocus}
            type="button"
          >
            <span aria-hidden="true">×</span>
          </button>
        </div>

        <nav aria-label={t.navigation.mobileLabel}>
          <ul className="mobile-navigation__links">
            {items.map((item) => (
              <li key={`${item.label}-${item.href}`}>
                {item.external ? (
                  <a href={item.href}>{item.label}</a>
                ) : (
                  <Link onClick={closeAndRestoreFocus} to={item.href}>
                    {item.label}
                  </Link>
                )}
              </li>
            ))}
          </ul>
        </nav>

        <div className="mobile-navigation__actions">
          {showSignIn ? (
            <Link
              className="button button--quiet"
              onClick={closeAndRestoreFocus}
              to="/auth"
            >
              {status === "authenticated"
                ? session?.user.email ?? t.auth.account
                : t.navigation.signIn}
            </Link>
          ) : null}
          <Link
            className="button button--primary"
            onClick={closeAndRestoreFocus}
            to="/workspace"
          >
            {t.navigation.analyze}
          </Link>
          <CreatorBadge />
        </div>

        <label className="language-control">
          <span>{t.navigation.language}</span>
          <select
            aria-label={t.navigation.language}
            onChange={(event) => handleLocaleChange(event.target.value)}
            value={locale}
          >
            <option value="en">{t.navigation.languageEnglish}</option>
            <option value="zh-CN">{t.navigation.languageChinese}</option>
          </select>
        </label>
      </div>
    </div>,
    document.body,
  );
}
