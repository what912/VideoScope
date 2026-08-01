import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";

import { useI18n } from "../../i18n/I18nProvider";
import { useAuth } from "./AuthProvider";
import { buildCurrentCallbackUrl } from "./callback-url";
import "./auth.css";

type CallbackStatus = "working" | "complete" | "failed";

export function AuthCallbackPage() {
  const { t } = useI18n();
  const auth = useAuth();
  const location = useLocation();
  const started = useRef(false);
  const [status, setStatus] = useState<CallbackStatus>("working");

  useEffect(() => {
    if (started.current) {
      return;
    }
    started.current = true;
    const callbackUrl = buildCurrentCallbackUrl(
      location.search,
      location.hash,
    );
    void auth.completeCallback(callbackUrl).then((completed) => {
      setStatus(completed ? "complete" : "failed");
    });
  }, [auth, location.hash, location.search]);

  return (
    <section className="auth-page" aria-labelledby="auth-callback-title">
      <div className="auth-panel">
        <p className="eyebrow">{t.auth.callbackEyebrow}</p>
        <h1 id="auth-callback-title">
          {status === "complete"
            ? t.auth.callbackComplete
            : status === "failed"
              ? t.auth.callbackFailed
              : t.auth.callbackWorking}
        </h1>
        <p>
          {status === "complete"
            ? t.auth.callbackCompleteDescription
            : status === "failed"
              ? t.auth.errors.callback
              : t.auth.callbackWorkingDescription}
        </p>
        <Link className="button button--primary" to="/workspace">
          {t.auth.continueToAnalysis}
        </Link>
      </div>
    </section>
  );
}
