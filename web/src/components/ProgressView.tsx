import { useEffect, useState } from "react";
import type { JobResponse } from "../types";

interface Props {
  job: JobResponse;
  onCancel: () => void;
}

function elapsedSeconds(createdAt: string): number {
  return Math.max(0, (Date.now() - Date.parse(createdAt)) / 1000);
}

export function ProgressView({ job, onCancel }: Props): React.JSX.Element {
  const [elapsed, setElapsed] = useState(() => elapsedSeconds(job.created_at));
  useEffect(() => {
    const timer = window.setInterval(
      () => setElapsed(elapsedSeconds(job.created_at)),
      500,
    );
    return () => window.clearInterval(timer);
  }, [job.created_at]);

  return (
    <main className="progress-shell">
      <section className="progress-card" aria-live="polite">
        <div className="analysis-pulse" aria-hidden="true">
          <span />
          <span />
          <span />
        </div>
        <p className="eyebrow">Analysis in progress</p>
        <h1>{job.message}</h1>
        <p className="muted">
          Source data remains in the local VideoScope job directory.
        </p>
        <div className="progress-track" aria-label="Analysis progress">
          <span style={{ width: `${job.progress_percent}%` }} />
        </div>
        <div className="progress-metrics">
          <div>
            <small>Stage</small>
            <strong>{job.status}</strong>
          </div>
          <div>
            <small>Progress</small>
            <strong>{job.progress_percent}%</strong>
          </div>
          <div>
            <small>Current detector</small>
            <strong>{job.current_detector ?? "Preparing…"}</strong>
          </div>
          <div>
            <small>Elapsed</small>
            <strong>{elapsed.toFixed(1)} s</strong>
          </div>
        </div>
        <button className="secondary-button danger-button" type="button" onClick={onCancel}>
          Cancel analysis
        </button>
      </section>
    </main>
  );
}
