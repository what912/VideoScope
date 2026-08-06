import { describe, expect, it } from "vitest";
import { rescueStatusText, rescueText } from "./rescueI18n";

describe("rescueText", () => {
  it("keeps the creator mark invariant across supported locales", () => {
    expect(rescueText("creator", "en")).toBe("what912");
    expect(rescueText("creator", "zh-CN")).toBe("what912");
  });

  it("labels needs-review as a distinct Rescue lifecycle status", () => {
    expect(rescueStatusText("needs_review", "en")).toBe("Needs review");
  });
});
