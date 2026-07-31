import type {
  AnalysisOptions,
  AnalysisReport,
  DetectorManifest,
  JobEvent,
  JobResponse,
} from "./types";

const API_ROOT = "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function parseError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: string };
    return payload.detail ?? `Request failed with status ${response.status}`;
  } catch {
    return `Request failed with status ${response.status}`;
  }
}

async function requestJson<T>(
  path: string,
  init?: RequestInit,
  fetcher: typeof fetch = fetch,
): Promise<T> {
  const response = await fetcher(`${API_ROOT}${path}`, init);
  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }
  return (await response.json()) as T;
}

export function listDetectors(fetcher?: typeof fetch): Promise<DetectorManifest[]> {
  return requestJson("/detectors", undefined, fetcher);
}

export function getJob(jobId: string, fetcher?: typeof fetch): Promise<JobResponse> {
  return requestJson(`/jobs/${encodeURIComponent(jobId)}`, undefined, fetcher);
}

export function getReport(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<AnalysisReport> {
  return requestJson(`/jobs/${encodeURIComponent(jobId)}/report`, undefined, fetcher);
}

export function cancelJob(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<JobResponse | null> {
  return requestJson(
    `/jobs/${encodeURIComponent(jobId)}`,
    { method: "DELETE" },
    fetcher,
  );
}

export async function createJob(
  video: File,
  prompt: string,
  options: AnalysisOptions,
  fetcher: typeof fetch = fetch,
): Promise<JobResponse> {
  const form = new FormData();
  form.append("video", video);
  if (prompt.trim()) form.append("prompt", prompt.trim());
  form.append(
    "config",
    JSON.stringify({
      sample_fps: options.sampleFps,
      thumbnail_max_size: options.thumbnailMaxSize,
      enabled_detectors: options.detectorIds,
      locale: options.locale,
    }),
  );
  return requestJson("/jobs", { method: "POST", body: form }, fetcher);
}

export function videoUrl(jobId: string): string {
  return `${API_ROOT}/jobs/${encodeURIComponent(jobId)}/video`;
}

export function reportDownloadUrl(jobId: string): string {
  return `${API_ROOT}/jobs/${encodeURIComponent(jobId)}/report`;
}

export function artifactUrl(jobId: string, path: string): string {
  const safePath = path
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `${API_ROOT}/jobs/${encodeURIComponent(jobId)}/artifacts/${safePath}`;
}

interface EventSourceLike {
  addEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void;
  close(): void;
}
type EventSourceFactory = (url: string) => EventSourceLike;

export interface JobEventSubscription {
  close(): void;
}

export function subscribeToJobEvents(
  jobId: string,
  onEvent: (event: JobEvent) => void,
  onError: (error: Error) => void,
  options: {
    sourceFactory?: EventSourceFactory;
    reconnectDelayMs?: number;
    setTimer?: typeof setTimeout;
    clearTimer?: typeof clearTimeout;
  } = {},
): JobEventSubscription {
  const sourceFactory =
    options.sourceFactory ?? ((url: string) => new EventSource(url));
  const setTimer = options.setTimer ?? setTimeout;
  const clearTimer = options.clearTimer ?? clearTimeout;
  let source: EventSourceLike | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;
  let lastSequence = 0;

  const connect = (): void => {
    if (closed) return;
    const query = lastSequence > 0 ? `?after=${lastSequence}` : "";
    source = sourceFactory(
      `${API_ROOT}/jobs/${encodeURIComponent(jobId)}/events${query}`,
    );
    source.addEventListener("status", (rawEvent: Event) => {
      try {
        const event = JSON.parse((rawEvent as MessageEvent<string>).data) as JobEvent;
        lastSequence = Math.max(lastSequence, event.sequence);
        onEvent(event);
        if (["completed", "failed", "cancelled"].includes(event.status)) {
          closed = true;
          source?.close();
        }
      } catch {
        onError(new Error("Received an invalid progress event."));
      }
    });
    source.addEventListener("error", () => {
      source?.close();
      if (!closed) {
        reconnectTimer = setTimer(connect, options.reconnectDelayMs ?? 750);
      }
    });
  };

  connect();
  return {
    close(): void {
      closed = true;
      source?.close();
      if (reconnectTimer !== null) clearTimer(reconnectTimer);
    },
  };
}
