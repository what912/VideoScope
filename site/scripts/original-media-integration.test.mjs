import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it } from "vitest";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const mediaDirectory = path.resolve(scriptDirectory, "..", "public", "media");
const manifestPath = path.join(mediaDirectory, "media-sources.json");
const temporaryDirectories = [];

async function activeManifest() {
  return JSON.parse(await readFile(manifestPath, "utf8"));
}

async function writeTemporaryManifest(manifest) {
  const directory = await mkdtemp(
    path.join(tmpdir(), "active-media-媒体 directory-"),
  );
  temporaryDirectories.push(directory);
  const temporaryManifestPath = path.join(directory, "media-sources.json");
  await writeFile(
    temporaryManifestPath,
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf8",
  );
  return temporaryManifestPath;
}

async function writeTemporaryDirectory(prefix) {
  const directory = await mkdtemp(path.join(tmpdir(), prefix));
  temporaryDirectories.push(directory);
  return directory;
}

async function writeTemporaryDistribution(mediaFilenames) {
  const directory = await mkdtemp(
    path.join(tmpdir(), "active-media-build-媒体 directory-"),
  );
  temporaryDirectories.push(directory);
  const distributionMediaDirectory = path.join(directory, "media");
  await mkdir(distributionMediaDirectory);
  await Promise.all(
    mediaFilenames.map((filename) =>
      writeFile(path.join(distributionMediaDirectory, filename), "", "utf8")
    ),
  );
  return directory;
}

function expectedMediaFilenames(manifest) {
  return manifest.items.flatMap((item) =>
    [item.video, item.poster].filter(Boolean)
  );
}

async function expectPathSafeFailure(operation, expectedMessage, forbiddenFragments) {
  let failure;

  try {
    await operation();
  } catch (error) {
    failure = error;
  }

  expect(failure).toBeInstanceOf(Error);
  if (!(failure instanceof Error)) {
    throw new Error("Expected the deployed-media audit to fail");
  }
  expect(failure.message).toBe(expectedMessage);
  for (const fragment of forbiddenFragments) {
    expect(failure.message).not.toContain(fragment);
  }
}

afterEach(async () => {
  await Promise.all(
    temporaryDirectories.splice(0).map((directory) =>
      rm(directory, { force: true, recursive: true })
    ),
  );
});

async function importActiveEntrypoint(filename) {
  const source = await readFile(path.join(scriptDirectory, filename), "utf8");
  if (source.trimEnd().endsWith("await main();")) {
    throw new Error(`${filename} executes its CLI on import`);
  }
  return filename === "prepare-media.mjs"
    ? import("./prepare-media.mjs")
    : import("./verify-media.mjs");
}

describe("active original-media entry points", () => {
  it("preparation validates the manifest and delegates to generateOriginalMedia", async () => {
    const { prepareOriginalMedia } = await importActiveEntrypoint("prepare-media.mjs");
    const manifest = await activeManifest();
    const subprocessRunner = async () => undefined;
    const result = Symbol("generated");
    let received;

    const actual = await prepareOriginalMedia({
      manifestPath,
      mediaDirectory,
      ffmpeg: "ffmpeg-integration",
      runner: subprocessRunner,
      generate: async (options) => {
        received = options;
        return result;
      },
    });

    expect(actual).toBe(result);
    expect(received).toEqual({
      items: manifest.items,
      mediaDirectory,
      runner: subprocessRunner,
      ffmpeg: "ffmpeg-integration",
    });
  });

  it("verification delegates media checks before retained runtime and bundle audits", async () => {
    const { verifyMedia } = await importActiveEntrypoint("verify-media.mjs");
    const manifest = await activeManifest();
    const subprocessRunner = async () => undefined;
    const calls = [];

    await verifyMedia({
      manifestPath,
      mediaDirectory,
      distributionDirectory: "dist-integration",
      runner: subprocessRunner,
      verify: async (options) => {
        calls.push(["media", options]);
      },
      auditMedia: async (directory, receivedManifest) => {
        calls.push(["deployed-media", directory, receivedManifest]);
        return { fileCount: 15 };
      },
      auditRuntime: async (directory) => {
        calls.push(["runtime", directory]);
        return { checkedFiles: 4, urlCount: 0 };
      },
      auditBundles: async (directory) => {
        calls.push(["bundles", directory]);
        return {
          initialJavaScript: 1,
          initialCss: 1,
          largestDeferredJavaScript: 1,
          totalJavaScript: 1,
        };
      },
      output: { write() {} },
    });

    expect(calls).toEqual([
      ["media", { manifest, mediaDirectory, runner: subprocessRunner }],
      ["deployed-media", "dist-integration", manifest],
      ["runtime", "dist-integration"],
      ["bundles", "dist-integration"],
    ]);
  });

  it("rejects unexpected deployed files without echoing names or paths", async () => {
    const { auditBuiltMediaOutputs } = await importActiveEntrypoint("verify-media.mjs");
    const manifest = await activeManifest();
    const unexpectedFilename = "private-source-origin.txt";
    const distributionDirectory = await writeTemporaryDistribution([
      ...expectedMediaFilenames(manifest),
      unexpectedFilename,
    ]);

    await expectPathSafeFailure(
      () => auditBuiltMediaOutputs(distributionDirectory, manifest),
      "Built media does not match the exact manifest allowlist. "
        + "Unexpected deployed media files are present.",
      [distributionDirectory, unexpectedFilename],
    );
  });

  it("rejects missing expected files without echoing names or paths", async () => {
    const { auditBuiltMediaOutputs } = await importActiveEntrypoint("verify-media.mjs");
    const manifest = await activeManifest();
    const expectedMedia = expectedMediaFilenames(manifest);
    const missingFilename = expectedMedia[0];
    const distributionDirectory = await writeTemporaryDistribution(
      expectedMedia.slice(1),
    );

    await expectPathSafeFailure(
      () => auditBuiltMediaOutputs(distributionDirectory, manifest),
      "Built media does not match the exact manifest allowlist. "
        + "Required deployed media files are missing.",
      [distributionDirectory, missingFilename],
    );
  });

  it("rejects non-file deployed entries without echoing names or paths", async () => {
    const { auditBuiltMediaOutputs } = await importActiveEntrypoint("verify-media.mjs");
    const manifest = await activeManifest();
    const nonFileEntry = "private-build-directory";
    const distributionDirectory = await writeTemporaryDistribution(
      expectedMediaFilenames(manifest),
    );
    await mkdir(path.join(distributionDirectory, "media", nonFileEntry));

    await expectPathSafeFailure(
      () => auditBuiltMediaOutputs(distributionDirectory, manifest),
      "Built media does not match the exact manifest allowlist. "
        + "Non-file deployed media entries are present.",
      [distributionDirectory, nonFileEntry],
    );
  });

  it("reports a missing deployed-media directory without exposing its path", async () => {
    const { auditBuiltMediaOutputs } = await importActiveEntrypoint("verify-media.mjs");
    const manifest = await activeManifest();
    const distributionDirectory = await writeTemporaryDirectory(
      "missing-media-build-中文 directory-",
    );

    await expectPathSafeFailure(
      () => auditBuiltMediaOutputs(distributionDirectory, manifest),
      "Production media directory is missing; run npm run build first.",
      [distributionDirectory],
    );
  });

  it("distinguishes other deployed-media I/O failures without exposing paths", async () => {
    const { auditBuiltMediaOutputs } = await importActiveEntrypoint("verify-media.mjs");
    const manifest = await activeManifest();
    const distributionDirectory = await writeTemporaryDirectory(
      "unreadable-media-build-中文 directory-",
    );
    await writeFile(path.join(distributionDirectory, "media"), "not a directory", "utf8");

    await expectPathSafeFailure(
      () => auditBuiltMediaOutputs(distributionDirectory, manifest),
      "Production media directory could not be read.",
      [distributionDirectory],
    );
  });

  it.each([
    [
      "a forbidden third-party field",
      (manifest) => { manifest.items[0].downloadUrl = "forbidden-source"; },
      /forbidden field downloadUrl/u,
    ],
    [
      "a negative poster timestamp",
      (manifest) => { manifest.items[0].posterTimestampSeconds = -1; },
      /invalid poster timestamp/u,
    ],
  ])("rejects %s before media and build audits", async (
    _label,
    mutate,
    expectedMessage,
  ) => {
    const { verifyMedia } = await importActiveEntrypoint("verify-media.mjs");
    const manifest = await activeManifest();
    mutate(manifest);
    const invalidManifestPath = await writeTemporaryManifest(manifest);
    const calls = [];

    await expect(verifyMedia({
      manifestPath: invalidManifestPath,
      mediaDirectory,
      verify: async () => { calls.push("media"); },
      auditRuntime: async () => {
        calls.push("runtime");
        return { checkedFiles: 0, urlCount: 0 };
      },
      auditBundles: async () => {
        calls.push("bundles");
        return {
          initialJavaScript: 0,
          initialCss: 0,
          largestDeferredJavaScript: 0,
          totalJavaScript: 0,
        };
      },
      output: { write() {} },
    })).rejects.toThrow(expectedMessage);

    expect(calls).toEqual([]);
  });

  it("uses the configured ffprobe executable without changing the argument array", async () => {
    const { createFfprobeRunner } = await importActiveEntrypoint("verify-media.mjs");
    const args = ["-v", "error", "C:\\媒体 directory\\clip.mp4"];
    const options = { shell: false, timeout: 30_000 };
    const calls = [];
    const expected = { stdout: "{}" };
    const runner = createFfprobeRunner({
      ffprobe: "C:\\tools directory\\ffprobe.exe",
      execute: async (file, receivedArgs, receivedOptions) => {
        calls.push({ file, args: receivedArgs, options: receivedOptions });
        return expected;
      },
    });

    await expect(runner("ffprobe", args, options)).resolves.toBe(expected);
    expect(calls).toEqual([{
      file: "C:\\tools directory\\ffprobe.exe",
      args,
      options,
    }]);
  });

  it("propagates generation and verification failures without a legacy fallback", async () => {
    const [{ prepareOriginalMedia }, { verifyMedia }] = await Promise.all([
      importActiveEntrypoint("prepare-media.mjs"),
      importActiveEntrypoint("verify-media.mjs"),
    ]);
    const generationError = new Error("generation stopped");
    const verificationError = new Error("verification stopped");
    let runtimeAudits = 0;

    await expect(prepareOriginalMedia({
      manifestPath,
      mediaDirectory,
      generate: async () => { throw generationError; },
    })).rejects.toBe(generationError);

    await expect(verifyMedia({
      manifestPath,
      mediaDirectory,
      verify: async () => { throw verificationError; },
      auditRuntime: async () => { runtimeAudits += 1; },
      auditBundles: async () => { runtimeAudits += 1; },
    })).rejects.toBe(verificationError);
    expect(runtimeAudits).toBe(0);
  });

  it("keeps both active adapters free of network, URL, and third-party provenance code", async () => {
    const sources = await Promise.all([
      readFile(path.join(scriptDirectory, "prepare-media.mjs"), "utf8"),
      readFile(path.join(scriptDirectory, "verify-media.mjs"), "utf8"),
    ]);
    const activeSource = sources.join("\n");

    expect(activeSource).not.toMatch(/fetch\s*\(/u);
    expect(activeSource).not.toMatch(/https?:\/\//u);
    expect(activeSource).not.toMatch(/\b(?:sourcePage|downloadUrl|provider|downloadDate)\b/u);
    expect(activeSource).not.toMatch(/ATTRIBUTION\.md|legacy|fallback/iu);
  });
});
