import type { AuthClient } from "../../types/auth";
import { SupabaseAuthClient } from "./supabase-auth-client";
import { UnavailableAuthClient } from "./unavailable-auth-client";

export interface AuthEnvironment {
  anonKey?: string;
  url?: string;
}

export function readAuthEnvironment(): AuthEnvironment {
  return {
    anonKey: import.meta.env.VITE_SUPABASE_ANON_KEY,
    url: import.meta.env.VITE_SUPABASE_URL,
  };
}

export function createAuthClient(
  environment: AuthEnvironment = readAuthEnvironment(),
): AuthClient {
  const url = environment.url?.trim();
  const anonKey = environment.anonKey?.trim();
  if (!url || !anonKey) {
    return new UnavailableAuthClient();
  }
  return new SupabaseAuthClient({ anonKey, url });
}
