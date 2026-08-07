import type { PublishPlan } from "../types";
import {
  presentPublishAction,
  type PresentationLocale,
} from "../publishPresentation";

export interface PlanReviewCopy {
  sourceHeading: string;
  planHeading: string;
  dimensions: string;
  duration: string;
  codec: string;
  audio: string;
  yes: string;
  no: string;
  output: string;
  planVersion: string;
  actionLabel: string;
}

export function PublishPlanReview({
  plan,
  copy,
  locale,
}: {
  plan: PublishPlan;
  copy: PlanReviewCopy;
  locale: PresentationLocale;
}): React.JSX.Element {
  const metadata = plan.source_metadata;
  return (
    <div className="publish-review-grid">
      <section className="publish-source-summary" aria-labelledby="source-heading">
        <p className="step-label">02 / {copy.sourceHeading}</p>
        <h2 id="source-heading">{metadata.filename}</h2>
        <dl className="publish-metadata">
          <div>
            <dt>{copy.dimensions}</dt>
            <dd>
              {metadata.width} × {metadata.height}
            </dd>
          </div>
          <div>
            <dt>{copy.duration}</dt>
            <dd>{metadata.duration_seconds.toFixed(1)}s</dd>
          </div>
          <div>
            <dt>{copy.codec}</dt>
            <dd>{metadata.codec}</dd>
          </div>
          <div>
            <dt>{copy.audio}</dt>
            <dd>{metadata.has_audio ? copy.yes : copy.no}</dd>
          </div>
        </dl>
      </section>

      <section className="publish-plan" aria-labelledby="plan-heading">
        <div className="publish-plan-heading">
          <div>
            <p className="step-label">03 / {copy.planHeading}</p>
            <h2 id="plan-heading">{copy.planHeading}</h2>
          </div>
          <span>
            {copy.planVersion} {plan.schema_version}
          </span>
        </div>
        <ol className="publish-action-list">
          {plan.actions.map((action, index) => {
            const presentation = presentPublishAction(locale, action);
            return (
              <li
                key={action.action_id}
                aria-label={`${copy.actionLabel} ${index + 1}`}
              >
                <span>{String(index + 1).padStart(2, "0")}</span>
                <div>
                  <strong>{presentation.label}</strong>
                  <p>{presentation.description}</p>
                </div>
              </li>
            );
          })}
        </ol>
        <p className="publish-output-name">
          <span>{copy.output}</span>
          <strong>{plan.output_filename}</strong>
        </p>
      </section>
    </div>
  );
}
