import { useCallback, useEffect, useRef, useState } from "react";

import type {
  AnalysisProgress,
  BrowserAnalysisOptions,
  BrowserAnalysisService,
} from "../../services/browser-analysis";
import { BrowserAnalysisError } from "../../services/browser-analysis/errors";
import type { ReportStore } from "../../services/report-store/report-store";
import type { RealBrowserReport } from "../../types/report";
import { setSessionVideo } from "./session-video-store";

export type PublicAnalysisErrorCode =
  | "invalid_input"
  | "metadata_unavailable"
  | "duration_unavailable"
  | "decode_failed"
  | "canvas_unavailable"
  | "memory_pressure"
  | "analysis_failed"
  | "storage_failed";

export interface PublicAnalysisError {
  code: PublicAnalysisErrorCode;
}

export type JobState =
  | { status: "idle" }
  | { status: "running"; progress: AnalysisProgress }
  | { status: "completed"; report: RealBrowserReport }
  | { status: "cancelled" }
  | { status: "failed"; error: PublicAnalysisError };

export interface AnalysisJobDependencies {
  analysisService: BrowserAnalysisService;
  reportStore: ReportStore;
  navigate(path: string): void;
  createObjectURL(file: File): string;
  revokeObjectURL(url: string): void;
}

interface ActiveRun {
  controller: AbortController;
  objectUrl: string;
  ownsObjectUrl: boolean;
  cancelled: boolean;
  revokeObjectURL(url: string): void;
}

function publicError(error: unknown): PublicAnalysisError {
  if (error instanceof BrowserAnalysisError) {
    return { code: error.code };
  }
  return { code: "analysis_failed" };
}

export function useAnalysisJob(dependencies: AnalysisJobDependencies) {
  const [state, setState] = useState<JobState>({ status: "idle" });
  const activeRun = useRef<ActiveRun | null>(null);
  const persistenceQueue = useRef<Promise<void>>(Promise.resolve());

  const releaseRun = useCallback((run: ActiveRun) => {
    if (run.ownsObjectUrl) {
      run.revokeObjectURL(run.objectUrl);
      run.ownsObjectUrl = false;
    }
  }, []);

  const invalidateRun = useCallback((run: ActiveRun) => {
    run.cancelled = true;
    run.controller.abort();
    releaseRun(run);
    if (activeRun.current === run) {
      activeRun.current = null;
    }
  }, [releaseRun]);

  const releaseActiveRun = useCallback(() => {
    const run = activeRun.current;
    if (run) invalidateRun(run);
  }, [invalidateRun]);

  const cancel = useCallback(() => {
    const run = activeRun.current;
    if (!run) return;
    invalidateRun(run);
    setState({ status: "cancelled" });
  }, [invalidateRun]);

  const reset = useCallback(() => {
    releaseActiveRun();
    setState({ status: "idle" });
  }, [releaseActiveRun]);

  const start = useCallback(
    async (file: File, options: BrowserAnalysisOptions) => {
      releaseActiveRun();
      const controller = new AbortController();
      const objectUrl = dependencies.createObjectURL(file);
      activeRun.current = {
        controller,
        objectUrl,
        ownsObjectUrl: true,
        cancelled: false,
        revokeObjectURL: dependencies.revokeObjectURL,
      };
      const run = activeRun.current;
      const isActive = () =>
        activeRun.current === run &&
        !run.cancelled &&
        !run.controller.signal.aborted;
      setState({
        status: "running",
        progress: { stage: "validating", progress: 0 },
      });
      try {
        const report = await dependencies.analysisService.analyzeLocalVideo(
          file,
          options,
          controller.signal,
          (progress) => {
            if (isActive()) {
              setState({ status: "running", progress });
            }
          },
        );
        if (!isActive()) return;
        let persistedForRun = false;
        try {
          const persistenceOperation = persistenceQueue.current.then(
            async () => {
              if (!isActive()) return false;
              const previousReport = await dependencies.reportStore.get(
                report.id,
              );
              if (!isActive()) return false;
              await dependencies.reportStore.put(report);
              if (!isActive()) {
                try {
                  if (previousReport) {
                    await dependencies.reportStore.put(previousReport);
                  } else {
                    await dependencies.reportStore.delete(report.id);
                  }
                } catch {
                  // Keep cancellation stable when best-effort cleanup fails.
                }
                return false;
              }
              return true;
            },
          );
          persistenceQueue.current = persistenceOperation.then(
            () => undefined,
            () => undefined,
          );
          persistedForRun = await persistenceOperation;
        } catch {
          if (!isActive()) return;
          throw { storageFailure: true };
        }
        if (!persistedForRun || !isActive()) return;
        setSessionVideo(
          { reportId: report.id, file, objectUrl },
          dependencies.revokeObjectURL,
        );
        run.ownsObjectUrl = false;
        if (!isActive()) return;
        activeRun.current = null;
        setState({ status: "completed", report });
        if (run.cancelled || run.controller.signal.aborted) return;
        dependencies.navigate(
          `/workspace?report=${encodeURIComponent(report.id)}`,
        );
      } catch (error) {
        if (!isActive()) return;
        releaseRun(run);
        activeRun.current = null;
        if (
          controller.signal.aborted ||
          (error instanceof DOMException && error.name === "AbortError")
        ) {
          setState({ status: "cancelled" });
          return;
        }
        if (
          typeof error === "object" &&
          error !== null &&
          "storageFailure" in error
        ) {
          setState({
            status: "failed",
            error: { code: "storage_failed" },
          });
          return;
        }
        setState({ status: "failed", error: publicError(error) });
      }
    },
    [dependencies, releaseActiveRun, releaseRun],
  );

  useEffect(
    () => () => {
      releaseActiveRun();
    },
    [releaseActiveRun],
  );

  return { state, start, cancel, reset };
}
