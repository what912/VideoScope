import type {
  PrivacyJobResponse,
  PrivacyTechnicalReport,
} from "../types";
import type { WorkbenchLocale } from "./PublishReadyView";
import {
  privacyArtifactText,
  privacyCheckText,
  privacyServerText,
} from "../privacyI18n";

interface PrivacyResultProps {
  locale: WorkbenchLocale;
  job: PrivacyJobResponse;
  report: PrivacyTechnicalReport | null;
  artifactUrl: (path: string) => string;
  onNewTask: () => void;
  onDelete: () => void;
  onRevise?: () => void;
}

function resultHeading(
  status: PrivacyJobResponse["status"],
  zh: boolean,
): string {
  switch (status) {
    case "completed":
      return zh ? "分享副本已通过本地验证" : "Sharing copy verified locally";
    case "needs_review":
      return zh ? "分享副本需要人工复核" : "Sharing copy needs human review";
    case "partial":
      return zh ? "分享副本未获公开下载授权" : "Sharing copy is not cleared for download";
    case "failed":
      return zh ? "安全分享任务失败" : "Safe Sharing task failed";
    case "cancelled":
      return zh ? "安全分享任务已取消" : "Safe Sharing task cancelled";
    default:
      return zh ? "安全分享任务尚未完成" : "Safe Sharing task is not complete";
  }
}

export function PrivacyResult({
  locale,
  job,
  report,
  artifactUrl,
  onNewTask,
  onDelete,
  onRevise,
}: PrivacyResultProps): React.JSX.Element {
  const zh = locale === "zh-CN";
  const verified = job.status === "completed";
  return (
    <main className={`privacy-result status-${job.status}`}>
      <section className="privacy-result-hero">
        <p className="eyebrow">{zh ? "本地分享结果" : "LOCAL SHARING RESULT"}</p>
        <h1>{resultHeading(job.status, zh)}</h1>
        <p>
          {verified
            ? zh
              ? "这是启发式检查通过的单独副本，不代表绝对安全。"
              : "This separate copy passed the configured heuristic checks; it is not a guarantee of absolute safety."
            : job.error
              ? privacyServerText(job.error, locale, "error", "safe_sharing")
              :
              (zh
                ? "至少一项必需检查未通过或无法验证。"
                : "At least one required check failed or could not be verified.")}
        </p>
      </section>
      {job.warnings.length > 0 && (
        <div className="privacy-warning" role="alert">
          <strong>{zh ? "检查提醒" : "Review notice"}</strong>
          <ul>{job.warnings.map((warning) => <li key={warning}>{privacyServerText(warning, locale, "scanner_warning", warning.split(/\s/, 1)[0] || "scanner")}</li>)}</ul>
        </div>
      )}
      {report && (
        <div className="privacy-result-grid">
          <section>
            <h2>{zh ? "验证检查" : "Verification checks"}</h2>
            <ul className="privacy-check-list">
              {report.verification.checks.map((check) => (
                <li key={check.check_id} className={`status-${check.status}`}>
                  <span aria-hidden="true">{check.status === "passed" ? "✓" : check.status === "failed" ? "×" : "!"}</span>
                  <div><strong>{privacyCheckText(check.check_id, check.status, locale)}{zh && <code className="privacy-machine-code">{check.check_id}</code>}</strong><p>{privacyServerText(check.message, locale, "verification", check.check_id)}</p></div>
                </li>
              ))}
            </ul>
          </section>
          <section>
            <h2>{zh ? "分享包" : "Share package"}</h2>
            <ul className="privacy-downloads" aria-label={zh ? "分享包" : "Share package"}>
              {report.artifacts.map((artifact) => (
                <li key={artifact.relative_path}>
                  <a className="secondary-button" href={artifactUrl(artifact.relative_path)} download>
                    {privacyArtifactText(artifact.relative_path, locale)}
                  </a>
                  <code>{artifact.sha256.slice(0, 12)}…</code>
                </li>
              ))}
            </ul>
          </section>
        </div>
      )}
      {!report && (
        <div className="privacy-warning" role="alert">
          {zh
            ? "此状态不授权公开下载。请返回复核并重新生成。"
            : "This state does not authorize public downloads. Review and regenerate the task."}
        </div>
      )}
      <div className="privacy-result-actions">
        {onRevise && (job.status === "needs_review" || job.status === "partial") && (
          <button type="button" className="secondary-button" onClick={onRevise}>
            {zh ? "修改并重新运行" : "Revise and rerun"}
          </button>
        )}
        <button type="button" className="primary-button" onClick={onNewTask}>
          {zh ? "新建安全分享任务" : "New Safe Sharing task"}
        </button>
        <button type="button" className="danger-button" onClick={onDelete}>
          {zh ? "删除本地任务数据" : "Delete local task data"}
        </button>
      </div>
    </main>
  );
}
