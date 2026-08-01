import type { PropsWithChildren } from "react";

import {
  AuthProvider,
} from "../features/auth/AuthProvider";
import { I18nProvider } from "../i18n/I18nProvider";
import type { Locale } from "../i18n/types";
import { createAuthClient } from "../services/auth";
import type { AuthClient } from "../types/auth";
import { OnlineStatusProvider } from "../hooks/useOnlineStatus";

const defaultAuthClient = createAuthClient();

type AppProvidersProps = PropsWithChildren<{
  authClient?: AuthClient;
  initialLocale?: Locale;
  online?: boolean;
}>;

export function AppProviders({
  authClient = defaultAuthClient,
  children,
  initialLocale,
  online,
}: AppProvidersProps) {
  return (
    <I18nProvider initialLocale={initialLocale}>
      <OnlineStatusProvider online={online}>
        <AuthProvider client={authClient}>{children}</AuthProvider>
      </OnlineStatusProvider>
    </I18nProvider>
  );
}
