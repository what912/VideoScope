import { describe, expect, it } from "vitest";

import { createDemoReport } from "../../data/demo-report";
import type { BrowserReport } from "../../types/report";
import {
  comparisonPlaybackRates,
  compareReports,
  findPairedFinding,
  isSynchronizedTailPause,
  reconcileFindingSelection,
  seekComparison,
  seekComparisonAtSharedPosition,
  swapComparison,
  updateSynchronizedPlaying,
  type ComparisonPlayback,
} from "./comparison";

function reportWithDetector(
  side: string,
  ranges: Array<readonly [number, number]>,
  options: { duration?: number; status?: "ok" | "failed" | "skipped" } = {},
): BrowserReport {
  const report = createDemoReport("en");
  const detectorId = "near_black";
  const status = options.status ?? "ok";
  return {
    ...report,
    id: `${side}-report`,
    title: `Video ${side.toUpperCase()}`,
    metadata: {
      ...report.metadata,
      duration_seconds: options.duration ?? 10,
    },
    configuration: report.configuration.filter(
      (configuration) => configuration.detector_id === detectorId,
    ),
    detector_executions: [
      {
        detector_id: detectorId,
        detector_version: "browser-1",
        signal_kind: "browser_cpu",
        status,
        elapsed_seconds: 0.1,
        findings_count: status === "ok" ? ranges.length : 0,
        ...(status === "failed"
          ? { error_type: "DetectorError", error_message: "Unavailable" }
          : {}),
      },
    ],
    findings:
      status === "ok"
        ? ranges.map(([start, end], index) => ({
            ...report.findings[0]!,
            id: `${side}-${index}`,
            detector_id: detectorId,
            detector_version: "browser-1",
            signal_kind: "browser_cpu" as const,
            time_range: {
              start_seconds: start,
              end_seconds: end,
            },
          }))
        : [],
    metrics: [],
  };
}

describe("compareReports", () => {
  it("treats an absent detector as unknown rather than zero events", () => {
    const a = reportWithDetector("a", [[1, 2]]);
    const b = {
      ...reportWithDetector("b", []),
      configuration: [],
      detector_executions: [],
      findings: [],
    } satisfies BrowserReport;

    expect(compareReports(a, b).detectors).toEqual([
      {
        detectorId: "near_black",
        aEventCount: 1,
        bEventCount: null,
        aDurationSeconds: 1,
        bDurationSeconds: null,
        observation: "unknown",
      },
    ]);
  });

  it("uses union duration and duration as the tie-breaker for equal event counts", () => {
    const a = reportWithDetector("a", [
      [1, 3],
      [2, 4],
    ]);
    const b = reportWithDetector("b", [
      [1, 2],
      [5, 6],
    ]);

    expect(compareReports(a, b).detectors[0]).toEqual({
      detectorId: "near_black",
      aEventCount: 2,
      bEventCount: 2,
      aDurationSeconds: 3,
      bDurationSeconds: 2,
      observation: "b_fewer",
    });
  });

  it("reports equal when event counts and total durations match", () => {
    const result = compareReports(
      reportWithDetector("a", [[1, 2]]),
      reportWithDetector("b", [[7, 8]]),
    );

    expect(result.detectors[0]?.observation).toBe("equal");
  });

  it.each(["failed", "skipped"] as const)(
    "treats a %s detector execution as unknown",
    (status) => {
      const result = compareReports(
        reportWithDetector("a", [[1, 2]], { status }),
        reportWithDetector("b", []),
      );

      expect(result.detectors[0]).toMatchObject({
        aEventCount: null,
        aDurationSeconds: null,
        bEventCount: 0,
        bDurationSeconds: 0,
        observation: "unknown",
      });
    },
  );
});

describe("comparison synchronization", () => {
  const initial: ComparisonPlayback = {
    aSeconds: 2,
    bSeconds: 8,
    playing: false,
  };

  it("keeps the peer independent while synchronization is off", () => {
    expect(
      seekComparison(initial, "a", 7, {
        aDuration: 10,
        bDuration: 20,
        synchronized: false,
        timelineMode: "absolute",
      }),
    ).toEqual({ ...initial, aSeconds: 7 });
  });

  it("shares absolute seconds and clamps each video when synchronization is on", () => {
    expect(
      seekComparison(initial, "a", 15, {
        aDuration: 20,
        bDuration: 10,
        synchronized: true,
        timelineMode: "absolute",
      }),
    ).toEqual({ ...initial, aSeconds: 15, bSeconds: 10 });
  });

  it("keeps the longer peer tail reachable from a shorter absolute timeline", () => {
    expect(
      seekComparison(initial, "a", 15, {
        aDuration: 10,
        bDuration: 20,
        synchronized: true,
        timelineMode: "absolute",
      }),
    ).toEqual({ ...initial, aSeconds: 10, bSeconds: 15 });
  });

  it("maps shared seeks by progress ratio in normalized timeline mode", () => {
    expect(
      seekComparison(initial, "a", 5, {
        aDuration: 10,
        bDuration: 20,
        synchronized: true,
        timelineMode: "normalized",
      }),
    ).toEqual({ ...initial, aSeconds: 5, bSeconds: 10 });
  });

  it("uses the longer absolute duration as a shared scale without losing its tail", () => {
    expect(
      seekComparisonAtSharedPosition(initial, 15, {
        aDuration: 10,
        bDuration: 20,
        synchronized: true,
        timelineMode: "absolute",
      }),
    ).toEqual({ ...initial, aSeconds: 10, bSeconds: 15 });
  });

  it("uses zero-to-one progress for normalized shared seeking", () => {
    expect(
      seekComparisonAtSharedPosition(initial, 0.75, {
        aDuration: 10,
        bDuration: 20,
        synchronized: true,
        timelineMode: "normalized",
      }),
    ).toEqual({ ...initial, aSeconds: 7.5, bSeconds: 15 });
  });

  it("adjusts synchronized normalized playback rates to finish together", () => {
    expect(comparisonPlaybackRates(10, 20, true, "normalized")).toEqual({
      a: 0.5,
      b: 1,
    });
    expect(comparisonPlaybackRates(10, 20, true, "absolute")).toEqual({
      a: 1,
      b: 1,
    });
  });

  it("does not let the shorter absolute video pause the longer tail", () => {
    const options = {
      aDuration: 10,
      bDuration: 20,
      synchronized: true,
      timelineMode: "absolute" as const,
    };
    expect(
      isSynchronizedTailPause(
        "a",
        { aSeconds: 10, bSeconds: 12, playing: true },
        options,
      ),
    ).toBe(true);
    expect(
      isSynchronizedTailPause(
        "b",
        { aSeconds: 10, bSeconds: 20, playing: true },
        options,
      ),
    ).toBe(false);
  });

  it("swaps A and B reports, media, and playback positions", () => {
    const a = reportWithDetector("a", [[1, 2]]);
    const b = reportWithDetector("b", [[3, 4]]);

    expect(
      swapComparison(
        { report: a, mediaUrl: "blob:a" },
        { report: b, mediaUrl: "blob:b" },
        initial,
      ),
    ).toEqual({
      a: { report: b, mediaUrl: "blob:b" },
      b: { report: a, mediaUrl: "blob:a" },
      playback: { aSeconds: 8, bSeconds: 2, playing: false },
    });
  });
});

describe("Finding pairing", () => {
  it("keeps the exact selected Finding and pairs the nearest interval on the peer", () => {
    const a = reportWithDetector("a", [
      [1, 2],
      [7, 8],
    ]);
    const b = reportWithDetector("b", [
      [2, 3],
      [14, 15],
    ], { duration: 20 });
    const selected = a.findings[1]!;

    expect(findPairedFinding(a, b, selected, "normalized")).toMatchObject({
      sourceFindingId: selected.id,
      peerFindingId: b.findings[1]!.id,
    });
  });

  it("reconciles the paired Finding when timeline mode or reports change", () => {
    const a = reportWithDetector("a", [
      [1, 2],
      [7, 8],
    ]);
    const b = reportWithDetector("b", [
      [2, 3],
      [14, 15],
    ], { duration: 20 });
    const current = {
      detectorId: "near_black",
      aFindingId: a.findings[1]!.id,
      bFindingId: b.findings[0]!.id,
      anchorSide: "a" as const,
    };

    expect(
      reconcileFindingSelection(a, b, current, "normalized"),
    ).toMatchObject({
      aFindingId: a.findings[1]!.id,
      bFindingId: b.findings[1]!.id,
    });

    const replacementA = reportWithDetector("replacement", [[4, 5]]);
    expect(
      reconcileFindingSelection(replacementA, b, current, "absolute"),
    ).toMatchObject({
      aFindingId: replacementA.findings[0]!.id,
      detectorId: "near_black",
    });
  });
});

describe("synchronized play state", () => {
  it("marks only the shorter side ended while the longer absolute tail plays", () => {
    expect(
      updateSynchronizedPlaying(
        "a",
        false,
        { a: true, b: true },
        { aSeconds: 10, bSeconds: 12, playing: true },
        {
          aDuration: 10,
          bDuration: 20,
          synchronized: true,
          timelineMode: "absolute",
        },
      ),
    ).toEqual({
      a: false,
      b: true,
      playing: true,
    });
  });
});
