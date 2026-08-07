import { useI18n } from "../../i18n/I18nProvider";
import { PUBLIC_RECOVERY_STATE_IDS } from "./recovery-states";
import { getStaticCopy, type CapabilityId } from "./static-copy";
import "./static.css";

const capabilities = [
  { id: "browser_cpu_detectors", browser: true },
  { id: "local_reports", browser: true },
  { id: "ffmpeg_probe", browser: false },
  { id: "publish_ready", browser: false },
  { id: "safe_sharing", browser: false },
  { id: "video_rescue", browser: false },
  { id: "useful_content", browser: false },
  { id: "advanced_ai", browser: false },
  { id: "benchmark", browser: false },
  { id: "ai_providers", browser: false },
  { id: "ocr", browser: false },
  { id: "web_api", browser: false },
] as const satisfies ReadonlyArray<{ id: CapabilityId; browser: boolean }>;

export function DocsPage() {
  const { locale } = useI18n();
  const copy = getStaticCopy(locale).docs;

  return (
    <article className="static-page static-page--docs" aria-labelledby="docs-title">
      <header className="static-page__header">
        <p className="eyebrow">{copy.eyebrow}</p>
        <h1 id="docs-title">{copy.title}</h1>
        <p>{copy.introduction}</p>
      </header>

      <div className="static-page__boundary-grid">
        <section>
          <h2>{copy.browserPreview}</h2>
          <p>{copy.browserPreviewBody}</p>
        </section>
        <section>
          <h2>{copy.privacyBoundary}</h2>
          <p>{copy.privacyBoundaryBody}</p>
        </section>
      </div>

      <div className="static-table-wrap">
        <table aria-label={copy.matrixLabel}>
          <thead>
            <tr>
              <th scope="col">{copy.capability}</th>
              <th scope="col">{copy.browser}</th>
              <th scope="col">{copy.desktop}</th>
            </tr>
          </thead>
          <tbody>
            {capabilities.map((capability) => (
              <tr key={capability.id}>
                <th scope="row">{copy.capabilityNames[capability.id]}</th>
                <td>{capability.browser ? copy.available : copy.desktopOnly}</td>
                <td>{copy.available}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <section className="recovery-guide" aria-labelledby="troubleshooting-title">
        <div className="static-page__section-heading">
          <p className="eyebrow">RECOVERY MAP</p>
          <h2 id="troubleshooting-title">{copy.troubleshooting}</h2>
        </div>
        <ol>
          {PUBLIC_RECOVERY_STATE_IDS.map((stateId) => {
            const state = copy.recovery[stateId];
            return (
              <li data-testid={`recovery-${stateId}`} key={stateId}>
                <span aria-hidden="true" className="recovery-guide__signal">
                  !
                </span>
                <div>
                  <h3>{state.title}</h3>
                  <p>{state.action}</p>
                </div>
              </li>
            );
          })}
        </ol>
      </section>
    </article>
  );
}
