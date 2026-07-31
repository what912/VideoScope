import {
  DiagnosticOverlay,
  DiagnosticTimeline,
  DetectorStatusList,
} from "../../components/diagnostics";
import { formatTimestamp } from "../../components/diagnostics/diagnostic-geometry";
import { useI18n } from "../../i18n/I18nProvider";
import type { Finding } from "../../types/analysis";
import type { DemoBrowserReport } from "../../types/report";
import { HomeMedia } from "./HomeMedia";

interface ProductProofWindowProps {
  currentTime: number;
  report: DemoBrowserReport;
  selectedFinding: Finding;
  onSelectFinding(finding: Finding): void;
  onSeek(time: number): void;
}

export function ProductProofWindow({
  currentTime,
  report,
  selectedFinding,
  onSelectFinding,
  onSeek,
}: ProductProofWindowProps) {
  const { t } = useI18n();
  return (
    <section className="product-proof">
      <div className="product-proof__toolbar">
        <div>
          <p className="eyebrow">{t.home.demoLabel}</p>
          <h2>{t.home.proof.title}</h2>
        </div>
        <div className="product-proof__meta numeric">
          <span>1280 × 720</span>
          <span>24 FPS</span>
          <span>00:18.000</span>
        </div>
      </div>
      <p>{t.home.proof.description}</p>
      <div className="product-proof__layout">
        <div className="product-proof__stage">
          <HomeMedia
            className="product-proof__video"
            label={t.home.proof.mediaLabel}
            role="product-proof"
          />
          <DiagnosticOverlay finding={selectedFinding} />
          <span
            className="product-proof__time numeric"
            data-testid="home-demo-time"
          >
            {t.home.proof.currentTime} {formatTimestamp(currentTime)}
          </span>
        </div>
        <DetectorStatusList executions={report.detector_executions} />
      </div>
      <DiagnosticTimeline
        currentTime={currentTime}
        duration={report.metadata.duration_seconds}
        findings={report.findings}
        onSeek={onSeek}
        onSelectFinding={onSelectFinding}
        selectedFindingId={selectedFinding.id}
      />
    </section>
  );
}
