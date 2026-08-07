import path from "node:path";

export const GENERATED_MEDIA_LICENSE = "Apache-2.0 project-authored media";
export const MEDIA_ROLES = Object.freeze([
  "hero",
  "product-proof",
  "upload-lab",
  "diagnosis",
  "compare-a",
  "compare-b",
  "evidence-a",
  "evidence-b",
  "evidence-c",
]);
export const SCENE_IDS = Object.freeze([
  "optical-aperture",
  "night-observation-grid",
  "fluid-spectrum",
  "diagnostic-mesh",
  "cool-topography",
  "dawn-spectrum",
  "cyan-caustic",
  "violet-lattice",
  "amber-contour",
]);

const EXPECTED_OUTPUTS = Object.freeze([
  ["hero-optical.mp4", "hero-optical.webp"],
  ["city-nightlife.mp4", "city-nightlife.webp"],
  ["upload-liquid.mp4", "upload-liquid.webp"],
  ["diagnosis-fashion.mp4", "diagnosis-fashion.webp"],
  ["compare-hills.mp4", "compare-hills.webp"],
  ["compare-sunrise.mp4", "compare-sunrise.webp"],
  [undefined, "evidence-lake.webp"],
  [undefined, "evidence-city.webp"],
  [undefined, "evidence-studio.webp"],
]);
const FORBIDDEN_FIELDS = Object.freeze([
  "sourcePage",
  "downloadUrl",
  "provider",
  "downloadDate",
]);
const GENERATION_SETTINGS = Object.freeze({
  workingWidth: 1280,
  workingHeight: 720,
  outputWidth: 1280,
  outputHeight: 720,
  frameRate: 24,
});
const VIDEO_ITEM_COUNT = 6;

function requireSafeFilename(value, role, field) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    path.basename(value) !== value
  ) {
    throw new Error(`Media role ${role} has an unsafe ${field} filename`);
  }
}

function requireGenerationSettings(item) {
  for (const [field, expected] of Object.entries(GENERATION_SETTINGS)) {
    if (!Number.isFinite(item[field]) || item[field] !== expected) {
      throw new Error(
        `Media role ${item.role} has an unsupported generation setting ${field}`,
      );
    }
  }
}

function addUniqueFilename(filenames, filename) {
  if (filenames.has(filename)) {
    throw new Error(`Media filename is reused: ${filename}`);
  }
  filenames.add(filename);
}

export function validateManifest(manifest) {
  if (
    typeof manifest !== "object" ||
    manifest === null ||
    manifest.schemaVersion !== 1 ||
    manifest.generatorVersion !== "1.0.0" ||
    manifest.license !== GENERATED_MEDIA_LICENSE ||
    !Array.isArray(manifest.items) ||
    manifest.items.length !== MEDIA_ROLES.length
  ) {
    throw new Error("media-sources.json does not define the nine approved items");
  }

  const filenames = new Set();
  for (const [index, item] of manifest.items.entries()) {
    if (typeof item !== "object" || item === null) {
      throw new Error("media-sources.json contains an invalid media item");
    }
    for (const field of FORBIDDEN_FIELDS) {
      if (Object.hasOwn(item, field)) {
        throw new Error(
          `Media role ${String(item.role)} includes forbidden field ${field}`,
        );
      }
    }

    if (item.role !== MEDIA_ROLES[index]) {
      throw new Error("media-sources.json has an invalid role ordering");
    }
    if (item.scene !== SCENE_IDS[index]) {
      throw new Error(`Media role ${item.role} has an invalid scene identifier`);
    }
    requireGenerationSettings(item);

    const [expectedVideo, expectedPoster] = EXPECTED_OUTPUTS[index];
    requireSafeFilename(item.poster, item.role, "poster");
    if (!item.poster.endsWith(".webp")) {
      throw new Error(`Media role ${item.role} must declare a WebP poster`);
    }
    addUniqueFilename(filenames, item.poster);
    if (item.poster !== expectedPoster) {
      throw new Error(`Media role ${item.role} has an unexpected poster filename`);
    }

    if (index < VIDEO_ITEM_COUNT) {
      requireSafeFilename(item.video, item.role, "video");
      if (!item.video.endsWith(".mp4")) {
        throw new Error(`Media role ${item.role} must declare an MP4 video`);
      }
      addUniqueFilename(filenames, item.video);
      if (item.video !== expectedVideo) {
        throw new Error(`Media role ${item.role} has an unexpected video filename`);
      }
      if (
        !Number.isFinite(item.durationSeconds) ||
        item.durationSeconds < 6 ||
        item.durationSeconds > 8
      ) {
        throw new Error(
          `Media role ${item.role} has an unsupported generation setting durationSeconds`,
        );
      }
      if (
        !Number.isFinite(item.posterTimestampSeconds) ||
        item.posterTimestampSeconds < 0 ||
        item.posterTimestampSeconds >= item.durationSeconds
      ) {
        throw new Error(`Media role ${item.role} has an invalid poster timestamp`);
      }
    } else {
      if (
        Object.hasOwn(item, "video") ||
        Object.hasOwn(item, "durationSeconds") ||
        Object.hasOwn(item, "posterTimestampSeconds")
      ) {
        throw new Error(`Evidence role ${item.role} must not declare a video output`);
      }
      if (!Number.isFinite(item.stillTimeSeconds) || item.stillTimeSeconds !== 2) {
        throw new Error(
          `Evidence role ${item.role} has an unsupported still render time`,
        );
      }
    }
  }

  if (filenames.size !== 15) {
    throw new Error("Original media manifest must declare 15 unique filenames");
  }
}

export async function runWithIndependentCleanup(operation, cleanups) {
  let operationResult;
  let operationError;
  let operationFailed = false;

  try {
    operationResult = await operation();
  } catch (error) {
    operationFailed = true;
    operationError = error;
  }

  const cleanupResults = await Promise.allSettled(
    cleanups.map((cleanup) => Promise.resolve().then(cleanup)),
  );
  const cleanupErrors = cleanupResults
    .filter((result) => result.status === "rejected")
    .map((result) => result.reason);

  if (operationFailed && cleanupErrors.length > 0) {
    throw new AggregateError(
      [operationError, ...cleanupErrors],
      "Media preparation failed and cleanup was incomplete.",
      { cause: operationError },
    );
  }
  if (operationFailed) {
    throw operationError;
  }
  if (cleanupErrors.length > 0) {
    throw new AggregateError(
      cleanupErrors,
      "Media preparation cleanup was incomplete.",
    );
  }

  return operationResult;
}
