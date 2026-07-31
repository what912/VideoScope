import { describe, expect, it } from "vitest";

import {
  clearAllLocalShareRecords,
  getLocalShareRecordUsage,
  LocalShareRecordStore,
  MemoryShareRecordStore,
  type ShareRecord,
} from "./share-record-store";

const record: ShareRecord = {
  publicId: "00000000-0000-4000-8000-000000000001",
  createdAt: "2026-07-30T08:00:00.000Z",
  expiresAt: "2026-08-06T08:00:00.000Z",
  reportId: "local-report",
  title: "Team review",
};

describe("share record store", () => {
  it("counts and removes every VideoScope share index without touching unrelated storage", async () => {
    const storage = new Map<string, string>([
      ["videoscope.share-records.v1.owner-a", JSON.stringify([record])],
      [
        "videoscope.share-records.v1.owner-b",
        JSON.stringify([{ ...record, publicId: "public-b" }]),
      ],
      ["another.application.preference", "keep-me"],
    ]);
    const adapter: Storage = {
      get length() {
        return storage.size;
      },
      clear: () => storage.clear(),
      getItem: (key) => storage.get(key) ?? null,
      key: (index) => [...storage.keys()][index] ?? null,
      removeItem: (key) => {
        storage.delete(key);
      },
      setItem: (key, value) => storage.set(key, value),
    };

    await expect(getLocalShareRecordUsage(adapter)).resolves.toEqual({
      bytes_used: expect.any(Number),
      key_count: 2,
      record_count: 2,
    });
    await expect(clearAllLocalShareRecords(adapter)).resolves.toEqual({
      removed_key_count: 2,
    });
    await expect(getLocalShareRecordUsage(adapter)).resolves.toEqual({
      bytes_used: 0,
      key_count: 0,
      record_count: 0,
    });
    expect(storage.get("another.application.preference")).toBe("keep-me");
  });

  it("attempts every matching key when one share index cannot be removed", async () => {
    const storage = new Map<string, string>([
      ["videoscope.share-records.v1.owner-a", JSON.stringify([record])],
      [
        "videoscope.share-records.v1.owner-b",
        JSON.stringify([{ ...record, publicId: "public-b" }]),
      ],
    ]);
    const adapter: Storage = {
      get length() {
        return storage.size;
      },
      clear: () => storage.clear(),
      getItem: (key) => storage.get(key) ?? null,
      key: (index) => [...storage.keys()][index] ?? null,
      removeItem: (key) => {
        if (key.endsWith("owner-a")) throw new Error("blocked");
        storage.delete(key);
      },
      setItem: (key, value) => storage.set(key, value),
    };

    await expect(clearAllLocalShareRecords(adapter)).rejects.toThrow(
      /could not be removed/i,
    );
    expect(storage.has("videoscope.share-records.v1.owner-a")).toBe(true);
    expect(storage.has("videoscope.share-records.v1.owner-b")).toBe(false);
  });

  it("persists only the minimal owner-scoped record and restores it", async () => {
    const storage = new Map<string, string>();
    const adapter: Storage = {
      get length() {
        return storage.size;
      },
      clear: () => storage.clear(),
      getItem: (key) => storage.get(key) ?? null,
      key: (index) => [...storage.keys()][index] ?? null,
      removeItem: (key) => storage.delete(key),
      setItem: (key, value) => storage.set(key, value),
    };
    const store = new LocalShareRecordStore(adapter, () =>
      new Date("2026-07-31T00:00:00.000Z"),
    );

    await store.put("owner-a", record);

    const restoredStore = new LocalShareRecordStore(adapter, () =>
      new Date("2026-07-31T00:00:00.000Z"),
    );
    await expect(
      restoredStore.list("owner-a", "local-report"),
    ).resolves.toEqual([record]);
    await expect(
      restoredStore.list("owner-b", "local-report"),
    ).resolves.toEqual([]);
    const serialized = [...storage.values()].join("");
    expect(serialized).not.toMatch(
      /report_json|share_schema_version|video|finding|evidence/i,
    );
    expect(JSON.parse(serialized)[0]).toEqual(record);
  });

  it("removes revoked records and prunes expired records", async () => {
    const store = new MemoryShareRecordStore(() =>
      new Date("2026-08-07T00:00:00.000Z"),
    );
    await store.put("owner-a", record);
    await expect(store.list("owner-a", "local-report")).resolves.toEqual([]);

    await store.put("owner-a", { ...record, expiresAt: undefined });
    await store.remove("owner-a", record.publicId);
    await expect(store.list("owner-a", "local-report")).resolves.toEqual([]);
  });
});
