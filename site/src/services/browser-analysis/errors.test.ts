import { describe, expect, it } from "vitest";

import { sanitizeError } from "./errors";

describe("detector error sanitization", () => {
  it.each([
    "failed at /tmp/private clip.mp4 line 4",
    "failed at /private/var/folders/User Name/clip.mp4",
    "failed at C:\\Users\\User Name\\secret clip.mp4",
    "failed at C:private-clip.mp4",
    "failed (C:private.mp4)",
    "failed [D:private.mp4]",
    "failed 'E:private.mp4'",
    "failed:F:private.mp4",
    "failed.G:private.mp4",
    "failed at \\\\server\\Private Share\\secret clip.mp4",
    "https://example.test/video.mp4?TOKEN=SecretValue",
    "Authorization: Bearer TopSecret",
    "api_KEY=TopSecret",
  ])("redacts path or secret-bearing message: %s", (message) => {
    const sanitized = sanitizeError(new Error(message)).errorMessage;

    expect(sanitized).toBe("Detector execution failed");
    expect(sanitized).not.toMatch(
      /tmp|private|users|server|token|secret|bearer|api_key/i,
    );
  });

  it("preserves a short allowlisted diagnostic without path markers", () => {
    expect(sanitizeError(new Error("Numeric series is empty")).errorMessage).toBe(
      "Numeric series is empty",
    );
  });

  it.each([
    "Phase A: failed safely",
    "Ratio 1:2 is unsupported",
    "Drive C: is unavailable",
  ])("preserves a safe non-path colon diagnostic: %s", (message) => {
    expect(sanitizeError(new Error(message)).errorMessage).toBe(message);
  });
});
