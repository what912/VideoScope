import { execFile } from "node:child_process";
import { Buffer } from "node:buffer";
import { randomUUID } from "node:crypto";
import {
  mkdir,
  rename,
  rm,
  stat,
} from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";

import { runWithIndependentCleanup } from "./media-safety.mjs";
import { stillFilterFor, videoFilterFor } from "./original-scenes.mjs";

const execFileAsync = promisify(execFile);
const stagingPrefix = ".media-staging-";
const backupPrefix = ".media-backup-";
const ffmpegRenderTimeoutMilliseconds = 600_000;
const maximumStderrCharacters = 4_000;
const maximumConcurrentRenders = 2;

function assertDirectChild(candidate, parent, prefix) {
  const resolvedCandidate = path.resolve(candidate);
  const resolvedParent = path.resolve(parent);
  if (
    path.dirname(resolvedCandidate) !== resolvedParent
    || !path.basename(resolvedCandidate).startsWith(prefix)
  ) {
    throw new Error(`Refusing unsafe temporary path: ${resolvedCandidate}`);
  }
  return resolvedCandidate;
}

function outputPath(stagingDirectory, filename) {
  if (typeof filename !== "string" || path.basename(filename) !== filename) {
    throw new Error(`Unsafe generated media filename: ${filename}`);
  }
  return path.join(stagingDirectory, filename);
}

function isFfmpegTimeout(error) {
  return error?.killed === true || error?.code === "ETIMEDOUT";
}

function ffmpegErrorDetail(error) {
  const stderr = error?.stderr;
  const detail = typeof stderr === "string"
    ? stderr
    : Buffer.isBuffer(stderr)
      ? stderr.toString("utf8")
      : String(error?.message ?? error);
  if (
    detail.trim().length === 0
    && isFfmpegTimeout(error)
  ) {
    return `FFmpeg exceeded the bounded ${ffmpegRenderTimeoutMilliseconds}ms render timeout.`;
  }
  return detail.slice(-maximumStderrCharacters);
}

function inputArguments(graph, timestampSeconds) {
  const seekArguments = typeof timestampSeconds === "number"
    ? ["-ss", String(timestampSeconds)]
    : [];
  return ["-f", "lavfi", ...seekArguments, "-i", graph];
}

function commonArguments() {
  return ["-hide_banner", "-loglevel", "error", "-y"];
}

function videoArguments(item, destination) {
  return [
    ...commonArguments(),
    ...inputArguments(videoFilterFor(item)),
    "-map", "0:v:0",
    "-an",
    "-map_metadata", "-1",
    "-fflags", "+bitexact",
    "-c:v", "libx264",
    "-preset", "slow",
    "-crf", "20",
    "-pix_fmt", "yuv420p",
    "-r", String(item.frameRate),
    "-threads", "1",
    "-movflags", "+faststart",
    destination,
  ];
}

function posterArguments(item, destination) {
  const isVideoPoster = typeof item.video === "string";
  const graph = isVideoPoster ? videoFilterFor(item) : stillFilterFor(item);
  const timestampSeconds = isVideoPoster ? item.posterTimestampSeconds : undefined;
  return [
    ...commonArguments(),
    ...inputArguments(graph, timestampSeconds),
    "-map", "0:v:0",
    "-an",
    "-frames:v", "1",
    "-map_metadata", "-1",
    "-fflags", "+bitexact",
    "-c:v", "libwebp",
    "-compression_level", "6",
    "-q:v", "82",
    "-threads", "1",
    destination,
  ];
}

function declaredOutputNames(items) {
  const filenames = [];
  for (const item of items) {
    if (item === null || typeof item !== "object") {
      throw new TypeError("Generated media item must be an object");
    }
    if (typeof item.video === "string") {
      filenames.push(item.video);
    }
    filenames.push(item.poster);
  }
  return filenames;
}

function validateOutputDeclarations(items) {
  if (items.length !== 9) {
    throw new Error("Generated media requires exactly nine items");
  }

  const filenames = declaredOutputNames(items);
  const everyNameIsSafe = filenames.every(
    (filename) => typeof filename === "string" && path.basename(filename) === filename,
  );
  if (
    filenames.length !== 15
    || new Set(filenames).size !== 15
    || !everyNameIsSafe
  ) {
    throw new Error("Generated media requires 15 unique output files");
  }
  return filenames;
}

async function removeVerifiedDirectory(candidate, parent, prefix) {
  const verified = assertDirectChild(candidate, parent, prefix);
  await rm(verified, { force: true, recursive: true });
}

async function verifyStagedOutputs(stagingDirectory, filenames) {
  for (const filename of filenames) {
    const stagedPath = outputPath(stagingDirectory, filename);
    let details;
    try {
      details = await stat(stagedPath);
    } catch {
      throw new Error(`FFmpeg did not produce ${filename}`);
    }
    if (!details.isFile() || details.size === 0) {
      throw new Error(`FFmpeg did not produce ${filename}`);
    }
  }
}

async function backupExistingOutputs(
  filenames,
  mediaDirectory,
  backupDirectory,
  renameFile,
  backedUp,
) {
  for (const filename of filenames) {
    const destination = outputPath(mediaDirectory, filename);
    let details;
    try {
      details = await stat(destination);
    } catch (error) {
      if (error?.code === "ENOENT") {
        continue;
      }
      throw error;
    }
    if (!details.isFile()) {
      throw new Error(`Existing media output is not a file: ${filename}`);
    }
    await renameFile(destination, outputPath(backupDirectory, filename));
    backedUp.push(filename);
  }
}

async function rollbackPublication({
  mediaDirectory,
  backupDirectory,
  published,
  backedUp,
  renameFile,
}) {
  const errors = [];
  for (const filename of published.toReversed()) {
    try {
      await rm(outputPath(mediaDirectory, filename), { force: true });
    } catch (error) {
      errors.push(error);
    }
  }
  for (const filename of backedUp.toReversed()) {
    try {
      await renameFile(
        outputPath(backupDirectory, filename),
        outputPath(mediaDirectory, filename),
      );
    } catch (error) {
      errors.push(error);
    }
  }
  return errors;
}

async function publishStagedOutputs({
  filenames,
  stagingDirectory,
  mediaDirectory,
  backupDirectory,
  renameFile,
}) {
  const published = [];
  const backedUp = [];
  try {
    await backupExistingOutputs(
      filenames,
      mediaDirectory,
      backupDirectory,
      renameFile,
      backedUp,
    );
    for (const filename of filenames) {
      await renameFile(
        outputPath(stagingDirectory, filename),
        outputPath(mediaDirectory, filename),
      );
      published.push(filename);
    }
  } catch (error) {
    const rollbackErrors = await rollbackPublication({
      mediaDirectory,
      backupDirectory,
      published,
      backedUp,
      renameFile,
    });
    if (rollbackErrors.length > 0) {
      throw new AggregateError(
        [error, ...rollbackErrors],
        "Media publication failed and rollback was incomplete.",
        { cause: error },
      );
    }
    throw error;
  }
}

export async function runFfmpeg(args, {
  runner = execFileAsync,
  ffmpeg = "ffmpeg",
} = {}) {
  if (!Array.isArray(args) || args.some((argument) => typeof argument !== "string")) {
    throw new TypeError("FFmpeg arguments must be a string array");
  }

  try {
    await runner(ffmpeg, args, {
      maxBuffer: 64 * 1024,
      shell: false,
      timeout: ffmpegRenderTimeoutMilliseconds,
      windowsHide: true,
    });
  } catch (error) {
    if (error?.code === "ENOENT") {
      throw new Error(
        "FFmpeg was not found. Install it or pass its executable path explicitly.",
      );
    }
    const safeError = new Error(`FFmpeg failed: ${ffmpegErrorDetail(error)}`);
    if (isFfmpegTimeout(error)) {
      throw safeError;
    }
    throw new Error(safeError.message, { cause: error });
  }
}

export async function generateItem(item, stagingDirectory, runner, ffmpeg) {
  if (item === null || typeof item !== "object") {
    throw new TypeError("Generated media item must be an object");
  }

  if (typeof item.video === "string") {
    await runFfmpeg(videoArguments(item, outputPath(stagingDirectory, item.video)), {
      runner,
      ffmpeg,
    });
  }
  await runFfmpeg(posterArguments(item, outputPath(stagingDirectory, item.poster)), {
    runner,
    ffmpeg,
  });
}

async function generateItems(items, stagingDirectory, runner, ffmpeg) {
  let nextIndex = 0;
  let stopped = false;
  const workerCount = Math.min(maximumConcurrentRenders, items.length);
  const workers = Array.from({ length: workerCount }, async () => {
    while (!stopped) {
      const index = nextIndex;
      nextIndex += 1;
      if (index >= items.length) {
        return;
      }
      try {
        await generateItem(items[index], stagingDirectory, runner, ffmpeg);
      } catch (error) {
        stopped = true;
        throw error;
      }
    }
  });
  const outcomes = await Promise.allSettled(workers);
  const failure = outcomes.find((outcome) => outcome.status === "rejected");
  if (failure?.status === "rejected") {
    throw failure.reason;
  }
}

export async function generateOriginalMedia({
  items,
  mediaDirectory,
  runner,
  ffmpeg,
  renameFile = rename,
}) {
  if (!Array.isArray(items)) {
    throw new TypeError("Generated media items must be an array");
  }
  if (typeof mediaDirectory !== "string") {
    throw new TypeError("Generated media directory must be a string");
  }
  if (typeof renameFile !== "function") {
    throw new TypeError("Generated media rename function must be callable");
  }

  const filenames = validateOutputDeclarations(items);
  const resolvedMediaDirectory = path.resolve(mediaDirectory);
  await mkdir(resolvedMediaDirectory, { recursive: true });
  const stagingDirectory = path.join(
    resolvedMediaDirectory,
    `${stagingPrefix}${randomUUID()}`,
  );
  const backupDirectory = path.join(
    resolvedMediaDirectory,
    `${backupPrefix}${randomUUID()}`,
  );
  let retainBackup = false;

  return runWithIndependentCleanup(
    async () => {
      assertDirectChild(stagingDirectory, resolvedMediaDirectory, stagingPrefix);
      assertDirectChild(backupDirectory, resolvedMediaDirectory, backupPrefix);
      await mkdir(stagingDirectory);
      await mkdir(backupDirectory);
      await generateItems(items, stagingDirectory, runner, ffmpeg);

      await verifyStagedOutputs(stagingDirectory, filenames);
      try {
        await publishStagedOutputs({
          filenames,
          stagingDirectory,
          mediaDirectory: resolvedMediaDirectory,
          backupDirectory,
          renameFile,
        });
      } catch (error) {
        retainBackup = error instanceof AggregateError;
        throw error;
      }
    },
    [
      () => removeVerifiedDirectory(
        stagingDirectory,
        resolvedMediaDirectory,
        stagingPrefix,
      ),
      () => retainBackup
        ? undefined
        : removeVerifiedDirectory(
          backupDirectory,
          resolvedMediaDirectory,
          backupPrefix,
        ),
    ],
  );
}
