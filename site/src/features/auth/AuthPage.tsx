import { type ChangeEvent, type FormEvent, useState } from "react";
import { Link } from "react-router";

import { useI18n } from "../../i18n/I18nProvider";
import { useOnlineStatus } from "../../hooks/useOnlineStatus";
import { isLocalDeviceAuthClient } from "../../services/auth";
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
  const [displayName, setDisplayName] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [confirmPassphrase, setConfirmPassphrase] = useState("");
  const [localWorking, setLocalWorking] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const [localNotice, setLocalNotice] = useState<string | null>(null);
  const localClient = isLocalDeviceAuthClient(auth.client)
    ? auth.client
    : null;
  const message = errorMessage(auth.error, t.auth.errors);
  const authenticated = auth.status === "authenticated" && auth.session;
  const analyzeLabel = authenticated
    ? t.auth.analyze
    : t.auth.analyzeAnonymously;

  async function handleMagicLink(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await auth.signInWithMagicLink(email.trim(), buildAuthCallbackUrl());
  }

  async function handleLocalAccount(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!localClient) return;
    setLocalError(null);
    setLocalNotice(null);
    if (!localClient.hasAccount() && passphrase !== confirmPassphrase) {
      setLocalError(t.auth.localAccount.passphrasesDiffer);
      return;
    }
    setLocalWorking(true);
    try {
      if (localClient.hasAccount()) {
        await localClient.signIn(passphrase);
      } else {
        await localClient.register(displayName, passphrase);
      }
      setPassphrase("");
      setConfirmPassphrase("");
    } catch {
      setLocalError(t.auth.localAccount.failed);
    } finally {
      setLocalWorking(false);
    }
  }

  function exportLocalBackup() {
    if (!localClient) return;
    try {
      const blob = new Blob([localClient.exportEncryptedBackup()], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "videoscope-local-account.json";
      link.click();
      URL.revokeObjectURL(url);
      setLocalNotice(t.auth.localAccount.exported);
    } catch {
      setLocalError(t.auth.localAccount.failed);
    }
  }

  async function importLocalBackup(event: ChangeEvent<HTMLInputElement>) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file || !localClient) return;
    try {
      localClient.importEncryptedBackup(await file.text());
      setLocalNotice(t.auth.localAccount.imported);
      setLocalError(null);
    } catch {
      setLocalError(t.auth.localAccount.invalidBackup);
    }
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

        {!localClient && !online && auth.status !== "unavailable" ? (
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

        {localClient &&
        (auth.status === "anonymous" || auth.status === "error") ? (
          <form className="auth-form" onSubmit={handleLocalAccount}>
            <div className="auth-panel__notice" role="status">
              <strong>{t.auth.localAccount.title}</strong>
              <p>{t.auth.localAccount.description}</p>
            </div>
            {!localClient.hasAccount() ? (
              <>
                <label htmlFor="local-display-name">
                  {t.auth.localAccount.displayName}
                </label>
                <input
                  autoComplete="nickname"
                  id="local-display-name"
                  maxLength={80}
                  minLength={2}
                  onChange={(event) => setDisplayName(event.target.value)}
                  required
                  value={displayName}
                />
              </>
            ) : null}
            <label htmlFor="local-passphrase">
              {t.auth.localAccount.passphrase}
            </label>
                <input
                  autoComplete={localClient.hasAccount() ? "current-password" : "new-password"}
                  id="local-passphrase"
                  maxLength={256}
                  minLength={10}
              onChange={(event) => setPassphrase(event.target.value)}
              required
              type="password"
              value={passphrase}
            />
            {!localClient.hasAccount() ? (
              <>
                <label htmlFor="local-passphrase-confirm">
                  {t.auth.localAccount.confirmPassphrase}
                </label>
                <input
                  autoComplete="new-password"
                  id="local-passphrase-confirm"
                  maxLength={256}
                  minLength={10}
                  onChange={(event) => setConfirmPassphrase(event.target.value)}
                  required
                  type="password"
                  value={confirmPassphrase}
                />
              </>
            ) : null}
            <button
              className="button button--primary"
              disabled={localWorking}
              type="submit"
            >
              {localClient.hasAccount()
                ? t.auth.localAccount.unlock
                : t.auth.localAccount.create}
            </button>
            <label className="button button--quiet" htmlFor="local-backup-import">
              {t.auth.localAccount.import}
            </label>
            <input
              accept="application/json,.json"
              className="visually-hidden"
              id="local-backup-import"
              onChange={(event) => void importLocalBackup(event)}
              type="file"
            />
          </form>
        ) : null}

        {!localClient &&
        (auth.status === "anonymous" || auth.status === "error") && (
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

        {localClient && authenticated ? (
          <div className="auth-panel__local-actions">
            <button
              className="button button--quiet"
              onClick={exportLocalBackup}
              type="button"
            >
              {t.auth.localAccount.export}
            </button>
            <button
              className="button button--quiet"
              onClick={() => {
                if (window.confirm(t.auth.localAccount.deleteConfirm)) {
                  localClient.deleteAccount();
                }
              }}
              type="button"
            >
              {t.auth.localAccount.delete}
            </button>
          </div>
        ) : null}

        {localNotice ? (
          <p className="auth-panel__success" role="status">{localNotice}</p>
        ) : null}
        {localError ? (
          <p className="auth-panel__error" role="alert">{localError}</p>
        ) : null}

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
