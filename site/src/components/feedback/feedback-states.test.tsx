import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/AppProviders";
import { EmptyState } from "./EmptyState";
import { ErrorState } from "./ErrorState";
import { LoadingState } from "./LoadingState";

describe("shared feedback states", () => {
  it("renders dictionary-backed empty and loading semantics", () => {
    render(
      <AppProviders>
        <EmptyState />
        <LoadingState />
      </AppProviders>,
    );

    expect(
      screen.getByRole("region", { name: "Empty state" }),
    ).toHaveTextContent("Nothing to review yet");
    expect(screen.getByRole("status", { name: "Loading" })).toHaveTextContent(
      "Preparing the observatory",
    );
  });

  it("announces errors and offers the localized retry action", () => {
    const onRetry = vi.fn();

    render(
      <AppProviders>
        <ErrorState onRetry={onRetry} />
      </AppProviders>,
    );

    expect(screen.getByRole("alert", { name: "Error" })).toHaveTextContent(
      "Something went wrong",
    );
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});
