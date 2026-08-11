import { Buffer } from "node:buffer";
import {
  mkdtemp,
  readFile,
  rm,
  truncate,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  probeMedia,
  validateProbe,
  verifyOriginalMedia,
} from "./original-media-verifier.mjs";

const temporaryDirectories = [];

const roleScenes = [
  ["hero", "optical-aperture"],
  ["product-proof", "night-observation-grid"],
  ["upload-lab", "fluid-spectrum"],
  ["diagnosis", "diagnostic-mesh"],
  ["compare-a", "cool-topography"],
  ["compare-b", "dawn-spectrum"],
  ["evidence-a", "cyan-caustic"],
  ["evidence-b", "violet-lattice"],
  ["evidence-c", "amber-contour"],
];

const videoItem = Object.freeze({
  role: "product-proof",
  scene: "night-observation-grid",
  workingWidth: 1280,
  workingHeight: 720,
  outputWidth: 1280,
  outputHeight: 720,
  frameRate: 24,
  durationSeconds: 6,
  video: "product-proof.mp4",
  poster: "product-proof.webp",
  posterTimestampSeconds: 2,
});

const validVideoProbe = {
  streams: [{
    codec_name: "h264", pix_fmt: "yuv420p", width: 1280, height: 720,
    avg_frame_rate: "24/1", duration: "6.000000",
  }],
  format: { duration: "6.000000" },
};

const validPosterProbe = {
  streams: [{ codec_name: "webp", width: 1280, height: 720 }],
  format: { duration: "0.000000" },
};

function manifest() {
  return {
    schemaVersion: 1,
    generatorVersion: "1.0.0",
    license: "Apache-2.0 project-authored media",
    items: roleScenes.map(([role, scene], index) => {
      const base = {
        role,
        scene,
        workingWidth: 1280,
        workingHeight: 720,
        outputWidth: 1280,
        outputHeight: 720,
        frameRate: 24,
        poster: `${role}.webp`,
      };
      if (index < 6) {
        return {
          ...base,
          durationSeconds: index === 0 ? 8 : 6,
          video: `${role}.mp4`,
          posterTimestampSeconds: 2,
        };
      }
      return { ...base, stillTimeSeconds: 2 };
    }),
  };
}

function provenanceFor(currentManifest) {
  const roleSceneLines = currentManifest.items
    .map((item) => `- \`${item.role}\`: \`${item.scene}\``)
    .join("\n");
  return [
    "# Project-authored VideoScope media",
    "",
    "The nine scenes below are authored as deterministic FFmpeg filter graphs in `site/scripts/original-scenes.mjs`.",
    `They were generated under version ${currentManifest.generatorVersion}, licensed with the repository under ${currentManifest.license}, contain no external media input, and are not endorsements by FFmpeg or any third party.`,
    "",
    "## Role and scene declarations",
    "",
    roleSceneLines,
    "",
  ].join("\n");
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

async function temporaryDirectory() {
  const directory = await mkdtemp(path.join(tmpdir(), "original-media-verifier-"));
  temporaryDirectories.push(directory);
  return directory;
}

async function writeSignature(filePath, kind) {
  const signature = kind === "mp4"
    ? Buffer.from("0000ftypisom", "ascii")
    : Buffer.from("RIFF0000WEBP", "ascii");
  await writeFile(filePath, signature);
}

async function writeValidMedia(directory, currentManifest) {
  await Promise.all(currentManifest.items.flatMap((item) => [
    ...(item.video === undefined
      ? []
      : [writeSignature(path.join(directory, item.video), "mp4")]),
    writeSignature(path.join(directory, item.poster), "webp"),
  ]));
  await writeFile(
    path.join(directory, "PROVENANCE.md"),
    provenanceFor(currentManifest),
    "utf8",
  );
}

async function useWindowsLineEndings(directory) {
  const provenancePath = path.join(directory, "PROVENANCE.md");
  const provenance = await readFile(provenancePath, "utf8");
  await writeFile(provenancePath, provenance.replace(/\n/gu, "\r\n"), "utf8");
}

function probeRunner(_file, args) {
  const target = args.at(-1);
  const videoProbe = clone(validVideoProbe);
  if (target.endsWith("hero.mp4")) {
    videoProbe.streams[0].duration = "8.000000";
    videoProbe.format.duration = "8.000000";
  }
  return {
    stdout: JSON.stringify(target.endsWith(".mp4") ? videoProbe : validPosterProbe),
  };
}

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map(
    (directory) => rm(directory, { force: true, recursive: true }),
  ));
});

describe("original media probe validation", () => {
  it("accepts a manifest-matching H.264 render", () => {
    expect(() => validateProbe(videoItem, validVideoProbe, "video")).not.toThrow();
  });

  it.each([
    ["codec_name", "hevc"], ["pix_fmt", "yuv444p"],
    ["width", 1920], ["height", 1080], ["avg_frame_rate", "30/1"],
    ["avg_frame_rate", "48/2"],
  ])("rejects mismatched %s", (field, value) => {
    const probe = clone(validVideoProbe);
    probe.streams[0][field] = value;
    expect(() => validateProbe(videoItem, probe, "video")).toThrow(/does not match manifest/u);
  });

  it("accepts exactly one frame of duration error and rejects a larger frame error", () => {
    const withinOneFrame = clone(validVideoProbe);
    withinOneFrame.streams[0].duration = String(
      videoItem.durationSeconds + 1 / videoItem.frameRate,
    );
    expect(() => validateProbe(videoItem, withinOneFrame, "video")).not.toThrow();

    const beyondOneFrame = clone(validVideoProbe);
    beyondOneFrame.streams[0].duration = String(
      videoItem.durationSeconds + 1 / videoItem.frameRate + 0.0001,
    );
    expect(() => validateProbe(videoItem, beyondOneFrame, "video")).toThrow(/does not match manifest/u);
  });

  it("rejects a missing video stream and malformed frame-rate rational", () => {
    expect(() => validateProbe(videoItem, { streams: [], format: {} }, "video"))
      .toThrow(/missing a video stream/u);
    const malformed = clone(validVideoProbe);
    malformed.streams[0].avg_frame_rate = "24/0";
    expect(() => validateProbe(videoItem, malformed, "video"))
      .toThrow(/malformed frame-rate/u);
  });

  it("validates a WebP poster's codec and manifest dimensions", () => {
    expect(() => validateProbe(videoItem, validPosterProbe, "poster")).not.toThrow();
    const wrongDimensions = clone(validPosterProbe);
    wrongDimensions.streams[0].height = 1080;
    expect(() => validateProbe(videoItem, wrongDimensions, "poster"))
      .toThrow(/does not match manifest/u);
  });

  it("runs ffprobe with argument-array safety and parses its JSON", async () => {
    const calls = [];
    const directory = await temporaryDirectory();
    const filePath = path.join(directory, "clip.mp4");
    const probe = await probeMedia(filePath, async (file, args, options) => {
      calls.push({ file, args, options });
      return { stdout: JSON.stringify(validVideoProbe) };
    });

    expect(probe).toEqual(validVideoProbe);
    expect(calls).toEqual([{
      file: "ffprobe",
      args: [
        "-v", "error", "-show_entries",
        "stream=codec_name,pix_fmt,width,height,avg_frame_rate,duration:format=duration",
        "-of", "json", filePath,
      ],
      options: {
        shell: false,
        windowsHide: true,
        timeout: 30_000,
        maxBuffer: 64 * 1024,
      },
    }]);
  });

  it("reports ffprobe timeouts and removes absolute paths from failures", async () => {
    const directory = await temporaryDirectory();
    const filePath = path.join(directory, "media directory", "clip.mp4");
    await expect(probeMedia(filePath, async () => {
      const error = new Error("process timed out");
      error.code = "ETIMEDOUT";
      throw error;
    })).rejects.toThrow(/ffprobe timed out/u);

    let caught;
    try {
      await probeMedia(filePath, async () => {
        const error = new Error("process failed");
        error.stderr = `${"x".repeat(2_100)} "C:\\private media\\clip.mp4" tail`;
        throw error;
      });
    } catch (error) {
      caught = error;
    }
    expect(caught.message).toMatch(/tail$/u);
    expect(caught.message).not.toMatch(/[A-Za-z]:\\|private media|media directory/u);
    expect(caught.message.length).toBeLessThanOrEqual(2_100);
  });

  it.each([
    ["Windows drive path", "C:\\Users\\姓名\\Media Folder\\clip.mp4", ["C:\\", "姓名", "Media Folder"]],
    ["POSIX file path", "/home/private-user/secret-video/clip.mp4", ["/home", "private-user", "secret-video"]],
    ["UNC path", "\\\\server\\private-share\\secret-user\\clip.mp4", ["\\\\server", "private-share", "secret-user"]],
  ])("removes %s and media-directory paths from ffprobe failures", async (
    _label,
    absolutePath,
    forbiddenFragments,
  ) => {
    const directory = await temporaryDirectory();
    const mediaDirectory = path.join(directory, "媒体 directory");
    const filePath = path.join(mediaDirectory, "clip.mp4");
    let caught;

    try {
      await probeMedia(filePath, async () => {
        const error = new Error("process failed");
        error.stderr = `input=${absolutePath}; media=${mediaDirectory};`;
        throw error;
      });
    } catch (error) {
      caught = error;
    }

    expect(caught.message).toContain("[path]");
    expect(caught.message).not.toContain(mediaDirectory);
    for (const fragment of forbiddenFragments) {
      expect(caught.message).not.toContain(fragment);
    }
  });
});

describe("original media file and provenance verification", () => {
  it("verifies nine unique project-authored declarations and their files", async () => {
    const directory = await temporaryDirectory();
    const currentManifest = manifest();
    await writeValidMedia(directory, currentManifest);

    await expect(verifyOriginalMedia({
      manifest: currentManifest,
      mediaDirectory: directory,
      runner: probeRunner,
    })).resolves.toBeUndefined();
  });

  it("accepts an otherwise exact provenance file with Windows line endings", async () => {
    const directory = await temporaryDirectory();
    const currentManifest = manifest();
    await writeValidMedia(directory, currentManifest);
    await useWindowsLineEndings(directory);

    await expect(verifyOriginalMedia({
      manifest: currentManifest,
      mediaDirectory: directory,
      runner: probeRunner,
    })).resolves.toBeUndefined();
  });

  it.each([
    ["workingWidth", 1920],
    ["workingHeight", 1080],
    ["outputWidth", 1920],
    ["outputHeight", 1080],
    ["frameRate", 30],
  ])("rejects manifest %s=%s before probing files", async (field, value) => {
    const directory = await temporaryDirectory();
    const currentManifest = manifest();
    currentManifest.items[0][field] = value;
    let runnerCalls = 0;

    await expect(verifyOriginalMedia({
      manifest: currentManifest,
      mediaDirectory: directory,
      runner: async () => {
        runnerCalls += 1;
        return { stdout: JSON.stringify(validVideoProbe) };
      },
    })).rejects.toThrow(/generation settings/u);

    expect(runnerCalls).toBe(0);
  });

  it("rejects duplicate files, invalid signatures, and over-budget media", async () => {
    const directory = await temporaryDirectory();
    const currentManifest = manifest();
    await writeValidMedia(directory, currentManifest);

    currentManifest.items[1].poster = currentManifest.items[0].poster;
    await expect(verifyOriginalMedia({
      manifest: currentManifest,
      mediaDirectory: directory,
      runner: probeRunner,
    })).rejects.toThrow(/reused/u);

    const signatureManifest = manifest();
    await writeFile(path.join(directory, signatureManifest.items[0].video), "not an mp4");
    await expect(verifyOriginalMedia({
      manifest: signatureManifest,
      mediaDirectory: directory,
      runner: probeRunner,
    })).rejects.toThrow(/not a valid mp4/u);

    await writeSignature(path.join(directory, signatureManifest.items[0].video), "mp4");
    await truncate(path.join(directory, signatureManifest.items[0].video), 4 * 1024 * 1024 + 1);
    await expect(verifyOriginalMedia({
      manifest: signatureManifest,
      mediaDirectory: directory,
      runner: probeRunner,
    })).rejects.toThrow(/exceeds 4 MiB/u);

    await writeSignature(path.join(directory, signatureManifest.items[0].video), "mp4");
    await truncate(path.join(directory, signatureManifest.items[0].poster), 350 * 1024 + 1);
    await expect(verifyOriginalMedia({
      manifest: signatureManifest,
      mediaDirectory: directory,
      runner: probeRunner,
    })).rejects.toThrow(/exceeds 350 KiB/u);
  });

  it("requires exact provenance for manifest version, license, and role-scene pairs", async () => {
    const directory = await temporaryDirectory();
    const currentManifest = manifest();
    await writeValidMedia(directory, currentManifest);
    const provenancePath = path.join(directory, "PROVENANCE.md");

    await writeFile(provenancePath, (await readFile(provenancePath, "utf8")).replace(
      "night-observation-grid",
      "incorrect-scene",
    ));
    await expect(verifyOriginalMedia({
      manifest: currentManifest,
      mediaDirectory: directory,
      runner: probeRunner,
    })).rejects.toThrow(/PROVENANCE.md does not exactly match/u);
  });
});
