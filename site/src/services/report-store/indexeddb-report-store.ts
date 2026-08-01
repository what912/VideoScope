import type { BrowserReport } from "../../types/report";
import type {
  ReportDatabase,
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

const DATABASE_NAME = "videoscope-browser-reports";
const DATABASE_VERSION = 1;
const STORE_NAME = "reports";

function requestResult<T>(request: IDBRequest<T>) {
  return new Promise<T>((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB error"));
  });
}

function transactionResult(transaction: IDBTransaction) {
  return new Promise<void>((resolve, reject) => {
    const rejectTransaction = () => {
      reject(
        transaction.error ??
          new DOMException("IndexedDB transaction failed", "AbortError"),
      );
    };
    transaction.oncomplete = () => resolve();
    transaction.onabort = rejectTransaction;
    transaction.onerror = rejectTransaction;
  });
}

class NativeReportDatabase implements ReportDatabase {
  constructor(private readonly database: IDBDatabase) {}

  async put(report: BrowserReport) {
    const transaction = this.database.transaction(STORE_NAME, "readwrite");
    const completion = transactionResult(transaction);
    await Promise.all([
      requestResult(transaction.objectStore(STORE_NAME).put(report)),
      completion,
    ]);
  }

  async get(id: string) {
    const transaction = this.database.transaction(STORE_NAME, "readonly");
    const completion = transactionResult(transaction);
    const [report] = await Promise.all([
      requestResult(transaction.objectStore(STORE_NAME).get(id)),
      completion,
    ]);
    return (report as BrowserReport | undefined) ?? null;
  }

  async getAll() {
    const transaction = this.database.transaction(STORE_NAME, "readonly");
    const completion = transactionResult(transaction);
    const [reports] = await Promise.all([
      requestResult(transaction.objectStore(STORE_NAME).getAll()),
      completion,
    ]);
    return reports as BrowserReport[];
  }

  async delete(id: string) {
    const transaction = this.database.transaction(STORE_NAME, "readwrite");
    const completion = transactionResult(transaction);
    await Promise.all([
      requestResult(transaction.objectStore(STORE_NAME).delete(id)),
      completion,
    ]);
  }

  async clear() {
    const transaction = this.database.transaction(STORE_NAME, "readwrite");
    const completion = transactionResult(transaction);
    await Promise.all([
      requestResult(transaction.objectStore(STORE_NAME).clear()),
      completion,
    ]);
  }
}

function openNativeDatabase(factory: IDBFactory): Promise<ReportDatabase> {
  return new Promise((resolve, reject) => {
    const request = factory.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) {
        database.createObjectStore(STORE_NAME, { keyPath: "id" });
      }
    };
    request.onsuccess = () =>
      resolve(new NativeReportDatabase(request.result));
    request.onerror = () =>
      reject(request.error ?? new Error("Unable to open IndexedDB"));
    request.onblocked = () =>
      reject(new Error("IndexedDB upgrade is blocked"));
  });
}

export class IndexedDBReportStore implements ReportStore {
  readonly #database: Promise<ReportDatabase>;

  constructor(options?: {
    indexedDB?: IDBFactory;
    openDatabase?: () => Promise<ReportDatabase>;
  }) {
    if (options?.openDatabase) {
      this.#database = options.openDatabase();
      return;
    }
    const factory = options?.indexedDB ?? globalThis.indexedDB;
    if (!factory) {
      this.#database = Promise.reject(new Error("IndexedDB unavailable"));
      return;
    }
    this.#database = openNativeDatabase(factory);
  }

  async ready() {
    await this.#database;
  }

  async put(report: BrowserReport) {
    const database = await this.#database;
    await database.put(compactReport(report));
  }

  async get(id: string) {
    const database = await this.#database;
    return database.get(id);
  }

  async list(): Promise<ReportIndexEntry[]> {
    const database = await this.#database;
    const reports = await database.getAll();
    return sortReportIndexes(reports.map(reportToIndex));
  }

  async delete(id: string) {
    const database = await this.#database;
    await database.delete(id);
  }

  async clear() {
    const database = await this.#database;
    await database.clear();
  }

  async usage(): Promise<StorageUsage> {
    const database = await this.#database;
    return calculateStorageUsage(await database.getAll());
  }
}
