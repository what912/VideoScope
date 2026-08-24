import { useState } from "react";

import { useI18n } from "../../i18n/I18nProvider";
import { detectorProtocolExample, repositoryUrl } from "./home-data";
import { legacyHomeCopy } from "./legacy-home-copy";

export function OpenSourceSection() {
  const { locale } = useI18n();
  const copy = legacyHomeCopy[locale];
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
        <p className="eyebrow">{copy.openSource.eyebrow}</p>
        <h2>{copy.openSource.title}</h2>
        <p>{copy.openSource.description}</p>
        <p>{copy.openSource.benchmark}</p>
        <a className="text-link" href={repositoryUrl}>
          GitHub · what912
        </a>
      </div>
      <figure>
        <figcaption>
          <span>{copy.openSource.protocol}</span>
          <button className="button button--quiet" onClick={() => void copyExample()} type="button">
            {copy.openSource.copy}
          </button>
        </figcaption>
        <pre>
          <code>{detectorProtocolExample}</code>
        </pre>
        {copyState !== "idle" ? (
          <p role="status">
            {copyState === "copied"
              ? copy.openSource.copied
              : copy.openSource.copyFailed}
          </p>
        ) : null}
      </figure>
    </section>
  );
}
