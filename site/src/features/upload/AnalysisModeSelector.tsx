import type { Dictionary } from "../../i18n/types";
import type { BrowserAnalysisModeId } from "./analysis-modes";
import { Link } from "react-router-dom";

interface AnalysisModeSelectorProps {
  copy: Dictionary["upload"];
  selectedMode: BrowserAnalysisModeId;
  onSelect(mode: BrowserAnalysisModeId): void;
}

const browserModes: BrowserAnalysisModeId[] = [
  "quick",
  "deep",
  "research",
];

export function AnalysisModeSelector({
  copy,
  selectedMode,
  onSelect,
}: AnalysisModeSelectorProps) {
  return (
    <fieldset className="analysis-modes">
      <legend>{copy.modesLabel}</legend>
      <div className="analysis-modes__grid">
        {browserModes.map((mode) => (
          <label
            className="analysis-mode"
            data-selected={selectedMode === mode}
            key={mode}
          >
            <input
              checked={selectedMode === mode}
              name="analysis-mode"
              onChange={() => onSelect(mode)}
              type="radio"
              value={mode}
            />
            <span className="analysis-mode__signal">{copy.cpuBadge}</span>
            <strong>{copy.modes[mode].label}</strong>
            <span>{copy.modes[mode].description}</span>
          </label>
        ))}
        <Link className="analysis-mode analysis-mode--action" to="/compare">
          <strong>{copy.modes.compare.label}</strong>
          <span>{copy.modes.compare.description}</span>
          <small>{copy.compareHint}</small>
        </Link>
        <div className="analysis-mode analysis-mode--action analysis-mode--batch">
          <button
            aria-describedby="batch-mode-hint"
            disabled
            type="button"
          >
            <strong>{copy.modes.batch.label}</strong>
            <span>{copy.modes.batch.description}</span>
          </button>
          <small id="batch-mode-hint">{copy.batchHint}</small>
          <Link to="/docs#batch-analysis">{copy.batchDocumentation}</Link>
        </div>
      </div>
    </fieldset>
  );
}
