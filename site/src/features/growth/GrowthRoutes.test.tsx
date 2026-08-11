import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { TestApp } from "../../app/router";
import { findCaseStudy } from "../../data/case-studies";

const timelineRescue = findCaseStudy("timeline-rescue");

if (!timelineRescue) {
  throw new Error("Expected the timeline-rescue case study fixture");
}

describe("public growth routes", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it.each([
    "/rescue",
    "/examples",
    "/examples/timeline-rescue",
    "/download",
    "/developers",
    "/roadmap",
    "/community",
  ])("renders %s without authentication", async (path) => {
    render(<TestApp initialEntries={[path]} />);

    expect(await screen.findByRole("main")).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", {
        name: "This route is outside the observatory",
      }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Created by what912")).toBeInTheDocument();
    expect(screen.queryByText(/sign in to continue/iu)).not.toBeInTheDocument();
  });

  it("keeps attribution unchanged after locale switch", async () => {
    render(<TestApp initialEntries={["/rescue"]} />);

    const language = await screen.findByRole("combobox", { name: "Language" });
    fireEvent.change(language, { target: { value: "zh-CN" } });

    expect(screen.getByText("Created by what912")).toBeInTheDocument();
  });

  it.each(["en", "zh-CN"] as const)(
    "renders localized manifest evidence for timeline-rescue in %s",
    async (locale) => {
      render(<TestApp initialEntries={["/examples/timeline-rescue"]} />);

      if (locale === "zh-CN") {
        fireEvent.change(
          await screen.findByRole("combobox", { name: "Language" }),
          { target: { value: locale } },
        );
      }

      expect(screen.getByText(timelineRescue.provenance)).toBeVisible();
      expect(screen.getByText(timelineRescue.authorizationSummary[locale])).toBeVisible();
      for (const action of timelineRescue.actions) {
        const expectedCount = timelineRescue.actions.filter(
          (candidate) => candidate.description[locale] === action.description[locale],
        ).length;
        expect(screen.getAllByText(action.description[locale])).toHaveLength(expectedCount);
      }
      expect(screen.getByText(timelineRescue.verification.status)).toBeVisible();
      for (const check of timelineRescue.verification.checks) {
        expect(screen.getByText(check.summary[locale])).toBeVisible();
      }
      for (const limitation of timelineRescue.limitations) {
        expect(screen.getByText(limitation[locale])).toBeVisible();
      }
    },
  );
});
