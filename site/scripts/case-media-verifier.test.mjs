import { createHash } from "node:crypto";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  validateCaseManifest,
  verifyCaseMedia,
} from "./case-media-verifier.mjs";
import { auditBuiltCaseOutputs } from "./verify-media.mjs";

const temporaryDirectories = [];
const CASE_SLUG = "demonstration-case";
const CASE_ROOT = `/VideoScope/cases/${CASE_SLUG}`;
const comparison = { startSeconds: 2, endSeconds: 8 };
const media = { durationSeconds: 12, width: 640, height: 360, frameRate: 24 };
const MP4_FORMAT_NAME = "mov,mp4,m4a,3gp,3g2,mj2";

function sha256(contents) {
  return createHash("sha256").update(contents).digest("hex");
}

function validManifest(overrides = {}) {
  const assets = {
    beforeVideo: `${CASE_ROOT}/before.mp4`,
    afterVideo: `${CASE_ROOT}/after.mp4`,
    poster: `${CASE_ROOT}/poster.webp`,
    publicReport: `${CASE_ROOT}/public-report.json`,
  };
  const hashes = {
    beforeVideo: sha256("before video bytes"),
    afterVideo: sha256("after video bytes"),
    poster: sha256("poster image bytes"),
  };
  const report = JSON.stringify({ output_sha256: hashes });
  return {
    schemaVersion: 1,
    generatedBy: "scripts/generate_growth_cases.py",
    cases: [{
      id: "growth-demonstration-case-v1",
      slug: CASE_SLUG,
      comparison,
      media,
      assets,
      sha256: { ...hashes, publicReport: sha256(report) },
      ...overrides,
    }],
  };
}

async function temporaryCaseDirectory() {
  const directory = await mkdtemp(path.join(os.tmpdir(), "videoscope case verifier "));
  temporaryDirectories.push(directory);
  const caseDirectory = path.join(directory, CASE_SLUG);
  await mkdir(caseDirectory, { recursive: true });
  await Promise.all([
    writeFile(path.join(caseDirectory, "before.mp4"), "before video bytes"),
    writeFile(path.join(caseDirectory, "after.mp4"), "after video bytes"),
    writeFile(path.join(caseDirectory, "poster.webp"), "poster image bytes"),
    writeFile(path.join(caseDirectory, "public-report.json"), JSON.stringify({
      output_sha256: {
        beforeVideo: sha256("before video bytes"),
        afterVideo: sha256("after video bytes"),
        poster: sha256("poster image bytes"),
      },
    })),
    writeFile(path.join(directory, "PROVENANCE.md"), "Project-authored case provenance.\n"),
  ]);
  return directory;
}

async function temporaryBuiltCaseDirectory({ unexpected = false } = {}) {
  const directory = await mkdtemp(path.join(os.tmpdir(), "videoscope deployed cases "));
  temporaryDirectories.push(directory);
  const caseDirectory = path.join(directory, "cases", CASE_SLUG);
  await mkdir(caseDirectory, { recursive: true });
  await Promise.all([
    writeFile(path.join(caseDirectory, "before.mp4"), "before"),
    writeFile(path.join(caseDirectory, "after.mp4"), "after"),
    writeFile(path.join(caseDirectory, "poster.webp"), "poster"),
    writeFile(path.join(caseDirectory, "public-report.json"), "report"),
    ...(unexpected ? [writeFile(path.join(directory, "cases", "PROVENANCE.md"), "audit record")] : []),
  ]);
  return directory;
}

function mediaProbe({
  duration = 6,
  width = 640,
  height = 360,
  frameRate = "24/1",
  includeAudio = true,
  audioCodec = "aac",
  formatName = MP4_FORMAT_NAME,
} = {}) {
  return {
    streams: [
      {
        codec_type: "video",
        codec_name: "h264",
        pix_fmt: "yuv420p",
        width,
        height,
        avg_frame_rate: frameRate,
      },
      ...(includeAudio ? [{ codec_type: "audio", codec_name: audioCodec }] : []),
    ],
    format: { duration: String(duration), format_name: formatName },
  };
}

function mediaProbeForArgs(args, overrides = {}) {
  const probe = mediaProbe(overrides);
  const selectorIndex = args.indexOf("-select_streams");
  const selector = selectorIndex === -1 ? undefined : args[selectorIndex + 1];
  if (selector === "v" || selector === "v:0") {
    return { ...probe, streams: probe.streams.filter((stream) => stream.codec_type === "video") };
  }
  return probe;
}

async function fakeProbe(_executable, args) {
  if (args.includes("-show_frames")) {
    const interval = args[args.indexOf("-read_intervals") + 1];
    const timestamp = interval === "0%+#1" ? 0 : 6 - 1 / 24;
    return { stdout: JSON.stringify({ frames: [{ best_effort_timestamp_time: String(timestamp) }] }) };
  }
  if (String(args.at(-1)).endsWith("poster.webp")) {
    return {
      stdout: JSON.stringify({
        streams: [{ codec_type: "video", codec_name: "webp", width: 640, height: 360, avg_frame_rate: "0/1" }],
        format: { duration: "0", format_name: "webp_pipe" },
      }),
    };
  }
  return { stdout: JSON.stringify(mediaProbeForArgs(args)) };
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) => rm(directory, { force: true, recursive: true })),
  );
});

describe("case media manifest validation", () => {
  it("rejects paths outside the canonical public case root", () => {
    const manifest = validManifest({
      assets: {
        ...validManifest().cases[0].assets,
        beforeVideo: "/VideoScope/private/before.mp4",
      },
    });

    expect(() => validateCaseManifest(manifest)).toThrow(
      "Case media manifest contains an unsafe asset path.",
    );
  });

  it("rejects a manifest comparison that extends beyond its declared source media", () => {
    const manifest = validManifest({ comparison: { startSeconds: 2, endSeconds: 13 } });

    expect(() => validateCaseManifest(manifest)).toThrow(
      "Case comparison range is outside the declared media duration.",
    );
  });
});

describe("case media verification", () => {
  it("verifies exactly the files and media declared by a canonical case manifest", async () => {
    const directory = await temporaryCaseDirectory();

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: fakeProbe }))
      .resolves.toEqual({ caseCount: 1, fileCount: 5 });
  });

  it("rejects a hash mismatch without exposing the absolute directory", async () => {
    const directory = await temporaryCaseDirectory();

    let caught;
    try {
      await verifyCaseMedia({
        manifest: validManifest({
          sha256: { ...validManifest().cases[0].sha256, afterVideo: "0".repeat(64) },
        }),
        directory,
        runner: fakeProbe,
      });
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(Error);
    expect(caught.message).toBe("Case media hash does not match the manifest.");
    expect(caught.message).not.toContain(directory);
  });

  it("rejects different before and after durations", async () => {
    const directory = await temporaryCaseDirectory();
    const mismatchedDurationProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) return fakeProbe(_executable, args);
      const isAfter = String(args.at(-1)).endsWith("after.mp4");
      return { stdout: JSON.stringify(mediaProbeForArgs(args, { duration: isAfter ? 5 : 6 })) };
    };

    await expect(verifyCaseMedia({
      manifest: validManifest(), directory, runner: mismatchedDurationProbe,
    })).rejects.toThrow("Case comparison media must cover the same duration.");
  });

  it("accepts one reported frame of before-after duration drift", async () => {
    const directory = await temporaryCaseDirectory();
    const oneFrameDriftProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) {
        const interval = args[args.indexOf("-read_intervals") + 1];
        const duration = String(args.at(-1)).endsWith("after.mp4") ? 6.041667 : 6;
        return {
          stdout: JSON.stringify({
            frames: [{ best_effort_timestamp_time: String(interval === "0%+#1" ? 0 : duration - 1 / 24) }],
          }),
        };
      }
      if (String(args.at(-1)).endsWith("poster.webp")) return fakeProbe(_executable, args);
      const isAfter = String(args.at(-1)).endsWith("after.mp4");
      return { stdout: JSON.stringify(mediaProbeForArgs(args, { duration: isAfter ? 6.041667 : 6 })) };
    };

    await expect(verifyCaseMedia({
      manifest: validManifest(), directory, runner: oneFrameDriftProbe,
    })).resolves.toEqual({ caseCount: 1, fileCount: 5 });
  });

  it("uses declared scale-and-pad output dimensions for a vertical after comparison", async () => {
    const directory = await temporaryCaseDirectory();
    const manifest = validManifest({
      verification: {
        checks: [{
          checkId: "vertical-scale-and-pad",
          measured: { publicOutputWidth: 406, publicOutputHeight: 720 },
        }],
      },
    });
    const verticalAfterProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) return fakeProbe(_executable, args);
      if (String(args.at(-1)).endsWith("poster.webp")) {
        return {
          stdout: JSON.stringify({
            streams: [{ codec_type: "video", codec_name: "webp", width: 406, height: 720, avg_frame_rate: "0/0" }],
            format: { duration: "0", format_name: "webp_pipe" },
          }),
        };
      }
      const isAfter = String(args.at(-1)).endsWith("after.mp4");
      return {
        stdout: JSON.stringify(mediaProbeForArgs(args, {
          width: isAfter ? 406 : 640,
          height: isAfter ? 720 : 360,
        })),
      };
    };

    await expect(verifyCaseMedia({ manifest, directory, runner: verticalAfterProbe }))
      .resolves.toEqual({ caseCount: 1, fileCount: 5 });
  });

  it("uses declared scale-and-pad output dimensions for a vertical case poster", async () => {
    const directory = await temporaryCaseDirectory();
    const manifest = validManifest({
      verification: {
        checks: [{
          checkId: "vertical-scale-and-pad",
          measured: { publicOutputWidth: 406, publicOutputHeight: 720 },
        }],
      },
    });
    const verticalPosterProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) return fakeProbe(_executable, args);
      if (String(args.at(-1)).endsWith("poster.webp")) {
        return {
          stdout: JSON.stringify({
            streams: [{ codec_type: "video", codec_name: "webp", width: 406, height: 720, avg_frame_rate: "0/0" }],
            format: { duration: "0", format_name: "webp_pipe" },
          }),
        };
      }
      const isAfter = String(args.at(-1)).endsWith("after.mp4");
      return {
        stdout: JSON.stringify(mediaProbeForArgs(args, {
          width: isAfter ? 406 : 640,
          height: isAfter ? 720 : 360,
        })),
      };
    };

    await expect(verifyCaseMedia({ manifest, directory, runner: verticalPosterProbe }))
      .resolves.toEqual({ caseCount: 1, fileCount: 5 });
  });

  it("rejects an undeclared public case file", async () => {
    const directory = await temporaryCaseDirectory();
    await writeFile(path.join(directory, CASE_SLUG, "draft.mp4"), "not declared");

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: fakeProbe }))
      .rejects.toThrow("Public case directory contains an undeclared file.");
  });

  it("rejects unsupported comparison video metadata", async () => {
    const directory = await temporaryCaseDirectory();
    const unsupportedProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) return fakeProbe(_executable, args);
      return {
        stdout: JSON.stringify(mediaProbeForArgs(args, { width: 1281, frameRate: "31/1" })),
      };
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: unsupportedProbe }))
      .rejects.toThrow("Case comparison video exceeds the public media limits.");
  });

  it("rejects comparison media without an audio stream", async () => {
    const directory = await temporaryCaseDirectory();
    const missingAudioProbe = async (_executable, args) => {
      if (args.includes("-show_frames") || String(args.at(-1)).endsWith("poster.webp")) {
        return fakeProbe(_executable, args);
      }
      return { stdout: JSON.stringify(mediaProbeForArgs(args, { includeAudio: false })) };
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: missingAudioProbe }))
      .rejects.toThrow("Case comparison video has an unsupported format.");
  });

  it("rejects comparison media with a non-AAC audio stream", async () => {
    const directory = await temporaryCaseDirectory();
    const nonAacProbe = async (_executable, args) => {
      if (args.includes("-show_frames") || String(args.at(-1)).endsWith("poster.webp")) {
        return fakeProbe(_executable, args);
      }
      return { stdout: JSON.stringify(mediaProbeForArgs(args, { audioCodec: "opus" })) };
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: nonAacProbe }))
      .rejects.toThrow("Case comparison video has an unsupported format.");
  });

  it("rejects comparison media in a non-MP4 container and requests the container inventory", async () => {
    const directory = await temporaryCaseDirectory();
    const calls = [];
    const nonMp4Probe = async (_executable, args) => {
      calls.push(args);
      if (args.includes("-show_frames") || String(args.at(-1)).endsWith("poster.webp")) {
        return fakeProbe(_executable, args);
      }
      return { stdout: JSON.stringify(mediaProbeForArgs(args, { formatName: "matroska,webm" })) };
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: nonMp4Probe }))
      .rejects.toThrow("Case comparison video has an unsupported format.");
    expect(calls.some((args) => {
      const entries = args[args.indexOf("-show_entries") + 1];
      return typeof entries === "string" && entries.includes("format=format_name") &&
        !args.includes("-select_streams");
    })).toBe(true);
  });

  it("rejects comparison media and its declared range when they exceed 25 seconds", async () => {
    const directory = await temporaryCaseDirectory();
    const manifest = validManifest({
      comparison: { startSeconds: 1, endSeconds: 27 },
      media: { ...media, durationSeconds: 30 },
    });
    const longDurationProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) {
        const interval = args[args.indexOf("-read_intervals") + 1];
        return {
          stdout: JSON.stringify({
            frames: [{ best_effort_timestamp_time: interval === "0%+#1" ? "0" : String(26 - 1 / 24) }],
          }),
        };
      }
      if (String(args.at(-1)).endsWith("poster.webp")) return fakeProbe(_executable, args);
      return { stdout: JSON.stringify(mediaProbeForArgs(args, { duration: 26 })) };
    };

    await expect(verifyCaseMedia({ manifest, directory, runner: longDurationProbe }))
      .rejects.toThrow("Case comparison video exceeds the public media limits.");
  });

  it("accepts a WebP poster that has no video frame rate", async () => {
    const directory = await temporaryCaseDirectory();
    const imageProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) return fakeProbe(_executable, args);
      if (String(args.at(-1)).endsWith("poster.webp")) {
        return {
          stdout: JSON.stringify({
            streams: [{ codec_type: "video", codec_name: "webp", width: 640, height: 360, avg_frame_rate: "0/0" }],
            format: { duration: "0", format_name: "webp_pipe" },
          }),
        };
      }
      return { stdout: JSON.stringify(mediaProbeForArgs(args)) };
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: imageProbe }))
      .resolves.toEqual({ caseCount: 1, fileCount: 5 });
  });

  it("rejects a public report that does not bind its declared comparison hashes", async () => {
    const directory = await temporaryCaseDirectory();
    await writeFile(path.join(directory, CASE_SLUG, "public-report.json"), JSON.stringify({
      output_sha256: { beforeVideo: "0".repeat(64), afterVideo: "0".repeat(64), poster: "0".repeat(64) },
    }));
    const manifest = validManifest({
      sha256: {
        ...validManifest().cases[0].sha256,
        publicReport: sha256(JSON.stringify({
          output_sha256: { beforeVideo: "0".repeat(64), afterVideo: "0".repeat(64), poster: "0".repeat(64) },
        })),
      },
    });

    await expect(verifyCaseMedia({ manifest, directory, runner: fakeProbe }))
      .rejects.toThrow("Public case report does not bind the declared media hashes.");
  });

  it("rejects a comparison video when its first or last frame is not decodable", async () => {
    const directory = await temporaryCaseDirectory();
    const missingLastFrameProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) return { stdout: JSON.stringify({ frames: [] }) };
      return { stdout: JSON.stringify(mediaProbeForArgs(args)) };
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: missingLastFrameProbe }))
      .rejects.toThrow("Case comparison video is not decodable at its boundary frames.");
  });

  it("probes an EOF-adjacent primary-video frame and requires its timestamp within one frame of the end", async () => {
    const directory = await temporaryCaseDirectory();
    const calls = [];
    const recordingProbe = async (executable, args, options) => {
      calls.push({ executable, args, options });
      return fakeProbe(executable, args, options);
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: recordingProbe }))
      .resolves.toEqual({ caseCount: 1, fileCount: 5 });

    const frameCalls = calls.filter(({ args }) => args.includes("-show_frames"));
    const endFrameCalls = frameCalls.filter(({ args }) =>
      args[args.indexOf("-read_intervals") + 1] !== "0%+#1",
    );
    expect(endFrameCalls).toHaveLength(2);
    for (const { args } of endFrameCalls) {
      expect(args).toContain("-select_streams");
      expect(args[args.indexOf("-select_streams") + 1]).toBe("v:0");
      const interval = args[args.indexOf("-read_intervals") + 1];
      const seekSeconds = Number(interval.slice(0, interval.indexOf("%")));
      expect(interval).toMatch(/%$/u);
      expect(Math.abs(seekSeconds - (6 - 1 / 24))).toBeLessThan(0.000001);
    }
  });

  it("rejects a nonempty early frame returned for the EOF-adjacent probe", async () => {
    const directory = await temporaryCaseDirectory();
    const earlyEndFrameProbe = async (_executable, args) => {
      if (args.includes("-show_frames")) {
        const interval = args[args.indexOf("-read_intervals") + 1];
        return {
          stdout: JSON.stringify({
            frames: [{ best_effort_timestamp_time: interval === "0%+#1" ? "0" : "0.5" }],
          }),
        };
      }
      return fakeProbe(_executable, args);
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: earlyEndFrameProbe }))
      .rejects.toThrow("Case comparison video is not decodable at its boundary frames.");
  });

  it("selects v:0 metadata even when an audio stream is listed first", async () => {
    const directory = await temporaryCaseDirectory();
    const calls = [];
    const audioFirstProbe = async (_executable, args) => {
      calls.push(args);
      if (args.includes("-show_frames")) return fakeProbe(_executable, args);
      if (String(args.at(-1)).endsWith("poster.webp")) return fakeProbe(_executable, args);
      const selectorIndex = args.indexOf("-select_streams");
      const selector = selectorIndex === -1 ? undefined : args[selectorIndex + 1];
      if (selector === "v" || selector === "v:0") {
        return {
          stdout: JSON.stringify({
            streams: [mediaProbe().streams[0]],
            format: { duration: "6", format_name: MP4_FORMAT_NAME },
          }),
        };
      }
      return {
        stdout: JSON.stringify({
          streams: [
            { codec_type: "audio", codec_name: "aac" },
            mediaProbe().streams[0],
          ],
          format: { duration: "6", format_name: MP4_FORMAT_NAME },
        }),
      };
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: audioFirstProbe }))
      .resolves.toEqual({ caseCount: 1, fileCount: 5 });

    const metadataCalls = calls.filter((args) => args.includes("-show_entries") &&
      args[args.indexOf("-show_entries") + 1].includes("avg_frame_rate"));
    expect(metadataCalls).not.toHaveLength(0);
    for (const args of metadataCalls) {
      expect(args).toContain("-select_streams");
      expect(args[args.indexOf("-select_streams") + 1]).toBe("v:0");
    }
  });

  it("rejects an asset with an additional nonconforming video stream", async () => {
    const directory = await temporaryCaseDirectory();
    const multiVideoProbe = async (_executable, args) => {
      if (args.includes("-show_frames") || String(args.at(-1)).endsWith("poster.webp")) {
        return fakeProbe(_executable, args);
      }
      const selectorIndex = args.indexOf("-select_streams");
      const selector = selectorIndex === -1 ? undefined : args[selectorIndex + 1];
      if (selector !== "v:0") {
        return {
          stdout: JSON.stringify({
            streams: [
              mediaProbe().streams[0],
              {
                codec_type: "video",
                codec_name: "vp9",
                pix_fmt: "yuv444p",
                width: 1920,
                height: 1080,
                avg_frame_rate: "60/1",
              },
              { codec_type: "audio", codec_name: "aac" },
            ],
            format: { duration: "6", format_name: MP4_FORMAT_NAME },
          }),
        };
      }
      return { stdout: JSON.stringify(mediaProbeForArgs(args)) };
    };

    await expect(verifyCaseMedia({ manifest: validManifest(), directory, runner: multiVideoProbe }))
      .rejects.toThrow("Case media asset must contain exactly one video stream.");
  });
});

describe("built case allowlist", () => {
  it("allows only case files declared by the canonical manifest in a production build", async () => {
    const directory = await temporaryBuiltCaseDirectory();

    await expect(auditBuiltCaseOutputs(directory, validManifest())).resolves.toEqual({ fileCount: 4 });
  });

  it("rejects build-time provenance metadata left in deployed case files", async () => {
    const directory = await temporaryBuiltCaseDirectory({ unexpected: true });

    await expect(auditBuiltCaseOutputs(directory, validManifest())).rejects
      .toThrow("Built case media does not match the exact manifest allowlist.");
  });
});
