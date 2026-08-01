export const DIRECT_MEDIA_MAX_BYTES = 500 * 1024 * 1024;

export type DirectMediaImportErrorCode =
  | "consent_required"
  | "invalid_url"
  | "unsafe_redirect"
  | "redirect_not_supported"
  | "cors_or_network"
  | "request_failed"
  | "invalid_content_type"
  | "file_too_large"
  | "empty_response"
  | "stream_unavailable";

export class DirectMediaImportError extends Error {
  constructor(public readonly code: DirectMediaImportErrorCode) {
    super(code);
    this.name = "DirectMediaImportError";
  }
}

export interface DirectMediaImportDependencies {
  consent: boolean;
  fetch?: typeof globalThis.fetch;
  signal?: AbortSignal;
  maxBytes?: number;
}

function parseSafeHttpsUrl(
  input: string,
  errorCode: "invalid_url" | "unsafe_redirect",
) {
  let url: URL;
  try {
    url = new URL(input);
  } catch {
    throw new DirectMediaImportError(errorCode);
  }
  if (
    url.protocol !== "https:" ||
    Boolean(url.username) ||
    Boolean(url.password) ||
    Boolean(url.port)
  ) {
    throw new DirectMediaImportError(errorCode);
  }
  return url;
}

function safeFilename(url: URL) {
  const finalSegment = url.pathname.split("/").filter(Boolean).at(-1);
  if (!finalSegment) return "remote-video";
  try {
    const decoded = decodeURIComponent(finalSegment);
    const withoutControlCharacters = Array.from(decoded, (character) => {
      const codePoint = character.codePointAt(0) ?? 0;
      return codePoint < 32 || codePoint === 127 ? "_" : character;
    }).join("");
    return (
      withoutControlCharacters
        .replace(/[\\/:*?"<>|]/g, "_")
        .slice(0, 180) || "remote-video"
    );
  } catch {
    return "remote-video";
  }
}

async function readResponse(
  response: Response,
  maxBytes: number,
  signal?: AbortSignal,
): Promise<BlobPart[]> {
  const reader = response.body?.getReader();
  if (!reader) {
    throw new DirectMediaImportError("stream_unavailable");
  }

  const chunks: BlobPart[] = [];
  let totalBytes = 0;
  try {
    while (true) {
      if (signal?.aborted) {
        throw new DOMException("Import cancelled", "AbortError");
      }
      const { done, value } = await reader.read();
      if (done) break;
      if (!value) continue;
      totalBytes += value.byteLength;
      if (totalBytes > maxBytes) {
        await reader.cancel();
        throw new DirectMediaImportError("file_too_large");
      }
      chunks.push(value.slice().buffer as ArrayBuffer);
    }
  } finally {
    reader.releaseLock();
  }
  if (totalBytes === 0) {
    throw new DirectMediaImportError("empty_response");
  }
  return chunks;
}

export async function importDirectMediaUrl(
  input: string,
  dependencies: DirectMediaImportDependencies,
): Promise<File> {
  if (!dependencies.consent) {
    throw new DirectMediaImportError("consent_required");
  }
  const sourceUrl = parseSafeHttpsUrl(input.trim(), "invalid_url");
  const fetchImplementation = dependencies.fetch ?? globalThis.fetch;
  const maxBytes = dependencies.maxBytes ?? DIRECT_MEDIA_MAX_BYTES;
  let response: Response;
  try {
    response = await fetchImplementation(sourceUrl.href, {
      method: "GET",
      mode: "cors",
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: dependencies.signal,
    });
  } catch (error) {
    if (
      error instanceof DOMException &&
      error.name === "AbortError"
    ) {
      throw error;
    }
    throw new DirectMediaImportError("cors_or_network");
  }
  if (!response.ok) {
    throw new DirectMediaImportError("request_failed");
  }
  if (response.redirected) {
    throw new DirectMediaImportError("redirect_not_supported");
  }

  const finalUrl = parseSafeHttpsUrl(
    response.url || sourceUrl.href,
    "invalid_url",
  );
  const contentType = response.headers
    .get("content-type")
    ?.split(";", 1)[0]
    .trim()
    .toLowerCase();
  if (!contentType?.startsWith("video/")) {
    throw new DirectMediaImportError("invalid_content_type");
  }
  const declaredLength = Number(response.headers.get("content-length"));
  if (
    Number.isFinite(declaredLength) &&
    declaredLength > maxBytes
  ) {
    throw new DirectMediaImportError("file_too_large");
  }
  const chunks = await readResponse(response, maxBytes, dependencies.signal);
  return new File(chunks, safeFilename(finalUrl), { type: contentType });
}
