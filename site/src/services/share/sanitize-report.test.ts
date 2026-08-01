import { describe, expect, it } from "vitest";

import { createDemoReport } from "../../data/demo-report";
import {
  evidenceSelectionId,
  sanitizeReportForShare,
} from "./sanitize-report";

describe("sanitizeReportForShare", () => {
  it("removes identifying fields, prompt, unsafe references, cache data, and unselected evidence", () => {
    const report = structuredClone(createDemoReport("en"));
    report.title = "C:\\Users\\private\\secret-video.mp4";
    report.metadata.filename = "secret-video.mp4";
    report.prompt = "private generation prompt";
    report.warnings.push("Source secret-video.mp4 used a browser decoder.");
    report.runtime = {
      ...report.runtime,
      cache_path: "C:\\Users\\private\\.cache",
      frame_cache: {
        src: "blob:https://videoscope.invalid/private",
      },
      diagnostics: {
        safe_summary: "observable values only",
        unsafe_path: "/Users/private/evidence.png",
        alternate_path: "/srv/videoscope/private.bin",
      },
    } as typeof report.runtime;
    report.findings[0].evidence[0].thumbnail = {
      src: "data:image/jpeg;base64,AAAA",
      width: 160,
      height: 90,
    };
    report.findings[0].evidence.push({
      evidence_type: "frame",
      timestamp_seconds: 3.7,
      description: "unselected evidence",
      thumbnail: {
        src: "file:///Users/private/frame.jpg",
        width: 160,
        height: 90,
      },
      metadata: { local_path: "/Users/private/frame.jpg" },
    });

    const selectedEvidence = new Set([
      evidenceSelectionId(report.findings[0].id, 0),
    ]);
    const sanitized = sanitizeReportForShare(report, {
      includePrompt: false,
      selectedEvidence,
    });
    const serialized = JSON.stringify(sanitized);

    expect(sanitized).not.toHaveProperty("title");
    expect(sanitized).not.toHaveProperty("prompt");
    expect(sanitized.metadata).not.toHaveProperty("filename");
    expect(sanitized.findings[0].evidence).toHaveLength(1);
    expect(sanitized.findings[0].evidence[0]).not.toHaveProperty("thumbnail");
    expect(sanitized.runtime).not.toHaveProperty("cache_path");
    expect(sanitized.runtime).not.toHaveProperty("frame_cache");
    expect(sanitized.runtime).toMatchObject({
      diagnostics: { safe_summary: "observable values only" },
    });
    expect(serialized).not.toMatch(
      /secret-video|private generation|blob:|data:|file:|[a-z]:\\|\/Users\/|\/srv\//i,
    );
  });

  it("includes only a user-supplied title and separately opted-in prompt", () => {
    const report = createDemoReport("zh-CN");
    const sanitized = sanitizeReportForShare(report, {
      includePrompt: true,
      reportTitle: "公开复核报告",
      selectedEvidence: new Set(),
    });

    expect(sanitized.title).toBe("公开复核报告");
    expect(sanitized.prompt).toBe(report.prompt);
    expect(sanitized.metadata).not.toHaveProperty("filename");
    expect(
      sanitized.findings.every((finding) => finding.evidence.length === 0),
    ).toBe(true);
  });

  it("drops a supplied title or prompt when it contains a local reference", () => {
    const report = createDemoReport("en");
    report.prompt = "See file:///Users/private/source.mov";

    const sanitized = sanitizeReportForShare(report, {
      includePrompt: true,
      reportTitle: "C:\\Users\\private\\report",
      selectedEvidence: new Set(),
    });

    expect(sanitized).not.toHaveProperty("title");
    expect(sanitized).not.toHaveProperty("prompt");
  });

  it("removes derived filename tokens, identifiers, all URL schemes, and camel-case caches", () => {
    const report = structuredClone(createDemoReport("en"));
    report.metadata.filename = "My Secret Clip.mp4";
    report.warnings.push("Generated from my secret clip");
    report.findings[0].id = "my-secret-clip-finding";
    report.configuration[0].detector_id = "My Secret Clip detector";
    report.metrics[0].description = "ftp://files.example/private";
    report.runtime = {
      ...report.runtime,
      cachePath: "/srv/cache",
      objectUrl: "blob:https://example.test/value",
      nestedCacheStats: { hits: 10 },
      contacts: [
        "ws://socket.example",
        "mailto:private@example.test",
        "//cdn.example/private",
      ],
    } as typeof report.runtime;

    const sanitized = sanitizeReportForShare(report, {
      includePrompt: false,
      selectedEvidence: new Set(),
    });
    const serialized = JSON.stringify(sanitized);

    expect(serialized).not.toMatch(
      /my secret clip|my-secret-clip|ftp:|ws:|mailto:|\/\/cdn|cachePath|objectUrl|nestedCache/i,
    );
  });

  it("removes punctuation-normalized filename variants and bare web domains", () => {
    const report = structuredClone(createDemoReport("en"));
    report.metadata.filename = "Secret.Final-Cut_v2.mp4";
    report.warnings.push("Source secret final cut v2 was decoded.");
    report.metrics[0].description = "Inspect www.example.com/private";
    report.findings[0].description = "Mirror example.org/review";

    const serialized = JSON.stringify(
      sanitizeReportForShare(report, {
        includePrompt: false,
        selectedEvidence: new Set(),
      }),
    );

    expect(serialized).not.toMatch(
      /secret[ ._-]*final[ ._-]*cut[ ._-]*v2|www\.example|example\.org/i,
    );
  });
});
