import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { featuredCaseStudies } from "../../data/case-studies";
import { I18nProvider } from "../../i18n/I18nProvider";
import publicFunnelCopy from "../../../public/growth-home-copy.json";
import { HomeCaseEvidence } from "./HomeCaseEvidence";

function renderEvidence(loader: () => Promise<typeof featuredCaseStudies>) {
  return render(
    <I18nProvider initialLocale="en">
      <MemoryRouter>
        <HomeCaseEvidence copy={publicFunnelCopy.en.home} loadCases={loader} />
      </MemoryRouter>
    </I18nProvider>,
  );
}

describe("HomeCaseEvidence", () => {
  it("announces a local case-data loading state before rendering the verified proof", async () => {
    let resolveCases: (cases: typeof featuredCaseStudies) => void = () => undefined;
    const loadCases = vi.fn(
      () => new Promise<typeof featuredCaseStudies>((resolve) => {
        resolveCases = resolve;
      }),
    );

    renderEvidence(loadCases);

    expect(screen.getByRole("status")).toHaveTextContent("Loading verified case evidence");

    resolveCases(featuredCaseStudies);

    expect(await screen.findByTestId("featured-case-comparison")).toBeVisible();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});
