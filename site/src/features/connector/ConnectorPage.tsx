import {
  type FormEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import {
  connectorInstall,
  type ConnectorInstall,
} from "../../config/connector-install";
import { useI18n } from "../../i18n/I18nProvider";
import {
  connectorClient,
  type ConnectorProvider,
  type ConnectorStatus,
} from "../../services/connector/connector-client";
import "./connector.css";

type State = "checking" | "offline" | "online" | "pairing" | "paired" | "error";
type CopyTarget = "install" | "start" | null;

const modes = [
  { id: "publish", symbol: "A", key: "publish" },
  { id: "privacy", symbol: "D", key: "privacy" },
  { id: "rescue", symbol: "B", key: "rescue" },
  { id: "content", symbol: "C", key: "content" },
  { id: "content", symbol: "AI", key: "advanced" },
] as const;

function platformKey(): "windows" | "macos" | "linux" | "other" {
  const platform = `${navigator.platform ?? ""} ${navigator.userAgent}`.toLowerCase();
  if (platform.includes("win")) return "windows";
  if (platform.includes("mac")) return "macos";
  if (platform.includes("linux")) return "linux";
  return "other";
}

async function copyToClipboard(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

interface ConnectorPageProps {
  install?: ConnectorInstall;
}

export function ConnectorPage({ install = connectorInstall }: ConnectorPageProps) {
  const { t } = useI18n();
  const [state, setState] = useState<State>("checking");
  const [status, setStatus] = useState<ConnectorStatus | null>(null);
  const [providers, setProviders] = useState<ConnectorProvider[]>([]);
  const [pairingCode, setPairingCode] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [copied, setCopied] = useState<CopyTarget>(null);
  const pairingInput = useRef<HTMLInputElement>(null);
  const platform = platformKey();

  const refresh = useCallback(async (signal?: AbortSignal, quiet = false) => {
    if (!quiet) setState("checking");
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
          // An expired browser session returns to explicit local pairing.
        }
      }
      setState("online");
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setStatus(null);
      setState("offline");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refresh(controller.signal);
    return () => controller.abort();
  }, [refresh]);

  useEffect(() => {
    if (state !== "offline") return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh(undefined, true);
    }, 2_000);
    return () => window.clearInterval(timer);
  }, [refresh, state]);

  async function pair(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setMessage(null);
    setState("pairing");
    try {
      await connectorClient.pair(pairingCode.replace(/\s/g, ""));
      setProviders(await connectorClient.providers());
      setPairingCode("");
      setState("paired");
    } catch {
      setState("error");
      setMessage(t.connector.pairingFailed);
      window.requestAnimationFrame(() => pairingInput.current?.focus());
    }
  }

  async function copy(target: Exclude<CopyTarget, null>, value: string) {
    try {
      await copyToClipboard(value);
      setCopied(target);
      setMessage(null);
    } catch {
      setMessage(t.connector.copyFailed);
    }
  }

  const ffmpegReady = status?.ffmpeg_available !== false && status?.ffprobe_available !== false;

  return (
    <section className="connector-page" aria-labelledby="connector-title">
      <header className="connector-hero">
        <div>
          <p className="eyebrow">{t.connector.eyebrow}</p>
          <h1 id="connector-title">{t.connector.title}</h1>
          <p>{t.connector.description}</p>
          <div className="connector-platform" aria-label={t.connector.detectedSystem}>
            <span>{t.connector.detectedSystem}</span>
            <strong>{t.connector.platforms[platform]}</strong>
          </div>
        </div>
        <div
          className={`connector-signal connector-signal--${state}`}
          role="status"
          aria-live="polite"
        >
          <span aria-hidden="true" />
          <strong>{t.connector.states[state]}</strong>
          {status ? <small>v{status.version}</small> : <small>{t.connector.autoChecking}</small>}
        </div>
      </header>

      {state === "checking" ? (
        <div className="connector-checking" role="status">
          <span className="connector-checking__scan" aria-hidden="true" />
          <div><strong>{t.connector.checkingTitle}</strong><p>{t.connector.checkingDescription}</p></div>
        </div>
      ) : null}

      {state === "offline" ? (
        <section className="connector-onboarding" aria-labelledby="connector-setup-title">
          <div className="connector-onboarding__intro">
            <div>
              <p className="eyebrow">{t.connector.firstTimeEyebrow}</p>
              <h2 id="connector-setup-title">{t.connector.firstTimeTitle}</h2>
              <p>{t.connector.firstTimeDescription}</p>
            </div>
            <span>{t.connector.estimatedTime}</span>
          </div>

          <ol className="connector-setup">
            <li className="connector-step connector-step--active">
              <span>01</span>
              <div>
                <p className="connector-step__label">{t.connector.stepLabels.download}</p>
                <h3>{t.connector.installTitle}</h3>
                <p>{t.connector.installDescription}</p>
                <div className="connector-actions">
                  <a
                    className="button button--primary"
                    href={install.windowsInstallerUrl}
                    rel="noreferrer"
                  >
                    {install.hasDirectWindowsInstaller
                      ? t.connector.downloadWindows
                      : t.connector.downloadFallback}
                  </a>
                  <a className="button button--quiet" href={install.releasesUrl} rel="noreferrer">
                    {t.connector.allDownloads}
                  </a>
                </div>
                {!install.hasDirectWindowsInstaller ? (
                  <p className="connector-notice">{t.connector.installerPreviewNotice}</p>
                ) : (
                  <>
                    <p className="connector-notice">
                      SHA-256: <code>{install.windowsInstallerSha256}</code>
                    </p>
                    <p className="connector-notice">{t.connector.windowsWarningDescription}</p>
                  </>
                )}
              </div>
            </li>

            <li className="connector-step">
              <span>02</span>
              <div>
                <p className="connector-step__label">{t.connector.stepLabels.install}</p>
                <h3>{t.connector.runInstallerTitle}</h3>
                <p>{t.connector.runInstallerDescription}</p>
                <ul className="connector-checklist">
                  <li>{t.connector.noPythonNeeded}</li>
                  <li>{t.connector.ffmpegCheck}</li>
                  <li>{t.connector.localOnlyInstall}</li>
                </ul>
              </div>
            </li>

            <li className="connector-step">
              <span>03</span>
              <div>
                <p className="connector-step__label">{t.connector.stepLabels.start}</p>
                <h3>{t.connector.startTitle}</h3>
                <p>{t.connector.startDescription}</p>
                <a className="button button--quiet" href={install.startProtocolUrl}>
                  {t.connector.startButton}
                </a>
              </div>
            </li>

            <li className="connector-step connector-step--watching">
              <span>04</span>
              <div>
                <p className="connector-step__label">{t.connector.stepLabels.detect}</p>
                <h3>{t.connector.retryTitle}</h3>
                <p>{t.connector.autoRetryDescription}</p>
                <button className="button button--primary" onClick={() => void refresh()} type="button">
                  {t.connector.retry}
                </button>
              </div>
              <b className="connector-watching" aria-hidden="true"><i /></b>
            </li>

            <li className="connector-step connector-step--locked">
              <span>05</span>
              <div>
                <p className="connector-step__label">{t.connector.stepLabels.pair}</p>
                <h3>{t.connector.futurePairTitle}</h3>
                <p>{t.connector.futurePairDescription}</p>
              </div>
            </li>
          </ol>

          <details className="connector-help">
            <summary>{t.connector.needHelp}</summary>
            <div className="connector-help__grid">
              <div><strong>{t.connector.windowsWarningTitle}</strong><p>{t.connector.windowsWarningDescription}</p></div>
              <div><strong>{t.connector.notDetectedTitle}</strong><p>{t.connector.notDetectedDescription}</p></div>
              <div><strong>{t.connector.ffmpegHelpTitle}</strong><p>{t.connector.ffmpegHelpDescription}</p></div>
            </div>
            <div className="connector-manual">
              <div><strong>{t.connector.manualTitle}</strong><p>{t.connector.manualDescription}</p></div>
              <pre><code>{install.legacyInstallCommands}</code></pre>
              <button
                className="button button--quiet"
                onClick={() => void copy("install", install.legacyInstallCommands)}
                type="button"
              >
                {copied === "install" ? t.connector.copied : t.connector.copyCommands}
              </button>
            </div>
          </details>
        </section>
      ) : null}

      {(state === "online" || state === "pairing" || state === "error") ? (
        <section className="connector-pair-stage" aria-labelledby="connector-pair-title">
          <div className="connector-progress" aria-label={t.connector.progressLabel}>
            <span className="is-complete">1 {t.connector.progressInstalled}</span>
            <span className="is-complete">2 {t.connector.progressRunning}</span>
            <span className="is-current" aria-current="step">3 {t.connector.progressPair}</span>
            <span>4 {t.connector.progressAnalyze}</span>
          </div>
          <form className="connector-pair" onSubmit={pair}>
            <div>
              <p className="eyebrow">{t.connector.pairEyebrow}</p>
              <h2 id="connector-pair-title">{t.connector.pairTitle}</h2>
              <p>{t.connector.pairDescription}</p>
            </div>
            <div className="connector-code-example" aria-label={t.connector.codeExampleLabel}>
              <span>VideoScope Local Connector pairing code:</span>
              <strong>Ab1_cdEF2345</strong>
              <em>{t.connector.copyOnlyCode}</em>
            </div>
            <label htmlFor="connector-pairing-code">{t.connector.pairingCode}</label>
            <div className="connector-pair__controls">
              <input
                ref={pairingInput}
                id="connector-pairing-code"
                aria-describedby="connector-pairing-help"
                autoComplete="off"
                onChange={(event) => setPairingCode(event.target.value)}
                placeholder="Ab1_cdEF2345"
                required
                spellCheck={false}
                value={pairingCode}
              />
              <button
                className="button button--primary"
                disabled={state === "pairing" || pairingCode.trim().length < 6}
                type="submit"
              >
                {state === "pairing" ? t.connector.pairing : t.connector.pair}
              </button>
            </div>
            <p id="connector-pairing-help" className="connector-pair__help">{t.connector.pairHelp}</p>
            {message ? <p className="auth-panel__error" role="alert">{message}</p> : null}
          </form>
          <details className="connector-help connector-help--compact">
            <summary>{t.connector.cannotFindCode}</summary>
            <p>{t.connector.cannotFindCodeDescription}</p>
            <div className="connector-command-row">
              <code>{install.legacyStartCommand}</code>
              <button
                className="button button--quiet"
                onClick={() => void copy("start", install.legacyStartCommand)}
                type="button"
              >
                {copied === "start" ? t.connector.copied : t.connector.copyCommand}
              </button>
            </div>
          </details>
        </section>
      ) : null}

      {state === "paired" ? (
        <>
          {!ffmpegReady ? (
            <div className="connector-degraded" role="alert">
              <strong>{t.connector.degradedTitle}</strong>
              <p>{t.connector.degradedDescription}</p>
              <a className="button button--quiet" href={connectorClient.origin}>{t.connector.openSettings}</a>
            </div>
          ) : null}
          <section className="connector-ready" aria-labelledby="connector-ready-title">
            <div>
              <p className="eyebrow">{t.connector.readyEyebrow}</p>
              <h2 id="connector-ready-title">{t.connector.readyTitle}</h2>
              <p>{t.connector.readyDescription}</p>
            </div>
            <a className="button button--primary" href={connectorClient.workbenchUrl("analyze")}>
              {t.connector.firstAnalysis}
            </a>
          </section>
          <ol className="connector-first-run">
            <li><span>1</span><div><strong>{t.connector.firstRun.chooseTitle}</strong><p>{t.connector.firstRun.chooseDescription}</p></div></li>
            <li><span>2</span><div><strong>{t.connector.firstRun.runTitle}</strong><p>{t.connector.firstRun.runDescription}</p></div></li>
            <li><span>3</span><div><strong>{t.connector.firstRun.reviewTitle}</strong><p>{t.connector.firstRun.reviewDescription}</p></div></li>
          </ol>
          <div className="connector-trust">
            <div><strong>{t.connector.localCompute}</strong><p>{t.connector.localComputeDescription}</p></div>
            <div><strong>{t.connector.byok}</strong><p>{t.connector.byokDescription}</p></div>
            <div><strong>{t.connector.zeroCost}</strong><p>{t.connector.zeroCostDescription}</p></div>
          </div>
          <div className="connector-section-heading"><p className="eyebrow">{t.connector.moreModesEyebrow}</p><h2>{t.connector.moreModesTitle}</h2><p>{t.connector.moreModesDescription}</p></div>
          <div className="connector-modes" aria-label={t.connector.modesLabel}>
            {modes.map((mode) => {
              const copy = t.connector.modes[mode.key];
              return <a className="connector-mode" href={connectorClient.workbenchUrl(mode.id)} key={mode.key}><span>{mode.symbol}</span><div><h3>{copy.title}</h3><p>{copy.description}</p></div><b aria-hidden="true">↗</b></a>;
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
