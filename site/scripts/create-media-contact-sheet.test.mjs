import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { describe, expect, it } from "vitest";

import {
  contactSheetArguments,
  createMediaContactSheet,
} from "./create-media-contact-sheet.mjs";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.join(scriptDirectory, "create-media-contact-sheet.mjs");
const siteDirectory = path.resolve(scriptDirectory, "..");
const mediaDirectory = path.join(siteDirectory, "public", "media");
const reviewDirectory = path.resolve(siteDirectory, "..", "runs", "media-review");
const outputPath = path.join(reviewDirectory, "contact-sheet.webp");
const posters = Object.freeze([
  "hero-optical.webp",
  "city-nightlife.webp",
  "upload-liquid.webp",
  "diagnosis-fashion.webp",
  "compare-hills.webp",
  "compare-sunrise.webp",
  "evidence-lake.webp",
  "evidence-city.webp",
  "evidence-studio.webp",
].map((filename) => path.join(mediaDirectory, filename)));
const execFileAsync = promisify(execFile);

const layout = "0_0|640_0|1280_0|0_360|640_360|1280_360|0_720|640_720|1280_720";
const filterGraph = [
  "[0:v]scale=640:360:flags=lanczos[tile0]",
  "[1:v]scale=640:360:flags=lanczos[tile1]",
  "[2:v]scale=640:360:flags=lanczos[tile2]",
  "[3:v]scale=640:360:flags=lanczos[tile3]",
  "[4:v]scale=640:360:flags=lanczos[tile4]",
  "[5:v]scale=640:360:flags=lanczos[tile5]",
  "[6:v]scale=640:360:flags=lanczos[tile6]",
  "[7:v]scale=640:360:flags=lanczos[tile7]",
  "[8:v]scale=640:360:flags=lanczos[tile8]",
  `[tile0][tile1][tile2][tile3][tile4][tile5][tile6][tile7][tile8]xstack=inputs=9:layout=${layout}[sheet]`,
].join(";");

describe("media review contact sheet", () => {
  it("builds a deterministic nine-poster 1920x1080 WebP command", () => {
    expect(contactSheetArguments(posters, outputPath)).toEqual([
      "-hide_banner", "-loglevel", "error", "-y",
      "-i", posters[0],
      "-i", posters[1],
      "-i", posters[2],
      "-i", posters[3],
      "-i", posters[4],
      "-i", posters[5],
      "-i", posters[6],
      "-i", posters[7],
      "-i", posters[8],
      "-filter_complex", filterGraph,
      "-map", "[sheet]",
      "-frames:v", "1",
      "-an",
      "-map_metadata", "-1",
      "-c:v", "libwebp",
      "-compression_level", "6",
      "-q:v", "82",
      "-s:v", "1920x1080",
      outputPath,
    ]);
  });

  it.each([
    ["a missing manifest poster", posters.slice(0, 8)],
    ["a duplicate manifest poster", [...posters.slice(0, 8), posters[0]]],
    ["a non-manifest poster", [...posters.slice(0, 8), path.join(mediaDirectory, "unlisted.webp")]],
    ["manifest posters from a foreign directory", posters.map((poster) => path.join(reviewDirectory, path.basename(poster)))],
    ["manifest poster URLs", posters.map((poster) => `https://example.invalid/${path.basename(poster)}`)],
    ["manifest posters out of order", [posters[1], posters[0], ...posters.slice(2)]],
  ])("rejects %s", (_label, invalidPosters) => {
    expect(() => contactSheetArguments(invalidPosters, outputPath)).toThrow(
      /nine unique manifest poster files/u,
    );
  });

  it("uses an argument-array process call and logs only the relative artifact path", async () => {
    const calls = [];
    const messages = [];

    await createMediaContactSheet({
      ffmpeg: "ffmpeg",
      runner: async (file, args, options) => {
        calls.push({ file, args, options });
      },
      output: { write(message) { messages.push(message); } },
    });

    expect(calls).toEqual([{
      file: "ffmpeg",
      args: contactSheetArguments(posters, outputPath),
      options: {
        shell: false,
        windowsHide: true,
        timeout: 60_000,
        maxBuffer: 64 * 1024,
      },
    }]);
    expect(messages).toEqual([
      "Created media review contact sheet: runs/media-review/contact-sheet.webp\n",
    ]);
    expect(messages.join("")).not.toContain(reviewDirectory);
  });

  it("reports an unavailable FFmpeg executable without leaking the contact-sheet path", async () => {
    let caught;

    try {
      await createMediaContactSheet({
        runner: async () => {
          const error = new Error(`spawn failed for ${outputPath}`);
          error.code = "ENOENT";
          throw error;
        },
        output: { write() {} },
      });
    } catch (error) {
      caught = error;
    }

    expect(caught.message).toBe("FFmpeg was not found for the media review.");
    expect(caught.message).not.toContain(outputPath);
    expect(caught.cause).toBeUndefined();
  });

  it("recognizes the killed SIGTERM shape emitted by an execFile timeout", async () => {
    await expect(createMediaContactSheet({
      runner: async () => {
        throw Object.assign(new Error(`timed out for ${outputPath}`), {
          code: null,
          killed: true,
          signal: "SIGTERM",
        });
      },
      output: { write() {} },
    })).rejects.toThrow(
      "FFmpeg timed out after 60 seconds while rendering the media review.",
    );
  });

  it("prints only a neutral error when the direct CLI cannot start FFmpeg", async () => {
    const temporaryDirectory = await mkdtemp(
      path.join(os.tmpdir(), "videoscope-media-review-cli-"),
    );
    const missingFfmpeg = path.join(temporaryDirectory, "missing-ffmpeg.exe");
    let failure;

    try {
      await execFileAsync(process.execPath, [scriptPath], {
        cwd: siteDirectory,
        env: { ...process.env, FFMPEG_PATH: missingFfmpeg },
        windowsHide: true,
      });
    } catch (error) {
      failure = error;
    } finally {
      await rm(temporaryDirectory, { force: true, recursive: true });
    }

    expect(failure?.code).toBe(1);
    expect(failure?.stderr).toBe(
      "FFmpeg was not found for the media review.\n",
    );
    expect(failure?.stderr).not.toContain(temporaryDirectory);
    expect(failure?.stderr).not.toContain(siteDirectory);
    expect(failure?.stderr).not.toContain("file://");
  });
});
