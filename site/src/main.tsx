import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AppProviders } from "./app/AppProviders";
import { AppRouter } from "./app/router";
import "./styles/tokens.css";
import "./styles/globals.css";
import "./styles/print.css";

export const pagesRouteStorageKey = "videoscope:pages-route";

interface PagesRoutePayload {
  hash: string;
  origin: string;
  pathname: string;
  search: string;
}

function isPagesRoutePayload(value: unknown): value is PagesRoutePayload {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.origin === "string" &&
    typeof candidate.pathname === "string" &&
    typeof candidate.search === "string" &&
    typeof candidate.hash === "string"
  );
}

export function restorePagesRouteBeforeRouter(target: Window = window) {
  let serialized: string | null;
  try {
    serialized = target.sessionStorage.getItem(pagesRouteStorageKey);
    if (serialized === null) {
      return false;
    }
    target.sessionStorage.removeItem(pagesRouteStorageKey);
  } catch {
    return false;
  }

  try {
    const payload: unknown = JSON.parse(serialized);
    if (!isPagesRoutePayload(payload)) {
      return false;
    }
    if (
      payload.origin !== target.location.origin ||
      !payload.pathname.startsWith("/VideoScope/") ||
      (payload.search !== "" && !payload.search.startsWith("?")) ||
      (payload.hash !== "" && !payload.hash.startsWith("#"))
    ) {
      return false;
    }

    const restored = new URL(
      `${payload.pathname}${payload.search}${payload.hash}`,
      target.location.origin,
    );
    if (
      restored.origin !== target.location.origin ||
      restored.pathname !== payload.pathname ||
      restored.search !== payload.search ||
      restored.hash !== payload.hash ||
      !restored.pathname.startsWith("/VideoScope/")
    ) {
      return false;
    }

    target.history.replaceState(
      null,
      "",
      `${restored.pathname}${restored.search}${restored.hash}`,
    );
    return true;
  } catch {
    return false;
  }
}

restorePagesRouteBeforeRouter();

const rootElement = document.getElementById("root");
if (rootElement !== null) {
  createRoot(rootElement).render(
    <StrictMode>
      <AppProviders>
        <AppRouter />
      </AppProviders>
    </StrictMode>,
  );
}
