export interface AuthUser {
  id: string;
  email?: string;
  displayName?: string;
  avatarUrl?: string;
}

export interface AuthSession {
  user: AuthUser;
  expiresAt?: number;
}

export interface AuthClient {
  getSession(): Promise<AuthSession | null>;
  onSessionChange(
    callback: (session: AuthSession | null) => void,
  ): () => void;
  signInWithMagicLink(email: string, redirectTo: string): Promise<void>;
  signInWithGitHub(redirectTo: string): Promise<void>;
  completeCallback(url: URL): Promise<void>;
  signOut(): Promise<void>;
}

export type AuthStatus =
  | "loading"
  | "unavailable"
  | "anonymous"
  | "authenticated"
  | "error";
