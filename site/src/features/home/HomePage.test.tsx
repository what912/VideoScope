import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AppProviders } from "../../app/AppProviders";
import { TestApp } from "../../app/router";
import manifest from "../../data/case-studies.json";
import publicFunnelCopy from "../../../public/growth-home-copy.json";
import { HomePage } from "./HomePage";

function renderHome() {
  return render(
    <AppProviders>
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>
    </AppProviders>,
  );
}

describe("result-led homepage", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
      ok: true,
      json: async () => url.endsWith("growth-home-copy.json") ? publicFunnelCopy : manifest,
    })));
  });

  it("shows verified proof before the browser check", async () => {
    renderHome();

    const proof = await screen.findByTestId(
      "featured-case-comparison",
      undefined,
      { timeout: 6_000 },
    );
    const browserCheck = screen.getByTestId("home-upload-lab");
    expect(
      proof.compareDocumentPosition(browserCheck) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("uses one creator-facing primary action", async () => {
    renderHome();

    const primary = await screen.findByRole("link", {
      name: /rescue.*publish/i,
    });
    expect(primary).toHaveAttribute("href", "/rescue");
    expect(screen.getAllByRole("link", { name: /rescue.*publish/i })).toHaveLength(1);
  });

  it("keeps a browser-only quick check available without replacing Rescue", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    renderHome();

    fireEvent.click(await screen.findByRole("button", { name: "Run a quick browser check" }));

    expect(screen.getByTestId("home-upload-lab")).toHaveFocus();
    expect(scrollIntoView).toHaveBeenCalledWith({
      behavior: "smooth",
      block: "start",
    });
  });

  it("keeps the public funnel bilingual while preserving its attribution", async () => {
    render(<TestApp initialEntries={["/"]} />);

    fireEvent.change(await screen.findByRole("combobox", { name: "Language" }), {
      target: { value: "zh-CN" },
    });

    expect(await screen.findByRole("link", { name: /抢救.*发布/i })).toHaveAttribute(
      "href",
      "/rescue",
    );
    expect(screen.getAllByText("Created by what912").some((element) => {
      return element.classList.contains("funnel-attribution") ||
        element.parentElement?.classList.contains("funnel-attribution");
    })).toBe(true);
  });

  it("announces unavailable local funnel copy instead of rendering a stale funnel", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({}),
    }));

    renderHome();

    expect(await screen.findByText(
      /Local page content is unavailable\.|本地页面内容当前不可用。/u,
    )).toHaveTextContent(
      /Local page content is unavailable\.|本地页面内容当前不可用。/u,
    );
  });
});
