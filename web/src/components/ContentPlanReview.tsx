import { contentText, type ContentLocale } from "../contentI18n";
import type { ContentPlan } from "../types";

export function ContentPlanReview({
  locale,
  plan,
  busy,
  onConfirm,
}: {
  locale: ContentLocale;
  plan: ContentPlan;
  busy: boolean;
  onConfirm(): void;
}): React.JSX.Element {
  const changing = plan.actions.filter(
    (action) => action.changes_content && action.requires_confirmation,
  );
  return (
    <section className="content-panel content-plan" aria-labelledby="content-plan-title">
      <div className="content-section-heading">
        <div>
          <p className="eyebrow">PLAN / DIGEST BOUND</p>
          <h2 id="content-plan-title">{contentText("plan", locale)}</h2>
        </div>
        <p>{contentText("confirmationHelp", locale)}</p>
      </div>
      <dl className="content-plan-summary">
        <div><dt>{contentText("goal", locale)}</dt><dd>{contentText(plan.goal, locale)}</dd></div>
        <div><dt>{contentText("outputOrder", locale)}</dt><dd>{plan.storyboard.items.filter((item) => item.decision === "keep").length}</dd></div>
        <div><dt>{contentText("digest", locale)}</dt><dd><code>{plan.plan_digest}</code></dd></div>
      </dl>
      <h3>{contentText("exactActions", locale)}</h3>
      <ol className="content-action-list">
        {changing.map((action) => (
          <li key={action.id}>
            <strong>{action.kind}</strong>
            <span>{action.description}</span>
            <code>{action.id.slice(0, 20)}…</code>
          </li>
        ))}
      </ol>
      <p className="content-lock-summary">
        {plan.locked_ranges.length} locked range(s) · {Object.keys(plan.preview_identities).length} preview identity/identities
      </p>
      <button type="button" className="primary-button" disabled={busy} onClick={onConfirm}>
        {contentText("confirm", locale)}
      </button>
    </section>
  );
}
