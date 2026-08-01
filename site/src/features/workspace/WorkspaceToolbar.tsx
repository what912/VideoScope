import { useI18n } from "../../i18n/I18nProvider";

interface WorkspaceToolbarProps {
  currentTime: number;
  playbackRate: number;
  railOpen: boolean;
  onRailToggle(trigger: HTMLButtonElement): void;
  onFrameStep(direction: -1 | 1): void;
  onSpeedChange(value: number): void;
  onCopy(): void;
  onExport(): void;
  onPrint(): void;
  onNewAnalysis(): void;
  onClear(trigger: HTMLButtonElement): void;
}

export function WorkspaceToolbar({
  playbackRate,
  railOpen,
  onRailToggle,
  onFrameStep,
  onSpeedChange,
  onCopy,
  onExport,
  onPrint,
  onNewAnalysis,
  onClear,
}: WorkspaceToolbarProps) {
  const { t } = useI18n();
  return (
    <nav aria-label={t.workspace.toolbar} className="workspace-toolbar">
      <button
        aria-expanded={railOpen}
        className="button button--quiet"
        onClick={(event) => onRailToggle(event.currentTarget)}
        type="button"
      >
        {t.workspace.projects}
      </button>
      <div className="workspace-toolbar__group">
        <button
          className="icon-button"
          onClick={() => onFrameStep(-1)}
          type="button"
          aria-label={t.workspace.previousFrame}
        >
          ‹
        </button>
        <button
          className="icon-button"
          onClick={() => onFrameStep(1)}
          type="button"
          aria-label={t.workspace.nextFrame}
        >
          ›
        </button>
        <label>
          <span className="visually-hidden">{t.workspace.playbackSpeed}</span>
          <select
            aria-label={t.workspace.playbackSpeed}
            onChange={(event) => onSpeedChange(Number(event.currentTarget.value))}
            value={playbackRate}
          >
            {[0.5, 0.75, 1, 1.25, 1.5, 2].map((rate) => (
              <option key={rate} value={rate}>
                {rate.toFixed(2)}×
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="workspace-toolbar__actions">
        <button className="button button--quiet" onClick={onCopy} type="button">
          {t.workspace.copyTimestamp}
        </button>
        <button className="button button--quiet" onClick={onExport} type="button">
          {t.workspace.exportJson}
        </button>
        <button className="button button--quiet" onClick={onPrint} type="button">
          {t.workspace.printReport}
        </button>
        <button
          className="button button--quiet"
          onClick={onNewAnalysis}
          type="button"
        >
          {t.workspace.newAnalysis}
        </button>
        <button
          className="button button--quiet"
          onClick={(event) => onClear(event.currentTarget)}
          type="button"
        >
          {t.workspace.clearLocalData}
        </button>
      </div>
    </nav>
  );
}
