import { useMemo, type CSSProperties } from "react";

import { useI18n } from "../../i18n/I18nProvider";
import type { Finding, Severity } from "../../types/analysis";
import { clampTime, formatTimestamp } from "./diagnostic-geometry";
import { TimelineMarker } from "./TimelineMarker";
import "./diagnostics.css";

export interface DiagnosticTimelineProps {
  currentTime: number;
  duration: number;
  findings: Finding[];
  selectedFindingId?: string;
  seekStep?: number;
  onSeek(seconds: number): void;
  onSelectFinding(finding: Finding): void;
}

export function DiagnosticTimeline({
  currentTime,
  duration,
  findings,
  selectedFindingId,
  seekStep = 1,
  onSeek,
  onSelectFinding,
}: DiagnosticTimelineProps) {
  const { t } = useI18n();
  const safeTime = clampTime(currentTime, duration);
  const rows = useMemo(() => {
    const grouped = new Map<string, Finding[]>();
    findings.forEach((finding) => {
      const current = grouped.get(finding.detector_id) ?? [];
      current.push(finding);
      grouped.set(finding.detector_id, current);
    });
    return [...grouped.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [findings]);
  const severityLabels: Record<Severity, string> = t.diagnostics.severity;

  const handleKeys = (event: React.KeyboardEvent<HTMLInputElement>) => {
    let target: number | undefined;
    if (event.key === "ArrowLeft") target = safeTime - seekStep;
    if (event.key === "ArrowRight") target = safeTime + seekStep;
    if (event.key === "Home") target = 0;
    if (event.key === "End") target = duration;
    if (target !== undefined) {
      event.preventDefault();
      onSeek(clampTime(target, duration));
    }
  };

  return (
    <section
      aria-label={t.diagnostics.timeline}
      className="diagnostic-timeline"
      style={
        {
          "--timeline-label-width": "10rem",
          "--timeline-mobile-label-width": "5.5rem",
        } as CSSProperties
      }
    >
      <div className="diagnostic-timeline__ruler-row" aria-hidden="true">
        <span />
        <div className="diagnostic-timeline__ruler">
          {[0, 0.25, 0.5, 0.75, 1].map((ratio) => (
            <span key={ratio} style={{ left: `${ratio * 100}%` }}>
              {formatTimestamp(duration * ratio)}
            </span>
          ))}
        </div>
      </div>
      <div className="diagnostic-timeline__rows">
        {rows.map(([detectorId, detectorFindings]) => (
          <div className="diagnostic-timeline__row" key={detectorId}>
            <span className="diagnostic-timeline__label">{detectorId}</span>
            <div
              className="diagnostic-timeline__track"
              data-testid="timeline-marker-track"
            >
              {detectorFindings.map((finding) => (
                <TimelineMarker
                  duration={duration}
                  finding={finding}
                  key={finding.id}
                  onSelect={onSelectFinding}
                  selected={finding.id === selectedFindingId}
                  severityText={severityLabels[finding.severity]}
                />
              ))}
            </div>
          </div>
        ))}
      </div>
      <div aria-hidden="true" className="diagnostic-timeline__coordinate-grid">
        <span aria-hidden="true" />
        <div
          className="diagnostic-timeline__coordinate-track"
          data-testid="timeline-coordinate-track"
        >
          <div
            aria-hidden="true"
            className="diagnostic-timeline__playhead"
            data-testid="timeline-playhead"
            style={{
              left: `${duration > 0 ? (safeTime / duration) * 100 : 0}%`,
            }}
          />
        </div>
      </div>
      <div className="diagnostic-timeline__seek-row">
        <span aria-hidden="true" />
        <div
          className="diagnostic-timeline__seek-track"
          data-testid="timeline-seek-track"
          style={{ minHeight: "2.75rem" }}
        >
          <input
            aria-label={t.diagnostics.playhead}
            className="diagnostic-timeline__slider"
            max={Math.max(0, duration)}
            min={0}
            onChange={(event) => onSeek(Number(event.currentTarget.value))}
            onKeyDown={handleKeys}
            step={0.1}
            type="range"
            value={safeTime}
          />
        </div>
      </div>
    </section>
  );
}
