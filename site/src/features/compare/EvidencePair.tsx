import { useI18n } from "../../i18n/I18nProvider";
import type { BrowserReport } from "../../types/report";

interface EvidencePairProps {
  detectorId?: string;
  findingIdA?: string;
  findingIdB?: string;
  a?: BrowserReport;
  b?: BrowserReport;
  onSeekA(seconds: number): void;
  onSeekB(seconds: number): void;
}

export function EvidencePair({
  detectorId,
  findingIdA,
  findingIdB,
  a,
  b,
  onSeekA,
  onSeekB,
}: EvidencePairProps) {
  const { t } = useI18n();
  if (!detectorId) return null;
  const evidence = [
    a?.findings
      .find((finding) => finding.id === findingIdA)
      ?.evidence.find((item) => item.thumbnail),
    b?.findings
      .find((finding) => finding.id === findingIdB)
      ?.evidence.find((item) => item.thumbnail),
  ];
  if (!evidence.some((item) => item?.thumbnail)) {
    return (
      <section aria-label={t.compare.evidencePair} className="evidence-pair">
        <h2>{t.compare.evidencePair}</h2>
        <p>{t.compare.noEvidence}</p>
      </section>
    );
  }

  return (
    <section aria-label={t.compare.evidencePair} className="evidence-pair">
      <h2>{t.compare.evidencePair}</h2>
      <div>
        {evidence.map((item, index) => (
          <figure key={index}>
            <figcaption>
              {index === 0 ? t.compare.evidenceA : t.compare.evidenceB}
            </figcaption>
            {item?.thumbnail ? (
              <button
                className="evidence-pair__image"
                onClick={() =>
                  (index === 0 ? onSeekA : onSeekB)(item.timestamp_seconds)
                }
                type="button"
              >
                <img
                  alt={item.description}
                  height={item.thumbnail.height}
                  loading="lazy"
                  src={item.thumbnail.src}
                  width={item.thumbnail.width}
                />
                <span className="numeric">
                  {item.timestamp_seconds.toFixed(2)} s
                </span>
              </button>
            ) : (
              <p>{t.compare.noEvidence}</p>
            )}
          </figure>
        ))}
      </div>
    </section>
  );
}
