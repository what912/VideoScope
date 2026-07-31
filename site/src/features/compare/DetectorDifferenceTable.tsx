import { useI18n } from "../../i18n/I18nProvider";
import type { DetectorDifference } from "./comparison";

interface DetectorDifferenceTableProps {
  differences: DetectorDifference[];
  selectedDetectorId?: string;
  onSelect(detectorId: string): void;
}

function formatDuration(value: number | null, unknown: string) {
  return value === null ? unknown : `${value.toFixed(2)} s`;
}

export function DetectorDifferenceTable({
  differences,
  selectedDetectorId,
  onSelect,
}: DetectorDifferenceTableProps) {
  const { t } = useI18n();
  return (
    <section className="comparison-differences">
      <div className="comparison-section-heading">
        <div>
          <p className="eyebrow">{t.compare.observation}</p>
          <h2>{t.compare.detectorDifferences}</h2>
        </div>
        <p>{t.compare.neutralSummary}</p>
      </div>
      <div className="comparison-table-scroll">
        <table aria-label={t.compare.detectorDifferences}>
          <thead>
            <tr>
              <th scope="col">{t.compare.detector}</th>
              <th scope="col">{t.compare.eventsA}</th>
              <th scope="col">{t.compare.durationA}</th>
              <th scope="col">{t.compare.eventsB}</th>
              <th scope="col">{t.compare.durationB}</th>
              <th scope="col">{t.compare.observation}</th>
            </tr>
          </thead>
          <tbody>
            {differences.map((difference) => (
              <tr
                data-selected={
                  difference.detectorId === selectedDetectorId || undefined
                }
                key={difference.detectorId}
              >
                <th scope="row">
                  <button
                    aria-label={`${t.compare.viewEvidence} ${difference.detectorId}`}
                    aria-pressed={
                      difference.detectorId === selectedDetectorId
                    }
                    className="text-button"
                    onClick={() => onSelect(difference.detectorId)}
                    type="button"
                  >
                    {difference.detectorId}
                  </button>
                  {difference.optionalDemo ? (
                    <span
                      className="comparison-table__badge"
                      data-kind="optional"
                    >
                      {t.compare.optionalDemo}
                    </span>
                  ) : null}
                </th>
                <td className="numeric">
                  {difference.aEventCount ?? t.compare.unknown}
                </td>
                <td className="numeric">
                  {formatDuration(
                    difference.aDurationSeconds,
                    t.compare.unknown,
                  )}
                </td>
                <td className="numeric">
                  {difference.bEventCount ?? t.compare.unknown}
                </td>
                <td className="numeric">
                  {formatDuration(
                    difference.bDurationSeconds,
                    t.compare.unknown,
                  )}
                </td>
                <td>{t.compare.observations[difference.observation]}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
