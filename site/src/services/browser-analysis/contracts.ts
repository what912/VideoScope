import type {
  BrowserCpuFinding,
  EvidenceThumbnail,
  JsonValue,
  VideoMetadata,
} from "../../types/analysis";
import type { RealBrowserReport } from "../../types/report";

export type AnalysisStage =
  | "validating"
  | "hashing"
  | "reading_metadata"
  | "sampling_frames"
  | "segmenting_scenes"
  | "running_detectors"
  | "selecting_evidence"
  | "assembling_report"
  | "complete";

export interface AnalysisProgress {
  stage: AnalysisStage;
  progress: number;
  detector_id?: string;
}

export interface BrowserSample {
  sample_index: number;
  timestamp_seconds: number;
  width: number;
  height: number;
  mean_luma: number;
  median_luma: number;
  dark_pixel_ratio: number;
  sharpness: number;
  pixel_difference: number;
  hash_distance: number;
}

export interface BrowserScene {
  scene_index: number;
  start_seconds: number;
  end_seconds: number;
  representative_timestamp: number;
}

export interface BrowserSamplingOptions {
  sample_fps: number;
  max_samples: number;
  max_dimension: number;
  evidence_max_dimension: number;
  evidence_quality: number;
  max_evidence_items: number;
  max_evidence_total_bytes: number;
  dark_pixel_threshold: number;
  scene_cut_difference_threshold: number;
}

export interface EvidenceCaptureResult {
  thumbnails: Map<number, EvidenceThumbnail>;
  capped_by_count: boolean;
  capped_by_bytes: boolean;
  retained_bytes: number;
}

export interface BrowserSamplingSession {
  metadata: VideoMetadata;
  samples: BrowserSample[];
  scenes: BrowserScene[];
  captureEvidence(
    timestamps: number[],
    signal: AbortSignal,
  ): Promise<EvidenceCaptureResult>;
}

export interface BrowserSampler {
  withSession<T>(
    file: File,
    options: BrowserSamplingOptions,
    signal: AbortSignal,
    onProgress: (event: AnalysisProgress) => void,
    consume: (session: BrowserSamplingSession) => Promise<T>,
  ): Promise<T>;
}

export type BrowserFindingDraft = Omit<BrowserCpuFinding, "id">;
export type BrowserDetectorConfig = {
  enabled: boolean;
  [key: string]: JsonValue;
};

export interface BrowserDetectorContext {
  samples: readonly BrowserSample[];
  scenes: readonly BrowserScene[];
  locale: "en" | "zh-CN";
}

export interface BrowserDetector {
  id: string;
  version: string;
  defaultConfig: BrowserDetectorConfig;
  analyze(
    context: BrowserDetectorContext,
    config: BrowserDetectorConfig,
  ): BrowserFindingDraft[];
}

export interface BrowserAnalysisOptions extends BrowserSamplingOptions {
  title?: string;
  prompt?: string;
  retain_prompt: boolean;
  locale: "en" | "zh-CN";
  reduced_motion: boolean;
  detectors: Record<string, BrowserDetectorConfig>;
}

export interface BrowserAnalysisService {
  analyzeLocalVideo(
    file: File,
    options: BrowserAnalysisOptions,
    signal: AbortSignal,
    onProgress: (event: AnalysisProgress) => void,
  ): Promise<RealBrowserReport>;
}
