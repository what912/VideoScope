import { execFile } from "node:child_process";
import { mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import mediaManifest from "../public/media/media-sources.json" with { type: "json" };

import { validateManifest } from "./media-safety.mjs";

const execFileAsync = promisify(execFile);
const contactSheetTimeoutMilliseconds = 60_000;
const contactSheetOutputFilename = "contact-sheet.webp";
const contactSheetRelativePath = "runs/media-review/contact-sheet.webp";
const tileWidth = 640;
const tileHeight = 360;
const contactSheetWidth = 1920;
const contactSheetHeight = 1080;
const contactSheetLayout = "0_0|640_0|1280_0|0_360|640_360|1280_360|0_720|640_720|1280_720";
const ffmpegMissingMessage = "FFmpeg was not found for the media review.";
const ffmpegTimeoutMessage = "FFmpeg timed out after 60 seconds while rendering the media review.";
const ffmpegFailureMessage = "FFmpeg failed while rendering the media review.";
const mediaReviewFailureMessage = "Media review failed.";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const siteDirectory = path.resolve(scriptDirectory, "..");
const repositoryDirectory = path.resolve(siteDirectory, "..");
const defaultMediaDirectory = path.join(siteDirectory, "public", "media");
const defaultManifestPath = path.join(defaultMediaDirectory, "media-sources.json");
const defaultReviewDirectory = path.join(repositoryDirectory, "runs", "media-review");

validateManifest(mediaManifest);
const approvedPosterFilenames = Object.freeze(
  mediaManifest.items.map(({ poster }) => poster),
);
const approvedPosterPaths = Object.freeze(
  approvedPosterFilenames.map((poster) => path.join(defaultMediaDirectory, poster)),
);
const safeCliMessages = new Set([
  ffmpegMissingMessage,
  ffmpegTimeoutMessage,
  ffmpegFailureMessage,
]);

function hasExactlyApprovedPosters(posters) {
  return Array.isArray(posters)
    && posters.length === approvedPosterPaths.length
    && posters.every((poster, index) => poster === approvedPosterPaths[index]);
}

function isTimeoutError(error) {
  return error?.code === "ETIMEDOUT"
    || error?.killed === true
    || error?.signal === "SIGTERM";
}

function userFacingCliMessage(error) {
  return error instanceof Error && safeCliMessages.has(error.message)
    ? error.message
    : mediaReviewFailureMessage;
}

function contactSheetFilterGraph() {
  const scaledInputs = approvedPosterFilenames.map(
    (_poster, index) => `[${index}:v]scale=${tileWidth}:${tileHeight}:flags=lanczos[tile${index}]`,
  );
  const stackedInputs = approvedPosterFilenames.map(
    (_poster, index) => `[tile${index}]`,
  ).join("");
  return [
    ...scaledInputs,
    `${stackedInputs}xstack=inputs=9:layout=${contactSheetLayout}[sheet]`,
  ].join(";");
}

export function contactSheetArguments(posters, output) {
  if (!hasExactlyApprovedPosters(posters)) {
    throw new Error("Contact sheet requires nine unique manifest poster files");
  }
  if (typeof output !== "string" || output.length === 0) {
    throw new TypeError("Contact sheet output must be a non-empty string");
  }

  return [
    "-hide_banner", "-loglevel", "error", "-y",
    ...posters.flatMap((poster) => ["-i", poster]),
    "-filter_complex", contactSheetFilterGraph(),
    "-map", "[sheet]",
    "-frames:v", "1",
    "-an",
    "-map_metadata", "-1",
    "-c:v", "libwebp",
    "-compression_level", "6",
    "-q:v", "82",
    "-s:v", `${contactSheetWidth}x${contactSheetHeight}`,
    output,
  ];
}

export async function createMediaContactSheet({
  manifestPath = defaultManifestPath,
  mediaDirectory = defaultMediaDirectory,
  reviewDirectory = defaultReviewDirectory,
  runner = execFileAsync,
  ffmpeg = process.env.FFMPEG_PATH || "ffmpeg",
  output = process.stdout,
} = {}) {
  if (typeof runner !== "function") {
    throw new TypeError("Contact sheet process runner must be callable");
  }
  if (typeof ffmpeg !== "string" || ffmpeg.length === 0) {
    throw new TypeError("FFmpeg executable must be a non-empty string");
  }

  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  validateManifest(manifest);
  const posters = manifest.items.map(({ poster }) => path.join(mediaDirectory, poster));
  const destination = path.join(reviewDirectory, contactSheetOutputFilename);
  const args = contactSheetArguments(posters, destination);

  await mkdir(reviewDirectory, { recursive: true });
  try {
    await runner(ffmpeg, args, {
      shell: false,
      windowsHide: true,
      timeout: contactSheetTimeoutMilliseconds,
      maxBuffer: 64 * 1024,
    });
  } catch (error) {
    if (error?.code === "ENOENT") {
      throw new Error(ffmpegMissingMessage);
    }
    if (isTimeoutError(error)) {
      throw new Error(ffmpegTimeoutMessage);
    }
    throw new Error(ffmpegFailureMessage);
  }

  if (typeof output?.write === "function") {
    output.write(`Created media review contact sheet: ${contactSheetRelativePath}\n`);
  }
  return destination;
}

function isDirectExecution() {
  return process.argv[1] !== undefined
    && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}

export async function runMediaContactSheetCli({ stderr = process.stderr } = {}) {
  try {
    await createMediaContactSheet();
    return 0;
  } catch (error) {
    if (typeof stderr?.write === "function") {
      stderr.write(`${userFacingCliMessage(error)}\n`);
    }
    return 1;
  }
}

if (isDirectExecution()) {
  process.exitCode = await runMediaContactSheetCli();
}
