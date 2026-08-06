import { useCallback, useEffect, useRef, useState } from "react";
import type { JobEventSubscription } from "../api";
import type { RescueDamageMap, RescueJobEvent, RescueJobResponse, RescueJobStatus, RescuePlan, RescueTechnicalReport } from "../types";

export interface RescueLifecycleApi {
  getJob(jobId: string): Promise<RescueJobResponse>;
  getDamageMap(jobId: string): Promise<RescueDamageMap>;
  getPlan(jobId: string): Promise<RescuePlan>;
  getTechnicalReport(jobId: string): Promise<RescueTechnicalReport>;
  subscribeToEvents(jobId: string, onEvent: (event: RescueJobEvent) => void, onError: (error: Error) => void): JobEventSubscription;
}

const ORDER: Record<RescueJobStatus, number> = { queued: 0, scanning: 1, planning: 2, previewing: 3, awaiting_confirmation: 4, processing: 5, verifying: 6, completed: 7, needs_review: 7, partial: 7, failed: 7, cancelled: 7 };
const TERMINAL = new Set<RescueJobStatus>(["completed", "needs_review", "partial", "failed", "cancelled"]);

export function mergeRescueSnapshot(current: RescueJobResponse | null, incoming: RescueJobResponse): RescueJobResponse {
  if (!current || current.job_id !== incoming.job_id) return incoming;
  if (ORDER[incoming.status] < ORDER[current.status]) return current;
  if (ORDER[incoming.status] === ORDER[current.status] && incoming.updated_at < current.updated_at) return current;
  return incoming;
}

export interface RescueLifecycle { job: RescueJobResponse | null; damageMap: RescueDamageMap | null; plan: RescuePlan | null; technicalReport: RescueTechnicalReport | null; applySnapshot(snapshot: RescueJobResponse): Promise<boolean>; }

export function useRescueLifecycle({ api, jobId, onError }: { api: RescueLifecycleApi; jobId: string | null; onError?: (error: Error) => void }): RescueLifecycle {
  const generation = useRef(0); const activeJobId = useRef<string | null>(jobId); const latest = useRef<RescueJobResponse | null>(null); const lastSequence = useRef(0);
  const [job, setJob] = useState<RescueJobResponse | null>(null); const [damageMap, setDamageMap] = useState<RescueDamageMap | null>(null); const [plan, setPlan] = useState<RescuePlan | null>(null); const [technicalReport, setTechnicalReport] = useState<RescueTechnicalReport | null>(null);
  activeJobId.current = jobId;
  const applySnapshot = useCallback(async (snapshot: RescueJobResponse): Promise<boolean> => {
    const token = generation.current;
    if (snapshot.job_id !== activeJobId.current) return false;
    if (mergeRescueSnapshot(latest.current, snapshot) !== snapshot) return false;
    latest.current = snapshot; setJob(snapshot);
    if (["awaiting_confirmation", "previewing"].includes(snapshot.status)) {
      const [map, nextPlan] = await Promise.all([api.getDamageMap(snapshot.job_id), api.getPlan(snapshot.job_id)]);
      if (token !== generation.current || activeJobId.current !== snapshot.job_id) return false;
      setDamageMap(map); setPlan(nextPlan); setTechnicalReport(null);
    } else if (TERMINAL.has(snapshot.status)) {
      if (["completed", "needs_review", "partial"].includes(snapshot.status)) {
        const report = await api.getTechnicalReport(snapshot.job_id);
        if (token !== generation.current || activeJobId.current !== snapshot.job_id) return false;
        setTechnicalReport(report);
      } else setTechnicalReport(null);
      setPlan(null);
    } else { setDamageMap(null); setPlan(null); setTechnicalReport(null); }
    return true;
  }, [api]);
  useEffect(() => {
    const token = generation.current + 1; generation.current = token; latest.current = null; lastSequence.current = 0; setJob(null); setDamageMap(null); setPlan(null); setTechnicalReport(null);
    if (!jobId) return;
    let active = true;
    const refresh = async (): Promise<void> => { try { const snapshot = await api.getJob(jobId); if (active && token === generation.current) await applySnapshot(snapshot); } catch (caught) { if (active && token === generation.current) onError?.(caught instanceof Error ? caught : new Error("Video Rescue lifecycle failed")); } };
    void refresh();
    const sub = api.subscribeToEvents(jobId, (event) => { if (event.sequence <= lastSequence.current) return; lastSequence.current = event.sequence; void refresh(); }, (error) => { if (active && token === generation.current) onError?.(error); });
    return () => { active = false; generation.current += 1; sub.close(); };
  }, [api, applySnapshot, jobId, onError]);
  return { job, damageMap, plan, technicalReport, applySnapshot };
}
