import { useCallback, useEffect, useRef, useState } from "react";
import type { JobEventSubscription } from "../api";
import type {
  ContentJobEvent,
  ContentJobResponse,
  ContentJobStatus,
  ContentJoinPreview,
  ContentMap,
  ContentPlan,
} from "../types";

export interface ContentLifecycleApi {
  getJob(jobId: string): Promise<ContentJobResponse>;
  getMap(jobId: string): Promise<ContentMap>;
  getPlan(jobId: string): Promise<ContentPlan>;
  getPreviews(jobId: string): Promise<ContentJoinPreview[]>;
  subscribeToEvents(
    jobId: string,
    onEvent: (event: ContentJobEvent) => void,
    onError: (error: Error) => void,
  ): JobEventSubscription;
}

const ORDER: Record<ContentJobStatus, number> = {
  queued: 0,
  probing: 1,
  mapping: 2,
  planning: 3,
  awaiting_review: 4,
  previewing: 5,
  ready_to_confirm: 6,
  rendering: 7,
  verifying: 8,
  completed: 9,
  partial: 9,
  needs_review: 9,
  failed: 9,
  cancelled: 9,
};

const TERMINAL = new Set<ContentJobStatus>([
  "completed",
  "partial",
  "needs_review",
  "failed",
  "cancelled",
]);

export function mergeContentSnapshot(
  current: ContentJobResponse | null,
  incoming: ContentJobResponse,
): ContentJobResponse {
  if (!current || current.job_id !== incoming.job_id) return incoming;
  if (incoming.revision < current.revision) return current;
  if (
    incoming.revision === current.revision &&
    ORDER[incoming.status] < ORDER[current.status]
  ) {
    return current;
  }
  if (
    incoming.revision === current.revision &&
    ORDER[incoming.status] === ORDER[current.status] &&
    incoming.updated_at < current.updated_at
  ) {
    return current;
  }
  return incoming;
}

export interface ContentLifecycle {
  job: ContentJobResponse | null;
  contentMap: ContentMap | null;
  plan: ContentPlan | null;
  previews: ContentJoinPreview[];
  applySnapshot(snapshot: ContentJobResponse): Promise<boolean>;
  refresh(): Promise<void>;
}

export function useContentLifecycle({
  api,
  jobId,
  onError,
}: {
  api: ContentLifecycleApi;
  jobId: string | null;
  onError?: (error: Error) => void;
}): ContentLifecycle {
  const generation = useRef(0);
  const activeJobId = useRef<string | null>(jobId);
  const latest = useRef<ContentJobResponse | null>(null);
  const lastSequence = useRef(0);
  const [job, setJob] = useState<ContentJobResponse | null>(null);
  const [contentMap, setContentMap] = useState<ContentMap | null>(null);
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [previews, setPreviews] = useState<ContentJoinPreview[]>([]);
  activeJobId.current = jobId;

  const applySnapshot = useCallback(
    async (snapshot: ContentJobResponse): Promise<boolean> => {
      const token = generation.current;
      if (snapshot.job_id !== activeJobId.current) return false;
      if (mergeContentSnapshot(latest.current, snapshot) !== snapshot) return false;
      latest.current = snapshot;
      setJob(snapshot);

      if (
        [
          "awaiting_review",
          "previewing",
          "ready_to_confirm",
          "rendering",
          "verifying",
          "completed",
          "partial",
          "needs_review",
        ].includes(snapshot.status)
      ) {
        const nextMap = await api.getMap(snapshot.job_id);
        if (token !== generation.current || snapshot.job_id !== activeJobId.current) {
          return false;
        }
        setContentMap(nextMap);
      } else {
        setContentMap(null);
      }

      if (
        snapshot.status === "ready_to_confirm" ||
        snapshot.status === "rendering" ||
        snapshot.status === "verifying" ||
        ["completed", "partial", "needs_review"].includes(snapshot.status)
      ) {
        const [nextPlan, nextPreviews] = await Promise.all([
          api.getPlan(snapshot.job_id),
          snapshot.status === "ready_to_confirm"
            ? api.getPreviews(snapshot.job_id)
            : Promise.resolve([]),
        ]);
        if (token !== generation.current || snapshot.job_id !== activeJobId.current) {
          return false;
        }
        setPlan(nextPlan);
        setPreviews(nextPreviews);
      } else {
        setPlan(null);
        setPreviews([]);
      }
      return true;
    },
    [api],
  );

  const refresh = useCallback(async (): Promise<void> => {
    if (!jobId) return;
    try {
      await applySnapshot(await api.getJob(jobId));
    } catch (caught) {
      onError?.(
        caught instanceof Error
          ? caught
          : new Error("Useful-content lifecycle failed"),
      );
    }
  }, [api, applySnapshot, jobId, onError]);

  useEffect(() => {
    const token = generation.current + 1;
    generation.current = token;
    latest.current = null;
    lastSequence.current = 0;
    setJob(null);
    setContentMap(null);
    setPlan(null);
    setPreviews([]);
    if (!jobId) return;

    let active = true;
    const recover = async (): Promise<void> => {
      try {
        const snapshot = await api.getJob(jobId);
        if (active && token === generation.current) await applySnapshot(snapshot);
      } catch (caught) {
        if (active && token === generation.current) {
          onError?.(
            caught instanceof Error
              ? caught
              : new Error("Could not restore useful-content job"),
          );
        }
      }
    };
    void recover();
    const subscription = api.subscribeToEvents(
      jobId,
      (event) => {
        if (event.sequence <= lastSequence.current) return;
        lastSequence.current = event.sequence;
        void recover();
      },
      (error) => {
        if (active && token === generation.current) onError?.(error);
      },
    );
    return () => {
      active = false;
      generation.current += 1;
      subscription.close();
    };
  }, [api, applySnapshot, jobId, onError]);

  return { job, contentMap, plan, previews, applySnapshot, refresh };
}

export { TERMINAL as CONTENT_TERMINAL_STATUSES };
