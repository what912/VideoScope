import type {
  AuthChangeEvent,
  Session,
  SupabaseClient,
} from "@supabase/supabase-js";

import type { AuthClient, AuthSession } from "../../types/auth";

type SupabaseModule = typeof import("@supabase/supabase-js");
type SupabaseLoader = () => Promise<SupabaseModule>;

interface SupabaseAuthClientOptions {
  anonKey: string;
  loadSupabase?: SupabaseLoader;
  url: string;
}

function toAuthSession(session: Session | null): AuthSession | null {
  if (!session) {
    return null;
  }

  const metadata = session.user.user_metadata;
  return {
    expiresAt: session.expires_at,
    user: {
      avatarUrl:
        typeof metadata.avatar_url === "string" ? metadata.avatar_url : undefined,
      displayName:
        typeof metadata.full_name === "string" ? metadata.full_name : undefined,
      email: session.user.email,
      id: session.user.id,
    },
  };
}

function throwAuthError(response: { error: Error | null }) {
  if (response.error) {
    throw response.error;
  }
}

export class SupabaseAuthClient implements AuthClient {
  readonly #anonKey: string;
  readonly #loadSupabase: SupabaseLoader;
  readonly #url: string;
  #clientPromise?: Promise<SupabaseClient>;

  constructor({
    anonKey,
    loadSupabase = () => import("@supabase/supabase-js"),
    url,
  }: SupabaseAuthClientOptions) {
    this.#anonKey = anonKey;
    this.#loadSupabase = loadSupabase;
    this.#url = url;
  }

  async getSession(): Promise<AuthSession | null> {
    const client = await this.#getClient();
    const { data, error } = await client.auth.getSession();
    if (error) {
      throw error;
    }
    return toAuthSession(data.session);
  }

  onSessionChange(callback: (session: AuthSession | null) => void) {
    let active = true;
    let unsubscribe: () => void = () => undefined;

    void this.#getClient()
      .then((client) => {
        if (!active) {
          return;
        }
        const listener = client.auth.onAuthStateChange(
          (_event: AuthChangeEvent, session: Session | null) => {
            callback(toAuthSession(session));
          },
        );
        unsubscribe = () => listener.data.subscription.unsubscribe();
      })
      .catch(() => {
        // getSession reports provider initialization failures to AuthProvider.
      });

    return () => {
      active = false;
      unsubscribe();
    };
  }

  async signInWithMagicLink(
    email: string,
    redirectTo: string,
  ): Promise<void> {
    const client = await this.#getClient();
    const response = await client.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: redirectTo },
    });
    throwAuthError(response);
  }

  async signInWithGitHub(redirectTo: string): Promise<void> {
    const client = await this.#getClient();
    const response = await client.auth.signInWithOAuth({
      provider: "github",
      options: { redirectTo },
    });
    throwAuthError(response);
  }

  async completeCallback(url: URL): Promise<void> {
    const providerError = url.searchParams.get("error_description");
    if (providerError) {
      throw new Error("Authentication callback was rejected.");
    }

    const code = url.searchParams.get("code");
    if (!code) {
      throw new Error("Authentication callback code is missing.");
    }

    const client = await this.#getClient();
    const response = await client.auth.exchangeCodeForSession(code);
    throwAuthError(response);
  }

  async signOut(): Promise<void> {
    const client = await this.#getClient();
    const response = await client.auth.signOut();
    if (response.error) {
      throw response.error;
    }
  }

  #getClient(): Promise<SupabaseClient> {
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
