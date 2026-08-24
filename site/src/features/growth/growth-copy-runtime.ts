import type { Locale } from "../../i18n/types";

export function publicFunnelCopyUrlFor(baseUrl: string): string {
  return `${baseUrl.endsWith("/") ? baseUrl : `${baseUrl}/`}growth-home-copy.json`;
}

export const publicFunnelCopyUrl = publicFunnelCopyUrlFor(import.meta.env.BASE_URL);

export interface PageCopy {
  readonly eyebrow: string;
  readonly title: string;
  readonly description: string;
  readonly action: string;
}

export interface GrowthCopy {
  readonly positioning: string;
  readonly sourcePreserved: string;
  readonly localBoundary: string;
  readonly caseEvidence: {
    readonly provenance: string;
    readonly source: string;
    readonly actions: string;
    readonly verification: string;
    readonly verificationStatus: string;
    readonly limitations: string;
  };
  readonly pages: Record<
    "rescue" | "examples" | "caseStudy" | "download" | "developers" | "roadmap" | "community" | "missingCase",
    PageCopy
  >;
}

export interface HomeCopy {
  readonly uploadAtmosphere: string;
  readonly hero: {
    readonly eyebrow: string;
    readonly quickCheck: string;
    readonly examples: string;
    readonly media: string;
    readonly primaryAction: string;
  };
  readonly finalCta: PageCopy;
  readonly cases: {
    readonly loading: string;
    readonly unavailable: string;
    readonly casesEyebrow: string;
    readonly casesTitle: string;
    readonly casesAction: string;
  };
  readonly comparison: {
    readonly eyebrow: string;
    readonly before: string;
    readonly after: string;
    readonly position: string;
    readonly play: string;
    readonly pause: string;
    readonly authored: string;
    readonly limitations: string;
    readonly verification: string;
    readonly range: string;
  };
  readonly funnel: {
    readonly journeyEyebrow: string;
    readonly journeyTitle: string;
    readonly journey: readonly { readonly title: string; readonly description: string }[];
    readonly boundaryTitle: string;
    readonly boundaryDescription: string;
    readonly developerTitle: string;
    readonly developerDescription: string;
    readonly developerAction: string;
    readonly star: string;
  };
}

export type PublicFunnelLocaleCopy = GrowthCopy & { readonly home: HomeCopy };

export type PublicFunnelCopy = Record<Locale, PublicFunnelLocaleCopy>;

type UnknownRecord = Record<string, unknown>;
type CopySchemaObject = { readonly [key: string]: CopySchema };
type CopySchema = true | CopySchemaObject | readonly [CopySchema, number];

const text = true as const;
const pageSchema = { eyebrow: text, title: text, description: text, action: text };
const localeSchema = {
  positioning: text,
  sourcePreserved: text,
  localBoundary: text,
  caseEvidence: {
    provenance: text,
    source: text,
    actions: text,
    verification: text,
    verificationStatus: text,
    limitations: text,
  },
  pages: {
    rescue: pageSchema,
    examples: pageSchema,
    caseStudy: pageSchema,
    download: pageSchema,
    developers: pageSchema,
    roadmap: pageSchema,
    community: pageSchema,
    missingCase: pageSchema,
  },
  home: {
    uploadAtmosphere: text,
    hero: { eyebrow: text, quickCheck: text, examples: text, media: text, primaryAction: text },
    finalCta: pageSchema,
    cases: { loading: text, unavailable: text, casesEyebrow: text, casesTitle: text, casesAction: text },
    comparison: {
      eyebrow: text,
      before: text,
      after: text,
      position: text,
      play: text,
      pause: text,
      authored: text,
      limitations: text,
      verification: text,
      range: text,
    },
    funnel: {
      journeyEyebrow: text,
      journeyTitle: text,
      journey: [{ title: text, description: text }, 3],
      boundaryTitle: text,
      boundaryDescription: text,
      developerTitle: text,
      developerDescription: text,
      developerAction: text,
      star: text,
    },
  },
} as const satisfies Exclude<CopySchema, true | readonly unknown[]>;
const publicFunnelSchema = { en: localeSchema, "zh-CN": localeSchema } as const;

function isPlainRecord(value: unknown): value is UnknownRecord {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function requireRecord(value: unknown, path: string): UnknownRecord {
  if (!isPlainRecord(value)) {
    throw new Error(`Public funnel copy must use JSON objects at ${path}`);
  }
  return value;
}

function isArraySchema(schema: CopySchema): schema is readonly [CopySchema, number] {
  return Array.isArray(schema);
}

function validate(value: unknown, schema: CopySchema, path: string): unknown {
  if (schema === true) {
    if (typeof value !== "string" || value.trim() === "") {
      throw new Error(`Public funnel copy requires non-empty text at ${path}`);
    }
    return value;
  }
  if (isArraySchema(schema)) {
    if (!Array.isArray(value) || value.length !== schema[1]) {
      throw new Error(`Public funnel copy requires ${schema[1]} entries at ${path}`);
    }
    return value.map((item, index) => validate(item, schema[0], `${path}.${index}`));
  }
  const objectSchema = schema as CopySchemaObject;
  const record = requireRecord(value, path);
  const keys = Object.keys(objectSchema);
  if (keys.some((key) => !Object.hasOwn(record, key))) {
    throw new Error(`Public funnel copy has missing keys at ${path}`);
  }
  if (Object.keys(record).some((key) => !Object.hasOwn(objectSchema, key))) {
    throw new Error(`Public funnel copy has unknown keys at ${path}`);
  }
  return Object.fromEntries(keys.map((key) => [key, validate(record[key], objectSchema[key], `${path}.${key}`)]));
}

export function validatePublicFunnelCopy(value: unknown): PublicFunnelCopy {
  const root = requireRecord(value, "root");
  const keys = Object.keys(publicFunnelSchema);
  if (keys.some((key) => !Object.hasOwn(root, key))) {
    throw new Error("Public funnel copy has missing keys at root");
  }
  if (Object.keys(root).some((key) => !Object.hasOwn(publicFunnelSchema, key))) {
    throw new Error("Public funnel copy has unknown keys at root");
  }
  return {
    en: validate(root.en, localeSchema, "en") as PublicFunnelLocaleCopy,
    "zh-CN": validate(root["zh-CN"], localeSchema, "zh-CN") as PublicFunnelLocaleCopy,
  };
}

export async function loadPublicFunnelCopy(
  request: typeof fetch = fetch,
  baseUrl = import.meta.env.BASE_URL,
): Promise<PublicFunnelCopy> {
  const response = await request(publicFunnelCopyUrlFor(baseUrl));
  if (!response.ok) {
    throw new Error("The local public funnel copy could not be loaded.");
  }
  return validatePublicFunnelCopy(await response.json());
}
