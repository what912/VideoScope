import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import { formatTimestamp } from "./diagnostic-geometry";
import { severitySymbols } from "./severity";
import "./diagnostics.css";

interface DiagnosticOverlayProps {
  finding?: Finding;
}

function readBox(finding: Finding) {
  for (const evidence of finding.evidence) {
    const box = evidence.metadata.bounding_box;
    if (
      box &&
      typeof box === "object" &&
      !Array.isArray(box) &&
      ["x_min", "y_min", "x_max", "y_max"].every(
        (key) => typeof box[key] === "number" && Number.isFinite(box[key]),
      )
    ) {
      const values = box as Record<string, number>;
      return {
        left: Math.max(0, Math.min(1, values.x_min)) * 100,
        top: Math.max(0, Math.min(1, values.y_min)) * 100,
        width:
          Math.max(0, Math.min(1, values.x_max) - Math.max(0, values.x_min)) *
          100,
        height:
          Math.max(0, Math.min(1, values.y_max) - Math.max(0, values.y_min)) *
          100,
      };
    }
  }
  return { left: 12, top: 14, width: 76, height: 72 };
}

export function DiagnosticOverlay({ finding }: DiagnosticOverlayProps) {
  const { t } = useI18n();
  if (!finding) return null;
  const box = readBox(finding);
  return (
    <div
      aria-label={t.diagnostics.activeFinding}
      className="diagnostic-overlay"
      data-testid="diagnostic-overlay"
    >
      <div
        className="diagnostic-overlay__box"
        data-testid="diagnostic-overlay-box"
        style={{
          left: `${box.left}%`,
          top: `${box.top}%`,
          width: `${box.width}%`,
          height: `${box.height}%`,
        }}
      >
        <span className="diagnostic-overlay__label">
          <span aria-hidden="true">{severitySymbols[finding.severity]}</span>{" "}
          {finding.title}
        </span>
      </div>
      <span className="diagnostic-overlay__time numeric">
        {formatTimestamp(finding.time_range.start_seconds)}–
        {formatTimestamp(finding.time_range.end_seconds)}
      </span>
    </div>
  );
}
