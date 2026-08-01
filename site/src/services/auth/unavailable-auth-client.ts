import type { AuthClient } from "../../types/auth";

export class AuthUnavailableError extends Error {
  constructor() {
    super("Optional authentication is not configured.");
    this.name = "AuthUnavailableError";
  }
}

export class UnavailableAuthClient implements AuthClient {
  async getSession(): Promise<null> {
    return null;
  }

  onSessionChange() {
    return () => undefined;
  }

  async signInWithMagicLink(): Promise<void> {
    throw new AuthUnavailableError();
  }

  async signInWithGitHub(): Promise<void> {
    throw new AuthUnavailableError();
  }

  async completeCallback(): Promise<void> {
    throw new AuthUnavailableError();
  }

  async signOut(): Promise<void> {
    throw new AuthUnavailableError();
  }
}

export function isUnavailableAuthClient(
  client: AuthClient,
): client is UnavailableAuthClient {
  return client instanceof UnavailableAuthClient;
}
