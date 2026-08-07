import type { PrivacyPlan } from "../types";
import { privacyIdentifierText } from "../privacyI18n";
import type { WorkbenchLocale } from "./PublishReadyView";

interface PrivacyPlanReviewProps {
  locale: WorkbenchLocale;
  plan: PrivacyPlan;
  previewUrl: string;
  sourceUrl: string | null;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function PrivacyPlanReview({
  locale,
  plan,
  previewUrl,
  sourceUrl,
  busy,
  onConfirm,
  onCancel,
}: PrivacyPlanReviewProps): React.JSX.Element {
  const zh = locale === "zh-CN";
  return (
    <main className="privacy-shell privacy-confirmation-shell">
      <header className="privacy-hero compact">
        <p className="eyebrow">{zh ? "确认门槛" : "CONFIRMATION GATE"}</p>
        <h1>{zh ? "检查脱敏预览" : "Review the redaction preview"}</h1>
        <p>
          {zh
            ? "只有下方完全匹配的计划摘要会被执行。源视频保持只读。"
            : "Only this exact digest-bound plan will run. The source stays read-only."}
        </p>
      </header>
      <section className="privacy-preview-compare" aria-label={zh ? "源视频和预览对比" : "Source and preview comparison"}>
        <figure>
          <figcaption>{zh ? "源视频（只读）" : "Source · read-only"}</figcaption>
          {sourceUrl ? <video src={sourceUrl} controls /> : <div className="privacy-preview-missing">{zh ? "恢复后源预览不可用；计划与证据仍保留。" : "Source preview is unavailable after recovery; the plan and evidence remain."}</div>}
        </figure>
        <figure>
          <figcaption>{zh ? "私有短预览" : "Private short preview"}</figcaption>
          <video src={previewUrl} controls />
        </figure>
      </section>
      <section className="privacy-plan-review">
        <div className="privacy-section-heading">
          <div>
            <p className="step-label">{zh ? "计划摘要" : "PLAN SUMMARY"}</p>
            <h2>{zh ? `${plan.actions.length} 项本地操作` : `${plan.actions.length} local actions`}</h2>
          </div>
          <span>{privacyIdentifierText("profile", plan.profile, locale)}</span>
        </div>
        <ol className="privacy-action-list">
          {plan.actions.map((action) => (
            <li key={action.id}>
              <strong>{privacyIdentifierText("action", action.kind, locale)}</strong>
              <span>{action.start_seconds.toFixed(2)}–{action.end_seconds.toFixed(2)} s</span>
            </li>
          ))}
        </ol>
        <div className="privacy-digest">
          <span>{zh ? "精确计划摘要" : "Exact plan digest"}</span>
          <code>{plan.digest}</code>
        </div>
        <div className="privacy-action-row">
          <button type="button" className="danger-button" onClick={onCancel} disabled={busy}>
            {zh ? "取消任务" : "Cancel task"}
          </button>
          <button type="button" className="primary-button" onClick={onConfirm} disabled={busy}>
            {busy
              ? zh
                ? "正在确认…"
                : "Confirming…"
              : zh
                ? "确认并创建分享副本"
                : "Confirm and create share copy"}
          </button>
        </div>
      </section>
    </main>
  );
}
