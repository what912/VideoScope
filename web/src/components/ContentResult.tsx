import { contentStatusText, contentText, type ContentLocale } from "../contentI18n";
import type { ContentJobResponse, ContentPlan } from "../types";

export function ContentResult({
  locale,
  job,
  plan,
  artifactUrl,
  onNewTask,
  onDelete,
}: {
  locale: ContentLocale;
  job: ContentJobResponse;
  plan: ContentPlan | null;
  artifactUrl(path: string): string;
  onNewTask(): void;
  onDelete(): void;
}): React.JSX.Element {
  return (
    <main className={`content-result status-${job.status}`}>
      <section className="content-result-hero">
        <p className="creator-mark">what912</p>
        <p className="eyebrow">CONTENT / {job.status.toUpperCase()}</p>
        <h1>{contentText("result", locale)}</h1>
        <p className="content-outcome"><strong>{contentStatusText(job.status, locale)}</strong> — {job.message}</p>
        {job.error && <p role="alert" className="content-error">{job.error}</p>}
      </section>
      {plan && (
        <section className="content-result-grid">
          <div className="content-panel">
            <h2>{contentText("download", locale)}</h2>
            <ul className="content-downloads">
              {plan.public_artifacts.map((path) => (
                <li key={path}>
                  <a className="secondary-button" href={artifactUrl(path.replace(/^content-output\//, ""))} download>
                    {path.replace(/^content-output\//, "")}
                  </a>
                </li>
              ))}
            </ul>
          </div>
          <div className="content-panel">
            <h2>{contentText("sourceMap", locale)}</h2>
            <p>{plan.storyboard.estimated_output_duration_seconds.toFixed(2)}s · {(plan.storyboard.estimated_source_coverage * 100).toFixed(1)}% source coverage</p>
            <ol className="content-source-map-list">
              {plan.storyboard.items.filter((item) => item.decision === "keep").map((item) => (
                <li key={item.id}>
                  <span>{item.source_range.start_seconds.toFixed(2)}–{item.source_range.end_seconds.toFixed(2)}s</span>
                  <span>→ {item.output_order_index}</span>
                </li>
              ))}
            </ol>
          </div>
        </section>
      )}
      {job.warnings.length > 0 && <section className="content-panel"><h2>{contentText("warnings", locale)}</h2><ul>{job.warnings.map((warning) => <li key={warning}>{warning}</li>)}</ul></section>}
      <div className="content-result-actions">
        <button type="button" className="primary-button" onClick={onNewTask}>{contentText("newTask", locale)}</button>
        <button type="button" className="danger-button" onClick={onDelete}>{contentText("cancel", locale)}</button>
      </div>
    </main>
  );
}
