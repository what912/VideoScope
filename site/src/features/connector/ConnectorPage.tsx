import { type FormEvent, useEffect, useState } from "react";

import { useI18n } from "../../i18n/I18nProvider";
import {
  connectorClient,
  type ConnectorProvider,
  type ConnectorStatus,
} from "../../services/connector/connector-client";
import "./connector.css";

type State = "checking" | "offline" | "online" | "paired" | "error";

const modes = [
  { id: "publish", symbol: "A", key: "publish" },
  { id: "privacy", symbol: "D", key: "privacy" },
  { id: "rescue", symbol: "B", key: "rescue" },
  { id: "content", symbol: "C", key: "content" },
  { id: "content", symbol: "AI", key: "advanced" },
] as const;

export function ConnectorPage() {
  const { t } = useI18n();
  const [state, setState] = useState<State>("checking");
  const [status, setStatus] = useState<ConnectorStatus | null>(null);
  const [providers, setProviders] = useState<ConnectorProvider[]>([]);
  const [pairingCode, setPairingCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  async function refresh(signal?: AbortSignal) {
    setState("checking");
    setMessage(null);
    try {
      const nextStatus = await connectorClient.status(signal);
      setStatus(nextStatus);
      if (connectorClient.isPaired()) {
        try {
          setProviders(await connectorClient.providers());
          setState("paired");
          return;
        } catch {
          // Expired pairing falls back to the explicit pairing form.
        }
      }
      setState("online");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setState("offline");
    }
  }

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, []);

  async function pair(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    try {
      await connectorClient.pair(pairingCode.trim());
      setProviders(await connectorClient.providers());
      setPairingCode("");
      setState("paired");
    } catch {
      setState("error");
      setMessage(t.connector.pairingFailed);
    }
  }

  return (
    <section className="connector-page" aria-labelledby="connector-title">
      <header className="connector-hero">
        <div>
          <p className="eyebrow">{t.connector.eyebrow}</p>
          <h1 id="connector-title">{t.connector.title}</h1>
          <p>{t.connector.description}</p>
        </div>
        <div className={`connector-signal connector-signal--${state}`} role="status">
          <span aria-hidden="true" />
          <strong>{t.connector.states[state]}</strong>
          {status ? <small>v{status.version}</small> : null}
        </div>
      </header>

      {state === "offline" ? (
        <div className="connector-setup">
          <div className="connector-step"><span>01</span><div><h2>{t.connector.installTitle}</h2><p>{t.connector.installDescription}</p><a className="button button--quiet" href="https://github.com/what912/VideoScope/releases/latest">{t.connector.download}</a></div></div>
          <div className="connector-step"><span>02</span><div><h2>{t.connector.startTitle}</h2><code>videoscope serve --port 8765</code></div></div>
          <div className="connector-step"><span>03</span><div><h2>{t.connector.retryTitle}</h2><button className="button button--primary" onClick={() => void refresh()} type="button">{t.connector.retry}</button></div></div>
        </div>
      ) : null}

      {(state === "online" || state === "error") ? (
        <form className="connector-pair" onSubmit={pair}>
          <div><p className="eyebrow">{t.connector.pairEyebrow}</p><h2>{t.connector.pairTitle}</h2><p>{t.connector.pairDescription}</p></div>
          <label htmlFor="connector-pairing-code">{t.connector.pairingCode}</label>
          <div className="connector-pair__controls">
            <input id="connector-pairing-code" onChange={(event) => setPairingCode(event.target.value)} required value={pairingCode} />
            <button className="button button--primary" type="submit">{t.connector.pair}</button>
          </div>
          {message ? <p className="auth-panel__error" role="alert">{message}</p> : null}
        </form>
      ) : null}

      {state === "paired" ? (
        <>
          <div className="connector-trust">
            <div><strong>{t.connector.localCompute}</strong><p>{t.connector.localComputeDescription}</p></div>
            <div><strong>{t.connector.byok}</strong><p>{t.connector.byokDescription}</p></div>
            <div><strong>{t.connector.zeroCost}</strong><p>{t.connector.zeroCostDescription}</p></div>
          </div>
          <div className="connector-modes" aria-label={t.connector.modesLabel}>
            {modes.map((mode) => {
              const copy = t.connector.modes[mode.key];
              return <a className="connector-mode" href={connectorClient.workbenchUrl(mode.id)} key={mode.key}><span>{mode.symbol}</span><div><h2>{copy.title}</h2><p>{copy.description}</p></div><b aria-hidden="true">↗</b></a>;
            })}
          </div>
          <section className="connector-providers">
            <div><p className="eyebrow">{t.connector.providersEyebrow}</p><h2>{t.connector.providersTitle}</h2><p>{t.connector.providersDescription}</p></div>
            {providers.length ? <ul>{providers.map((provider) => <li key={provider.profile_id}><strong>{provider.display_name}</strong><span>{provider.model_id}</span><small>{provider.credential_state}</small></li>)}</ul> : <p className="connector-providers__empty">{t.connector.noProviders}</p>}
            <a className="button button--quiet" href={connectorClient.origin}>{t.connector.openSettings}</a>
          </section>
          <button className="button button--quiet" onClick={() => void connectorClient.disconnect().then(() => setState("online"))} type="button">{t.connector.disconnect}</button>
        </>
      ) : null}
    </section>
  );
}
