import { useCallback, useEffect, useMemo, useState } from "react";
import {
  cancelJob,
  createJob,
  getJob,
  getReport,
  listDetectors,
  subscribeToJobEvents,
  videoUrl,
} from "./api";
import { Header } from "./components/Header";
import { ProgressView } from "./components/ProgressView";
import {
  PublishReadyView,
  type WorkbenchLocale,
} from "./components/PublishReadyView";
import { PrivacyView } from "./components/PrivacyView";
import { RescueView } from "./components/RescueView";
import { ContentView } from "./components/ContentView";
import { ReportView } from "./components/ReportView";
import { UploadPanel } from "./components/UploadPanel";
import mockReportData from "./mocks/mock-report.json";
import type {
  AnalysisOptions,
  AnalysisReport,
  DetectorManifest,
  JobResponse,
  JobStatus,
} from "./types";

const MOCK_DETECTORS: DetectorManifest[] = [
  {
    id: "near_black",
    display_name: "Near-black intervals",
    version: "1.0.0",
    description: "Sustained frames with near-black luminance.",
    default_enabled: true,
    requires_prompt: false,
    requires_gpu: false,
    requires_network: false,
    optional_packages: [],
    estimated_cost: "low",
    category: "cpu",
    available: true,
    unavailable_reason: null,
  },
  {
    id: "possible_freeze",
    display_name: "Possible freeze",
    version: "1.0.0",
    description: "Repeated or structurally near-identical frames.",
    default_enabled: true,
    requires_prompt: false,
    requires_gpu: false,
    requires_network: false,
    optional_packages: [],
    estimated_cost: "low",
    category: "cpu",
    available: true,
    unavailable_reason: null,
  },
  {
    id: "scene_relative_blur",
    display_name: "Relative sharpness",
    version: "1.0.0",
    description: "Scene-relative sharpness drops over time.",
    default_enabled: true,
    requires_prompt: false,
    requires_gpu: false,
    requires_network: false,
    optional_packages: [],
    estimated_cost: "low",
    category: "cpu",
    available: true,
    unavailable_reason: null,
  },
  {
    id: "global_flicker",
    display_name: "Global flicker",
    version: "1.0.0",
    description: "High-frequency global luminance variation.",
    default_enabled: true,
    requires_prompt: false,
    requires_gpu: false,
    requires_network: false,
    optional_packages: [],
    estimated_cost: "low",
    category: "cpu",
    available: true,
    unavailable_reason: null,
  },
  {
    id: "prompt_alignment",
    display_name: "Prompt alignment",
    version: "1.0.0",
    description: "Optional prompt-to-frame similarity diagnostics.",
    default_enabled: false,
    requires_prompt: true,
    requires_gpu: false,
    requires_network: false,
    optional_packages: ["open-clip-torch", "torch"],
    estimated_cost: "high",
    category: "ai",
    available: false,
    unavailable_reason: "Install genvideoscope[ai] and local model weights.",
  },
  {
    id: "visual_semantic_drift",
    display_name: "Visual semantic drift",
    version: "1.0.0",
    description: "Scene-local visual embedding discontinuities.",
    default_enabled: false,
    requires_prompt: false,
    requires_gpu: false,
    requires_network: false,
    optional_packages: ["torch", "torchvision"],
    estimated_cost: "high",
    category: "ai",
    available: false,
    unavailable_reason: "Install genvideoscope[ai] and local model weights.",
  },
  {
    id: "text_stability",
    display_name: "Text stability",
    version: "1.0.0",
    description: "Optional temporal OCR stability diagnostics.",
    default_enabled: false,
    requires_prompt: false,
    requires_gpu: false,
    requires_network: false,
    optional_packages: ["paddleocr", "paddlepaddle"],
    estimated_cost: "high",
    category: "ocr",
    available: false,
    unavailable_reason: "Install genvideoscope[ocr] and local OCR weights.",
  },
];

const TERMINAL = new Set<JobStatus>(["completed", "failed", "cancelled"]);

type WorkbenchMode = "analyze" | "publish" | "rescue" | "content" | "privacy";

function initialLocale(): WorkbenchLocale {
  const stored = window.localStorage.getItem("videoscope-locale");
  if (stored === "en" || stored === "zh-CN") return stored;
  return window.navigator.language.toLowerCase().startsWith("zh")
    ? "zh-CN"
    : "en";
}

function replaceQueryParam(name: string, value: string | null): void {
  const next = new URLSearchParams(window.location.search);
  if (value === null) next.delete(name);
  else next.set(name, value);
  const suffix = next.toString();
  window.history.replaceState(null, "", suffix ? `?${suffix}` : "/");
}

function mockJob(status: JobStatus, progress: number, message: string): JobResponse {
  return {
    job_id: "mock-dashboard",
    status,
    message,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    upload_size_bytes: 350348,
    progress_percent: progress,
    current_detector:
      status === "detecting" ? "scene_relative_blur" : null,
    warnings: [],
    error: null,
    links: {},
  };
}

export default function App(): React.JSX.Element {
  const query = useMemo(() => new URLSearchParams(window.location.search), []);
  const mockMode = query.get("mock") === "1";
  const initialJobId = query.get("job");
  const initialPublishJobId = query.get("publishJob");
  const initialPrivacyJobId = query.get("privacyJob");
  const initialRescueJobId = query.get("rescueJob") ?? window.localStorage.getItem("videoscope-rescue-job");
  const initialContentJobId = query.get("contentJob") ?? window.localStorage.getItem("videoscope-content-job");
  const requestedMode = query.get("mode");
  const [publishJobId, setPublishJobId] = useState<string | null>(
    initialPublishJobId,
  );
  const [privacyJobId, setPrivacyJobId] = useState<string | null>(
    initialPrivacyJobId,
  );
  const [rescueJobId, setRescueJobId] = useState<string | null>(initialRescueJobId);
  const [contentJobId, setContentJobId] = useState<string | null>(initialContentJobId);
  const [mode, setMode] = useState<WorkbenchMode>(
    requestedMode === "content" || (requestedMode === null && initialContentJobId)
      ? "content"
      : requestedMode === "rescue" || (requestedMode === null && initialRescueJobId)
      ? "rescue"
      : requestedMode === "privacy" ||
      (requestedMode === null && initialPrivacyJobId)
      ? "privacy"
      : requestedMode === "publish" ||
          (requestedMode === null && initialPublishJobId)
      ? "publish"
      : "analyze",
  );
  const [locale, setLocale] = useState<WorkbenchLocale>(initialLocale);
  const [detectors, setDetectors] = useState<DetectorManifest[]>([]);
  const [loadingDetectors, setLoadingDetectors] = useState(true);
  const [job, setJob] = useState<JobResponse | null>(null);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [videoSource, setVideoSource] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    window.localStorage.setItem("videoscope-locale", locale);
    document.documentElement.lang = locale;
  }, [locale]);

  const switchMode = (nextMode: WorkbenchMode): void => {
    setMode(nextMode);
    const next = new URLSearchParams(window.location.search);
    if (["publish", "privacy", "rescue", "content"].includes(nextMode)) {
      next.set("mode", nextMode);
    } else if (next.has("publishJob") || next.has("privacyJob") || next.has("rescueJob") || next.has("contentJob")) {
      next.set("mode", "analyze");
    }
    else next.delete("mode");
    const suffix = next.toString();
    window.history.replaceState(null, "", suffix ? `?${suffix}` : "/");
  };

  const rememberPublishJob = (jobId: string | null): void => {
    setPublishJobId(jobId);
    replaceQueryParam("publishJob", jobId);
  };

  const rememberPrivacyJob = (jobId: string | null): void => {
    setPrivacyJobId(jobId);
    replaceQueryParam("privacyJob", jobId);
  };
  const rememberRescueJob = (jobId: string | null): void => {
    setRescueJobId(jobId);
    if (jobId) window.localStorage.setItem("videoscope-rescue-job", jobId);
    else window.localStorage.removeItem("videoscope-rescue-job");
    replaceQueryParam("rescueJob", jobId);
  };
  const rememberContentJob = (jobId: string | null): void => {
    setContentJobId(jobId);
    if (jobId) window.localStorage.setItem("videoscope-content-job", jobId);
    else window.localStorage.removeItem("videoscope-content-job");
    replaceQueryParam("contentJob", jobId);
  };

  const showReport = useCallback(
    async (jobId: string): Promise<void> => {
      const loaded = mockMode
        ? (mockReportData as AnalysisReport)
        : await getReport(jobId);
      setReport(loaded);
      if (!mockMode) setVideoSource(videoUrl(jobId));
    },
    [mockMode],
  );

  useEffect(() => {
    let active = true;
    const load = async (): Promise<void> => {
      try {
        const manifest = mockMode ? MOCK_DETECTORS : await listDetectors();
        if (active) setDetectors(manifest);
      } catch (caught) {
        if (active) {
          setError(caught instanceof Error ? caught.message : "Could not load detectors.");
        }
      } finally {
        if (active) setLoadingDetectors(false);
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [mockMode]);

  useEffect(() => {
    if (!initialJobId) return;
    if (mockMode) {
      setJob(mockJob("completed", 100, "Analysis completed"));
      void showReport(initialJobId);
      return;
    }
    let active = true;
    void getJob(initialJobId)
      .then((restored) => {
        if (!active) return;
        setJob(restored);
        if (restored.status === "completed") return showReport(restored.job_id);
        if (TERMINAL.has(restored.status)) {
          setError(restored.error ?? `Job ${restored.status}.`);
        }
      })
      .catch((caught: unknown) => {
        if (active) {
          setError(caught instanceof Error ? caught.message : "Could not restore job.");
        }
      });
    return () => {
      active = false;
    };
  }, [initialJobId, mockMode, showReport]);

  useEffect(() => {
    if (!job || TERMINAL.has(job.status) || mockMode) return;
    const subscription = subscribeToJobEvents(
      job.job_id,
      (event) => {
        void getJob(job.job_id).then((latest) => {
          setJob(latest);
          if (event.status === "completed") void showReport(job.job_id);
          if (event.status === "failed" || event.status === "cancelled") {
            setError(latest.error ?? `Analysis ${event.status}.`);
          }
        });
      },
      (sseError) => setError(sseError.message),
    );
    return () => subscription.close();
  }, [job?.job_id, job?.status, mockMode, showReport]);

  const start = async (
    file: File,
    prompt: string,
    options: AnalysisOptions,
  ): Promise<void> => {
    setError(null);
    setVideoSource(URL.createObjectURL(file));
    try {
      if (mockMode) {
        const states: Array<[JobStatus, number, string]> = [
          ["queued", 4, "Job queued"],
          ["probing", 16, "Probing video metadata"],
          ["sampling", 36, "Sampling analysis frames"],
          ["detecting", 68, "Running detector: scene_relative_blur"],
          ["rendering", 92, "Rendering offline HTML report"],
        ];
        for (const [status, progress, message] of states) {
          setJob(mockJob(status, progress, message));
          await new Promise((resolve) => window.setTimeout(resolve, 260));
        }
        const completed = mockJob("completed", 100, "Analysis completed");
        setJob(completed);
        setReport(mockReportData as AnalysisReport);
        replaceQueryParam("job", "mock-dashboard");
        return;
      }
      const created = await createJob(file, prompt, options);
      setJob(created);
      replaceQueryParam("job", created.job_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not start analysis.");
      setJob(null);
    }
  };

  const cancel = async (): Promise<void> => {
    if (!job) return;
    try {
      const cancelled = mockMode
        ? mockJob("cancelled", 100, "Job cancelled")
        : await cancelJob(job.job_id);
      setJob(cancelled);
      setError("Analysis cancelled.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not cancel analysis.");
    }
  };

  const reset = (): void => {
    if (videoSource?.startsWith("blob:")) URL.revokeObjectURL(videoSource);
    setJob(null);
    setReport(null);
    setVideoSource(null);
    setError(null);
    replaceQueryParam("job", null);
  };

  return (
    <>
      <Header />
      <nav
        className="workbench-nav"
        aria-label={locale === "zh-CN" ? "工作台模式" : "Workbench mode"}
      >
        <div className="mode-switch" role="group">
          <button
            type="button"
            className={mode === "analyze" ? "is-active" : ""}
            aria-pressed={mode === "analyze"}
            onClick={() => switchMode("analyze")}
          >
            {locale === "zh-CN" ? "检查" : "Check"}
          </button>
          <button
            type="button"
            className={mode === "publish" ? "is-active" : ""}
            aria-pressed={mode === "publish"}
            onClick={() => switchMode("publish")}
          >
            {locale === "zh-CN" ? "A 发布就绪" : "A · Publish Ready"}
          </button>
          <button type="button" className={mode === "rescue" ? "is-active" : ""} aria-pressed={mode === "rescue"} onClick={() => switchMode("rescue")}>
            {locale === "zh-CN" ? "B 视频抢救" : "B · Video Rescue"}
          </button>
          <button type="button" className={mode === "content" ? "is-active" : ""} aria-pressed={mode === "content"} onClick={() => switchMode("content")}>
            {locale === "zh-CN" ? "C 有用内容" : "C · Useful Content"}
          </button>
          <button
            type="button"
            className={mode === "privacy" ? "is-active" : ""}
            aria-pressed={mode === "privacy"}
            onClick={() => switchMode("privacy")}
          >
            {locale === "zh-CN" ? "D 安全分享" : "D · Safe Sharing"}
          </button>
        </div>
        <div className="workbench-meta">
          <span className="creator-mark">what912</span>
          <button
            className="language-switch"
            type="button"
            onClick={() => setLocale(locale === "en" ? "zh-CN" : "en")}
            aria-label={
              locale === "en" ? "切换到简体中文" : "Switch to English"
            }
          >
            {locale === "en" ? "中文" : "EN"}
          </button>
        </div>
      </nav>
      {mode === "content" ? (
        <ContentView locale={locale} initialJobId={contentJobId} onJobChange={rememberContentJob} />
      ) : mode === "rescue" ? (
        <RescueView locale={locale} onLocaleChange={setLocale} initialJobId={rescueJobId} onJobChange={rememberRescueJob} />
      ) : mode === "publish" ? (
        <PublishReadyView
          locale={locale}
          initialJobId={publishJobId}
          onJobIdChange={rememberPublishJob}
        />
      ) : mode === "privacy" ? (
        <PrivacyView
          locale={locale}
          initialJobId={privacyJobId}
          onJobChange={rememberPrivacyJob}
        />
      ) : report && job ? (
        <ReportView
          jobId={job.job_id}
          report={report}
          videoSource={videoSource}
          mockMode={mockMode}
          onNewAnalysis={reset}
        />
      ) : job && !TERMINAL.has(job.status) ? (
        <ProgressView job={job} onCancel={() => void cancel()} />
      ) : (
        <UploadPanel
          detectors={detectors}
          loadingDetectors={loadingDetectors}
          initialError={error}
          onSubmit={(file, prompt, options) => void start(file, prompt, options)}
        />
      )}
    </>
  );
}
