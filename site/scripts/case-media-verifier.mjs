import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

const execFileAsync = promisify(execFile);
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const siteDirectory = path.resolve(scriptDirectory, "..");
const defaultDirectory = path.join(siteDirectory, "public", "cases");
const defaultManifestPath = path.join(siteDirectory, "src", "data", "case-studies.json");
const CASE_ASSET_PREFIX = "/VideoScope/cases/";
const SHA256 = /^[a-f0-9]{64}$/u;
const MAXIMUM_WIDTH = 1280;
const MAXIMUM_HEIGHT = 720;
const MAXIMUM_FRAME_RATE = 30;
const MAXIMUM_DURATION_SECONDS = 25;
const PROVENANCE_FILE = "PROVENANCE.md";
// ffprobe reports decimal durations, so preserve an inclusive one-frame bound.
const DURATION_COMPARISON_EPSILON_SECONDS = 0.000001;
const FRAME_TIMESTAMP_EPSILON_SECONDS = 0.000001;
const PROBE_OPTIONS = Object.freeze({
  shell: false,
  windowsHide: true,
  timeout: 30_000,
  maxBuffer: 64 * 1024,
});

function fail(message) {
  throw new Error(message);
}

function isPlainObject(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype;
}

function own(record, key) {
  return Object.prototype.hasOwnProperty.call(record, key);
}

function requireRecord(value, message) {
  if (!isPlainObject(value)) fail(message);
  return value;
}

function requireString(value, message) {
  if (typeof value !== "string" || value.length === 0) fail(message);
  return value;
}

function requireFiniteNumber(value, message) {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(message);
  return value;
}

function caseAssetRelativePath(value, slug) {
  const assetPath = requireString(value, "Case media manifest contains an unsafe asset path.");
  if (!assetPath.startsWith(CASE_ASSET_PREFIX)) {
    fail("Case media manifest contains an unsafe asset path.");
  }
  const relative = assetPath.slice(CASE_ASSET_PREFIX.length);
  const segments = relative.split("/");
  if (
    segments.length !== 2 ||
    segments[0] !== slug ||
    segments.some((segment) => !/^[A-Za-z0-9][A-Za-z0-9._-]*$/u.test(segment))
  ) {
    fail("Case media manifest contains an unsafe asset path.");
  }
  return relative;
}

function requireHash(value) {
  const hash = requireString(value, "Case media manifest contains an invalid SHA-256 hash.");
  if (!SHA256.test(hash)) fail("Case media manifest contains an invalid SHA-256 hash.");
  return hash;
}

function requireAssetShape(assets, slug) {
  const record = requireRecord(assets, "Case media manifest assets are invalid.");
  const expectedExtensions = {
    beforeVideo: ".mp4",
    afterVideo: ".mp4",
    poster: ".webp",
    publicReport: ".json",
  };
  const result = {};
  for (const [key, extension] of Object.entries(expectedExtensions)) {
    if (!own(record, key)) fail("Case media manifest assets are invalid.");
    const relative = caseAssetRelativePath(record[key], slug);
    if (!relative.endsWith(extension)) fail("Case media manifest contains an unsupported asset format.");
    result[key] = relative;
  }
  return result;
}

function requireHashShape(hashes) {
  const record = requireRecord(hashes, "Case media manifest hashes are invalid.");
  const result = {};
  for (const key of ["beforeVideo", "afterVideo", "poster", "publicReport"]) {
    if (!own(record, key)) fail("Case media manifest hashes are invalid.");
    result[key] = requireHash(record[key]);
  }
  return result;
}

function declaredAfterDimensions(record, fallback) {
  if (record.verification === undefined) return fallback;
  const verification = requireRecord(
    record.verification,
    "Case media manifest verification geometry is invalid.",
  );
  if (!Array.isArray(verification.checks)) {
    fail("Case media manifest verification geometry is invalid.");
  }
  const scaleAndPadChecks = verification.checks.filter(
    (check) => isPlainObject(check) && check.checkId === "vertical-scale-and-pad",
  );
  if (scaleAndPadChecks.length === 0) return fallback;
  if (scaleAndPadChecks.length !== 1) {
    fail("Case media manifest verification geometry is invalid.");
  }
  const measured = requireRecord(
    scaleAndPadChecks[0].measured,
    "Case media manifest verification geometry is invalid.",
  );
  const width = requireFiniteNumber(
    measured.publicOutputWidth,
    "Case media manifest verification geometry is invalid.",
  );
  const height = requireFiniteNumber(
    measured.publicOutputHeight,
    "Case media manifest verification geometry is invalid.",
  );
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
    fail("Case media manifest verification geometry is invalid.");
  }
  return { width, height };
}

function validateCase(item) {
  const record = requireRecord(item, "Case media manifest case is invalid.");
  const slug = requireString(record.slug, "Case media manifest case slug is invalid.");
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]*$/u.test(slug)) {
    fail("Case media manifest case slug is invalid.");
  }
  const id = requireString(record.id, "Case media manifest case ID is invalid.");
  const comparison = requireRecord(record.comparison, "Case comparison range is invalid.");
  const media = requireRecord(record.media, "Case media declaration is invalid.");
  const startSeconds = requireFiniteNumber(
    comparison.startSeconds,
    "Case comparison range is invalid.",
  );
  const endSeconds = requireFiniteNumber(
    comparison.endSeconds,
    "Case comparison range is invalid.",
  );
  const durationSeconds = requireFiniteNumber(media.durationSeconds, "Case media declaration is invalid.");
  const width = requireFiniteNumber(media.width, "Case media declaration is invalid.");
  const height = requireFiniteNumber(media.height, "Case media declaration is invalid.");
  const frameRate = requireFiniteNumber(media.frameRate, "Case media declaration is invalid.");
  if (
    startSeconds < 0 || endSeconds <= startSeconds || endSeconds > durationSeconds ||
    durationSeconds <= 0
  ) {
    fail("Case comparison range is outside the declared media duration.");
  }
  if (
    !Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0 ||
    frameRate <= 0
  ) {
    fail("Case media declaration is invalid.");
  }
  return {
    id,
    slug,
    comparison: { startSeconds, endSeconds },
    media: { durationSeconds, width, height, frameRate },
    afterDimensions: declaredAfterDimensions(record, { width, height }),
    assets: requireAssetShape(record.assets, slug),
    hashes: requireHashShape(record.sha256),
  };
}

export function validateCaseManifest(manifest) {
  const record = requireRecord(manifest, "Case media manifest is invalid.");
  if (record.schemaVersion !== 1 || !Array.isArray(record.cases)) {
    fail("Case media manifest is invalid.");
  }
  requireString(record.generatedBy, "Case media manifest is invalid.");
  const cases = record.cases.map(validateCase);
  const ids = new Set();
  const slugs = new Set();
  const files = new Set();
  for (const item of cases) {
    if (ids.has(item.id) || slugs.has(item.slug)) {
      fail("Case media manifest contains duplicate case identifiers.");
    }
    ids.add(item.id);
    slugs.add(item.slug);
    for (const file of Object.values(item.assets)) {
      if (files.has(file)) fail("Case media manifest reuses a public asset path.");
      files.add(file);
    }
  }
  if (cases.length === 0) fail("Case media manifest must declare at least one case.");
  return { cases };
}

function declaredCaseFiles(manifest) {
  const declared = new Set([PROVENANCE_FILE]);
  for (const item of manifest.cases) {
    for (const relativePath of Object.values(item.assets)) declared.add(relativePath);
  }
  return declared;
}

async function walkPublicFiles(directory, prefix = "") {
  let entries;
  try {
    entries = await readdir(path.join(directory, prefix), { withFileTypes: true });
  } catch {
    fail("Public case directory could not be read.");
  }
  const files = [];
  for (const entry of entries) {
    const relativePath = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isFile()) {
      files.push(relativePath);
    } else if (entry.isDirectory()) {
      files.push(...await walkPublicFiles(directory, relativePath));
    } else {
      fail("Public case directory contains an undeclared file.");
    }
  }
  return files;
}

async function assertExactFileAllowlist(directory, declared) {
  const files = await walkPublicFiles(directory);
  const found = new Set(files);
  if (
    files.some((file) => !declared.has(file)) ||
    [...declared].some((file) => !found.has(file)) ||
    files.length !== declared.size
  ) {
    fail("Public case directory contains an undeclared file.");
  }
}

async function sha256File(filePath) {
  let contents;
  try {
    contents = await readFile(filePath);
  } catch {
    fail("Case media file could not be read.");
  }
  return createHash("sha256").update(contents).digest("hex");
}

async function verifyHashBindings(item, directory) {
  for (const key of ["beforeVideo", "afterVideo", "poster", "publicReport"]) {
    const observed = await sha256File(path.join(directory, item.assets[key]));
    if (observed !== item.hashes[key]) {
      fail("Case media hash does not match the manifest.");
    }
  }
}

function createFfprobeRunner() {
  return (_requestedExecutable, args, options) => execFileAsync("ffprobe", args, options);
}

async function runProbe(filePath, runner, args) {
  let result;
  try {
    result = await runner("ffprobe", args, PROBE_OPTIONS);
  } catch {
    fail("Case media probe failed.");
  }
  try {
    return JSON.parse(result.stdout);
  } catch {
    fail("Case media probe returned invalid JSON.");
  }
}

async function probeMedia(filePath, runner) {
  // Public cases intentionally carry exactly one video stream; reject alternate
  // multiplexed variants instead of silently validating only their primary stream.
  const inventory = await runProbe(filePath, runner, [
    "-v", "error",
    "-show_entries", "stream=codec_type,codec_name:format=format_name",
    "-of", "json",
    filePath,
  ]);
  const videoStreams = Array.isArray(inventory.streams)
    ? inventory.streams.filter((stream) => stream?.codec_type === "video")
    : [];
  if (videoStreams.length !== 1) {
    fail("Case media asset must contain exactly one video stream.");
  }
  const primaryVideo = await runProbe(filePath, runner, [
    "-v", "error",
    "-select_streams", "v:0",
    "-show_entries", "stream=codec_type,codec_name,pix_fmt,width,height,avg_frame_rate:format=duration",
    "-of", "json",
    filePath,
  ]);
  return { inventory, primaryVideo };
}

function parseFrameRate(value) {
  if (typeof value !== "string" || !/^\d+(?:\.\d+)?\/\d+(?:\.\d+)?$/u.test(value)) {
    fail("Case media probe has an invalid frame rate.");
  }
  const [numerator, denominator] = value.split("/").map(Number);
  if (denominator === 0) fail("Case media probe has an invalid frame rate.");
  return numerator / denominator;
}

function videoMetadata(probe) {
  const streams = Array.isArray(probe?.primaryVideo?.streams)
    ? probe.primaryVideo.streams.filter((item) => item?.codec_type === "video")
    : [];
  const inventoryStreams = Array.isArray(probe?.inventory?.streams) ? probe.inventory.streams : [];
  const audioCodecs = inventoryStreams
    .filter((item) => item?.codec_type === "audio")
    .map((item) => item?.codec_name);
  const [stream] = streams;
  const duration = Number(probe?.primaryVideo?.format?.duration);
  if (streams.length !== 1 || !stream || !Number.isFinite(duration) || duration < 0) {
    fail("Case comparison video metadata is invalid.");
  }
  return {
    codec: stream.codec_name,
    pixelFormat: stream.pix_fmt,
    width: stream.width,
    height: stream.height,
    frameRate: parseFrameRate(stream.avg_frame_rate),
    duration,
    audioCodecs,
    containerFormat: probe?.inventory?.format?.format_name,
  };
}

function imageMetadata(probe) {
  const streams = Array.isArray(probe?.primaryVideo?.streams)
    ? probe.primaryVideo.streams.filter((item) => item?.codec_type === "video")
    : [];
  const [stream] = streams;
  if (
    streams.length !== 1 || !stream || !Number.isInteger(stream.width) ||
    !Number.isInteger(stream.height)
  ) {
    fail("Case poster metadata is invalid.");
  }
  return { codec: stream.codec_name, width: stream.width, height: stream.height };
}

function verifyComparisonMetadata(metadata, item, expectedDimensions) {
  const containerFormats = typeof metadata.containerFormat === "string"
    ? metadata.containerFormat.split(",").map((format) => format.trim().toLowerCase())
    : [];
  if (
    metadata.codec !== "h264" || metadata.pixelFormat !== "yuv420p" ||
    !containerFormats.includes("mp4") || metadata.audioCodecs.length === 0 ||
    metadata.audioCodecs.some((codec) => codec !== "aac")
  ) {
    fail("Case comparison video has an unsupported format.");
  }
  if (
    metadata.width > MAXIMUM_WIDTH || metadata.height > MAXIMUM_HEIGHT ||
    metadata.frameRate > MAXIMUM_FRAME_RATE
  ) {
    fail("Case comparison video exceeds the public media limits.");
  }
  if (
    metadata.width !== expectedDimensions.width ||
    metadata.height !== expectedDimensions.height
  ) {
    fail("Case comparison video dimensions do not match the manifest.");
  }
  if (Math.abs(metadata.frameRate - item.media.frameRate) > 0.001) {
    fail("Case comparison video frame rate does not match the manifest.");
  }
}

function frameTimestamp(probe) {
  if (!Array.isArray(probe?.frames) || probe.frames.length === 0) {
    fail("Case comparison video is not decodable at its boundary frames.");
  }
  const timestamps = probe.frames.map((frame) => Number(frame?.best_effort_timestamp_time));
  if (timestamps.some((timestamp) => !Number.isFinite(timestamp))) {
    fail("Case comparison video is not decodable at its boundary frames.");
  }
  return Math.max(...timestamps);
}

async function assertBoundaryFrames(filePath, runner, metadata) {
  const frameDuration = 1 / metadata.frameRate;
  const endSeek = Math.max(0, metadata.duration - frameDuration);
  const boundaries = [
    { interval: "0%+#1", isEnd: false },
    // ffprobe seeks to a preceding keyframe. Reading through EOF is required
    // to prove that an actual end-adjacent frame, not that keyframe, decodes.
    { interval: `${endSeek.toFixed(12)}%`, isEnd: true },
  ];
  for (const { interval, isEnd } of boundaries) {
    const probe = await runProbe(filePath, runner, [
      "-v", "error",
      "-select_streams", "v:0",
      "-read_intervals", interval,
      "-show_frames",
      "-show_entries", "frame=best_effort_timestamp_time",
      "-of", "json",
      filePath,
    ]);
    const timestamp = frameTimestamp(probe);
    if (
      (!isEnd && (timestamp < -FRAME_TIMESTAMP_EPSILON_SECONDS || timestamp > frameDuration + FRAME_TIMESTAMP_EPSILON_SECONDS)) ||
      (isEnd && (
        timestamp < metadata.duration - frameDuration - FRAME_TIMESTAMP_EPSILON_SECONDS ||
        timestamp > metadata.duration + FRAME_TIMESTAMP_EPSILON_SECONDS
      ))
    ) {
      fail("Case comparison video is not decodable at its boundary frames.");
    }
  }
}

async function verifySameRangeMedia(item, directory, runner) {
  const beforePath = path.join(directory, item.assets.beforeVideo);
  const afterPath = path.join(directory, item.assets.afterVideo);
  const [beforeProbe, afterProbe] = await Promise.all([
    probeMedia(beforePath, runner),
    probeMedia(afterPath, runner),
  ]);
  const before = videoMetadata(beforeProbe);
  const after = videoMetadata(afterProbe);
  verifyComparisonMetadata(before, item, item.media);
  verifyComparisonMetadata(after, item, item.afterDimensions);
  const tolerance = 1 / item.media.frameRate;
  const expectedDuration = item.comparison.endSeconds - item.comparison.startSeconds;
  if (
    before.duration > MAXIMUM_DURATION_SECONDS + DURATION_COMPARISON_EPSILON_SECONDS ||
    after.duration > MAXIMUM_DURATION_SECONDS + DURATION_COMPARISON_EPSILON_SECONDS ||
    expectedDuration > MAXIMUM_DURATION_SECONDS + DURATION_COMPARISON_EPSILON_SECONDS
  ) {
    fail("Case comparison video exceeds the public media limits.");
  }
  if (Math.abs(before.duration - after.duration) > tolerance + DURATION_COMPARISON_EPSILON_SECONDS) {
    fail("Case comparison media must cover the same duration.");
  }
  if (
    Math.abs(before.duration - expectedDuration) > tolerance + DURATION_COMPARISON_EPSILON_SECONDS ||
    Math.abs(after.duration - expectedDuration) > tolerance + DURATION_COMPARISON_EPSILON_SECONDS
  ) {
    fail("Case comparison media duration does not match the manifest range.");
  }
  await Promise.all([
    assertBoundaryFrames(beforePath, runner, before),
    assertBoundaryFrames(afterPath, runner, after),
  ]);
}

async function verifyPoster(item, directory, runner) {
  const probe = await probeMedia(path.join(directory, item.assets.poster), runner);
  const metadata = imageMetadata(probe);
  if (metadata.codec !== "webp") fail("Case poster has an unsupported format.");
  if (
    metadata.width !== item.afterDimensions.width ||
    metadata.height !== item.afterDimensions.height
  ) {
    fail("Case poster dimensions do not match the manifest.");
  }
}

async function verifyPublicReport(item, directory) {
  let report;
  try {
    report = JSON.parse(await readFile(path.join(directory, item.assets.publicReport), "utf8"));
  } catch {
    fail("Public case report could not be read.");
  }
  const outputHashes = isPlainObject(report) ? report.output_sha256 : undefined;
  if (!isPlainObject(outputHashes)) {
    fail("Public case report does not bind the declared media hashes.");
  }
  for (const key of ["beforeVideo", "afterVideo", "poster"]) {
    if (outputHashes[key] !== item.hashes[key]) {
      fail("Public case report does not bind the declared media hashes.");
    }
  }
}

export async function verifyCaseMedia({ manifest, directory, runner } = {}) {
  const validated = validateCaseManifest(manifest);
  if (typeof directory !== "string" || typeof runner !== "function") {
    fail("Case media verification inputs are invalid.");
  }
  const declared = declaredCaseFiles(validated);
  await assertExactFileAllowlist(directory, declared);
  for (const item of validated.cases) {
    await verifyHashBindings(item, directory);
    await verifySameRangeMedia(item, directory, runner);
    await verifyPoster(item, directory, runner);
    await verifyPublicReport(item, directory);
  }
  return { caseCount: validated.cases.length, fileCount: declared.size };
}

async function verifyDefaultCaseMedia() {
  const manifest = JSON.parse(await readFile(defaultManifestPath, "utf8"));
  const result = await verifyCaseMedia({
    manifest,
    directory: defaultDirectory,
    runner: createFfprobeRunner(),
  });
  process.stdout.write(
    `Verified ${result.fileCount} public case files across ${result.caseCount} case studies.\n`,
  );
}

if (process.argv[1] !== undefined && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await verifyDefaultCaseMedia();
}
