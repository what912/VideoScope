import type { AuthClient, AuthSession } from "../../types/auth";

type FakeAuthOperation =
  | "getSession"
  | "magicLink"
  | "github"
  | "callback"
  | "signOut";

interface FakeAuthClientOptions {
  initialSession?: AuthSession | null;
  sessionMode?: "resolved" | "pending";
  failures?: Partial<Record<FakeAuthOperation, Error>>;
}

export class FakeAuthClient implements AuthClient {
  readonly callbackUrls: URL[] = [];
  readonly githubRedirects: string[] = [];
  readonly magicLinkRequests: Array<{ email: string; redirectTo: string }> = [];
  signOutCount = 0;

  readonly #failures: Partial<Record<FakeAuthOperation, Error>>;
  readonly #listeners = new Set<(session: AuthSession | null) => void>();
  readonly #sessionMode: "resolved" | "pending";
  #session: AuthSession | null;

  constructor(options: FakeAuthClientOptions = {}) {
    this.#session = options.initialSession ?? null;
    this.#sessionMode = options.sessionMode ?? "resolved";
    this.#failures = options.failures ?? {};
  }

  async getSession(): Promise<AuthSession | null> {
    this.#throwFailure("getSession");
    if (this.#sessionMode === "pending") {
      return new Promise<AuthSession | null>(() => undefined);
    }
    return this.#session;
  }

  onSessionChange(callback: (session: AuthSession | null) => void) {
    this.#listeners.add(callback);
    return () => this.#listeners.delete(callback);
  }

  async signInWithMagicLink(
    email: string,
    redirectTo: string,
  ): Promise<void> {
    this.#throwFailure("magicLink");
    this.magicLinkRequests.push({ email, redirectTo });
  }

  async signInWithGitHub(redirectTo: string): Promise<void> {
    this.#throwFailure("github");
    this.githubRedirects.push(redirectTo);
  }

  async completeCallback(url: URL): Promise<void> {
    this.#throwFailure("callback");
    this.callbackUrls.push(url);
  }

  async signOut(): Promise<void> {
    this.#throwFailure("signOut");
    this.signOutCount += 1;
    this.#session = null;
    this.#emit();
  }

  setSession(session: AuthSession | null) {
    this.#session = session;
    this.#emit();
  }

  #emit() {
    for (const listener of this.#listeners) {
      listener(this.#session);
    }
  }

  #throwFailure(operation: FakeAuthOperation) {
    const failure = this.#failures[operation];
    if (failure) {
      throw failure;
    }
  }
}
