import { contentText, type ContentLocale } from "../contentI18n";
import type { ContentMap } from "../types";

function percentage(value: number, duration: number): string {
  return `${Math.max(0, Math.min(100, (value / duration) * 100))}%`;
}

export function ContentMapTimeline({
  locale,
  contentMap,
  onSeek,
}: {
  locale: ContentLocale;
  contentMap: ContentMap;
  onSeek(seconds: number): void;
}): React.JSX.Element {
  const hasProposal = contentMap.segments.some(
    (segment) => segment.selection_eligibility === "eligible",
  );
  return (
    <section className="content-panel content-map" aria-labelledby="content-map-title">
      <div className="content-section-heading">
        <div>
          <p className="eyebrow">OBSERVATORY / SOURCE</p>
          <h2 id="content-map-title">{contentText("map", locale)}</h2>
        </div>
        <p>{contentText("mapHelp", locale)}</p>
      </div>
      <div className="content-lanes" aria-label={contentText("map", locale)}>
        {contentMap.segments.map((segment) => (
          <button
            type="button"
            key={segment.id}
            className={`content-segment eligibility-${segment.selection_eligibility}`}
            style={{
              left: percentage(segment.source_range.start_seconds, contentMap.duration_seconds),
              width: percentage(
                segment.source_range.end_seconds - segment.source_range.start_seconds,
                contentMap.duration_seconds,
              ),
            }}
            title={`${segment.source_range.start_seconds.toFixed(2)}–${segment.source_range.end_seconds.toFixed(2)} s · ${segment.reason}`}
            aria-label={`${segment.reason}, ${segment.source_range.start_seconds.toFixed(2)} to ${segment.source_range.end_seconds.toFixed(2)} seconds`}
            onClick={() => onSeek(segment.source_range.start_seconds)}
          >
            <span>{segment.signals.map((signal) => signal.signal_type).join(" · ")}</span>
          </button>
        ))}
        {contentMap.user_ranges.map((range) => (
          <button
            type="button"
            key={range.id}
            className={`content-user-range kind-${range.kind}`}
            style={{
              left: percentage(range.source_range.start_seconds, contentMap.duration_seconds),
              width: percentage(
                range.source_range.end_seconds - range.source_range.start_seconds,
                contentMap.duration_seconds,
              ),
            }}
            onClick={() => onSeek(range.source_range.start_seconds)}
            aria-label={`${range.kind}, ${range.source_range.start_seconds} to ${range.source_range.end_seconds} seconds`}
          />
        ))}
      </div>
      <div className="content-ruler" aria-hidden="true">
        <span>00:00</span>
        <span>{(contentMap.duration_seconds / 2).toFixed(1)}s</span>
        <span>{contentMap.duration_seconds.toFixed(1)}s</span>
      </div>
      {!hasProposal && <p className="content-empty-note">{contentText("noProposal", locale)}</p>}
      {contentMap.provider_executions.some((item) => item.status !== "ok") && (
        <ul className="content-provider-warnings">
          {contentMap.provider_executions
            .filter((item) => item.status !== "ok")
            .map((item) => (
              <li key={item.provider_id}>
                {item.provider_id}: {item.status} — {item.warning ?? "No evidence available"}
              </li>
            ))}
        </ul>
      )}
    </section>
  );
}
