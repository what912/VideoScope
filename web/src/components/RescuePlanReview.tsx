import type { RescuePlan } from "../types";
import { rescueText, type RescueLocale } from "../rescueI18n";

export function RescuePlanReview({ locale, plan, onConfirm, busy }: { locale: RescueLocale; plan: RescuePlan; onConfirm(): void; busy: boolean }): React.JSX.Element {
  const confirmable = plan.actions.filter((action) => action.requires_confirmation);
  const improvementKinds = new Set(["adjust_luma", "denoise_video", "sharpen", "deflicker", "stabilize", "normalize_audio", "denoise_audio"]);
  const canImprove = plan.strategy === "balanced" && confirmable.some((action) => improvementKinds.has(action.kind));
  return <section className="rescue-plan" aria-label={rescueText("plan", locale)}><h1>{rescueText("plan", locale)}</h1><p><code>{plan.plan_digest}</code></p><p>{rescueText("maximumStrength", locale)}: <strong>{plan.effective_config.balanced_strength_limit}</strong></p><p>{plan.assessment_limitations.join(" ")}</p><details className="rescue-advanced"><summary>{rescueText("advanced", locale)}</summary>{plan.actions.map((action) => {
    const strength = action.parameters.strength;
    return <div className="rescue-action" key={action.id}><p>{rescueText("action", locale)}: {action.description}</p>{typeof strength === "number" && <label>{rescueText("strength", locale)} <input type="range" min="0" max={plan.effective_config.balanced_strength_limit} step="0.05" value={Math.min(strength, plan.effective_config.balanced_strength_limit)} disabled aria-label={`${action.id} ${rescueText("strength", locale)}`} /><output>{String(strength)}</output></label>}</div>;
  })}</details>{canImprove && <p>{rescueText("improved", locale)}</p>}<button type="button" className="primary-button" disabled={busy} onClick={onConfirm}>{rescueText("confirm", locale)}</button></section>;
}
