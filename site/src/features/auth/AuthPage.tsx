import { type FormEvent, useState } from "react";
import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import { useOnlineStatus } from "../../hooks/useOnlineStatus";
import { useAuth, type AuthErrorCode } from "./AuthProvider";
import { buildAuthCallbackUrl } from "./callback-url";
import "./auth.css";

function errorMessage(
  error: AuthErrorCode | null,
  messages: ReturnType<typeof useI18n>["t"]["auth"]["errors"],
) {
  return error ? messages[error] : null;
}

export function AuthPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const online = useOnlineStatus();
  const [email, setEmail] = useState("");
  const message = errorMessage(auth.error, t.auth.errors);
  const authenticated = auth.status === "authenticated" && auth.session;
  const analyzeLabel = authenticated
    ? t.auth.analyze
    : t.auth.analyzeAnonymously;

  async function handleMagicLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await auth.signInWithMagicLink(email.trim(), buildAuthCallbackUrl());
  }

  return (
    <section className="auth-page" aria-labelledby="auth-title">
      <div className="auth-panel">
        <p className="eyebrow">{t.auth.eyebrow}</p>
        <h1 id="auth-title">{t.auth.title}</h1>
        <p className="auth-panel__description">{t.auth.description}</p>

        {auth.status === "loading" ? (
          <p aria-live="polite" className="auth-panel__status">
            {t.auth.loading}
          </p>
        ) : null}

        {auth.status === "unavailable" ? (
          <div className="auth-panel__notice" role="status">
            <strong>{t.auth.unavailableTitle}</strong>
            <p>{t.auth.unavailableDescription}</p>
          </div>
        ) : null}

        {!online && auth.status !== "unavailable" ? (
          <div
            aria-label={t.auth.offlineTitle}
            className="auth-panel__notice"
            role="status"
          >
            <strong>{t.auth.offlineTitle}</strong>
            <p>{t.auth.offlineDescription}</p>
          </div>
        ) : null}

        {authenticated ? (
          <div className="auth-account">
            <span aria-hidden="true" className="auth-account__avatar">
              {auth.session?.user.email?.slice(0, 1).toUpperCase() ?? "V"}
            </span>
            <div>
              <strong>
                {auth.session?.user.displayName ?? t.auth.account}
              </strong>
              {auth.session?.user.email ? (
                <p className="numeric">{auth.session.user.email}</p>
              ) : null}
            </div>
            <button
              className="button button--quiet"
              disabled={auth.working}
              onClick={() => void auth.signOut()}
              type="button"
            >
              {t.auth.signOut}
            </button>
          </div>
        ) : null}

        {(auth.status === "anonymous" || auth.status === "error") && (
          <>
            <p className="auth-panel__anonymous">{t.auth.anonymous}</p>
            <form className="auth-form" onSubmit={handleMagicLink}>
              <label htmlFor="auth-email">{t.auth.email}</label>
              <input
                autoComplete="email"
                id="auth-email"
                onChange={(event) => {
                  auth.clearError();
                  auth.clearMagicLinkNotice();
                  setEmail(event.target.value);
                }}
                required
                type="email"
                value={email}
              />
              <button
                className="button button--primary"
                disabled={auth.working || !online}
                type="submit"
              >
                {t.auth.magicLink}
              </button>
            </form>
            <div className="auth-panel__divider">
              <span>{t.auth.or}</span>
            </div>
            <button
              className="button button--quiet"
              disabled={auth.working || !online}
              onClick={() =>
                void auth.signInWithGitHub(buildAuthCallbackUrl())
              }
              type="button"
            >
              {t.auth.github}
            </button>
          </>
        )}

        {auth.magicLinkSent ? (
          <p aria-live="polite" className="auth-panel__success" role="status">
            {t.auth.magicLinkSent}
          </p>
        ) : null}
        {message ? (
          <p className="auth-panel__error" role="alert">
            {message}
          </p>
        ) : null}

        <div className="auth-panel__local">
          <strong>{t.auth.localTitle}</strong>
          <p>{t.auth.localDescription}</p>
          <Link className="button button--primary" to="/workspace">
            {analyzeLabel}
          </Link>
        </div>
      </div>
    </section>
  );
}
