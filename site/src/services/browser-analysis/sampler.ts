import type { EvidenceThumbnail, VideoMetadata } from "../../types/analysis";
import type {
  AnalysisProgress,
  BrowserSampler,
  BrowserSamplingOptions,
  BrowserSamplingSession,
} from "./contracts";
import { BrowserAnalysisError, throwIfAborted } from "./errors";
import { dataUrlByteSize } from "./evidence";
import {
  computeFrameMetrics,
  hammingDistance,
  meanAbsoluteDifference,
} from "./metrics";
import { segmentScenes } from "./scene-segmentation";

const MEDIA_EVENT_TIMEOUT_MS = 20_000;

export interface BrowserVideoSamplerDependencies {
  createObjectURL(file: Blob): string;
  revokeObjectURL(url: string): void;
  createVideo(): HTMLVideoElement;
  createCanvas(): HTMLCanvasElement;
}

function waitForMediaEvent(
  video: HTMLVideoElement,
  successEvent: string,
  signal: AbortSignal,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      cleanup();
      reject(
        new BrowserAnalysisError(
          "decode_failed",
          `Video ${successEvent} timed out`,
        ),
      );
    }, MEDIA_EVENT_TIMEOUT_MS);
    const onSuccess = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(
        new BrowserAnalysisError(
          "metadata_unavailable",
          "Browser could not read video metadata",
        ),
      );
    };
    const onAbort = () => {
      cleanup();
      reject(new DOMException("Analysis cancelled", "AbortError"));
    };
    const cleanup = () => {
      window.clearTimeout(timeout);
      video.removeEventListener(successEvent, onSuccess);
      video.removeEventListener("error", onError);
      signal.removeEventListener("abort", onAbort);
    };
    video.addEventListener(successEvent, onSuccess, { once: true });
    video.addEventListener("error", onError, { once: true });
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function loadVideo(
  video: HTMLVideoElement,
  source: string,
  signal: AbortSignal,
): Promise<void> {
  throwIfAborted(signal);
  video.preload = "metadata";
  video.muted = true;
  video.playsInline = true;
  video.src = source;
  const metadataReady = waitForMediaEvent(video, "loadedmetadata", signal);
  video.load();
  await metadataReady;
  if (video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
    await waitForMediaEvent(video, "loadeddata", signal);
  }
}

async function seekVideo(
  video: HTMLVideoElement,
  timestamp: number,
  signal: AbortSignal,
): Promise<void> {
  throwIfAborted(signal);
  if (
    Math.abs(video.currentTime - timestamp) < 0.0001 &&
    video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA
  ) {
    return;
  }
  const seeked = waitForMediaEvent(video, "seeked", signal);
  video.currentTime = timestamp;
  await seeked;
}

function canvasSize(
  width: number,
  height: number,
  maximumDimension: number,
): { width: number; height: number } {
  const scale = Math.min(1, maximumDimension / Math.max(width, height));
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

function readFrameMetrics(
  context: CanvasRenderingContext2D,
  width: number,
  height: number,
  darkPixelThreshold: number,
) {
  try {
    return computeFrameMetrics(
      context.getImageData(0, 0, width, height),
      darkPixelThreshold,
    );
  } catch (error) {
    if (
      error instanceof RangeError ||
      (error instanceof DOMException &&
        (error.name === "QuotaExceededError" ||
          error.name === "InvalidStateError"))
    ) {
      throw new BrowserAnalysisError(
        "memory_pressure",
        "Browser frame memory is unavailable",
      );
    }
    throw error;
  }
}

function safeFilename(filename: string): string {
  return filename.split(/[\\/]/).at(-1) || "video";
}

async function yieldToBrowser(): Promise<void> {
  await new Promise<void>((resolve) => {
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(() => resolve());
    } else {
      window.setTimeout(resolve, 0);
    }
  });
}

function defaultDependencies(): BrowserVideoSamplerDependencies {
  return {
    createObjectURL: (file) => URL.createObjectURL(file),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
    createVideo: () => document.createElement("video"),
    createCanvas: () => document.createElement("canvas"),
  };
}

export function createBrowserVideoSampler(
  overrides: Partial<BrowserVideoSamplerDependencies> = {},
): BrowserSampler {
  const dependencies = { ...defaultDependencies(), ...overrides };
  return {
    async withSession<T>(
      file: File,
      options: BrowserSamplingOptions,
      signal: AbortSignal,
      onProgress: (event: AnalysisProgress) => void,
      consume: (session: BrowserSamplingSession) => Promise<T>,
    ): Promise<T> {
      const objectUrl = dependencies.createObjectURL(file);
      const video = dependencies.createVideo();
      const canvas = dependencies.createCanvas();
      try {
        await loadVideo(video, objectUrl, signal);
        if (
          !Number.isFinite(video.duration) ||
          video.duration <= 0
        ) {
          throw new BrowserAnalysisError(
            "duration_unavailable",
            "Video duration is unavailable",
          );
        }
        if (
          video.videoWidth <= 0 ||
          video.videoHeight <= 0
        ) {
          throw new BrowserAnalysisError(
            "metadata_unavailable",
            "Video metadata is incomplete",
          );
        }
        const context = canvas.getContext("2d", {
          alpha: false,
          willReadFrequently: true,
        });
        if (!context) {
          throw new BrowserAnalysisError(
            "canvas_unavailable",
            "Canvas analysis is unavailable",
          );
        }
        const size = canvasSize(
          video.videoWidth,
          video.videoHeight,
          options.max_dimension,
        );
        canvas.width = size.width;
        canvas.height = size.height;
        const desiredCount = Math.max(
          1,
          Math.ceil(video.duration * options.sample_fps),
        );
        const sampleCount = Math.min(options.max_samples, desiredCount);
        const interval = video.duration / sampleCount;
        const samples = [];
        let previousGrayscale: Uint8Array | undefined;
        let previousHash: bigint | undefined;
        for (let index = 0; index < sampleCount; index += 1) {
          throwIfAborted(signal);
          const timestamp = Math.min(
            Math.max(0, video.duration - 0.001),
            index * interval,
          );
          await seekVideo(video, timestamp, signal);
          context.drawImage(video, 0, 0, size.width, size.height);
          const metrics = readFrameMetrics(
            context,
            size.width,
            size.height,
            options.dark_pixel_threshold,
          );
          samples.push({
            sample_index: index,
            timestamp_seconds: Number(timestamp.toFixed(6)),
            width: size.width,
            height: size.height,
            mean_luma: metrics.meanLuma,
            median_luma: metrics.medianLuma,
            dark_pixel_ratio: metrics.darkPixelRatio,
            sharpness: metrics.sharpness,
            pixel_difference: meanAbsoluteDifference(
              metrics.grayscale,
              previousGrayscale,
            ),
            hash_distance: hammingDistance(
              metrics.perceptualHash,
              previousHash,
            ),
          });
          previousGrayscale = metrics.grayscale;
          previousHash = metrics.perceptualHash;
          onProgress({
            stage: "sampling_frames",
            progress: 0.25 + ((index + 1) / sampleCount) * 0.25,
          });
          await yieldToBrowser();
        }
        const metadata: VideoMetadata = {
          filename: safeFilename(file.name),
          mime_type: file.type || "application/octet-stream",
          width: video.videoWidth,
          height: video.videoHeight,
          duration_seconds: video.duration,
          file_size_bytes: file.size,
        };
        const scenes = segmentScenes(
          samples,
          video.duration,
          options.scene_cut_difference_threshold,
        );
        const session: BrowserSamplingSession = {
          metadata,
          samples,
          scenes,
          async captureEvidence(timestamps, captureSignal) {
            const evidenceSize = canvasSize(
              video.videoWidth,
              video.videoHeight,
              options.evidence_max_dimension,
            );
            canvas.width = evidenceSize.width;
            canvas.height = evidenceSize.height;
            const evidence = new Map<number, EvidenceThumbnail>();
            let retainedBytes = 0;
            let cappedByBytes = false;
            const boundedTimestamps = timestamps.slice(
              0,
              options.max_evidence_items,
            );
            for (const timestamp of boundedTimestamps) {
              throwIfAborted(captureSignal);
              await seekVideo(video, timestamp, captureSignal);
              context.drawImage(
                video,
                0,
                0,
                evidenceSize.width,
                evidenceSize.height,
              );
              const src = canvas.toDataURL(
                "image/jpeg",
                options.evidence_quality,
              );
              const candidateBytes = dataUrlByteSize(src);
              if (
                retainedBytes + candidateBytes >
                options.max_evidence_total_bytes
              ) {
                cappedByBytes = true;
                break;
              }
              evidence.set(timestamp, {
                src,
                width: evidenceSize.width,
                height: evidenceSize.height,
              });
              retainedBytes += candidateBytes;
              await yieldToBrowser();
            }
            return {
              thumbnails: evidence,
              capped_by_count:
                timestamps.length > boundedTimestamps.length,
              capped_by_bytes: cappedByBytes,
              retained_bytes: retainedBytes,
            };
          },
        };
        return await consume(session);
      } finally {
        video.pause();
        video.removeAttribute("src");
        video.load();
        dependencies.revokeObjectURL(objectUrl);
      }
    },
  };
}
