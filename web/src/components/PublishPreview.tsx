export interface PreviewCopy {
  heading: string;
  description: string;
  source: string;
  output: string;
  unavailable: string;
}

interface Props {
  sourceUrl: string | null;
  previewUrl: string;
  copy: PreviewCopy;
}

export function PublishPreview({
  sourceUrl,
  previewUrl,
  copy,
}: Props): React.JSX.Element {
  return (
    <section className="publish-preview" aria-labelledby="preview-heading">
      <div className="publish-section-heading">
        <div>
          <p className="step-label">04 / {copy.heading}</p>
          <h2 id="preview-heading">{copy.heading}</h2>
        </div>
        <p>{copy.description}</p>
      </div>
      <div className="preview-compare">
        <figure>
          <figcaption>{copy.source}</figcaption>
          {sourceUrl ? (
            <video
              aria-label={copy.source}
              src={sourceUrl}
              controls
              preload="metadata"
            />
          ) : (
            <div className="preview-unavailable">{copy.unavailable}</div>
          )}
        </figure>
        <figure>
          <figcaption>{copy.output}</figcaption>
          <video
            aria-label={copy.output}
            src={previewUrl}
            controls
            preload="metadata"
          />
        </figure>
      </div>
    </section>
  );
}
