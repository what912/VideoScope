import {
  mkdir,
  mkdtemp,
  readFile,
  readdir,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { inspect } from "node:util";

import { afterEach, describe, expect, it } from "vitest";

import { runWithIndependentCleanup } from "./media-safety.mjs";
import {
  generateItem,
  generateOriginalMedia,
  runFfmpeg,
} from "./original-media-generator.mjs";

const temporaryDirectories = [];

const videoItem = Object.freeze({
  role: "hero",
  scene: "optical-aperture",
  workingWidth: 1920,
  workingHeight: 1080,
  outputWidth: 1280,
  outputHeight: 720,
  frameRate: 24,
  durationSeconds: 8,
  video: "hero-optical.mp4",
  poster: "hero-optical.webp",
  posterTimestampSeconds: 2.25,
});

const stillItem = Object.freeze({
  role: "evidence-a",
  scene: "cyan-caustic",
  workingWidth: 1920,
  workingHeight: 1080,
  outputWidth: 1280,
  outputHeight: 720,
  frameRate: 24,
  poster: "evidence-lake.webp",
  stillTimeSeconds: 2,
});

const completeItems = Object.freeze([
  ...Array.from({ length: 6 }, (_unused, index) => ({
    ...videoItem,
    role: `video-${index}`,
    video: `video-${index}.mp4`,
    poster: `video-${index}.webp`,
  })),
  ...Array.from({ length: 3 }, (_unused, index) => ({
    ...stillItem,
    role: `still-${index}`,
    poster: `still-${index}.webp`,
  })),
]);

async function temporaryDirectory() {
  const directory = await mkdtemp(path.join(tmpdir(), "original-media-generator-"));
  temporaryDirectories.push(directory);
  return directory;
}

async function writeFakeOutputsFromArguments(args) {
  await writeFile(args.at(-1), "generated media", "utf8");
}

afterEach(async () => {
  await Promise.all(temporaryDirectories.splice(0).map(
    (directory) => rm(directory, { force: true, recursive: true }),
  ));
});

describe("offline original media generation", () => {
  it("generates from lavfi without a network or filesystem source", async () => {
    const calls = [];
    const directory = await temporaryDirectory();
    const stagingDirectory = path.join(directory, "staging");
    await mkdir(stagingDirectory);

    await generateItem(videoItem, stagingDirectory, async (file, args, options) => {
      calls.push({ file, args, options });
      await writeFakeOutputsFromArguments(args);
    });

    expect(calls).toHaveLength(2);
    expect(calls.every(({ args }) => args.includes("-f") && args.includes("lavfi"))).toBe(true);
    expect(calls.every(({ options }) => options.shell === false)).toBe(true);
    expect(calls.every(({ options }) => options.timeout === 600_000)).toBe(true);
    expect(calls.every(({ options }) => options.maxBuffer === 64 * 1024)).toBe(true);
    expect(JSON.stringify(calls)).not.toMatch(/https?:|fetch|download/iu);
  });

  it("normalizes missing and failed FFmpeg process errors with bounded stderr", async () => {
    await expect(runFfmpeg(["-version"], {
      runner: async () => {
        const error = new Error("not found");
        error.code = "ENOENT";
        throw error;
      },
    })).rejects.toThrow(/FFmpeg was not found/u);

    let caught;
    try {
      await runFfmpeg(["-version"], {
        runner: async () => {
          const error = new Error("failed");
          error.stderr = `begin${"x".repeat(5_000)}end`;
          throw error;
        },
      });
    } catch (error) {
      caught = error;
    }
    expect(caught.message).toMatch(/^FFmpeg failed: x{100}/u);
    expect(caught.message).toMatch(/end$/u);
    expect(caught.message).not.toContain("begin");
  });

  it("reports an empty-stderr FFmpeg timeout without leaking its process message", async () => {
    const windowsPrivatePath = "C:\\private\\operator\\source.mp4";
    const unixPrivatePath = "/private/operator/source.mp4";
    const stagingFilename = ".media-staging-secret/hero-optical.mp4";
    const command = `ffmpeg -i ${windowsPrivatePath} ${unixPrivatePath} ${stagingFilename}`;
    let caught;
    try {
      await runFfmpeg(["-version"], {
        runner: async () => {
          const error = new Error(`Command failed: ${command}`);
          error.code = "ETIMEDOUT";
          error.killed = true;
          error.stderr = "";
          throw error;
        },
      });
    } catch (error) {
      caught = error;
    }

    expect(caught.message).toBe(
      "FFmpeg failed: FFmpeg exceeded the bounded 600000ms render timeout.",
    );
    const renderedError = inspect(caught, { depth: null });
    expect(renderedError).not.toContain(windowsPrivatePath);
    expect(renderedError).not.toContain(unixPrivatePath);
    expect(renderedError).not.toContain(command);
    expect(renderedError).not.toContain(stagingFilename);
  });

  it("does not replace an existing file when a later staged render fails", async () => {
    const mediaDirectory = await temporaryDirectory();
    const existingPath = path.join(mediaDirectory, videoItem.video);
    await writeFile(existingPath, "known good media", "utf8");
    let calls = 0;

    await expect(generateOriginalMedia({
      items: completeItems,
      mediaDirectory,
      runner: async (_file, args) => {
        calls += 1;
        if (calls === 2) {
          throw new Error("simulated poster failure");
        }
        await writeFakeOutputsFromArguments(args);
      },
    })).rejects.toThrow(/FFmpeg failed: simulated poster failure/u);

    await expect(readFile(existingPath, "utf8")).resolves.toBe("known good media");
    expect((await readdir(mediaDirectory)).filter((name) => name.startsWith(".media-staging-"))).toEqual([]);
  });

  it("uses a direct-child staging directory and publishes only non-empty completed outputs", async () => {
    const mediaDirectory = await temporaryDirectory();
    const destinations = [];

    await generateOriginalMedia({
      items: completeItems,
      mediaDirectory,
      runner: async (_file, args) => {
        destinations.push(args.at(-1));
        await writeFakeOutputsFromArguments(args);
      },
    });

    expect(destinations).toHaveLength(15);
    for (const destination of destinations) {
      const stagingDirectory = path.dirname(destination);
      expect(path.dirname(stagingDirectory)).toBe(path.resolve(mediaDirectory));
      expect(path.basename(stagingDirectory)).toMatch(/^\.media-staging-/u);
    }
    await expect(readFile(path.join(mediaDirectory, completeItems[0].video), "utf8")).resolves.toBe("generated media");
    await expect(readFile(path.join(mediaDirectory, completeItems[0].poster), "utf8")).resolves.toBe("generated media");
    await expect(readFile(path.join(mediaDirectory, completeItems.at(-1).poster), "utf8")).resolves.toBe("generated media");
  });

  it.each([
    ["empty declarations", []],
    ["incomplete declarations", completeItems.slice(0, 8)],
    [
      "duplicate output declarations",
      completeItems.map((item, index) => index === 1
        ? { ...item, video: completeItems[0].video }
        : item),
    ],
  ])("rejects %s before starting the runner", async (_label, items) => {
    const mediaDirectory = await temporaryDirectory();
    let calls = 0;

    await expect(generateOriginalMedia({
      items,
      mediaDirectory,
      runner: async () => {
        calls += 1;
      },
    })).rejects.toThrow(/nine items|15 unique output files/u);

    expect(calls).toBe(0);
    await expect(readdir(mediaDirectory)).resolves.toEqual([]);
  });

  it("restores every existing target after a mid-publish rename failure", async () => {
    const mediaDirectory = await temporaryDirectory();
    const firstVideo = completeItems[0].video;
    const firstPoster = completeItems[0].poster;
    await writeFile(path.join(mediaDirectory, firstVideo), "old video", "utf8");
    await writeFile(path.join(mediaDirectory, firstPoster), "old poster", "utf8");
    const publishError = new Error("publish poster failed");

    await expect(generateOriginalMedia({
      items: completeItems,
      mediaDirectory,
      runner: async (_file, args) => writeFakeOutputsFromArguments(args),
      renameFile: async (source, destination) => {
        const sourceParent = path.basename(path.dirname(source));
        if (
          sourceParent.startsWith(".media-staging-")
          && path.basename(source) === firstPoster
        ) {
          throw publishError;
        }
        await rename(source, destination);
      },
    })).rejects.toBe(publishError);

    await expect(readFile(path.join(mediaDirectory, firstVideo), "utf8")).resolves.toBe("old video");
    await expect(readFile(path.join(mediaDirectory, firstPoster), "utf8")).resolves.toBe("old poster");
    expect((await readdir(mediaDirectory)).filter((name) => name.startsWith(".media-"))).toEqual([]);
  });

  it("restores earlier backups when a later backup move fails", async () => {
    const mediaDirectory = await temporaryDirectory();
    const firstVideo = completeItems[0].video;
    const firstPoster = completeItems[0].poster;
    await writeFile(path.join(mediaDirectory, firstVideo), "old video", "utf8");
    await writeFile(path.join(mediaDirectory, firstPoster), "old poster", "utf8");
    const backupError = new Error("backup poster failed");

    await expect(generateOriginalMedia({
      items: completeItems,
      mediaDirectory,
      runner: async (_file, args) => writeFakeOutputsFromArguments(args),
      renameFile: async (source, destination) => {
        if (
          path.dirname(source) === path.resolve(mediaDirectory)
          && path.basename(source) === firstPoster
        ) {
          throw backupError;
        }
        await rename(source, destination);
      },
    })).rejects.toBe(backupError);

    await expect(readFile(path.join(mediaDirectory, firstVideo), "utf8")).resolves.toBe("old video");
    await expect(readFile(path.join(mediaDirectory, firstPoster), "utf8")).resolves.toBe("old poster");
    expect((await readdir(mediaDirectory)).filter((name) => name.startsWith(".media-"))).toEqual([]);
  });

  it("keeps publish and rollback failures diagnosable", async () => {
    const mediaDirectory = await temporaryDirectory();
    const firstVideo = completeItems[0].video;
    const firstPoster = completeItems[0].poster;
    await writeFile(path.join(mediaDirectory, firstVideo), "old video", "utf8");
    await writeFile(path.join(mediaDirectory, firstPoster), "old poster", "utf8");
    const publishError = new Error("publish poster failed");
    const rollbackError = new Error("restore video failed");

    let caught;
    try {
      await generateOriginalMedia({
        items: completeItems,
        mediaDirectory,
        runner: async (_file, args) => writeFakeOutputsFromArguments(args),
        renameFile: async (source, destination) => {
          const sourceParent = path.basename(path.dirname(source));
          if (
            sourceParent.startsWith(".media-staging-")
            && path.basename(source) === firstPoster
          ) {
            throw publishError;
          }
          if (
            sourceParent.startsWith(".media-backup-")
            && path.basename(source) === firstVideo
          ) {
            throw rollbackError;
          }
          await rename(source, destination);
        },
      });
    } catch (error) {
      caught = error;
    }

    expect(caught).toBeInstanceOf(AggregateError);
    expect(caught.errors).toContain(publishError);
    expect(caught.errors).toContain(rollbackError);
    const backupDirectories = (await readdir(mediaDirectory)).filter(
      (name) => name.startsWith(".media-backup-"),
    );
    expect(backupDirectories).toHaveLength(1);
    const backupDirectory = path.join(mediaDirectory, backupDirectories[0]);
    expect(path.dirname(backupDirectory)).toBe(path.resolve(mediaDirectory));
    await expect(readFile(path.join(backupDirectory, firstVideo), "utf8")).resolves.toBe("old video");
    expect((await readdir(mediaDirectory)).filter(
      (name) => name.startsWith(".media-staging-"),
    )).toEqual([]);
  });

  it("preserves the operation error when independent cleanups also fail", async () => {
    const operationError = new Error("render failed");
    const cleanupError = new Error("staging cleanup failed");
    const order = [];

    let caught;
    try {
      await runWithIndependentCleanup(
        async () => {
          order.push("operation");
          throw operationError;
        },
        [
          () => {
            order.push("cleanup failed");
            throw cleanupError;
          },
          () => {
            order.push("cleanup completed");
          },
        ],
      );
    } catch (error) {
      caught = error;
    }

    expect(order).toEqual(["operation", "cleanup failed", "cleanup completed"]);
    expect(caught).toBeInstanceOf(AggregateError);
    expect(caught.cause).toBe(operationError);
    expect(caught.errors).toEqual([operationError, cleanupError]);
  });
});
