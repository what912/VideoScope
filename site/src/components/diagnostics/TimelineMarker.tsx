import { useState } from "react";

import type { Finding, Severity } from "../../types/analysis";
import { intervalToPercent } from "./diagnostic-geometry";
import { severitySymbols } from "./severity";

interface TimelineMarkerProps {
  duration: number;
  finding: Finding;
  selected: boolean;
  severityText: string;
  onSelect(finding: Finding): void;
}

export function TimelineMarker({
  duration,
  finding,
  selected,
  severityText,
  onSelect,
}: TimelineMarkerProps) {
  const [previewVisible, setPreviewVisible] = useState(false);
  const geometry = intervalToPercent(
    finding.time_range.start_seconds,
    finding.time_range.end_seconds,
    duration,
  );
  const evidence = finding.evidence.find((item) => item.thumbnail);
  const activate = () => onSelect(finding);
  const remainingWidth = Math.max(0, 100 - geometry.left);
  const visualWidth = Math.min(geometry.width, remainingWidth);
  const previewAnchor =
    geometry.left <= 15
      ? "start"
      : geometry.left + visualWidth >= 85
        ? "end"
        : "center";

  return (
    <span
      className="timeline-marker-wrap"
      data-preview-anchor={previewAnchor}
      style={{
        left: `${geometry.left}%`,
        width: `${visualWidth}%`,
        maxWidth: `${remainingWidth}%`,
      }}
    >
      <span
        aria-hidden="true"
        className="timeline-marker__visual"
        data-point={visualWidth === 0 || undefined}
        data-severity={finding.severity}
        data-testid="timeline-marker-visual"
      />
      <button
        aria-label={`${severityText}: ${finding.title}`}
        aria-pressed={selected}
        className="timeline-marker timeline-marker__hit-target"
        data-preview-anchor={previewAnchor}
        data-severity={finding.severity}
        onBlur={() => setPreviewVisible(false)}
        onClick={activate}
        onFocus={() => setPreviewVisible(true)}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            activate();
          }
        }}
        onMouseEnter={() => setPreviewVisible(true)}
        onMouseLeave={() => setPreviewVisible(false)}
        type="button"
      >
        <span aria-hidden="true">{severitySymbols[finding.severity]}</span>
        <span className="visually-hidden">{severityText}</span>
      </button>
      {previewVisible && evidence?.thumbnail ? (
        <span
          className="timeline-marker__preview"
          data-preview-anchor={previewAnchor}
          role="tooltip"
        >
          <img
            alt={evidence.description}
            height={evidence.thumbnail.height}
            src={evidence.thumbnail.src}
            width={evidence.thumbnail.width}
          />
          <span>{finding.title}</span>
        </span>
      ) : null}
    </span>
  );
}

export type { Severity };
