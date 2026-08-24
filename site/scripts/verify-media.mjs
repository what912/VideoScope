import { execFile } from "node:child_process";
import { readFile, readdir, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

import { auditBuiltRuntimeUrls } from "./build-safety.mjs";
import { validateCaseManifest } from "./case-media-verifier.mjs";
import { validateManifest } from "./media-safety.mjs";
import { verifyOriginalMedia } from "./original-media-verifier.mjs";

const execFileAsync = promisify(execFile);
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const siteDirectory = path.resolve(scriptDirectory, "..");
const defaultMediaDirectory = path.join(siteDirectory, "public", "media");
const defaultDistributionDirectory = path.join(siteDirectory, "dist");
const defaultManifestPath = path.join(
  defaultMediaDirectory,
  "media-sources.json",
);
const defaultCaseManifestPath = path.join(
  siteDirectory,
  "src",
  "data",
  "case-studies.json",
);
const bundleBudgets = Object.freeze({
  initialJavaScript: 450 * 1024,
  initialCss: 45 * 1024,
  largestDeferredJavaScript: 130 * 1024,
  totalJavaScript: 700 * 1024,
});

export function createFfprobeRunner({
  ffprobe = process.env.FFPROBE_PATH || "ffprobe",
  execute = execFileAsync,
} = {}) {
  if (typeof ffprobe !== "string" || ffprobe.length === 0) {
    throw new TypeError("ffprobe executable must be a non-empty string");
  }
  if (typeof execute !== "function") {
    throw new TypeError("ffprobe process runner must be callable");
  }
  return (_requestedExecutable, args, options) =>
    execute(ffprobe, args, options);
}

export async function auditBundleBudgets(
  distributionDirectory = defaultDistributionDirectory,
) {
  const indexPath = path.join(distributionDirectory, "index.html");
  const indexHtml = await readFile(indexPath, "utf8").catch(() => {
    throw new Error(
      "Production build is missing at dist/index.html; run npm run build first.",
    );
  });

  const assetsDirectory = path.join(distributionDirectory, "assets");
  const assetNames = await readdir(assetsDirectory);
  const javascriptAssets = assetNames.filter((name) => name.endsWith(".js"));
  const cssAssets = assetNames.filter((name) => name.endsWith(".css"));
  const initialAssets = new Set(
    [...indexHtml.matchAll(/\/assets\/([^"'?]+\.(?:css|js))/gu)].map(
      (match) => match[1],
    ),
  );

  const sizes = new Map();
  for (const name of [...javascriptAssets, ...cssAssets]) {
    sizes.set(name, (await stat(path.join(assetsDirectory, name))).size);
  }

  const sum = (names) =>
    names.reduce((total, name) => total + (sizes.get(name) ?? 0), 0);
  const initialJavaScript = sum(
    javascriptAssets.filter((name) => initialAssets.has(name)),
  );
  const initialCss = sum(cssAssets.filter((name) => initialAssets.has(name)));
  const largestDeferredJavaScript = Math.max(
    0,
    ...javascriptAssets
      .filter((name) => !initialAssets.has(name))
      .map((name) => sizes.get(name) ?? 0),
  );
  const totalJavaScript = sum(javascriptAssets);
  const measurements = {
    initialJavaScript,
    initialCss,
    largestDeferredJavaScript,
    totalJavaScript,
  };

  for (const [name, size] of Object.entries(measurements)) {
    const budget = bundleBudgets[name];
    if (size > budget) {
      throw new Error(
        `${name} is ${size.toLocaleString("en-US")} bytes; budget is ${budget.toLocaleString("en-US")} bytes`,
      );
    }
  }
  return measurements;
}

export async function auditBuiltMediaOutputs(
  distributionDirectory = defaultDistributionDirectory,
  manifest,
) {
  const expected = manifest.items
    .flatMap((item) => [item.video, item.poster])
    .filter(Boolean)
    .sort((left, right) => left.localeCompare(right, "en"));
  const mediaDirectory = path.join(distributionDirectory, "media");
  let entries;
  try {
    entries = await readdir(mediaDirectory, { withFileTypes: true });
  } catch (error) {
    if (
      typeof error === "object"
      && error !== null
      && "code" in error
      && error.code === "ENOENT"
    ) {
      throw new Error(
        "Production media directory is missing; run npm run build first.",
      );
    }
    throw new Error("Production media directory could not be read.");
  }
  const files = entries
    .filter((entry) => entry.isFile())
    .map((entry) => entry.name)
    .sort((left, right) => left.localeCompare(right, "en"));
  const nonFiles = entries
    .filter((entry) => !entry.isFile())
    .map((entry) => entry.name)
    .sort((left, right) => left.localeCompare(right, "en"));
  const expectedSet = new Set(expected);
  const filesSet = new Set(files);
  const unexpected = files.filter((filename) => !expectedSet.has(filename));
  const missing = expected.filter((filename) => !filesSet.has(filename));

  if (unexpected.length > 0 || missing.length > 0 || nonFiles.length > 0) {
    const details = [
      unexpected.length > 0
        ? "Unexpected deployed media files are present."
        : undefined,
      missing.length > 0
        ? "Required deployed media files are missing."
        : undefined,
      nonFiles.length > 0
        ? "Non-file deployed media entries are present."
        : undefined,
    ].filter(Boolean);
    throw new Error(
      `Built media does not match the exact manifest allowlist. ${details.join(" ")}`,
    );
  }

  return { fileCount: files.length };
}

async function builtCaseFiles(directory, prefix = "") {
  let entries;
  try {
    entries = await readdir(path.join(directory, prefix), { withFileTypes: true });
  } catch (error) {
    if (
      typeof error === "object" && error !== null && "code" in error &&
      error.code === "ENOENT"
    ) {
      throw new Error("Production case directory is missing; run npm run build first.");
    }
    throw new Error("Production case directory could not be read.");
  }
  const files = [];
  for (const entry of entries) {
    const relative = prefix === "" ? entry.name : `${prefix}/${entry.name}`;
    if (entry.isFile()) {
      files.push(relative);
    } else if (entry.isDirectory()) {
      files.push(...await builtCaseFiles(directory, relative));
    } else {
      throw new Error("Built case media does not match the exact manifest allowlist.");
    }
  }
  return files;
}

export async function auditBuiltCaseOutputs(
  distributionDirectory = defaultDistributionDirectory,
  manifest,
) {
  const validated = validateCaseManifest(manifest);
  const expected = validated.cases
    .flatMap((item) => Object.values(item.assets))
    .sort((left, right) => left.localeCompare(right, "en"));
  const files = (await builtCaseFiles(path.join(distributionDirectory, "cases")))
    .sort((left, right) => left.localeCompare(right, "en"));
  const expectedSet = new Set(expected);
  const actualSet = new Set(files);
  if (
    files.length !== expected.length ||
    files.some((file) => !expectedSet.has(file)) ||
    expected.some((file) => !actualSet.has(file))
  ) {
    throw new Error("Built case media does not match the exact manifest allowlist.");
  }
  return { fileCount: files.length };
}

function bundleSummary(measurements) {
  return (
    `Bundle budgets: initial JS ${measurements.initialJavaScript.toLocaleString("en-US")}/${bundleBudgets.initialJavaScript.toLocaleString("en-US")} bytes, ` +
    `initial CSS ${measurements.initialCss.toLocaleString("en-US")}/${bundleBudgets.initialCss.toLocaleString("en-US")} bytes, ` +
    `largest deferred JS ${measurements.largestDeferredJavaScript.toLocaleString("en-US")}/${bundleBudgets.largestDeferredJavaScript.toLocaleString("en-US")} bytes, ` +
    `total JS ${measurements.totalJavaScript.toLocaleString("en-US")}/${bundleBudgets.totalJavaScript.toLocaleString("en-US")} bytes.\n`
  );
}

export async function verifyMedia({
  manifestPath = defaultManifestPath,
  caseManifestPath = defaultCaseManifestPath,
  mediaDirectory = defaultMediaDirectory,
  distributionDirectory = defaultDistributionDirectory,
  runner = createFfprobeRunner(),
  verify = verifyOriginalMedia,
  auditMedia = auditBuiltMediaOutputs,
  auditCaseMedia = auditBuiltCaseOutputs,
  auditRuntime = auditBuiltRuntimeUrls,
  auditBundles = auditBundleBudgets,
  output = process.stdout,
} = {}) {
  if (
    typeof verify !== "function" ||
    typeof auditMedia !== "function" || typeof auditCaseMedia !== "function" ||
    typeof auditRuntime !== "function" ||
    typeof auditBundles !== "function"
  ) {
    throw new TypeError("Media verification dependencies must be callable");
  }

  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  const caseManifest = JSON.parse(await readFile(caseManifestPath, "utf8"));
  validateManifest(manifest);
  validateCaseManifest(caseManifest);
  await verify({ manifest, mediaDirectory, runner });
  const deployedMediaAudit = await auditMedia(distributionDirectory, manifest);
  const deployedCaseAudit = await auditCaseMedia(distributionDirectory, caseManifest);
  const runtimeAudit = await auditRuntime(distributionDirectory);
  const bundleMeasurements = await auditBundles(distributionDirectory);

  if (typeof output?.write === "function") {
    output.write("Verified 15 project-authored media files offline.\n");
    output.write(
      `Audited ${deployedMediaAudit.fileCount} deployed media files against the exact manifest allowlist.\n`,
    );
    output.write(
      `Audited ${deployedCaseAudit.fileCount} deployed case files against the canonical case manifest.\n`,
    );
    output.write(
      `Audited ${runtimeAudit.checkedFiles} built files and ${runtimeAudit.urlCount} documented remote references.\n`,
    );
    output.write(bundleSummary(bundleMeasurements));
  }

  return { deployedMediaAudit, deployedCaseAudit, runtimeAudit, bundleMeasurements };
}

function isDirectExecution() {
  return process.argv[1] !== undefined &&
    path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}

if (isDirectExecution()) {
  await verifyMedia();
}
