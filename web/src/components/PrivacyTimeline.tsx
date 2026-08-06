import type { ManualAudioInterval, PrivacyRisk } from "../types";
import type { WorkbenchLocale } from "./PublishReadyView";

interface PrivacyTimelineProps {
  locale: WorkbenchLocale;
  duration: number;
  currentTime: number;
  risks: PrivacyRisk[];
  audioIntervals: ManualAudioInterval[];
  selectedRiskId: string | null;
  onSeek: (seconds: number) => void;
  onSelectRisk: (risk: PrivacyRisk) => void;
}

function position(seconds: number, duration: number): number {
  if (duration <= 0) return 0;
  return Math.max(0, Math.min(100, (seconds / duration) * 100));
}

export function PrivacyTimeline({
  locale,
  duration,
  currentTime,
  risks,
  audioIntervals,
  selectedRiskId,
  onSeek,
  onSelectRisk,
}: PrivacyTimelineProps): React.JSX.Element {
  const zh = locale === "zh-CN";
  const seek = (event: React.MouseEvent<HTMLDivElement>): void => {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (bounds.width <= 0) return;
    onSeek(((event.clientX - bounds.left) / bounds.width) * duration);
  };
  const seekWithKeyboard = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.shiftKey ? 1 : 0.1;
    const direction = event.key === "ArrowRight" ? 1 : -1;
    onSeek(Math.max(0, Math.min(duration, currentTime + direction * delta)));
  };
  return (
    <section className="privacy-timeline-panel" aria-label={zh ? "隐私时间轴" : "Privacy timeline"}>
      <div className="privacy-timeline-meta">
        <strong>{zh ? "风险时间轴" : "Risk timeline"}</strong>
        <span>{currentTime.toFixed(2)} / {duration.toFixed(2)} s</span>
      </div>
      <div
        className="privacy-timeline"
        role="slider"
        tabIndex={0}
        aria-label={zh ? "隐私时间轴" : "Privacy timeline"}
        aria-valuemin={0}
        aria-valuemax={duration}
        aria-valuenow={Number(currentTime.toFixed(2))}
        aria-valuetext={`${currentTime.toFixed(2)} / ${duration.toFixed(2)} s`}
        onClick={seek}
        onKeyDown={seekWithKeyboard}
      >
        <div className="privacy-timeline-grid" aria-hidden="true" />
        {risks.map((risk) => (
          <button
            key={risk.id}
            type="button"
            className={`privacy-timeline-risk severity-${risk.severity} ${
              selectedRiskId === risk.id ? "is-selected" : ""
            }`}
            style={{
              left: `${position(risk.start_seconds, duration)}%`,
              width: `${Math.max(
                0.8,
                position(risk.end_seconds, duration) -
                  position(risk.start_seconds, duration),
              )}%`,
            }}
            aria-label={`${zh ? "跳转到" : "Seek to"} ${risk.title}`}
            onClick={(event) => {
              event.stopPropagation();
              onSelectRisk(risk);
            }}
          />
        ))}
        {audioIntervals.map((interval, index) => (
          <span
            key={`${interval.start_seconds}-${interval.end_seconds}-${index}`}
            className="privacy-audio-range"
            style={{
              left: `${position(interval.start_seconds, duration)}%`,
              width: `${Math.max(
                0.8,
                position(interval.end_seconds, duration) -
                  position(interval.start_seconds, duration),
              )}%`,
            }}
            aria-label={`${zh ? "静音区间" : "Mute interval"} ${interval.start_seconds}–${interval.end_seconds}`}
          />
        ))}
        <span
          className="privacy-playhead"
          style={{ left: `${position(currentTime, duration)}%` }}
          aria-hidden="true"
        />
      </div>
    </section>
  );
}
