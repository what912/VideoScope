import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { I18nProvider } from "../../i18n/I18nProvider";
import { MemoryReportStore } from "../../services/report-store/memory-report-store";
import { HomeUploadLab } from "./HomeUploadLab";

describe("HomeUploadLab", () => {
  it("waits for the shared browser report store before enabling analysis", async () => {
    let resolveStore:
      | ((value: {
          store: MemoryReportStore;
          storage: "memory";
          warning: string | null;
        }) => void)
      | undefined;
    const storeResolution = new Promise<{
      store: MemoryReportStore;
      storage: "memory";
      warning: string | null;
    }>((resolve) => {
      resolveStore = resolve;
    });

    render(
      <MemoryRouter>
        <I18nProvider initialLocale="en">
          <HomeUploadLab resolveReportStore={() => storeResolution} />
        </I18nProvider>
      </MemoryRouter>,
    );

    expect(screen.getByText("Preparing the observatory")).toBeVisible();
    resolveStore?.({
      store: new MemoryReportStore(),
      storage: "memory",
      warning: null,
    });
    expect(
      await screen.findByRole("heading", {
        name: "Inspect a video without uploading it",
      }),
    ).toBeVisible();
  });
});
