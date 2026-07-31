import { describe, expect, it } from "vitest";

import {
  MAX_LOCAL_VIDEO_BYTES,
  validateLocalVideoSelection,
} from "./validation";

describe("validateLocalVideoSelection", () => {
  it("accepts a supported non-empty video with a CPU detector selected", () => {
    const file = new File(["video"], "clip.mp4", { type: "video/mp4" });

    expect(
      validateLocalVideoSelection(file, ["near_black"]),
    ).toBeNull();
  });

  it.each(["clip.MP4", "clip.mov", "clip.MkV", "clip.webm"])(
    "accepts %s when the browser leaves MIME empty",
    (filename) => {
      const file = new File(["video"], filename, { type: "" });

      expect(
        validateLocalVideoSelection(file, ["near_black"]),
      ).toBeNull();
    },
  );

  it("rejects an empty MIME file outside the constrained extension allowlist", () => {
    const file = new File(["video"], "clip.avi", { type: "" });

    expect(validateLocalVideoSelection(file, ["near_black"])).toBe(
      "unsupported_type",
    );
  });

  it("does not let a supported extension override a conflicting MIME type", () => {
    const file = new File(["text"], "clip.mp4", { type: "text/plain" });

    expect(validateLocalVideoSelection(file, ["near_black"])).toBe(
      "unsupported_type",
    );
  });

  it("rejects unsupported media types", () => {
    const file = new File(["text"], "notes.txt", { type: "text/plain" });

    expect(validateLocalVideoSelection(file, ["near_black"])).toBe(
      "unsupported_type",
    );
  });

  it("rejects files over the 500 MiB public limit", () => {
    const file = new File(["x"], "large.mp4", { type: "video/mp4" });
    Object.defineProperty(file, "size", {
      configurable: true,
      value: MAX_LOCAL_VIDEO_BYTES + 1,
    });

    expect(validateLocalVideoSelection(file, ["near_black"])).toBe(
      "file_too_large",
    );
  });

  it("requires at least one CPU detector", () => {
    const file = new File(["video"], "clip.webm", { type: "video/webm" });

    expect(validateLocalVideoSelection(file, [])).toBe(
      "no_detectors_selected",
    );
  });
});
