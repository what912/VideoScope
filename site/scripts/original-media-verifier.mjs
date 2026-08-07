import { Buffer } from "node:buffer";
import { execFile } from "node:child_process";
import { open, readFile, stat } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const ffprobeArguments = [
  "-v", "error", "-show_entries",
  "stream=codec_name,pix_fmt,width,height,avg_frame_rate,duration:format=duration",
  "-of", "json",
];
const ffprobeOptions = Object.freeze({
  shell: false,
  windowsHide: true,
  timeout: 30_000,
  maxBuffer: 64 * 1024,
});
const maximumVideoBytes = 4 * 1024 * 1024;
const maximumPosterBytes = 350 * 1024;
const maximumDurationFrameError = 1;
const frameErrorSignificantDigits = 12;
const expectedRoleScenes = Object.freeze([
  ["hero", "optical-aperture"],
  ["product-proof", "night-observation-grid"],
  ["upload-lab", "fluid-spectrum"],
  ["diagnosis", "diagnostic-mesh"],
  ["compare-a", "cool-topography"],
  ["compare-b", "dawn-spectrum"],
  ["evidence-a", "cyan-caustic"],
  ["evidence-b", "violet-lattice"],
  ["evidence-c", "amber-contour"],
]);
const generatedLicense = "Apache-2.0 project-authored media";

function requireObject(value, message) {
  if (typeof value !== "object" || value === null) {
    throw new Error(message);
  }
  return value;
}

function parseFrameRate(value) {
  if (typeof value !== "string" || !/^\d+\/\d+$/u.test(value)) {
    throw new Error("Video probe has a malformed frame-rate rational");
  }
  const [numerator, denominator] = value.split("/").map(Number);
  if (numerator <= 0 || denominator <= 0) {
    throw new Error("Video probe has a malformed frame-rate rational");
  }
  return numerator / denominator;
}

function finiteDuration(value) {
  const duration = Number(value);
  return Number.isFinite(duration) && duration >= 0 ? duration : undefined;
}

function probeStream(probe, kind) {
  const source = requireObject(probe, `${kind} probe is invalid`);
  if (!Array.isArray(source.streams)) {
    throw new Error(`${kind} probe is missing a video stream`);
  }
  const stream = source.streams.find((candidate) =>
    typeof candidate === "object" && candidate !== null &&
    Number.isFinite(Number(candidate.width)) &&
    Number.isFinite(Number(candidate.height)),
  );
  if (stream === undefined) {
    throw new Error(`${kind} probe is missing a video stream`);
  }
  return stream;
}

function assertMatchesManifest(actual, expected, field, kind) {
  if (actual !== expected) {
    throw new Error(`${kind} ${field} does not match manifest`);
  }
}

function originalProvenance(manifest) {
  const roleSceneLines = manifest.items
    .map((item) => `- \`${item.role}\`: \`${item.scene}\``)
    .join("\n");
  return [
    "# Project-authored VideoScope media",
    "",
    "The nine scenes below are authored as deterministic FFmpeg filter graphs in `site/scripts/original-scenes.mjs`.",
    `They were generated under version ${manifest.generatorVersion}, licensed with the repository under ${manifest.license}, contain no external media input, and are not endorsements by FFmpeg or any third party.`,
    "",
    "## Role and scene declarations",
    "",
    roleSceneLines,
    "",
  ].join("\n");
}

function assertLocalFilename(filename, extension) {
  if (
    typeof filename !== "string" ||
    path.basename(filename) !== filename ||
    path.extname(filename).toLowerCase() !== extension
  ) {
    throw new Error(`Invalid local media filename: ${String(filename)}`);
  }
}

function assertOriginalManifest(manifest) {
  requireObject(manifest, "Original media manifest is invalid");
  if (
    manifest.schemaVersion !== 1 ||
    manifest.generatorVersion !== "1.0.0" ||
    manifest.license !== generatedLicense ||
    !Array.isArray(manifest.items) ||
    manifest.items.length !== expectedRoleScenes.length
  ) {
    throw new Error("Original media manifest does not define the approved declarations");
  }

  const filenames = new Set();
  manifest.items.forEach((item, index) => {
    requireObject(item, "Original media item is invalid");
    const [role, scene] = expectedRoleScenes[index];
    if (item.role !== role || item.scene !== scene) {
      throw new Error("Original media role and scene declarations do not match");
    }
    if (
      item.workingWidth !== 1280 ||
      item.workingHeight !== 720 ||
      item.outputWidth !== 1280 ||
      item.outputHeight !== 720 ||
      item.frameRate !== 24
    ) {
      throw new Error("Original media generation settings do not match contract");
    }
    assertLocalFilename(item.poster, ".webp");
    if (filenames.has(item.poster)) {
      throw new Error(`Media filename is reused: ${item.poster}`);
    }
    filenames.add(item.poster);

    if (index < 6) {
      assertLocalFilename(item.video, ".mp4");
      if (filenames.has(item.video)) {
        throw new Error(`Media filename is reused: ${item.video}`);
      }
      filenames.add(item.video);
    } else if (item.video !== undefined) {
      throw new Error(`Evidence role ${item.role} must not declare a video`);
    }
  });
}

async function requireFile(filePath) {
  try {
    const details = await stat(filePath);
    if (!details.isFile()) {
      throw new Error("not a file");
    }
    return details;
  } catch {
    throw new Error(`Required media file is missing: ${path.basename(filePath)}`);
  }
}

async function assertSignature(filePath, type) {
  const handle = await open(filePath, "r");
  try {
    const header = Buffer.alloc(12);
    const { bytesRead } = await handle.read(header, 0, header.length, 0);
    const valid = bytesRead === header.length && (
      type === "mp4"
        ? header.toString("ascii", 4, 8) === "ftyp"
        : header.toString("ascii", 0, 4) === "RIFF" &&
          header.toString("ascii", 8, 12) === "WEBP"
    );
    if (!valid) {
      throw new Error(`${path.basename(filePath)} is not a valid ${type} file`);
    }
  } finally {
    await handle.close();
  }
}

function sanitizeStderr(value, filePath) {
  let detail = String(value ?? "").slice(-2_000);
  for (const candidate of [filePath, path.dirname(filePath)]) {
    detail = detail.replaceAll(candidate, "[path]");
    detail = detail.replaceAll(candidate.replaceAll("\\", "/"), "[path]");
  }
  return detail
    .replace(/[A-Za-z]:[\\/][^"\r\n]*/gu, "[path]")
    .replace(/\\\\[^\r\n]*/gu, "[path]")
    .replace(/(^|[^A-Za-z0-9])\/[^\r\n]*/gmu, "$1[path]")
    .replace(/\s+/gu, " ")
    .trim();
}

function probeError(error, filePath) {
  if (error?.code === "ETIMEDOUT" || error?.killed === true) {
    return new Error("ffprobe timed out after 30 seconds");
  }
  if (error?.code === "ENOENT") {
    return new Error("ffprobe was not found");
  }
  const detail = sanitizeStderr(error?.stderr ?? error?.message, filePath);
  return new Error(detail === "" ? "ffprobe failed" : `ffprobe failed: ${detail}`);
}

export async function probeMedia(filePath, runner = execFileAsync) {
  try {
    const result = await runner("ffprobe", [...ffprobeArguments, filePath], ffprobeOptions);
    return JSON.parse(String(result.stdout));
  } catch (error) {
    throw probeError(error, filePath);
  }
}

export function validateProbe(item, probe, kind) {
  requireObject(item, "Original media item is invalid");
  const stream = probeStream(probe, kind);
  assertMatchesManifest(stream.width, item.outputWidth, "width", kind);
  assertMatchesManifest(stream.height, item.outputHeight, "height", kind);

  if (kind === "poster") {
    assertMatchesManifest(stream.codec_name, "webp", "codec_name", kind);
    return;
  }
  if (kind !== "video") {
    throw new Error(`Unsupported probe kind: ${String(kind)}`);
  }

  assertMatchesManifest(stream.codec_name, "h264", "codec_name", kind);
  assertMatchesManifest(stream.pix_fmt, "yuv420p", "pix_fmt", kind);
  const frameRate = parseFrameRate(stream.avg_frame_rate);
  assertMatchesManifest(stream.avg_frame_rate, `${item.frameRate}/1`, "avg_frame_rate", kind);
  assertMatchesManifest(frameRate, item.frameRate, "avg_frame_rate", kind);
  const duration = finiteDuration(stream.duration) ?? finiteDuration(probe.format?.duration);
  if (duration === undefined || !Number.isFinite(item.durationSeconds)) {
    throw new Error("video duration does not match manifest");
  }
  const frameError = Number(
    Math.abs((duration - item.durationSeconds) * frameRate)
      .toPrecision(frameErrorSignificantDigits),
  );
  if (frameError > maximumDurationFrameError) {
    throw new Error("video duration does not match manifest");
  }
}

async function verifyFile(item, filename, kind, mediaDirectory, runner) {
  const filePath = path.join(mediaDirectory, filename);
  const details = await requireFile(filePath);
  const budget = kind === "video" ? maximumVideoBytes : maximumPosterBytes;
  if (details.size > budget) {
    throw new Error(`${filename} exceeds ${kind === "video" ? "4 MiB" : "350 KiB"}`);
  }
  await assertSignature(filePath, kind === "video" ? "mp4" : "webp");
  validateProbe(item, await probeMedia(filePath, runner), kind);
}

export async function verifyOriginalMedia({ manifest, mediaDirectory, runner } = {}) {
  assertOriginalManifest(manifest);
  if (typeof mediaDirectory !== "string") {
    throw new Error("Original media directory is invalid");
  }

  const provenancePath = path.join(mediaDirectory, "PROVENANCE.md");
  const provenance = await readFile(provenancePath, "utf8").catch(() => {
    throw new Error("Required media file is missing: PROVENANCE.md");
  });
  if (provenance !== originalProvenance(manifest)) {
    throw new Error("PROVENANCE.md does not exactly match the original media manifest");
  }

  for (const item of manifest.items) {
    if (item.video !== undefined) {
      await verifyFile(item, item.video, "video", mediaDirectory, runner);
    }
    await verifyFile(item, item.poster, "poster", mediaDirectory, runner);
  }
}
