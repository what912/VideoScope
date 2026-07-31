import type { Locale } from "../i18n/types";
import type {
  DetectorConfiguration,
  DetectorExecution,
  Finding,
  JsonValue,
  QualityMetric,
  Severity,
  SignalKind,
} from "../types/analysis";
import {
  BROWSER_REPORT_SCHEMA_VERSION,
  type DemoBrowserReport,
} from "../types/report";
import {
  demoCopyByLocale,
  type DemoFindingId,
  type DemoMetricId,
} from "./demo-copy";
import { homepageMedia, type HomepageMediaRole } from "./media-manifest";

interface DemoFindingSpec {
  readonly id: DemoFindingId;
  readonly detectorId: string;
  readonly detectorVersion: string;
  readonly signalKind: SignalKind;
  readonly severity: Severity;
  readonly score: number;
  readonly confidence: number;
  readonly start: number;
  readonly end: number;
  readonly evidenceRole: HomepageMediaRole;
  readonly parameters: Record<string, JsonValue>;
}

interface DemoMetricSpec {
  readonly id: DemoMetricId;
  readonly value: number;
  readonly kind: QualityMetric["kind"];
  readonly detectorId: string;
}

const findingSpecs: readonly DemoFindingSpec[] = [
  {
    id: "demo-flicker",
    detectorId: "global_flicker",
    detectorVersion: "browser-demo-1",
    signalKind: "browser_cpu",
    severity: "medium",
    score: 0.72,
    confidence: 0.79,
    start: 3.2,
    end: 4.1,
    evidenceRole: "evidence-b",
    parameters: {
      residual_threshold: 0.18,
      scene_boundary_guard_seconds: 0.25,
    },
  },
  {
    id: "demo-hand-geometry",
    detectorId: "demo_optional_geometry",
    detectorVersion: "demo-1",
    signalKind: "optional_demo",
    severity: "high",
    score: 0.86,
    confidence: 0.75,
    start: 6.8,
    end: 7.5,
    evidenceRole: "evidence-c",
    parameters: { provider: "demo", mode: "descriptive" },
  },
  {
    id: "demo-background-warping",
    detectorId: "demo_optional_background",
    detectorVersion: "demo-1",
    signalKind: "optional_demo",
    severity: "medium",
    score: 0.68,
    confidence: 0.7,
    start: 9,
    end: 10.4,
    evidenceRole: "evidence-a",
    parameters: { provider: "demo", comparison_window_seconds: 0.8 },
  },
  {
    id: "demo-text-instability",
    detectorId: "demo_optional_text",
    detectorVersion: "demo-1",
    signalKind: "optional_demo",
    severity: "high",
    score: 0.84,
    confidence: 0.74,
    start: 12.1,
    end: 12.8,
    evidenceRole: "evidence-b",
    parameters: { provider: "demo-ocr", minimum_track_frames: 3 },
  },
  {
    id: "demo-motion-jitter",
    detectorId: "demo_optional_motion",
    detectorVersion: "demo-1",
    signalKind: "optional_demo",
    severity: "low",
    score: 0.41,
    confidence: 0.66,
    start: 15.2,
    end: 16,
    evidenceRole: "evidence-c",
    parameters: { provider: "demo", temporal_window_frames: 4 },
  },
];

const metricSpecs: readonly DemoMetricSpec[] = [
  {
    id: "luminance-stability",
    value: 0.71,
    kind: "browser_cpu",
    detectorId: "global_flicker",
  },
  {
    id: "relative-sharpness",
    value: 0.88,
    kind: "browser_cpu",
    detectorId: "scene_relative_blur",
  },
  {
    id: "frame-change",
    value: 0.76,
    kind: "browser_cpu",
    detectorId: "possible_freeze",
  },
  {
    id: "dark-interval-screening",
    value: 0.91,
    kind: "browser_cpu",
    detectorId: "near_black",
  },
  {
    id: "geometry-consistency",
    value: 0.69,
    kind: "optional_demo",
    detectorId: "demo_optional_geometry",
  },
  {
    id: "text-stability",
    value: 0.64,
    kind: "optional_demo",
    detectorId: "demo_optional_text",
  },
  {
    id: "background-stability",
    value: 0.79,
    kind: "optional_demo",
    detectorId: "demo_optional_background",
  },
  {
    id: "motion-continuity",
    value: 0.76,
    kind: "optional_demo",
    detectorId: "demo_optional_motion",
  },
];

function posterFor(role: HomepageMediaRole) {
  const media = homepageMedia.find((item) => item.role === role);
  if (!media) throw new Error(`Missing demo media role: ${role}`);
  const filename = media.poster.split("/").at(-1);
  if (!filename) throw new Error(`Missing demo poster filename: ${role}`);
  return `media/${filename}`;
}

function makeFindings(locale: Locale): Finding[] {
  const copy = demoCopyByLocale[locale];
  return findingSpecs.map((spec) => {
    const findingCopy = copy.findings[spec.id];
    const midpoint = Number(((spec.start + spec.end) / 2).toFixed(2));
    return {
      id: spec.id,
      detector_id: spec.detectorId,
      detector_version: spec.detectorVersion,
      signal_kind: spec.signalKind,
      title: findingCopy.title,
      description: findingCopy.description,
      severity: spec.severity,
      score: spec.score,
      confidence: spec.confidence,
      time_range: {
        start_seconds: spec.start,
        end_seconds: spec.end,
      },
      evidence: [
        {
          evidence_type: "frame",
          timestamp_seconds: midpoint,
          description: `${findingCopy.title} ${copy.evidenceFrame}`,
          thumbnail: {
            src: posterFor(spec.evidenceRole),
            width: 480,
            height: 270,
          },
          metadata: {
            demo: true,
            signal_kind: spec.signalKind,
          },
        },
      ],
      tags: ["interactive-demo", spec.signalKind],
      parameters: spec.parameters,
      limitations: [...findingCopy.limitations],
    };
  });
}

function makeMetrics(locale: Locale): QualityMetric[] {
  const copy = demoCopyByLocale[locale];
  return metricSpecs.map((spec) => ({
    id: spec.id,
    label: copy.metrics[spec.id].label,
    value: spec.value,
    kind: spec.kind,
    detector_id: spec.detectorId,
    unit: "ratio",
    description: copy.metrics[spec.id].description,
  }));
}

export function createDemoReport(locale: Locale): DemoBrowserReport {
  const findings = makeFindings(locale);
  const detectorConfigurations: DetectorConfiguration[] = findings.map(
    (finding) => ({
      detector_id: finding.detector_id,
      detector_version: finding.detector_version,
      signal_kind: finding.signal_kind,
      enabled: true,
      parameters: finding.parameters,
    }),
  );
  const detectorExecutions: DetectorExecution[] = findings.map((finding) => ({
    detector_id: finding.detector_id,
    detector_version: finding.detector_version,
    signal_kind: finding.signal_kind,
    status: "ok",
    elapsed_seconds: finding.signal_kind === "browser_cpu" ? 0.18 : 0,
    findings_count: 1,
  }));

  return {
    schema_version: BROWSER_REPORT_SCHEMA_VERSION,
    tool_version: "0.2.0",
    id: "videoscope-interactive-demo",
    analysis_id: "demo-analysis-v1",
    source: "demo",
    demo_label: "INTERACTIVE DEMO",
    title: demoCopyByLocale[locale].reportTitle,
    created_at: "2026-01-01T00:00:00.000Z",
    input_hash: "demo-fixture-v1",
    metadata: {
      filename: "observatory-demo.mp4",
      mime_type: "video/mp4",
      width: 1280,
      height: 720,
      duration_seconds: 18,
      file_size_bytes: 0,
      frame_rate: 24,
      has_audio: false,
    },
    configuration: detectorConfigurations,
    detector_executions: detectorExecutions,
    findings,
    metrics: makeMetrics(locale),
    summary: {
      review_interval_count: findings.length,
      severity_counts: {
        info: 0,
        low: 1,
        medium: 2,
        high: 2,
        critical: 0,
      },
    },
    warnings: [...demoCopyByLocale[locale].warnings],
    runtime: {
      environment: "browser",
      user_agent_family: "demo",
      analysis_seconds: 0.18,
      sample_count: 36,
    },
    reviewed_finding_ids: [],
    preferences: {
      locale,
      creator_view: true,
      reduced_motion: false,
    },
  };
}

export const demoReport = createDemoReport("en");
