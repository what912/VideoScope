import type { Severity } from "../../types/analysis";

export const severitySymbols: Record<Severity, string> = {
  info: "i",
  low: "↓",
  medium: "!",
  high: "!!",
  critical: "!!!",
};

export function severityLabel(
  severity: Severity,
  labels: Record<Severity, string>,
) {
  return labels[severity];
}
