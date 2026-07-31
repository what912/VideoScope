import type { Session, SupabaseClient } from "@supabase/supabase-js";
import { describe, expect, it } from "vitest";

import { SupabaseAuthClient } from "../../services/auth";

function createAdapterHarness() {
  let loadCount = 0;
  let unsubscribeCount = 0;
  const createCalls: unknown[][] = [];
  const magicLinkRequests: unknown[] = [];
  const oauthRequests: unknown[] = [];
  const callbackCodes: string[] = [];
  let signOutCount = 0;

  const session: Session = {
    access_token: "test-access-token",
    expires_in: 3_600,
    expires_at: 2_000_000_000,
    refresh_token: "test-refresh-token",
    token_type: "bearer",
    user: {
      app_metadata: {
        provider: "email",
        providers: ["email"],
      },
      aud: "authenticated",
      created_at: "2033-05-18T03:33:20.000Z",
      email: "researcher@example.test",
      id: "user-123",
      role: "authenticated",
      updated_at: "2033-05-18T03:33:20.000Z",
      user_metadata: {
        avatar_url: "https://avatars.example/researcher.png",
        full_name: "Video Researcher",
      },
    },
  };

  const auth = {
    exchangeCodeForSession: async (code: string) => {
      callbackCodes.push(code);
      return { data: { session, user: session.user }, error: null };
    },
    getSession: async () => ({ data: { session }, error: null }),
    onAuthStateChange: () => ({
      data: {
        subscription: {
          unsubscribe: () => {
            unsubscribeCount += 1;
          },
        },
      },
    }),
    signInWithOAuth: async (request: unknown) => {
      oauthRequests.push(request);
      return {
        data: { provider: "github", url: "https://provider.example" },
        error: null,
      };
    },
    signInWithOtp: async (request: unknown) => {
      magicLinkRequests.push(request);
      return { data: { messageId: "message" }, error: null };
    },
    signOut: async () => {
      signOutCount += 1;
      return { error: null };
    },
  };

  const loadSupabase = async () => {
    loadCount += 1;
    return {
      createClient: (...args: unknown[]) => {
        createCalls.push(args);
        return { auth } as unknown as SupabaseClient;
      },
    } as unknown as typeof import("@supabase/supabase-js");
  };

  return {
    callbackCodes,
    client: new SupabaseAuthClient({
      anonKey: "public-anon-key",
      loadSupabase,
      url: "https://project.supabase.co",
    }),
    createCalls,
    getLoadCount: () => loadCount,
    getSignOutCount: () => signOutCount,
    getUnsubscribeCount: () => unsubscribeCount,
    magicLinkRequests,
    oauthRequests,
  };
}

describe("Supabase auth adapter", () => {
  it("loads once on first use and configures a persistent PKCE browser client", async () => {
    const harness = createAdapterHarness();
    expect(harness.getLoadCount()).toBe(0);

    const session = await harness.client.getSession();
    await harness.client.getSession();

    expect(harness.getLoadCount()).toBe(1);
    expect(harness.createCalls).toEqual([
      [
        "https://project.supabase.co",
        "public-anon-key",
        {
          auth: {
            autoRefreshToken: true,
            detectSessionInUrl: false,
            flowType: "pkce",
            persistSession: true,
          },
        },
      ],
    ]);
    expect(session).toEqual({
      expiresAt: 2_000_000_000,
      user: {
        avatarUrl: "https://avatars.example/researcher.png",
        displayName: "Video Researcher",
        email: "researcher@example.test",
        id: "user-123",
      },
    });
  });

  it("passes public redirect targets through the auth operations", async () => {
    const harness = createAdapterHarness();
    const redirectTo =
      "https://what912.github.io/VideoScope/auth/callback";

    await harness.client.signInWithMagicLink(
      "maker@example.test",
      redirectTo,
    );
    await harness.client.signInWithGitHub(redirectTo);
    await harness.client.completeCallback(
      new URL(`${redirectTo}?code=public-code`),
    );
    await harness.client.signOut();

    expect(harness.magicLinkRequests).toEqual([
      {
        email: "maker@example.test",
        options: { emailRedirectTo: redirectTo },
      },
    ]);
    expect(harness.oauthRequests).toEqual([
      {
        options: { redirectTo },
        provider: "github",
      },
    ]);
    expect(harness.callbackCodes).toEqual(["public-code"]);
    expect(harness.getSignOutCount()).toBe(1);
  });

  it("rejects malformed callbacks before loading the provider", async () => {
    const harness = createAdapterHarness();

    await expect(
      harness.client.completeCallback(
        new URL("https://what912.github.io/VideoScope/auth/callback"),
      ),
    ).rejects.toThrow("code is missing");
    expect(harness.getLoadCount()).toBe(0);
  });

  it("unsubscribes the registered provider listener during cleanup", async () => {
    const harness = createAdapterHarness();
    await harness.client.getSession();

    const unsubscribe = harness.client.onSessionChange(() => undefined);
    await new Promise((resolve) => setTimeout(resolve, 0));
    unsubscribe();

    expect(harness.getUnsubscribeCount()).toBe(1);
  });

  it("does not leak an unhandled rejection when subscription setup fails", async () => {
    const client = new SupabaseAuthClient({
      anonKey: "public-anon-key",
      loadSupabase: async () => {
        throw new Error("provider module unavailable");
      },
      url: "https://project.supabase.co",
    });

    const unsubscribe = client.onSessionChange(() => undefined);
    await new Promise((resolve) => setTimeout(resolve, 0));

    expect(unsubscribe).not.toThrow();
  });
});
