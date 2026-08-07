import type { RescueDamageMap, RescueJobResponse, RescueTechnicalReport, RescueVerificationStatus } from "../types";
import { rescueText, type RescueLocale } from "../rescueI18n";
import { RescueDamageTimeline } from "./RescueDamageTimeline";
import { RescuePreviewComparison } from "./RescuePreviewComparison";

function verificationStatusText(status: RescueVerificationStatus, locale: RescueLocale): string {
  if (status === "passed") return rescueText("verificationPassed", locale);
  if (status === "needs_review") return rescueText("verificationNeedsReview", locale);
  return rescueText("verificationFailed", locale);
}

export function RescueResult({ locale, job, report, damageMap, lockedRanges, originalUrl, artifactUrl, onNewTask, onDelete }: { locale: RescueLocale; job: RescueJobResponse; report: RescueTechnicalReport | null; damageMap: RescueDamageMap | null; lockedRanges: Array<[number, number]>; originalUrl: string | null; artifactUrl(path: string): string; onNewTask(): void; onDelete(): void }): React.JSX.Element {
  const outcome = report?.outcome === "partial" || job.status === "partial" ? rescueText("partial", locale) : report?.outcome === "needs_review" || job.status === "needs_review" ? rescueText("needsReview", locale) : report?.outcome === "failed" || job.status === "failed" ? rescueText("failed", locale) : job.status === "cancelled" ? rescueText("cancelled", locale) : rescueText("completed", locale);
  const artifacts = report?.artifacts ?? [];
  const find = (part: string) => artifacts.find((artifact) => artifact.relative_path.includes(part));
  const verified = report?.verification.artifacts ?? [];
  const faithful = verified.find((artifact) => artifact.artifact_role === "faithful");
  const improved = verified.find((artifact) => artifact.artifact_role === "improved");
  return <main className={`rescue-result status-${job.status}`}><p className="creator-mark">what912</p><p>{rescueText("local", locale)}</p><h1>{outcome}</h1>{job.error && <p role="alert">{job.error}</p>}{damageMap && <RescueDamageTimeline locale={locale} damageMap={damageMap} lockedRanges={lockedRanges} />}<RescuePreviewComparison locale={locale} originalUrl={originalUrl} faithfulUrl={faithful ? artifactUrl(faithful.relative_path) : null} improvedUrl={improved ? artifactUrl(improved.relative_path) : null} />{report && <>{faithful && <p>{rescueText("faithful", locale)} {rescueText("verification", locale)}: {verificationStatusText(report.verification.faithful_status, locale)}</p>}{improved && report.verification.improved_status && <p>{rescueText("improved", locale)} {rescueText("verification", locale)}: {verificationStatusText(report.verification.improved_status, locale)}</p>}<p>{report.limitations.join(" ")}</p>{report.manual_review_reasons.length > 0 && <ul>{report.manual_review_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}<div className="rescue-downloads">{faithful && <a download href={artifactUrl(faithful.relative_path)}>{rescueText("downloadFaithful", locale)}</a>}{improved && <a download href={artifactUrl(improved.relative_path)}>{rescueText("downloadImproved", locale)}</a>}{find("technical-report") && <a download href={artifactUrl(find("technical-report")!.relative_path)}>{rescueText("downloadReport", locale)}</a>}{find("rescue-plan") && <a download href={artifactUrl(find("rescue-plan")!.relative_path)}>{rescueText("downloadJson", locale)}</a>}{find("report.html") && <a href={artifactUrl(find("report.html")!.relative_path)}>{rescueText("openHtml", locale)}</a>}</div></>}<button type="button" onClick={onNewTask}>{rescueText("newTask", locale)}</button><button type="button" className="danger-button" onClick={onDelete}>{rescueText("delete", locale)}</button></main>;
}
