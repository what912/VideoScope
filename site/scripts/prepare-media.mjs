import { readFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

import { validateManifest } from "./media-safety.mjs";
import { generateOriginalMedia } from "./original-media-generator.mjs";

const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const siteDirectory = path.resolve(scriptDirectory, "..");
const defaultMediaDirectory = path.join(siteDirectory, "public", "media");
const defaultManifestPath = path.join(
  defaultMediaDirectory,
  "media-sources.json",
);

export async function prepareOriginalMedia({
  manifestPath = defaultManifestPath,
  mediaDirectory = defaultMediaDirectory,
  runner,
  ffmpeg = process.env.FFMPEG_PATH || "ffmpeg",
  generate = generateOriginalMedia,
} = {}) {
  if (typeof generate !== "function") {
    throw new TypeError("Original media generator must be callable");
  }

  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  validateManifest(manifest);
  return generate({
    items: manifest.items,
    mediaDirectory,
    runner,
    ffmpeg,
  });
}

function isDirectExecution() {
  return process.argv[1] !== undefined &&
    path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
}

if (isDirectExecution()) {
  await prepareOriginalMedia();
  process.stdout.write("Generated 15 project-authored media files offline.\n");
}
