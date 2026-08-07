import type {
  PublishJobResponse,
  PublishTechnicalReport,
} from "../types";
import {
  presentPublishFailure,
  presentReviewReason,
  presentVerificationCheck,
  type PresentationLocale,
} from "../publishPresentation";

export interface ResultCopy {
  productLabel: string;
  completed: string;
  needsReview: string;
  failed: string;
  cancelled: string;
  passedDescription: string;
  reviewDescription: string;
  failedDescription: string;
  cancelledDescription: string;
  checks: string;
  reviewReasons: string;
  artifacts: string;
  download: string;
  loadingReport: string;
  statusPassed: string;
  statusNeedsReview: string;
  statusFailed: string;
  newPublish: string;
}

interface Props {
  job: PublishJobResponse;
  report: PublishTechnicalReport | null;
  copy: ResultCopy;
  artifactUrl: (jobId: string, path: string) => string;
  locale: PresentationLocale;
  onNewPublish: () => void;
}

export function PublishResult({
  job,
  report,
  copy,
  artifactUrl,
  locale,
  onNewPublish,
}: Props): React.JSX.Element {
  let heading: string;
  let description: string;
  switch (job.status) {
    case "completed":
      heading = copy.completed;
      description = copy.passedDescription;
      break;
    case "needs_review":
      heading = copy.needsReview;
      description = copy.reviewDescription;
      break;
    case "failed":
      heading = copy.failed;
      description = copy.failedDescription;
      break;
    case "cancelled":
      heading = copy.cancelled;
      description = copy.cancelledDescription;
      break;
    default:
      return <></>;
  }
  const published = job.status === "completed" || job.status === "needs_review";

  return (
    <main className={`publish-result status-${job.status}`}>
      <section className="publish-result-hero">
        <p className="step-label">
          {copy.productLabel} / {job.profile_id}
        </p>
        <h1>{heading}</h1>
        <p>{description}</p>
        {job.error && (
          <p className="form-error">
            {presentPublishFailure(locale, job.error)}
          </p>
        )}
        <button
          className="secondary-button"
          type="button"
          onClick={onNewPublish}
        >
          {copy.newPublish}
        </button>
      </section>

      {published && !report && <p aria-live="polite">{copy.loadingReport}</p>}
      {report && (
        <div className="publish-result-grid">
          <section aria-labelledby="checks-heading">
            <h2 id="checks-heading">{copy.checks}</h2>
            <ul className="verification-list">
              {report.verification.checks.map((check) => (
                <li key={check.check_id} className={`status-${check.status}`}>
                  <span>
                    {check.status === "passed"
                      ? copy.statusPassed
                      : check.status === "needs_review"
                        ? copy.statusNeedsReview
                        : copy.statusFailed}
                  </span>
                  <strong>{presentVerificationCheck(locale, check)}</strong>
                </li>
              ))}
            </ul>
            {report.verification.manual_review_reasons.length > 0 && (
              <div className="manual-review-reasons">
                <h3>{copy.reviewReasons}</h3>
                <ul>
                  {report.verification.manual_review_reasons.map((reason) => (
                    <li key={reason}>
                      {presentReviewReason(
                        locale,
                        reason,
                        report.verification.checks,
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>
          <section aria-labelledby="artifacts-heading">
            <h2 id="artifacts-heading">{copy.artifacts}</h2>
            <div className="publish-downloads">
              {report.artifacts.map((artifact) => (
                <a
                  className="secondary-button"
                  href={artifactUrl(job.job_id, artifact.relative_path)}
                  download
                  key={artifact.relative_path}
                  aria-label={`${copy.download} ${artifact.relative_path}`}
                >
                  <span>{artifact.relative_path}</span>
                  <span aria-hidden="true">↓</span>
                </a>
              ))}
            </div>
          </section>
        </div>
      )}
    </main>
  );
}
