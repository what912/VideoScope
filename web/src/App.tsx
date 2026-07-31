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
  const [detectors, setDetectors] = useState<DetectorManifest[]>([]);
  const [loadingDetectors, setLoadingDetectors] = useState(true);
  const [job, setJob] = useState<JobResponse | null>(null);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [videoSource, setVideoSource] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

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
        window.history.replaceState(null, "", "?mock=1&job=mock-dashboard");
        return;
      }
      const created = await createJob(file, prompt, options);
      setJob(created);
      window.history.replaceState(null, "", `?job=${created.job_id}`);
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
    window.history.replaceState(null, "", mockMode ? "?mock=1" : "/");
  };

  return (
    <>
      <Header />
      {report && job ? (
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
