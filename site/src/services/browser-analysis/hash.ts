import { createSHA256 } from "hash-wasm";

import type { JsonValue } from "../../types/analysis";
import { abortError, throwIfAborted } from "./errors";

async function updateFromSlices(
  file: File,
  update: (chunk: Uint8Array) => void,
  signal: AbortSignal,
): Promise<void> {
  const readSlice = (blob: Blob) =>
    new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      const cleanup = () => {
        reader.removeEventListener("load", onLoad);
        reader.removeEventListener("error", onError);
        reader.removeEventListener("abort", onReaderAbort);
        signal.removeEventListener("abort", onSignalAbort);
      };
      const onLoad = () => {
        cleanup();
        resolve(reader.result as ArrayBuffer);
      };
      const onError = () => {
        cleanup();
        reject(reader.error ?? new Error("Unable to read file slice"));
      };
      const onReaderAbort = () => {
        cleanup();
        reject(abortError());
      };
      const onSignalAbort = () => {
        reader.abort();
        cleanup();
        reject(abortError());
      };
      reader.addEventListener("load", onLoad);
      reader.addEventListener("error", onError);
      reader.addEventListener("abort", onReaderAbort);
      signal.addEventListener("abort", onSignalAbort, { once: true });
      if (signal.aborted) {
        onSignalAbort();
        return;
      }
      reader.readAsArrayBuffer(blob);
    });
  const chunkSize = 1024 * 1024;
  for (let offset = 0; offset < file.size; offset += chunkSize) {
    throwIfAborted(signal);
    const chunk = file.slice(offset, Math.min(file.size, offset + chunkSize));
    const buffer = await readSlice(chunk);
    update(new Uint8Array(buffer));
  }
}

export async function hashFileIncrementally(
  file: File,
  signal: AbortSignal,
): Promise<string> {
  throwIfAborted(signal);
  const hash = await createSHA256();
  hash.init();
  if (typeof file.stream === "function") {
    const reader = file.stream().getReader();
    try {
      for (;;) {
        throwIfAborted(signal);
        const { done, value } =
          await new Promise<ReadableStreamReadResult<Uint8Array>>(
            (resolve, reject) => {
              const onAbort = () => {
                void reader.cancel().catch(() => undefined);
                reject(abortError());
              };
              signal.addEventListener("abort", onAbort, { once: true });
              void reader.read().then(resolve, reject).finally(() => {
                signal.removeEventListener("abort", onAbort);
              });
            },
          );
        if (done) break;
        hash.update(value);
      }
    } finally {
      try {
        reader.releaseLock();
      } catch {
        // A cancelled reader can remain locked briefly in some engines.
      }
    }
  } else {
    // jsdom's File currently lacks stream(); production browsers use the branch
    // above. This bounded fallback preserves incremental memory behavior.
    await updateFromSlices(file, (chunk) => hash.update(chunk), signal);
  }
  throwIfAborted(signal);
  return hash.digest("hex");
}

function stableJson(value: JsonValue): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(",")}]`;
  }
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map(
        (key) =>
          `${JSON.stringify(key)}:${stableJson(
            (value as Record<string, JsonValue>)[key],
          )}`,
      )
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export interface FindingIdInput {
  inputHash: string;
  detectorId: string;
  detectorVersion: string;
  startSeconds: number;
  endSeconds: number;
  configuration: Record<string, JsonValue>;
}

export async function makeDeterministicFindingId(
  input: FindingIdInput,
): Promise<string> {
  const payload: JsonValue = {
    input_hash: input.inputHash,
    detector_id: input.detectorId,
    detector_version: input.detectorVersion,
    start_seconds: Number(input.startSeconds.toFixed(6)),
    end_seconds: Number(input.endSeconds.toFixed(6)),
    configuration: input.configuration,
  };
  const hash = await createSHA256();
  hash.init();
  hash.update(stableJson(payload));
  return `finding_${hash.digest("hex")}`;
}
