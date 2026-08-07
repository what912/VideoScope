import { isRouteErrorResponse, useRouteError } from "react-router";

import { ErrorState } from "../components/feedback/ErrorState";

export function AppErrorBoundary() {
  const error = useRouteError();
  const message = isRouteErrorResponse(error)
    ? error.statusText || undefined
    : undefined;

  return (
    <main>
      <ErrorState message={message} />
    </main>
  );
}
