export interface ShareRecord {
  publicId: string;
  createdAt: string;
  expiresAt?: string;
  reportId: string;
  title: string;
}

export interface ShareRecordStore {
  list(ownerId: string, reportId?: string): Promise<ShareRecord[]>;
  put(ownerId: string, record: ShareRecord): Promise<void>;
  remove(ownerId: string, publicId: string): Promise<void>;
}

export interface LocalShareRecordUsage {
  bytes_used: number;
  key_count: number;
  record_count: number;
}

const STORAGE_PREFIX = "videoscope.share-records.v1.";
const MAX_RECORDS = 100;

function cleanRecord(value: unknown): ShareRecord | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return undefined;
  }
  const candidate = value as Record<string, unknown>;
  const allowed = new Set([
    "publicId",
    "createdAt",
    "expiresAt",
    "reportId",
    "title",
  ]);
  if (Object.keys(candidate).some((key) => !allowed.has(key))) return undefined;
  if (
    typeof candidate.publicId !== "string" ||
    typeof candidate.createdAt !== "string" ||
    typeof candidate.reportId !== "string" ||
    typeof candidate.title !== "string" ||
    candidate.publicId.length === 0 ||
    candidate.publicId.length > 256 ||
    candidate.reportId.length === 0 ||
    candidate.reportId.length > 512 ||
    candidate.title.length === 0 ||
    candidate.title.length > 512 ||
    !Number.isFinite(Date.parse(candidate.createdAt)) ||
    (candidate.expiresAt !== undefined &&
      (typeof candidate.expiresAt !== "string" ||
        !Number.isFinite(Date.parse(candidate.expiresAt))))
  ) {
    return undefined;
  }
  return {
    publicId: candidate.publicId,
    createdAt: candidate.createdAt,
    ...(candidate.expiresAt ? { expiresAt: candidate.expiresAt } : {}),
    reportId: candidate.reportId,
    title: candidate.title,
  };
}

function activeRecords(records: ShareRecord[], now: Date) {
  const nowMs = now.getTime();
  return records.filter(
    (record) =>
      record.expiresAt === undefined ||
      Date.parse(record.expiresAt) > nowMs,
  );
}

function upsert(records: ShareRecord[], record: ShareRecord) {
  return [
    record,
    ...records.filter((candidate) => candidate.publicId !== record.publicId),
  ]
    .sort((left, right) => right.createdAt.localeCompare(left.createdAt))
    .slice(0, MAX_RECORDS);
}

function shareStorageKeys(storage: Storage) {
  const keys: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key?.startsWith(STORAGE_PREFIX)) keys.push(key);
  }
  return keys;
}

export async function getLocalShareRecordUsage(
  storage: Storage = window.localStorage,
): Promise<LocalShareRecordUsage> {
  let bytesUsed = 0;
  let recordCount = 0;
  const keys = shareStorageKeys(storage);
  for (const key of keys) {
    const serialized = storage.getItem(key) ?? "";
    bytesUsed += new TextEncoder().encode(`${key}${serialized}`).byteLength;
    try {
      const parsed: unknown = JSON.parse(serialized);
      if (Array.isArray(parsed)) {
        recordCount += parsed.reduce(
          (count, item) => count + (cleanRecord(item) ? 1 : 0),
          0,
        );
      }
    } catch {
      // Invalid legacy entries still count toward bytes and can be removed.
    }
  }
  return {
    bytes_used: bytesUsed,
    key_count: keys.length,
    record_count: recordCount,
  };
}

export async function clearAllLocalShareRecords(
  storage: Storage = window.localStorage,
) {
  const errors: unknown[] = [];
  let removedKeyCount = 0;
  for (const key of shareStorageKeys(storage)) {
    try {
      storage.removeItem(key);
      removedKeyCount += 1;
    } catch (error) {
      errors.push(error);
    }
  }
  if (errors.length > 0) {
    throw new AggregateError(
      errors,
      "Some local share index data could not be removed.",
    );
  }
  return { removed_key_count: removedKeyCount };
}

export class LocalShareRecordStore implements ShareRecordStore {
  readonly #now: () => Date;
  readonly #storage: Storage;

  constructor(
    storage: Storage = window.localStorage,
    now: () => Date = () => new Date(),
  ) {
    this.#storage = storage;
    this.#now = now;
  }

  async list(ownerId: string, reportId?: string) {
    const records = this.#read(ownerId);
    const active = activeRecords(records, this.#now());
    if (active.length !== records.length) this.#write(ownerId, active);
    return active
      .filter((record) => reportId === undefined || record.reportId === reportId)
      .map((record) => structuredClone(record));
  }

  async put(ownerId: string, record: ShareRecord) {
    const clean = cleanRecord(record);
    if (!clean) throw new TypeError("Invalid local share record.");
    this.#write(ownerId, upsert(this.#read(ownerId), clean));
  }

  async remove(ownerId: string, publicId: string) {
    this.#write(
      ownerId,
      this.#read(ownerId).filter((record) => record.publicId !== publicId),
    );
  }

  #key(ownerId: string) {
    return `${STORAGE_PREFIX}${encodeURIComponent(ownerId)}`;
  }

  #read(ownerId: string) {
    try {
      const value = JSON.parse(this.#storage.getItem(this.#key(ownerId)) ?? "[]");
      return Array.isArray(value)
        ? value.flatMap((item) => {
            const clean = cleanRecord(item);
            return clean ? [clean] : [];
          })
        : [];
    } catch {
      return [];
    }
  }

  #write(ownerId: string, records: ShareRecord[]) {
    try {
      this.#storage.setItem(this.#key(ownerId), JSON.stringify(records));
    } catch {
      throw new Error("Local share index is unavailable.");
    }
  }
}

export class MemoryShareRecordStore implements ShareRecordStore {
  readonly #now: () => Date;
  readonly #records = new Map<string, ShareRecord[]>();

  constructor(now: () => Date = () => new Date()) {
    this.#now = now;
  }

  async list(ownerId: string, reportId?: string) {
    const records = activeRecords(this.#records.get(ownerId) ?? [], this.#now());
    this.#records.set(ownerId, records);
    return records
      .filter((record) => reportId === undefined || record.reportId === reportId)
      .map((record) => structuredClone(record));
  }

  async put(ownerId: string, record: ShareRecord) {
    const clean = cleanRecord(record);
    if (!clean) throw new TypeError("Invalid local share record.");
    this.#records.set(
      ownerId,
      upsert(this.#records.get(ownerId) ?? [], clean),
    );
  }

  async remove(ownerId: string, publicId: string) {
    this.#records.set(
      ownerId,
      (this.#records.get(ownerId) ?? []).filter(
        (record) => record.publicId !== publicId,
      ),
    );
  }
}

export function createShareRecordStore(): ShareRecordStore {
  if (typeof window === "undefined") return new MemoryShareRecordStore();
  try {
    return new LocalShareRecordStore(window.localStorage);
  } catch {
    return new MemoryShareRecordStore();
  }
}
