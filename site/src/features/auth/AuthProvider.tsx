import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

import { isUnavailableAuthClient } from "../../services/auth";
import type {
  AuthClient,
  AuthSession,
  AuthStatus,
} from "../../types/auth";

export type AuthErrorCode =
  | "session"
  | "magic_link"
  | "github"
  | "callback"
  | "sign_out";

interface AuthContextValue {
  client: AuthClient;
  error: AuthErrorCode | null;
  magicLinkSent: boolean;
  session: AuthSession | null;
  status: AuthStatus;
  working: boolean;
  clearError(): void;
  clearMagicLinkNotice(): void;
  completeCallback(url: URL): Promise<boolean>;
  signInWithGitHub(redirectTo: string): Promise<void>;
  signInWithMagicLink(email: string, redirectTo: string): Promise<void>;
  signOut(): Promise<void>;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

interface AuthProviderProps extends PropsWithChildren {
  client: AuthClient;
}

function statusForSession(session: AuthSession | null): AuthStatus {
  return session ? "authenticated" : "anonymous";
}

export function AuthProvider({ children, client }: AuthProviderProps) {
  const unavailable = isUnavailableAuthClient(client);
  const [session, setSession] = useState<AuthSession | null>(null);
  const [status, setStatus] = useState<AuthStatus>(
    unavailable ? "unavailable" : "loading",
  );
  const [error, setError] = useState<AuthErrorCode | null>(null);
  const [working, setWorking] = useState(false);
  const [magicLinkSent, setMagicLinkSent] = useState(false);

  useEffect(() => {
    if (unavailable) {
      setSession(null);
      setStatus("unavailable");
      return;
    }

    let active = true;
    const unsubscribe = client.onSessionChange((nextSession) => {
      if (!active) {
        return;
      }
      setSession(nextSession);
      setError(null);
      setMagicLinkSent(false);
      setStatus(statusForSession(nextSession));
    });

    void client
      .getSession()
      .then((nextSession) => {
        if (!active) {
          return;
        }
        setSession(nextSession);
        setStatus(statusForSession(nextSession));
      })
      .catch(() => {
        if (!active) {
          return;
        }
        setSession(null);
        setError("session");
        setStatus("error");
      });

    return () => {
      active = false;
      unsubscribe();
    };
  }, [client, unavailable]);

  const clearError = useCallback(() => setError(null), []);
  const clearMagicLinkNotice = useCallback(
    () => setMagicLinkSent(false),
    [],
  );

  const signInWithMagicLink = useCallback(
    async (email: string, redirectTo: string) => {
      setWorking(true);
      setError(null);
      setMagicLinkSent(false);
      try {
        await client.signInWithMagicLink(email, redirectTo);
        setMagicLinkSent(true);
      } catch {
        setError("magic_link");
      } finally {
        setWorking(false);
      }
    },
    [client],
  );

  const signInWithGitHub = useCallback(
    async (redirectTo: string) => {
      setWorking(true);
      setError(null);
      setMagicLinkSent(false);
      try {
        await client.signInWithGitHub(redirectTo);
      } catch {
        setError("github");
      } finally {
        setWorking(false);
      }
    },
    [client],
  );

  const completeCallback = useCallback(
    async (url: URL) => {
      setWorking(true);
      setError(null);
      setMagicLinkSent(false);
      try {
        await client.completeCallback(url);
        return true;
      } catch {
        setError("callback");
        return false;
      } finally {
        setWorking(false);
      }
    },
    [client],
  );

  const signOut = useCallback(async () => {
    setWorking(true);
    setError(null);
    setMagicLinkSent(false);
    try {
      await client.signOut();
      setSession(null);
      setStatus("anonymous");
    } catch {
      setError("sign_out");
    } finally {
      setWorking(false);
    }
  }, [client]);

  const value = useMemo<AuthContextValue>(
    () => ({
      clearError,
      clearMagicLinkNotice,
      client,
      completeCallback,
      error,
      magicLinkSent,
      session,
      signInWithGitHub,
      signInWithMagicLink,
      signOut,
      status,
      working,
    }),
    [
      clearError,
      clearMagicLinkNotice,
      client,
      completeCallback,
      error,
      magicLinkSent,
      session,
      signInWithGitHub,
      signInWithMagicLink,
      signOut,
      status,
      working,
    ],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) {
    throw new Error("useAuth must be used within AuthProvider");
  }
  return value;
}
