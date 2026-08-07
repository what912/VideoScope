import { useCallback, useEffect, useRef, useState } from "react";
import type { JobEventSubscription } from "../api";
import type {
  PrivacyJobEvent,
  PrivacyJobResponse,
  PrivacyJobStatus,
  PrivacyPlan,
  PrivacyRiskMap,
  PrivacyTechnicalReport,
} from "../types";

export interface PrivacyLifecycleApi {
  getJob(jobId: string): Promise<PrivacyJobResponse>;
  getRiskMap(jobId: string): Promise<PrivacyRiskMap>;
  getPlan(jobId: string): Promise<PrivacyPlan>;
  getTechnicalReport(jobId: string): Promise<PrivacyTechnicalReport>;
  subscribeToEvents(
    jobId: string,
    onEvent: (event: PrivacyJobEvent) => void,
    onError: (error: Error) => void,
  ): JobEventSubscription;
}

const STATUS_ORDER: Record<PrivacyJobStatus, number> = {
  queued: 0,
  inspecting: 1,
  scanning: 2,
  awaiting_review: 3,
  planning: 4,
  previewing: 5,
  awaiting_confirmation: 6,
  processing: 7,
  verifying: 8,
  completed: 9,
  needs_review: 9,
  partial: 9,
  failed: 9,
  cancelled: 9,
};

export function mergePrivacySnapshot(
  current: PrivacyJobResponse | null,
  incoming: PrivacyJobResponse,
): PrivacyJobResponse {
  if (!current || current.job_id !== incoming.job_id) return incoming;
  const currentRank = STATUS_ORDER[current.status];
  const incomingRank = STATUS_ORDER[incoming.status];
  if (incomingRank < currentRank) return current;
  if (incomingRank === currentRank && incoming.updated_at < current.updated_at) {
    return current;
  }
  return incoming;
}

interface UsePrivacyLifecycleOptions {
  api: PrivacyLifecycleApi;
  jobId: string | null;
  onError?: (error: Error) => void;
}

export interface PrivacyLifecycle {
  job: PrivacyJobResponse | null;
  riskMap: PrivacyRiskMap | null;
  plan: PrivacyPlan | null;
  technicalReport: PrivacyTechnicalReport | null;
  applySnapshot(snapshot: PrivacyJobResponse): Promise<boolean>;
}

export function usePrivacyLifecycle({
  api,
  jobId,
  onError,
}: UsePrivacyLifecycleOptions): PrivacyLifecycle {
  const generation = useRef(0);
  const activeJobId = useRef<string | null>(jobId);
  const latestSnapshot = useRef<PrivacyJobResponse | null>(null);
  const lastSequence = useRef(0);
  const [job, setJob] = useState<PrivacyJobResponse | null>(null);
  const [riskMap, setRiskMap] = useState<PrivacyRiskMap | null>(null);
  const [plan, setPlan] = useState<PrivacyPlan | null>(null);
  const [technicalReport, setTechnicalReport] =
    useState<PrivacyTechnicalReport | null>(null);

  activeJobId.current = jobId;

  const applySnapshot = useCallback(
    async (snapshot: PrivacyJobResponse): Promise<boolean> => {
      const requestGeneration = generation.current;
      if (snapshot.job_id !== activeJobId.current) return false;
      const merged = mergePrivacySnapshot(latestSnapshot.current, snapshot);
      if (merged !== snapshot) return false;
      latestSnapshot.current = snapshot;
      setJob(snapshot);

      if (snapshot.status === "awaiting_review") {
        const loaded = await api.getRiskMap(snapshot.job_id);
        if (
          generation.current !== requestGeneration ||
          activeJobId.current !== snapshot.job_id
        ) {
          return false;
        }
        setRiskMap(loaded);
        setPlan(null);
        setTechnicalReport(null);
      } else if (snapshot.status === "awaiting_confirmation") {
        const loaded = await api.getPlan(snapshot.job_id);
        if (
          generation.current !== requestGeneration ||
          activeJobId.current !== snapshot.job_id
        ) {
          return false;
        }
        setRiskMap(null);
        setPlan(loaded);
        setTechnicalReport(null);
      } else if (snapshot.status === "completed") {
        const loaded = await api.getTechnicalReport(snapshot.job_id);
        if (
          generation.current !== requestGeneration ||
          activeJobId.current !== snapshot.job_id
        ) {
          return false;
        }
        setRiskMap(null);
        setPlan(null);
        setTechnicalReport(loaded);
      } else {
        setRiskMap(null);
        setPlan(null);
        setTechnicalReport(null);
      }
      return true;
    },
    [api],
  );

  useEffect(() => {
    const requestGeneration = generation.current + 1;
    generation.current = requestGeneration;
    latestSnapshot.current = null;
    lastSequence.current = 0;
    setJob(null);
    setRiskMap(null);
    setPlan(null);
    setTechnicalReport(null);
    if (!jobId) return;

    let active = true;
    const loadCurrent = async (): Promise<void> => {
      try {
        const snapshot = await api.getJob(jobId);
        if (!active || generation.current !== requestGeneration) return;
        await applySnapshot(snapshot);
      } catch (caught) {
        if (active && generation.current === requestGeneration) {
          onError?.(
            caught instanceof Error ? caught : new Error("Privacy lifecycle failed"),
          );
        }
      }
    };
    void loadCurrent();
    const subscription = api.subscribeToEvents(
      jobId,
      (event) => {
        if (event.sequence <= lastSequence.current) return;
        lastSequence.current = event.sequence;
        void loadCurrent();
      },
      (error) => {
        if (active && generation.current === requestGeneration) onError?.(error);
      },
    );
    return () => {
      active = false;
      generation.current += 1;
      subscription.close();
    };
  }, [api, applySnapshot, jobId, onError]);

  return { job, riskMap, plan, technicalReport, applySnapshot };
}
