import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useSearchParams } from "react-router";

import {
  DiagnosticTimeline,
  IssueDetailPanel,
  IssueList,
  VideoPlayer,
} from "../../components/diagnostics";
import { LoadingState } from "../../components/feedback/LoadingState";
import { ErrorState } from "../../components/feedback/ErrorState";
import { useI18n } from "../../i18n/I18nProvider";
import { hashFileIncrementally } from "../../services/browser-analysis/hash";
import {
  createReportStore,
  type ReportIndexEntry,
  type ReportStore,
} from "../../services/report-store/report-store";
import type { Finding, Severity } from "../../types/analysis";
import type { BrowserReport } from "../../types/report";
import {
  clearSessionVideo as clearStoredSessionVideo,
  getSessionVideo as readSessionVideo,
  setSessionVideo,
  type SessionVideo,
} from "../upload/session-video-store";
import { WorkspaceFilters } from "./WorkspaceFilters";
import { WorkspaceConfirmDialog } from "./WorkspaceConfirmDialog";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { MobileFindingSheet } from "./MobileFindingSheet";
import { WorkspaceProjectRail } from "./WorkspaceProjectRail";
import { WorkspaceSignals } from "./WorkspaceSignals";
import { WorkspaceToolbar } from "./WorkspaceToolbar";
import { exportWorkspaceReport } from "./workspace-export";
import "./workspace.css";

const defaultStore = createReportStore().then(({ store }) => store);

function preciseTimestamp(seconds: number) {
  const safeSeconds = Number.isFinite(seconds) ? Math.max(0, seconds) : 0;
  const minutes = Math.floor(safeSeconds / 60);
  const remainder = safeSeconds - minutes * 60;
  return `${String(minutes).padStart(2, "0")}:${remainder
    .toFixed(3)
    .padStart(6, "0")}`;
}

function useMobileLayout(override?: boolean) {
  const [matches, setMatches] = useState(() =>
    override !== undefined
      ? override
      : typeof window.matchMedia === "function" &&
        window.matchMedia("(max-width: 63.99rem)").matches,
  );

  useEffect(() => {
    if (override !== undefined || typeof window.matchMedia !== "function") {
      setMatches(override ?? false);
      return;
    }
    const query = window.matchMedia("(max-width: 63.99rem)");
    const update = () => setMatches(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, [override]);

  return matches;
}

export interface WorkspacePageProps {
  reportStore?: ReportStore;
  getSessionVideo?(): SessionVideo | null;
  clearSession?(): void;
  navigate?(path: string): void;
  writeClipboard?(value: string): Promise<void>;
  exportReport?(report: BrowserReport): void;
  hashFile?(file: File, signal: AbortSignal): Promise<string>;
  replaceSessionVideo?(session: SessionVideo): void;
  isMobile?: boolean;
}

type LoadState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "missing"; reason: "no-selection" | "not-found" }
  | { status: "ready"; report: BrowserReport };

type ActionMessage = {
  text: string;
  tone: "error" | "status" | "success";
};

export function WorkspacePage({
  reportStore,
  getSessionVideo = readSessionVideo,
  clearSession = clearStoredSessionVideo,
  navigate: navigateOverride,
  writeClipboard = (value) => navigator.clipboard.writeText(value),
  exportReport = exportWorkspaceReport,
  hashFile = hashFileIncrementally,
  replaceSessionVideo = setSessionVideo,
  isMobile: mobileOverride,
}: WorkspacePageProps = {}) {
  const { t } = useI18n();
  const routerNavigate = useNavigate();
  const navigate = navigateOverride ?? routerNavigate;
  const [searchParams] = useSearchParams();
  const reportId = searchParams.get("report");
  const isMobile = useMobileLayout(mobileOverride);
  const [loadState, setLoadState] = useState<LoadState>({ status: "loading" });
  const [store, setStore] = useState<ReportStore | null>(reportStore ?? null);
  const [reportIndexes, setReportIndexes] = useState<ReportIndexEntry[]>([]);
  const [session, setSession] = useState<SessionVideo | null>(null);
  const [selectedFindingId, setSelectedFindingId] = useState<string>();
  const [reviewedIds, setReviewedIds] = useState<ReadonlySet<string>>(
    new Set(),
  );
  const [detectorFilter, setDetectorFilter] = useState("all");
  const [severityFilter, setSeverityFilter] = useState<Severity | "all">("all");
  const [currentTime, setCurrentTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [playbackRate, setPlaybackRate] = useState(1);
  const [railOpen, setRailOpen] = useState(!isMobile);
  const [signalsOpen, setSignalsOpen] = useState(true);
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false);
  const [actionMessage, setActionMessage] = useState<ActionMessage>();
  const [loadRevision, setLoadRevision] = useState(0);
  const [clearDialogOpen, setClearDialogOpen] = useState(false);
  const [clearingLocalData, setClearingLocalData] = useState(false);
  const [clearError, setClearError] = useState<string>();
  const mobileDetailTrigger = useRef<HTMLElement | null>(null);
  const projectRailTrigger = useRef<HTMLButtonElement | null>(null);
  const clearDialogTrigger = useRef<HTMLButtonElement | null>(null);
  const reviewedIdsRef = useRef<ReadonlySet<string>>(new Set());
  const reportRef = useRef<BrowserReport | undefined>(undefined);
  const mutationQueue = useRef<Promise<void>>(Promise.resolve());
  const clearInProgress = useRef(false);
  const reselectAbort = useRef<AbortController | undefined>(undefined);
  const reselectRevision = useRef(0);
  const activeReportId = useRef<string | null>(reportId);
  const previousMobileDetailOpen = useRef(false);
  const previousRailOpen = useRef(railOpen);
  const previousClearDialogOpen = useRef(false);
  activeReportId.current = reportId;

  useEffect(() => {
    setRailOpen(!isMobile);
  }, [isMobile]);

  useEffect(() => {
    reselectRevision.current += 1;
    reselectAbort.current?.abort();
    setActionMessage(undefined);
  }, [reportId]);

  useEffect(() => {
    if (previousMobileDetailOpen.current && !mobileDetailOpen) {
      const trigger = mobileDetailTrigger.current;
      if (trigger?.isConnected) trigger.focus();
    }
    previousMobileDetailOpen.current = mobileDetailOpen;
  }, [mobileDetailOpen]);

  useEffect(() => {
    if (isMobile && previousRailOpen.current && !railOpen) {
      const trigger = projectRailTrigger.current;
      if (trigger?.isConnected) trigger.focus();
    }
    previousRailOpen.current = railOpen;
  }, [isMobile, railOpen]);

  useEffect(() => {
    if (previousClearDialogOpen.current && !clearDialogOpen) {
      const trigger = clearDialogTrigger.current;
      if (trigger?.isConnected) trigger.focus();
    }
    previousClearDialogOpen.current = clearDialogOpen;
  }, [clearDialogOpen]);

  useEffect(() => {
    let active = true;
    if (reportStore) {
      setStore(reportStore);
      return () => {
        active = false;
      };
    }
    void defaultStore.then((resolvedStore) => {
      if (active) setStore(resolvedStore);
    });
    return () => {
      active = false;
    };
  }, [reportStore]);

  useEffect(() => {
    let active = true;
    if (!reportId) {
      setLoadState({ status: "missing", reason: "no-selection" });
      return () => {
        active = false;
      };
    }
    if (!store) {
      setLoadState({ status: "loading" });
      return () => {
        active = false;
      };
    }
    setLoadState({ status: "loading" });
    void Promise.all([store.get(reportId), store.list()])
      .then(([report, indexes]) => {
        if (!active) return;
        setReportIndexes(indexes);
        if (!report) {
          setLoadState({ status: "missing", reason: "not-found" });
          return;
        }
        reportRef.current = report;
        reviewedIdsRef.current = new Set(report.reviewed_finding_ids);
        setLoadState({ status: "ready", report });
        setReviewedIds(new Set(report.reviewed_finding_ids));
        setSelectedFindingId(report.findings[0]?.id);
        const currentSession = getSessionVideo();
        setSession(
          currentSession?.reportId === report.id ? currentSession : null,
        );
      })
      .catch(() => {
        if (active) setLoadState({ status: "error" });
      });
    return () => {
      active = false;
    };
  }, [getSessionVideo, loadRevision, reportId, store]);

  useEffect(
    () => () => {
      reselectAbort.current?.abort();
    },
    [],
  );

  const report = loadState.status === "ready" ? loadState.report : undefined;
  const filteredFindings = useMemo(
    () =>
      report?.findings.filter(
        (finding) =>
          (detectorFilter === "all" ||
            finding.detector_id === detectorFilter) &&
          (severityFilter === "all" ||
            finding.severity === severityFilter),
      ) ?? [],
    [detectorFilter, report, severityFilter],
  );
  const selectedFinding = report?.findings.find(
    (finding) => finding.id === selectedFindingId,
  );
  const detectorIds = useMemo(
    () =>
      report
        ? [...new Set(report.findings.map((finding) => finding.detector_id))].sort()
        : [],
    [report],
  );

  useEffect(() => {
    if (
      selectedFindingId &&
      filteredFindings.some((finding) => finding.id === selectedFindingId)
    ) {
      return;
    }
    const nextFinding = filteredFindings[0];
    setSelectedFindingId(nextFinding?.id);
    if (nextFinding) {
      setCurrentTime(nextFinding.time_range.start_seconds);
    }
  }, [filteredFindings, selectedFindingId]);

  const selectFinding = useCallback(
    (finding: Finding) => {
      setSelectedFindingId(finding.id);
      setCurrentTime(finding.time_range.start_seconds);
      setPlaying(false);
      if (isMobile) {
        setRailOpen(false);
        mobileDetailTrigger.current =
          document.activeElement instanceof HTMLElement
            ? document.activeElement
            : null;
        setMobileDetailOpen(true);
      }
    },
    [isMobile],
  );

  const closeMobileDetail = useCallback(() => {
    setMobileDetailOpen(false);
  }, []);

  const persistReviewed = useCallback(
    async (finding: Finding, reviewed: boolean) => {
      const currentReport = reportRef.current ?? report;
      if (!currentReport || !store || clearInProgress.current) return;
      const nextIds = new Set(reviewedIdsRef.current);
      if (reviewed) nextIds.add(finding.id);
      else nextIds.delete(finding.id);
      reviewedIdsRef.current = nextIds;
      setReviewedIds(nextIds);
      const nextReport: BrowserReport = {
        ...currentReport,
        reviewed_finding_ids: [...nextIds].sort(),
      };
      reportRef.current = nextReport;
      setLoadState({ status: "ready", report: nextReport });
      const write = mutationQueue.current
        .catch(() => undefined)
        .then(() => store.put(nextReport));
      mutationQueue.current = write.catch(() => {
        if (!clearInProgress.current) {
          setActionMessage({
            text: t.workspace.reviewSaveFailed,
            tone: "error",
          });
        }
      });
      await mutationQueue.current;
    },
    [report, store, t.workspace.reviewSaveFailed],
  );

  const frameStep =
    report?.metadata.frame_rate && report.metadata.frame_rate > 0
      ? 1 / report.metadata.frame_rate
      : 1 / 30;

  const reselectVideo = async (file: File) => {
    if (!report) return;
    reselectAbort.current?.abort();
    const controller = new AbortController();
    reselectAbort.current = controller;
    const operationRevision = reselectRevision.current + 1;
    reselectRevision.current = operationRevision;
    const expectedReportId = report.id;
    const expectedInputHash = report.input_hash;
    setActionMessage({
      text: t.workspace.verifyingVideo,
      tone: "status",
    });
    try {
      const inputHash = await hashFile(file, controller.signal);
      if (
        controller.signal.aborted ||
        operationRevision !== reselectRevision.current ||
        activeReportId.current !== expectedReportId ||
        reportRef.current?.id !== expectedReportId
      ) {
        return;
      }
      if (inputHash !== expectedInputHash) {
        setActionMessage({
          text: t.workspace.videoHashMismatch,
          tone: "error",
        });
        return;
      }
      const objectUrl = URL.createObjectURL(file);
      const nextSession = { reportId: expectedReportId, file, objectUrl };
      replaceSessionVideo(nextSession);
      setSession(nextSession);
      setActionMessage(undefined);
    } catch {
      if (
        !controller.signal.aborted &&
        operationRevision === reselectRevision.current &&
        activeReportId.current === expectedReportId
      ) {
        setActionMessage({
          text: t.workspace.videoHashFailed,
          tone: "error",
        });
      }
    }
  };

  const clearLocalData = async () => {
    if (!store) return;
    clearInProgress.current = true;
    reselectRevision.current += 1;
    reselectAbort.current?.abort();
    setClearingLocalData(true);
    setClearError(undefined);
    const clearMutation = mutationQueue.current
      .catch(() => undefined)
      .then(() => store.clear());
    mutationQueue.current = clearMutation.catch(() => undefined);
    try {
      await clearMutation;
      clearSession();
      setSession(null);
      setReportIndexes([]);
      setClearDialogOpen(false);
      setLoadState({ status: "missing", reason: "not-found" });
    } catch {
      setClearError(t.workspace.clearLocalDataFailed);
    } finally {
      clearInProgress.current = false;
      setClearingLocalData(false);
    }
  };

  if (!reportId || loadState.status === "missing") {
    const unavailable =
      Boolean(reportId) &&
      loadState.status === "missing" &&
      loadState.reason === "not-found";
    return (
      <section
        className="workspace-state"
        aria-labelledby="workspace-state-title"
      >
        <p className="eyebrow">{t.workspace.eyebrow}</p>
        <h1 id="workspace-state-title">
          {unavailable
            ? t.workspace.unavailableReportTitle
            : t.workspace.missingReportTitle}
        </h1>
        <p>
          {unavailable
            ? t.workspace.unavailableReportMessage
            : t.workspace.missingReportMessage}
        </p>
        <button
          className="button button--primary"
          onClick={() => navigate("/")}
          type="button"
        >
          {t.workspace.newAnalysis}
        </button>
      </section>
    );
  }

  if (loadState.status === "loading" || !report) {
    if (loadState.status === "error") {
      return (
        <section className="workspace-state">
          <ErrorState
            message={t.workspace.storageErrorMessage}
            onRetry={() => setLoadRevision((revision) => revision + 1)}
            title={t.workspace.storageErrorTitle}
          />
          <button
            className="button button--primary"
            onClick={() => navigate("/")}
            type="button"
          >
            {t.workspace.newAnalysis}
          </button>
        </section>
      );
    }
    return <LoadingState />;
  }

  const completedDetectorCount = report.detector_executions.filter(
    (execution) => execution.status === "ok",
  ).length;
  const failedDetectorCount = report.detector_executions.filter(
    (execution) => execution.status === "failed",
  ).length;
  const emptyFindingMessage =
    completedDetectorCount === 0
      ? t.workspace.incompleteNoFindings
      : failedDetectorCount > 0
        ? t.workspace.partialNoFindings
        : t.diagnostics.noFindings;
  const modalOpen =
    clearDialogOpen || (isMobile && (railOpen || mobileDetailOpen));

  return (
    <>
      <section
        aria-hidden={modalOpen ? "true" : undefined}
        aria-labelledby="workspace-title"
        className="workspace"
        data-testid="workspace-surface"
        inert={modalOpen ? true : undefined}
      >
      <WorkspaceToolbar
        currentTime={currentTime}
        onClear={(trigger) => {
          clearDialogTrigger.current = trigger;
          setClearError(undefined);
          setClearDialogOpen(true);
        }}
        onCopy={() => {
          void writeClipboard(preciseTimestamp(currentTime))
            .then(() =>
              setActionMessage({
                text: t.workspace.timestampCopied,
                tone: "success",
              }),
            )
            .catch(() =>
              setActionMessage({
                text: t.workspace.timestampCopyFailed,
                tone: "error",
              }),
            );
        }}
        onExport={() => exportReport(report)}
        onFrameStep={(direction) =>
          setCurrentTime((time) =>
            Math.min(
              report.metadata.duration_seconds,
              Math.max(0, time + direction * frameStep),
            ),
          )
        }
        onNewAnalysis={() => navigate("/")}
        onPrint={() => window.print()}
        onRailToggle={(trigger) => {
          projectRailTrigger.current = trigger;
          if (isMobile) setMobileDetailOpen(false);
          setRailOpen((open) => !open);
        }}
        onSpeedChange={setPlaybackRate}
        playbackRate={playbackRate}
        railOpen={railOpen}
      />
      {actionMessage ? (
        <p
          className="workspace__action-message"
          data-status={actionMessage.tone}
          role={actionMessage.tone === "error" ? "alert" : "status"}
        >
          {actionMessage.text}
        </p>
      ) : null}

      <WorkspaceHeader
        onReselect={(file) => void reselectVideo(file)}
        report={report}
        sessionLoaded={Boolean(session)}
      />

      <div
        className="workspace__grid"
        data-rail-open={railOpen ? "true" : "false"}
        data-testid="workspace-grid"
      >
        {railOpen ? (
          <WorkspaceProjectRail
            activeReportId={report.id}
            entries={reportIndexes}
            isMobile={isMobile}
            onClose={() => {
              setRailOpen(false);
            }}
            onSelect={(id) => {
              setRailOpen(false);
              navigate(`/workspace?report=${encodeURIComponent(id)}`);
            }}
          />
        ) : null}

        <div className="workspace__stage">
          <VideoPlayer
            currentTime={currentTime}
            duration={report.metadata.duration_seconds}
            onPlayingChange={setPlaying}
            onSeek={setCurrentTime}
            onTimeUpdate={setCurrentTime}
            playbackRate={playbackRate}
            playing={playing}
            selectedFinding={selectedFinding}
            src={
              session?.reportId === report.id ? session.objectUrl : undefined
            }
            videoHeight={report.metadata.height}
            videoWidth={report.metadata.width}
          />
          <DiagnosticTimeline
            currentTime={currentTime}
            duration={report.metadata.duration_seconds}
            findings={filteredFindings}
            onSeek={setCurrentTime}
            onSelectFinding={selectFinding}
            seekStep={frameStep}
            selectedFindingId={selectedFindingId}
          />
          <WorkspaceSignals
            currentTime={currentTime}
            onToggle={() => setSignalsOpen((open) => !open)}
            open={signalsOpen}
            report={report}
          />
        </div>

        <aside className="workspace__issues">
          <WorkspaceFilters
            detectorFilter={detectorFilter}
            detectorIds={detectorIds}
            onDetectorChange={setDetectorFilter}
            onSeverityChange={setSeverityFilter}
            severityFilter={severityFilter}
          />
          {report.findings.length === 0 ? (
            <p className="diagnostic-empty">{emptyFindingMessage}</p>
          ) : filteredFindings.length === 0 ? (
            <p className="diagnostic-empty">
              {t.workspace.noFilterMatches}
            </p>
          ) : (
            <IssueList
              findings={filteredFindings}
              onReviewChange={(finding, reviewed) =>
                void persistReviewed(finding, reviewed)
              }
              onSelectFinding={selectFinding}
              reviewedFindingIds={reviewedIds}
              selectedFindingId={selectedFindingId}
            />
          )}
          {!isMobile ? (
            <IssueDetailPanel
              finding={selectedFinding}
              onEvidenceSeek={setCurrentTime}
            />
          ) : null}
        </aside>
      </div>

      {isMobile && mobileDetailOpen && selectedFinding ? (
        <MobileFindingSheet
          finding={selectedFinding}
          onClose={closeMobileDetail}
          onEvidenceSeek={setCurrentTime}
        />
      ) : null}
      </section>
      {clearDialogOpen ? (
        <WorkspaceConfirmDialog
          busy={clearingLocalData}
          error={clearError}
          onCancel={() => {
            if (!clearingLocalData) {
              setClearDialogOpen(false);
              setClearError(undefined);
            }
          }}
          onConfirm={() => void clearLocalData()}
        />
      ) : null}
    </>
  );
}
