import type { ShareClient } from "./contracts";
import { SupabaseShareClient } from "./supabase-share-client";
import { UnavailableShareClient } from "./unavailable-share-client";

export interface ShareEnvironment {
  anonKey?: string;
  enabled?: string;
  url?: string;
}

export function readShareEnvironment(): ShareEnvironment {
  return {
    anonKey: import.meta.env.VITE_SUPABASE_ANON_KEY,
    enabled: import.meta.env.VITE_SUPABASE_SHARE_ENABLED,
    url: import.meta.env.VITE_SUPABASE_URL,
  };
}

export function isShareEnvironmentEnabled(environment: ShareEnvironment) {
  return environment.enabled?.trim().toLowerCase() === "true";
}

export function createShareClient(
  environment: ShareEnvironment = readShareEnvironment(),
): ShareClient {
  const enabled = isShareEnvironmentEnabled(environment);
  const url = environment.url?.trim();
  const anonKey = environment.anonKey?.trim();
  if (!enabled || !url || !anonKey) {
    return new UnavailableShareClient();
  }
  return new SupabaseShareClient({ anonKey, url });
}
