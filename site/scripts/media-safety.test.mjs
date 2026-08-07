import { execFile } from "node:child_process";
import { Buffer, isUtf8 } from "node:buffer";
import { access, readFile } from "node:fs/promises";
import path from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  GENERATED_MEDIA_LICENSE,
  MEDIA_ROLES,
  runWithIndependentCleanup,
  SCENE_IDS,
  validateManifest,
} from "./media-safety.mjs";

const execFileAsync = promisify(execFile);
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const siteDirectory = path.resolve(scriptDirectory, "..");
const repositoryDirectory = path.resolve(siteDirectory, "..");
const mediaDirectory = path.join(siteDirectory, "public", "media");
const manifestPath = path.join(mediaDirectory, "media-sources.json");
const excludedTrackedDirectories = new Set([".git", ".worktrees", "node_modules"]);
const generatedTrackedPaths = [
  /^site\/dist\//u,
  /^site\/public\/media\/.*\.(?:mp4|webp)$/iu,
];
const legacyProviderDomains = [
  ["mix", "kit", ".", "co"].join(""),
  ["assets", ".", "mix", "kit", ".", "co"].join(""),
];
const legacyStockMediaLicense = ["mix", "kit stock video free license"].join("");
const mediaTargets = [
  ["site", "public", "media"].join("/"),
  ["public", "media"].join("/"),
];
const networkRetrievalPatterns = [
  /\bdownload\b/u,
  /\bretrieve\b/u,
  /\bcurl\b/u,
  /\bwget\b/u,
  /\binvoke-webrequest\b/u,
  /\birm\b/u,
  /\bfetch\s*\(/u,
  /\bhttps?\.get\b/u,
  /\breadable\.fromweb\b/u,
];
const projectMediaPath = (...segments) => ["site", "public", "media", ...segments].join("/");

const expectedDeclarations = Object.freeze([
  ["hero", "optical-aperture", "hero-optical.mp4", "hero-optical.webp"],
  ["product-proof", "night-observation-grid", "city-nightlife.mp4", "city-nightlife.webp"],
  ["upload-lab", "fluid-spectrum", "upload-liquid.mp4", "upload-liquid.webp"],
  ["diagnosis", "diagnostic-mesh", "diagnosis-fashion.mp4", "diagnosis-fashion.webp"],
  ["compare-a", "cool-topography", "compare-hills.mp4", "compare-hills.webp"],
  ["compare-b", "dawn-spectrum", "compare-sunrise.mp4", "compare-sunrise.webp"],
  ["evidence-a", "cyan-caustic", undefined, "evidence-lake.webp"],
  ["evidence-b", "violet-lattice", undefined, "evidence-city.webp"],
  ["evidence-c", "amber-contour", undefined, "evidence-studio.webp"],
]);

async function approvedManifest() {
  return JSON.parse(await readFile(manifestPath, "utf8"));
}

function isTrackedText(contents) {
  return isUtf8(contents) && !contents.includes(0);
}

function hasNetworkRetrievalInstruction(contents) {
  const nonDownloadRetrieval = networkRetrievalPatterns.slice(1);
  if (nonDownloadRetrieval.some((pattern) => pattern.test(contents))) {
    return true;
  }

  return contents
    .split(/\r?\n/u)
    .some((line) => /\bdownload\b/u.test(line)
      && !/\b(?:no|not|never|without)\b[^\n]*\bdownload\b/u.test(line)
      && !/\bdownload\s+url\b/u.test(line)
      && !/(?:\bdownload\b[^\n]*\b(?:json|html|report|model|weights?)\b|\b(?:json|html|report)\b[^\n]*\bdownload\b)/u.test(line));
}

function provenanceViolations(filename, contents) {
  if (!isTrackedText(contents)) {
    return [];
  }

  const normalizedContents = contents.toString("utf8").toLowerCase();
  const violations = [];
  const legacyDomain = legacyProviderDomains.find((domain) => normalizedContents.includes(domain));

  if (legacyDomain) {
    violations.push(`${filename}: legacy provider domain ${legacyDomain}`);
  }
  if (normalizedContents.includes(legacyStockMediaLicense)) {
    violations.push(`${filename}: legacy stock-media license`);
  }
  if (mediaTargets.some((target) => normalizedContents.includes(target))
    && hasNetworkRetrievalInstruction(normalizedContents)) {
    violations.push(`${filename}: operational media download instruction`);
  }

  return violations;
}

describe("media preparation cleanup", () => {
  it("attempts every cleanup and preserves the primary operation error", async () => {
    const primaryError = new Error("optimization failed");
    const cleanupError = new Error("download cleanup failed");
    let secondCleanupCompleted = false;

    let caught;
    try {
      await runWithIndependentCleanup(
        async () => {
          throw primaryError;
        },
        [
          () => {
            throw cleanupError;
          },
          () => {
            secondCleanupCompleted = true;
          },
        ],
      );
    } catch (error) {
      caught = error;
    }

    expect(secondCleanupCompleted).toBe(true);
    expect(caught).toBeInstanceOf(AggregateError);
    expect(caught.cause).toBe(primaryError);
    expect(caught.errors).toEqual([primaryError, cleanupError]);
  });
});

describe("project-authored media manifest", () => {
  it("accepts the exact nine role, scene, and frontend filename declarations", async () => {
    const manifest = await approvedManifest();

    expect(() => validateManifest(manifest)).not.toThrow();
    expect(manifest.schemaVersion).toBe(1);
    expect(manifest.generatorVersion).toBe("1.0.0");
    expect(manifest.license).toBe(GENERATED_MEDIA_LICENSE);
    expect(manifest.items.map(({ role }) => role)).toEqual(MEDIA_ROLES);
    expect(manifest.items.map(({ scene }) => scene)).toEqual(SCENE_IDS);
    expect(manifest.items.map(({ role, scene, video, poster }) => [
      role,
      scene,
      video,
      poster,
    ])).toEqual(expectedDeclarations);
  });

  it("declares fifteen globally unique local outputs and WebP posters", async () => {
    const manifest = await approvedManifest();
    const filenames = manifest.items.flatMap((item) => [
      ...(item.video === undefined ? [] : [item.video]),
      item.poster,
    ]);

    expect(filenames).toHaveLength(15);
    expect(new Set(filenames)).toHaveLength(15);
    expect(manifest.items.every(({ poster }) => poster.endsWith(".webp"))).toBe(true);
  });

  it.each(["sourcePage", "downloadUrl", "provider", "downloadDate"])(
    "rejects forbidden third-party provenance field %s",
    async (field) => {
      const manifest = await approvedManifest();
      manifest.items[0][field] = "https://example.invalid/source";
      expect(() => validateManifest(manifest)).toThrow(/forbidden field/u);
    },
  );

  it.each([
    ["workingWidth", 1920],
    ["workingHeight", 1080],
    ["outputWidth", 1920],
    ["outputHeight", 1080],
    ["frameRate", 23],
    ["durationSeconds", 5],
    ["durationSeconds", 9],
  ])("rejects unsupported %s=%s", async (field, value) => {
    const manifest = await approvedManifest();
    manifest.items[0][field] = value;
    expect(() => validateManifest(manifest)).toThrow(/generation setting/u);
  });

  it.each([
    ["durationSeconds", 0, Number.NaN],
    ["durationSeconds", 0, Number.POSITIVE_INFINITY],
    ["posterTimestampSeconds", 0, Number.NEGATIVE_INFINITY],
    ["stillTimeSeconds", 6, Number.NaN],
  ])("rejects non-finite timing %s on item %s", async (field, index, value) => {
    const manifest = await approvedManifest();
    manifest.items[index][field] = value;
    expect(() => validateManifest(manifest)).toThrow(/generation setting|timestamp|still render time/u);
  });

  it.each([
    ["duplicate role", (manifest) => { manifest.items[1].role = "hero"; }, /role/u],
    ["wrong role scene", (manifest) => { manifest.items[0].scene = "night-observation-grid"; }, /scene/u],
    ["wrong frontend video filename", (manifest) => { manifest.items[0].video = "hero.mp4"; }, /filename/u],
    ["wrong frontend poster filename", (manifest) => { manifest.items[0].poster = "hero.webp"; }, /filename/u],
    ["duplicate video poster and evidence poster", (manifest) => { manifest.items[6].poster = manifest.items[0].poster; }, /reused|unique/u],
    ["poster without WebP", (manifest) => { manifest.items[0].poster = "hero-optical.png"; }, /WebP/u],
    ["unsafe filename", (manifest) => { manifest.items[0].poster = "../hero-optical.webp"; }, /unsafe/u],
    ["missing poster", (manifest) => { delete manifest.items[0].poster; }, /poster/u],
    ["video role without an MP4", (manifest) => { manifest.items[0].video = "hero-optical.webm"; }, /MP4/u],
    ["evidence role with an MP4", (manifest) => { manifest.items[6].video = "evidence.mp4"; }, /evidence/u],
  ])("rejects %s", async (_label, mutate, message) => {
    const manifest = await approvedManifest();
    mutate(manifest);
    expect(() => validateManifest(manifest)).toThrow(message);
  });
});

describe("tracked generated-media safety", () => {
  it("tracks metadata and provenance but no generated MP4 or WebP binaries", async () => {
    const { stdout } = await execFileAsync(
      "git",
      ["-c", "safe.directory=*", "ls-files", "--", projectMediaPath()],
      {
        cwd: repositoryDirectory,
        shell: false,
        timeout: 30_000,
        windowsHide: true,
      },
    );
    const tracked = stdout.split(/\r?\n/u).filter(Boolean);

    expect(tracked).toContain(projectMediaPath("media-sources.json"));
    expect(tracked).toContain(projectMediaPath("PROVENANCE.md"));
    expect(tracked).not.toContain(projectMediaPath("ATTRIBUTION.md"));
    expect(tracked.filter((filename) => /\.(?:mp4|webp)$/iu.test(filename))).toEqual([]);
    await expect(access(path.join(mediaDirectory, "ATTRIBUTION.md"))).rejects.toMatchObject({ code: "ENOENT" });
    await expect(access(path.join(mediaDirectory, "PROVENANCE.md"))).resolves.toBeUndefined();
  });

  it.each([
    ["script.py", Buffer.from(`# ${["down", "load"].join("")} ${["site", "public", "media"].join("/")}`), "operational media download instruction"],
    ["script.ps1", Buffer.from(`Invoke-WebRequest ${["public", "media"].join("/")}`), "operational media download instruction"],
    ["icon.svg", Buffer.from(`<svg><!-- ${["mix", "kit", ".", "co"].join("")} --></svg>`), "legacy provider domain"],
    ["site.webmanifest", Buffer.from(`{"license": "${["mix", "kit Stock Video Free License"].join("")}"}`), "legacy stock-media license"],
  ])("checks UTF-8 text content without relying on %s", (filename, contents, expectedViolation) => {
    expect(provenanceViolations(filename, contents).some(
      (violation) => violation.startsWith(`${filename}: ${expectedViolation}`),
    )).toBe(true);
  });

  it("skips binary buffers instead of attempting to decode them as text", () => {
    const binary = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x00, 0x1a, 0x0a]);

    expect(isTrackedText(binary)).toBe(false);
    expect(provenanceViolations("generated.bin", binary)).toEqual([]);
  });

  it("matches legacy provenance without case sensitivity", () => {
    const filename = "notes.md";
    const contents = Buffer.from([
      ["mix", "kit", ".", "co"].join("").toUpperCase(),
      ["mix", "kit Stock Video Free License"].join("").toUpperCase(),
    ].join("\n"));

    expect(provenanceViolations(filename, contents)).toEqual([
      `${filename}: legacy provider domain ${["mix", "kit", ".", "co"].join("")}`,
      `${filename}: legacy stock-media license`,
    ]);
  });

  it.each([
    "download",
    "retrieve",
    "curl",
    "wget",
    "Invoke-WebRequest",
    "irm",
    "fetch(",
    "http.get",
    "https.get",
    "Readable.fromWeb",
  ])("rejects a %s network instruction even when its media target is distant", (networkToken) => {
    const filename = "instructions.md";
    const contents = Buffer.from(`${networkToken}\n${"context\n".repeat(400)}${["site", "public", "media"].join("/")}`);

    expect(provenanceViolations(filename, contents)).toEqual([
      `${filename}: operational media download instruction`,
    ]);
  });

  it("allows a product report download that does not name a media target", () => {
    expect(provenanceViolations(
      "docs.md",
      Buffer.from("Download the JSON report from the browser for local review."),
    )).toEqual([]);
  });

  it("rejects legacy third-party provenance and media download instructions in tracked text", async () => {
    const { stdout } = await execFileAsync(
      "git",
      ["-c", "safe.directory=*", "ls-files", "-z"],
      {
        cwd: repositoryDirectory,
        shell: false,
        timeout: 30_000,
        windowsHide: true,
      },
    );
    const trackedTextFiles = stdout
      .split("\0")
      .filter(Boolean)
      .filter((filename) => {
        const segments = filename.split("/");
        return !segments.some((segment) => excludedTrackedDirectories.has(segment))
          && !generatedTrackedPaths.some((pattern) => pattern.test(filename));
      });
    const violations = [];

    for (const filename of trackedTextFiles) {
      const contents = await readFile(path.join(repositoryDirectory, filename));
      violations.push(...provenanceViolations(filename, contents));
    }

    expect(violations).toEqual([]);
  });
});
