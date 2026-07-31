import { DiagnosticTimeline } from "../../components/diagnostics/DiagnosticTimeline";
import { IssueDetailPanel } from "../../components/diagnostics/IssueDetailPanel";
import { VideoPlayer } from "../../components/diagnostics/VideoPlayer";
import { formatTimestamp } from "../../components/diagnostics/diagnostic-geometry";
import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import type { BrowserReport } from "../../types/report";
import type { ComparisonSide as Side } from "./comparison";

interface ComparisonSideProps {
  side: Side;
  report?: BrowserReport;
  mediaUrl?: string;
  currentTime: number;
  timelineDuration: number;
  playing: boolean;
  playbackRate: number;
  selectedFindingId?: string;
  disabled: boolean;
  onFile(file: File | undefined): void;
  onReportFile(file: File | undefined): void;
  onSeek(seconds: number): void;
  onTimeUpdate(seconds: number): void;
  onPlayingChange(playing: boolean): void;
  onSelectFinding(finding: Finding): void;
}

export function ComparisonSide({
  side,
  report,
  mediaUrl,
  currentTime,
  timelineDuration,
  playing,
  playbackRate,
  selectedFindingId,
  disabled,
  onFile,
  onReportFile,
  onSeek,
  onTimeUpdate,
  onPlayingChange,
  onSelectFinding,
}: ComparisonSideProps) {
  const { t } = useI18n();
  const isA = side === "a";
  const sideLabel = isA ? t.compare.sideA : t.compare.sideB;
  const videoLabel = isA ? t.compare.videoA : t.compare.videoB;
  const localVideoLabel = isA ? t.compare.localVideoA : t.compare.localVideoB;
  const reportLabel = isA ? t.compare.reportA : t.compare.reportB;
  const previousFrameLabel = isA
    ? t.compare.previousFrameA
    : t.compare.previousFrameB;
  const nextFrameLabel = isA
    ? t.compare.nextFrameA
    : t.compare.nextFrameB;
  const duration = report?.metadata.duration_seconds ?? 0;
  const frameStep = 1 / Math.max(1, report?.metadata.frame_rate ?? 24);
  const selectedFinding = report?.findings.find(
    (finding) => finding.id === selectedFindingId,
  );
  const hasOptionalDemo = report?.findings.some(
    (finding) => finding.signal_kind === "optional_demo",
  );

  return (
    <section
      aria-label={sideLabel}
      className="comparison-side"
      data-side={side}
      data-testid={`comparison-side-${side}`}
    >
      <header className="comparison-side__header">
        <div>
          <span className="comparison-side__letter">{side.toUpperCase()}</span>
          <h2>{report?.title ?? videoLabel}</h2>
          {report?.source === "demo" ? (
            <span className="comparison-side__badge">
              {t.compare.interactiveDemo}
            </span>
          ) : null}
          {hasOptionalDemo ? (
            <span className="comparison-side__badge" data-kind="optional">
              {t.compare.optionalDemo}
            </span>
          ) : null}
        </div>
        {report ? (
          <span className="numeric">
            {report.metadata.width}×{report.metadata.height} ·{" "}
            {formatTimestamp(duration)}
          </span>
        ) : null}
      </header>

      <div className="comparison-side__inputs">
        <label className="comparison-file-control">
          <span>{localVideoLabel}</span>
          <input
            accept="video/mp4,video/webm,video/quicktime,video/x-matroska"
            aria-label={localVideoLabel}
            disabled={disabled}
            onChange={(event) => onFile(event.currentTarget.files?.[0])}
            type="file"
          />
        </label>
        <label className="comparison-file-control">
          <span>{reportLabel}</span>
          <input
            accept=".json,application/json"
            aria-label={reportLabel}
            disabled={disabled}
            onChange={(event) =>
              onReportFile(event.currentTarget.files?.[0])
            }
            type="file"
          />
        </label>
      </div>

      <VideoPlayer
        currentTime={currentTime}
        duration={duration}
        onPlayingChange={onPlayingChange}
        onSeek={onSeek}
        onTimeUpdate={onTimeUpdate}
        playbackRate={playbackRate}
        playing={playing}
        selectedFinding={selectedFinding}
        src={mediaUrl}
        videoHeight={report?.metadata.height}
        videoWidth={report?.metadata.width}
      />

      <div className="comparison-side__frame-controls">
        <button
          aria-label={previousFrameLabel}
          className="button button--quiet"
          disabled={!report}
          onClick={() => onSeek(currentTime - frameStep)}
          type="button"
        >
          −1f
        </button>
        <output
          className="numeric"
          data-testid={`compare-time-${side}`}
        >
          {formatTimestamp(currentTime)}
        </output>
        <button
          aria-label={nextFrameLabel}
          className="button button--quiet"
          disabled={!report}
          onClick={() => onSeek(currentTime + frameStep)}
          type="button"
        >
          +1f
        </button>
      </div>

      {report ? (
        <DiagnosticTimeline
          currentTime={currentTime}
          duration={timelineDuration}
          findings={report.findings}
          onSeek={onSeek}
          onSelectFinding={onSelectFinding}
          selectedFindingId={selectedFinding?.id}
          seekStep={frameStep}
        />
      ) : (
        <p className="comparison-side__empty">{t.compare.inputHint}</p>
      )}
      {report ? (
        <IssueDetailPanel
          finding={selectedFinding}
          onEvidenceSeek={onSeek}
        />
      ) : null}
    </section>
  );
}
