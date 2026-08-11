import type { Locale } from "../i18n/types";

import rawManifest from "./case-studies.json";

export type CaseProvenance =
  | "project-authored"
  | "user-authorized"
  | "synthetic-regression";
export type LocalizedCaseText = Readonly<{ [key in Locale]: string }>;
export type PublicCaseStatus = "completed" | "needs_review" | "partial" | "failed";
export type PublicVerificationStatus = "passed" | "needs_review" | "failed";

export interface CaseAction {
  readonly workflow: "video-rescue" | "publish-ready";
  readonly actionId: string;
  readonly version: string;
  readonly kind: string;
  readonly description: LocalizedCaseText;
  readonly parameters: Readonly<Record<string, string | number | boolean | null>>;
}

export interface CaseVerificationCheck {
  readonly checkId: string;
  readonly status: PublicVerificationStatus;
  readonly summary: LocalizedCaseText;
  readonly measured: Readonly<Record<string, string | number | boolean | null>>;
}

export interface CaseStudy {
  readonly id: string;
  readonly slug: string;
  readonly featured: boolean;
  readonly provenance: CaseProvenance;
  readonly authorizationSummary: LocalizedCaseText;
  readonly title: LocalizedCaseText;
  readonly summary: LocalizedCaseText;
  readonly observableSymptom: LocalizedCaseText;
  readonly actions: readonly CaseAction[];
  readonly unresolved: readonly LocalizedCaseText[];
  readonly limitations: readonly LocalizedCaseText[];
  readonly comparison: Readonly<{ startSeconds: number; endSeconds: number }>;
  readonly media: Readonly<{
    durationSeconds: number;
    width: number;
    height: number;
    frameRate: number;
  }>;
  readonly versions: Readonly<{
    videoscope: string;
    ffmpeg: string;
    platform: string;
    configuration: string;
  }>;
  readonly verification: Readonly<{
    status: PublicCaseStatus;
    checks: readonly CaseVerificationCheck[];
  }>;
  readonly assets: Readonly<{
    beforeVideo: string;
    afterVideo: string;
    poster: string;
    publicReport: string;
  }>;
  readonly sha256: Readonly<{
    beforeVideo: string;
    afterVideo: string;
    poster: string;
    publicReport: string;
  }>;
  readonly reproduction: readonly string[];
}

export interface CaseStudyManifest {
  readonly schemaVersion: 1;
  readonly generatedBy: string;
  readonly cases: readonly CaseStudy[];
}

type ScalarValue = string | number | boolean | null;
type UnknownRecord = Record<string, unknown>;

const SAFE_CASE_ASSET_PATH =
  /^\/VideoScope\/cases\/[A-Za-z0-9][A-Za-z0-9._-]*(?:\/[A-Za-z0-9][A-Za-z0-9._-]*)*$/;
const SHA256 = /^[a-f0-9]{64}$/;
const SYNTHETIC_PROVENANCE_CLAIM = /\breal user\b|真实用户/i;

function fail(message: string): never {
  throw new Error(message);
}

function assertRecord(value: unknown, message: string): UnknownRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) fail(message);
  return value as UnknownRecord;
}

function assertExactKeys(
  value: unknown,
  keys: readonly string[],
  objectName: string,
): UnknownRecord {
  const record = assertRecord(value, `${objectName} must be an object`);
  const allowed = new Set(keys);

  if (Object.keys(record).some((key) => !allowed.has(key))) {
    fail("Case study manifest contains unknown keys");
  }
  if (keys.some((key) => !(key in record))) fail(`${objectName} has missing keys`);
  return record;
}

function assertNonEmptyString(value: unknown, message: string): string {
  if (typeof value !== "string" || value.trim() === "") fail(message);
  return value;
}

function assertBoolean(value: unknown, message: string): boolean {
  if (typeof value !== "boolean") fail(message);
  return value;
}

function assertFiniteNumber(value: unknown, message: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) fail(message);
  return value;
}

function assertPositiveNumber(value: unknown, message: string): number {
  const number = assertFiniteNumber(value, message);
  if (number <= 0) fail(message);
  return number;
}

function assertPositiveInteger(value: unknown, message: string): number {
  const number = assertPositiveNumber(value, message);
  if (!Number.isInteger(number)) fail(message);
  return number;
}

function assertEnum<Value extends string>(
  value: unknown,
  allowed: readonly Value[],
  message: string,
): Value {
  if (typeof value !== "string" || !allowed.includes(value as Value)) fail(message);
  return value as Value;
}

function parseLocalizedCaseText(value: unknown): LocalizedCaseText {
  const record = assertExactKeys(value, ["en", "zh-CN"], "Case bilingual copy");
  return {
    en: assertNonEmptyString(record.en, "Case bilingual copy must be non-empty"),
    "zh-CN": assertNonEmptyString(
      record["zh-CN"],
      "Case bilingual copy must be non-empty",
    ),
  };
}

function parseScalarRecord(value: unknown, objectName: string): Readonly<Record<string, ScalarValue>> {
  const record = assertRecord(value, `${objectName} must be an object`);
  for (const item of Object.values(record)) {
    if (
      item !== null &&
      typeof item !== "string" &&
      typeof item !== "boolean" &&
      (typeof item !== "number" || !Number.isFinite(item))
    ) {
      fail(`${objectName} values must be JSON scalars`);
    }
  }
  return record as Readonly<Record<string, ScalarValue>>;
}

function parseAction(value: unknown, copy: LocalizedCaseText[]): CaseAction {
  const record = assertExactKeys(
    value,
    ["workflow", "actionId", "version", "kind", "description", "parameters"],
    "Case action",
  );
  const description = parseLocalizedCaseText(record.description);
  copy.push(description);
  return {
    workflow: assertEnum(record.workflow, ["video-rescue", "publish-ready"], "Invalid case workflow"),
    actionId: assertNonEmptyString(record.actionId, "Case action ID must be non-empty"),
    version: assertNonEmptyString(record.version, "Case action version must be non-empty"),
    kind: assertNonEmptyString(record.kind, "Case action kind must be non-empty"),
    description,
    parameters: parseScalarRecord(record.parameters, "Case action parameters"),
  };
}

function parseVerificationCheck(value: unknown, copy: LocalizedCaseText[]): CaseVerificationCheck {
  const record = assertExactKeys(
    value,
    ["checkId", "status", "summary", "measured"],
    "Case verification check",
  );
  const summary = parseLocalizedCaseText(record.summary);
  copy.push(summary);
  return {
    checkId: assertNonEmptyString(record.checkId, "Case verification check ID must be non-empty"),
    status: assertEnum(
      record.status,
      ["passed", "needs_review", "failed"],
      "Invalid case verification check status",
    ),
    summary,
    measured: parseScalarRecord(record.measured, "Case verification measured values"),
  };
}

function parseAssetPaths(value: unknown): CaseStudy["assets"] {
  const record = assertExactKeys(
    value,
    ["beforeVideo", "afterVideo", "poster", "publicReport"],
    "Case assets",
  );
  const assets = {
    beforeVideo: assertNonEmptyString(record.beforeVideo, "Case asset path must be non-empty"),
    afterVideo: assertNonEmptyString(record.afterVideo, "Case asset path must be non-empty"),
    poster: assertNonEmptyString(record.poster, "Case asset path must be non-empty"),
    publicReport: assertNonEmptyString(record.publicReport, "Case asset path must be non-empty"),
  };
  if (!Object.values(assets).every((path) => SAFE_CASE_ASSET_PATH.test(path))) {
    fail("Case assets must use safe /VideoScope/cases paths");
  }
  return assets;
}

function parseHashes(value: unknown): CaseStudy["sha256"] {
  const record = assertExactKeys(
    value,
    ["beforeVideo", "afterVideo", "poster", "publicReport"],
    "Case hashes",
  );
  const hashes = {
    beforeVideo: assertNonEmptyString(record.beforeVideo, "Case hash must be non-empty"),
    afterVideo: assertNonEmptyString(record.afterVideo, "Case hash must be non-empty"),
    poster: assertNonEmptyString(record.poster, "Case hash must be non-empty"),
    publicReport: assertNonEmptyString(record.publicReport, "Case hash must be non-empty"),
  };
  if (!Object.values(hashes).every((hash) => SHA256.test(hash))) {
    fail("Case hashes must be lowercase SHA-256 values");
  }
  return hashes;
}

function parseCaseStudy(value: unknown): CaseStudy {
  const record = assertExactKeys(
    value,
    [
      "id",
      "slug",
      "featured",
      "provenance",
      "authorizationSummary",
      "title",
      "summary",
      "observableSymptom",
      "actions",
      "unresolved",
      "limitations",
      "comparison",
      "media",
      "versions",
      "verification",
      "assets",
      "sha256",
      "reproduction",
    ],
    "Case study",
  );
  const copy: LocalizedCaseText[] = [];
  const authorizationSummary = parseLocalizedCaseText(record.authorizationSummary);
  const title = parseLocalizedCaseText(record.title);
  const summary = parseLocalizedCaseText(record.summary);
  const observableSymptom = parseLocalizedCaseText(record.observableSymptom);
  copy.push(authorizationSummary, title, summary, observableSymptom);

  if (!Array.isArray(record.actions)) fail("Case actions must be an array");
  const actions = record.actions.map((item) => parseAction(item, copy));
  if (!Array.isArray(record.unresolved)) fail("Case unresolved copy must be an array");
  const unresolved = record.unresolved.map((item) => {
    const text = parseLocalizedCaseText(item);
    copy.push(text);
    return text;
  });
  if (!Array.isArray(record.limitations)) fail("Case limitations must be an array");
  const limitations = record.limitations.map((item) => {
    const text = parseLocalizedCaseText(item);
    copy.push(text);
    return text;
  });

  const comparisonRecord = assertExactKeys(
    record.comparison,
    ["startSeconds", "endSeconds"],
    "Case comparison",
  );
  const comparison = {
    startSeconds: assertFiniteNumber(
      comparisonRecord.startSeconds,
      "Case comparison must be a positive range within the media duration",
    ),
    endSeconds: assertFiniteNumber(
      comparisonRecord.endSeconds,
      "Case comparison must be a positive range within the media duration",
    ),
  };
  const mediaRecord = assertExactKeys(
    record.media,
    ["durationSeconds", "width", "height", "frameRate"],
    "Case media",
  );
  const media = {
    durationSeconds: assertPositiveNumber(mediaRecord.durationSeconds, "Case media duration must be positive"),
    width: assertPositiveInteger(mediaRecord.width, "Case media dimensions must be positive integers"),
    height: assertPositiveInteger(mediaRecord.height, "Case media dimensions must be positive integers"),
    frameRate: assertPositiveNumber(mediaRecord.frameRate, "Case media frame rate must be positive"),
  };
  if (
    comparison.startSeconds < 0 ||
    comparison.endSeconds <= comparison.startSeconds ||
    comparison.endSeconds > media.durationSeconds
  ) {
    fail("Case comparison must be a positive range within the media duration");
  }

  const versionsRecord = assertExactKeys(
    record.versions,
    ["videoscope", "ffmpeg", "platform", "configuration"],
    "Case versions",
  );
  const versions = {
    videoscope: assertNonEmptyString(versionsRecord.videoscope, "Case version must be non-empty"),
    ffmpeg: assertNonEmptyString(versionsRecord.ffmpeg, "Case version must be non-empty"),
    platform: assertNonEmptyString(versionsRecord.platform, "Case version must be non-empty"),
    configuration: assertNonEmptyString(versionsRecord.configuration, "Case version must be non-empty"),
  };

  const verificationRecord = assertExactKeys(
    record.verification,
    ["status", "checks"],
    "Case verification",
  );
  if (!Array.isArray(verificationRecord.checks)) fail("Case verification checks must be an array");
  const verification = {
    status: assertEnum(
      verificationRecord.status,
      ["completed", "needs_review", "partial", "failed"],
      "Invalid case verification status",
    ),
    checks: verificationRecord.checks.map((item) => parseVerificationCheck(item, copy)),
  };
  const featured = assertBoolean(record.featured, "Case featured must be a boolean");
  if (featured && verification.status !== "completed") {
    fail("Featured cases must have completed verification");
  }

  const provenance = assertEnum(
    record.provenance,
    ["project-authored", "user-authorized", "synthetic-regression"],
    "Invalid case provenance",
  );
  if (
    provenance === "synthetic-regression" &&
    copy.some((text) => SYNTHETIC_PROVENANCE_CLAIM.test(`${text.en}\n${text["zh-CN"]}`))
  ) {
    fail("Synthetic regression cases cannot claim real-user provenance");
  }

  if (!Array.isArray(record.reproduction)) fail("Case reproduction must be an array");
  const reproduction = record.reproduction.map((step) =>
    assertNonEmptyString(step, "Case reproduction steps must be non-empty"),
  );

  return {
    id: assertNonEmptyString(record.id, "Case ID must be non-empty"),
    slug: assertNonEmptyString(record.slug, "Case slug must be non-empty"),
    featured,
    provenance,
    authorizationSummary,
    title,
    summary,
    observableSymptom,
    actions,
    unresolved,
    limitations,
    comparison,
    media,
    versions,
    verification,
    assets: parseAssetPaths(record.assets),
    sha256: parseHashes(record.sha256),
    reproduction,
  };
}

export function validateCaseStudyManifest(value: unknown): CaseStudyManifest {
  const record = assertExactKeys(value, ["schemaVersion", "generatedBy", "cases"], "Case study manifest");
  if (record.schemaVersion !== 1) fail("Unsupported case study manifest schema version");
  if (!Array.isArray(record.cases)) fail("Case study manifest cases must be an array");

  const cases = record.cases.map(parseCaseStudy);
  const ids = new Set<string>();
  const slugs = new Set<string>();
  for (const item of cases) {
    if (ids.has(item.id) || slugs.has(item.slug)) fail("Case IDs and slugs must be unique");
    ids.add(item.id);
    slugs.add(item.slug);
  }

  return {
    schemaVersion: 1,
    generatedBy: assertNonEmptyString(record.generatedBy, "Case manifest generator must be non-empty"),
    cases,
  };
}

export const caseStudyManifest = validateCaseStudyManifest(rawManifest);
export const featuredCaseStudies = caseStudyManifest.cases.filter(
  (item) => item.featured && item.verification.status === "completed",
);

export function findCaseStudy(slug: string): CaseStudy | undefined {
  return caseStudyManifest.cases.find((item) => item.slug === slug);
}
