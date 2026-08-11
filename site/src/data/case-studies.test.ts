import { describe, expect, it } from "vitest";

import { caseStudyManifest, validateCaseStudyManifest } from "./case-studies";
import validCase from "./test-fixtures/valid-case-study.json";

describe("case study manifest", () => {
  it("starts as a valid versioned manifest", () => {
    expect(caseStudyManifest.schemaVersion).toBe(1);
    expect(caseStudyManifest.cases).toEqual([]);
  });

  it("accepts one complete safe case record", () => {
    const manifest = validateCaseStudyManifest({
      schemaVersion: 1,
      generatedBy: "test",
      cases: [validCase],
    });

    expect(manifest.cases[0]?.comparison).toEqual({
      startSeconds: 2,
      endSeconds: 8,
    });
  });

  it("rejects a path outside the public case root", () => {
    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [
          {
            ...validCase,
            assets: {
              ...validCase.assets,
              beforeVideo: "C:\\private\\input.mp4",
            },
          },
        ],
      }),
    ).toThrow("Case assets must use safe /VideoScope/cases paths");
  });

  it("rejects duplicate identifiers, invalid hashes, and unknown fields", () => {
    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [validCase, { ...validCase, slug: "another-case" }],
      }),
    ).toThrow("Case IDs and slugs must be unique");

    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [validCase, { ...validCase, id: "another-case-id" }],
      }),
    ).toThrow("Case IDs and slugs must be unique");

    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [
          {
            ...validCase,
            sha256: { ...validCase.sha256, beforeVideo: "not-a-sha256" },
          },
        ],
      }),
    ).toThrow("Case hashes must be lowercase SHA-256 values");

    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [{ ...validCase, unrecognized: true }],
      }),
    ).toThrow("Case study manifest contains unknown keys");
  });

  it("rejects incomplete comparison and publication safety records", () => {
    for (const comparison of [
      { startSeconds: -1, endSeconds: 2 },
      { startSeconds: 2, endSeconds: 2 },
      { startSeconds: 8, endSeconds: 2 },
      { startSeconds: 2, endSeconds: 10.1 },
    ]) {
      expect(() =>
        validateCaseStudyManifest({
          schemaVersion: 1,
          generatedBy: "test",
          cases: [{ ...validCase, comparison }],
        }),
      ).toThrow("Case comparison must be a positive range within the media duration");
    }

    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [
          {
            ...validCase,
            featured: true,
            verification: { ...validCase.verification, status: "partial" },
          },
        ],
      }),
    ).toThrow("Featured cases must have completed verification");
  });

  it("rejects empty bilingual copy and synthetic cases that claim real users", () => {
    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [{ ...validCase, title: { ...validCase.title, en: "" } }],
      }),
    ).toThrow("Case bilingual copy must be non-empty");

    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [
          {
            ...validCase,
            provenance: "synthetic-regression",
            summary: { ...validCase.summary, en: "A real user supplied this clip." },
          },
        ],
      }),
    ).toThrow("Synthetic regression cases cannot claim real-user provenance");
  });

  it("rejects URL and traversal case asset paths", () => {
    for (const beforeVideo of [
      "https://example.com/case.mp4",
      "/VideoScope/cases/../private/case.mp4",
    ]) {
      expect(() =>
        validateCaseStudyManifest({
          schemaVersion: 1,
          generatedBy: "test",
          cases: [{ ...validCase, assets: { ...validCase.assets, beforeVideo } }],
        }),
      ).toThrow("Case assets must use safe /VideoScope/cases paths");
    }
  });

  it("rejects nested unknown keys", () => {
    expect(() =>
      validateCaseStudyManifest({
        schemaVersion: 1,
        generatedBy: "test",
        cases: [
          {
            ...validCase,
            verification: {
              ...validCase.verification,
              checks: [
                {
                  ...validCase.verification.checks[0],
                  unexpectedCheckField: true,
                },
              ],
            },
          },
        ],
      }),
    ).toThrow("Case study manifest contains unknown keys");
  });

  it("rejects records that are not plain JSON objects", () => {
    class FixtureParameters {
      readonly enabled = true;
    }

    for (const parameters of [
      new Date(),
      new FixtureParameters(),
      Object.create({ inherited: "value" }),
    ]) {
      expect(() =>
        validateCaseStudyManifest({
          schemaVersion: 1,
          generatedBy: "test",
          cases: [
            {
              ...validCase,
              actions: [{ ...validCase.actions[0], parameters }],
            },
          ],
        }),
      ).toThrow("Case action parameters must be a plain JSON object");
    }
  });

  it("requires manifest keys to be own properties", () => {
    Object.defineProperty(Object.prototype, "generatedBy", {
      configurable: true,
      value: "test",
    });
    try {
      expect(() =>
        validateCaseStudyManifest({ schemaVersion: 1, cases: [validCase] }),
      ).toThrow("Case study manifest has missing keys");
    } finally {
      delete (Object.prototype as { generatedBy?: string }).generatedBy;
    }
  });

  it("copies scalar records so later input mutation cannot change a validated manifest", () => {
    const parameters: Record<string, unknown> = { mode: "fixture" };
    const manifest = validateCaseStudyManifest({
      schemaVersion: 1,
      generatedBy: "test",
      cases: [
        {
          ...validCase,
          actions: [{ ...validCase.actions[0], parameters }],
        },
      ],
    });

    parameters.mode = { altered: true };

    expect(manifest.cases[0]?.actions[0]?.parameters).toEqual({ mode: "fixture" });
  });
});
