import { describe, expect, it } from "vitest";

import { hashFileIncrementally, makeDeterministicFindingId } from "./hash";

describe("incremental browser hashing", () => {
  it("hashes a file without using File.arrayBuffer", async () => {
    const file = new File(["abc"], "sample.mp4", { type: "video/mp4" });
    Object.defineProperty(file, "arrayBuffer", {
      value: () => {
        throw new Error("whole-file buffering is forbidden");
      },
    });

    await expect(
      hashFileIncrementally(file, new AbortController().signal),
    ).resolves.toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });

  it("returns deterministic IDs that change with effective configuration", async () => {
    const base = {
      inputHash: "a".repeat(64),
      detectorId: "near_black",
      detectorVersion: "browser-1",
      startSeconds: 2,
      endSeconds: 4,
    };

    const first = await makeDeterministicFindingId({
      ...base,
      configuration: { mean_luma_threshold: 12, enabled: true },
    });
    const reordered = await makeDeterministicFindingId({
      ...base,
      configuration: { enabled: true, mean_luma_threshold: 12 },
    });
    const changed = await makeDeterministicFindingId({
      ...base,
      configuration: { mean_luma_threshold: 13, enabled: true },
    });

    expect(first).toMatch(/^finding_[a-f0-9]{64}$/);
    expect(reordered).toBe(first);
    expect(changed).not.toBe(first);
  });

  it("aborts before reading when the signal is already cancelled", async () => {
    const controller = new AbortController();
    controller.abort();

    await expect(
      hashFileIncrementally(new File(["abc"], "sample.mp4"), controller.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
  });

  it("cancels a pending stream read promptly after abort", async () => {
    let cancelled = false;
    let markPullStarted: (() => void) | undefined;
    const pullStarted = new Promise<void>((resolve) => {
      markPullStarted = resolve;
    });
    const stream = {
      getReader: () => ({
        read: () => {
          markPullStarted?.();
          return new Promise<ReadableStreamReadResult<Uint8Array>>(
            () => undefined,
          );
        },
        cancel: async () => {
          cancelled = true;
        },
        releaseLock: () => undefined,
      }),
    } as unknown as ReadableStream<Uint8Array>;
    const file = new File(["unused"], "sample.mp4");
    Object.defineProperty(file, "stream", { value: () => stream });
    const controller = new AbortController();
    const hashing = hashFileIncrementally(file, controller.signal).then(
      () => "resolved",
      (error: unknown) =>
        error instanceof DOMException ? error.name : "unexpected-error",
    );

    await pullStarted;
    controller.abort();
    const outcome = await Promise.race([
      hashing,
      new Promise<string>((resolve) =>
        setTimeout(() => resolve("timed-out"), 100),
      ),
    ]);

    expect(outcome).toBe("AbortError");
    expect(cancelled).toBe(true);
  });

  it("aborts an in-flight FileReader fallback slice", async () => {
    const originalFileReader = globalThis.FileReader;
    let readStarted: (() => void) | undefined;
    const started = new Promise<void>((resolve) => {
      readStarted = resolve;
    });
    let aborted = false;
    class PendingFileReader {
      result: ArrayBuffer | string | null = null;
      error: DOMException | null = null;
      private listeners = new Map<string, EventListener>();
      addEventListener(type: string, listener: EventListener) {
        this.listeners.set(type, listener);
      }
      removeEventListener(type: string) {
        this.listeners.delete(type);
      }
      readAsArrayBuffer() {
        readStarted?.();
      }
      abort() {
        aborted = true;
        this.listeners.get("abort")?.(new Event("abort"));
      }
    }
    Object.defineProperty(globalThis, "FileReader", {
      configurable: true,
      value: PendingFileReader,
    });
    const file = new File(["fallback"], "sample.mp4");
    Object.defineProperty(file, "stream", { value: undefined });
    const controller = new AbortController();
    try {
      const hashing = hashFileIncrementally(file, controller.signal);
      await started;
      controller.abort();

      await expect(hashing).rejects.toMatchObject({ name: "AbortError" });
      expect(aborted).toBe(true);
    } finally {
      Object.defineProperty(globalThis, "FileReader", {
        configurable: true,
        value: originalFileReader,
      });
    }
  });
});
