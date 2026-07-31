import type {
  BrowserCpuFinding,
  EvidenceThumbnail,
} from "../../types/analysis";
import type { BrowserFindingDraft } from "./contracts";

export interface EvidenceTimestampSelection {
  timestamps: number[];
  requested_count: number;
  capped_by_count: boolean;
}

export function selectEvidenceTimestamps(
  findings: readonly BrowserFindingDraft[],
  maximumItems: number,
): EvidenceTimestampSelection {
  const ordered = [...findings].sort(
    (left, right) =>
      left.time_range.start_seconds - right.time_range.start_seconds ||
      left.detector_id.localeCompare(right.detector_id),
  );
  const allUnique = new Set(
    ordered.flatMap((finding) =>
      finding.evidence.map((evidence) => evidence.timestamp_seconds),
    ),
  );
  const selected = new Set<number>();
  const maximumEvidencePerFinding = Math.max(
    0,
    ...ordered.map((finding) => finding.evidence.length),
  );
  for (
    let evidenceIndex = 0;
    evidenceIndex < maximumEvidencePerFinding &&
    selected.size < maximumItems;
    evidenceIndex += 1
  ) {
    for (const finding of ordered) {
      const timestamp =
        finding.evidence[evidenceIndex]?.timestamp_seconds;
      if (timestamp !== undefined) {
        selected.add(timestamp);
      }
      if (selected.size >= maximumItems) break;
    }
  }
  return {
    timestamps: [...selected].sort((left, right) => left - right),
    requested_count: allUnique.size,
    capped_by_count: allUnique.size > selected.size,
  };
}

export function dataUrlByteSize(dataUrl: string): number {
  return new TextEncoder().encode(dataUrl).byteLength;
}

export function attachEvidenceThumbnails(
  finding: BrowserCpuFinding,
  thumbnails: ReadonlyMap<number, EvidenceThumbnail>,
): BrowserCpuFinding {
  return {
    ...finding,
    evidence: finding.evidence.map((evidence) => {
      const thumbnail = thumbnails.get(evidence.timestamp_seconds);
      return thumbnail ? { ...evidence, thumbnail } : evidence;
    }),
  };
}
