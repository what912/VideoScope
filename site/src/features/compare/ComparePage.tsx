import { useEffect, useMemo, useRef, useState } from "react";

import { formatTimestamp } from "../../components/diagnostics/diagnostic-geometry";
import { useI18n } from "../../i18n/I18nProvider";
import {
  browserAnalysisService,
  defaultBrowserAnalysisOptions,
  type BrowserAnalysisService,
} from "../../services/browser-analysis";
import type { Finding } from "../../types/analysis";
import { parseCompatibleBrowserReport } from "./compare-inputs";
import {
  comparisonPlaybackRates,
  compareReports,
  findPairedFinding,
  reconcileFindingSelection,
  seekComparison,
  seekComparisonAtSharedPosition,
  swapComparison,
  updateSynchronizedPlaying,
  type ComparisonFindingSelection,
  type ComparisonPlayback,
  type ComparisonSide as Side,
  type ComparisonSlot,
  type ComparisonTimelineMode,
} from "./comparison";
import { ComparisonSide } from "./ComparisonSide";
import { DetectorDifferenceTable } from "./DetectorDifferenceTable";
import { EvidencePair } from "./EvidencePair";
import "./compare.css";

export interface ComparePageProps {
  initialA?: ComparisonSlot;
  initialB?: ComparisonSlot;
  analysisService?: BrowserAnalysisService;
  parseReport?: typeof parseCompatibleBrowserReport;
  createObjectURL?(file: File): string;
  revokeObjectURL?(url: string): void;
}

const initialPlayback: ComparisonPlayback = {
  aSeconds: 0,
  bSeconds: 0,
  playing: false,
};

function defaultCreateObjectURL(file: File) {
  return URL.createObjectURL(file);
}

function defaultRevokeObjectURL(url: string) {
  URL.revokeObjectURL(url);
}

export function ComparePage({
  initialA,
  initialB,
  analysisService = browserAnalysisService,
  parseReport = parseCompatibleBrowserReport,
  createObjectURL = defaultCreateObjectURL,
  revokeObjectURL = defaultRevokeObjectURL,
}: ComparePageProps) {
  const { locale, t } = useI18n();
  const [a, setA] = useState<ComparisonSlot | undefined>(initialA);
  const [b, setB] = useState<ComparisonSlot | undefined>(initialB);
  const [fileA, setFileA] = useState<File>();
  const [fileB, setFileB] = useState<File>();
  const [playback, setPlayback] =
    useState<ComparisonPlayback>(initialPlayback);
  const [aPlaying, setAPlaying] = useState(false);
  const [bPlaying, setBPlaying] = useState(false);
  const [synchronized, setSynchronized] = useState(true);
  const [timelineMode, setTimelineMode] =
    useState<ComparisonTimelineMode>("absolute");
  const [findingSelection, setFindingSelection] =
    useState<ComparisonFindingSelection>({});
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<string>();
  const abortRef = useRef<AbortController | undefined>(undefined);
  const runIdRef = useRef(0);
  const reportOperationIds = useRef({ a: 0, b: 0 });
  const ownedUrls = useRef(new Set<string>());
  const mounted = useRef(true);

  const comparison = useMemo(
    () => (a && b ? compareReports(a.report, b.report) : undefined),
    [a, b],
  );
  const aDuration = a?.report.metadata.duration_seconds ?? 0;
  const bDuration = b?.report.metadata.duration_seconds ?? 0;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      runIdRef.current += 1;
      abortRef.current?.abort();
      ownedUrls.current.forEach((url) => revokeObjectURL(url));
      ownedUrls.current.clear();
    };
  }, [revokeObjectURL]);

  useEffect(() => {
    if (!a || !b) return;
    setFindingSelection((current) => {
      const next = reconcileFindingSelection(
        a.report,
        b.report,
        current,
        timelineMode,
      );
      return JSON.stringify(next) === JSON.stringify(current)
        ? current
        : next;
    });
  }, [a, b, timelineMode]);

  const playbackRates = comparisonPlaybackRates(
    aDuration,
    bDuration,
    synchronized,
    timelineMode,
  );

  function releaseOwnedSlot(slot: ComparisonSlot | undefined) {
    if (slot?.mediaUrl && ownedUrls.current.delete(slot.mediaUrl)) {
      revokeObjectURL(slot.mediaUrl);
    }
  }

  function replaceSlot(side: Side, slot: ComparisonSlot) {
    if (side === "a") {
      setA((current) => {
        releaseOwnedSlot(current);
        return slot;
      });
    } else {
      setB((current) => {
        releaseOwnedSlot(current);
        return slot;
      });
    }
  }

  function seek(side: Side, seconds: number) {
    setPlayback((current) =>
      seekComparison(current, side, seconds, {
        aDuration,
        bDuration,
        synchronized,
        timelineMode,
      }),
    );
  }

  function setSidePlaying(side: Side, next: boolean) {
    const state = updateSynchronizedPlaying(
      side,
      next,
      { a: aPlaying, b: bPlaying },
      playback,
      { aDuration, bDuration, synchronized, timelineMode },
    );
    setAPlaying(state.a);
    setBPlaying(state.b);
    setPlayback((current) => ({ ...current, playing: state.playing }));
  }

  function toggleSharedPlayback() {
    const next = !(aPlaying || bPlaying);
    setAPlaying(next);
    setBPlaying(next);
    setPlayback((current) => ({ ...current, playing: next }));
  }

  function toggleSynchronization(next: boolean) {
    setSynchronized(next);
    if (next) {
      setPlayback((current) =>
        seekComparison(current, "a", current.aSeconds, {
          aDuration,
          bDuration,
          synchronized: true,
          timelineMode,
        }),
      );
      const nextPlaying = aPlaying || bPlaying;
      setAPlaying(nextPlaying);
      setBPlaying(nextPlaying);
    }
  }

  function changeTimelineMode(next: ComparisonTimelineMode) {
    setTimelineMode(next);
    if (synchronized) {
      setPlayback((current) =>
        seekComparison(current, "a", current.aSeconds, {
          aDuration,
          bDuration,
          synchronized: true,
          timelineMode: next,
        }),
      );
    }
  }

  function handleSwap() {
    if (busy || !a || !b) return;
    reportOperationIds.current.a += 1;
    reportOperationIds.current.b += 1;
    const swapped = swapComparison(a, b, playback);
    setA(swapped.a);
    setB(swapped.b);
    setPlayback(swapped.playback);
    setAPlaying(bPlaying);
    setBPlaying(aPlaying);
    setFileA(fileB);
    setFileB(fileA);
    setFindingSelection((current) => ({
      detectorId: current.detectorId,
      aFindingId: current.bFindingId,
      bFindingId: current.aFindingId,
      anchorSide:
        current.anchorSide === "a"
          ? "b"
          : current.anchorSide === "b"
            ? "a"
            : undefined,
    }));
  }

  async function loadReport(side: Side, file: File | undefined) {
    if (busy || !file) return;
    const operationId = ++reportOperationIds.current[side];
    setError(undefined);
    try {
      const report = await parseReport(file);
      if (
        !mounted.current ||
        operationId !== reportOperationIds.current[side]
      ) {
        return;
      }
      replaceSlot(side, { report });
      if (side === "a") setFileA(undefined);
      else setFileB(undefined);
      setPlayback(initialPlayback);
    } catch {
      if (
        mounted.current &&
        operationId === reportOperationIds.current[side]
      ) {
        setError(t.compare.inputError);
      }
    }
  }

  async function analyzeBoth() {
    if (!fileA || !fileB) {
      setError(t.compare.bothFilesRequired);
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const runId = ++runIdRef.current;
    reportOperationIds.current.a += 1;
    reportOperationIds.current.b += 1;
    const aFile = fileA;
    const bFile = fileB;
    setBusy(true);
    setProgress(0);
    setError(undefined);
    const options = {
      ...structuredClone(defaultBrowserAnalysisOptions),
      locale,
      reduced_motion:
        typeof matchMedia === "function" &&
        matchMedia("(prefers-reduced-motion: reduce)").matches,
    };
    let aProgress = 0;
    let bProgress = 0;
    try {
      const [aReport, bReport] = await Promise.all([
        analysisService.analyzeLocalVideo(
          aFile,
          options,
          controller.signal,
          (event) => {
            aProgress = event.progress;
            if (mounted.current && runId === runIdRef.current) {
              setProgress((aProgress + bProgress) / 2);
            }
          },
        ),
        analysisService.analyzeLocalVideo(
          bFile,
          options,
          controller.signal,
          (event) => {
            bProgress = event.progress;
            if (mounted.current && runId === runIdRef.current) {
              setProgress((aProgress + bProgress) / 2);
            }
          },
        ),
      ]);
      if (
        !mounted.current ||
        controller.signal.aborted ||
        runId !== runIdRef.current
      ) {
        return;
      }
      let aUrl: string | undefined;
      let bUrl: string | undefined;
      try {
        aUrl = createObjectURL(aFile);
        bUrl = createObjectURL(bFile);
      } catch (caught) {
        if (aUrl) revokeObjectURL(aUrl);
        if (bUrl) revokeObjectURL(bUrl);
        throw caught;
      }
      ownedUrls.current.add(aUrl);
      ownedUrls.current.add(bUrl);
      replaceSlot("a", { report: aReport, mediaUrl: aUrl });
      replaceSlot("b", { report: bReport, mediaUrl: bUrl });
      setPlayback(initialPlayback);
      setAPlaying(false);
      setBPlaying(false);
      setProgress(1);
    } catch (caught) {
      const wasCancelled = controller.signal.aborted;
      controller.abort();
      if (
        mounted.current &&
        runId === runIdRef.current &&
        !wasCancelled &&
        !(caught instanceof DOMException && caught.name === "AbortError")
      ) {
        setError(t.compare.inputError);
      }
    } finally {
      if (
        mounted.current &&
        runId === runIdRef.current &&
        abortRef.current === controller
      ) {
        setBusy(false);
      }
    }
  }

  function selectFinding(side: Side, finding: Finding) {
    if (a && b) {
      const pairing =
        side === "a"
          ? findPairedFinding(a.report, b.report, finding, timelineMode)
          : findPairedFinding(b.report, a.report, finding, timelineMode);
      setFindingSelection(
        side === "a"
          ? {
              detectorId: finding.detector_id,
              aFindingId: pairing.sourceFindingId,
              bFindingId: pairing.peerFindingId,
              anchorSide: "a",
            }
          : {
              detectorId: finding.detector_id,
              aFindingId: pairing.peerFindingId,
              bFindingId: pairing.sourceFindingId,
              anchorSide: "b",
            },
      );
    } else {
      setFindingSelection({
        detectorId: finding.detector_id,
        ...(side === "a"
          ? { aFindingId: finding.id }
          : { bFindingId: finding.id }),
        anchorSide: side,
      });
    }
    seek(side, finding.time_range.start_seconds);
  }

  function selectDetector(detectorId: string) {
    setFindingSelection(
      a && b
        ? reconcileFindingSelection(
            a.report,
            b.report,
            { detectorId, anchorSide: "a" },
            timelineMode,
          )
        : { detectorId },
    );
  }

  function cancelAnalysis() {
    runIdRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = undefined;
    setBusy(false);
    setProgress(0);
    setAPlaying(false);
    setBPlaying(false);
  }

  function updateFile(side: Side, file: File | undefined) {
    if (busy) return;
    reportOperationIds.current[side] += 1;
    if (side === "a") setFileA(file);
    else setFileB(file);
  }

  function handleTimeUpdate(side: Side, seconds: number) {
    if (!synchronized) {
      seek(side, seconds);
      return;
    }
    if (timelineMode === "normalized") {
      if (side === "a") seek(side, seconds);
      return;
    }
    const masterSide = bDuration > aDuration ? "b" : "a";
    if (side === masterSide) seek(side, seconds);
  }

  const sharedMax =
    timelineMode === "normalized"
      ? 1
      : Math.max(0, aDuration, bDuration);
  const sharedValue =
    timelineMode === "normalized"
      ? aDuration > 0
        ? playback.aSeconds / aDuration
        : bDuration > 0
          ? playback.bSeconds / bDuration
          : 0
      : Math.max(playback.aSeconds, playback.bSeconds);

  return (
    <article className="compare-page">
      <header className="compare-page__heading">
        <div>
          <p className="eyebrow">{t.compare.eyebrow}</p>
          <h1>{t.compare.title}</h1>
          <p>{t.compare.description}</p>
        </div>
        <p className="compare-page__privacy">
          <span aria-hidden="true">◎</span> {t.compare.localOnly}
        </p>
      </header>

      <div className="compare-page__inputs-note">
        <p>{t.compare.inputHint}</p>
        <button
          className="button button--primary"
          disabled={busy}
          onClick={() => void analyzeBoth()}
          type="button"
        >
          {busy ? t.compare.analyzing : t.compare.analyze}
        </button>
        {busy ? (
          <button
            className="button button--quiet"
            onClick={cancelAnalysis}
            type="button"
          >
            {t.compare.cancel}
          </button>
        ) : null}
      </div>
      {busy ? (
        <progress
          aria-label={t.compare.analyzing}
          max={1}
          value={progress}
        />
      ) : null}
      {error ? (
        <p className="compare-page__error" role="alert">
          {error}
        </p>
      ) : null}

      <section
        aria-label={t.compare.controls}
        className="comparison-controls"
      >
        <label>
          <input
            checked={synchronized}
            disabled={busy}
            onChange={(event) => toggleSynchronization(event.currentTarget.checked)}
            type="checkbox"
          />
          {t.compare.synchronize}
        </label>
        <label>
          <input
            checked={timelineMode === "normalized"}
            disabled={busy}
            onChange={(event) =>
              changeTimelineMode(
                event.currentTarget.checked ? "normalized" : "absolute",
              )
            }
            type="checkbox"
          />
          {t.compare.normalized}
        </label>
        <button
          className="button button--quiet"
          disabled={busy || !a || !b}
          onClick={handleSwap}
          type="button"
        >
          {t.compare.swap}
        </button>
        <button
          className="button button--quiet"
          disabled={busy || !a?.mediaUrl || !b?.mediaUrl}
          onClick={toggleSharedPlayback}
          type="button"
        >
          {aPlaying || bPlaying ? t.compare.pauseBoth : t.compare.playBoth}
        </button>
        <label className="comparison-controls__seek">
          <span>{t.compare.sharedSeek}</span>
          <input
            aria-label={t.compare.sharedSeek}
            disabled={busy || !synchronized || !a || !b}
            max={sharedMax}
            min={0}
            onChange={(event) => {
              const position = Number(event.currentTarget.value);
              setPlayback((current) =>
                seekComparisonAtSharedPosition(
                  current,
                  position,
                  {
                    aDuration,
                    bDuration,
                    synchronized,
                    timelineMode,
                  },
                ),
              );
            }}
            step={timelineMode === "normalized" ? 0.01 : 0.1}
            type="range"
            value={Math.min(sharedValue, sharedMax)}
          />
          <output className="numeric">
            {timelineMode === "normalized"
              ? `${Math.round(sharedValue * 100)}%`
              : formatTimestamp(sharedValue)}
          </output>
        </label>
      </section>

      <div className="comparison-players">
        <ComparisonSide
          currentTime={playback.aSeconds}
          timelineDuration={
            timelineMode === "absolute"
              ? Math.max(aDuration, bDuration)
              : aDuration
          }
          mediaUrl={a?.mediaUrl}
          disabled={busy}
          onFile={(file) => updateFile("a", file)}
          onPlayingChange={(next) => setSidePlaying("a", next)}
          onReportFile={(file) => void loadReport("a", file)}
          onSeek={(seconds) => seek("a", seconds)}
          onSelectFinding={(finding) => selectFinding("a", finding)}
          onTimeUpdate={(seconds) => handleTimeUpdate("a", seconds)}
          playbackRate={playbackRates.a}
          playing={aPlaying}
          report={a?.report}
          selectedFindingId={findingSelection.aFindingId}
          side="a"
        />
        <ComparisonSide
          currentTime={playback.bSeconds}
          timelineDuration={
            timelineMode === "absolute"
              ? Math.max(aDuration, bDuration)
              : bDuration
          }
          mediaUrl={b?.mediaUrl}
          disabled={busy}
          onFile={(file) => updateFile("b", file)}
          onPlayingChange={(next) => setSidePlaying("b", next)}
          onReportFile={(file) => void loadReport("b", file)}
          onSeek={(seconds) => seek("b", seconds)}
          onSelectFinding={(finding) => selectFinding("b", finding)}
          onTimeUpdate={(seconds) => handleTimeUpdate("b", seconds)}
          playbackRate={playbackRates.b}
          playing={bPlaying}
          report={b?.report}
          selectedFindingId={findingSelection.bFindingId}
          side="b"
        />
      </div>

      {comparison ? (
        <>
          <DetectorDifferenceTable
            differences={comparison.detectors}
            onSelect={selectDetector}
            selectedDetectorId={findingSelection.detectorId}
          />
          <EvidencePair
            a={a?.report}
            b={b?.report}
            detectorId={findingSelection.detectorId}
            findingIdA={findingSelection.aFindingId}
            findingIdB={findingSelection.bFindingId}
            onSeekA={(seconds) => seek("a", seconds)}
            onSeekB={(seconds) => seek("b", seconds)}
          />
        </>
      ) : null}
    </article>
  );
}
