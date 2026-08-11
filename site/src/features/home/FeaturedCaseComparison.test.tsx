import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { featuredCaseStudies } from "../../data/case-studies";
import { I18nProvider } from "../../i18n/I18nProvider";
import publicFunnelCopy from "../../../public/growth-home-copy.json";
import { FeaturedCaseComparison } from "./FeaturedCaseComparison";

function renderComparison() {
  return render(
    <I18nProvider initialLocale="en">
      <FeaturedCaseComparison copy={publicFunnelCopy.en.home.comparison} item={featuredCaseStudies[0]} />
    </I18nProvider>,
  );
}

describe("FeaturedCaseComparison", () => {
  it("synchronizes the before and after video positions", () => {
    renderComparison();

    fireEvent.timeUpdate(screen.getByLabelText(/before/i), {
      target: { currentTime: 4.2 },
    });

    expect(
      (screen.getByLabelText(/after/i) as HTMLVideoElement).currentTime,
    ).toBeCloseTo(4.2, 1);
    expect(screen.getByLabelText("Comparison position")).toHaveValue("4.2");
  });

  it("uses the manifest-bound media and states the case limitations", () => {
    renderComparison();

    expect(screen.getByLabelText(/before/i)).toHaveAttribute(
      "src",
      featuredCaseStudies[0].assets.beforeVideo,
    );
    expect(screen.getByLabelText(/after/i)).toHaveAttribute(
      "src",
      featuredCaseStudies[0].assets.afterVideo,
    );
    expect(screen.getByText("Project-authored demonstration")).toBeVisible();
    expect(screen.getByText(featuredCaseStudies[0].limitations[0].en)).toBeVisible();
  });

  it("offers keyboard-operable shared playback", () => {
    const play = vi
      .spyOn(HTMLMediaElement.prototype, "play")
      .mockResolvedValue(undefined);
    renderComparison();

    fireEvent.click(screen.getByRole("button", { name: "Play comparison" }));

    expect(play).toHaveBeenCalledTimes(2);
  });
});
