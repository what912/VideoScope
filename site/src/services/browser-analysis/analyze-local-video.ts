import type {
  BrowserCpuDetectorConfiguration,
  BrowserCpuDetectorExecution,
  BrowserCpuFinding,
  BrowserCpuQualityMetric,
  JsonValue,
  Severity,
} from "../../types/analysis";
import {
  createRealBrowserReport,
  type RealBrowserReport,
} from "../../types/report";
import { validateBrowserAnalysisOptions } from "./config";
import type {
  AnalysisProgress,
  AnalysisStage,
  BrowserAnalysisService,
  BrowserDetector,
  BrowserDetectorConfig,
  BrowserFindingDraft,
  BrowserSampler,
} from "./contracts";
import { builtInBrowserDetectors } from "./detectors";
import {
  attachEvidenceThumbnails,
  selectEvidenceTimestamps,
} from "./evidence";
import {
  BrowserAnalysisError,
  sanitizeError,
  throwIfAborted,
} from "./errors";
import {
  hashFileIncrementally,
  makeDeterministicFindingId,
} from "./hash";
import { validateInterval } from "./intervals";
import { getAnalysisCopy } from "./messages";
import { createBrowserVideoSampler } from "./sampler";

const TOOL_VERSION = "0.2.0";
const severityRank: Record<Severity, number> = {
  info: 0,
  low: 1,
  medium: 2,
  high: 3,
  critical: 4,
};

export interface BrowserAnalysisDependencies {
  sampler?: BrowserSampler;
  detectors?: BrowserDetector[];
  hashFile?: typeof hashFileIncrementally;
  now?: () => Date;
  randomId?: () => string;
  monotonicNow?: () => number;
}

function createProgressEmitter(
  onProgress: (event: AnalysisProgress) => void,
) {
  let lastProgress = 0;
  return (
    stageOrEvent: AnalysisStage | AnalysisProgress,
    progress?: number,
    detectorId?: string,
  ) => {
    const event =
      typeof stageOrEvent === "string"
        ? {
            stage: stageOrEvent,
            progress: progress ?? lastProgress,
            ...(detectorId ? { detector_id: detectorId } : {}),
          }
        : stageOrEvent;
    const monotonicProgress = Math.max(
      lastProgress,
      Math.min(1, Math.max(0, event.progress)),
    );
    lastProgress = monotonicProgress;
    onProgress({ ...event, progress: monotonicProgress });
  };
}

function safeFilename(filename: string): string {
  return filename.split(/[\\/]/).at(-1) || "video";
}

function detectorConfiguration(
  detector: BrowserDetector,
  configured: Record<string, BrowserDetectorConfig>,
): BrowserDetectorConfig {
  return {
    ...detector.defaultConfig,
    ...(configured[detector.id] ?? {}),
  };
}

function configurationRecord(
  detector: BrowserDetector,
  config: BrowserDetectorConfig,
): BrowserCpuDetectorConfiguration {
  return {
    detector_id: detector.id,
    detector_version: detector.version,
    signal_kind: "browser_cpu",
    enabled: config.enabled,
    parameters: { ...config },
  };
}

function validateDrafts(
  drafts: readonly BrowserFindingDraft[],
  detector: BrowserDetector,
  durationSeconds: number,
): void {
  for (const draft of drafts) {
    validateInterval(draft.time_range);
    if (
      draft.detector_id !== detector.id ||
      draft.detector_version !== detector.version ||
      draft.signal_kind !== "browser_cpu"
    ) {
      throw new TypeError("Detector returned an incompatible Finding");
    }
    if (
      draft.time_range.end_seconds > durationSeconds + 0.001 ||
      draft.evidence.length === 0
    ) {
      throw new TypeError("Detector returned an invalid Finding boundary");
    }
  }
}

function sortFindings(findings: BrowserCpuFinding[]): BrowserCpuFinding[] {
  return findings.sort(
    (left, right) =>
      left.time_range.start_seconds - right.time_range.start_seconds ||
      severityRank[left.severity] - severityRank[right.severity] ||
      left.detector_id.localeCompare(right.detector_id) ||
      left.id.localeCompare(right.id),
  );
}

function summarize(findings: readonly BrowserCpuFinding[]) {
  const severityCounts: Record<Severity, number> = {
    info: 0,
    low: 0,
    medium: 0,
    high: 0,
    critical: 0,
  };
  findings.forEach((finding) => {
    severityCounts[finding.severity] += 1;
  });
  return {
    review_interval_count: findings.length,
    severity_counts: severityCounts,
  };
}

function detectorMetrics(
  executions: readonly BrowserCpuDetectorExecution[],
  locale: "en" | "zh-CN",
): BrowserCpuQualityMetric[] {
  const copy = getAnalysisCopy(locale);
  return executions
    .filter((execution) => execution.status === "ok")
    .map((execution) => ({
      id: `${execution.detector_id}-finding-count`,
      label: `${execution.detector_id} ${copy.detectorMetricSuffix}`,
      value: execution.findings_count,
      kind: "browser_cpu",
      detector_id: execution.detector_id,
      unit: "count",
      description: copy.detectorMetricDescription,
    }));
}

export function createBrowserAnalysisService(
  dependencies: BrowserAnalysisDependencies = {},
): BrowserAnalysisService {
  const sampler = dependencies.sampler ?? createBrowserVideoSampler();
  const detectors = [...(dependencies.detectors ?? builtInBrowserDetectors)].sort(
    (left, right) => left.id.localeCompare(right.id),
  );
  const hashFile = dependencies.hashFile ?? hashFileIncrementally;
  const now = dependencies.now ?? (() => new Date());
  const randomId =
    dependencies.randomId ??
    (() =>
      typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(16).slice(2)}`);
  const monotonicNow =
    dependencies.monotonicNow ??
    (() =>
      typeof performance !== "undefined" ? performance.now() : Date.now());

  return {
    async analyzeLocalVideo(
      file,
      options,
      signal,
      onProgress,
    ): Promise<RealBrowserReport> {
      const progress = createProgressEmitter(onProgress);
      const analysisStarted = monotonicNow();
      progress("validating", 0.02);
      throwIfAborted(signal);
      validateBrowserAnalysisOptions(options);
      if (!(file instanceof File) || file.size <= 0) {
        throw new BrowserAnalysisError(
          "invalid_input",
          "Select a non-empty local video file",
        );
      }

      progress("hashing", 0.08);
      const inputHash = await hashFile(file, signal);
      throwIfAborted(signal);
      progress("reading_metadata", 0.18);
      progress("sampling_frames", 0.24);
      const configuredDarkPixelThreshold =
        options.detectors.near_black?.dark_pixel_threshold;
      const samplingOptions = {
        ...options,
        dark_pixel_threshold:
          typeof configuredDarkPixelThreshold === "number"
            ? configuredDarkPixelThreshold
            : options.dark_pixel_threshold,
      };

      return sampler.withSession(
        file,
        samplingOptions,
        signal,
        (event) => progress(event),
        async (session) => {
          throwIfAborted(signal);
          progress("segmenting_scenes", 0.52);
          const configurations: BrowserCpuDetectorConfiguration[] = [];
          const executions: BrowserCpuDetectorExecution[] = [];
          const drafts: BrowserFindingDraft[] = [];

          progress("running_detectors", 0.58);
          for (const [index, detector] of detectors.entries()) {
            throwIfAborted(signal);
            const config = detectorConfiguration(detector, options.detectors);
            configurations.push(
              configurationRecord(detector, config),
            );
            if (!config.enabled) {
              executions.push({
                detector_id: detector.id,
                detector_version: detector.version,
                signal_kind: "browser_cpu",
                status: "skipped",
                elapsed_seconds: 0,
                findings_count: 0,
              });
              continue;
            }
            const detectorStarted = monotonicNow();
            try {
              const detectorDrafts = detector.analyze(
                {
                  samples: session.samples,
                  scenes: session.scenes,
                  locale: options.locale,
                },
                config,
              );
              validateDrafts(
                detectorDrafts,
                detector,
                session.metadata.duration_seconds,
              );
              drafts.push(...detectorDrafts);
              executions.push({
                detector_id: detector.id,
                detector_version: detector.version,
                signal_kind: "browser_cpu",
                status: "ok",
                elapsed_seconds: Math.max(
                  0,
                  (monotonicNow() - detectorStarted) / 1_000,
                ),
                findings_count: detectorDrafts.length,
              });
            } catch (error) {
              if (
                error instanceof DOMException &&
                error.name === "AbortError"
              ) {
                throw error;
              }
              const sanitized = sanitizeError(error);
              executions.push({
                detector_id: detector.id,
                detector_version: detector.version,
                signal_kind: "browser_cpu",
                status: "failed",
                elapsed_seconds: Math.max(
                  0,
                  (monotonicNow() - detectorStarted) / 1_000,
                ),
                findings_count: 0,
                error_type: sanitized.errorType,
                error_message: sanitized.errorMessage,
              });
            }
            progress(
              "running_detectors",
              0.58 + ((index + 1) / Math.max(1, detectors.length)) * 0.18,
              detector.id,
            );
          }

          progress("selecting_evidence", 0.8);
          const evidenceSelection = selectEvidenceTimestamps(
            drafts,
            options.max_evidence_items,
          );
          const capture = await session.captureEvidence(
            evidenceSelection.timestamps,
            signal,
          );
          const evidenceWasCapped =
            evidenceSelection.capped_by_count ||
            capture.capped_by_count ||
            capture.capped_by_bytes;
          const analysisCopy = getAnalysisCopy(options.locale);
          const sampleCapReached =
            Math.ceil(
              session.metadata.duration_seconds * options.sample_fps,
            ) > options.max_samples;
          const findings: BrowserCpuFinding[] = [];
          for (const draft of drafts) {
            const configuration =
              configurations.find(
                (entry) => entry.detector_id === draft.detector_id,
              )?.parameters ?? {};
            const id = await makeDeterministicFindingId({
              inputHash,
              detectorId: draft.detector_id,
              detectorVersion: draft.detector_version,
              startSeconds: draft.time_range.start_seconds,
              endSeconds: draft.time_range.end_seconds,
              configuration: configuration as Record<string, JsonValue>,
            });
            const finding = attachEvidenceThumbnails(
              { ...draft, id },
              capture.thumbnails,
            );
            findings.push(
              evidenceWasCapped
                ? {
                    ...finding,
                    limitations: [
                      ...finding.limitations,
                      analysisCopy.evidenceCapLimitation,
                    ],
                  }
                : finding,
            );
          }
          sortFindings(findings);
          progress("assembling_report", 0.94);
          const createdAt = now();
          const filename = safeFilename(file.name);
          const report = createRealBrowserReport({
            tool_version: TOOL_VERSION,
            id: `browser-${inputHash.slice(0, 20)}`,
            analysis_id: randomId(),
            title: options.title?.trim() || filename,
            created_at: createdAt.toISOString(),
            input_hash: inputHash,
            ...(options.retain_prompt && options.prompt
              ? { prompt: options.prompt }
              : {}),
            metadata: {
              ...session.metadata,
              filename,
            },
            configuration: configurations,
            detector_executions: executions,
            findings,
            metrics: detectorMetrics(executions, options.locale),
            summary: summarize(findings),
            warnings: [
              analysisCopy.sampleWarning,
              ...(sampleCapReached ? [analysisCopy.sampleCapWarning] : []),
              analysisCopy.desktopWarning,
              ...(evidenceWasCapped
                ? [analysisCopy.evidenceCapWarning]
                : []),
            ],
            runtime: {
              environment: "browser",
              user_agent_family: "browser",
              analysis_seconds: Math.max(
                0,
                (monotonicNow() - analysisStarted) / 1_000,
              ),
              sample_count: session.samples.length,
            },
            reviewed_finding_ids: [],
            preferences: {
              locale: options.locale,
              creator_view: true,
              reduced_motion: options.reduced_motion,
            },
          });
          progress("complete", 1);
          return report;
        },
      );
    },
  };
}

export const browserAnalysisService = createBrowserAnalysisService();
