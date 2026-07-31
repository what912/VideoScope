import type { Finding } from "../../types/analysis";
import type { BrowserReport } from "../../types/report";

export type DetectorId = string;
export type ComparisonObservation =
  | "a_fewer"
  | "b_fewer"
  | "equal"
  | "unknown";
export type ComparisonTimelineMode = "absolute" | "normalized";
export type ComparisonSide = "a" | "b";

export interface DetectorDifference {
  detectorId: DetectorId;
  aEventCount: number | null;
  bEventCount: number | null;
  aDurationSeconds: number | null;
  bDurationSeconds: number | null;
  observation: ComparisonObservation;
  optionalDemo?: true;
}

export interface ReportComparison {
  detectors: DetectorDifference[];
}

export interface ComparisonSlot {
  report: BrowserReport;
  mediaUrl?: string;
}

export interface ComparisonPlayback {
  aSeconds: number;
  bSeconds: number;
  playing: boolean;
}

export interface ComparisonSeekOptions {
  aDuration: number;
  bDuration: number;
  synchronized: boolean;
  timelineMode: ComparisonTimelineMode;
}

export interface PairedFindingSelection {
  sourceFindingId: string;
  peerFindingId?: string;
}

export interface ComparisonFindingSelection {
  detectorId?: string;
  aFindingId?: string;
  bFindingId?: string;
  anchorSide?: ComparisonSide;
}

export interface ComparisonPlayingState {
  a: boolean;
  b: boolean;
  playing: boolean;
}

function clamp(seconds: number, duration: number) {
  if (!Number.isFinite(seconds) || !Number.isFinite(duration) || duration <= 0) {
    return 0;
  }
  return Math.min(duration, Math.max(0, seconds));
}

function detectorIds(report: BrowserReport) {
  return new Set([
    ...report.configuration.map((item) => item.detector_id),
    ...report.detector_executions.map((item) => item.detector_id),
    ...report.findings.map((item) => item.detector_id),
  ]);
}

function mergedDuration(
  ranges: Array<{ start_seconds: number; end_seconds: number }>,
) {
  const sorted = ranges
    .filter(
      (range) =>
        Number.isFinite(range.start_seconds) &&
        Number.isFinite(range.end_seconds) &&
        range.end_seconds >= range.start_seconds,
    )
    .sort(
      (left, right) =>
        left.start_seconds - right.start_seconds ||
        left.end_seconds - right.end_seconds,
    );
  let total = 0;
  let currentStart: number | undefined;
  let currentEnd: number | undefined;
  for (const range of sorted) {
    if (currentStart === undefined || currentEnd === undefined) {
      currentStart = range.start_seconds;
      currentEnd = range.end_seconds;
      continue;
    }
    if (range.start_seconds <= currentEnd) {
      currentEnd = Math.max(currentEnd, range.end_seconds);
      continue;
    }
    total += currentEnd - currentStart;
    currentStart = range.start_seconds;
    currentEnd = range.end_seconds;
  }
  return currentStart === undefined || currentEnd === undefined
    ? 0
    : total + currentEnd - currentStart;
}

function detectorMeasurement(report: BrowserReport, detectorId: string) {
  const execution = report.detector_executions.find(
    (item) => item.detector_id === detectorId,
  );
  if (!execution || execution.status !== "ok") {
    return null;
  }
  const findings = report.findings.filter(
    (finding) => finding.detector_id === detectorId,
  );
  return {
    eventCount: findings.length,
    durationSeconds: mergedDuration(
      findings.map((finding) => finding.time_range),
    ),
  };
}

function isOptionalDemoDetector(
  report: BrowserReport,
  detectorId: string,
) {
  return (
    report.configuration.some(
      (item) =>
        item.detector_id === detectorId &&
        item.signal_kind === "optional_demo",
    ) ||
    report.detector_executions.some(
      (item) =>
        item.detector_id === detectorId &&
        item.signal_kind === "optional_demo",
    ) ||
    report.findings.some(
      (item) =>
        item.detector_id === detectorId &&
        item.signal_kind === "optional_demo",
    ) ||
    report.metrics.some(
      (item) =>
        item.detector_id === detectorId && item.kind === "optional_demo",
    )
  );
}

function observation(
  a: ReturnType<typeof detectorMeasurement>,
  b: ReturnType<typeof detectorMeasurement>,
): ComparisonObservation {
  if (!a || !b) return "unknown";
  if (a.eventCount < b.eventCount) return "a_fewer";
  if (b.eventCount < a.eventCount) return "b_fewer";
  if (a.durationSeconds < b.durationSeconds - Number.EPSILON) return "a_fewer";
  if (b.durationSeconds < a.durationSeconds - Number.EPSILON) return "b_fewer";
  return "equal";
}

export function compareReports(
  aReport: BrowserReport,
  bReport: BrowserReport,
): ReportComparison {
  const ids = new Set([...detectorIds(aReport), ...detectorIds(bReport)]);
  return {
    detectors: [...ids]
      .sort((left, right) => left.localeCompare(right))
      .map((detectorId) => {
        const a = detectorMeasurement(aReport, detectorId);
        const b = detectorMeasurement(bReport, detectorId);
        return {
          detectorId,
          aEventCount: a?.eventCount ?? null,
          bEventCount: b?.eventCount ?? null,
          aDurationSeconds: a?.durationSeconds ?? null,
          bDurationSeconds: b?.durationSeconds ?? null,
          observation: observation(a, b),
          ...(isOptionalDemoDetector(aReport, detectorId) ||
          isOptionalDemoDetector(bReport, detectorId)
            ? { optionalDemo: true as const }
            : {}),
        };
      }),
  };
}

export function seekComparison(
  playback: ComparisonPlayback,
  source: ComparisonSide,
  requestedSeconds: number,
  options: ComparisonSeekOptions,
): ComparisonPlayback {
  const sourceDuration =
    source === "a" ? options.aDuration : options.bDuration;
  const peerDuration =
    source === "a" ? options.bDuration : options.aDuration;
  const sourceSeconds = clamp(requestedSeconds, sourceDuration);
  const peerSeconds =
    options.timelineMode === "normalized"
      ? clamp(
          sourceDuration > 0
            ? (sourceSeconds / sourceDuration) * peerDuration
            : 0,
          peerDuration,
        )
      : clamp(sourceSeconds, peerDuration);
  const synchronizedPeerSeconds =
    options.timelineMode === "absolute"
      ? clamp(requestedSeconds, peerDuration)
      : peerSeconds;

  if (source === "a") {
    return {
      ...playback,
      aSeconds: sourceSeconds,
      ...(options.synchronized
        ? { bSeconds: synchronizedPeerSeconds }
        : {}),
    };
  }
  return {
    ...playback,
    bSeconds: sourceSeconds,
    ...(options.synchronized
      ? { aSeconds: synchronizedPeerSeconds }
      : {}),
  };
}

export function seekComparisonAtSharedPosition(
  playback: ComparisonPlayback,
  position: number,
  options: ComparisonSeekOptions,
): ComparisonPlayback {
  if (options.timelineMode === "normalized") {
    const progress = clamp(position, 1);
    return {
      ...playback,
      aSeconds: clamp(progress * options.aDuration, options.aDuration),
      bSeconds: clamp(progress * options.bDuration, options.bDuration),
    };
  }
  return {
    ...playback,
    aSeconds: clamp(position, options.aDuration),
    bSeconds: clamp(position, options.bDuration),
  };
}

export function comparisonPlaybackRates(
  aDuration: number,
  bDuration: number,
  synchronized: boolean,
  timelineMode: ComparisonTimelineMode,
) {
  if (!synchronized || timelineMode === "absolute") {
    return { a: 1, b: 1 };
  }
  const commonDuration = Math.max(aDuration, bDuration);
  if (commonDuration <= 0) return { a: 1, b: 1 };
  return {
    a: Math.max(0.0625, aDuration / commonDuration),
    b: Math.max(0.0625, bDuration / commonDuration),
  };
}

export function isSynchronizedTailPause(
  side: ComparisonSide,
  playback: ComparisonPlayback,
  options: ComparisonSeekOptions,
) {
  if (
    !options.synchronized ||
    options.timelineMode !== "absolute"
  ) {
    return false;
  }
  const sideDuration =
    side === "a" ? options.aDuration : options.bDuration;
  const peerDuration =
    side === "a" ? options.bDuration : options.aDuration;
  const sideSeconds =
    side === "a" ? playback.aSeconds : playback.bSeconds;
  const peerSeconds =
    side === "a" ? playback.bSeconds : playback.aSeconds;
  return (
    sideDuration < peerDuration &&
    sideSeconds >= sideDuration - 0.15 &&
    peerSeconds < peerDuration - 0.15
  );
}

export function updateSynchronizedPlaying(
  side: ComparisonSide,
  next: boolean,
  current: Pick<ComparisonPlayingState, "a" | "b">,
  playback: ComparisonPlayback,
  options: ComparisonSeekOptions,
): ComparisonPlayingState {
  if (!options.synchronized) {
    const state = { ...current, [side]: next };
    return { ...state, playing: state.a || state.b };
  }
  if (!next && isSynchronizedTailPause(side, playback, options)) {
    const state = { ...current, [side]: false };
    return { ...state, playing: state.a || state.b };
  }
  return { a: next, b: next, playing: next };
}

function findingPosition(
  report: BrowserReport,
  finding: Finding,
  timelineMode: ComparisonTimelineMode,
) {
  const midpoint =
    (finding.time_range.start_seconds + finding.time_range.end_seconds) / 2;
  return timelineMode === "normalized" &&
    report.metadata.duration_seconds > 0
    ? midpoint / report.metadata.duration_seconds
    : midpoint;
}

export function findPairedFinding(
  sourceReport: BrowserReport,
  peerReport: BrowserReport,
  sourceFinding: Finding,
  timelineMode: ComparisonTimelineMode,
): PairedFindingSelection {
  const sourcePosition = findingPosition(
    sourceReport,
    sourceFinding,
    timelineMode,
  );
  const peerFinding = peerReport.findings
    .filter(
      (finding) => finding.detector_id === sourceFinding.detector_id,
    )
    .sort(
      (left, right) =>
        Math.abs(
          findingPosition(peerReport, left, timelineMode) - sourcePosition,
        ) -
          Math.abs(
            findingPosition(peerReport, right, timelineMode) - sourcePosition,
          ) ||
        left.time_range.start_seconds - right.time_range.start_seconds ||
        left.id.localeCompare(right.id),
    )[0];
  return {
    sourceFindingId: sourceFinding.id,
    ...(peerFinding ? { peerFindingId: peerFinding.id } : {}),
  };
}

export function reconcileFindingSelection(
  aReport: BrowserReport,
  bReport: BrowserReport,
  current: ComparisonFindingSelection,
  timelineMode: ComparisonTimelineMode,
): ComparisonFindingSelection {
  const differences = compareReports(aReport, bReport).detectors;
  const detectorId = differences.some(
    (difference) => difference.detectorId === current.detectorId,
  )
    ? current.detectorId
    : differences[0]?.detectorId;
  if (!detectorId) return {};

  const findings = {
    a: aReport.findings.filter(
      (finding) => finding.detector_id === detectorId,
    ),
    b: bReport.findings.filter(
      (finding) => finding.detector_id === detectorId,
    ),
  };
  let anchorSide = current.anchorSide ?? "a";
  let anchorFinding = findings[anchorSide].find(
    (finding) =>
      finding.id ===
      (anchorSide === "a" ? current.aFindingId : current.bFindingId),
  );
  anchorFinding ??= findings[anchorSide][0];
  if (!anchorFinding) {
    anchorSide = anchorSide === "a" ? "b" : "a";
    anchorFinding = findings[anchorSide].find(
      (finding) =>
        finding.id ===
        (anchorSide === "a" ? current.aFindingId : current.bFindingId),
    );
    anchorFinding ??= findings[anchorSide][0];
  }
  if (!anchorFinding) {
    return { detectorId, anchorSide: current.anchorSide };
  }

  const sourceReport = anchorSide === "a" ? aReport : bReport;
  const peerReport = anchorSide === "a" ? bReport : aReport;
  const paired = findPairedFinding(
    sourceReport,
    peerReport,
    anchorFinding,
    timelineMode,
  );
  return anchorSide === "a"
    ? {
        detectorId,
        aFindingId: paired.sourceFindingId,
        bFindingId: paired.peerFindingId,
        anchorSide,
      }
    : {
        detectorId,
        aFindingId: paired.peerFindingId,
        bFindingId: paired.sourceFindingId,
        anchorSide,
      };
}

export function swapComparison(
  a: ComparisonSlot,
  b: ComparisonSlot,
  playback: ComparisonPlayback,
) {
  return {
    a: b,
    b: a,
    playback: {
      ...playback,
      aSeconds: playback.bSeconds,
      bSeconds: playback.aSeconds,
    },
  };
}
