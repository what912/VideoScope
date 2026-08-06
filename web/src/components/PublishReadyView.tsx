import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  cancelPublishJob,
  confirmPublishJob,
  createPublishJob,
  getPublishJob,
  getPublishPlan,
  getPublishTechnicalReport,
  listPublishProfiles,
  publishArtifactUrl,
  subscribeToPublishEvents,
  type JobEventSubscription,
} from "../api";
import {
  capturePublishError,
  presentPublishError,
  presentPublishStatus,
  type PublishPresentationError,
  type PresentationLocale,
} from "../publishPresentation";
import type {
  PublishJobEvent,
  PublishJobResponse,
  PublishJobStatus,
  PublishPlan,
  PublishProfile,
  PublishProfileId,
  PublishTechnicalReport,
} from "../types";
import {
  PublishPlanReview,
  type PlanReviewCopy,
} from "./PublishPlanReview";
import { PublishPreview, type PreviewCopy } from "./PublishPreview";
import {
  PublishProfileSelector,
  type ProfileSelectorCopy,
} from "./PublishProfileSelector";
import { PublishResult, type ResultCopy } from "./PublishResult";

export type WorkbenchLocale = PresentationLocale;

interface PublishCopy {
  eyebrow: string;
  title: string;
  intro: string;
  localStatement: string;
  sourceStatement: string;
  copyStatement: string;
  boundariesLabel: string;
  chooseVideo: string;
  dropVideo: string;
  chooseHint: string;
  fileReady: string;
  inspect: string;
  loadingProfiles: string;
  stages: Record<"inspect" | "plan" | "confirm" | "process" | "verify", string>;
  currentStatus: string;
  cancel: string;
  confirmHeading: string;
  confirmCopy: string;
  confirm: string;
  confirming: string;
  profile: ProfileSelectorCopy;
  plan: PlanReviewCopy;
  preview: PreviewCopy;
  result: ResultCopy;
}

const COPY: Record<WorkbenchLocale, PublishCopy> = {
  en: {
    eyebrow: "Local Resolve workflow",
    title: "Publish Ready",
    intro:
      "Prepare a versioned compatibility plan, inspect its six-second preview, then explicitly confirm local processing.",
    localStatement: "Processing stays in the loopback local service.",
    sourceStatement: "The original is never overwritten.",
    copyStatement:
      "The browser upload is copied only into the configured local job directory.",
    boundariesLabel: "Publish Ready boundaries",
    chooseVideo: "Choose a local video for Publish Ready",
    dropVideo: "Choose a video to inspect",
    chooseHint: "MP4 · MOV · MKV · WEBM",
    fileReady: "ready for local inspection",
    inspect: "Inspect and plan",
    loadingProfiles: "Reading local Publish Ready profiles…",
    stages: {
      inspect: "Inspect source",
      plan: "Build plan",
      confirm: "Review & confirm",
      process: "Process locally",
      verify: "Verifying output",
    },
    currentStatus: "Current status",
    cancel: "Cancel publish job",
    confirmHeading: "Confirm this exact plan",
    confirmCopy:
      "Confirmation starts native local processing. No final output is written before this step.",
    confirm: "Confirm and process",
    confirming: "Confirmation submitted",
    profile: {
      legend: "Choose one versioned output profile",
      compatible: "Compatible MP4",
      compatibleDescription: "Preserve source dimensions in an H.264/AAC MP4.",
      vertical: "Vertical 9:16",
      horizontal: "Horizontal 16:9",
      scalePadDescription:
        "Scale and pad to the target canvas; the source frame is never cropped.",
      canvas: "Canvas",
      preserve: "Source size",
    },
    plan: {
      sourceHeading: "Source metadata",
      planHeading: "Ordered plan",
      dimensions: "Dimensions",
      duration: "Duration",
      codec: "Video codec",
      audio: "Audio",
      yes: "Present",
      no: "None",
      output: "Separate output",
      planVersion: "Schema",
      actionLabel: "plan action",
    },
    preview: {
      heading: "Before / planned output",
      description:
        "This six-second preview is prepared locally from the exact plan above.",
      source: "Source preview",
      output: "Planned output preview",
      unavailable: "Re-select the local source to restore its browser preview.",
    },
    result: {
      productLabel: "Publish Ready",
      completed: "Publish Ready",
      needsReview: "Needs review",
      failed: "Publish failed",
      cancelled: "Publish cancelled",
      passedDescription:
        "The separate output exists and passed every versioned profile check.",
      reviewDescription:
        "The output exists, but at least one check requires human review. It is not marked Publish Ready.",
      failedDescription:
        "No verified Publish Ready result is available. Review the local error below.",
      cancelledDescription: "Processing stopped before a verified result was published.",
      checks: "Verification checks",
      reviewReasons: "Manual review reasons",
      artifacts: "Local artifacts",
      download: "Download",
      loadingReport: "Reading the local technical report…",
      statusPassed: "passed",
      statusNeedsReview: "needs review",
      statusFailed: "failed",
      newPublish: "New Publish",
    },
  },
  "zh-CN": {
    eyebrow: "本地 Resolve 工作流",
    title: "发布就绪",
    intro: "先生成版本化兼容计划并检查六秒预览，再明确确认本地处理。",
    localStatement: "所有处理均保留在环回本地服务中。",
    sourceStatement: "工作流不会覆盖源文件。",
    copyStatement: "浏览器上传只会复制到配置的本地任务目录。",
    boundariesLabel: "发布就绪边界",
    chooseVideo: "为发布就绪选择本地视频",
    dropVideo: "选择要检查的视频",
    chooseHint: "MP4 · MOV · MKV · WEBM",
    fileReady: "已准备进行本地检查",
    inspect: "检查并制定计划",
    loadingProfiles: "正在读取本地发布 Profile…",
    stages: {
      inspect: "检查源文件",
      plan: "生成计划",
      confirm: "复核并确认",
      process: "本地处理",
      verify: "验证输出",
    },
    currentStatus: "当前状态",
    cancel: "取消发布任务",
    confirmHeading: "确认此精确计划",
    confirmCopy: "确认后才会启动原生本地处理；在此之前不会写入最终输出。",
    confirm: "确认并处理",
    confirming: "已提交确认",
    profile: {
      legend: "选择一个版本化输出 Profile",
      compatible: "兼容 MP4",
      compatibleDescription: "保留源尺寸，输出 H.264/AAC MP4。",
      vertical: "竖屏 9:16",
      horizontal: "横屏 16:9",
      scalePadDescription: "缩放并留边到目标画布，绝不裁剪源画面。",
      canvas: "画布",
      preserve: "源尺寸",
    },
    plan: {
      sourceHeading: "源文件元数据",
      planHeading: "有序计划",
      dimensions: "尺寸",
      duration: "时长",
      codec: "视频编码",
      audio: "音频",
      yes: "有",
      no: "无",
      output: "独立输出",
      planVersion: "模式",
      actionLabel: "计划动作",
    },
    preview: {
      heading: "处理前 / 计划输出",
      description: "此六秒预览由上述精确计划在本地生成。",
      source: "源视频预览",
      output: "计划输出预览",
      unavailable: "请重新选择本地源文件以恢复浏览器预览。",
    },
    result: {
      productLabel: "发布就绪",
      completed: "发布就绪",
      needsReview: "需要复核",
      failed: "发布失败",
      cancelled: "发布已取消",
      passedDescription: "独立输出已生成，并通过全部版本化 Profile 检查。",
      reviewDescription: "输出已生成，但至少一项检查需要人工复核，不能标记为发布就绪。",
      failedDescription: "没有可用的已验证发布结果，请检查下方本地错误。",
      cancelledDescription: "在发布已验证结果前，处理已停止。",
      checks: "验证检查",
      reviewReasons: "人工复核原因",
      artifacts: "本地产物",
      download: "下载",
      loadingReport: "正在读取本地技术报告…",
      statusPassed: "通过",
      statusNeedsReview: "需要复核",
      statusFailed: "失败",
      newPublish: "新建发布",
    },
  },
};

export interface PublishReadyApi {
  listPublishProfiles(): Promise<PublishProfile[]>;
  createPublishJob(
    file: File,
    profileId: PublishProfileId,
  ): Promise<PublishJobResponse>;
  getPublishJob(jobId: string): Promise<PublishJobResponse>;
  getPublishPlan(jobId: string): Promise<PublishPlan>;
  confirmPublishJob(
    jobId: string,
    planDigest: string,
  ): Promise<PublishJobResponse>;
  cancelPublishJob(jobId: string): Promise<PublishJobResponse | null>;
  getPublishTechnicalReport(jobId: string): Promise<PublishTechnicalReport>;
  subscribeToPublishEvents(
    jobId: string,
    onEvent: (event: PublishJobEvent) => void,
    onError: (error: Error) => void,
  ): JobEventSubscription;
  publishArtifactUrl(jobId: string, path: string): string;
}

const DEFAULT_API: PublishReadyApi = {
  listPublishProfiles,
  createPublishJob,
  getPublishJob,
  getPublishPlan,
  confirmPublishJob,
  cancelPublishJob,
  getPublishTechnicalReport,
  subscribeToPublishEvents,
  publishArtifactUrl,
};

const TERMINAL = new Set<PublishJobStatus>([
  "completed",
  "needs_review",
  "failed",
  "cancelled",
]);
const PLAN_AVAILABLE = new Set<PublishJobStatus>([
  "awaiting_confirmation",
  "processing",
  "verifying",
  "completed",
  "needs_review",
]);
const STAGES: Array<keyof PublishCopy["stages"]> = [
  "inspect",
  "plan",
  "confirm",
  "process",
  "verify",
];
const STAGE_INDEX: Record<PublishJobStatus, number> = {
  queued: 0,
  inspecting: 0,
  planning: 1,
  awaiting_confirmation: 2,
  processing: 3,
  verifying: 4,
  completed: 5,
  needs_review: 5,
  failed: 5,
  cancelled: 5,
};

interface Props {
  locale: WorkbenchLocale;
  initialJobId?: string | null;
  api?: PublishReadyApi;
  onJobIdChange?: (jobId: string | null) => void;
}

export function PublishReadyView({
  locale,
  initialJobId = null,
  api = DEFAULT_API,
  onJobIdChange,
}: Props): React.JSX.Element {
  const copy = COPY[locale];
  const [profiles, setProfiles] = useState<PublishProfile[]>([]);
  const [selectedProfile, setSelectedProfile] =
    useState<PublishProfileId>("compatible_mp4");
  const [file, setFile] = useState<File | null>(null);
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [job, setJob] = useState<PublishJobResponse | null>(null);
  const [plan, setPlan] = useState<PublishPlan | null>(null);
  const [technicalReport, setTechnicalReport] =
    useState<PublishTechnicalReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [confirmationSubmitted, setConfirmationSubmitted] = useState(false);
  const [error, setError] = useState<PublishPresentationError | null>(null);
  const restoredId = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    void api
      .listPublishProfiles()
      .then((loaded) => {
        if (active) setProfiles(loaded);
      })
      .catch((caught: unknown) => {
        if (active) setError(capturePublishError(caught));
      });
    return () => {
      active = false;
    };
  }, [api]);

  useEffect(() => {
    if (!initialJobId || restoredId.current === initialJobId) return;
    restoredId.current = initialJobId;
    let active = true;
    setLoading(true);
    void api
      .getPublishJob(initialJobId)
      .then((restored) => {
        if (active) setJob(restored);
      })
      .catch((caught: unknown) => {
        if (active) setError(capturePublishError(caught));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [api, initialJobId]);

  useEffect(() => {
    if (!job || plan || !PLAN_AVAILABLE.has(job.status)) return;
    let active = true;
    void api
      .getPublishPlan(job.job_id)
      .then((loaded) => {
        if (active) setPlan(loaded);
      })
      .catch((caught: unknown) => {
        if (active) setError(capturePublishError(caught));
      });
    return () => {
      active = false;
    };
  }, [api, job, plan]);

  useEffect(() => {
    if (
      !job ||
      technicalReport ||
      (job.status !== "completed" && job.status !== "needs_review")
    ) {
      return;
    }
    let active = true;
    void api
      .getPublishTechnicalReport(job.job_id)
      .then((loaded) => {
        if (active) setTechnicalReport(loaded);
      })
      .catch((caught: unknown) => {
        if (active) setError(capturePublishError(caught));
      });
    return () => {
      active = false;
    };
  }, [api, job, technicalReport]);

  const subscriptionJobId = job?.job_id ?? null;

  useEffect(() => {
    if (!subscriptionJobId || !job || TERMINAL.has(job.status)) return;
    let active = true;
    let lastSequence = 0;
    const subscription = api.subscribeToPublishEvents(
      subscriptionJobId,
      (event) => {
        if (!active || event.sequence <= lastSequence) return;
        lastSequence = event.sequence;
        setJob((current) =>
          current?.job_id === subscriptionJobId
            ? {
                ...current,
                status: event.status,
                message: event.message,
                progress_percent: Math.max(
                  current.progress_percent,
                  event.progress_percent,
                ),
                updated_at: event.created_at,
              }
            : current,
        );
        if (TERMINAL.has(event.status)) {
          const terminalSequence = event.sequence;
          void api
            .getPublishJob(subscriptionJobId)
            .then((latest) => {
              if (!active || lastSequence !== terminalSequence) return;
              setJob((current) =>
                current?.job_id === subscriptionJobId ? latest : current,
              );
            })
            .catch((caught: unknown) => {
              if (active) setError(capturePublishError(caught));
            });
        }
      },
      (caught) => {
        if (active) setError(capturePublishError(caught));
      },
    );
    return () => {
      active = false;
      subscription.close();
    };
  }, [api, subscriptionJobId]);

  useEffect(
    () => () => {
      if (sourceUrl?.startsWith("blob:")) URL.revokeObjectURL(sourceUrl);
    },
    [sourceUrl],
  );

  const previewUrl = useMemo(
    () =>
      job
        ? api.publishArtifactUrl(job.job_id, "preview/publish-preview.mp4")
        : "",
    [api, job],
  );

  const selectFile = (nextFile: File | null): void => {
    if (!nextFile) return;
    if (sourceUrl?.startsWith("blob:")) URL.revokeObjectURL(sourceUrl);
    setFile(nextFile);
    setSourceUrl(
      typeof URL.createObjectURL === "function"
        ? URL.createObjectURL(nextFile)
        : "about:blank#local-source-preview",
    );
  };

  const submit = async (event: FormEvent): Promise<void> => {
    event.preventDefault();
    if (!file) return;
    setLoading(true);
    setError(null);
    setPlan(null);
    setTechnicalReport(null);
    setConfirmationSubmitted(false);
    try {
      const created = await api.createPublishJob(file, selectedProfile);
      setJob(created);
      onJobIdChange?.(created.job_id);
    } catch (caught) {
      setError(capturePublishError(caught));
    } finally {
      setLoading(false);
    }
  };

  const confirm = async (): Promise<void> => {
    if (!job || !plan || confirmationSubmitted) return;
    setConfirmationSubmitted(true);
    setError(null);
    try {
      setJob(await api.confirmPublishJob(job.job_id, plan.plan_digest));
    } catch (caught) {
      setConfirmationSubmitted(false);
      setError(capturePublishError(caught));
    }
  };

  const cancel = async (): Promise<void> => {
    if (!job || TERMINAL.has(job.status)) return;
    setError(null);
    try {
      const cancelled = await api.cancelPublishJob(job.job_id);
      if (cancelled) setJob(cancelled);
    } catch (caught) {
      setError(capturePublishError(caught));
    }
  };

  const reset = (): void => {
    if (sourceUrl?.startsWith("blob:")) URL.revokeObjectURL(sourceUrl);
    restoredId.current = null;
    setSelectedProfile("compatible_mp4");
    setFile(null);
    setSourceUrl(null);
    setJob(null);
    setPlan(null);
    setTechnicalReport(null);
    setLoading(false);
    setConfirmationSubmitted(false);
    setError(null);
    onJobIdChange?.(null);
  };

  const displayedError = error ? presentPublishError(locale, error) : null;

  if (job && TERMINAL.has(job.status)) {
    return (
      <>
        {displayedError && (
          <p className="publish-global-error form-error">{displayedError}</p>
        )}
        <PublishResult
          job={job}
          report={technicalReport}
          copy={copy.result}
          artifactUrl={api.publishArtifactUrl}
          locale={locale}
          onNewPublish={reset}
        />
      </>
    );
  }

  return (
    <main className="publish-shell">
      <section className="publish-hero">
        <p className="eyebrow">{copy.eyebrow}</p>
        <h1>{copy.title}</h1>
        <p className="hero-copy">{copy.intro}</p>
        <div className="publish-boundaries" aria-label={copy.boundariesLabel}>
          <span>{copy.localStatement}</span>
          <span>{copy.sourceStatement}</span>
          <span>{copy.copyStatement}</span>
        </div>
      </section>

      {!job && (
        <form className="publish-start-card" onSubmit={(event) => void submit(event)}>
          <label className={`publish-file ${file ? "has-file" : ""}`}>
            <input
              className="visually-hidden"
              type="file"
              accept="video/*,.mkv"
              aria-label={copy.chooseVideo}
              onChange={(event) => selectFile(event.target.files?.[0] ?? null)}
            />
            <span aria-hidden="true">↑</span>
            <strong>{file?.name ?? copy.dropVideo}</strong>
            <small>{file ? copy.fileReady : copy.chooseHint}</small>
          </label>
          {profiles.length === 0 ? (
            <p className="muted">{copy.loadingProfiles}</p>
          ) : (
            <PublishProfileSelector
              profiles={profiles}
              selected={selectedProfile}
              copy={copy.profile}
              disabled={loading}
              onChange={setSelectedProfile}
            />
          )}
          {displayedError && <p className="form-error">{displayedError}</p>}
          <button
            className="primary-button"
            type="submit"
            disabled={!file || profiles.length !== 3 || loading}
          >
            <span>{copy.inspect}</span>
            <span aria-hidden="true">→</span>
          </button>
        </form>
      )}

      {job && (
        <>
          <PublishProgress job={job} copy={copy} locale={locale} />
          {plan && (
            <>
              <PublishPlanReview plan={plan} copy={copy.plan} locale={locale} />
              <PublishPreview
                sourceUrl={sourceUrl}
                previewUrl={previewUrl}
                copy={copy.preview}
              />
              {job.status === "awaiting_confirmation" && (
                <section className="publish-confirm" aria-labelledby="confirm-heading">
                  <div>
                    <p className="step-label">05 / {copy.confirmHeading}</p>
                    <h2 id="confirm-heading">{copy.confirmHeading}</h2>
                    <p>{copy.confirmCopy}</p>
                  </div>
                  <button
                    className="primary-button compact"
                    type="button"
                    disabled={confirmationSubmitted}
                    onClick={() => void confirm()}
                  >
                    {confirmationSubmitted ? copy.confirming : copy.confirm}
                  </button>
                </section>
              )}
            </>
          )}
          {displayedError && <p className="form-error">{displayedError}</p>}
          <button
            className="secondary-button danger-button publish-cancel"
            type="button"
            onClick={() => void cancel()}
          >
            {copy.cancel}
          </button>
        </>
      )}
    </main>
  );
}

function PublishProgress({
  job,
  copy,
  locale,
}: {
  job: PublishJobResponse;
  copy: PublishCopy;
  locale: WorkbenchLocale;
}): React.JSX.Element {
  const activeIndex = STAGE_INDEX[job.status];
  return (
    <section className="publish-progress" aria-live="polite">
      <div className="publish-progress-heading">
        <div>
          <p className="step-label">01 / {copy.currentStatus}</p>
          <h2>{copy.stages[STAGES[Math.min(activeIndex, STAGES.length - 1)]]}</h2>
        </div>
        <strong>{job.progress_percent}%</strong>
      </div>
      <div
        className="progress-track"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={job.progress_percent}
        aria-label={copy.currentStatus}
      >
        <span style={{ width: `${job.progress_percent}%` }} />
      </div>
      <ol className="publish-stage-list">
        {STAGES.map((stage, index) => (
          <li
            key={stage}
            className={index < activeIndex ? "is-complete" : index === activeIndex ? "is-current" : ""}
          >
            <span aria-hidden="true">{index < activeIndex ? "✓" : index + 1}</span>
            <strong>{copy.stages[stage]}</strong>
          </li>
        ))}
      </ol>
      <p>{presentPublishStatus(locale, job.status, job.message)}</p>
    </section>
  );
}
