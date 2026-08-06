import { describe, expect, it } from "vitest";
import { contentStatusText, contentText } from "./contentI18n";

describe("useful-content translations", () => {
  it("keeps the creator attribution invariant", () => {
    expect(contentText("creator", "en")).toBe("what912");
    expect(contentText("creator", "zh-CN")).toBe("what912");
  });

  it("provides distinct bilingual outcome and status labels", () => {
    expect(contentText("selected_clips", "en")).toBe("Selected Clips");
    expect(contentText("selected_clips", "zh-CN")).toBe("精选片段");
    expect(contentStatusText("needs_review", "zh-CN")).toBe("需要复核");
  });
});
