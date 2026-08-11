import { useEffect, useRef } from "react";

import {
  DiagnosticOverlay,
  DiagnosticTimeline,
  IssueDetailPanel,
  IssueList,
} from "../../components/diagnostics";
import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import type { DemoBrowserReport } from "../../types/report";
import { HomeMedia } from "./HomeMedia";
import { legacyHomeCopy } from "./legacy-home-copy";

interface InteractiveDiagnosisDemoProps {
  currentTime: number;
  report: DemoBrowserReport;
  selectedFinding: Finding;
  onSelectFinding(finding: Finding): void;
  onSeek(time: number): void;
}

export function InteractiveDiagnosisDemo({
  currentTime,
  report,
  selectedFinding,
  onSelectFinding,
  onSeek,
}: InteractiveDiagnosisDemoProps) {
  const { locale, t } = useI18n();
  const copy = legacyHomeCopy[locale];
  const stepRefs = useRef<(HTMLElement | null)[]>([]);

  useEffect(() => {
    if (!("IntersectionObserver" in window)) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
        const index = visible
          ? Number((visible.target as HTMLElement).dataset.findingIndex)
          : Number.NaN;
        const finding = report.findings[index];
        if (finding) onSelectFinding(finding);
      },
      { rootMargin: "-30% 0px -45%", threshold: [0.25, 0.55] },
    );
    stepRefs.current.forEach((element) => {
      if (element) observer.observe(element);
    });
    return () => observer.disconnect();
  }, [onSelectFinding, report]);

  return (
    <section className="home-section home-narrative" id="features">
      <div className="home-section__heading">
        <p className="eyebrow">{copy.narrative.eyebrow}</p>
        <h2>{copy.narrative.title}</h2>
        <p>{copy.narrative.description}</p>
      </div>
      <div className="home-narrative__layout">
        <div className="home-narrative__sticky">
          <span className="demo-label">{copy.demoLabel}</span>
          <div className="home-narrative__stage">
            <HomeMedia
              className="home-narrative__video"
              label={copy.narrative.mediaLabel}
              role="diagnosis"
            />
            <DiagnosticOverlay finding={selectedFinding} />
          </div>
          <DiagnosticTimeline
            currentTime={currentTime}
            duration={report.metadata.duration_seconds}
            findings={report.findings}
            onSeek={onSeek}
            onSelectFinding={onSelectFinding}
            selectedFindingId={selectedFinding.id}
          />
        </div>
        <div className="home-narrative__steps">
          {report.findings.map((finding, index) => (
            <article
              data-finding-index={index}
              data-testid={
                finding.signal_kind === "optional_demo"
                  ? "optional-demo-topic"
                  : undefined
              }
              key={finding.id}
              ref={(element) => {
                stepRefs.current[index] = element;
              }}
            >
              <span className="signal-kind">
                {finding.signal_kind === "browser_cpu"
                  ? copy.narrative.browserCpu
                  : copy.narrative.optional}
              </span>
              <h3>{finding.title}</h3>
              <p>{finding.description}</p>
              <button
                aria-pressed={selectedFinding.id === finding.id}
                className="text-button"
                onClick={() => onSelectFinding(finding)}
                type="button"
              >
                {t.diagnostics.viewDetails}: {finding.title}
              </button>
            </article>
          ))}
          <IssueList
            findings={report.findings}
            onSelectFinding={onSelectFinding}
            selectedFindingId={selectedFinding.id}
          />
          <IssueDetailPanel
            finding={selectedFinding}
            onEvidenceSeek={onSeek}
          />
        </div>
      </div>
    </section>
  );
}
