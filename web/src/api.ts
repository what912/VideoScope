import type {
  AnalysisOptions,
  AnalysisReport,
  DetectorManifest,
  JobEvent,
  JobResponse,
  PublishJobEvent,
  PublishJobResponse,
  PublishPlan,
  PublishProfile,
  PublishProfileId,
  PublishTechnicalReport,
  PrivacyJobEvent,
  PrivacyJobResponse,
  PrivacyPlan,
  PrivacyReviewPayload,
  PrivacyRiskMap,
  PrivacyTechnicalReport,
  ShareAudienceProfile,
  RescueConfirmation,
  RescueDamageMap,
  RescueJobEvent,
  RescueJobResponse,
  RescuePlan,
  RescuePrepareOptions,
  RescueStrategy,
  RescueSymptom,
  RescueTechnicalReport,
  ContentCreateOptions,
  ContentJobEvent,
  ContentJobResponse,
  ContentJoinPreview,
  ContentMap,
  ContentPlan,
  ContentRevisionPayload,
  AdvancedAIPrepareOptions,
  AISuggestionBatch,
  AIReviewDecision,
  AIReviewManifest,
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
    const payload = (await response.json()) as { detail?: unknown };
    return (
      formatErrorDetail(payload.detail) ??
      `Request failed with status ${response.status}`
    );
  } catch {
    return `Request failed with status ${response.status}`;
  }
}

function formatErrorDetail(detail: unknown): string | null {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (!Array.isArray(detail)) return null;
  const messages = detail.flatMap((item): string[] => {
    if (typeof item !== "object" || item === null) return [];
    const record = item as Record<string, unknown>;
    if (typeof record.msg !== "string" || !record.msg.trim()) return [];
    const location = Array.isArray(record.loc)
      ? record.loc
          .filter(
            (part): part is string | number =>
              typeof part === "string" || typeof part === "number",
          )
          .join(".")
      : "";
    return [location ? `${location}: ${record.msg}` : record.msg];
  });
  return messages.length > 0 ? messages.join("; ") : null;
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

export function listPublishProfiles(
  fetcher?: typeof fetch,
): Promise<PublishProfile[]> {
  return requestJson("/publish/profiles", undefined, fetcher);
}

export async function createPublishJob(
  video: File,
  profileId: PublishProfileId,
  fetcher: typeof fetch = fetch,
): Promise<PublishJobResponse> {
  const form = new FormData();
  form.append("video", video);
  form.append("profile_id", profileId);
  return requestJson("/publish/jobs", { method: "POST", body: form }, fetcher);
}

export function getPublishJob(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<PublishJobResponse> {
  return requestJson(
    `/publish/jobs/${encodeURIComponent(jobId)}`,
    undefined,
    fetcher,
  );
}

export function getPublishPlan(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<PublishPlan> {
  return requestJson(
    `/publish/jobs/${encodeURIComponent(jobId)}/plan`,
    undefined,
    fetcher,
  );
}

export function confirmPublishJob(
  jobId: string,
  planDigest: string,
  fetcher?: typeof fetch,
): Promise<PublishJobResponse> {
  return requestJson(
    `/publish/jobs/${encodeURIComponent(jobId)}/confirm`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ plan_digest: planDigest }),
    },
    fetcher,
  );
}

export async function cancelPublishJob(
  jobId: string,
  fetcher: typeof fetch = fetch,
): Promise<PublishJobResponse | null> {
  const response = await fetcher(
    `${API_ROOT}/publish/jobs/${encodeURIComponent(jobId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }
  if (response.status === 204) return null;
  return (await response.json()) as PublishJobResponse;
}

export function publishArtifactUrl(jobId: string, path: string): string {
  const safePath = path
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `${API_ROOT}/publish/jobs/${encodeURIComponent(jobId)}/artifacts/${safePath}`;
}

export function getPublishTechnicalReport(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<PublishTechnicalReport> {
  return requestJson(
    `/publish/jobs/${encodeURIComponent(jobId)}/artifacts/technical-report.json`,
    undefined,
    fetcher,
  );
}

export function listPrivacyProfiles(
  fetcher?: typeof fetch,
): Promise<ShareAudienceProfile[]> {
  return requestJson("/privacy/profiles", undefined, fetcher);
}

export async function createPrivacyJob(
  video: File,
  profileId: string,
  enableOcr: boolean,
  fetcher: typeof fetch = fetch,
): Promise<PrivacyJobResponse> {
  const form = new FormData();
  form.append("video", video);
  form.append("profile_id", profileId);
  form.append("enable_ocr", String(enableOcr));
  return requestJson("/privacy/jobs", { method: "POST", body: form }, fetcher);
}

export function getPrivacyJob(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<PrivacyJobResponse> {
  return requestJson(
    `/privacy/jobs/${encodeURIComponent(jobId)}`,
    undefined,
    fetcher,
  );
}

export function getPrivacyRiskMap(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<PrivacyRiskMap> {
  return requestJson(
    `/privacy/jobs/${encodeURIComponent(jobId)}/risk-map`,
    undefined,
    fetcher,
  );
}

export function reviewPrivacyJob(
  jobId: string,
  review: PrivacyReviewPayload,
  fetcher?: typeof fetch,
): Promise<PrivacyJobResponse> {
  return requestJson(
    `/privacy/jobs/${encodeURIComponent(jobId)}/review`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(review),
    },
    fetcher,
  );
}

export function preparePrivacyJob(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<PrivacyJobResponse> {
  return requestJson(
    `/privacy/jobs/${encodeURIComponent(jobId)}/prepare`,
    { method: "POST" },
    fetcher,
  );
}

export function getPrivacyPlan(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<PrivacyPlan> {
  return requestJson(
    `/privacy/jobs/${encodeURIComponent(jobId)}/plan`,
    undefined,
    fetcher,
  );
}

export function confirmPrivacyJob(
  jobId: string,
  planDigest: string,
  fetcher?: typeof fetch,
): Promise<PrivacyJobResponse> {
  return requestJson(
    `/privacy/jobs/${encodeURIComponent(jobId)}/confirm`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ plan_digest: planDigest }),
    },
    fetcher,
  );
}

export async function deletePrivacyJob(
  jobId: string,
  fetcher: typeof fetch = fetch,
): Promise<PrivacyJobResponse | null> {
  const response = await fetcher(
    `${API_ROOT}/privacy/jobs/${encodeURIComponent(jobId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new ApiError(await parseError(response), response.status);
  }
  if (response.status === 204) return null;
  return (await response.json()) as PrivacyJobResponse;
}

function privacyArtifactPath(jobId: string, scope: string, path: string): string {
  const safePath = path
    .split("/")
    .map((part) => encodeURIComponent(part))
    .join("/");
  return `${API_ROOT}/privacy/jobs/${encodeURIComponent(jobId)}/${scope}/${safePath}`;
}

export function privacyArtifactUrl(jobId: string, path: string): string {
  return privacyArtifactPath(jobId, "artifacts", path);
}

export function privacyPrivateArtifactUrl(jobId: string, path: string): string {
  return privacyArtifactPath(jobId, "private-artifacts", path);
}

export async function createRescueJob(
  video: File,
  strategy: RescueStrategy,
  symptoms: RescueSymptom[],
  options: RescuePrepareOptions,
  fetcher: typeof fetch = fetch,
): Promise<RescueJobResponse> {
  const form = new FormData();
  form.append("video", video);
  form.append("strategy", strategy);
  symptoms.forEach((symptom) => form.append("symptoms", symptom));
  form.append("locked_ranges", JSON.stringify(options.lockedRanges));
  form.append("balanced_strength_limit", String(options.balancedStrengthLimit));
  return requestJson("/rescue/jobs", { method: "POST", body: form }, fetcher);
}

export function getRescueJob(jobId: string, fetcher?: typeof fetch): Promise<RescueJobResponse> {
  return requestJson(`/rescue/jobs/${encodeURIComponent(jobId)}`, undefined, fetcher);
}

export function getRescueDamageMap(jobId: string, fetcher?: typeof fetch): Promise<RescueDamageMap> {
  return requestJson(`/rescue/jobs/${encodeURIComponent(jobId)}/damage-map`, undefined, fetcher);
}

export function getRescuePlan(jobId: string, fetcher?: typeof fetch): Promise<RescuePlan> {
  return requestJson(`/rescue/jobs/${encodeURIComponent(jobId)}/plan`, undefined, fetcher);
}

export function confirmRescueJob(jobId: string, confirmation: RescueConfirmation, fetcher?: typeof fetch): Promise<RescueJobResponse> {
  return requestJson(`/rescue/jobs/${encodeURIComponent(jobId)}/confirm`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(confirmation),
  }, fetcher);
}

export async function deleteRescueJob(jobId: string, fetcher: typeof fetch = fetch): Promise<RescueJobResponse | null> {
  const response = await fetcher(`${API_ROOT}/rescue/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
  if (!response.ok) throw new ApiError(await parseError(response), response.status);
  return response.status === 204 ? null : (await response.json()) as RescueJobResponse;
}

function rescueArtifactPath(jobId: string, scope: "artifacts" | "private-artifacts", path: string): string {
  // encodeURIComponent leaves dots untouched; encode each segment strictly so a
  // caller cannot make a browser normalize a traversal segment before the API
  // receives its own validated artifact path.
  const safePath = path.split("/").map((part) => encodeURIComponent(part).replace(/\./g, "%2E")).join("/");
  return `${API_ROOT}/rescue/jobs/${encodeURIComponent(jobId)}/${scope}/${safePath}`;
}

export function rescueArtifactUrl(jobId: string, path: string): string { return rescueArtifactPath(jobId, "artifacts", path); }
export function rescuePrivateArtifactUrl(jobId: string, path: string): string { return rescueArtifactPath(jobId, "private-artifacts", path); }
export function getRescueTechnicalReport(jobId: string, fetcher?: typeof fetch): Promise<RescueTechnicalReport> {
  return requestJson(`/rescue/jobs/${encodeURIComponent(jobId)}/artifacts/technical-report.json`, undefined, fetcher);
}

export function getPrivacyTechnicalReport(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<PrivacyTechnicalReport> {
  return requestJson(
    `/privacy/jobs/${encodeURIComponent(jobId)}/artifacts/technical-report.json`,
    undefined,
    fetcher,
  );
}

export async function createContentJob(
  video: File,
  options: ContentCreateOptions,
  fetcher: typeof fetch = fetch,
): Promise<ContentJobResponse> {
  const form = new FormData();
  form.append("video", video);
  form.append("goal", options.goal);
  if (options.transcript) form.append("transcript", options.transcript);
  if (options.config) {
    form.append(
      "config_json",
      JSON.stringify({ ...options.config, goal: options.goal }),
    );
  }
  return requestJson(
    "/content/jobs",
    { method: "POST", body: form },
    fetcher,
  );
}

export function getContentJob(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<ContentJobResponse> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}`,
    undefined,
    fetcher,
  );
}

export function getContentMap(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<ContentMap> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/map`,
    undefined,
    fetcher,
  );
}

export function getContentPlan(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<ContentPlan> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/plan`,
    undefined,
    fetcher,
  );
}

export function reviseContentStoryboard(
  jobId: string,
  payload: ContentRevisionPayload,
  fetcher?: typeof fetch,
): Promise<ContentJobResponse> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/storyboard`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    },
    fetcher,
  );
}

export function createContentPreviews(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<ContentJobResponse> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/previews`,
    { method: "POST" },
    fetcher,
  );
}

export function getContentPreviews(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<ContentJoinPreview[]> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/previews`,
    undefined,
    fetcher,
  );
}

export function confirmContentJob(
  jobId: string,
  plan: ContentPlan,
  revision: number,
  fetcher?: typeof fetch,
): Promise<ContentJobResponse> {
  const accepted = plan.actions
    .filter((action) => action.changes_content && action.requires_confirmation)
    .map((action) => action.id);
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/confirm`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        plan_digest: plan.plan_digest,
        revision,
        accepted_action_ids: accepted,
      }),
    },
    fetcher,
  );
}

export async function deleteContentJob(
  jobId: string,
  fetcher: typeof fetch = fetch,
): Promise<ContentJobResponse | null> {
  const response = await fetcher(
    `${API_ROOT}/content/jobs/${encodeURIComponent(jobId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) throw new ApiError(await parseError(response), response.status);
  return response.status === 204
    ? null
    : ((await response.json()) as ContentJobResponse);
}

function contentArtifactPath(
  jobId: string,
  scope: "artifacts" | "previews",
  path: string,
): string {
  const safePath = path
    .split("/")
    .map((part) => encodeURIComponent(part).replace(/\./g, "%2E"))
    .join("/");
  return `${API_ROOT}/content/jobs/${encodeURIComponent(jobId)}/${scope}/${safePath}`;
}

export function contentArtifactUrl(jobId: string, path: string): string {
  return contentArtifactPath(jobId, "artifacts", path);
}

export function contentPreviewUrl(jobId: string, path: string): string {
  return contentArtifactPath(jobId, "previews", path);
}

export function prepareContentAI(
  jobId: string,
  options: AdvancedAIPrepareOptions,
  fetcher?: typeof fetch,
): Promise<AISuggestionBatch> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/ai/prepare`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(options),
    },
    fetcher,
  );
}

export function getContentAISuggestions(
  jobId: string,
  fetcher?: typeof fetch,
): Promise<AISuggestionBatch> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/ai/suggestions`,
    undefined,
    fetcher,
  );
}

export function reviewContentAI(
  jobId: string,
  decisions: AIReviewDecision[],
  fetcher?: typeof fetch,
): Promise<AIReviewManifest> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/ai/review`,
    {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ decisions }),
    },
    fetcher,
  );
}

export function applyContentAI(
  jobId: string,
  expectedRevision: number,
  fetcher?: typeof fetch,
): Promise<ContentJobResponse> {
  return requestJson(
    `/content/jobs/${encodeURIComponent(jobId)}/ai/apply`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ expected_revision: expectedRevision }),
    },
    fetcher,
  );
}

export async function cancelContentAI(
  jobId: string,
  fetcher: typeof fetch = fetch,
): Promise<void> {
  const response = await fetcher(
    `${API_ROOT}/content/jobs/${encodeURIComponent(jobId)}/ai`,
    { method: "DELETE" },
  );
  if (!response.ok) throw new ApiError(await parseError(response), response.status);
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
        if (!Number.isInteger(event.sequence) || event.sequence < 1) {
          throw new Error("Invalid progress event sequence");
        }
        if (event.sequence <= lastSequence) return;
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

export function subscribeToPublishEvents(
  jobId: string,
  onEvent: (event: PublishJobEvent) => void,
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
      `${API_ROOT}/publish/jobs/${encodeURIComponent(jobId)}/events${query}`,
    );
    source.addEventListener("status", (rawEvent: Event) => {
      try {
        const event = JSON.parse(
          (rawEvent as MessageEvent<string>).data,
        ) as PublishJobEvent;
        if (!Number.isInteger(event.sequence) || event.sequence < 1) {
          throw new Error("Invalid Publish Ready progress event sequence");
        }
        if (event.sequence <= lastSequence) return;
        lastSequence = Math.max(lastSequence, event.sequence);
        onEvent(event);
        if (
          ["completed", "needs_review", "failed", "cancelled"].includes(
            event.status,
          )
        ) {
          closed = true;
          source?.close();
        }
      } catch {
        onError(new Error("Received an invalid Publish Ready progress event."));
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

export function subscribeToPrivacyEvents(
  jobId: string,
  onEvent: (event: PrivacyJobEvent) => void,
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
    reconnectTimer = null;
    const query = lastSequence > 0 ? `?after=${lastSequence}` : "";
    source = sourceFactory(
      `${API_ROOT}/privacy/jobs/${encodeURIComponent(jobId)}/events${query}`,
    );
    source.addEventListener("status", (rawEvent: Event) => {
      try {
        const event = JSON.parse(
          (rawEvent as MessageEvent<string>).data,
        ) as PrivacyJobEvent;
        if (!Number.isInteger(event.sequence) || event.sequence < 1) {
          throw new Error("Invalid Safe Sharing progress event sequence");
        }
        if (event.sequence <= lastSequence) return;
        lastSequence = event.sequence;
        onEvent(event);
        if (
          [
            "completed",
            "needs_review",
            "partial",
            "failed",
            "cancelled",
          ].includes(event.status)
        ) {
          closed = true;
          source?.close();
        }
      } catch {
        onError(new Error("Received an invalid Safe Sharing progress event."));
      }
    });
    source.addEventListener("error", () => {
      source?.close();
      if (!closed && reconnectTimer === null) {
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

/** Subscribe to ordered local Rescue events. `after` is the explicit replay
 * cursor; native EventSource also retains the last SSE id when available. */
export function subscribeToRescueEvents(
  jobId: string,
  onEvent: (event: RescueJobEvent) => void,
  onError: (error: Error) => void,
  options: {
    sourceFactory?: EventSourceFactory;
    reconnectDelayMs?: number;
    setTimer?: typeof setTimeout;
    clearTimer?: typeof clearTimeout;
  } = {},
): JobEventSubscription {
  const sourceFactory = options.sourceFactory ?? ((url: string) => new EventSource(url));
  const setTimer = options.setTimer ?? setTimeout;
  const clearTimer = options.clearTimer ?? clearTimeout;
  let source: EventSourceLike | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let lastSequence = 0;
  let attempts = 0;
  let closed = false;
  const terminal = new Set<RescueJobEvent["status"]>(["completed", "needs_review", "partial", "failed", "cancelled"]);
  const connect = (): void => {
    if (closed) return;
    reconnectTimer = null;
    const suffix = lastSequence > 0 ? `?after=${lastSequence}` : "";
    source = sourceFactory(`${API_ROOT}/rescue/jobs/${encodeURIComponent(jobId)}/events${suffix}`);
    source.addEventListener("status", (raw: Event) => {
      try {
        const event = JSON.parse((raw as MessageEvent<string>).data) as RescueJobEvent;
        if (!Number.isInteger(event.sequence) || event.sequence < 1) throw new Error("Invalid Video Rescue progress event.");
        if (event.sequence <= lastSequence) return;
        lastSequence = event.sequence;
        attempts = 0;
        onEvent(event);
        if (terminal.has(event.status)) { closed = true; source?.close(); }
      } catch {
        onError(new Error("Received an invalid Video Rescue progress event."));
      }
    });
    source.addEventListener("error", () => {
      source?.close();
      if (closed || reconnectTimer !== null) return;
      attempts += 1;
      const base = options.reconnectDelayMs ?? 750;
      const delay = Math.min(base * 2 ** Math.min(attempts - 1, 4), 12000);
      reconnectTimer = setTimer(connect, delay);
    });
  };
  connect();
  return { close: () => { closed = true; source?.close(); if (reconnectTimer !== null) clearTimer(reconnectTimer); } };
}

export function subscribeToContentEvents(
  jobId: string,
  onEvent: (event: ContentJobEvent) => void,
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
  let lastSequence = 0;
  let attempts = 0;
  let closed = false;
  const terminal = new Set<ContentJobEvent["status"]>([
    "completed",
    "partial",
    "needs_review",
    "failed",
    "cancelled",
  ]);
  const connect = (): void => {
    if (closed) return;
    reconnectTimer = null;
    const suffix = lastSequence > 0 ? `?after=${lastSequence}` : "";
    source = sourceFactory(
      `${API_ROOT}/content/jobs/${encodeURIComponent(jobId)}/events${suffix}`,
    );
    source.addEventListener("status", (raw: Event) => {
      try {
        const event = JSON.parse(
          (raw as MessageEvent<string>).data,
        ) as ContentJobEvent;
        if (!Number.isInteger(event.sequence) || event.sequence < 1) {
          throw new Error("Invalid useful-content progress event");
        }
        if (event.sequence <= lastSequence) return;
        lastSequence = event.sequence;
        attempts = 0;
        onEvent(event);
        if (terminal.has(event.status)) {
          closed = true;
          source?.close();
        }
      } catch {
        onError(new Error("Received an invalid useful-content progress event."));
      }
    });
    source.addEventListener("error", () => {
      source?.close();
      if (closed || reconnectTimer !== null) return;
      attempts += 1;
      const base = options.reconnectDelayMs ?? 750;
      reconnectTimer = setTimer(
        connect,
        Math.min(base * 2 ** Math.min(attempts - 1, 4), 12_000),
      );
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
