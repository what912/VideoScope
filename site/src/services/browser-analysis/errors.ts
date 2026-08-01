export class BrowserAnalysisError extends Error {
  constructor(
    public readonly code:
      | "invalid_input"
      | "metadata_unavailable"
      | "duration_unavailable"
      | "decode_failed"
      | "canvas_unavailable"
      | "memory_pressure",
    message: string,
  ) {
    super(message);
    this.name = "BrowserAnalysisError";
  }
}

export function abortError(): DOMException {
  return new DOMException("Analysis cancelled", "AbortError");
}

export function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) {
    throw abortError();
  }
}

export function sanitizeError(error: unknown): {
  errorType: string;
  errorMessage: string;
} {
  const type = error instanceof Error ? error.name : "DetectorError";
  const original =
    error instanceof Error ? error.message : "Detector execution failed";
  const containsSensitiveMarker =
    /[\\/]|%2f|%5c|(?:^|[^a-z0-9])[a-z]:(?=\S)|https?:|file:|blob:|(?:api[_-]?key|token|secret|password|authorization|bearer|credential|signature)\b/i.test(
      original,
    );
  const matchesSafeDiagnostic =
    /^[A-Za-z0-9][A-Za-z0-9 .,:;()_-]{0,159}$/.test(original);
  return {
    errorType: type.replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 80) || "Error",
    errorMessage:
      !containsSensitiveMarker && matchesSafeDiagnostic
        ? original
        : "Detector execution failed",
  };
}
