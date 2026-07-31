import type { SupabaseClient } from "@supabase/supabase-js";

import type {
  CreateShareRequest,
  CreateShareResult,
  ShareClient,
} from "./contracts";
import { validateSanitizedSharedReport } from "./validate-shared-report";

interface SupabaseFactory {
  createClient(
    url: string,
    anonKey: string,
    options: {
      auth: {
        autoRefreshToken: boolean;
        detectSessionInUrl: boolean;
        flowType: "pkce";
        persistSession: boolean;
      };
    },
  ): SupabaseClient;
}

type SupabaseLoader = () => Promise<SupabaseFactory>;

interface SupabaseShareClientOptions {
  anonKey: string;
  loadSupabase?: SupabaseLoader;
  now?: () => Date;
  randomId?: () => string;
  url: string;
}

function secureRandomId() {
  if (!globalThis.crypto?.randomUUID) {
    throw new Error("Secure random identifiers are unavailable.");
  }
  return globalThis.crypto.randomUUID();
}

export class SupabaseShareClient implements ShareClient {
  readonly availability = "configured" as const;
  readonly #anonKey: string;
  readonly #loadSupabase: SupabaseLoader;
  readonly #now: () => Date;
  readonly #randomId: () => string;
  readonly #url: string;
  #clientPromise?: Promise<SupabaseClient>;

  constructor({
    anonKey,
    loadSupabase = async () => {
      const { createClient } = await import("@supabase/supabase-js");
      return { createClient };
    },
    now = () => new Date(),
    randomId = secureRandomId,
    url,
  }: SupabaseShareClientOptions) {
    this.#anonKey = anonKey;
    this.#loadSupabase = loadSupabase;
    this.#now = now;
    this.#randomId = randomId;
    this.#url = url;
  }

  async createShare(
    request: CreateShareRequest,
  ): Promise<CreateShareResult> {
    const publicId = this.#randomId();
    const createdAt = this.#now().toISOString();
    const client = await this.#getClient();
    const { error } = await client.from("shared_reports").insert({
      created_at: createdAt,
      expires_at: request.expiresAt ?? null,
      owner_id: request.ownerId,
      public_id: publicId,
      report_json: request.report,
      revoked_at: null,
    });
    if (error) throw error;
    return {
      createdAt,
      ...(request.expiresAt ? { expiresAt: request.expiresAt } : {}),
      publicId,
    };
  }

  async getSharedReport(publicId: string) {
    const client = await this.#getClient();
    const { data, error } = await client.rpc("get_shared_report", {
      requested_public_id: publicId,
    });
    if (error) throw error;
    if (!Array.isArray(data) || data.length === 0) return null;
    const row = data[0] as { report_json?: unknown };
    return validateSanitizedSharedReport(row.report_json);
  }

  async revokeShare(publicId: string) {
    const client = await this.#getClient();
    const { data, error } = await client
      .from("shared_reports")
      .update({ revoked_at: this.#now().toISOString() })
      .eq("public_id", publicId)
      .select("public_id");
    if (error) throw error;
    if (!Array.isArray(data) || data.length !== 1) {
      throw new Error("Share link was not found or not owned by this account.");
    }
  }

  #getClient() {
    this.#clientPromise ??= this.#loadSupabase().then(({ createClient }) =>
      createClient(this.#url, this.#anonKey, {
        auth: {
          autoRefreshToken: true,
          detectSessionInUrl: false,
          flowType: "pkce",
          persistSession: true,
        },
      }),
    );
    return this.#clientPromise;
  }
}
