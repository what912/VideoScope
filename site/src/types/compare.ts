import type { BrowserReport } from "./report";

export interface ComparisonMetric {
  metric_id: string;
  left_value: number;
  right_value: number;
  unit: "ratio" | "count" | "seconds";
  difference: number;
}

export interface ComparisonResult {
  left_report: BrowserReport;
  right_report: BrowserReport;
  metrics: ComparisonMetric[];
  synchronized_duration_seconds: number;
  summary: string[];
}
