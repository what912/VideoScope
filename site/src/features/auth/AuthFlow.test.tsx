import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { describe, expect, it } from "vitest";

import { AppProviders } from "../../app/AppProviders";
import {
  createAuthClient,
  FakeAuthClient,
  UnavailableAuthClient,
} from "../../services/auth";
import type { AuthSession } from "../../types/auth";
import { AuthCallbackPage } from "./AuthCallbackPage";
import { AuthPage } from "./AuthPage";
import { useAuth } from "./AuthProvider";
import { buildAuthCallbackUrl } from "./callback-url";

const RESTORED_SESSION: AuthSession = {
  expiresAt: 2_000_000_000,
  user: {
    avatarUrl: "https://avatars.example/account.png",
    displayName: "Video Researcher",
    email: "researcher@example.test",
    id: "user-123",
  },
};

function renderAuthPage(
  client: FakeAuthClient | UnavailableAuthClient,
  initialLocale: "en" | "zh-CN" = "en",
  online?: boolean,
) {
  return render(
    <AppProviders
      authClient={client}
      initialLocale={initialLocale}
      online={online}
    >
      <MemoryRouter initialEntries={["/auth"]}>
        <AuthPage />
      </MemoryRouter>
    </AppProviders>,
  );
}

function AuthActionProbe() {
  const auth = useAuth();
  return (
    <div>
      <output aria-label="Magic link state">
        {auth.magicLinkSent ? "sent" : "clear"}
      </output>
      <button
        onClick={() =>
          void auth.signInWithMagicLink(
            "maker@example.test",
            "https://example.test/auth/callback",
          )
        }
        type="button"
      >
        Request link
      </button>
      <button
        onClick={() =>
          void auth.signInWithGitHub("https://example.test/auth/callback")
        }
        type="button"
      >
        Start GitHub
      </button>
      <button
        onClick={() =>
          void auth.completeCallback(
            new URL("https://example.test/auth/callback?code=public-code"),
          )
        }
        type="button"
      >
        Complete callback
      </button>
      <button onClick={() => void auth.signOut()} type="button">
        Sign out probe
      </button>
    </div>
  );
}

describe("optional authentication", () => {
  it.each([
    [{ anonKey: "", url: "https://project.supabase.co" }],
    [{ anonKey: "public-anon-key", url: "" }],
    [{ anonKey: undefined, url: undefined }],
  ])(
    "uses an unavailable adapter when either public setting is missing",
    async (environment) => {
      const client = createAuthClient(environment);

      expect(client).toBeInstanceOf(UnavailableAuthClient);
      await expect(client.getSession()).resolves.toBeNull();
    },
  );

  it("builds the production callback from the configured application base", () => {
    expect(
      buildAuthCallbackUrl("https://what912.github.io", "/VideoScope/"),
    ).toBe("https://what912.github.io/VideoScope/auth/callback");
  });

  it("starts anonymously and keeps local analysis available", async () => {
    renderAuthPage(new FakeAuthClient({ initialSession: null }));

    expect(
      await screen.findByRole("heading", { name: "Optional account" }),
    ).toBeVisible();
    expect(screen.getByText("You are using VideoScope anonymously.")).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Analyze anonymously" }),
    ).toHaveAttribute("href", "/workspace");
  });

  it("restores a saved session without making analysis depend on it", async () => {
    renderAuthPage(new FakeAuthClient({ initialSession: RESTORED_SESSION }));

    expect(await screen.findByText("researcher@example.test")).toBeVisible();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
    expect(
      screen.getByRole("link", { name: "Analyze a video" }),
    ).toHaveAttribute("href", "/workspace");
  });

  it("requests a magic link with the base-aware callback", async () => {
    const client = new FakeAuthClient({ initialSession: null });
    renderAuthPage(client);

    fireEvent.change(await screen.findByLabelText("Email address"), {
      target: { value: "maker@example.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Email me a sign-in link" }));

    expect(await screen.findByText("Check your email for the sign-in link.")).toBeVisible();
    expect(client.magicLinkRequests).toEqual([
      {
        email: "maker@example.test",
        redirectTo: "http://localhost:3000/auth/callback",
      },
    ]);
    expect(
      screen.getByRole("link", { name: "Analyze anonymously" }),
    ).toBeVisible();
  });

  it("clears an old magic-link success when the email or provider changes", async () => {
    const client = new FakeAuthClient({ initialSession: null });
    renderAuthPage(client);

    const email = await screen.findByLabelText("Email address");
    fireEvent.change(email, { target: { value: "first@example.test" } });
    fireEvent.click(screen.getByRole("button", { name: "Email me a sign-in link" }));
    expect(await screen.findByText("Check your email for the sign-in link.")).toBeVisible();

    fireEvent.change(email, { target: { value: "second@example.test" } });
    expect(
      screen.queryByText("Check your email for the sign-in link."),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Email me a sign-in link" }));
    expect(await screen.findByText("Check your email for the sign-in link.")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Continue with GitHub" }));
    expect(
      screen.queryByText("Check your email for the sign-in link."),
    ).not.toBeInTheDocument();
  });

  it("clears an old magic-link success before callback and sign-out operations", async () => {
    const client = new FakeAuthClient({ initialSession: null });
    render(
      <AppProviders authClient={client} initialLocale="en">
        <MemoryRouter>
          <AuthActionProbe />
        </MemoryRouter>
      </AppProviders>,
    );

    await screen.findByText("clear");
    fireEvent.click(screen.getByRole("button", { name: "Request link" }));
    expect(await screen.findByText("sent")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Complete callback" }));
    expect(await screen.findByText("clear")).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: "Request link" }));
    expect(await screen.findByText("sent")).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: "Sign out probe" }));
    expect(await screen.findByText("clear")).toBeVisible();
  });

  it("starts GitHub OAuth with the same base-aware callback", async () => {
    const client = new FakeAuthClient({ initialSession: null });
    renderAuthPage(client);

    fireEvent.click(
      await screen.findByRole("button", { name: "Continue with GitHub" }),
    );

    await waitFor(() => {
      expect(client.githubRedirects).toEqual([
        "http://localhost:3000/auth/callback",
      ]);
    });
  });

  it("disables network sign-in while offline and recovers after the online event", async () => {
    const client = new FakeAuthClient({ initialSession: null });
    const view = renderAuthPage(client, "en", false);

    expect(
      await screen.findByRole("status", { name: "Sign-in is offline" }),
    ).toHaveTextContent(
      "Reconnect before using email or GitHub sign-in. Local analysis remains available.",
    );
    const emailButton = screen.getByRole("button", {
      name: "Email me a sign-in link",
    });
    const githubButton = screen.getByRole("button", {
      name: "Continue with GitHub",
    });
    expect(emailButton).toBeDisabled();
    expect(githubButton).toBeDisabled();
    fireEvent.click(emailButton);
    fireEvent.click(githubButton);
    expect(client.magicLinkRequests).toHaveLength(0);
    expect(client.githubRedirects).toHaveLength(0);

    view.rerender(
      <AppProviders authClient={client} initialLocale="en" online>
        <MemoryRouter initialEntries={["/auth"]}>
          <AuthPage />
        </MemoryRouter>
      </AppProviders>,
    );
    expect(await screen.findByRole("button", {
      name: "Continue with GitHub",
    })).toBeEnabled();
  });

  it("completes the callback without exposing provider details", async () => {
    const client = new FakeAuthClient({ initialSession: null });

    render(
      <AppProviders authClient={client} initialLocale="en">
        <MemoryRouter initialEntries={["/auth/callback?code=public-code#state"]}>
          <Routes>
            <Route path="/auth/callback" element={<AuthCallbackPage />} />
          </Routes>
        </MemoryRouter>
      </AppProviders>,
    );

    expect(
      await screen.findByRole("heading", { name: "Sign-in complete" }),
    ).toBeVisible();
    expect(client.callbackUrls.map(String)).toEqual([
      "http://localhost:3000/auth/callback?code=public-code#state",
    ]);
    expect(
      screen.getByRole("link", { name: "Continue to analysis" }),
    ).toHaveAttribute("href", "/workspace");
  });

  it("signs out and returns to anonymous use", async () => {
    const client = new FakeAuthClient({ initialSession: RESTORED_SESSION });
    renderAuthPage(client);

    fireEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(
      await screen.findByText("You are using VideoScope anonymously."),
    ).toBeVisible();
    expect(client.signOutCount).toBe(1);
    expect(
      screen.getByRole("link", { name: "Analyze anonymously" }),
    ).toBeVisible();
  });

  it("localizes provider failures without rendering raw error details", async () => {
    const client = new FakeAuthClient({
      failures: {
        magicLink: new Error(
          "fetch failed at C:\\Users\\private\\project with service token secret",
        ),
      },
      initialSession: null,
    });
    renderAuthPage(client, "zh-CN");

    fireEvent.change(await screen.findByLabelText("电子邮箱"), {
      target: { value: "maker@example.test" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送登录链接" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("无法发送登录链接，请稍后重试。");
    expect(alert).not.toHaveTextContent("C:\\Users");
    expect(alert).not.toHaveTextContent("secret");
    expect(screen.getByRole("link", { name: "匿名分析" })).toBeVisible();
  });

  it.each([
    ["loading", new FakeAuthClient({ sessionMode: "pending" })],
    ["unavailable", new UnavailableAuthClient()],
    ["anonymous", new FakeAuthClient({ initialSession: null })],
    ["authenticated", new FakeAuthClient({ initialSession: RESTORED_SESSION })],
    [
      "session error",
      new FakeAuthClient({
        failures: { getSession: new Error("private provider failure") },
      }),
    ],
  ])("keeps anonymous analysis enabled while auth is %s", async (_, client) => {
    renderAuthPage(client);

    expect(
      await screen.findByRole("link", {
        name: /Analyze (?:a video|anonymously)/,
      }),
    ).toHaveAttribute("href", "/workspace");
  });
});
