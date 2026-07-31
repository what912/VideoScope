import { useCallback, useMemo, useRef, useState } from "react";

import { createDemoReport } from "../../data/demo-report";
import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import { ComparePreview } from "./ComparePreview";
import { FinalCta } from "./FinalCta";
import { Hero } from "./Hero";
import { HomeUploadLab } from "./HomeUploadLab";
import { InteractiveDiagnosisDemo } from "./InteractiveDiagnosisDemo";
import { MetricsSpectrum } from "./MetricsSpectrum";
import { OpenSourceSection } from "./OpenSourceSection";
import { ProductProofWindow } from "./ProductProofWindow";
import { WorkflowSection } from "./WorkflowSection";
import "./home.css";

export function HomePage() {
  const { locale } = useI18n();
  const report = useMemo(() => createDemoReport(locale), [locale]);
  const uploadRef = useRef<HTMLElement>(null);
  const demoRef = useRef<HTMLDivElement>(null);
  const [selectedFindingId, setSelectedFindingId] = useState(
    report.findings[0].id,
  );
  const selectedFinding =
    report.findings.find((finding) => finding.id === selectedFindingId) ??
    report.findings[0];
  const [currentTime, setCurrentTime] = useState(
    report.findings[0].time_range.start_seconds,
  );

  const selectFinding = useCallback((finding: Finding) => {
    setSelectedFindingId(finding.id);
    setCurrentTime(finding.time_range.start_seconds);
  }, []);

  const focusSection = (element: HTMLElement | null) => {
    const reducedMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    element?.scrollIntoView({
      behavior: reducedMotion ? "auto" : "smooth",
      block: "start",
    });
    element?.focus({ preventScroll: true });
  };

  const openDemo = () => {
    selectFinding(report.findings[0]);
    focusSection(demoRef.current);
  };

  return (
    <div className="home-page">
      <Hero
        intervalCount={report.summary.review_interval_count}
        onAnalyze={() => focusSection(uploadRef.current)}
        onDemo={openDemo}
      />
      <ProductProofWindow
        currentTime={currentTime}
        onSeek={setCurrentTime}
        onSelectFinding={selectFinding}
        report={report}
        selectedFinding={selectedFinding}
      />
      <section
        className="home-anchor"
        data-testid="home-upload-lab"
        ref={uploadRef}
        tabIndex={-1}
      >
        <HomeUploadLab />
      </section>
      <div className="home-anchor" ref={demoRef} tabIndex={-1}>
        <InteractiveDiagnosisDemo
          currentTime={currentTime}
          onSeek={setCurrentTime}
          onSelectFinding={selectFinding}
          report={report}
          selectedFinding={selectedFinding}
        />
      </div>
      <MetricsSpectrum metrics={report.metrics} />
      <ComparePreview />
      <WorkflowSection />
      <OpenSourceSection />
      <FinalCta
        onAnalyze={() => focusSection(uploadRef.current)}
        onDemo={openDemo}
      />
    </div>
  );
}
