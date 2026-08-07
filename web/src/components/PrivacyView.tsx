import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  confirmPrivacyJob,
  createPrivacyJob,
  deletePrivacyJob,
  getPrivacyJob,
  getPrivacyPlan,
  getPrivacyRiskMap,
  getPrivacyTechnicalReport,
  listPrivacyProfiles,
  preparePrivacyJob,
  privacyArtifactUrl,
  privacyPrivateArtifactUrl,
  reviewPrivacyJob,
  subscribeToPrivacyEvents,
  type JobEventSubscription,
} from "../api";
import type {
  ManualAudioInterval,
  ManualVisualRegion,
  NormalizedBox,
  PrivacyDecision,
  PrivacyJobEvent,
  PrivacyJobResponse,
  PrivacyJobStatus,
  PrivacyPlan,
  PrivacyReviewPayload,
  PrivacyRisk,
  PrivacyRiskMap,
  PrivacyTechnicalReport,
  RedactionStyle,
  ShareAudienceProfile,
} from "../types";
import {
  mergePrivacySnapshot,
  usePrivacyLifecycle,
  type PrivacyLifecycleApi,
} from "../hooks/usePrivacyLifecycle";
import {
  privacyIdentifierText,
  privacyServerText,
  privacyStatusText,
} from "../privacyI18n";
import { PrivacyOverlayEditor } from "./PrivacyOverlayEditor";
import { PrivacyPlanReview } from "./PrivacyPlanReview";
import { PrivacyResult } from "./PrivacyResult";
import { PrivacyRiskList } from "./PrivacyRiskList";
import { PrivacyTimeline } from "./PrivacyTimeline";
import type { WorkbenchLocale } from "./PublishReadyView";

export interface PrivacyApi extends PrivacyLifecycleApi {
  listProfiles(): Promise<ShareAudienceProfile[]>;
  createJob(video: File, profileId: string, enableOcr: boolean): Promise<PrivacyJobResponse>;
  getJob(jobId: string): Promise<PrivacyJobResponse>;
  getRiskMap(jobId: string): Promise<PrivacyRiskMap>;
  review(jobId: string, payload: PrivacyReviewPayload): Promise<PrivacyJobResponse>;
  prepare(jobId: string): Promise<PrivacyJobResponse>;
  getPlan(jobId: string): Promise<PrivacyPlan>;
  confirm(jobId: string, digest: string): Promise<PrivacyJobResponse>;
  deleteJob(jobId: string): Promise<PrivacyJobResponse | null>;
  getTechnicalReport(jobId: string): Promise<PrivacyTechnicalReport>;
  subscribeToEvents(
    jobId: string,
    onEvent: (event: PrivacyJobEvent) => void,
    onError: (error: Error) => void,
  ): JobEventSubscription;
  publicArtifactUrl(jobId: string, path: string): string;
  privateArtifactUrl(jobId: string, path: string): string;
}

const DEFAULT_API: PrivacyApi = {
  listProfiles: () => listPrivacyProfiles(),
  createJob: (video, profileId, enableOcr) =>
    createPrivacyJob(video, profileId, enableOcr),
  getJob: (jobId) => getPrivacyJob(jobId),
  getRiskMap: (jobId) => getPrivacyRiskMap(jobId),
  review: (jobId, payload) => reviewPrivacyJob(jobId, payload),
  prepare: (jobId) => preparePrivacyJob(jobId),
  getPlan: (jobId) => getPrivacyPlan(jobId),
  confirm: (jobId, digest) => confirmPrivacyJob(jobId, digest),
  deleteJob: (jobId) => deletePrivacyJob(jobId),
  getTechnicalReport: (jobId) => getPrivacyTechnicalReport(jobId),
  subscribeToEvents: (jobId, onEvent, onError) =>
    subscribeToPrivacyEvents(jobId, onEvent, onError),
  publicArtifactUrl: privacyArtifactUrl,
  privateArtifactUrl: privacyPrivateArtifactUrl,
};

const TERMINAL = new Set<PrivacyJobStatus>([
  "completed",
  "needs_review",
  "partial",
  "failed",
  "cancelled",
]);

export { mergePrivacySnapshot } from "../hooks/usePrivacyLifecycle";

interface PrivacyViewProps {
  locale: WorkbenchLocale;
  initialJobId?: string | null;
  onJobChange: (jobId: string | null) => void;
  api?: PrivacyApi;
}

interface ManualVisualDraft extends ManualVisualRegion {
  clientId: string;
}

interface ManualAudioDraft extends ManualAudioInterval {
  clientId: string;
}

function evidencePath(risk: PrivacyRisk | null): string | null {
  if (!risk) return null;
  for (const item of risk.private_evidence) {
    const path = item.relative_path;
    if (typeof path === "string" && path.startsWith("evidence/")) return path;
  }
  return null;
}

function styleForRisk(risk: PrivacyRisk): RedactionStyle {
  if (risk.risk_type === "metadata") return "remove_metadata";
  if (risk.risk_type === "manual_audio") return "mute";
  return risk.recommended_style ?? "blur";
}

function localizedError(
  caught: unknown,
  zh: boolean,
  englishFallback: string,
  chineseFallback: string,
): string {
  if (!zh && caught instanceof Error) return caught.message;
  if (zh && caught instanceof Error && caught.message)
    return privacyServerText(caught.message, "zh-CN", "error", "safe_sharing");
  return zh ? chineseFallback : englishFallback;
}

export function PrivacyView({
  locale,
  initialJobId = null,
  onJobChange,
  api = DEFAULT_API,
}: PrivacyViewProps): React.JSX.Element {
  const zh = locale === "zh-CN";
  const fileRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const manualIdRef = useRef(0);
  const [profiles, setProfiles] = useState<ShareAudienceProfile[]>([]);
  const [profileId, setProfileId] = useState("public");
  const [enableOcr, setEnableOcr] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(initialJobId);
  const [selectedRiskId, setSelectedRiskId] = useState<string | null>(null);
  const [selectedManualVisualId, setSelectedManualVisualId] = useState<string | null>(null);
  const [decisions, setDecisions] = useState<Record<string, PrivacyDecision>>({});
  const [styles, setStyles] = useState<Record<string, RedactionStyle>>({});
  const [editedBoxes, setEditedBoxes] = useState<Record<string, NormalizedBox>>({});
  const [manualVisuals, setManualVisuals] = useState<ManualVisualDraft[]>([]);
  const [audioIntervals, setAudioIntervals] = useState<ManualAudioDraft[]>([]);
  const [audioStart, setAudioStart] = useState("0");
  const [audioEnd, setAudioEnd] = useState("1");
  const [currentTime, setCurrentTime] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const lifecycleError = useCallback(
    (caught: Error): void => {
      setError(
        localizedError(
          caught,
          zh,
          "Could not refresh Safe Sharing task.",
          "无法刷新安全分享任务",
        ),
      );
    },
    [zh],
  );
  const { job, riskMap, plan, technicalReport, applySnapshot } =
    usePrivacyLifecycle({ api, jobId, onError: lifecycleError });

  const selectedRisk = useMemo(
    () => riskMap?.risks.find((risk) => risk.id === selectedRiskId) ?? null,
    [riskMap, selectedRiskId],
  );
  const selectedManualVisual = useMemo(
    () => manualVisuals.find((item) => item.clientId === selectedManualVisualId) ?? null,
    [manualVisuals, selectedManualVisualId],
  );

  useEffect(() => {
    let active = true;
    void api
      .listProfiles()
      .then((items) => {
        if (!active) return;
        setProfiles(items);
      })
      .catch((caught: unknown) => {
        if (active) {
          setError(
            localizedError(
              caught,
              zh,
              "Could not load privacy profiles.",
              "无法加载隐私配置",
            ),
          );
        }
      });
    return () => {
      active = false;
    };
  }, [api, zh]);

  useEffect(() => {
    if (profiles.length > 0 && !profiles.some((item) => item.id === profileId)) {
      setProfileId(profiles[0].id);
    }
  }, [profileId, profiles]);

  useEffect(() => {
    if (!riskMap) return;
    setSelectedRiskId((current) =>
      current && riskMap.risks.some((risk) => risk.id === current)
        ? current
        : riskMap.risks[0]?.id ?? null,
    );
  }, [riskMap]);

  useEffect(
    () => () => {
      if (sourceUrl?.startsWith("blob:")) URL.revokeObjectURL(sourceUrl);
    },
    [sourceUrl],
  );

  const resetLocal = (): void => {
    setJobId(null);
    setSelectedRiskId(null);
    setSelectedManualVisualId(null);
    setDecisions({});
    setStyles({});
    setEditedBoxes({});
    setManualVisuals([]);
    setAudioIntervals([]);
    setError(null);
    setFile(null);
    setSourceUrl(null);
    onJobChange(null);
  };

  const start = async (): Promise<void> => {
    if (!file) {
      fileRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await api.createJob(file, profileId, enableOcr);
      setJobId(created.job_id);
      onJobChange(created.job_id);
    } catch (caught) {
      setError(
        localizedError(
          caught,
          zh,
          "Could not start Safe Sharing.",
          "无法启动安全分享任务",
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const chooseRisk = (risk: PrivacyRisk): void => {
    setSelectedRiskId(risk.id);
    setSelectedManualVisualId(null);
    setCurrentTime(risk.start_seconds);
    if (videoRef.current) videoRef.current.currentTime = risk.start_seconds;
    if (risk.box && !editedBoxes[risk.id]) {
      setEditedBoxes((current) => ({ ...current, [risk.id]: risk.box! }));
    }
    if (!styles[risk.id]) {
      setStyles((current) => ({ ...current, [risk.id]: styleForRisk(risk) }));
    }
  };

  const decide = (decision: PrivacyDecision): void => {
    if (!selectedRisk) return;
    setDecisions((current) => ({ ...current, [selectedRisk.id]: decision }));
    if (decision === "redact") {
      setStyles((current) => ({
        ...current,
        [selectedRisk.id]: current[selectedRisk.id] ?? styleForRisk(selectedRisk),
      }));
    }
  };

  const addManualVisual = (): void => {
    const duration = riskMap?.duration_seconds ?? 1;
    const start = Math.max(0, Math.min(currentTime, Math.max(0, duration - 0.1)));
    const clientId = `manual-visual-${++manualIdRef.current}`;
    setSelectedRiskId(null);
    setSelectedManualVisualId(clientId);
    setManualVisuals((current) => [
      ...current,
      {
        clientId,
        start_seconds: start,
        end_seconds: Math.min(duration, Math.max(start + 0.1, start + 1)),
        box: { x_min: 0.3, y_min: 0.3, x_max: 0.7, y_max: 0.7 },
        style: "blur",
      },
    ]);
  };

  const addAudio = (): void => {
    const duration = riskMap?.duration_seconds ?? 0;
    const start = Number(audioStart);
    const end = Number(audioEnd);
    if (
      !Number.isFinite(start) ||
      !Number.isFinite(end) ||
      end <= start ||
      start < 0 ||
      end > duration
    ) {
      setError(zh ? "静音区间必须位于视频时长内，且结束时间晚于开始时间。" : "Mute intervals must be inside the video and end after they start.");
      return;
    }
    setAudioIntervals((current) => [
      ...current,
      {
        clientId: `manual-audio-${++manualIdRef.current}`,
        start_seconds: start,
        end_seconds: end,
        style: "mute",
      },
    ]);
    setError(null);
  };

  const generatePreview = async (): Promise<void> => {
    if (!jobId || !riskMap) return;
    const reviews = riskMap.risks.flatMap((risk) => {
      const decision = decisions[risk.id] ?? risk.decision;
      if (decision === "unreviewed") return [];
      return [
        {
          risk_id: risk.id,
          decision,
          style: decision === "redact" ? styles[risk.id] ?? styleForRisk(risk) : null,
          edited_box: editedBoxes[risk.id] ?? null,
          reviewed_at: new Date().toISOString(),
        },
      ];
    });
    const payload: PrivacyReviewPayload = {
      reviews,
      manual_visual_regions: manualVisuals.map((item) => ({
        start_seconds: item.start_seconds,
        end_seconds: item.end_seconds,
        box: item.box,
        style: item.style,
      })),
      manual_audio_intervals: audioIntervals.map((item) => ({
        start_seconds: item.start_seconds,
        end_seconds: item.end_seconds,
        style: item.style,
      })),
    };
    setBusy(true);
    setError(null);
    try {
      await api.review(jobId, payload);
      const prepared = await api.prepare(jobId);
      await applySnapshot(prepared);
    } catch (caught) {
      setError(
        localizedError(
          caught,
          zh,
          "Could not generate privacy preview.",
          "无法生成隐私预览",
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const confirm = async (): Promise<void> => {
    if (!jobId || !plan) return;
    setBusy(true);
    setError(null);
    try {
      const accepted = await api.confirm(jobId, plan.digest);
      await applySnapshot(accepted);
    } catch (caught) {
      setError(
        localizedError(
          caught,
          zh,
          "Could not confirm privacy plan.",
          "无法确认隐私处理计划",
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const deleteTask = async (): Promise<void> => {
    if (!jobId) return;
    setBusy(true);
    try {
      const response = await api.deleteJob(jobId);
      if (response === null) resetLocal();
      else await applySnapshot(response);
    } catch (caught) {
      setError(
        localizedError(
          caught,
          zh,
          "Could not remove local task data.",
          "无法删除本地任务数据",
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const reviseAndRerun = async (): Promise<void> => {
    setError(null);
    setSelectedRiskId(null);
    setSelectedManualVisualId(null);
    setDecisions({});
    setStyles({});
    setEditedBoxes({});
    setManualVisuals([]);
    setAudioIntervals([]);
    if (!file) {
      setJobId(null);
      onJobChange(null);
      setError(
        zh
          ? "请重新选择源视频后再修改此任务。"
          : "Re-select the source video to revise this task.",
      );
      return;
    }
    setBusy(true);
    try {
      const created = await api.createJob(file, profileId, enableOcr);
      setJobId(created.job_id);
      onJobChange(created.job_id);
    } catch (caught) {
      setError(
        localizedError(
          caught,
          zh,
          "Could not restart Safe Sharing.",
          "无法重新启动安全分享任务。",
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  const seek = (seconds: number): void => {
    setCurrentTime(seconds);
    if (videoRef.current) videoRef.current.currentTime = seconds;
  };

  const stageUnavailable = Boolean(
    job &&
      ((job.status === "awaiting_review" && !riskMap) ||
        (job.status === "awaiting_confirmation" && !plan) ||
        (job.status === "completed" && !technicalReport)),
  );

  const retryStage = async (): Promise<void> => {
    if (!job) return;
    setBusy(true);
    try {
      await applySnapshot(job);
      setError(null);
    } catch (caught) {
      setError(
        localizedError(
          caught,
          zh,
          "Could not reload the current Safe Sharing stage.",
          "无法重新加载当前安全分享阶段。",
        ),
      );
    } finally {
      setBusy(false);
    }
  };

  if (!jobId) {
    return (
      <main className="privacy-shell">
        <section className="privacy-hero">
          <p className="eyebrow">{zh ? "本地隐私处理" : "LOCAL PRIVACY WORKFLOW"}</p>
          <h1>{zh ? "分享前，看清并处理风险" : "See and redact risks before you share"}</h1>
          <p>{zh ? "扫描视频、人工确认每个建议，并在本机生成单独的分享副本。不会覆盖源视频，也不承诺发现所有风险。" : "Scan a video, review every proposal, and create a separate sharing copy on this machine. The source is never overwritten and no scan can promise every risk is found."}</p>
          <div className="privacy-boundaries" aria-label={zh ? "隐私处理边界" : "Privacy processing boundaries"}>
            <span>⌂ {zh ? "仅本机处理" : "On-device only"}</span>
            <span>◈ {zh ? "源视频只读" : "Source read-only"}</span>
            <span>◎ {zh ? "人工确认后才修改" : "Changes require review"}</span>
          </div>
        </section>
        <section className="privacy-start-card">
          <fieldset className="privacy-profile-fieldset">
            <legend>{zh ? "选择分享对象" : "Choose who will receive it"}</legend>
            <div className="privacy-profile-grid">
              {profiles.map((profile) => (
                <label key={profile.id} className={profileId === profile.id ? "is-selected" : ""}>
                  <input type="radio" name="privacy-profile" value={profile.id} checked={profileId === profile.id} onChange={() => setProfileId(profile.id)} aria-label={privacyIdentifierText("profile", profile.id, locale)} />
                  <strong>{privacyIdentifierText("profile", profile.id, locale)}</strong>
                  <small>{zh ? `${profile.required_manual_review_categories.length} 类需要人工复核` : `${profile.required_manual_review_categories.length} categories require review`}</small>
                </label>
              ))}
            </div>
          </fieldset>
          <label className={`privacy-file ${file ? "has-file" : ""}`}>
            <input ref={fileRef} type="file" accept="video/*,.mkv" onChange={(event) => {
              const next = event.target.files?.[0] ?? null;
              setFile(next);
              if (next) setSourceUrl(URL.createObjectURL(next));
            }} />
            <span><strong>{file?.name ?? (zh ? "选择本地视频" : "Choose a local video")}</strong><small>{file ? `${(file.size / 1024 / 1024).toFixed(1)} MiB` : zh ? "MP4 · MOV · MKV · WEBM" : "MP4 · MOV · MKV · WEBM"}</small></span>
          </label>
          <label className="privacy-ocr-toggle"><input type="checkbox" checked={enableOcr} onChange={(event) => setEnableOcr(event.target.checked)} /><span><strong>{zh ? "启用可选本地 OCR" : "Enable optional local OCR"}</strong><small>{zh ? "仅在本机已安装时使用；不会自动下载模型。" : "Used only when already installed locally; no model is downloaded automatically."}</small></span></label>
          {error && <p className="form-error" role="alert">{error}</p>}
          <button type="button" className="primary-button" disabled={!file || busy} onClick={() => void start()}>{busy ? (zh ? "正在创建…" : "Creating…") : zh ? "分享前扫描" : "Scan before sharing"}</button>
        </section>
      </main>
    );
  }

  if (job && stageUnavailable && error) {
    return (
      <main className="privacy-shell privacy-progress-shell">
        <section className="privacy-progress-card privacy-recovery-card">
          <p className="eyebrow">{zh ? "本地恢复" : "LOCAL RECOVERY"}</p>
          <h1>{privacyStatusText(job.status, locale)}</h1>
          <p className="form-error" role="alert">{error}</p>
          <p>
            {zh
              ? "任务状态已保留。安全重试只重新读取当前阶段，不会重新处理或覆盖源视频。"
              : "The job status is preserved. Safe retry only reloads this stage; it does not reprocess or overwrite the source."}
          </p>
          <div className="privacy-action-row">
            <button type="button" className="danger-button" disabled={busy} onClick={() => void deleteTask()}>{zh ? "取消任务" : "Cancel task"}</button>
            <button type="button" className="primary-button" disabled={busy} onClick={() => void retryStage()}>{zh ? "安全重试" : "Safe retry"}</button>
          </div>
        </section>
      </main>
    );
  }

  if (job && TERMINAL.has(job.status) && !stageUnavailable) {
    return (
      <PrivacyResult locale={locale} job={job} report={technicalReport} artifactUrl={(path) => api.publicArtifactUrl(jobId, path)} onNewTask={resetLocal} onDelete={() => void deleteTask()} onRevise={() => void reviseAndRerun()} />
    );
  }

  if (job?.status === "awaiting_confirmation" && plan) {
    return (
      <PrivacyPlanReview locale={locale} plan={plan} previewUrl={api.privateArtifactUrl(jobId, plan.effective_config.preview_identity)} sourceUrl={sourceUrl} busy={busy} onConfirm={() => void confirm()} onCancel={() => void deleteTask()} />
    );
  }

  if (job?.status === "awaiting_review" && riskMap) {
    const selectedInterval = selectedRisk ?? selectedManualVisual;
    const intervalActive = selectedInterval
      ? currentTime >= selectedInterval.start_seconds && currentTime <= selectedInterval.end_seconds
      : false;
    const selectedBox = intervalActive
      ? selectedRisk
        ? editedBoxes[selectedRisk.id] ?? selectedRisk.box
        : selectedManualVisual?.box ?? null
      : null;
    const path = evidencePath(selectedRisk);
    return (
      <main className="privacy-workbench">
        <header className="privacy-workbench-header">
          <div><p className="step-label">{zh ? "本地安全分享" : "SAFE SHARING · LOCAL"}</p><h1>{zh ? "隐私风险复核" : "Privacy risk review"}</h1></div>
          <div><span className="privacy-status-chip">● {zh ? "等待人工决定" : "Awaiting decisions"}</span><button type="button" className="danger-button" onClick={() => void deleteTask()}>{zh ? "取消任务" : "Cancel task"}</button></div>
        </header>
        {job?.warnings.length ? <div className="privacy-warning" role="alert"><strong>{zh ? "扫描器提醒" : "Scanner notice"}</strong><ul>{job.warnings.map((warning) => <li key={warning}>{privacyServerText(warning, locale, "scanner_warning", warning.split(/\s/, 1)[0] || "scanner")}</li>)}</ul></div> : null}
        <p className="privacy-mobile-notice">{zh ? "精确逐帧区域编辑建议使用桌面端" : "Precise frame-by-frame region editing works best on desktop"}</p>
        <div className="privacy-review-layout">
          <section className="privacy-editor-column">
            <PrivacyOverlayEditor locale={locale} box={selectedBox} evidenceUrl={path ? api.privateArtifactUrl(jobId, path) : null} sourceUrl={sourceUrl} videoRef={videoRef} currentTime={currentTime} onTimeChange={setCurrentTime} onBoxChange={(box) => {
              if (selectedRisk) setEditedBoxes((current) => ({ ...current, [selectedRisk.id]: box }));
              else if (selectedManualVisualId) setManualVisuals((current) => current.map((item) => item.clientId === selectedManualVisualId ? { ...item, box } : item));
            }} />
            <PrivacyTimeline locale={locale} duration={riskMap.duration_seconds} currentTime={currentTime} risks={riskMap.risks} audioIntervals={audioIntervals} selectedRiskId={selectedRiskId} onSeek={seek} onSelectRisk={chooseRisk} />
            <section className="privacy-manual-tools">
              <div><p className="step-label">{zh ? "人工补充" : "MANUAL FALLBACK"}</p><h2>{zh ? "补充扫描器没有覆盖的区域" : "Add what the scanners may have missed"}</h2></div>
              <div className="privacy-tool-row"><button type="button" className="secondary-button" onClick={addManualVisual}>{zh ? "添加视觉区域" : "Add visual region"}</button><span>{manualVisuals.length} {zh ? "个区域" : "regions"}</span></div>
              {manualVisuals.length > 0 && (
                <div className="privacy-manual-list">
                  {manualVisuals.map((item, index) => (
                    <button
                      key={item.clientId}
                      type="button"
                      data-manual-id={item.clientId}
                      className={selectedManualVisualId === item.clientId ? "is-selected" : ""}
                      aria-label={`${zh ? "编辑人工视觉区域" : "Edit manual visual region"} ${index + 1}`}
                      onClick={() => {
                        setSelectedRiskId(null);
                        setSelectedManualVisualId(item.clientId);
                        seek(item.start_seconds);
                      }}
                    >
                      {zh ? `区域 ${index + 1}` : `Region ${index + 1}`}
                    </button>
                  ))}
                </div>
              )}
              {selectedManualVisual && (
                <div className="privacy-manual-editor">
                  <label>
                    {zh ? "视觉开始（秒）" : "Visual start (seconds)"}
                    <input aria-label={zh ? "视觉开始（秒）" : "Visual start (seconds)"} type="number" min="0" step="0.1" value={selectedManualVisual.start_seconds} onChange={(event) => { const value = Number(event.target.value); setManualVisuals((current) => current.map((item) => item.clientId === selectedManualVisual.clientId ? { ...item, start_seconds: value } : item)); if (Number.isFinite(value)) seek(value); }} />
                  </label>
                  <label>
                    {zh ? "视觉结束（秒）" : "Visual end (seconds)"}
                    <input aria-label={zh ? "视觉结束（秒）" : "Visual end (seconds)"} type="number" min="0" step="0.1" value={selectedManualVisual.end_seconds} onChange={(event) => setManualVisuals((current) => current.map((item) => item.clientId === selectedManualVisual.clientId ? { ...item, end_seconds: Number(event.target.value) } : item))} />
                  </label>
                  <label>
                    {zh ? "视觉处理方式" : "Visual style"}
                    <select aria-label={zh ? "视觉处理方式" : "Visual style"} value={selectedManualVisual.style} onChange={(event) => setManualVisuals((current) => current.map((item) => item.clientId === selectedManualVisual.clientId ? { ...item, style: event.target.value as ManualVisualRegion["style"] } : item))}>
                      <option value="blur">{zh ? "模糊" : "Blur"}</option>
                      <option value="pixelate">{zh ? "像素化" : "Pixelate"}</option>
                      <option value="solid_fill">{zh ? "纯色遮挡" : "Solid fill"}</option>
                    </select>
                  </label>
                  <button type="button" className="danger-button" onClick={() => {
                    const id = selectedManualVisual.clientId;
                    setManualVisuals((current) => current.filter((item) => item.clientId !== id));
                    setSelectedManualVisualId(null);
                  }}>{zh ? "移除所选视觉区域" : "Remove selected visual region"}</button>
                </div>
              )}
              <div className="privacy-audio-editor">
                <label htmlFor="privacy-audio-start">{zh ? "新静音开始（秒）" : "New mute start (seconds)"}</label>
                {!zh && <label className="visually-hidden" htmlFor="privacy-audio-start">Audio start (seconds)</label>}
                <input id="privacy-audio-start" type="number" min="0" step="0.1" value={audioStart} onChange={(event) => setAudioStart(event.target.value)} />
                <label htmlFor="privacy-audio-end">{zh ? "新静音结束（秒）" : "New mute end (seconds)"}</label>
                {!zh && <label className="visually-hidden" htmlFor="privacy-audio-end">Audio end (seconds)</label>}
                <input id="privacy-audio-end" type="number" min="0" step="0.1" value={audioEnd} onChange={(event) => setAudioEnd(event.target.value)} />
                <button type="button" className="secondary-button" onClick={addAudio}>{zh ? "添加音频静音区间" : "Add audio mute interval"}</button>
              </div>
              {audioIntervals.map((item, index) => (
                <div className="privacy-audio-editor" key={item.clientId}>
                  <label>{zh ? `静音区间 ${index + 1} 开始（秒）` : `Mute interval ${index + 1} start (seconds)`}<input aria-label={zh ? `静音区间 ${index + 1} 开始（秒）` : `Mute interval ${index + 1} start (seconds)`} type="number" min="0" step="0.1" value={item.start_seconds} onChange={(event) => setAudioIntervals((current) => current.map((candidate) => candidate.clientId === item.clientId ? { ...candidate, start_seconds: Number(event.target.value) } : candidate))} /></label>
                  <label>{zh ? `静音区间 ${index + 1} 结束（秒）` : `Mute interval ${index + 1} end (seconds)`}<input aria-label={zh ? `静音区间 ${index + 1} 结束（秒）` : `Mute interval ${index + 1} end (seconds)`} type="number" min="0" step="0.1" value={item.end_seconds} onChange={(event) => setAudioIntervals((current) => current.map((candidate) => candidate.clientId === item.clientId ? { ...candidate, end_seconds: Number(event.target.value) } : candidate))} /></label>
                  <button type="button" className="danger-button" aria-label={zh ? `移除静音区间 ${index + 1}` : `Remove mute interval ${index + 1}`} onClick={() => setAudioIntervals((current) => current.filter((candidate) => candidate.clientId !== item.clientId))}>{zh ? "移除" : "Remove"}</button>
                </div>
              ))}
            </section>
          </section>
          <aside className="privacy-review-drawer">
            {riskMap.risks.length === 0 ? <div className="privacy-empty"><strong>{zh ? "未提出自动风险" : "No automatic risks were proposed"}</strong><p>{zh ? "这不表示视频绝对安全。请使用人工区域和音频区间完成复核。" : "This does not mean the video is risk-free. Add manual visual or audio intervals before sharing."}</p></div> : <PrivacyRiskList locale={locale} risks={riskMap.risks} selectedRiskId={selectedRiskId} decisions={decisions} onSelect={chooseRisk} />}
            {selectedRisk && <section className="privacy-risk-detail"><p className="step-label">{selectedRisk.scanner_id}</p><h2>{zh ? privacyIdentifierText("risk", selectedRisk.risk_type, locale) : selectedRisk.title}</h2><p>{privacyServerText(selectedRisk.public_description, locale, "risk_description", selectedRisk.scanner_id)}</p><div className="privacy-confidence"><span>{zh ? "置信度" : "Confidence"}</span><strong>{Math.round(selectedRisk.confidence * 100)}%</strong></div><div className="privacy-decision-controls" role="group" aria-label={zh ? "风险决定" : "Risk decision"}><button type="button" className={decisions[selectedRisk.id] === "allow" ? "is-active" : ""} onClick={() => decide("allow")}>{zh ? "允许" : "Allow"}</button><button type="button" className={decisions[selectedRisk.id] === "redact" ? "is-active" : ""} onClick={() => decide("redact")}>{zh ? "脱敏" : "Redact"}</button></div>{decisions[selectedRisk.id] === "redact" && <label className="privacy-style-field">{zh ? "处理方式" : "Redaction style"}<select value={styles[selectedRisk.id] ?? styleForRisk(selectedRisk)} onChange={(event) => setStyles((current) => ({ ...current, [selectedRisk.id]: event.target.value as RedactionStyle }))}>{selectedRisk.risk_type === "metadata" ? <option value="remove_metadata">{zh ? "移除元数据" : "Remove metadata"}</option> : <><option value="blur">{zh ? "模糊" : "Blur"}</option><option value="pixelate">{zh ? "像素化" : "Pixelate"}</option><option value="solid_fill">{zh ? "纯色遮挡" : "Solid fill"}</option></>}</select></label>}<details><summary>{zh ? "限制说明" : "Limitations"}</summary><ul>{selectedRisk.limitations.map((item, index) => <li key={`${selectedRisk.scanner_id}-${index}`}>{zh ? `启发式扫描可能误报或漏报（${selectedRisk.scanner_id}）` : item}</li>)}</ul></details></section>}
            {error && <p className="form-error" role="alert">{error}</p>}
            <button type="button" className="primary-button privacy-generate" disabled={busy} onClick={() => void generatePreview()}>{busy ? (zh ? "正在生成…" : "Generating…") : zh ? "生成预览" : "Generate preview"}</button>
          </aside>
        </div>
      </main>
    );
  }

  return (
    <main className="privacy-shell privacy-progress-shell">
      <section className="privacy-progress-card">
        <p className="eyebrow">{zh ? "本地处理进行中" : "LOCAL PROCESSING"}</p>
        <h1>
          {job
            ? job.message?.toLowerCase().includes("cancel")
              ? privacyServerText(job.message, locale, "verification", "job_status")
              : privacyStatusText(job.status, locale)
            : zh
              ? "正在恢复任务…"
              : "Restoring task…"}
        </h1>
        <div className="progress-track" aria-label={zh ? "任务进度" : "Task progress"}><span style={{ width: `${job?.progress_percent ?? 0}%` }} /></div>
        <strong>{job?.progress_percent ?? 0}%</strong>
        <p>{zh ? "视频不会上传。扫描器失败会明确显示，不会伪装成零风险。" : "The video is not uploaded. Scanner failures remain visible instead of becoming an empty risk list."}</p>
        {error && <p className="form-error" role="alert">{error}</p>}
        {job && !stageUnavailable && job.status !== "awaiting_confirmation" && job.status !== "awaiting_review" && (
          <button type="button" className="danger-button" onClick={() => void deleteTask()} disabled={busy}>{zh ? "取消任务" : "Cancel task"}</button>
        )}
      </section>
    </main>
  );
}
