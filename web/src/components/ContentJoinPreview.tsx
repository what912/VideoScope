import { contentText, type ContentLocale } from "../contentI18n";
import type { ContentJoinPreview as Preview } from "../types";

export function ContentJoinPreview({
  locale,
  previews,
  urlFor,
  onSeek,
}: {
  locale: ContentLocale;
  previews: Preview[];
  urlFor(path: string): string;
  onSeek(seconds: number): void;
}): React.JSX.Element {
  return (
    <section className="content-panel content-previews" aria-labelledby="content-preview-title">
      <div className="content-section-heading">
        <div>
          <p className="eyebrow">PRIVATE / LOOPBACK ONLY</p>
          <h2 id="content-preview-title">{contentText("privatePreview", locale)}</h2>
        </div>
      </div>
      {previews.length === 0 ? (
        <p className="content-empty-note">{contentText("previewUnavailable", locale)}</p>
      ) : (
        <div className="content-preview-grid">
          {previews.map((preview) => {
            const joined = preview.relative_paths.at(-1);
            return (
              <article key={preview.action_id}>
                <h3>{preview.action_id.slice(0, 18)}…</h3>
                {joined && (
                  <video controls preload="metadata" src={urlFor(joined)}>
                    <track kind="captions" />
                  </video>
                )}
                <div className="content-preview-ranges">
                  {preview.context_ranges.map((range, index) => (
                    <button type="button" key={`${range.start_seconds}-${index}`} onClick={() => onSeek(range.start_seconds)}>
                      {range.start_seconds.toFixed(2)}–{range.end_seconds.toFixed(2)}s
                    </button>
                  ))}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </section>
  );
}
