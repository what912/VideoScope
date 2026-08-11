import { describe, expect, it } from "vitest";

import {
  loadPublicFunnelCopy,
  validatePublicFunnelCopy,
} from "./growth-copy-runtime";

function validCopy() {
  const page = {
    eyebrow: "Eyebrow",
    title: "Title",
    description: "Description",
    action: "Action",
  };
  const locale = {
    positioning: "Positioning",
    sourcePreserved: "Source preserved",
    localBoundary: "Local boundary",
    caseEvidence: {
      provenance: "Provenance",
      source: "Source",
      actions: "Actions",
      verification: "Verification",
      verificationStatus: "Verification status",
      limitations: "Limitations",
    },
    pages: {
      rescue: page,
      examples: page,
      caseStudy: page,
      download: page,
      developers: page,
      roadmap: page,
      community: page,
      missingCase: page,
    },
    home: {
      uploadAtmosphere: "Upload atmosphere",
      hero: {
        eyebrow: "Hero eyebrow",
        quickCheck: "Quick check",
        examples: "Examples",
        media: "Hero media",
        primaryAction: "Primary action",
      },
      finalCta: {
        eyebrow: "Final eyebrow",
        title: "Final title",
        description: "Final description",
        action: "Final action",
      },
      cases: {
        loading: "Loading cases",
        unavailable: "Cases unavailable",
        casesEyebrow: "Cases eyebrow",
        casesTitle: "Cases title",
        casesAction: "Cases action",
      },
      comparison: {
        eyebrow: "Comparison eyebrow",
        before: "Before",
        after: "After",
        position: "Position",
        play: "Play",
        pause: "Pause",
        authored: "Authored",
        limitations: "Limitations",
        verification: "Verification",
        range: "Range",
      },
      funnel: {
        journeyEyebrow: "Journey eyebrow",
        journeyTitle: "Journey title",
        journey: [
          { title: "Rescue", description: "Rescue description" },
          { title: "Review", description: "Review description" },
          { title: "Publish", description: "Publish description" },
        ],
        boundaryTitle: "Boundary title",
        boundaryDescription: "Boundary description",
        developerTitle: "Developer title",
        developerDescription: "Developer description",
        developerAction: "Developer action",
        star: "Star",
      },
    },
  };
  return {
    en: locale,
    "zh-CN": structuredClone(locale),
  };
}

describe("public funnel copy runtime", () => {
  it("loads a complete same-origin local asset through the Vite base", async () => {
    const copy = validCopy();
    const request: typeof fetch = async (url: URL | RequestInfo) => {
      expect(String(url)).toBe("/VideoScope/growth-home-copy.json");
      return { ok: true, json: async () => copy } as Response;
    };

    await expect(loadPublicFunnelCopy(request, "/VideoScope/")).resolves.toEqual(copy);
  });

  it("rejects malformed copy instead of accepting an incomplete locale", () => {
    const copy = validCopy();
    delete (copy["zh-CN"].home.funnel as Record<string, unknown>).star;

    expect(() => validatePublicFunnelCopy(copy)).toThrow(
      "Public funnel copy has missing keys at zh-CN.home.funnel",
    );
  });

  it("reports an unavailable local asset without producing fallback funnel copy", async () => {
    const request = async () => ({ ok: false, json: async () => validCopy() } as Response);

    await expect(loadPublicFunnelCopy(request)).rejects.toThrow(
      "The local public funnel copy could not be loaded.",
    );
  });

  it("requires English and Simplified Chinese to expose the same copy shape", () => {
    const copy = validCopy();
    delete (copy["zh-CN"].home.comparison as Record<string, unknown>).pause;

    expect(() => validatePublicFunnelCopy(copy)).toThrow(
      "Public funnel copy has missing keys at zh-CN.home.comparison",
    );
  });
});
