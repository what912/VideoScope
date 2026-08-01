import { useState } from "react";

import { useI18n } from "../../i18n/I18nProvider";
import { detectorProtocolExample, repositoryUrl } from "./home-data";

export function OpenSourceSection() {
  const { t } = useI18n();
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">(
    "idle",
  );
  const copyExample = async () => {
    try {
      await navigator.clipboard.writeText(detectorProtocolExample);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };
  return (
    <section className="home-section open-source-section">
      <div>
        <p className="eyebrow">{t.home.openSource.eyebrow}</p>
        <h2>{t.home.openSource.title}</h2>
        <p>{t.home.openSource.description}</p>
        <p>{t.home.openSource.benchmark}</p>
        <a className="text-link" href={repositoryUrl}>
          GitHub · what912
        </a>
      </div>
      <figure>
        <figcaption>
          <span>{t.home.openSource.protocol}</span>
          <button className="button button--quiet" onClick={() => void copyExample()} type="button">
            {t.home.openSource.copy}
          </button>
        </figcaption>
        <pre>
          <code>{detectorProtocolExample}</code>
        </pre>
        {copyState !== "idle" ? (
          <p role="status">
            {copyState === "copied"
              ? t.home.openSource.copied
              : t.home.openSource.copyFailed}
          </p>
        ) : null}
      </figure>
    </section>
  );
}
