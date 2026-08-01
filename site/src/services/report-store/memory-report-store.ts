import type { BrowserReport } from "../../types/report";
import type {
  ReportIndexEntry,
  ReportStore,
  StorageUsage,
} from "./report-store";
import {
  calculateStorageUsage,
  compactReport,
  reportToIndex,
  sortReportIndexes,
} from "./report-store";

export class MemoryReportStore implements ReportStore {
  readonly #reports = new Map<string, string>();

  async put(report: BrowserReport) {
    const compact = compactReport(report);
    this.#reports.set(compact.id, JSON.stringify(compact));
  }

  async get(id: string) {
    const json = this.#reports.get(id);
    return json ? (JSON.parse(json) as BrowserReport) : null;
  }

  async list(): Promise<ReportIndexEntry[]> {
    return sortReportIndexes(
      [...this.#reports.values()].map((json) =>
        reportToIndex(JSON.parse(json) as BrowserReport),
      ),
    );
  }

  async delete(id: string) {
    this.#reports.delete(id);
  }

  async clear() {
    this.#reports.clear();
  }

  async usage(): Promise<StorageUsage> {
    return calculateStorageUsage(
      [...this.#reports.values()].map(
        (json) => JSON.parse(json) as BrowserReport,
      ),
    );
  }
}
