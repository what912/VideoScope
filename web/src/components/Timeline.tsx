import type { Finding } from "../types";
import { formatTime, intervalPosition } from "../timeline";

interface Props {
  duration: number;
  findings: Finding[];
  currentTime: number;
  selectedId: string | null;
  onSeek: (seconds: number) => void;
  onSelect: (finding: Finding) => void;
}

export function Timeline({
  duration,
  findings,
  currentTime,
  selectedId,
  onSeek,
  onSelect,
}: Props): React.JSX.Element {
  const playhead = duration > 0 ? Math.min(100, (currentTime / duration) * 100) : 0;
  return (
    <section className="timeline-panel" aria-label="Finding timeline">
      <div className="timeline-header">
        <div>
          <p className="step-label">Diagnostic timeline</p>
          <h2>Observable intervals</h2>
        </div>
        <span className="timeline-duration">{formatTime(duration)}</span>
      </div>
      <div
        className="timeline-track"
        role="slider"
        tabIndex={0}
        aria-label="Video time"
        aria-valuemin={0}
        aria-valuemax={duration}
        aria-valuenow={currentTime}
        onClick={(event) => {
          const bounds = event.currentTarget.getBoundingClientRect();
          onSeek(((event.clientX - bounds.left) / bounds.width) * duration);
        }}
        onKeyDown={(event) => {
          if (event.key === "ArrowRight") onSeek(Math.min(duration, currentTime + 1));
          if (event.key === "ArrowLeft") onSeek(Math.max(0, currentTime - 1));
        }}
      >
        <span className="timeline-playhead" style={{ left: `${playhead}%` }} />
        {findings.map((finding) => {
          const position = intervalPosition(finding.time_range, duration);
          return (
            <button
              key={finding.id}
              type="button"
              className={`timeline-finding severity-${finding.severity} ${
                selectedId === finding.id ? "is-selected" : ""
              }`}
              style={{
                left: `${position.leftPercent}%`,
                width: `${position.widthPercent}%`,
              }}
              aria-label={`${finding.severity}: ${finding.title}, ${formatTime(
                finding.time_range.start_seconds,
              )} to ${formatTime(finding.time_range.end_seconds)}`}
              onClick={(event) => {
                event.stopPropagation();
                onSelect(finding);
              }}
            />
          );
        })}
      </div>
      <div className="timeline-ticks" aria-hidden="true">
        <span>0:00</span>
        <span>{formatTime(duration / 2)}</span>
        <span>{formatTime(duration)}</span>
      </div>
    </section>
  );
}
