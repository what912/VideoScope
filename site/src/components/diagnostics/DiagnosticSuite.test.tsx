import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../../i18n/I18nProvider";
import type { Finding, QualityMetric } from "../../types/analysis";
import { DiagnosticOverlay } from "./DiagnosticOverlay";
import { DiagnosticTimeline } from "./DiagnosticTimeline";
import { IssueDetailPanel } from "./IssueDetailPanel";
import { IssueList } from "./IssueList";
import { MetricChart } from "./MetricChart";
import { VideoPlayer } from "./VideoPlayer";

const findings: Finding[] = [
  {
    id: "near-black",
    detector_id: "near_black",
    detector_version: "1",
    signal_kind: "browser_cpu",
    title: "Near-black interval detected",
    description: "A sustained low-luminance interval was observed.",
    severity: "medium",
    score: 0.72,
    confidence: 0.78,
    time_range: { start_seconds: 1, end_seconds: 2 },
    evidence: [
      {
        evidence_type: "frame",
        timestamp_seconds: 1.5,
        description: "Dark interval midpoint",
        metadata: {},
      },
    ],
    tags: [],
    parameters: { mean_luma_threshold: 0.08 },
    limitations: ["This may be an intentional fade or night scene."],
  },
  {
    id: "freeze",
    detector_id: "possible_freeze",
    detector_version: "1",
    signal_kind: "browser_cpu",
    title: "Possible frozen or repeated frames",
    description: "A sustained near-repeat sequence was observed.",
    severity: "high",
    score: 0.86,
    confidence: 0.74,
    time_range: { start_seconds: 5, end_seconds: 7 },
    evidence: [
      {
        evidence_type: "frame",
        timestamp_seconds: 6,
        description: "Repeated-frame midpoint",
        thumbnail: { src: "freeze.webp", width: 160, height: 90 },
        metadata: {},
      },
    ],
    tags: [],
    parameters: { max_pixel_difference: 0.01 },
    limitations: ["A deliberately static shot can produce this signal."],
  },
];

const metric: QualityMetric = {
  id: "frame-change",
  label: "Frame-change continuity",
  value: 0.76,
  kind: "browser_cpu",
  detector_id: "possible_freeze",
  unit: "ratio",
  description: "Detector-local signal.",
};

function LinkedDiagnostics() {
  const [selectedId, setSelectedId] = useState(findings[0].id);
  const selected =
    findings.find((finding) => finding.id === selectedId) ?? findings[0];
  return (
    <>
      <DiagnosticOverlay finding={selected} />
      <DiagnosticTimeline
        currentTime={selected.time_range.start_seconds}
        duration={10}
        findings={findings}
        onSeek={() => undefined}
        onSelectFinding={(finding) => setSelectedId(finding.id)}
        selectedFindingId={selected.id}
      />
      <MetricChart
        currentTime={selected.time_range.start_seconds}
        duration={10}
        metric={metric}
        samples={[
          { time: 0, value: 0.2 },
          { time: 5, value: 0.8 },
          { time: 10, value: 0.4 },
        ]}
      />
      <IssueList
        findings={findings}
        onSelectFinding={(finding) => setSelectedId(finding.id)}
        selectedFindingId={selected.id}
      />
      <IssueDetailPanel finding={selected} onEvidenceSeek={() => undefined} />
    </>
  );
}

describe("diagnostic suite", () => {
  it("keeps issue, overlay, timeline, metric cursor, and detail controlled by one selection", () => {
    render(
      <I18nProvider initialLocale="en">
        <LinkedDiagnostics />
      </I18nProvider>,
    );

    const issueList = screen.getByRole("region", {
      name: "Review intervals",
    });
    fireEvent.click(
      within(issueList).getByRole("button", {
        name: /possible frozen or repeated frames/i,
      }),
    );

    expect(screen.getByTestId("diagnostic-overlay")).toHaveTextContent(
      "Possible frozen or repeated frames",
    );
    expect(screen.getByTestId("diagnostic-overlay-box")).toHaveStyle({
      left: "12%",
      width: "76%",
    });
    expect(screen.getByTestId("metric-cursor")).toHaveAttribute(
      "data-time",
      "5",
    );
    expect(screen.getByTestId("finding-detail")).toHaveTextContent(
      "max_pixel_difference",
    );
    expect(
      screen.getAllByRole("button", {
        name: /possible frozen or repeated frames/i,
      })[0],
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("emits evidence timestamps and renders neutral limitations", () => {
    const onEvidenceSeek = vi.fn();
    render(
      <I18nProvider initialLocale="en">
        <IssueDetailPanel
          finding={findings[1]}
          onEvidenceSeek={onEvidenceSeek}
        />
      </I18nProvider>,
    );

    fireEvent.click(
      screen.getByRole("button", { name: /repeated-frame midpoint/i }),
    );

    expect(onEvidenceSeek).toHaveBeenCalledWith(6);
    expect(
      screen.getByText(/deliberately static shot/i),
    ).toBeVisible();
    expect(screen.queryByText(/overall score/i)).not.toBeInTheDocument();
  });

  it("keeps VideoPlayer externally controlled and emits keyboard seek requests", () => {
    const onSeek = vi.fn();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(
      () => undefined,
    );
    render(
      <I18nProvider initialLocale="en">
        <VideoPlayer
          currentTime={4}
          duration={10}
          onPlayingChange={() => undefined}
          onSeek={onSeek}
          playbackRate={1}
          playing={false}
          selectedFinding={findings[0]}
          src="blob:video"
        />
      </I18nProvider>,
    );

    const player = screen.getByTestId("video-player");
    fireEvent.keyDown(player, { key: "ArrowRight" });
    fireEvent.keyDown(player, { key: "Home" });
    fireEvent.keyDown(player, { key: "End" });

    expect(onSeek.mock.calls.map(([value]) => value)).toEqual([5, 0, 10]);
    expect(player).toHaveAttribute("role", "region");
    expect(player).not.toHaveAttribute("role", "application");
  });

  it("uses the contained media rectangle for portrait overlays and updates on resize", () => {
    let observerCallback: ResizeObserverCallback | undefined;
    const observe = vi.fn();
    const disconnect = vi.fn();
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: ResizeObserverCallback) {
          observerCallback = callback;
        }
        observe = observe;
        disconnect = disconnect;
        unobserve = vi.fn();
      },
    );
    render(
      <I18nProvider initialLocale="en">
        <VideoPlayer
          currentTime={4}
          duration={10}
          onPlayingChange={() => undefined}
          onSeek={() => undefined}
          playbackRate={1}
          playing={false}
          selectedFinding={findings[0]}
          src="blob:portrait"
          videoHeight={1920}
          videoWidth={1080}
        />
      </I18nProvider>,
    );
    const player = screen.getByTestId("video-player");
    Object.defineProperties(player, {
      clientWidth: { configurable: true, value: 1600 },
      clientHeight: { configurable: true, value: 900 },
    });
    act(() => {
      observerCallback?.(
        [
          {
            target: player,
            contentRect: {
              width: 1600,
              height: 900,
            } as DOMRectReadOnly,
          } as unknown as ResizeObserverEntry,
        ],
        {} as ResizeObserver,
      );
    });

    expect(screen.getByTestId("diagnostic-media-layer")).toHaveStyle({
      left: "546.875px",
      top: "0px",
      width: "506.25px",
      height: "900px",
    });
    expect(observe).toHaveBeenCalledWith(player);
  });

  it("falls back to intrinsic dimensions when explicit dimensions are not finite positive values", () => {
    let observerCallback: ResizeObserverCallback | undefined;
    vi.stubGlobal(
      "ResizeObserver",
      class {
        constructor(callback: ResizeObserverCallback) {
          observerCallback = callback;
        }
        observe = vi.fn();
        disconnect = vi.fn();
        unobserve = vi.fn();
      },
    );
    render(
      <I18nProvider initialLocale="en">
        <VideoPlayer
          currentTime={0}
          duration={10}
          onPlayingChange={() => undefined}
          onSeek={() => undefined}
          playbackRate={1}
          playing={false}
          src="blob:intrinsic"
          videoHeight={0}
          videoWidth={Number.NaN}
        />
      </I18nProvider>,
    );
    const player = screen.getByTestId("video-player");
    const video = screen.getByLabelText("Selected video");
    Object.defineProperties(video, {
      videoWidth: { configurable: true, value: 1000 },
      videoHeight: { configurable: true, value: 1000 },
    });
    fireEvent.loadedMetadata(video);
    act(() => {
      observerCallback?.(
        [
          {
            target: player,
            contentRect: {
              width: 1600,
              height: 900,
            } as DOMRectReadOnly,
          } as unknown as ResizeObserverEntry,
        ],
        {} as ResizeObserver,
      );
    });

    expect(screen.getByTestId("diagnostic-media-layer")).toHaveStyle({
      left: "350px",
      top: "0px",
      width: "900px",
      height: "900px",
    });
  });

  it("uses a neutral fallback for non-finite evidence boxes", () => {
    const unsafeFinding: Finding = {
      ...findings[0],
      evidence: [
        {
          ...findings[0].evidence[0],
          metadata: {
            bounding_box: {
              x_min: Number.NaN,
              y_min: 0,
              x_max: Number.POSITIVE_INFINITY,
              y_max: 1,
            },
          },
        },
      ],
    };
    render(
      <I18nProvider initialLocale="en">
        <DiagnosticOverlay finding={unsafeFinding} />
      </I18nProvider>,
    );
    expect(screen.getByTestId("diagnostic-overlay-box")).toHaveStyle({
      left: "12%",
      top: "14%",
      width: "76%",
      height: "72%",
    });
  });

  it("omits playable media controls when no source exists", () => {
    const onPlayingChange = vi.fn();
    render(
      <I18nProvider initialLocale="en">
        <VideoPlayer
          currentTime={0}
          duration={0}
          onPlayingChange={onPlayingChange}
          onSeek={() => undefined}
          playbackRate={1}
          playing={false}
        />
      </I18nProvider>,
    );
    const player = screen.getByTestId("video-player");
    const play = screen.getByRole("button", { name: /^play$/i });

    expect(play).toBeDisabled();
    fireEvent.keyDown(player, { key: " " });
    expect(onPlayingChange).not.toHaveBeenCalled();
  });
});
