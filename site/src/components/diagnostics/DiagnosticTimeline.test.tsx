import { readFileSync } from "node:fs";
import path from "node:path";

import { fireEvent, render, screen } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import { DiagnosticTimeline } from "./DiagnosticTimeline";

const diagnosticsCss = readFileSync(
  path.resolve(process.cwd(), "src/components/diagnostics/diagnostics.css"),
  "utf8",
);

const finding: Finding = {
  id: "finding-1",
  detector_id: "global_flicker",
  detector_version: "1",
  signal_kind: "browser_cpu",
  title: "Potential global luminance flicker",
  description: "Rapid global luminance changes were observed.",
  severity: "high",
  score: 0.8,
  confidence: 0.7,
  time_range: { start_seconds: 2, end_seconds: 4 },
  evidence: [
    {
      evidence_type: "frame",
      timestamp_seconds: 3,
      description: "Peak luminance residual",
      thumbnail: { src: "frame.webp", width: 160, height: 90 },
      metadata: {},
    },
  ],
  tags: [],
  parameters: { threshold: 0.2 },
  limitations: ["Intentional lighting can resemble this signal."],
};

function renderTimeline(
  props: Partial<React.ComponentProps<typeof DiagnosticTimeline>> = {},
) {
  const onSeek = vi.fn();
  const onSelectFinding = vi.fn();
  const view = render(
    <I18nProvider initialLocale="en">
      <DiagnosticTimeline
        currentTime={3}
        duration={10}
        findings={[finding]}
        onSeek={onSeek}
        onSelectFinding={onSelectFinding}
        selectedFindingId={finding.id}
        {...props}
      />
    </I18nProvider>,
  );
  return { ...view, onSeek, onSelectFinding };
}

describe("DiagnosticTimeline", () => {
  let styles: HTMLStyleElement;

  beforeAll(() => {
    styles = document.createElement("style");
    styles.textContent = diagnosticsCss;
    document.head.append(styles);
  });

  afterAll(() => styles.remove());

  it("keeps endpoint ruler labels inside the track while centering interior labels", () => {
    const { container } = renderTimeline({ duration: 6 });
    const rulerLabels = container.querySelectorAll(
      ".diagnostic-timeline__ruler span",
    );

    expect(rulerLabels).toHaveLength(5);
    expect(getComputedStyle(rulerLabels[0]).transform).toBe("none");
    expect(getComputedStyle(rulerLabels[2]).transform).toBe(
      "translateX(-50%)",
    );
    expect(getComputedStyle(rulerLabels[4]).transform).toBe(
      "translateX(-100%)",
    );
  });

  it.each([
    [0, "0%"],
    [5, "50%"],
    [10, "100%"],
  ])(
    "keeps the %ss playhead inside the same labelled track as markers and slider",
    (currentTime, expectedLeft) => {
      renderTimeline({ currentTime });
      const track = screen.getByTestId("timeline-coordinate-track");
      const playhead = screen.getByTestId("timeline-playhead");
      const slider = screen.getByRole("slider", { name: /playhead/i });
      const seekTrack = screen.getByTestId("timeline-seek-track");
      const markerTrack = screen.getByTestId("timeline-marker-track");

      expect(track).toContainElement(playhead);
      expect(track).not.toContainElement(slider);
      expect(seekTrack).toContainElement(slider);
      expect(markerTrack).toHaveClass("diagnostic-timeline__track");
      expect(playhead).toHaveStyle({ left: expectedLeft });
      expect(track.closest(".diagnostic-timeline")).toHaveStyle({
        "--timeline-label-width": "10rem",
        "--timeline-mobile-label-width": "5.5rem",
      });
    },
  );

  it("seeks with arrows and timeline endpoints from the accessible slider", () => {
    const { onSeek } = renderTimeline();
    const slider = screen.getByRole("slider", { name: /playhead/i });

    fireEvent.keyDown(slider, { key: "ArrowRight" });
    fireEvent.keyDown(slider, { key: "ArrowLeft" });
    fireEvent.keyDown(slider, { key: "Home" });
    fireEvent.keyDown(slider, { key: "End" });

    expect(onSeek.mock.calls.map(([value]) => value)).toEqual([4, 2, 0, 10]);
  });

  it("selects a finding by keyboard and exposes severity without relying on color", () => {
    const { onSelectFinding } = renderTimeline();
    const marker = screen.getByRole("button", {
      name: /high.*potential global luminance flicker/i,
    });

    fireEvent.keyDown(marker, { key: "Enter" });
    fireEvent.keyDown(marker, { key: " " });

    expect(onSelectFinding).toHaveBeenCalledTimes(2);
    expect(screen.getByText("High")).toBeVisible();
  });

  it("reveals evidence on focus and keeps each detector in a labelled row", () => {
    renderTimeline();
    const marker = screen.getByRole("button", {
      name: /potential global luminance flicker/i,
    });

    fireEvent.focus(marker);

    expect(
      screen.getByRole("img", { name: "Peak luminance residual" }),
    ).toBeVisible();
    expect(screen.getByText("global_flicker")).toBeVisible();
  });

  it("keeps marker pointer targets above rows while the slider owns a separate seek surface", () => {
    const { onSeek, onSelectFinding } = renderTimeline();
    const marker = screen.getByRole("button", {
      name: /potential global luminance flicker/i,
    });
    const slider = screen.getByRole("slider", { name: /playhead/i });
    const markerRow = marker.closest(".diagnostic-timeline__row");
    const seekTrack = slider.closest(".diagnostic-timeline__seek-track");

    expect(markerRow).not.toBeNull();
    expect(seekTrack).not.toBeNull();
    expect(markerRow).not.toContainElement(slider);
    expect(seekTrack).not.toContainElement(marker);
    expect(getComputedStyle(marker).pointerEvents).not.toBe("none");
    expect(getComputedStyle(slider).pointerEvents).not.toBe("none");

    marker.click();
    fireEvent.change(slider, { target: { value: "7.5" } });
    expect(onSelectFinding).toHaveBeenCalledWith(finding);
    expect(onSeek).toHaveBeenCalledWith(7.5);
  });

  it("keeps an empty timeline keyboard- and pointer-seekable with a nonzero seek rail", () => {
    const { onSeek } = renderTimeline({ findings: [] });
    const slider = screen.getByRole("slider", { name: /playhead/i });
    const seekTrack = screen.getByTestId("timeline-seek-track");

    expect(seekTrack).toContainElement(slider);
    expect(seekTrack).toHaveStyle({ minHeight: "2.75rem" });
    fireEvent.change(slider, { target: { value: "6.5" } });
    fireEvent.keyDown(slider, { key: "End" });
    expect(onSeek.mock.calls.map(([value]) => value)).toEqual([6.5, 10]);
  });

  it.each([
    [{ start_seconds: 0, end_seconds: 0 }, "start"],
    [{ start_seconds: 9.999, end_seconds: 10 }, "end"],
    [{ start_seconds: 10, end_seconds: 10 }, "end"],
  ])(
    "keeps short and endpoint markers inside the track with edge-aware previews",
    (timeRange, expectedAnchor) => {
      renderTimeline({
        findings: [{ ...finding, time_range: timeRange }],
      });
      const marker = screen.getByRole("button", {
        name: /potential global luminance flicker/i,
      });
      const wrap = marker.closest(".timeline-marker-wrap");

      expect(wrap).toHaveAttribute("data-preview-anchor", expectedAnchor);
      expect(wrap?.getAttribute("style")).toMatch(/max-width: \d+(\.\d+)?%/);
      expect(marker).toHaveClass("timeline-marker__hit-target");
      expect(screen.getByTestId("timeline-marker-visual")).toBeVisible();

      fireEvent.focus(marker);
      expect(screen.getByRole("tooltip")).toHaveAttribute(
        "data-preview-anchor",
        expectedAnchor,
      );
    },
  );
});
